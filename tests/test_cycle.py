"""Sequencing tests for the portfolio cycle.

Every bug the 2026-08-04 audit found was an interaction between components, in
the one file with no test coverage. Each test below pins an invariant that was
actually violated, so the same class of bug fails loudly instead of quietly
placing an order.

Run: uv run python -m pytest tests/ -q
"""

from __future__ import annotations

from trading_bot.cycle import SessionState, plan_cycle
from trading_bot.ownership import Sleeve
from trading_bot.risk import RiskLimits

TODAY = "2026-08-04"
FROZEN = frozenset


def _sleeves(etf_enabled=True, crypto_enabled=False) -> list[Sleeve]:
    return [
        Sleeve("etf", FROZEN({"SPY", "XLI", "XLV"}), enabled=etf_enabled,
               stop_loss_pct=0.08, max_position_weight=0.60, max_sleeve_weight=0.60),
        Sleeve("regime", FROZEN({"QLD"}), enabled=True,
               stop_loss_pct=0.16, max_position_weight=0.20, max_sleeve_weight=0.20),
        Sleeve("crypto", FROZEN({"BTCUSD"}), enabled=crypto_enabled,
               stop_loss_pct=0.30, max_position_weight=0.05, max_sleeve_weight=0.20),
    ]


def _pos(**kw) -> dict[str, dict]:
    """_pos(SPY=(10_000, 0.01)) -> {"SPY": {market_value, unrealized_plpc}}"""
    return {k: {"market_value": v[0], "unrealized_plpc": v[1]} for k, v in kw.items()}


def _plan(**over):
    kwargs = dict(
        phase="open",
        today=TODAY,
        equity=100_000.0,
        positions={},
        state=SessionState(peak_equity=100_000, day_start_equity=100_000,
                           session_date=TODAY),
        sleeves=_sleeves(),
        limits=RiskLimits(),
        champion_symbols={"SPY", "XLI", "XLV"},
    )
    kwargs.update(over)
    return plan_cycle(**kwargs)


# ------------------------------------------------- double-order prevention


def test_wind_down_symbols_are_hidden_from_the_guard():
    """BUG: positions were read before the wind-down, so the guard emitted a
    second close for a symbol already being closed."""
    plan = _plan(
        positions=_pos(XLI=(3_480, -0.20), SPY=(10_000, 0.01)),
        champion_symbols={"SPY"},          # XLI's champion was retired
    )
    assert "XLI" in plan.wind_down
    assert "XLI" not in plan.guarded_positions
    assert all(a.symbol != "XLI" for a in plan.guard.actions)


def test_sleeves_defer_when_the_guard_will_act():
    """BUG: the guard trimmed QLD by $11,386 and the regime sleeve planned the
    identical $11,386 sell — live, that is a double sell."""
    plan = _plan(positions=_pos(QLD=(32_000, -0.02)))   # 32% vs a 20% cap
    assert plan.guard_will_act
    assert not plan.run_sleeves
    assert "deferring sleeves" in plan.skip_reason


def test_sleeves_defer_when_winding_down():
    plan = _plan(
        positions=_pos(XLV=(2_600, 0.01)),
        champion_symbols=set(),
    )
    assert plan.wind_down
    assert not plan.run_sleeves


def test_sleeves_run_on_a_quiet_book():
    """The deferral must not be permanent — a settled book trades normally."""
    plan = _plan(positions=_pos(SPY=(10_000, 0.01)))
    assert not plan.guard_will_act
    assert plan.run_sleeves
    assert plan.skip_reason is None


# ------------------------------------------------------ breaker stickiness


def test_kill_switch_latches_across_cycles():
    """BUG: drawdown was recomputed fresh, so a kill switch that fired at
    premarket disarmed itself by the open if equity recovered a point."""
    plan = _plan(
        equity=99_000,                                    # only -1%
        state=SessionState(peak_equity=100_000, day_start_equity=99_000,
                           session_date=TODAY, killed=True),
        positions=_pos(SPY=(10_000, 0.01)),
    )
    assert plan.guard.kill_switch
    assert not plan.run_sleeves
    assert plan.state.killed


def test_daily_halt_latches_within_the_session():
    plan = _plan(
        state=SessionState(peak_equity=100_000, day_start_equity=100_000,
                           session_date=TODAY, halted_today=True),
    )
    assert plan.guard.halt_trading
    assert not plan.run_sleeves
    assert plan.state.halted_today


def test_new_session_clears_the_daily_halt_but_not_the_kill_switch():
    plan = _plan(
        today="2026-08-05",
        state=SessionState(peak_equity=100_000, day_start_equity=90_000,
                           session_date=TODAY, halted_today=True, killed=True),
    )
    assert not plan.state.halted_today or plan.guard.kill_switch
    assert plan.state.killed              # kill switch survives the date change
    assert plan.state.session_date == "2026-08-05"


