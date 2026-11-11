"""Aggregate factor-exposure limits.

Symbol-level ownership stops two sleeves holding the same ticker. It does not
stop them holding the same *risk*. The first live book (2026-08-04):

    QLD  20% x beta 1.99 -> 39.8% Nasdaq   (regime sleeve, QQQ > 200-DMA)
    QQQ  34% x beta 1.00 -> 34.0% Nasdaq   (idle router, QQQ > 200-DMA)
                            -----
                            73.8% of equity in one index

Both passed their own weight caps. Both ran off the same 200-DMA trigger. QLD
and QQQ correlate 1.00 — QLD *is* 2x QQQ. One signal controlled three quarters
of the account and would have unwound all of it simultaneously.

Run: uv run python -m pytest tests/ -q
"""

from __future__ import annotations

from trading_bot.ownership import (
    FACTOR_CAPS,
    NASDAQ,
    Sleeve,
    factor_exposures,
    loading_of,
)
from trading_bot.risk import PortfolioRiskGuard, RiskLimits

FROZEN = frozenset


def _sleeves() -> list[Sleeve]:
    return [
        Sleeve("regime", FROZEN({"QLD"}), enabled=True,
               stop_loss_pct=0.30, max_position_weight=0.72, max_sleeve_weight=0.70),
        Sleeve("managed_futures", FROZEN({"QQQ", "DBMF", "KMLM"}), enabled=True,
               stop_loss_pct=0.25, max_position_weight=0.45, max_sleeve_weight=0.60),
        Sleeve("cash_park", FROZEN({"BIL"}), enabled=True),
    ]


def _guard() -> PortfolioRiskGuard:
    return PortfolioRiskGuard(RiskLimits(), _sleeves())


def _pos(**kw):
    return {k: {"market_value": v, "unrealized_plpc": 0.01} for k, v in kw.items()}


def _evaluate(positions, equity=100_000):
    return _guard().evaluate(
        equity=equity, peak_equity=equity, day_start_equity=equity,
        positions=positions,
    )


# ------------------------------------------------------------- the measurement


def test_leverage_is_counted_not_just_notional():
    """A 2x fund carries twice the factor exposure per dollar held."""
    assert loading_of("QLD", NASDAQ) == 2.0
    assert loading_of("QQQ", NASDAQ) == 1.0
    exp = factor_exposures({"QLD": 20_000}, 100_000)
    assert exp[NASDAQ] == 0.40          # 20% notional, 40% exposure


def test_the_factor_cap_alone_would_not_have_caught_the_broken_book():
    """Honest limit of this control. The real 2026-08-04 weights carried 74%
    Nasdaq at 16.7% vol; a healthy regime-33% + MF-60% book carries 72% at
    12.7% vol. Two points apart, one broken and one fine — no threshold on raw
    exposure separates them. The ROUTER rule is what actually prevents the
    stack; this cap is only an outer bound."""
    broken = {"QLD": 20_007, "QQQ": 34_003, "DBMF": 17_001, "KMLM": 17_011, "BIL": 9_986}
    healthy = {"QLD": 33_000, "DBMF": 30_000, "KMLM": 30_000, "BIL": 7_000}
    b = factor_exposures(broken, 99_987)[NASDAQ]
    h = factor_exposures(healthy, 100_000)[NASDAQ]
    assert 0.75 < b < 0.79 and 0.70 < h < 0.75
    assert abs(b - h) < 0.06            # indistinguishable by exposure alone
    assert b < FACTOR_CAPS[NASDAQ]      # and so the cap does NOT fire on it


def test_kmlm_is_the_only_real_diversifier():
    """DBMF measured 0.21 to QQQ; KMLM measured 0.00. Only one of them
    genuinely offsets equity risk."""
    assert loading_of("KMLM", NASDAQ) == 0.0
    assert loading_of("DBMF", NASDAQ) > 0.0


def test_unknown_symbol_is_assumed_risky_not_harmless():
    assert loading_of("SOME_NEW_TICKER", NASDAQ) > 0.0


# ------------------------------------------------------------- enforcement


def test_two_symbols_under_their_own_caps_still_trip_the_factor_cap():
    """Neither position breaches its per-symbol limit; together they breach the
    factor limit. This is the case per-symbol caps structurally cannot catch."""
    r = _evaluate(_pos(QLD=70_000, QQQ=30_000))   # 100% Nasdaq
    assert any("FACTOR CAP" in a.reason for a in r.actions)


