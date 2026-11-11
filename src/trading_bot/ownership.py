"""Which sleeve owns which symbol — and the checks that catch the two failure
modes that silently cost us money in June-August 2026.

Every position in the account should be the responsibility of exactly one
executor. Two things can go wrong, and both did:

**Orphans** — a position no running executor manages. The system was split
across two hosts trading two different Alpaca accounts: the Mac ran only
``run_live.py`` (the ETF sleeve), while the GCP VM ran regime / crypto /
managed-futures / cash-park from a hand-edited crontab. The Mac's QLD / KMLM /
DBMF / crypto positions were opened while the VM still pointed at that account;
when the VM moved to its own account on 2026-06-27, they were abandoned. From
then, 63% of the Mac's book drifted with no rebalance, no regime check, and —
because ``RiskManager`` is instantiated *inside* ``run_live.py`` and only
iterates that script's universe — no stop-loss. QLD ran to -10.35% past a -8%
stop that physically could not see it.

**Contested symbols** — a symbol two executors both target, with different
logic. ``ManagedFuturesExecutor`` routes idle capital into QQQ on the claim
that "QQQ is unowned"; but QQQ is in ``config.yaml``'s universe and the ETF
sleeve trades it with its own champion. Each executor undoes the other's
position. QQQ took 26 fills and lost $1,997 — our second-worst symbol.

Both are structural bugs, not strategy bugs: no amount of alpha survives an
executor that isn't running or two executors fighting. ``audit_ownership()``
turns them into a loud error at the start of every cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import (
    load_crypto_universe,
    load_pead_universe,
    load_universe,
    to_alpaca_position_symbol,
)
from .allocations import LIVE_SLEEVE_TARGETS

# Sleeves that hold a fixed, known instrument set.
REGIME_SYMBOLS = {"QLD"}       # RegimeExecutor.on_symbol
CASH_PARK_SYMBOLS = {"BIL"}    # CashParkExecutor.symbol
# ManagedFuturesExecutor.ROUTER_SYMBOLS. QQQ is the router's risk-ON beta leg
# and belongs to it exclusively — QQQ was removed from config.yaml's ETF
# universe to resolve the collision (see module docstring).
MANAGED_FUTURES_SYMBOLS = {"QQQ", "DBMF", "KMLM"}

# Symbols that are cash-equivalents or otherwise exempt from strategy stops.
# BIL is a 1-3mo T-bill ETF: a "stop-loss" on it is meaningless and firing one
# would just churn cash (it already cost 80 fills / $252k turnover in June).
CASH_EQUIVALENTS = {"BIL"}

# --------------------------------------------------------------------------
# Factor loadings — how much of one underlying risk each symbol carries.
#
# Symbol-level ownership stops two sleeves holding the same TICKER. It does not
# stop them holding the same RISK. Measured on the first live book (2026-08-04):
#
#     QLD  20% x beta 1.99 -> 39.8% Nasdaq
#     QQQ  34% x beta 1.00 -> 34.0% Nasdaq
#                             -----
#                             73.8% of the account in one index
#
# Both positions passed their individual weight caps. Both were driven by the
# SAME 200-DMA signal — the regime sleeve buys QLD above it, the idle router
# buys QQQ above it — so one trigger controlled three quarters of the book and
# would have unwound all of it at once. QLD/QQQ daily correlation: 1.00.
#
# Loadings are STATIC and deliberately so. Leverage factors (QLD is a 2x fund)
# are contractual, not estimated, and a risk check must not depend on a live
# data fetch that can fail — the whole point of marketdata.PriceUnavailable is
# that missing data must never silently change a trading decision. Broad-ETF
# betas are rounded approximations; erring high is the safe direction.
NASDAQ = "nasdaq100"
BROAD_EQUITY = "broad_equity"

FACTOR_LOADINGS: dict[str, dict[str, float]] = {
    "QLD":  {NASDAQ: 2.0, BROAD_EQUITY: 2.0},   # 2x QQQ, daily reset
    "QQQ":  {NASDAQ: 1.0, BROAD_EQUITY: 1.0},
    "SPY":  {NASDAQ: 0.8, BROAD_EQUITY: 1.0},
    "DIA":  {NASDAQ: 0.6, BROAD_EQUITY: 0.9},
    "IWM":  {NASDAQ: 0.6, BROAD_EQUITY: 1.0},
    "MDY":  {NASDAQ: 0.6, BROAD_EQUITY: 1.0},
    "XLK":  {NASDAQ: 0.9, BROAD_EQUITY: 1.1},
    "XLC":  {NASDAQ: 0.8, BROAD_EQUITY: 1.0},
    "XLY":  {NASDAQ: 0.8, BROAD_EQUITY: 1.0},
    "XLF":  {NASDAQ: 0.4, BROAD_EQUITY: 1.0},
    "XLI":  {NASDAQ: 0.4, BROAD_EQUITY: 1.0},
    "XLV":  {NASDAQ: 0.3, BROAD_EQUITY: 0.7},
    "XLP":  {NASDAQ: 0.1, BROAD_EQUITY: 0.5},
    "XLU":  {NASDAQ: 0.1, BROAD_EQUITY: 0.5},
    "XLE":  {NASDAQ: 0.2, BROAD_EQUITY: 0.7},
    "XLB":  {NASDAQ: 0.4, BROAD_EQUITY: 0.9},
    "GLD":  {NASDAQ: 0.0, BROAD_EQUITY: 0.1},
    "DBMF": {NASDAQ: 0.2, BROAD_EQUITY: 0.3},   # measured 0.21 vs QQQ
    "KMLM": {NASDAQ: 0.0, BROAD_EQUITY: 0.0},   # measured 0.00 — the real diversifier
    "BIL":  {NASDAQ: 0.0, BROAD_EQUITY: 0.0},
}

# Max fraction of equity of each factor the whole book may carry. 0.50 Nasdaq
# leaves room for a full regime sleeve (20% QLD = 40% Nasdaq) plus a modest
# second source, but blocks the 74% that the router and regime sleeve reached
# independently on day one.
# NOTE on the Nasdaq number, and on what this cap can and cannot do.
#
# 50% was a first guess and became the binding constraint on returns: it forced
# the regime sleeve to 20% and cost ~2.5pp of CAGR for 0.8pp of drawdown.
#
# But raising it exposed the real limitation. The broken 2026-08-04 book (QLD
# 20% + QQQ 34%) carried 74% Nasdaq at 16.7% vol. A healthy regime-33% + MF-60%
# book carries 72% Nasdaq at 12.7% vol. Nearly identical factor exposure, wildly
# different risk — because managed futures genuinely diversifies the second one
# and a duplicate QQQ leg did not. No threshold on raw exposure can separate
# them; only 2pp apart, one is fine and one is not.
#
# So this is an OUTER BOUND against runaway stacking, not a precision
# instrument. The primary defence against the original bug is the router rule
# "do not add a QQQ leg while the regime sleeve is already long Nasdaq", which
# is structural and does not depend on picking a number. 80% leaves room for a
# 33% regime sleeve (66% Nasdaq) plus the router's DBMF residual, and still
# stops anything genuinely unbounded.
# At a 70% regime sleeve this cap is close to non-binding by design: 70% of a
# 2x fund is already 140% Nasdaq. That is deliberate and worth being explicit
# about — at this risk budget the real controls are the 200-DMA exit and the
# catastrophe stop, NOT this number. It exists to stop a second sleeve stacking
# exposure unnoticed, which is what it was built for.
FACTOR_CAPS: dict[str, float] = {
    NASDAQ: 1.50,
    BROAD_EQUITY: 1.60,
}

# Default loading for an unrecognised symbol. Assume it is broad equity rather
# than harmless — an unknown ticker is more likely a stock than a T-bill.
DEFAULT_LOADING: dict[str, float] = {NASDAQ: 0.5, BROAD_EQUITY: 1.0}


def loading_of(symbol: str, factor: str) -> float:
    """This symbol's exposure to ``factor``, per dollar held."""
    return FACTOR_LOADINGS.get(symbol, DEFAULT_LOADING).get(factor, 0.0)


