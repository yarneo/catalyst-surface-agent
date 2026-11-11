"""Regime-aware IDLE ROUTER — puts idle capital to work based on the market regime.

Originally this sleeve always routed idle capital (capital the active sleeves —
QLD/PEAD/crypto — aren't using) into a DBMF+KMLM managed-futures blend. Backtested
2018-2026 (audit 2026-06-22), that was REGIME-BLIND and cost upside in bulls: idle
sat in market-neutral managed futures while the Nasdaq melted up.

Now it is REGIME-AWARE (the validated 200-DMA signal):
  * QQQ ABOVE its 200-DMA (risk-ON / bull)  -> route idle to QQQ (capture beta)
  * QQQ BELOW its 200-DMA (risk-OFF / bear)  -> route idle to DBMF+KMLM (hedge)

Backtest (full 2018-now, vs current idle->MF): +634% vs +515%, CAGR 26.6% vs 24.0%,
Sharpe 1.50 vs 1.40, maxDD -18% (better), AND IDENTICAL in the 2022 bear (-5%, -11%DD)
— the regime gate gives up zero crash protection. (We route to QQQ, not QLD, because
QLD is owned by the regime executor; QQQ is unowned so the router holds it cleanly,
and QQQ-in-bull backtested slightly better risk-adjusted than QLD-in-bull anyway.)

  idle_pool = equity - actively_deployed - BIL_buffer - cash_buffer
    where actively_deployed = market value of everything that ISN'T the router's own
    instruments {QQQ, DBMF, KMLM} or {BIL, cash}.
  Funds buys by liquidating BIL down to its buffer. On a regime flip, the wrong-regime
  holding (QQQ in a bear, or MF in a bull) is fully liquidated first.

Dry-run by default; --live submits orders.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..broker import AlpacaBroker
from ..marketdata import PriceUnavailable, last_close, last_close_and_sma
from ..notify import send_telegram

ROUTER_SYMBOLS = {"QQQ", "DBMF", "KMLM"}   # instruments this router may hold
IDLE_EXCLUDE = {"BIL", "DBMF", "KMLM", "QQQ"}  # not "actively deployed" by a real sleeve
QQQ_MA = 200


def idle_router_targets(
    idle_target: float, *, bull: bool, regime_nasdaq: float,
    idle_rule: str, ma_window: int = QQQ_MA,
) -> tuple[dict[str, float], list[str], str]:
    """Return the validated router targets without touching the broker.

    Keeping this decision pure makes the exact policy exercised by the live
    executor directly testable.  The QLD concentration guard intentionally
    overrides both Nasdaq-capable rules; ``mf_always`` never needs it.
    """
    if idle_rule not in {"split", "regime_qqq", "mf_always"}:
        raise ValueError(f"unsupported idle router rule {idle_rule!r}")
    if idle_rule == "mf_always":
        return (
            {"DBMF": idle_target * 0.5, "KMLM": idle_target * 0.5},
            ["QQQ"],
            "policy routes idle to DBMF+KMLM in every regime",
        )
    if bull and regime_nasdaq > 1.0:
        return (
            {"DBMF": idle_target * 0.5, "KMLM": idle_target * 0.5},
            ["QQQ"],
            (f"risk-ON but regime sleeve already long Nasdaq "
             f"(QLD ${regime_nasdaq:,.0f}) -> idle to DBMF+KMLM "
             f"(no stacked QQQ)"),
        )
    if bull and idle_rule == "regime_qqq":
        return (
            {"QQQ": idle_target}, ["DBMF", "KMLM"],
            f"risk-ON (QQQ>{ma_window}DMA) -> idle to QQQ",
        )
    if bull:
        return (
            {"QQQ": idle_target * 0.5,
             "DBMF": idle_target * 0.25, "KMLM": idle_target * 0.25},
            [],
            f"risk-ON (QQQ>{ma_window}DMA) -> idle SPLIT 50% QQQ / 50% MF",
        )
    return (
        {"DBMF": idle_target * 0.5, "KMLM": idle_target * 0.5},
        ["QQQ"],
        f"risk-OFF (QQQ<{ma_window}DMA) -> idle to DBMF+KMLM",
    )


class ManagedFuturesExecutor:
    def __init__(
        self,
        broker: AlpacaBroker | None = None,
        bil_buffer_pct: float = 0.10,   # keep this much in BIL for pre-buy hooks
        cash_buffer_pct: float = 0.02,  # keep this much free cash
        rebal_threshold: float = 0.05,  # only act if drift >= 5% of equity (low churn)
        min_trade_usd: float = 200.0,
        max_sleeve_weight: float | None = None,  # defaults to the ownership registry
        idle_rule: str = "split",
        ma_window: int = QQQ_MA,
        live: bool = False,
    ):
        self.broker = broker or AlpacaBroker()
        self.bil_buffer_pct = bil_buffer_pct
        self.cash_buffer_pct = cash_buffer_pct
        self.rebal_threshold = rebal_threshold
        self.min_trade_usd = min_trade_usd
        if idle_rule not in {"split", "regime_qqq", "mf_always"}:
            raise ValueError(f"unsupported idle router rule {idle_rule!r}")
        if not 100 <= int(ma_window) <= 300:
            raise ValueError("ma_window must be in [100, 300]")
        self.idle_rule = idle_rule
        self.ma_window = int(ma_window)
        if max_sleeve_weight is None:
            from ..ownership import build_sleeves
            mf = [s for s in build_sleeves() if s.name == "managed_futures"][0]
            max_sleeve_weight = mf.max_sleeve_weight or 1.0
        self.max_sleeve_weight = max_sleeve_weight
        self.live = live

    @staticmethod
    def _price(sym: str) -> float:
        return last_close(sym)

    @staticmethod
    def _qqq_bull(ma: int = QQQ_MA) -> bool:
        """Use the shared completed-session signal; missing data cannot authorize risk."""
        try:
            last, sma = last_close_and_sma("QQQ", ma)
            return last > sma
        except PriceUnavailable:
            raise

    def run(self) -> dict:
        acct = self.broker.get_account()
        equity = float(acct.portfolio_value)
        positions = {p.symbol: p for p in self.broker.get_positions()}
        values = {s: float(p.market_value) for s, p in positions.items()}
        active = sum(v for s, v in values.items() if s not in IDLE_EXCLUDE)
        idle = min(equity * self.max_sleeve_weight,
                   max(0.0, equity - active - equity * (self.bil_buffer_pct + self.cash_buffer_pct)))
        bull = self._qqq_bull(self.ma_window)
        targets, wrong, reason = idle_router_targets(
            idle, bull=bull, regime_nasdaq=values.get("QLD", 0.0),
            idle_rule=self.idle_rule, ma_window=self.ma_window)
        drift = max(abs(targets.get(s, 0.0) - values.get(s, 0.0)) for s in ROUTER_SYMBOLS)
        over = sum(values.get(s, 0.0) for s in ROUTER_SYMBOLS) > idle + equity * self.cash_buffer_pct
        flip = any(values.get(s, 0.0) > self.min_trade_usd for s in wrong)
        snap = {"equity": equity, "idle_target": idle, "regime_bull": bull,
                "current": {s: values.get(s, 0.0) for s in ROUTER_SYMBOLS},
                "targets": targets, "actions": [], "action": "hold"}
        print(f"Idle router: {reason}; target ${idle:,.0f}")
        if not flip and not over and drift < self.rebal_threshold * equity:
            return snap
        floor = self.min_trade_usd if over else max(self.min_trade_usd, self.rebal_threshold * equity * 0.5)
        # All reductions finish first. A failed sell raises; there is no buy path
        # behind an unconfirmed liquidation or a guessed cash credit.
        for symbol in sorted(ROUTER_SYMBOLS):
            held, target = values.get(symbol, 0.0), targets.get(symbol, 0.0)
            reduction = held - target
            if reduction < (self.min_trade_usd if symbol in wrong else floor):
                continue
            if self.live:
                if target <= 1:
                    order = self.broker.close_position(symbol, sleeve="managed_futures")
                else:
                    price = float(positions[symbol].current_price)
                    qty = min(float(positions[symbol].qty_available), reduction / price)
                    order = self.broker.submit_market_order(
                        symbol, qty, "sell", reference_price=price, sleeve="managed_futures")
                snap["actions"].append({"sym": symbol, "side": "sell",
                                        "qty": float(order.filled_qty), "status": "filled"})
            else:
                snap["actions"].append({"sym": symbol, "side": "would_sell", "notional": reduction})
        if self.live:
            self.broker.reconcile_orders()
            current = {p.symbol: float(p.market_value) for p in self.broker.get_positions()}
        else:
            current = {s: min(values.get(s, 0), targets.get(s, 0)) for s in ROUTER_SYMBOLS}
        for symbol, target in targets.items():
            increase = target - current.get(symbol, 0.0)
            if increase < floor:
                continue
            if self.live:
                quote = self.broker.quote(symbol)
                order = self.broker.submit_market_order(
                    symbol, increase / quote["ask"], "buy",
                    reference_price=quote["ask"], sleeve="managed_futures")
                snap["actions"].append({"sym": symbol, "side": "buy",
                                        "qty": float(order.filled_qty), "status": "filled"})
            else:
                snap["actions"].append({"sym": symbol, "side": "would_buy", "notional": increase})
        snap["action"] = "rebalanced" if snap["actions"] else "hold"
        return snap
