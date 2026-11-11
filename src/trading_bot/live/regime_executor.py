"""Regime-leveraged equity sleeve (QLD-regime).

Pre-specified rule (no fitting, no parameters that vary):
  - Filter: QQQ closing price vs its 200-day SMA
  - QQQ > 200dma  → hold QLD (2x QQQ ETF, daily reset)
  - QQQ <= 200dma → hold $0 (the cash_park sleeve will absorb the cash into BIL)
  - Daily check; rebalance only when drift >= rebal_threshold

  Note on OFF state: the backtest models BIL during regime-OFF; in live we let
  cash_park_executor handle that automatically (it parks excess cash into BIL
  above a 10% buffer). So when this sleeve sells QLD, the cash flows to cash
  and gets reparked, matching the backtest behavior without two executors
  fighting over BIL.

Why this sleeve: hypothesis-driven (Faber / "200dma" timing literature), tested
on TQQQ-era 2010-2026 with Sharpe 0.88 / CAGR 25% (QLD version). In the
combined portfolio test (vs current 33/33/33 baseline at Sharpe 1.20 / CAGR 18.3%):
  - Replace QQQ → QLD-regime (33/33/33): Sharpe 1.23 / CAGR 21.5% / vol 17.2%
  - Partial: 20 QQQ + 13 QLD-regime + 33 PEAD + 33 Crypto: Sh 1.25 / CAGR 19.7%

Both raise Sharpe & CAGR with minimal vol increase — the goal asked.

Risks:
  - QLD has volatility decay (daily 2x leverage). Backtest uses real QLD prices
    so decay is captured.
  - 2018 (-12.9%) and 2011 (-22.7%) were whipsaw years; mix with PEAD/Crypto
    smooths these out.
  - DD on the sleeve alone ~-42% (vs QQQ -35%). In the mix, DD stays near
    baseline (-23.7% → -23.9%).
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

import pandas as pd

from ..broker import AlpacaBroker
from ..marketdata import last_close, last_close_and_sma, realized_vol
from ..notify import send_telegram
from ..shadow_regime import (
    propose_shadow_target,
    propose_shadow_targets,
    record_shadow_portfolios,
)


class RegimeExecutor:
    def __init__(
        self,
        broker: AlpacaBroker | None = None,
        allocation: float = 0.13,                # fraction of TOTAL equity
        filter_symbol: str = "QQQ",
        on_symbol: str = "QLD",                  # held when filter > SMA (risk-on)
        sma_n: int = 200,
        # 5%, not 2%. Vol scaling moves the target every day, so a 2% band
        # would rebalance almost daily and pay the spread each time. The
        # backtest that justified vol targeting models costless daily
        # rebalancing; 5% keeps most of the benefit at a fraction of the
        # turnover. Matches ManagedFuturesExecutor.rebal_threshold.
        rebal_threshold: float = 0.05,
        target_vol: float | None = 0.18,   # None disables vol scaling
        vol_window: int = 20,
        health_verdict: str = "HEALTHY",
        live: bool = False,
    ):
        self.broker = broker or AlpacaBroker()
        self.allocation = allocation
        self.filter_symbol = filter_symbol
        self.on_symbol = on_symbol
        self.sma_n = sma_n
        self.rebal_threshold = rebal_threshold
        self.target_vol = target_vol
        self.vol_window = vol_window
        self.health_verdict = health_verdict
        self.live = live

    def _filter_state(self) -> tuple[bool, float, float]:
        """Returns (regime_on, last_close, sma_value).

        Raises PriceUnavailable if the filter can't be evaluated. Do NOT catch
        that and fall back to a default: a NaN close used to make ``last > sma``
        evaluate False, reading as a confirmed bear signal, and this sleeve
        would close its whole QLD position on a data glitch (see
        trading_bot/marketdata.py). Skipping the cycle is the safe response.
        """
        last, sma = last_close_and_sma(self.filter_symbol, self.sma_n)
        return last > sma, last, sma

    @staticmethod
    def _price(sym: str) -> float:
        return last_close(sym)

    def run(self) -> dict:
        acct = self.broker.get_account()
        total_equity = float(acct.portfolio_value)
        allocated = total_equity * self.allocation

        regime_on, last_close, sma_val = self._filter_state()

        # Scale exposure inversely to realised volatility. Measured over 19.1
        # years (2007-05..2026-08), holding the sleeve at a flat 70%:
        #
        #     flat        CAGR +16.33%  Sharpe 0.69  maxDD -31.5%
        #     vol-target  CAGR +15.73%  Sharpe 0.75  maxDD -24.9%
        #
        # and it was better in EVERY crash in the sample, not just on average
        # (2008 -11.3% vs -12.5%, 2018Q4 -21.1% vs -26.7%, COVID -7.1% vs
        # -11.2%, 2022 -15.4% vs -21.9%). Volatility is persistent, so a
        # high-vol reading is genuine information about tomorrow's risk in a
        # way that a price level is not. Capped at 1.0 — this only ever REDUCES
        # exposure, it never levers the sleeve beyond its allocation.
        #
        # The 20-day RAW realised vol is deliberate, not laziness. HAR-RV is a
        # far better FORECAST of it (OOS R² 0.231 vs -0.046) and made the
        # strategy WORSE at every target level (Sharpe 0.85 -> 0.84 at 18%,
        # drawdown -24.0% -> -26.2%). The sizing rule caps at 1.0, so the
        # forecast only bites in stress — and HAR mean-reverts, reading 32.7%
        # where raw reads 40.1% on the worst-decile days, so it de-risks less
        # exactly when that matters. Over-sizing in turbulence costs far more
        # than under-sizing in calm: the loss is asymmetric, so a conservative
        # bias beats an unbiased estimate. Do not "upgrade" this without
        # re-running research/vol-forecasting/strategy_impact.py.
        vol_scale = 1.0
        rv = None
        if self.target_vol:
            rv = realized_vol(self.filter_symbol, self.vol_window)
            vol_scale = min(1.0, self.target_vol / rv)
            print(f"  Vol: {self.filter_symbol} realised {rv:.1%} vs target "
                  f"{self.target_vol:.0%} → scale {vol_scale:.2f}x")

        # When regime is OFF we want $0 in the QLD position; cash_park_executor
        # absorbs the freed cash into BIL on its own daily run.
        target_usd = allocated * vol_scale if regime_on else 0.0

        # Observe a stricter alternative without granting it order authority.
        # The shadow caps QLD at 25% of equity, limits its standalone volatility
        # contribution to six vol points, and halves again while health is
        # DECAYING. Its ledger lets us compare before changing live behavior.
        # The sizing input is QQQ volatility, but QLD targets 2x QQQ daily.
        # Use the leveraged proxy for the challenger's risk budget.
        shadow_rv = rv * 2.0 if rv is not None else None
        shadow = propose_shadow_target(
            live_allocation=self.allocation,
            vol_scale=vol_scale,
            realized_vol=shadow_rv,
            regime_on=regime_on,
            health_verdict=self.health_verdict,
        )
        shadow_variants = propose_shadow_targets(
            live_allocation=self.allocation,
            vol_scale=vol_scale,
            realized_vol=shadow_rv,
            regime_on=regime_on,
            health_verdict=self.health_verdict,
        )
        shadow_row = None
        if self.live:
            try:
                shadow_row = record_shadow_portfolios(
                    shadow_variants,
                    price=self._price(self.on_symbol),
                    observed_equity=total_equity,
                    realized_vol=shadow_rv,
                    regime_on=regime_on,
                    health_verdict=self.health_verdict,
                )
            except Exception as exc:  # shadow evidence must never break live execution
                print(f"  ⚠ shadow QLD recorder failed: {exc}")
        print(
            f"  Shadow challenger (NO TRADES): {shadow.shadow_target_weight:.1%} "
            f"vs live {shadow.live_target_weight:.1%} · {shadow.limiting_reason}"
        )

        positions = {p.symbol: float(p.market_value) for p in self.broker.get_positions()}
        cur_target = positions.get(self.on_symbol, 0.0)

        today = datetime.now(timezone.utc).date().isoformat()
        mode = "LIVE (paper)" if self.live else "DRY-RUN"
        print(f"\n=== REGIME SLEEVE ({self.on_symbol}) | {today} | {mode} ===")
        print(f"  Filter: {self.filter_symbol} ${last_close:,.2f} vs SMA{self.sma_n} ${sma_val:,.2f}  "
              f"→ regime {'ON (hold ' + self.on_symbol + ')' if regime_on else 'OFF (hold cash)'}")
        print(f"  Equity ${total_equity:,.0f} | allocated ${allocated:,.0f} ({self.allocation:.0%})  "
              f"| target {self.on_symbol} ${target_usd:,.0f}")

        snapshot = {
            "total_equity": round(total_equity, 2),
            "allocated":    round(allocated, 2),
            "regime_on":    regime_on,
            "filter_close": round(last_close, 2),
            "filter_sma":   round(sma_val, 2),
            "on_symbol":    self.on_symbol,
            "actions":      [],
            "shadow_challenger": asdict(shadow),
            "shadow_variants": shadow_row.get("variants", {}) if shadow_row else {},
        }
        actions = []

        delta_usd = target_usd - cur_target
        drift = abs(delta_usd) / max(target_usd, 1.0) if target_usd > 0 else (
            1.0 if cur_target > 1.0 else 0.0)
        print(f"  {self.on_symbol}: held ${cur_target:,.0f} vs target ${target_usd:,.0f}  drift {drift:.1%}")
        snapshot[self.on_symbol] = {"held": round(cur_target, 2),
                                    "target": round(target_usd, 2),
                                    "drift":  round(drift, 4)}

        # Three branches: regime OFF (close any QLD), regime ON small drift (hold),
        # regime ON big drift (buy/sell).
        if target_usd <= 1.0:
            if cur_target > 1.0:
                print(f"  Regime OFF → closing {self.on_symbol} (${cur_target:,.0f})")
                if self.live:
                    try:
                        self.broker.close_position(self.on_symbol, sleeve="regime")
                        print(f"    ✅ close-position request submitted")
                        actions.append({"sym": self.on_symbol, "side": "close", "qty": 0,
                                        "notional": cur_target})
                        snapshot[self.on_symbol]["action"] = "close_regime_off"
                    except Exception as exc:  # noqa: BLE001
                        print(f"    ❌ close failed: {exc}")
                        snapshot[self.on_symbol]["action"] = "close_failed"
                        raise
                else:
                    print(f"    [dry-run] would close {self.on_symbol}")
                    snapshot[self.on_symbol]["action"] = "would_close"
            else:
                snapshot[self.on_symbol]["action"] = "flat_off"
        elif drift < self.rebal_threshold:
            snapshot[self.on_symbol]["action"] = "hold"
        else:
            try:
                price = self._price(self.on_symbol)
            except Exception as exc:  # noqa: BLE001
                print(f"    ⚠ price lookup failed: {exc}")
                snapshot[self.on_symbol]["action"] = "price_failed"
                price = None
            if price is not None:
                qty = round(abs(delta_usd) / price, 4)
                side = "buy" if delta_usd > 0 else "sell"
                print(f"    {side} {qty} {self.on_symbol} @ ${price:,.2f} (${qty*price:,.0f})")
                # The broker enforces reserves and fills any funding sale first.
                if self.live and qty > 0:
                    try:
                        order = self.broker.submit_market_order(
                            self.on_symbol, qty, side,
                            reference_price=price, sleeve="regime",
                        )
                        print(f"    ✅ submitted (id {order.id})")
                        actions.append({"sym": self.on_symbol, "side": side, "qty": qty,
                                        "price": price, "notional": qty * price})
                        snapshot[self.on_symbol]["action"] = side
                    except Exception as exc:  # noqa: BLE001
                        print(f"    ❌ order failed: {exc}")
                        snapshot[self.on_symbol]["action"] = "failed"
                        raise
                else:
                    snapshot[self.on_symbol]["action"] = f"would_{side}"

        snapshot["actions"] = actions
        if actions:
            try:
                send_telegram(_format_regime_msg(today, total_equity, allocated,
                                                  regime_on, self.on_symbol,
                                                  last_close, sma_val, actions))
            except Exception:
                pass
        return snapshot


def _format_regime_msg(today: str, equity: float, allocated: float, regime_on: bool,
                       on_sym: str, close: float, sma: float, actions: list) -> str:
    regime_label = f"ON (hold {on_sym})" if regime_on else "OFF (hold cash)"
    arrow_emoji = "🟢" if regime_on else "🔴"
    lines = [f"⚙️ *Regime sleeve ({on_sym})* · {today}",
             f"Equity ${equity:,.0f} · allocated ${allocated:,.0f}",
             f"{arrow_emoji} Regime {regime_label}  ·  QQQ ${close:,.2f}  SMA200 ${sma:,.2f}", ""]
    for a in actions:
        if a["side"] == "close":
            lines.append(f"♻ *{a['sym']}* liquidated (~${a['notional']:,.0f})")
        else:
            arrow = "📈 BUY" if a["side"] == "buy" else "📉 SELL"
            lines.append(f"{arrow} *{a['sym']}* {a['qty']:.2f} sh @ ${a['price']:.2f} "
                         f"(~${a['notional']:,.0f})")
    return "\n".join(lines)