def factor_exposures(
    positions: dict[str, float], equity: float
) -> dict[str, float]:
    """Portfolio exposure to each capped factor, as a fraction of equity.

    ``positions`` maps symbol -> market value.
    """
    if equity <= 0:
        return {f: 0.0 for f in FACTOR_CAPS}
    return {
        factor: sum(mv * loading_of(sym, factor) for sym, mv in positions.items()) / equity
        for factor in FACTOR_CAPS
    }


@dataclass(frozen=True)
class Sleeve:
    """One executor and the symbols it is allowed to hold."""

    name: str
    symbols: frozenset[str]
    enabled: bool
    # Per-sleeve stop-loss override. Trend-following sleeves need room to
    # breathe — an 8% stop on managed futures fights the strategy it is meant
    # to protect — so each sleeve declares its own tolerance.
    stop_loss_pct: float | None = None
    # Max fraction of TOTAL equity this sleeve may hold in ONE symbol.
    max_position_weight: float | None = None
    # Max fraction of TOTAL equity for the sleeve AS A WHOLE. Distinct from
    # max_position_weight: a 6-coin crypto sleeve at 30% is only ~5% per coin,
    # so clamping its sleeve-wide allocation to the per-symbol cap would shrink
    # it ~6x. For single-instrument sleeves (regime holds only QLD) the two are
    # necessarily equal.
    max_sleeve_weight: float | None = None


