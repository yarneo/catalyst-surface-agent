"""Thin Alpaca wrapper.

Credentials come only from the environment (loaded from a git-ignored ``.env``);
they are never passed in code or logged. This wraps the bits of alpaca-py the
bot needs — account, positions, market clock, and order submission — behind a
small surface so the rest of the codebase doesn't depend on the SDK directly.
"""

from __future__ import annotations

import os
import json
import math
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from ..runtime_state import DATA, append_record
from ..sessions import iso_now
from .orders import ExecutionFailure, OrderCoordinator

load_dotenv()

EXECUTION_INTENT_LEDGER = (
    Path(__file__).resolve().parents[3] / "data" / "execution_intents.jsonl"
)


def append_execution_intent(row: dict, path: Path = EXECUTION_INTENT_LEDGER) -> None:
    """Persist the strategy reference used to judge the eventual broker fill."""
    append_record(path, {
            "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **row,
        })


@dataclass
class AlpacaConfig:
    api_key: str
    secret_key: str
    paper: bool = True

    @classmethod
    def from_env(cls) -> "AlpacaConfig":
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError(
                "Missing Alpaca credentials. Copy .env.example to .env and set "
                "ALPACA_API_KEY and ALPACA_SECRET_KEY."
            )
        paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
        return cls(api_key=key, secret_key=secret, paper=paper)


