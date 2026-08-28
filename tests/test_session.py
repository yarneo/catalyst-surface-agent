"""Session planning tests, built to the audit's standard.

The previous suite passed while the ruin constraint, the EV floor and both cost
gates could each be deleted silently — because the fixtures were comfortable
enough that the guards never activated. Here every guard gets a fixture that
genuinely breaches it, and no assertion contains `or`.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from trading_bot.options.clock import ET, flat_by
from trading_bot.options.condor import Condor
from trading_bot.options.session import Holding, SessionPlan, plan_session
from trading_bot.options.spreads import Leg

IL = ZoneInfo("Asia/Jerusalem")
START = dt.datetime(2026, 8, 31, 9, 30, tzinfo=ET)
DEADLINE = dt.datetime(2026, 9, 4, 9, 30, tzinfo=ET)
MID = dt.datetime(2026, 8, 31, 12, 0, tzinfo=ET)      # Monday, market open


def sym(k, right, exp="260903"):
    return f"SPY{exp}{right}{int(round(k * 1000)):08d}"


def legs(exp="260903", name="SPY"):
    return (Leg(f"{name}{exp}C00780000", "sell"), Leg(f"{name}{exp}C00790000", "buy"),
            Leg(f"{name}{exp}P00750000", "sell"), Leg(f"{name}{exp}P00740000", "buy"))


def cond(underlying="SPY", credit=2.0, max_loss=8.0, exp="260903"):
    return Condor(underlying, legs(exp, underlying), -credit, max_loss,
                  "2026-09-03", 6, 780.0, 750.0, 10.0)


def hold(entry_id="h1", credit=2.0, mark=0.8, qty=5, max_loss=8.0,
         exp="260903", name="SPY"):
    return Holding(entry_id, name, legs(exp, name), qty, -credit, mark, max_loss)


def run(**kw):
    base = dict(now=MID, start=START, deadline=DEADLINE, equity=100_000.0,
                holdings=[], candidates=[cond()])
    base.update(kw)
    return plan_session(**base)


# --- the market has to be open ------------------------------------------

def test_a_closed_market_produces_no_orders_at_all():
    """Marks are stale and an exit ladder has nothing to fill against. The old
    code would happily plan closes at 16:05 off the closing print."""
    p = run(now=dt.datetime(2026, 8, 31, 16, 5, tzinfo=ET), holdings=[hold()])
    assert p.halted
    assert p.close == []
    assert p.open == []


def test_a_weekend_run_produces_no_orders():
    p = run(now=dt.datetime(2026, 8, 30, 12, 0, tzinfo=ET), holdings=[hold()])
    assert p.halted


def test_an_evening_israel_run_is_a_closed_us_market():
    p = run(now=dt.datetime(2026, 9, 1, 2, 0, tzinfo=IL), holdings=[hold()])
    assert p.halted


# --- the deadline --------------------------------------------------------

def test_past_flat_by_closes_everything():
    p = run(now=flat_by(DEADLINE) + dt.timedelta(minutes=1),
            holdings=[hold("a"), hold("b")])
    assert p.halted
    assert {r for _, r in p.close} == {"deadline"}
    assert len(p.close) == 2
    assert p.open == []


def test_just_before_flat_by_still_trades():
    p = run(now=flat_by(DEADLINE) - dt.timedelta(minutes=5), holdings=[hold()])
    assert not p.halted


def test_flat_by_fires_while_the_market_is_open():
    from trading_bot.options.clock import is_market_open
    assert is_market_open(flat_by(DEADLINE))


def test_a_holding_expiring_after_grading_is_closed():
    p = run(holdings=[hold(exp="260904")])
    assert len(p.close) == 1
    assert "after 2026-09-03" in p.close[0][1]


def test_a_candidate_expiring_after_grading_is_never_opened():
    p = run(candidates=[cond(exp="260904")])
    assert p.open == []
    assert any("expiry after" in n for n in p.notes)


# --- management ----------------------------------------------------------

def test_profit_target_closes():
    # 2.00 credit, 0.70 to close -> 65% captured
    p = run(holdings=[hold(credit=2.0, mark=0.70)])
    assert len(p.close) == 1
    assert "of credit" in p.close[0][1]


def test_short_of_the_target_is_left_alone():
    p = run(holdings=[hold(credit=2.0, mark=1.00)])   # 50%
    assert p.close == []


def test_exactly_on_the_target_closes():
    """Boundary equality is reachable and was untested before: a 2.00 credit
    marked at 0.80 is exactly 60%."""
    h = hold(credit=2.0, mark=0.80)
    assert h.profit_captured == pytest.approx(0.60)
    assert len(run(holdings=[h]).close) == 1


def test_loss_limit_closes_at_twice_the_credit():
    p = run(holdings=[hold(credit=2.0, mark=6.0)])    # -4.00 = 2x credit
    assert len(p.close) == 1
    assert "the credit" in p.close[0][1]


def test_pnl_sign_matches_the_broker_convention():
    h = hold(credit=2.0, mark=0.80, qty=3)
    assert h.pnl_per_share == pytest.approx(1.20)
    assert h.pnl_usd == pytest.approx(360.0)
    loser = hold(credit=2.0, mark=5.00, qty=3)
    assert loser.pnl_per_share == pytest.approx(-3.00)


# --- caps, with fixtures that genuinely breach them ---------------------

def test_a_candidate_that_would_breach_the_total_cap_is_sized_down():
    """Hostile: one contract risks $800 and the budget is $25,000, so an
    unconstrained sizer would take far more than the cap allows."""
    # per_name raised to match, so the TOTAL cap is what binds here rather
    # than the per-name one — otherwise this tests the wrong constraint.
    p = run(candidates=[cond(max_loss=8.0)], max_risk_pct=0.25, per_name_pct=0.25)
    total = sum(q * c.max_loss * 100 for c, q in p.open)
    assert total <= 25_000
    assert total > 20_000, "sized far below the cap for no reason"


def test_per_name_cap_binds_before_the_total_cap():
    p = run(candidates=[cond(max_loss=8.0)], max_risk_pct=0.25, per_name_pct=0.05)
    total = sum(q * c.max_loss * 100 for c, q in p.open)
    assert total <= 5_000


def test_two_names_each_respect_the_per_name_cap():
    p = run(candidates=[cond("SPY"), cond("QQQ")],
            max_risk_pct=0.25, per_name_pct=0.08)
    by = {}
    for c, q in p.open:
        by[c.underlying] = by.get(c.underlying, 0) + q * c.max_loss * 100
    assert set(by) == {"SPY", "QQQ"}
    for name, used in by.items():
        assert used <= 8_000, name


def test_existing_risk_reduces_the_budget():
    # $28,000 held against a $25,000 budget: over-budget already, so nothing
    # may be added. (At exactly $24,000 the remaining $1,000 sits precisely on
    # the 1%-of-equity floor, which tests the boundary and not the rule.)
    big = hold(max_loss=40.0, qty=7, mark=1.5)        # $28,000 held
    p = run(holdings=[big], max_risk_pct=0.25)
    assert p.close == []
    assert p.open == []
    assert any("no meaningful room" in n for n in p.notes)


def test_a_position_being_closed_frees_its_risk():
    """It must not both be closed and reserve budget — that would deadlock the
    book at its first profit target."""
    done = hold(max_loss=40.0, qty=6, credit=2.0, mark=0.5)   # 75% captured
    p = run(holdings=[done], max_risk_pct=0.25)
    assert len(p.close) == 1
    assert p.open, "closing did not free the budget"


# --- the credit gate -----------------------------------------------------

def test_a_structure_collecting_too_little_for_its_risk_is_refused():
    """Hostile fixture: 0.50 credit against 9.50 of risk is 5%, needing a 95%
    win rate to break even."""
    thin = cond(credit=0.5, max_loss=9.5)
    assert thin.credit_to_risk < 0.06
    p = run(candidates=[thin])
    assert p.open == []
    assert any("credit/risk below" in n for n in p.notes)


def test_a_healthy_structure_passes_the_same_gate():
    p = run(candidates=[cond(credit=2.0, max_loss=8.0)])
    assert p.open


def test_richer_structures_are_taken_first():
    rich, poor = cond("QQQ", credit=3.0, max_loss=7.0), cond("SPY", credit=1.5, max_loss=8.5)
    p = run(candidates=[poor, rich], max_risk_pct=0.08, per_name_pct=0.08)
    assert p.open[0][0].underlying == "QQQ"


# --- degenerate inputs ---------------------------------------------------

def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        run(now=dt.datetime(2026, 8, 31, 12, 0))


def test_zero_equity_is_refused_loudly():
    """The old code raised ZeroDivisionError here, which killed the whole plan
    INCLUDING the closes — so a near-blown account never went flat."""
    with pytest.raises(ValueError, match="equity"):
        run(equity=0.0)


def test_nan_equity_is_refused():
    with pytest.raises(ValueError, match="equity"):
        run(equity=float("nan"))


def test_no_candidates_is_not_an_error():
    p = run(candidates=[])
    assert p.open == []
    assert isinstance(p, SessionPlan)


# --- gaps found by mutation testing --------------------------------------

def test_exactly_at_flat_by_halts():
    """Boundary equality is reachable — a cron firing at 09:45:00 hits it
    exactly — and `>=` vs `>` was untested."""
    p = run(now=flat_by(DEADLINE), holdings=[hold()])
    assert p.halted
    assert len(p.close) == 1


def test_the_total_budget_is_decremented_as_positions_are_added():
    """Three names, each cheap enough to pass its own per-name cap, but which
    together exceed the total. Without decrementing, all three are taken."""
    cands = [cond("SPY", credit=2.0, max_loss=8.0),
             cond("QQQ", credit=2.0, max_loss=8.0),
             cond("IWM", credit=2.0, max_loss=8.0)]
    p = run(candidates=cands, max_risk_pct=0.10, per_name_pct=0.08)
    total = sum(q * c.max_loss * 100 for c, q in p.open)
    assert total <= 10_000, f"total budget breached: ${total:,.0f}"


def test_a_candidate_with_a_late_leg_is_refused():
    """Every leg must settle before grading, not just the first. A structure
    whose far wing expires later is a calendar spread wearing a condor's name."""
    mixed = Condor("SPY",
                   (Leg("SPY260903C00780000", "sell"),
                    Leg("SPY260903C00790000", "buy"),
                    Leg("SPY260903P00750000", "sell"),
                    Leg("SPY260904P00740000", "buy")),      # <- one day late
                   -2.0, 8.0, "2026-09-03", 6, 780.0, 750.0, 10.0)
    p = run(candidates=[mixed])
    assert p.open == []
    assert any("expiry after" in n for n in p.notes)