@dataclass
class OwnershipAudit:
    orphans: dict[str, float] = field(default_factory=dict)
    contested: dict[str, list[str]] = field(default_factory=dict)
    disabled_holdings: dict[str, float] = field(default_factory=dict)
    # Held, owned by an ENABLED sleeve, but that sleeve isn't actually managing
    # it this cycle — the ETF sleeve skips symbols whose champion was retired by
    # the DSR gate, so the position sits there with no strategy behind it. A
    # subtler version of the orphan bug: ownership looks correct, management
    # isn't happening.
    unmanaged: dict[str, float] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not (
            self.orphans or self.contested
            or self.disabled_holdings or self.unmanaged
        )


def build_sleeves(enabled: dict[str, bool] | None = None) -> list[Sleeve]:
    """The full sleeve registry.

    ``enabled`` maps sleeve name -> bool; anything omitted defaults to enabled.
    A *disabled* sleeve still owns its symbols — that is the point. Disabling
    a sleeve must not turn its holdings into orphans; it means "this executor
    does not run, so flag anything it still holds for deliberate wind-down".
    """
    on = enabled or {}
    crypto_positions = {to_alpaca_position_symbol(s) for s in load_crypto_universe()}

    return [
        # The ETF mean-reversion sleeve. QQQ is deliberately EXCLUDED — it was
        # removed from config.yaml's universe and belongs to the idle router.
        Sleeve(
            name="etf",
            symbols=frozenset(load_universe()),
            enabled=on.get("etf", True),
            stop_loss_pct=0.08,
            max_position_weight=0.60,
        ),
        # 2x QQQ behind a 200-DMA filter. Tighter stop than the 8% default:
        # a 2x instrument moves twice as far for the same underlying move, so
        # an 8% stop on QLD is really a 4% stop on QQQ — far too tight to be
        # meaningful, while no stop at all is what let it run to -10.35%.
        Sleeve(
            name="regime",
            symbols=frozenset(REGIME_SYMBOLS),
            enabled=on.get("regime", True),
            # 30%, not the 16% default. The regime sleeve's EXIT RULE is the
            # 200-DMA filter, not a stop. A tight stop fights it: the guard
            # sells QLD, then the regime executor sees QQQ still above its
            # 200-DMA and buys straight back — the same guard-vs-sleeve loop as
            # the weight caps, and reachable (QLD -16% is only QQQ -8%, well
            # inside the band above the average). 30% (QQQ -15%) is a level the
            # 200-DMA has almost certainly already crossed, so this is a
            # genuine catastrophe backstop rather than a competing exit signal.
            stop_loss_pct=0.30,
            # 70%. Targets QQQ-level returns with materially less drawdown,
            # measured over 19.1 years (2007-05..2026-08, so 2008 and COVID are
            # in the sample — the 5.6y DBMF/KMLM window excludes both):
            #
            #     QQQ         CAGR +16.26%  vol 22.4%  maxDD -53.4%
            #     regime 70%  CAGR +16.33%  vol 21.3%  maxDD -31.5%
            #
            # Known failure mode, and it is not small: the 200-DMA protects in
            # slow grinding bears and HURTS in fast V-shaped crashes, where it
            # sells the low and misses the recovery.
            #
            #     2008 GFC    regime70 -12.5%  QQQ -40.5%   works
            #     2022 bear   regime70 -21.9%  QQQ -32.6%   works
            #     2018 Q4     regime70 -26.7%  QQQ -16.7%   loses to QQQ
            #     2020 COVID  regime70 -11.2%  QQQ  +0.1%   loses to QQQ
            #
            # This is a deliberate risk-budget choice, not an edge: Sharpe is
            # ~0.65 here versus ~0.75 at 33%. You are buying return with risk.
            max_position_weight=0.72,
            max_sleeve_weight=LIVE_SLEEVE_TARGETS["regime"],
        ),
        # Trend-following. Drawdowns are the strategy working, not failing;
        # a tight stop would systematically sell its worst entries.
        #
        # 45% per instrument, not 20%. The router splits the idle pool 50/50
        # between DBMF and KMLM. In a bull the regime sleeve takes 20% first so
        # each leg lands near 34%; in a BEAR the regime sleeve holds cash, idle
        # becomes ~88% of the book and each leg targets ~44% — precisely the
        # regime where this hedge is supposed to carry the portfolio. Any cap
        # below what the executor targets is an infinite trim/buy-back loop:
        # at 20% the guard cut $14k off each leg every cycle and the router
        # restored it (observed live 2026-08-04). It was also backwards on the
        # merits, throttling the genuine diversifier (KMLM's measured beta to
        # QQQ is 0.00) while the factor cap still allows 50% Nasdaq.
        Sleeve(
            name="managed_futures",
            symbols=frozenset(MANAGED_FUTURES_SYMBOLS),
            enabled=on.get("managed_futures", True),
            stop_loss_pct=0.25,
            max_position_weight=0.35,
            # Sleeve-wide ceiling. In a bear the regime sleeve holds cash and
            # the router would put ~88% of the book into DBMF+KMLM. Measured
            # 2020-12..2026-08: that carries a -18.2% drawdown — a hedge posture
            # with a deeper drawdown than the thing it hedges is not doing its
            # job. The two funds also correlate 0.54, so 88% across them is not
            # two independent bets, and the blend's edge is concentrated in one
            # year (+55.8% total, +26.3% excluding 2022).
            #
            # NOT a Sharpe argument: on excess-over-cash returns Sharpe is flat
            # across every weight (0.41 at 88% MF, 0.45 at 20%), because scaling
            # one risky asset against cash cannot change risk-adjusted return.
            # This is purely a choice about how much drawdown the defensive
            # sleeve may carry. 60% keeps most of the crisis hedge (2022 was the
            # scenario it exists for) at -12.1% instead of -18.2%.
            max_sleeve_weight=0.60,
        ),
        Sleeve(
            name="crypto",
            symbols=frozenset(crypto_positions),
            enabled=on.get("crypto", False),
            stop_loss_pct=0.30,
            max_position_weight=0.05,   # per coin
            max_sleeve_weight=0.20,     # across all coins
        ),
        Sleeve(
            name="pead",
            symbols=frozenset(load_pead_universe()),
            enabled=on.get("pead", False),
            stop_loss_pct=0.10,
            max_position_weight=0.05,   # per name
            max_sleeve_weight=0.25,     # across all names
        ),
        Sleeve(
            name="cash_park",
            symbols=frozenset(CASH_PARK_SYMBOLS),
            enabled=on.get("cash_park", True),
        ),
    ]


