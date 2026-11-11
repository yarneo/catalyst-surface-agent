"""Bogleheads 60/40 SPY/BND sleeve.

Holds a static-weight equity/bond mix as the "core" portion of the combined
50/25/25 portfolio. Rebalances only when drift exceeds a threshold (default 2%),
so it trades rarely — the whole point of this sleeve is *boring stable beta*
that diversifies our event-driven PEAD and crypto-trend sleeves.

State: tracked entirely on Alpaca (current positions). No internal state file
since the rules are stateless (just rebalance to target).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..broker import AlpacaBroker
from ..marketdata import last_close
from ..notify import send_telegram


class BogleExecutor:
    def __init__(
        self,
        broker: AlpacaBroker | None = None,
        allocation: float = 0.50,         # fraction of TOTAL equity allocated to this sleeve
        equity_symbol: str = "SPY",       # the equity ETF (can be QQQ, VTI, etc.)
        bond_symbol: str = "BND",         # the bond ETF
        equity_weight: float = 0.60,      # equity portion within the sleeve
        bond_weight: float = 0.40,        # bond portion within the sleeve
        rebal_threshold: float = 0.02,    # only trade if drift >= 2% of target
        live: bool = False,
    ):
        if abs(equity_weight + bond_weight - 1.0) > 1e-6:
            raise ValueError("equity_weight + bond_weight must equal 1.0")
        self.broker = broker or AlpacaBroker()
        self.allocation = allocation
        self.equity_symbol = equity_symbol
        self.bond_symbol = bond_symbol
        self.equity_weight = equity_weight
        self.bond_weight = bond_weight
        self.rebal_threshold = rebal_threshold
        self.live = live

    @staticmethod
    def _price(sym: str) -> float:
        return last_close(sym)

    def run(self) -> dict:
        acct = self.broker.get_account()
        total_equity = float(acct.portfolio_value)
        allocated = total_equity * self.allocation
        target_eq_usd = allocated * self.equity_weight
        target_bd_usd = allocated * self.bond_weight

        positions = {p.symbol: float(p.market_value) for p in self.broker.get_positions()}
        cur_eq_usd = positions.get(self.equity_symbol, 0.0)
        cur_bd_usd = positions.get(self.bond_symbol, 0.0)

        # Also liquidate any LEGACY core symbols if we've switched (e.g. SPY → QQQ)
        legacy_to_close: list[tuple[str, float]] = []
        for legacy in ("SPY", "QQQ", "VTI", "BND", "TLT", "IEI"):
            if legacy not in (self.equity_symbol, self.bond_symbol) and positions.get(legacy, 0.0) > 1.0:
                legacy_to_close.append((legacy, positions[legacy]))

        today = datetime.now(timezone.utc).date().isoformat()
        mode = "LIVE (paper)" if self.live else "DRY-RUN"
        print(f"\n=== CORE SLEEVE ({self.equity_symbol}/{self.bond_symbol}) | {today} | {mode} | "
              f"equity ${total_equity:,.0f} | allocated ${allocated:,.0f} ({self.allocation:.0%}) ===")

        actions = []
        snapshot = {"total_equity": round(total_equity, 2), "allocated": round(allocated, 2)}
        # First: liquidate legacy positions (e.g. old SPY when we've switched to QQQ)
        for legacy_sym, legacy_usd in legacy_to_close:
            print(f"  LEGACY {legacy_sym}: held ${legacy_usd:,.0f}, no longer in target — liquidating")
            if self.live:
                try:
                    self.broker._client.close_position(legacy_sym)
                    print(f"    ✅ close-position request submitted")
                    actions.append({"sym": legacy_sym, "side": "close-legacy", "qty": 0,
                                    "price": 0, "notional": legacy_usd})
                except Exception as exc:  # noqa: BLE001
                    print(f"    ❌ close failed: {exc}")
            else:
                print(f"    [dry-run] would close legacy {legacy_sym}")
            snapshot[f"legacy_{legacy_sym}"] = {"held": round(legacy_usd, 2), "action": "close"}

        for sym, target_usd, cur_usd in [
            (self.equity_symbol, target_eq_usd, cur_eq_usd),
            (self.bond_symbol, target_bd_usd, cur_bd_usd),
        ]:
            delta_usd = target_usd - cur_usd
            drift = abs(delta_usd) / max(target_usd, 1.0)
            print(f"  {sym}: held ${cur_usd:,.0f} vs target ${target_usd:,.0f}  drift {drift:.1%}")
            snapshot[sym] = {"held": round(cur_usd, 2), "target": round(target_usd, 2),
                             "drift": round(drift, 4)}
            if drift < self.rebal_threshold:
                snapshot[sym]["action"] = "hold"
                continue
            # Full-exit case: target=$0 and we hold some — use close_position
            # so Alpaca uses its EXACT internal qty. Otherwise the qty we
            # compute from MV/price can round 0.0001+ shares above the actual
            # held qty, and Alpaca rejects the sell as "insufficient qty
            # available". (Bug spotted 2026-06-01: bogle's QQQ sell rejected
            # by 0.0012 shares.)
            full_exit = (target_usd <= 1.0 and cur_usd > 1.0)
            if full_exit:
                print(f"    full-exit {sym} (target $0, held ${cur_usd:,.0f}) via close_position")
                if self.live:
                    try:
                        self.broker._client.close_position(sym)
                        print(f"    ✅ close-position submitted")
                        actions.append({"sym": sym, "side": "close", "qty": 0,
                                        "price": 0, "notional": cur_usd})
                        snapshot[sym]["action"] = "close"
                    except Exception as exc:  # noqa: BLE001
                        print(f"    ❌ close failed: {exc}")
                        snapshot[sym]["action"] = "close_failed"
                else:
                    snapshot[sym]["action"] = "would_close"
                continue
            try:
                price = self._price(sym)
            except Exception as exc:  # noqa: BLE001
                print(f"    ⚠ price lookup failed: {exc}")
                snapshot[sym]["action"] = "price_failed"
                continue
            qty = round(abs(delta_usd) / price, 4)
            side = "buy" if delta_usd > 0 else "sell"
            print(f"    {side} {qty} {sym} @ ${price:,.2f} (${qty*price:,.0f})")
            if self.live:
                try:
                    order = self.broker.submit_market_order(
                        sym, qty, side, reference_price=price, sleeve="bogle"
                    )
                    print(f"    ✅ submitted (id {order.id})")
                    actions.append({"sym": sym, "side": side, "qty": qty, "price": price,
                                    "notional": qty * price})
                    snapshot[sym]["action"] = side
                except Exception as exc:  # noqa: BLE001
                    print(f"    ❌ order failed: {exc}")
                    snapshot[sym]["action"] = "failed"
            else:
                snapshot[sym]["action"] = f"would_{side}"

        if actions:
            try:
                send_telegram(_format_bogle_msg(today, total_equity, allocated, actions,
                                                snapshot, self.equity_symbol, self.bond_symbol))
            except Exception:
                pass
        return snapshot


def _format_bogle_msg(today: str, equity: float, allocated: float, actions: list,
                       snap: dict, eq_sym: str, bd_sym: str) -> str:
    label = f"{eq_sym}/{bd_sym}" if snap.get(bd_sym, {}).get("target", 0) > 0 else f"{eq_sym}-only"
    lines = [f"🏦 *Core sleeve ({label})* · {today}",
             f"Equity ${equity:,.0f} · allocated ${allocated:,.0f}", ""]
    for a in actions:
        if a["side"] == "close-legacy":
            lines.append(f"♻ *LEGACY {a['sym']}* liquidated (~${a['notional']:,.0f})")
            continue
        arrow = "📈 BUY" if a["side"] == "buy" else "📉 SELL"
        lines.append(f"{arrow} *{a['sym']}* {a['qty']:.2f} sh @ ${a['price']:.2f} "
                     f"(~${a['notional']:,.0f})")
    if snap.get(eq_sym) and snap.get(bd_sym):
        lines.append("")
        lines.append(f"{eq_sym}: ${snap[eq_sym]['held']:,.0f} → ${snap[eq_sym]['target']:,.0f}  "
                     f"{bd_sym}: ${snap[bd_sym]['held']:,.0f} → ${snap[bd_sym]['target']:,.0f}")
    elif snap.get(eq_sym):
        lines.append("")
        lines.append(f"{eq_sym}: ${snap[eq_sym]['held']:,.0f} → ${snap[eq_sym]['target']:,.0f}")
    return "\n".join(lines)