# --------------------------------------------------- session bookkeeping


def test_peak_equity_ratchets_even_when_no_sleeve_runs():
    """BUG: peak only advanced inside the ETF executor, which some cycles skip.
    A stale low peak understates drawdown, so the kill switch fires late."""
    plan = _plan(
        phase="close",                                   # sleeves don't run
        equity=120_000,
        state=SessionState(peak_equity=100_000, day_start_equity=119_000,
                           session_date=TODAY),
    )
    assert plan.state.peak_equity == 120_000
    assert not plan.run_sleeves


def test_new_day_resets_day_start_equity():
    plan = _plan(
        today="2026-08-05",
        equity=97_000,
        state=SessionState(peak_equity=100_000, day_start_equity=94_000,
                           session_date=TODAY),
    )
    assert plan.state.day_start_equity == 97_000
    assert plan.state.session_date == "2026-08-05"


def test_same_day_does_not_reset_day_start_equity():
    plan = _plan(
        equity=97_000,
        state=SessionState(peak_equity=100_000, day_start_equity=94_000,
                           session_date=TODAY),
    )
    assert plan.state.day_start_equity == 94_000


# ------------------------------------------- data failure must not liquidate


def test_missing_champion_store_does_not_wind_down_the_book():
    """BUG: ChampionStore yields {} for a missing file, which marked every ETF
    position unmanaged and would have liquidated them — a data-availability
    failure causing a sell-off, the same shape as the NaN price bug."""
    plan = _plan(
        positions=_pos(SPY=(10_000, 0.01), XLI=(3_480, 0.02)),
        champion_symbols=set(),
        champion_store_exists=False,
    )
    assert plan.wind_down == {}
    assert plan.audit.unmanaged == {}
    assert any("champion store missing" in w for w in plan.warnings)


def test_present_but_empty_store_is_a_real_verdict_and_does_wind_down():
    """The gate legitimately retiring everything is NOT the same as a missing
    file, and must still be acted on."""
    plan = _plan(
        positions=_pos(SPY=(10_000, 0.01)),
        champion_symbols=set(),
        champion_store_exists=True,
    )
    assert plan.wind_down == {"SPY": 10_000}


# ------------------------------------------------------------- wind-down policy


def test_retired_champion_winds_down_automatically():
    plan = _plan(positions=_pos(XLI=(3_480, 0.02)), champion_symbols={"SPY"})
    assert plan.wind_down == {"XLI": 3_480}


def test_disabled_sleeve_holding_needs_the_opt_in_flag():
    """Turning a sleeve off states intent, not a verdict on edge — a human
    decides when to liquidate."""
    held = _pos(BTCUSD=(785, 0.0))
    assert plan_cycle(
        phase="open", today=TODAY, equity=100_000, positions=held,
        state=SessionState(peak_equity=100_000, day_start_equity=100_000,
                           session_date=TODAY),
        sleeves=_sleeves(), limits=RiskLimits(),
        champion_symbols={"SPY", "XLI", "XLV"},
    ).wind_down == {}

    assert plan_cycle(
        phase="open", today=TODAY, equity=100_000, positions=held,
        state=SessionState(peak_equity=100_000, day_start_equity=100_000,
                           session_date=TODAY),
        sleeves=_sleeves(), limits=RiskLimits(),
        champion_symbols={"SPY", "XLI", "XLV"}, wind_down_disabled=True,
    ).wind_down == {"BTCUSD": 785}


def test_no_orders_are_planned_outside_the_open_phase():
    for phase in ("premarket", "close"):
        plan = _plan(phase=phase, positions=_pos(XLI=(3_480, 0.02)),
                     champion_symbols=set())
        assert plan.wind_down == {}, phase
        assert not plan.run_sleeves, phase


# ---------------------------------------------------------------- abort path


def test_contested_symbol_aborts_before_anything_else():
    sleeves = _sleeves() + [Sleeve("router", FROZEN({"SPY"}), enabled=True)]
    plan = plan_cycle(
        phase="open", today=TODAY, equity=100_000,
        positions=_pos(SPY=(10_000, -0.50)),   # would otherwise stop out
        state=SessionState(peak_equity=100_000, day_start_equity=100_000,
                           session_date=TODAY),
        sleeves=sleeves, limits=RiskLimits(), champion_symbols={"SPY"},
    )
    assert plan.abort_reason is not None
    assert plan.guard is None          # nothing evaluated
    assert plan.wind_down == {}        # nothing planned
    assert not plan.run_sleeves