class AlpacaBroker:
    """Execution + account access. Defaults to the paper endpoint."""

    def __init__(self, config: AlpacaConfig | None = None):
        from alpaca.trading.client import TradingClient

        self.config = config or AlpacaConfig.from_env()
        if not self.config.paper:
            # Guardrail: refuse to silently trade real money. Flipping this on
            # will be an explicit, deliberate change later.
            raise RuntimeError(
                "Live trading is disabled. Set ALPACA_PAPER=true while we build."
            )
        self._client = TradingClient(
            self.config.api_key, self.config.secret_key, paper=self.config.paper
        )
        self.orders = OrderCoordinator(self._client, DATA / "order_journal.json", "manual-" + iso_now())
        self.cash_reserve = 0.02
        self.bil_reserve = 0.10
        self.max_leverage = 1.0

    def bind_cycle(self, cycle_id: str) -> None:
        self.orders.cycle_id = cycle_id

    def reconcile_orders(self, *, protective: bool = False) -> None:
        if protective:
            for order in self.open_orders():
                # Cancel only our own old unresolved orders. Refresh actual
                # holdings after cancellation; partial fills are never retried
                # as though nothing had executed.
                if str(order.client_order_id or "").startswith("pf-") and order.submitted_at:
                    age = (datetime.now(timezone.utc) - order.submitted_at).total_seconds()
                    if age > 180:
                        self._client.cancel_order_by_id(order.id)
        if self.orders.reconcile() or self.open_orders():
            raise ExecutionFailure("outstanding broker orders; cycle cannot allocate capital")

    def quote(self, symbol: str) -> dict:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        from alpaca.data.enums import DataFeed
        client = StockHistoricalDataClient(self.config.api_key, self.config.secret_key)
        q = client.get_stock_latest_quote(StockLatestQuoteRequest(
            symbol_or_symbols=symbol, feed=DataFeed.IEX))[symbol]
        age = (datetime.now(timezone.utc) - q.timestamp).total_seconds()
        bid, ask = float(q.bid_price), float(q.ask_price)
        if not all(math.isfinite(v) and v > 0 for v in (bid, ask)) or ask < bid:
            raise ExecutionFailure(f"{symbol}: no valid two-sided quote")
        if age < -5 or age > 120 or (ask - bid) / ((ask + bid) / 2) > 0.02:
            raise ExecutionFailure(f"{symbol}: stale or excessively wide quote")
        return {"bid": bid, "ask": ask, "at": q.timestamp.isoformat(), "feed": "iex"}

    def close_position(self, symbol: str, *, sleeve: str = "risk_guard"):
        position = self._client.get_open_position(symbol)
        qty = Decimal(str(position.qty))
        if qty <= 0:
            raise ExecutionFailure("only long equity liquidations are enabled")
        return self.submit_market_order(symbol, qty, "sell", sleeve=sleeve,
                                        reference_price=float(position.current_price))

    def _approve_buy(self, symbol: str, qty: Decimal, price: float) -> Decimal:
        from ..ownership import build_sleeves, factor_exposures, FACTOR_CAPS, owner_of, loading_of
        from ..allocations import DEFAULT_SLEEVE_ENABLEMENT
        acct = self.get_account()
        equity, cash = float(acct.portfolio_value), float(acct.cash)
        if not math.isfinite(equity) or equity <= 0 or self.open_orders():
            raise ExecutionFailure("account unavailable or open orders reserve buying capacity")
        positions = {p.symbol: float(p.market_value) for p in self.get_positions()}
        sleeves = build_sleeves(getattr(self, 'enabled_sleeves', DEFAULT_SLEEVE_ENABLEMENT))
        owners = owner_of(symbol, sleeves)
        if len(owners) != 1 or not owners[0].enabled:
            raise ExecutionFailure("purchase has no single enabled owner")
        owner = owners[0]
        # Clamp to the smallest available risk budget before funding the purchase.
        # Include conservative price padding so a target exactly at its cap can
        # still be implemented without rejecting the entire rebalance.
        room = max(0.0, equity * self.max_leverage - sum(abs(v) for v in positions.values()))
        if symbol != "BIL":
            room += max(0.0, positions.get("BIL", 0.0) - equity * self.bil_reserve)
        if owner.max_position_weight:
            room = min(room, equity * owner.max_position_weight - positions.get(symbol, 0.0))
        if owner.max_sleeve_weight:
            room = min(room, equity * owner.max_sleeve_weight - sum(positions.get(s, 0.0) for s in owner.symbols))
        for factor, exposure in factor_exposures(positions, equity).items():
            loading = loading_of(symbol, factor)
            if loading > 0:
                room = min(room, equity * (FACTOR_CAPS[factor] - exposure) / loading)
        qty = min(qty, Decimal(str(max(0.0, room) / (price * 1.005))))
        if float(qty) * price < 1:
            raise ExecutionFailure(f"{symbol}: no remaining risk budget")
        # Funding sells complete before cash is read again. BIL itself never borrows.
        needed = float(qty) * price * 1.005 + equity * self.cash_reserve - cash
        free_bil = max(0.0, positions.get("BIL", 0.0) - equity * self.bil_reserve)
        if symbol != "BIL" and needed > 0 and free_bil > 1:
            bil = self._client.get_open_position("BIL")
            bil_quote = self.quote("BIL")
            fund_qty = min(Decimal(str(bil.qty_available)),
                           Decimal(str(min(needed * 1.01, free_bil) / bil_quote["bid"])))
            self.submit_market_order("BIL", fund_qty, "sell", sleeve="funding",
                                     reference_price=bil_quote["bid"])
            acct = self.get_account()
            cash = float(acct.cash)
            positions = {p.symbol: float(p.market_value) for p in self.get_positions()}
        reserve = equity * self.cash_reserve
        if symbol != "BIL":
            reserve += max(0.0, equity * self.bil_reserve - positions.get("BIL", 0.0))
        fundable = max(0.0, cash - reserve) / (price * 1.005)
        qty = min(qty, Decimal(str(fundable))).quantize(Decimal("0.000000001"), rounding=ROUND_DOWN)
        if qty <= 0:
            raise ExecutionFailure(f"{symbol}: no unreserved cash for purchase")
        projected = {**positions, symbol: positions.get(symbol, 0.0) + float(qty) * price * 1.005}
        if sum(abs(v) for v in projected.values()) > equity * self.max_leverage:
            raise ExecutionFailure("purchase breaches gross exposure limit")
        if owner.max_position_weight and projected[symbol] > equity * owner.max_position_weight:
            raise ExecutionFailure("purchase breaches position cap")
        if owner.max_sleeve_weight and sum(projected.get(s, 0) for s in owner.symbols) > equity * owner.max_sleeve_weight:
            raise ExecutionFailure("purchase breaches sleeve cap")
        for factor, exposure in factor_exposures(projected, equity).items():
            if exposure > FACTOR_CAPS[factor]:
                raise ExecutionFailure(f"purchase breaches {factor} cap")
        return qty

    # --- read-only ---------------------------------------------------------
    def get_account(self):
        return self._client.get_account()

    def get_positions(self):
        return self._client.get_all_positions()

    def get_clock(self):
        """Market clock: is it open, and when does it next open/close."""
        return self._client.get_clock()

    def open_orders(self):
        """Orders that have not reached a terminal state yet.

        Needed because Alpaca's ``cash`` reflects *settled* balance: it does not
        drop when a buy order is submitted, only when it fills. Sleeves run
        sequentially and each sizes itself from account state, so without
        waiting for fills every sleeve allocates against the same unspent cash.
        On an all-cash account that stacked into $206k of buy orders against
        $100k of equity (2026-08-04) — not rejected, because paper margin allows
        4x, just silently 2x levered.
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        return self._client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
        )

    # --- execution (used by the live executor in a later step) -------------
    def submit_market_order(
        self, symbol: str, qty: float, side: str, *,
        reference_price: float | None = None, sleeve: str | None = None,
    ):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if side.lower() not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        quantity = Decimal(str(qty))
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("finite positive quantity required")
        quote = None
        if side.lower() == "sell":
            pos = self._client.get_open_position(symbol)
            quantity = min(quantity, Decimal(str(pos.qty_available)))
            if quantity <= 0:
                raise ExecutionFailure("no available long shares to sell")
            try:
                quote = self.quote(symbol)
            except Exception:
                # Risk-reducing exits remain possible during a quote-feed outage.
                quote = {"unavailable": True}
        else:
            quote = self.quote(symbol)
            quantity = self._approve_buy(symbol, quantity, quote["ask"])
        quantity = quantity.quantize(Decimal("0.000000001"), rounding=ROUND_DOWN)
        req = MarketOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = self.orders.execute(req, sleeve=sleeve or "unknown",
                                    reference=reference_price, quote=quote)
        try:
            append_execution_intent({
                "order_id": str(order.id),
                "symbol": symbol,
                "side": side.lower(),
                "qty": float(quantity),
                "reference_price": float(reference_price) if reference_price else None,
                "reference_basis": "strategy_decision_price",
                "sleeve": sleeve or "unknown",
                "quote": quote,
            })
        except Exception:
            # Evidence capture is intentionally downstream of the broker call;
            # a local disk problem must not make a successful order look failed.
            pass
        return order

    def submit_crypto_market_order(self, symbol: str, qty: float, side: str):
        """Crypto market order (e.g. symbol='BTC/USD'). Crypto requires GTC and
        supports fractional quantities; markets are 24/7."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        from ..allocations import DEFAULT_SLEEVE_ENABLEMENT
        if not DEFAULT_SLEEVE_ENABLEMENT.get("crypto"):
            raise ExecutionFailure("crypto execution is disabled pending validation")
        raise ExecutionFailure("crypto requires the journaled equity-equivalent lifecycle before enablement")
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
        )
        return self._client.submit_order(req)
