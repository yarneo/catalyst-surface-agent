"""Tests for the two structural safeguards added after the Aug-2026 review.

These cover the exact failure modes that cost real money:
  * a position no running executor manages (orphan) went unnoticed for 2 months
  * a symbol two executors both targeted (QQQ) generated churn and losses
  * a stop-loss that could not see the position it was meant to protect

Run: uv run python -m pytest tests/ -q
"""

from __future__ import annotations

from trading_bot.ownership import (
    Sleeve,
    audit_ownership,
    build_sleeves,
    owner_of,
)
from trading_bot.risk import PortfolioRiskGuard, RiskLimits

FROZEN = frozenset


def _sleeves() -> list[Sleeve]:
    return [
        Sleeve("etf", FROZEN({"SPY", "XLI"}), enabled=True,
               stop_loss_pct=0.08, max_position_weight=0.60),
        Sleeve("regime", FROZEN({"QLD"}), enabled=True,
               stop_loss_pct=0.16, max_position_weight=0.20),
        Sleeve("crypto", FROZEN({"BTCUSD"}), enabled=False,
               stop_loss_pct=0.30, max_position_weight=0.05),
        Sleeve("cash_park", FROZEN({"BIL"}), enabled=True),
    ]


# --------------------------------------------------------------- ownership


def test_orphan_is_detected():
    """A held symbol no sleeve claims must be flagged. This is the check that
    would have caught 63% of the book drifting unmanaged."""
    audit = audit_ownership({"SPY": 1000.0, "TQQQ": 5000.0}, _sleeves())
    assert audit.orphans == {"TQQQ": 5000.0}
    assert not audit.clean


def test_contested_symbol_is_detected_even_when_not_held():
    """Two sleeves claiming one symbol is a bug whether or not it is currently
    held — catch it before they open fighting positions (the QQQ case)."""
    sleeves = _sleeves() + [
        Sleeve("router", FROZEN({"SPY", "DBMF"}), enabled=True)
    ]
    audit = audit_ownership({}, sleeves)
    assert "SPY" in audit.contested
    assert set(audit.contested["SPY"]) == {"etf", "router"}


def test_disabled_sleeve_holding_is_flagged_not_orphaned():
    """Disabling a sleeve must not silently reclassify its holdings as
    unowned — they need deliberate wind-down, not amnesia."""
    audit = audit_ownership({"BTCUSD": 784.0}, _sleeves())
    assert audit.disabled_holdings == {"BTCUSD": 784.0}
    assert audit.orphans == {}


def test_clean_book_reports_clean():
    audit = audit_ownership({"SPY": 100.0, "QLD": 100.0}, _sleeves())
    assert audit.clean


def test_unmanaged_position_is_detected():
    """The subtler orphan: the ETF sleeve owns XLI and is enabled, but XLI's
    champion was retired by the DSR gate, so nothing actually trades it."""
    audit = audit_ownership(
        {"SPY": 1000.0, "XLI": 3480.0}, _sleeves(), active={"etf": {"SPY"}}
    )
    assert audit.unmanaged == {"XLI": 3480.0}
    assert audit.orphans == {}          # it IS owned — just not traded
    assert not audit.clean


def test_sleeve_absent_from_active_map_manages_everything():
    """Only sleeves that report an active set get checked; others are assumed
    to manage all they own, so this can't cause false positives."""
    audit = audit_ownership(
        {"QLD": 100.0}, _sleeves(), active={"etf": set()}
    )
    assert audit.unmanaged == {}
    assert audit.clean


def test_real_registry_has_no_contested_symbols():
    """Regression guard for the live config: no symbol may have two owners."""
    audit = audit_ownership({}, build_sleeves())
    assert audit.contested == {}, f"contested symbols: {audit.contested}"


def test_qqq_is_owned_only_by_the_router():
    owners = [s.name for s in owner_of("QQQ", build_sleeves())]
    assert owners == ["managed_futures"], owners


# -------------------------------------------------------------- risk guard


def _guard() -> PortfolioRiskGuard:
    return PortfolioRiskGuard(RiskLimits(), _sleeves())


def test_stop_loss_fires_on_a_position_from_any_sleeve():
    """The core fix: QLD is not in run_live.py's universe, but the guard reads
    the broker's positions directly, so its stop is enforceable."""
    r = _guard().evaluate(
        equity=100_000, peak_equity=100_000, day_start_equity=100_000,
        positions={"QLD": {"market_value": 10_000, "unrealized_plpc": -0.18}},
    )
    assert [a.action for a in r.actions] == ["stop_loss"]
    assert r.actions[0].target_value == 0.0


def test_per_sleeve_stop_is_used_not_the_global_default():
    """QLD at -10% must NOT stop out: it is 2x, so its sleeve sets a 16% stop.
    The same loss on an ETF-sleeve symbol (8% stop) must stop out."""
    g = _guard()
    qld = g.evaluate(
        equity=100_000, peak_equity=100_000, day_start_equity=100_000,
        positions={"QLD": {"market_value": 10_000, "unrealized_plpc": -0.10}},
    )
    assert [a.action for a in qld.actions] == []

    spy = g.evaluate(
        equity=100_000, peak_equity=100_000, day_start_equity=100_000,
        positions={"SPY": {"market_value": 10_000, "unrealized_plpc": -0.10}},
    )
    assert [a.action for a in spy.actions] == ["stop_loss"]


