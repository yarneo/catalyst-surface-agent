"""Account-level risk sweep over EVERY open position.

``RiskManager`` (limits.py) is the right check in the wrong place: it is
instantiated inside ``run_live.py`` and only evaluates symbols in *that*
script's universe. Positions opened by the other five sleeves were invisible
to it, so their stop-losses could never fire. QLD reached -10.35% against a
-8% stop because nothing was looking at QLD.

This guard closes that hole. It runs FIRST in every cycle, reads the broker's
actual position list, and applies protection to whatever it finds — including
positions belonging to sleeves that are disabled or were never scheduled.

Design notes:

* **Stops are per-sleeve, not global.** An 8% stop is right for ETF
  mean-reversion and wrong for trend-following, where drawdowns are the
  strategy working. Each ``Sleeve`` declares its own tolerance.
* **Cash equivalents are exempt.** Stopping out of BIL is meaningless and
  just churns cash.
* **The daily-loss breaker halts, it does not flatten.** The original
  docstring promised "flatten and halt", but flattening a 60k book on a -3%
  day and rebuying tomorrow is a churn machine. The guard sets the halt flag
  so sleeves open no *new* risk; existing positions keep their own stops.
* **Only the drawdown kill switch liquidates**, because at -20% from peak the
  point is to stop the bleeding unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..ownership import (
    CASH_EQUIVALENTS,
    FACTOR_CAPS,
    NASDAQ,
    Sleeve,
    factor_exposures,
    loading_of,
    owner_of,
)
from .limits import RiskLimits


@dataclass
class GuardAction:
    symbol: str
    action: str          # "stop_loss" | "trim" | "kill_switch"
    reason: str
    current_value: float
    target_value: float


@dataclass
class GuardResult:
    equity: float
    peak_equity: float
    day_start_equity: float
    drawdown: float
    daily_pnl: float
    halt_trading: bool = False
    kill_switch: bool = False
    actions: list[GuardAction] = field(default_factory=list)

    @property
    def blocks_new_risk(self) -> bool:
        return self.halt_trading or self.kill_switch


class PortfolioRiskGuard:
    """Sweeps every open position and returns the de-risking actions required.

    Pure decision logic — it computes what should happen but submits nothing.
    The orchestrator applies the actions, so dry-run and live share one code
    path and can't diverge.
    """

    def __init__(self, limits: RiskLimits | None = None, sleeves: list[Sleeve] | None = None,
                 min_trim_usd: float = 250.0, cap_tolerance: float = 0.05):
        """``min_trim_usd`` / ``cap_tolerance`` stop the guard acting on dust.

        Without them the guard deadlocks the whole book. Observed live on
        2026-08-04: prices drifted a few basis points, putting QQQ at 20.3%
        against a 20% cap and the book at 50.4% Nasdaq against 50%. The guard
        dutifully ordered a $321 trim and a $26 trim — and because *any* guard
        action defers the sleeves for that cycle, the idle router never ran, so
        it never unwound the QQQ leg that was causing the breach. Every
        subsequent cycle repeated it: trim dust, defer sleeves, never converge.
        A cap enforced to the last cent is a cap that never lets the portfolio
        reach its target.

        ``cap_tolerance`` is 5% RELATIVE (a 20% cap acts above 21%), matching
        ManagedFuturesExecutor.rebal_threshold. A 1% band was still too tight:
        QQQ at 20.3% against a 20% cap produced a $321 trim every cycle, which
        cleared the dollar floor and kept the deadlock alive. Caps are risk
        ceilings, not precision targets.
        """
        self.limits = limits or RiskLimits()
        self.sleeves = sleeves or []
        self.min_trim_usd = min_trim_usd
        self.cap_tolerance = cap_tolerance

    def _stop_for(self, symbol: str) -> float | None:
        """The stop-loss that applies to ``symbol``, or None if exempt."""
        if symbol in CASH_EQUIVALENTS:
            return None
        owners = owner_of(symbol, self.sleeves)
        if owners and owners[0].stop_loss_pct is not None:
            return owners[0].stop_loss_pct
        # Orphans fall back to the global default — an unmanaged position is
        # exactly the case that most needs a stop.
        return self.limits.stop_loss_pct

    def _weight_cap_for(self, symbol: str) -> float:
        owners = owner_of(symbol, self.sleeves)
        if owners and owners[0].max_position_weight is not None:
            return owners[0].max_position_weight
        return self.limits.max_position_weight

    def evaluate(
        self,
        *,
        equity: float,
        peak_equity: float,
        day_start_equity: float,
        positions: dict[str, dict],
        already_killed: bool = False,
        already_halted: bool = False,
    ) -> GuardResult:
        """``positions`` maps symbol -> {market_value, unrealized_plpc}.

        ``already_killed`` / ``already_halted`` carry the persisted breaker state
        forward. Both breakers are STICKY — the kill switch until a human
        restarts, the daily halt until the next session. Without them a breaker
        that trips at premarket would silently disarm itself by the open if
        equity ticked back over the threshold, which is precisely when you least
        want trading to resume.
        """
        peak = max(peak_equity, equity)
        drawdown = equity / peak - 1.0 if peak > 0 else 0.0
        daily_pnl = equity / day_start_equity - 1.0 if day_start_equity > 0 else 0.0

        result = GuardResult(
            equity=equity,
            peak_equity=peak,
            day_start_equity=day_start_equity,
            drawdown=drawdown,
            daily_pnl=daily_pnl,
        )

        # Kill switch: liquidate everything, halt until a human restarts.
        if already_killed or drawdown <= -self.limits.max_drawdown_pct:
            result.kill_switch = True
            result.halt_trading = True
            for symbol, pos in positions.items():
                mv = pos["market_value"]
                if abs(mv) > 1.0:
                    result.actions.append(GuardAction(
                        symbol=symbol,
                        action="kill_switch",
                        reason=(
                            "KILL SWITCH (latched — manual restart required)"
                            if already_killed and drawdown > -self.limits.max_drawdown_pct
                            else f"KILL SWITCH: drawdown {drawdown:.1%} "
                                 f"<= -{self.limits.max_drawdown_pct:.0%}"
                        ),
                        current_value=mv,
                        target_value=0.0,
                    ))
            return result

        # Daily-loss breaker: block new risk, keep existing stops live. Sticky
        # for the rest of the session — the caller resets it on a new date.
        if already_halted or daily_pnl <= -self.limits.daily_loss_limit_pct:
            result.halt_trading = True

        for symbol, pos in positions.items():
            mv = pos["market_value"]
            if abs(mv) <= 1.0:
                continue

            stop = self._stop_for(symbol)
            pnl_pct = pos.get("unrealized_plpc", 0.0)
            if stop is not None and pnl_pct <= -stop:
                result.actions.append(GuardAction(
                    symbol=symbol,
                    action="stop_loss",
                    reason=f"STOP-LOSS {symbol}: {pnl_pct:.1%} <= -{stop:.0%}",
                    current_value=mv,
                    target_value=0.0,
                ))
                continue

            cap = self._weight_cap_for(symbol)
            weight = mv / equity if equity > 0 else 0.0
            # Tolerance band + minimum size: only act on a breach that is real,
            # not on price drift (see __init__).
            if weight > cap * (1 + self.cap_tolerance):
                target = cap * equity
                if mv - target >= self.min_trim_usd:
                    result.actions.append(GuardAction(
                        symbol=symbol,
                        action="trim",
                        reason=f"WEIGHT CAP {symbol}: {weight:.1%} > {cap:.0%} of equity",
                        current_value=mv,
                        target_value=target,
                    ))

        self._apply_factor_caps(result, positions, equity)
        self._apply_leverage_cap(result, positions, equity)
        return result

    def _apply_leverage_cap(
        self, result: GuardResult, positions: dict[str, dict], equity: float
    ) -> None:
        """Cap GROSS exposure. Per-symbol and factor caps both miss this.

        On 2026-08-05 the regime sleeve bought more QLD than cash could fund;
        Alpaca filled the rest on margin, leaving 1.08x gross with -$7,630 cash
        against a stated max_leverage of 1.0. Nothing stopped it: QLD was inside
        its 72% per-symbol cap, Nasdaq exposure was inside the factor cap, and
        the post-cycle leverage check never ran because the cycle had already
        aborted at the settlement step. A limit that is only enforced on the
        happy path is not a limit.

        Trims the MOST factor-dense holding first, same as the factor pass.
        The first version of this sorted the other way, reasoning that the goal
        was to raise cash with least disturbance to intended exposure — which
        made it sell KMLM (measured beta 0.00 to QQQ, the only real diversifier)
        to fix leverage caused by over-buying a 2x Nasdaq fund. That leaves the
        levered position intact and removes the hedge, making the book riskier
        while nominally de-levering. A risk control reduces risk; preserving the
        exposure mix is not its job.
        """
        if equity <= 0:
            return
        remaining = {
            sym: next((a.target_value for a in result.actions if a.symbol == sym),
                      pos["market_value"])
            for sym, pos in positions.items()
        }
        gross = sum(abs(v) for v in remaining.values())
        cap = self.limits.max_leverage
        if gross <= equity * cap * (1 + self.cap_tolerance):
            return
        excess = gross - equity * cap
        if excess < self.min_trim_usd:
            return

        already = {a.symbol for a in result.actions}
        ranked = sorted(
            ((s, v) for s, v in remaining.items()
             if v > 1.0 and s not in CASH_EQUIVALENTS),
            key=lambda kv: loading_of(kv[0], NASDAQ), reverse=True,
        )
        for sym, value in ranked:
            if excess <= 1.0:
                break
            cut = min(excess, value)
            new_target = max(0.0, value - cut)
            if value - new_target < self.min_trim_usd:
                continue
            excess -= cut
            reason = (f"LEVERAGE CAP: gross ${gross:,.0f} = "
                      f"{gross/equity:.2f}x > {cap:.2f}x")
            if sym in already:
                for a in result.actions:
                    if a.symbol == sym:
                        a.target_value = min(a.target_value, new_target)
                        a.reason += f" | {reason}"
            else:
                result.actions.append(GuardAction(
                    symbol=sym, action="trim", reason=reason,
                    current_value=positions[sym]["market_value"],
                    target_value=new_target,
                ))
                already.add(sym)

    def _apply_factor_caps(
        self, result: GuardResult, positions: dict[str, dict], equity: float
    ) -> None:
        """Cap aggregate exposure to a shared risk factor, not just per symbol.

        Per-symbol caps cannot see that two different tickers are the same bet.
        On the first live book QLD (20%) and QQQ (34%) each passed their own cap
        while together carrying 74% of equity in Nasdaq beta, driven by one
        200-DMA signal.

        Trims the most factor-dense holding first: reducing a 2x fund removes
        twice the exposure per dollar sold, so it restores the cap with the
        least disturbance to everything else.
        """
        already = {a.symbol for a in result.actions}
        # Value each symbol will still hold after the actions decided above.
        remaining = {
            sym: next((a.target_value for a in result.actions if a.symbol == sym),
                      pos["market_value"])
            for sym, pos in positions.items()
        }

        for factor, cap in FACTOR_CAPS.items():
            exposure = factor_exposures(remaining, equity).get(factor, 0.0)
            if exposure <= cap * (1 + self.cap_tolerance):
                continue
            excess = (exposure - cap) * equity      # in beta-dollars
            # A breach worth less than one meaningful trade is noise; acting on
            # it deadlocks the cycle without materially changing risk.
            if excess < self.min_trim_usd:
                continue
            # Densest first; skip anything already being closed outright.
            ranked = sorted(
                ((s, v) for s, v in remaining.items()
                 if v > 1.0 and loading_of(s, factor) > 0),
                key=lambda kv: loading_of(kv[0], factor), reverse=True,
            )
            for sym, value in ranked:
                if excess <= 1.0:
                    break
                load = loading_of(sym, factor)
                sellable_beta = value * load          # beta-dollars if fully sold
                cut_beta = min(excess, sellable_beta)
                cut_value = cut_beta / load           # dollars to actually sell
                new_target = max(0.0, value - cut_value)
                if value - new_target < self.min_trim_usd:
                    continue
                excess -= cut_beta
                remaining[sym] = new_target
                reason = (f"FACTOR CAP {factor}: book at {exposure:.0%} of equity "
                          f"> {cap:.0%}; {sym} carries {load:.1f}x")
                if sym in already:
                    for a in result.actions:
                        if a.symbol == sym:
                            a.target_value = min(a.target_value, new_target)
                            a.reason += f" | {reason}"
                else:
                    result.actions.append(GuardAction(
                        symbol=sym, action="trim", reason=reason,
                        current_value=positions[sym]["market_value"],
                        target_value=new_target,
                    ))
                    already.add(sym)


def format_guard(result: GuardResult) -> str:
    """Human-readable guard block for the daily log."""
    lines = [
        f"  Equity ${result.equity:,.0f} | peak ${result.peak_equity:,.0f} "
        f"| drawdown {result.drawdown:+.2%} | day P&L {result.daily_pnl:+.2%}"
    ]
    if result.kill_switch:
        lines.append(f"  🛑 KILL SWITCH ARMED — liquidating {len(result.actions)} positions")
    elif result.halt_trading:
        lines.append("  ⛔ DAILY HALT — no new risk today (existing stops still active)")

    if not result.actions:
        lines.append("  ✅ no risk actions required")
    else:
        for a in result.actions:
            delta = a.target_value - a.current_value
            lines.append(
                f"  ⚠  {a.action.upper():10s} {a.symbol:10s} "
                f"${a.current_value:,.0f} -> ${a.target_value:,.0f} ({delta:+,.0f})"
            )
            lines.append(f"       {a.reason}")
    return "\n".join(lines)
