"""Opening-Range Breakout (ORB) intraday backtester — the Fabio Valentini /
"intraday momentum" strategy, delta removed, vol-targeted (per the video).

Rules per session (intraday bars indexed in US/Eastern):
  * Opening range = high/low of 09:30–10:00 ET.
  * After 10:00, the first 5-min candle that CLOSES above the range high → go long
    at that close. Stop = range low. Target = entry + RR×(entry−stop) (RR=1 → 1:1).
  * One trade/day, long only. If neither hit by ``exit_time`` (3pm), close there.
  * Sizing: risk a fixed fraction of equity per trade (vol-targeting), capped at
    ``max_lev`` notional; realistic per-side costs in bps of notional.

Intrabar exits are pessimistic: if a bar touches both stop and target, assume the
stop filled first.
"""

from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

from .metrics import compute_metrics


def _trade_equity_return(entry, stop, exit_px, risk_frac, cost_bps, max_lev):
    stop_dist = (entry - stop) / entry
    if stop_dist <= 0:
        return 0.0, 0.0
    notional_frac = min(risk_frac / stop_dist, max_lev)          # leverage-capped
    gross = notional_frac * (exit_px - entry) / entry
    cost = notional_frac * 2 * cost_bps / 10_000.0               # entry + exit
    return gross - cost, notional_frac


def orb_backtest(
    bars: pd.DataFrame,
    *,
    risk_frac: float = 0.0075,
    cost_bps: float = 2.0,
    max_lev: float = 3.0,
    rr: float = 1.0,
    or_end: time = time(10, 0),
    exit_time: time = time(15, 0),
    periods_per_year: int = 252,
) -> dict:
    trades, daily = [], {}
    for day, g in bars.groupby(bars.index.date):
        g = g.sort_index()
        opening = g[g.index.time < or_end]
        rest = g[(g.index.time >= or_end) & (g.index.time <= exit_time)]
        daily[day] = 0.0
        if len(opening) < 2 or rest.empty:
            continue
        rh, rl = opening["high"].max(), opening["low"].min()

        entry = stop = target = None
        exit_px = outcome = None
        for _, bar in rest.iterrows():
            if entry is None:
                if bar["close"] > rh and (bar["close"] - rl) > 0:
                    entry, stop = float(bar["close"]), float(rl)
                    target = entry + rr * (entry - stop)
                continue
            if bar["low"] <= stop:            # pessimistic: stop checked first
                exit_px, outcome = stop, "stop"
                break
            if bar["high"] >= target:
                exit_px, outcome = target, "target"
                break
        if entry is not None and exit_px is None:   # never hit -> 3pm close
            exit_px, outcome = float(rest.iloc[-1]["close"]), "time"
        if entry is None:
            continue

        ret, lev = _trade_equity_return(entry, stop, exit_px, risk_frac, cost_bps, max_lev)
        daily[day] = ret
        trades.append({
            "date": day, "entry": round(entry, 2), "stop": round(stop, 2),
            "exit": round(exit_px, 2), "outcome": outcome,
            "R": round((exit_px - entry) / (entry - stop), 2), "ret": round(ret, 5),
            "leverage": round(lev, 2),
        })

    daily_ret = pd.Series(daily).sort_index()
    daily_ret.index = pd.to_datetime(daily_ret.index)
    equity = (1 + daily_ret).cumprod()
    traded = (daily_ret != 0).astype(float)
    metrics = compute_metrics(daily_ret, equity, traded, periods_per_year)

    tdf = pd.DataFrame(trades)
    wins = tdf[tdf["ret"] > 0]["ret"] if not tdf.empty else pd.Series(dtype=float)
    losses = tdf[tdf["ret"] < 0]["ret"] if not tdf.empty else pd.Series(dtype=float)
    profit_factor = (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf")
    return {
        "trades": tdf,
        "daily_ret": daily_ret,
        "equity": equity,
        "metrics": metrics,
        "n_trades": len(tdf),
        "win_rate": float((tdf["ret"] > 0).mean()) if not tdf.empty else 0.0,
        "profit_factor": float(profit_factor),
        "ret_over_dd": float(metrics.total_return / abs(metrics.max_drawdown)) if metrics.max_drawdown else float("nan"),
    }