def owner_of(symbol: str, sleeves: list[Sleeve]) -> list[Sleeve]:
    """Every sleeve that claims ``symbol`` (more than one == contested)."""
    return [s for s in sleeves if symbol in s.symbols]


def audit_ownership(
    positions: dict[str, float],
    sleeves: list[Sleeve],
    active: dict[str, set[str]] | None = None,
) -> OwnershipAudit:
    """Check held positions against the registry.

    ``positions`` maps symbol -> market value. ``active`` optionally maps sleeve
    name -> the symbols that sleeve is actually managing this cycle (e.g. the
    ETF sleeve's symbols that still have a champion); a sleeve absent from the
    mapping is assumed to manage everything it owns.

    Returns the four problem classes; ``OwnershipAudit.clean`` is True only when
    none are present.
    """
    audit = OwnershipAudit()
    active = active or {}

    for symbol, value in positions.items():
        owners = owner_of(symbol, sleeves)
        if not owners:
            audit.orphans[symbol] = value
        elif len(owners) > 1:
            audit.contested[symbol] = [s.name for s in owners]
        elif not owners[0].enabled:
            audit.disabled_holdings[symbol] = value
        else:
            managed = active.get(owners[0].name)
            if managed is not None and symbol not in managed:
                audit.unmanaged[symbol] = value

    # A symbol claimed by two sleeves is contested whether or not it is
    # currently held — catch it before it can open a fighting position.
    seen: dict[str, list[str]] = {}
    for sleeve in sleeves:
        for symbol in sleeve.symbols:
            seen.setdefault(symbol, []).append(sleeve.name)
    for symbol, names in seen.items():
        if len(names) > 1:
            audit.contested[symbol] = names

    return audit