def test_a_holding_whose_last_leg_expires_late_is_closed():
    h = Holding("h9", "SPY",
                (Leg("SPY260903C00780000", "sell"), Leg("SPY260904C00790000", "buy")),
                2, -2.0, 1.0, 8.0)
    assert h.expiry == dt.date(2026, 9, 4)
    p = run(holdings=[h])
    assert len(p.close) == 1


def test_nothing_is_opened_before_the_window_starts():
    """P&L before the start does not count toward the measured return, but the
    risk and the starting-equity baseline do — a loss here begins the contest
    behind, for nothing."""
    early = dt.datetime(2026, 8, 27, 12, 0, tzinfo=ET)     # Thursday, day before
    p = run(now=early, start=START)
    assert p.open == []
    assert any("before the window opens" in n for n in p.notes)


def test_positions_are_still_managed_before_the_window_starts():
    """A position that exists must always be closable, whatever the clock says."""
    early = dt.datetime(2026, 8, 27, 12, 0, tzinfo=ET)
    p = run(now=early, start=START, holdings=[hold(credit=2.0, mark=0.60)])
    assert len(p.close) == 1
    assert p.open == []


def test_flatten_closes_everything_and_opens_nothing():
    """Operator kill switch. Deliberately the same code path as the deadline
    flatten, so exercising one exercises the other."""
    p = run(holdings=[hold("a"), hold("b")], flatten=True)
    assert p.halted
    assert len(p.close) == 2
    assert {r for _, r in p.close} == {"flatten requested"}
    assert p.open == []


def test_flatten_still_respects_a_closed_market():
    p = run(now=dt.datetime(2026, 8, 31, 17, 0, tzinfo=ET),
            holdings=[hold()], flatten=True)
    assert p.close == [], "tried to trade into a closed book"


def test_nothing_is_opened_that_must_be_closed_within_hours():
    """A round trip costs ~10% of the credit; an hour of decay is worth far
    less. Found by walking the contest week hour by hour — at 13:00 on expiry
    day the agent would open a structure it had to close at 14:00."""
    late = dt.datetime(2026, 9, 3, 13, 0, tzinfo=ET)
    p = run(now=late, candidates=[cond(exp="260903")])
    assert p.open == []
    assert any("minimum hold" in n for n in p.notes)


def test_the_same_structure_is_opened_with_a_full_session_left():
    early = dt.datetime(2026, 9, 2, 10, 0, tzinfo=ET)
    p = run(now=early, candidates=[cond(exp="260903")])
    assert p.open, [n for n in p.notes]
