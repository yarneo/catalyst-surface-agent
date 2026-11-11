"""Cash-parking sleeve: convert excess cash to BIL (1-3mo Treasury ETF) so
idle capital earns yield instead of sitting at 0%.

Runs LAST in the daily sequence (after crypto / PEAD / bogle). The other
sleeves deploy first; whatever cash they don't need gets parked. A small
buffer (default 10% of equity) stays in cash for the next day's tactical
needs — so PEAD/Crypto can fire a buy without needing to liquidate BIL first.

In Alpaca paper this is mostly architectural (paper pays no interest), but
the same code runs the right way on a real broker (BIL yields ~4-5% APY).
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..broker import AlpacaBroker
from ..marketdata import last_close
from ..notify import send_telegram


class CashParkExecutor:
    def __init__(
        self,
        broker: AlpacaBroker | None = None,
        symbol: str = "BIL",           # 1-3 month Treasury ETF (alt: SHV, SGOV)
        buffer_pct: float = 0.02,      # 2% — MUST match ManagedFuturesExecutor.
                                       # cash_buffer_pct. At 1% this swept the
                                       # router's cash reserve into BIL daily,
                                       # latching its over-deployment guard into
                                       # sell-only mode (2026-07-14..17) and
                                       # round-tripping BIL against it. The 2%
                                       # cash at 0% matches the validated
                                       # backtest's cash_weight model.
        min_trade_usd: float = 200.0,  # don't make dust trades
        live: bool = False,
    ):
        self.broker = broker or AlpacaBroker()
        self.symbol = symbol
        self.buffer_pct = buffer_pct
        self.min_trade_usd = min_trade_usd
        self.live = live

    def _price(self) -> float:
        return last_close(self.symbol)

    def run(self) -> dict:
        acct = self.broker.get_account()
        total_equity = float(acct.portfolio_value)
        cash = float(acct.cash)
        positions = {p.symbol: float(p.market_value) for p in self.broker.get_positions()}
        bil_value = positions.get(self.symbol, 0.0)

        buffer_cash = total_equity * self.buffer_pct
        excess = cash - buffer_cash  # positive = too much cash, negative = need to free cash

        today = datetime.now(timezone.utc).date().isoformat()
        mode = "LIVE (paper)" if self.live else "DRY-RUN"
        print(f"\n=== CASH PARK ({self.symbol}) | {today} | {mode} ===")
        print(f"Equity ${total_equity:,.0f} | cash ${cash:,.0f} | buffer ${buffer_cash:,.0f} "
              f"| {self.symbol} ${bil_value:,.0f}")

        action = "none"
        order_id = None
        if abs(excess) < self.min_trade_usd:
            print(f"  no action — within ${self.min_trade_usd:.0f} of target buffer")
            return {"action": "none", "excess": excess}

        try:
            price = self._price()
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ price lookup failed: {exc}")
            return {"action": "price_failed"}

        qty = round(abs(excess) / price, 4)
        if excess > 0:
            print(f"  BUY {qty} {self.symbol} @ ${price:.2f} (~${qty*price:,.0f}) — parking excess cash")
            if self.live:
                try:
                    o = self.broker.submit_market_order(
                        self.symbol, qty, "buy", reference_price=price, sleeve="cash_park"
                    )
                    order_id = o.id
                    print(f"    ✅ submitted (id {order_id})")
                    action = "buy"
                except Exception as exc:  # noqa: BLE001
                    print(f"    ❌ failed: {exc}")
                    action = "failed"
            else:
                action = "would_buy"
        else:
            qty = min(qty, round(bil_value / price, 4))  # don't sell more than we have
            print(f"  SELL {qty} {self.symbol} @ ${price:.2f} (~${qty*price:,.0f}) — freeing cash")
            if self.live and qty > 0:
                try:
                    o = self.broker.submit_market_order(
                        self.symbol, qty, "sell", reference_price=price, sleeve="cash_park"
                    )
                    order_id = o.id
                    print(f"    ✅ submitted (id {order_id})")
                    action = "sell"
                except Exception as exc:  # noqa: BLE001
                    print(f"    ❌ failed: {exc}")
                    action = "failed"
            else:
                action = "would_sell"

        if action in ("buy", "sell"):
            try:
                arrow = "📈 BUY" if action == "buy" else "📉 SELL"
                send_telegram(
                    f"💰 *Cash park* · {today}\n"
                    f"{arrow} {qty:.0f} {self.symbol} @ ${price:.2f} (~${qty*price:,.0f})\n"
                    f"Cash now targeting {self.buffer_pct:.0%} buffer (~${buffer_cash:,.0f})"
                )
            except Exception:
                pass
        return {"action": action, "excess": excess, "qty": qty, "order_id": order_id}