def format_audit(audit: OwnershipAudit) -> str:
    """Human-readable audit block for the daily log."""
    if audit.clean:
        return "  ✅ ownership clean — every position has exactly one enabled owner"

    lines: list[str] = []
    if audit.orphans:
        total = sum(audit.orphans.values())
        lines.append(f"  🚨 ORPHANS — no executor manages these (${total:,.0f}):")
        for sym, val in sorted(audit.orphans.items(), key=lambda x: -abs(x[1])):
            lines.append(f"       {sym:10s} ${val:>10,.0f}")
    if audit.contested:
        lines.append("  🚨 CONTESTED — claimed by multiple sleeves (they will fight):")
        for sym, names in sorted(audit.contested.items()):
            lines.append(f"       {sym:10s} {' + '.join(names)}")
    if audit.disabled_holdings:
        total = sum(audit.disabled_holdings.values())
        lines.append(f"  ⚠  HELD BY DISABLED SLEEVE (${total:,.0f}) — wind these down:")
        for sym, val in sorted(audit.disabled_holdings.items(), key=lambda x: -abs(x[1])):
            lines.append(f"       {sym:10s} ${val:>10,.0f}")
    if audit.unmanaged:
        total = sum(audit.unmanaged.values())
        lines.append(
            f"  ⚠  HELD BUT UNMANAGED (${total:,.0f}) — owner is enabled but is not "
            "trading these (e.g. champion retired by the DSR gate):"
        )
        for sym, val in sorted(audit.unmanaged.items(), key=lambda x: -abs(x[1])):
            lines.append(f"       {sym:10s} ${val:>10,.0f}")
    return "\n".join(lines)
