import pytest

from trading_bot.options.spreads import Leg, Spread
from trading_bot.tournament.decision import (
    CandidateEvidence,
    EntryCandidate,
    TournamentLimits,
    plan_entries,
)


def evidence(**changes):
    values = dict(catalyst_strength=0.75, tape_confirmation=0.75,
                  surface_lag=0.60, model_agreement=2 / 3,
                  spread_capture=0.80)
    values.update(changes)
    return CandidateEvidence(**values)


def spread(*, debit=2.0, loss=None):
    return Spread("AMD", "call_debit_vertical", "BULLISH",
                  (Leg("AMD260904C00100000", "buy"),
                   Leg("AMD260904C00105000", "sell")),
                  net_credit=-debit, max_loss=loss or debit,
                  max_profit=5.0 - debit, expiry="2026-09-04", dte=4)


def candidate(name="c1", event="e1", ev=None, sp=None, risk_class="adaptive"):
    return EntryCandidate(name, event, sp or spread(), ev or evidence(), risk_class)


def run(**changes):
    values = dict(equity=100_000.0, measured_start_equity=100_000.0,
                  session_start_equity=100_000.0, current_max_loss_usd=0.0,
                  event_exposure_usd={}, candidates=[candidate()])
    values.update(changes)
    return plan_entries(**values)


def test_ordinary_candidate_is_sized_from_exact_max_loss():
    plan = run()
    assert plan.open[0][1] == 20       # 4% of 100k / $200 per contract
    assert plan.added_max_loss_usd == 4_000


def test_exceptional_evidence_earns_the_larger_but_still_bounded_limit():
    ev = evidence(catalyst_strength=0.9, tape_confirmation=0.9,
                  surface_lag=0.75, model_agreement=1.0, spread_capture=0.9)
    plan = run(candidates=[candidate(ev=ev)])
    assert plan.open[0][1] == 40       # 8%, still below 10% event limit
    assert plan.added_max_loss_usd == 8_000


@pytest.mark.parametrize("field,value", [
    ("catalyst_strength", 0.59),
    ("tape_confirmation", 0.59),
    ("surface_lag", 0.44),
    ("model_agreement", 0.65),
    ("spread_capture", 0.64),
    ("stale_mark_penalty", 0.26),
])
def test_each_evidence_gate_has_teeth(field, value):
    plan = run(candidates=[candidate(ev=evidence(**{field: value}))])
    assert plan.open == []
    assert any("evidence gates failed" in note for note in plan.notes)


def test_account_drawdown_halts_all_new_risk_at_the_boundary():
    plan = run(equity=75_000.0)
    assert plan.halted and plan.open == []
    assert "account drawdown halt" in plan.notes[0]


def test_daily_drawdown_halts_all_new_risk_at_the_boundary():
    plan = run(equity=88_000.0, measured_start_equity=80_000.0)
    assert plan.halted and "daily loss halt" in plan.notes[0]


def test_existing_live_risk_reduces_aggregate_room():
    plan = run(current_max_loss_usd=39_000.0)
    assert plan.open[0][1] == 5        # only $1,000 of 40% aggregate room


def test_existing_event_exposure_reduces_event_room():
    plan = run(event_exposure_usd={"e1": 9_600.0})
    assert plan.open[0][1] == 2


def test_credit_structure_cannot_enter_the_convex_v2_path():
    credit = Spread("AMD", "credit", "SELL_VOL",
                    (Leg("a", "sell"), Leg("b", "buy")),
                    net_credit=1.0, max_loss=4.0, max_profit=1.0,
                    expiry="2026-09-04", dte=4)
    plan = run(candidates=[candidate(sp=credit)])
    assert plan.open == []
    assert any("not a debit" in note for note in plan.notes)


def test_invalid_limit_hierarchy_is_rejected():
    with pytest.raises(ValueError):
        TournamentLimits(ordinary_candidate_pct=0.09,
                         exceptional_candidate_pct=0.08)
    with pytest.raises(ValueError):
        TournamentLimits(scheduled_event_pct=0.41)


def test_scheduled_event_straddle_can_use_the_predeclared_convex_budget():
    event_spread = Spread(
        "AVGO", "long_straddle", "BUY_VOL",
        (Leg("AVGO260904C00365000", "buy"),
         Leg("AVGO260904P00365000", "buy")),
        net_credit=-29.90, max_loss=29.90, max_profit=59.80,
        expiry="2026-09-04", dte=7)
    ev = evidence(catalyst_strength=0.9, tape_confirmation=0.9,
                  surface_lag=0.75, model_agreement=1.0, spread_capture=0.9)
    plan = run(candidates=[candidate(
        ev=ev, sp=event_spread, risk_class="scheduled_event")])
    assert plan.open[0][1] == 13
    assert plan.added_max_loss_usd == pytest.approx(38_870)
    assert plan.added_max_loss_usd <= 40_000


def test_scheduled_event_budget_rejects_a_non_straddle():
    ev = evidence(catalyst_strength=0.9, tape_confirmation=0.9,
                  surface_lag=0.75, model_agreement=1.0, spread_capture=0.9)
    plan = run(candidates=[candidate(ev=ev, risk_class="scheduled_event")])
    assert plan.open == []
    assert any("requires a long straddle" in note for note in plan.notes)


def test_scheduled_event_budget_requires_exceptional_evidence():
    event_spread = Spread(
        "AVGO", "long_straddle", "BUY_VOL", (Leg("c", "buy"), Leg("p", "buy")),
        net_credit=-10.0, max_loss=10.0, max_profit=20.0,
        expiry="2026-09-04", dte=7)
    plan = run(candidates=[candidate(sp=event_spread, risk_class="scheduled_event")])
    assert plan.open == []
    assert any("not exceptional" in note for note in plan.notes)