def test_trimming_restores_the_cap():
    r = _evaluate(_pos(QLD=70_000, QQQ=30_000, DBMF=10_000, KMLM=10_000))
    after = {"QLD": 70_000, "QQQ": 30_000, "DBMF": 10_000, "KMLM": 10_000}
    for a in r.actions:
        after[a.symbol] = a.target_value
    assert factor_exposures(after, 100_000)[NASDAQ] <= FACTOR_CAPS[NASDAQ] * 1.06


def test_densest_holding_is_trimmed_first():
    """Selling the 2x fund removes twice the exposure per dollar, so it
    restores the cap with the least disturbance elsewhere."""
    r = _evaluate(_pos(QLD=70_000, QQQ=30_000))
    factor_actions = [a for a in r.actions if "FACTOR CAP" in a.reason]
    assert factor_actions[0].symbol == "QLD"


def test_a_compliant_book_is_left_alone():
    r = _evaluate(_pos(QLD=20_000, DBMF=17_000, KMLM=17_000, BIL=10_000))
    assert factor_exposures(
        {"QLD": 20_000, "DBMF": 17_000, "KMLM": 17_000, "BIL": 10_000}, 100_000
    )[NASDAQ] <= FACTOR_CAPS[NASDAQ]
    assert [a for a in r.actions if "FACTOR CAP" in a.reason] == []


def test_cash_and_kmlm_never_attract_a_factor_trim():
    r = _evaluate(_pos(QLD=70_000, QQQ=30_000, KMLM=10_000, BIL=10_000))
    trimmed = {a.symbol for a in r.actions if "FACTOR CAP" in a.reason}
    assert "BIL" not in trimmed and "KMLM" not in trimmed


def test_factor_trim_does_not_override_a_stop_loss():
    """A stopped-out position is already going to zero; the factor pass must
    not raise its target back up."""
    g = _guard()
    r = g.evaluate(
        equity=100_000, peak_equity=100_000, day_start_equity=100_000,
        positions={
            "QLD": {"market_value": 70_000, "unrealized_plpc": -0.35},   # past the 30% catastrophe stop
            "QQQ": {"market_value": 30_000, "unrealized_plpc": 0.01},
        },
    )
    qld = [a for a in r.actions if a.symbol == "QLD"]
    assert qld and qld[0].target_value == 0.0


# --------------------------------------------------- dust / deadlock guard


def test_dust_breach_is_ignored():
    """Live 2026-08-04: prices drifted a few bp, QQQ sat at 20.3% against a 20%
    cap, and the guard ordered a $321 trim. Because ANY guard action defers the
    sleeves, the idle router never ran and never unwound the leg causing the
    breach — so the same dust trim repeated every cycle and the book could
    never converge. A cap enforced to the cent never lets the portfolio reach
    its target."""
    r = _evaluate(_pos(QQQ=20_329, QLD=13_331, DBMF=17_000, KMLM=17_000, BIL=10_000))
    assert r.actions == []


def test_a_material_breach_still_acts():
    r = _evaluate(_pos(QQQ=30_000, QLD=70_000))
    assert any("FACTOR CAP" in a.reason for a in r.actions)


def test_tolerance_does_not_swallow_a_stop_loss():
    """The dust threshold applies to cap trims, never to stops."""
    g = _guard()
    r = g.evaluate(
        equity=100_000, peak_equity=100_000, day_start_equity=100_000,
        positions={"QLD": {"market_value": 300, "unrealized_plpc": -0.30}},
    )
    assert [a.action for a in r.actions] == ["stop_loss"]


def test_kill_switch_ignores_the_dust_threshold():
    """At -20% everything goes, however small."""
    g = _guard()
    r = g.evaluate(
        equity=79_000, peak_equity=100_000, day_start_equity=79_500,
        positions={"QLD": {"market_value": 50, "unrealized_plpc": 0.0}},
    )
    assert r.kill_switch and [a.action for a in r.actions] == ["kill_switch"]


