import datetime as dt

import pytest

from trading_bot.options.clock import ET
from trading_bot.tournament.weekly import (
    CalendarFact,
    EventConsensus,
    EventTiming,
    PortfolioPolicy,
    PromotionDecision,
    ReplaySummary,
    WeeklyWindow,
    allocate_event_risk,
    calendar_consensus,
    evaluate_promotion,
    event_lifecycle,
    schedule_event,
)


WINDOW = WeeklyWindow(
    dt.datetime(2026, 8, 31, 9, 30, tzinfo=ET),
    dt.datetime(2026, 9, 4, 9, 30, tzinfo=ET))
SESSIONS = ["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02",
            "2026-09-03", "2026-09-04"]


def fact(source, *, date="2026-09-02", timing=EventTiming.AFTER_CLOSE):
    return CalendarFact(
        "AVGO", dt.date.fromisoformat(date), timing, source,
        f"{source}:avgo", "AVGO earnings schedule")


def consensus():
    return calendar_consensus([fact("yahoo"), fact("nasdaq")])[0]


def replay(**changes):
    values = dict(
        sample_size=8, last_mean=.47, last_median=.45, last_win_rate=.625,
        adverse_mean=.196, adverse_median=.177, adverse_win_rate=.625,
        premium_median=.07, premium_p75=.08)
    values.update(changes)
    return ReplaySummary(**values)


def schedule():
    return schedule_event(
        consensus(), sessions=SESSIONS,
        expiries=["2026-09-04", "2026-09-11"], window=WINDOW)


def test_two_independent_calendar_sources_confirm_date_and_session():
    row = consensus()
    assert row.confirmed
    assert row.event_date == dt.date(2026, 9, 2)
    assert row.timing is EventTiming.AFTER_CLOSE
    assert row.sources == ("nasdaq", "yahoo")


def test_calendar_conflict_fails_closed_even_when_two_sources_otherwise_agree():
    row = calendar_consensus([
        fact("yahoo"), fact("nasdaq"),
        fact("third", date="2026-09-03")])[0]
    assert not row.confirmed
    assert any("conflicting source" in reason for reason in row.reasons)


def test_same_vendor_cannot_manufacture_a_calendar_quorum():
    row = calendar_consensus([
        fact("yahoo"), CalendarFact(
            "AVGO", dt.date(2026, 9, 2), EventTiming.AFTER_CLOSE,
            "yahoo", "yahoo:second", "same vendor endpoint")])[0]
    assert not row.confirmed


def test_after_close_schedule_uses_same_close_and_next_exchange_morning():
    row = schedule()
    assert row.entry_start == dt.datetime(2026, 9, 2, 15, 20, tzinfo=ET)
    assert row.exit_at == dt.datetime(2026, 9, 3, 9, 45, tzinfo=ET)
    assert row.expiry == "2026-09-04"


def test_before_open_schedule_uses_previous_exchange_close():
    c = EventConsensus(
        "PANW", dt.date(2026, 9, 1), EventTiming.BEFORE_OPEN, "earnings",
        ("nasdaq", "yahoo"), ("a", "b"), True, ("confirmed",))
    row = schedule_event(
        c, sessions=SESSIONS, expiries=["2026-09-04"], window=WINDOW)
    assert row.entry_start.date() == dt.date(2026, 8, 31)
    assert row.exit_at.date() == dt.date(2026, 9, 1)


def test_event_without_a_pre_deadline_exit_is_excluded():
    c = EventConsensus(
        "LULU", dt.date(2026, 9, 3), EventTiming.AFTER_CLOSE, "earnings",
        ("nasdaq", "yahoo"), ("a", "b"), True, ("confirmed",))
    with pytest.raises(ValueError, match="deadline"):
        schedule_event(c, sessions=SESSIONS, expiries=["2026-09-04"], window=WINDOW)


def test_promotion_requires_every_frozen_evidence_layer():
    decision = evaluate_promotion(
        consensus=consensus(), semantic_confirmed=True, replay=replay(),
        schedule=schedule(), current_premium_to_spot=.075,
        current_total_spread_pct=.03)
    assert decision.promoted and decision.conservative_edge == pytest.approx(.177)


def test_weekend_planning_can_defer_but_never_waive_the_live_surface_gate():
    decision = evaluate_promotion(
        consensus=consensus(), semantic_confirmed=True, replay=replay(),
        schedule=schedule(), current_premium_to_spot=None,
        current_total_spread_pct=None, require_current_surface=False)
    assert decision.promoted
    live = evaluate_promotion(
        consensus=consensus(), semantic_confirmed=True, replay=replay(),
        schedule=schedule(), current_premium_to_spot=.09,
        current_total_spread_pct=.03)
    assert not live.promoted


@pytest.mark.parametrize("change,reason", [
    ({"semantic_confirmed": False}, "Featherless"),
    ({"replay": replay(adverse_median=-.01)}, "adverse-envelope median"),
    ({"current_premium_to_spot": .09}, "absolute gate"),
    ({"current_total_spread_pct": .051}, "liquidity gate"),
])
def test_promotion_failure_reasons_are_auditable(change, reason):
    values = dict(
        consensus=consensus(), semantic_confirmed=True, replay=replay(),
        schedule=schedule(), current_premium_to_spot=.075,
        current_total_spread_pct=.03)
    values.update(change)
    decision = evaluate_promotion(**values)
    assert not decision.promoted
    assert any(reason in value for value in decision.reasons)


def test_portfolio_allocates_singleton_budget_and_caps_overlaps():
    one = PromotionDecision("AVGO", True, .18, ("pass",))
    assert allocate_event_risk([one], equity=100_000) == {"AVGO": 25_000}
    rows = [one, PromotionDecision("SNOW", True, .09, ("pass",)),
            PromotionDecision("PANW", True, .03, ("pass",))]
    allocation = allocate_event_risk(rows, equity=100_000)
    assert sum(allocation.values()) == pytest.approx(25_000)
    assert all(value <= 12_500 for value in allocation.values())
    assert allocation["AVGO"] >= allocation["SNOW"] >= allocation["PANW"]


def test_event_lifecycle_keeps_deadline_exit_above_every_other_guard():
    row = schedule()
    assert event_lifecycle(
        now=row.entry_start, schedule=row, has_position=False,
        entry_was_attempted=False, global_deadline=WINDOW.deadline) == "ENTER"
    assert event_lifecycle(
        now=row.exit_at, schedule=row, has_position=True,
        entry_was_attempted=True, global_deadline=WINDOW.deadline) == "EXIT"
    assert event_lifecycle(
        now=WINDOW.deadline, schedule=row, has_position=True,
        entry_was_attempted=True, global_deadline=WINDOW.deadline) == "EXIT"