def test_cash_equivalents_are_exempt_from_stops():
    """Stopping out of BIL is meaningless and just churns cash."""
    r = _guard().evaluate(
        equity=100_000, peak_equity=100_000, day_start_equity=100_000,
        positions={"BIL": {"market_value": 10_000, "unrealized_plpc": -0.50}},
    )
    assert r.actions == []


def test_orphan_position_gets_the_default_stop():
    """An unmanaged position is exactly the case that most needs a stop."""
    r = _guard().evaluate(
        equity=100_000, peak_equity=100_000, day_start_equity=100_000,
        positions={"TQQQ": {"market_value": 10_000, "unrealized_plpc": -0.12}},
    )
    assert [a.action for a in r.actions] == ["stop_loss"]


def test_weight_cap_trims_to_target_not_to_zero():
    """QLD at 32% of equity against a 20% cap trims by the excess only."""
    r = _guard().evaluate(
        equity=100_000, peak_equity=100_000, day_start_equity=100_000,
        positions={"QLD": {"market_value": 32_000, "unrealized_plpc": -0.02}},
    )
    assert [a.action for a in r.actions] == ["trim"]
    assert r.actions[0].target_value == 20_000


def test_stop_loss_takes_precedence_over_trim():
    """A position that is both oversized and stopped out should be closed,
    not trimmed to the cap."""
    r = _guard().evaluate(
        equity=100_000, peak_equity=100_000, day_start_equity=100_000,
        positions={"QLD": {"market_value": 32_000, "unrealized_plpc": -0.20}},
    )
    assert [a.action for a in r.actions] == ["stop_loss"]


def test_daily_halt_blocks_new_risk_but_does_not_liquidate():
    """Flattening a whole book on a -3% day and rebuying tomorrow is a churn
    machine; the breaker blocks new risk instead."""
    r = _guard().evaluate(
        equity=96_000, peak_equity=100_000, day_start_equity=100_000,
        positions={"SPY": {"market_value": 10_000, "unrealized_plpc": 0.01}},
    )
    assert r.halt_trading
    assert not r.kill_switch
    assert r.blocks_new_risk
    assert [a for a in r.actions if a.action == "kill_switch"] == []


def test_kill_switch_liquidates_everything():
    r = _guard().evaluate(
        equity=79_000, peak_equity=100_000, day_start_equity=79_500,
        positions={
            "SPY": {"market_value": 10_000, "unrealized_plpc": 0.05},
            "QLD": {"market_value": 20_000, "unrealized_plpc": -0.05},
            "BIL": {"market_value": 5_000, "unrealized_plpc": 0.0},
        },
    )
    assert r.kill_switch and r.halt_trading
    assert {a.symbol for a in r.actions} == {"SPY", "QLD", "BIL"}
    assert all(a.target_value == 0.0 for a in r.actions)


def test_kill_switch_is_sticky_after_recovery():
    """Regression: the guard recomputed drawdown fresh each cycle, so a kill
    switch that fired at premarket disarmed itself by the open if equity ticked
    back over the threshold — exactly when you least want trading to resume."""
    r = _guard().evaluate(
        equity=95_000, peak_equity=100_000, day_start_equity=94_000,   # only -5%
        positions={"SPY": {"market_value": 10_000, "unrealized_plpc": 0.01}},
        already_killed=True,
    )
    assert r.kill_switch and r.halt_trading
    assert [a.action for a in r.actions] == ["kill_switch"]
    assert "latched" in r.actions[0].reason


def test_daily_halt_is_sticky_within_the_session():
    r = _guard().evaluate(
        equity=100_000, peak_equity=100_000, day_start_equity=100_000,  # flat
        positions={}, already_halted=True,
    )
    assert r.halt_trading and r.blocks_new_risk
    assert not r.kill_switch


def test_breakers_stay_off_when_not_previously_tripped():
    r = _guard().evaluate(
        equity=100_000, peak_equity=100_000, day_start_equity=100_000,
        positions={}, already_killed=False, already_halted=False,
    )
    assert not r.halt_trading and not r.kill_switch


def test_sleeve_wide_cap_is_distinct_from_per_symbol_cap():
    """Regression: clamping a multi-instrument sleeve's allocation to its
    PER-SYMBOL cap shrank it ~6x (crypto: 5% per coin vs 20% for the sleeve)."""
    crypto = [s for s in build_sleeves() if s.name == "crypto"][0]
    assert crypto.max_position_weight == 0.05
    assert crypto.max_sleeve_weight == 0.20
    regime = [s for s in build_sleeves() if s.name == "regime"][0]
    # Single-instrument sleeve: the per-symbol cap must not be TIGHTER than the
    # sleeve target, or the guard trims what the executor keeps re-buying.
    assert regime.max_position_weight >= regime.max_sleeve_weight


def test_peak_equity_ratchets_up():
    """A new high must reset the drawdown baseline, not report a fake gain."""
    r = _guard().evaluate(
        equity=110_000, peak_equity=100_000, day_start_equity=109_000,
        positions={},
    )
    assert r.peak_equity == 110_000
    assert r.drawdown == 0.0