def test_sleeve_caps_do_not_contradict_what_executors_target():
    """Invariant for the whole bug class: a guard cap tighter than the target an
    executor rebalances to produces an infinite trim/buy-back loop.

    The idle router splits its pool 50/50 between DBMF and KMLM, so with the
    other sleeves disabled each leg lands near 34% of equity. A 20% per-symbol
    cap meant the guard trimmed $14k off each every cycle and the router
    restored it — live, on 2026-08-04.
    """
    from trading_bot.ownership import build_sleeves as real_sleeves
    mf = [s for s in real_sleeves() if s.name == "managed_futures"][0]
    # Router's worst case: the sleeve ceiling, split 50/50 between DBMF and
    # KMLM. Before the ceiling existed this was the whole book minus buffers
    # (~44% a leg in a bear), which is what made a 20% cap loop.
    worst_case_leg = (mf.max_sleeve_weight or 1.0) / 2
    assert mf.max_position_weight >= worst_case_leg, (
        f"managed_futures cap {mf.max_position_weight} is below the "
        f"{worst_case_leg:.0%} the router targets — guard and router will fight"
    )


def test_router_respects_its_sleeve_ceiling_in_a_bear():
    """In a bear the regime sleeve holds cash, so the idle pool is ~88% of the
    book. Measured 2020-12..2026-08 that posture carries a -18.2% drawdown in
    two funds correlating 0.54 — a hedge deeper than what it hedges. The router
    must stop at the sleeve ceiling and leave the rest for BIL."""
    from trading_bot.live.managed_futures_executor import ManagedFuturesExecutor
    from trading_bot.ownership import build_sleeves as real_sleeves

    mf = [s for s in real_sleeves() if s.name == "managed_futures"][0]
    ex = ManagedFuturesExecutor.__new__(ManagedFuturesExecutor)
    ex.max_sleeve_weight = mf.max_sleeve_weight

    equity = 100_000
    # bear: nothing actively deployed, so idle would be equity - 10% BIL - 2% cash
    raw_idle = equity - equity * 0.10 - equity * 0.02
    capped = min(raw_idle, equity * ex.max_sleeve_weight)
    assert raw_idle / equity > 0.85          # the uncapped exposure
    assert capped / equity <= 0.60           # what the ceiling allows
    # and each 50/50 leg must sit under the per-symbol cap, or guard vs router loops
    assert (capped / 2) / equity <= mf.max_position_weight


def test_regime_stop_does_not_compete_with_the_200dma_exit():
    """The regime sleeve's exit rule IS the 200-DMA filter. A 16% stop on a 2x
    fund fires at only -8% on QQQ — well inside the band above the average — so
    the guard would sell and the executor would immediately buy back. The stop
    must sit far enough out to be a catastrophe backstop, not a rival signal."""
    from trading_bot.ownership import build_sleeves as real_sleeves
    regime = [s for s in real_sleeves() if s.name == "regime"][0]
    # QLD is 2x, so the stop in QQQ terms is half the QLD move.
    qqq_equivalent = regime.stop_loss_pct / 2
    assert qqq_equivalent >= 0.12, (
        f"stop fires at QQQ {qqq_equivalent:.0%}, shallow enough that QQQ may "
        f"still be above its 200-DMA — guard and regime sleeve will loop")


# ------------------------------------------------------------ leverage cap


def test_gross_leverage_is_capped():
    """Live 2026-08-05: the regime sleeve bought more QLD than cash could fund
    and Alpaca filled the rest on margin — 1.08x gross, -$7,630 cash, against a
    stated max_leverage of 1.0. Nothing caught it: QLD was inside its 72%
    per-symbol cap, Nasdaq was inside the factor cap, and the post-cycle check
    never ran because the cycle had aborted earlier. A limit enforced only on
    the happy path is not a limit."""
    r = _evaluate(_pos(QLD=46_327, DBMF=30_048, KMLM=29_965), equity=98_709)
    after = {"QLD": 46_327, "DBMF": 30_048, "KMLM": 29_965}
    for a in r.actions:
        after[a.symbol] = a.target_value
    assert sum(after.values()) / 98_709 <= 1.01


def test_leverage_trim_hits_the_levered_position_not_the_hedge():
    """Selling KMLM (measured beta 0.00 to QQQ) to fix leverage caused by a 2x
    Nasdaq fund would leave the risky position intact and remove the only real
    diversifier — de-levering on paper while making the book riskier."""
    r = _evaluate(_pos(QLD=46_327, DBMF=30_048, KMLM=29_965), equity=98_709)
    trimmed = {a.symbol for a in r.actions if "LEVERAGE CAP" in a.reason}
    assert "QLD" in trimmed
    assert "KMLM" not in trimmed


def test_unlevered_book_is_untouched():
    r = _evaluate(_pos(QLD=20_000, DBMF=30_000, KMLM=30_000, BIL=18_000))
    assert [a for a in r.actions if "LEVERAGE CAP" in a.reason] == []
