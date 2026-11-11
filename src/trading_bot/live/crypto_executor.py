"""Live (paper) executor for the crypto trend-following basket.

Differs from the equity executor: crypto is 24/7 (no market-clock gating, runs
once daily), uses a fixed validated ensemble (no per-symbol champions), and sizes
the whole basket to a target volatility. Flow each run:

    fresh daily bars -> ensemble trend signal per coin -> equal-weight 1/N
    -> vol-target scale (cap leverage 1.0) -> risk manager clamp/halt
    -> target notional per coin -> order = target - current (fractional, GTC).

Dry-run by default; --live submits to the (paper) account. Breaker state (peak /
day-start equity, kill switch) persists to data/crypto_state.json.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..backtest.basket import trend_basket_panels, vol_target_scale
from ..broker import AlpacaBroker
from ..config import to_alpaca_position_symbol, to_alpaca_symbol
from ..data import load_ohlcv
from ..notify import send_telegram
from ..risk import RiskManager
from ..signals import get_crypto_sentiment, sentiment_multiplier
from ..strategies import EnsembleTrend
from .executor import STATE_PATH, ExecutorState

CRYPTO_STATE_PATH = STATE_PATH.parent / "crypto_state.json"
DECISIONS_PATH = STATE_PATH.parent / "crypto_decisions.jsonl"  # per-run reasoning for the dashboard
MIN_TRADE_USD = 10.0  # skip dust rebalances


def _norm(symbol: str) -> str:
    return symbol.replace("/", "").replace("-", "").upper()


class CryptoExecutor:
    def __init__(
        self,
        symbols: list[str],
        broker: AlpacaBroker | None = None,
        strategy=None,
        risk_manager: RiskManager | None = None,
        target_vol: float = 0.20,
        lookback_days: int = 400,
        live: bool = False,
        use_sentiment: bool = True,
        capital_allocation: float = 1.0,  # fraction of TOTAL equity for this sleeve
    ):
        self.symbols = symbols                       # yfinance data symbols (BTC-USD)
        self.broker = broker or AlpacaBroker()
        self.strategy = strategy or EnsembleTrend()
        self.risk = risk_manager or RiskManager()
        self.target_vol = target_vol
        self.lookback_days = lookback_days
        self.live = live
        self.use_sentiment = use_sentiment
        self.capital_allocation = capital_allocation

    def _recent_data(self) -> dict[str, pd.DataFrame]:
        end = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        start = (datetime.now(timezone.utc).date() - timedelta(days=self.lookback_days)).isoformat()
        data = {}
        for s in self.symbols:
            try:
                data[s] = load_ohlcv(s, start, end, use_cache=False)
            except Exception as exc:  # noqa: BLE001
                print(f"  (skip {s}: {exc})")
        return data

    def _positions(self) -> dict[str, float]:
        return {_norm(p.symbol): float(p.qty) for p in self.broker.get_positions()}

    def run(self) -> ExecutorState:
        st = ExecutorState.load(CRYPTO_STATE_PATH)
        self.risk._killed = st.killed
        self.risk._halted_today = st.halted_today

        acct = self.broker.get_account()
        equity = float(acct.portfolio_value)
        today = datetime.now(timezone.utc).date().isoformat()
        if st.session_date != today:
            st.session_date = today
            st.day_start_equity = equity
            st.halted_today = False
            self.risk.reset_day()
        st.peak_equity = max(st.peak_equity, equity)

        mode = "LIVE (paper)" if self.live else "DRY-RUN"
        print(f"\n=== CRYPTO TREND BASKET | {today} UTC | {mode} ===")
        print(f"Equity ${equity:,.0f} | day-start ${st.day_start_equity:,.0f} | peak ${st.peak_equity:,.0f}")

        data = self._recent_data()
        if len(data) < 2:
            print("Insufficient data — no action.")
            return st

        R, W = trend_basket_panels(data, self.strategy)
        scale = vol_target_scale(R, W, self.target_vol, 30, 365, 1.0).iloc[-1]
        scale = 0.0 if pd.isna(scale) else float(scale)
        ensemble_now = W.iloc[-1]            # raw ensemble agreement 0..1 per coin
        target_w = ensemble_now * scale * self.capital_allocation
        positions = self._positions()
        print(f"vol-target scale: {scale:.2f} · allocation: {self.capital_allocation:.0%}")

        # Pre-pass: estimate total BUY notional this run will need. If cash is
        # short, liquidate BIL up-front (mirrors pead_executor's pattern).
        # Without this, the buy goes through on margin and cash goes negative.
        if self.live:
            buy_needed = 0.0
            for s in self.symbols:
                if s not in data: continue
                price = float(data[s]["close"].iloc[-1])
                cur_qty = positions.get(_norm(s), 0.0)
                # Use bare target_w (no risk/sentiment dampening) as the upper bound
                tgt_qty = (float(target_w.get(s, 0.0)) * equity) / price
                delta = tgt_qty - cur_qty
                if delta > 0:
                    buy_needed += delta * price
            if buy_needed > 0:
                acct = self.broker.get_account()
                cash_now = float(acct.cash)
                if buy_needed > cash_now:
                    # BIL (an equity ETF) only trades during market hours. This cron
                    # runs 00:15 UTC when the US market is CLOSED: the close order
                    # can't fill (it just holds the qty), and the crypto buys then
                    # went through on margin — confirmed negative-cash incidents
                    # 2026-07-12. When closed, skip the BIL sale and cap buys at
                    # available cash instead (the cap below).
                    try:
                        market_open = bool(self.broker.get_clock().is_open)
                    except Exception:
                        market_open = False  # if unsure, don't fire an unfillable order
                    bil_pos = next((p for p in self.broker.get_positions() if p.symbol == "BIL"), None)
                    if not market_open:
                        print(f"  Need ${buy_needed:,.0f} for crypto buys; cash ${cash_now:,.0f}. "
                              f"Market CLOSED — skipping BIL liquidation; buys capped at available cash.")
                    elif bil_pos and float(bil_pos.market_value) > 0:
                        print(f"  Need ${buy_needed:,.0f} for crypto buys; cash ${cash_now:,.0f}. "
                              f"Liquidating BIL (${float(bil_pos.market_value):,.0f}) to free cash…")
                        try:
                            self.broker._client.close_position("BIL")
                            print("    ✅ BIL close-position submitted; waiting 5s for fill")
                            import time as _t; _t.sleep(5)
                        except Exception as exc:  # noqa: BLE001
                            print(f"    ⚠ BIL liquidation failed: {exc}")

        # Cash cap for buys: never buy more than the cash actually available (no
        # margin). Sells submitted earlier in the loop credit the pool.
        cash_avail = None
        if self.live:
            try:
                cash_avail = max(0.0, float(self.broker.get_account().cash))
            except Exception:  # noqa: BLE001
                cash_avail = None  # unknown -> keep legacy behavior rather than block all buys

        decisions: dict[str, dict] = {}
        for s in self.symbols:
            if s not in data:
                continue
            price = float(data[s]["close"].iloc[-1])
            cur_qty = positions.get(_norm(s), 0.0)
            decision = self.risk.evaluate(
                symbol=s,
                desired_weight=float(target_w.get(s, 0.0)),
                equity=equity,
                peak_equity=st.peak_equity,
                day_start_equity=st.day_start_equity,
                last_price=price,
                position_qty=cur_qty,
            )
            for r in decision.reasons:
                self._log_reason(r)
            w = decision.target_weight
            sent = None
            # Dampen-only sentiment (only for coins we'd actually hold).
            if self.use_sentiment and w > 0:
                sent = get_crypto_sentiment(s)
                mult = sentiment_multiplier(sent["score"], sent["confidence"])
                if mult < 1.0:
                    print(f"    · sentiment {sent['score']:+.2f} (conf {sent['confidence']:.2f}, "
                          f"{sent['source']}) -> dampen x{mult:.2f}")
                w *= mult
            target_qty = (w * equity) / price
            delta = target_qty - cur_qty
            notional = abs(delta) * price
            print(f"  {s}: w {w:>5.2f} -> qty {target_qty:.4f} (have {cur_qty:.4f}) "
                  f"@ ${price:,.2f} | Δ ${notional:,.0f}")

            action, order_qty = "hold", 0.0
            if notional >= MIN_TRADE_USD:
                side = "buy" if delta > 0 else "sell"
                order_qty = round(abs(delta), 6)
                # Enforce the no-margin cap on buys (see cash_avail above).
                if side == "buy" and self.live and cash_avail is not None:
                    max_notional = max(0.0, cash_avail - 1.0)  # $1 safety
                    if order_qty * price > max_notional:
                        order_qty = round(max_notional / price, 6)
                        notional = order_qty * price
                        print(f"    · buy capped at available cash (${max_notional:,.0f})")
                    if notional < MIN_TRADE_USD:
                        print("    no action — insufficient cash for a meaningful buy (no margin)")
                        decisions[s] = {
                            "ensemble": round(float(ensemble_now.get(s, 0.0)), 3),
                            "target_weight": round(float(w), 4),
                            "price": price, "action": "skipped_no_cash", "order_usd": 0.0,
                        }
                        continue
                # Full-exit case: target=0 and we hold some — use close_position
                # instead of a rounded sell so we don't leave fractional dust.
                full_exit = (target_qty == 0 and cur_qty > 0 and side == "sell")
                if not self.live:
                    print(f"    [dry-run] would {side} {order_qty:.6f} {s}"
                          + (" (full exit via close_position)" if full_exit else ""))
                    action = f"would_{side}"
                else:
                    try:
                        if full_exit:
                            # Alpaca positions API requires the no-slash form for crypto
                            # ('DOGEUSD', not 'DOGE/USD') — using the slashed form returns
                            # 'Not Found' even though the position exists.
                            self.broker._client.close_position(to_alpaca_position_symbol(s))
                            print(f"    ✅ close_position {s} (full exit, no dust)")
                            action = "close"
                        else:
                            order = self.broker.submit_crypto_market_order(to_alpaca_symbol(s), order_qty, side)
                            print(f"    ✅ {side} {order_qty:.6f} {s} (id {order.id})")
                            action = side
                    except Exception as exc:  # noqa: BLE001
                        print(f"    ❌ order failed: {exc}")
                        action = "order_failed"
                    # Keep the no-margin cash pool current: sells free cash for
                    # later buys in this same loop; buys consume it.
                    if cash_avail is not None and action in ("buy", "sell", "close"):
                        if action == "buy":
                            cash_avail = max(0.0, cash_avail - order_qty * price)
                        else:
                            cash_avail += (cur_qty if action == "close" else order_qty) * price
            else:
                print("    no action (< $10)")

            decisions[s] = {
                "ensemble": round(float(ensemble_now.get(s, 0.0)), 3),
                "target_weight": round(float(w), 4),
                "price": price,
                "cur_qty": cur_qty,
                "held": cur_qty * price > 1.0,
                "sentiment": (round(sent["score"], 2) if sent else None),
                "sentiment_rationale": (sent["rationale"] if sent else None),
                "action": action,
                "order_usd": round(order_qty * price, 2),
            }

        # Persist this run's reasoning so the dashboard can explain every decision.
        try:
            with DECISIONS_PATH.open("a") as f:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "equity": round(equity, 2),
                    "vol_scale": round(scale, 3),
                    "live": self.live,
                    "coins": decisions,
                }) + "\n")
        except Exception:
            pass

        st.halted_today = self.risk._halted_today
        st.killed = self.risk._killed
        st.save(CRYPTO_STATE_PATH)

        # Telegram summary (silent no-op if creds not set)
        try:
            send_telegram(_format_crypto_msg(today, equity, scale, decisions, st.killed))
        except Exception:
            pass
        return st

    @staticmethod
    def _log_reason(r: str) -> None:
        print(f"  ⚠ {r}")


def _format_crypto_msg(today: str, equity: float, scale: float, decisions: dict, killed: bool) -> str:
    if killed:
        return (f"🚨 *CRYPTO KILL SWITCH*\n"
                f"{today} · equity ${equity:,.0f}\nAll positions halted by risk manager.")
    actions, holding, flat = [], [], []
    for coin, d in decisions.items():
        action = d.get("action", "")
        w = d.get("target_weight", 0)
        usd = d.get("order_usd", 0)
        sent = d.get("sentiment")
        if action in ("buy", "sell"):
            arrow = "📈 BUY" if action == "buy" else "📉 SELL"
            line = f"{arrow} *{coin}* → {w*100:.1f}% (${usd:,.0f})"
            if sent is not None and sent < 0:
                line += f" · sentiment {sent:+.2f} trimmed"
            actions.append(line)
        elif action == "order_failed":
            actions.append(f"❌ *{coin}*: order failed")
        elif w >= 0.005:        # held position, no rebalance trade needed
            line = f"💰 *{coin}* held @ {w*100:.1f}%"
            if sent is not None and sent < 0:
                line += f" · sentiment {sent:+.2f}"
            holding.append(line)
        else:
            flat.append(coin.replace("-USD", ""))
    lines = [f"🤖 *Crypto rebalance* · {today} UTC",
             f"Equity ${equity:,.0f} · vol-scale {scale:.2f}", ""]
    if actions:
        lines.extend(actions)
    if holding:
        if actions: lines.append("")
        lines.extend(holding)
    if not actions and not holding:
        lines.append("_All coins flat (0/3 rules) — sitting in cash._")
    if flat and (actions or holding):
        lines.append("")
        lines.append(f"Flat: {' '.join(flat)}")
    return "\n".join(lines)
