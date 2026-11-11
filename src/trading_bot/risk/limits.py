"""Hard safeguards: the layer that sits between a desired position and a real
order, and the one allowed to say "no".

These are stateful, account-level circuit breakers for *live* (paper) trading,
checked on every decision cycle:

* **Max position weight** — never put more than X% of equity in one position.
* **Stop-loss** — exit if an open position is down more than X% from entry.
* **Daily-loss circuit breaker** — if today's P&L drops below -X%, flatten and
  halt trading until tomorrow.
* **Max-drawdown kill switch** — if equity falls X% from its peak, halt entirely
  and require a manual restart.

A breaker firing always resolves toward *less* risk (flat / halt), never more.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RiskLimits:
    max_position_weight: float = 1.0   # fraction of equity per symbol
    stop_loss_pct: float = 0.08        # exit a position down 8% from entry
    daily_loss_limit_pct: float = 0.03  # halt for the day at -3% daily P&L
    max_drawdown_pct: float = 0.20     # kill switch at -20% from peak equity
    max_leverage: float = 1.0          # gross exposure cap


@dataclass
class RiskDecision:
    target_weight: float          # approved exposure for the symbol [-cap, cap]
    halt_trading: bool = False    # daily breaker tripped (resets next session)
    kill_switch: bool = False     # drawdown breaker tripped (manual restart)
    reasons: list[str] = field(default_factory=list)


class RiskManager:
    """Applies ``RiskLimits`` to a proposed position given account state."""

    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()
        self._halted_today = False
        self._killed = False

    def reset_day(self) -> None:
        """Call at the premarket cycle to clear the daily halt."""
        self._halted_today = False

    def evaluate(
        self,
        *,
        symbol: str,
        desired_weight: float,
        equity: float,
        peak_equity: float,
        day_start_equity: float,
        position_entry_price: float | None = None,
        last_price: float | None = None,
        position_qty: float = 0.0,
    ) -> RiskDecision:
        L = self.limits
        reasons: list[str] = []

        # Kill switch is sticky until a human restarts the process.
        drawdown = equity / peak_equity - 1.0 if peak_equity > 0 else 0.0
        if self._killed or drawdown <= -L.max_drawdown_pct:
            self._killed = True
            reasons.append(
                f"KILL SWITCH: drawdown {drawdown:.1%} <= -{L.max_drawdown_pct:.0%}"
            )
            return RiskDecision(0.0, halt_trading=True, kill_switch=True, reasons=reasons)

        # Daily-loss breaker is a no-new-risk gate. Existing position stops
        # remain owned by the portfolio guard; a daily halt is not a liquidation.
        daily_pnl = equity / day_start_equity - 1.0 if day_start_equity > 0 else 0.0
        if self._halted_today or daily_pnl <= -L.daily_loss_limit_pct:
            self._halted_today = True
            reasons.append(
                f"DAILY HALT: P&L {daily_pnl:.1%} <= -{L.daily_loss_limit_pct:.0%}"
            )
            held_weight = position_qty * (last_price or 0) / equity if equity > 0 else 0.0
            return RiskDecision(held_weight, halt_trading=True, reasons=reasons)

        # Per-position stop-loss.
        if (
            position_qty != 0.0
            and position_entry_price
            and last_price
        ):
            move = last_price / position_entry_price - 1.0
            pnl = move if position_qty > 0 else -move
            if pnl <= -L.stop_loss_pct:
                reasons.append(f"STOP-LOSS {symbol}: position P&L {pnl:.1%}")
                return RiskDecision(0.0, reasons=reasons)

        # Clamp to position-weight and leverage caps.
        cap = min(L.max_position_weight, L.max_leverage)
        approved = max(-cap, min(cap, desired_weight))
        if approved != desired_weight:
            reasons.append(
                f"CLAMP {symbol}: {desired_weight:+.2f} -> {approved:+.2f} (cap {cap:.2f})"
            )
        return RiskDecision(approved, reasons=reasons)
