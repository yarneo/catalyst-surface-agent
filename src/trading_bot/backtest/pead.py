"""Post-Earnings Announcement Drift (PEAD) backtester.

After an earnings surprise, prices tend to drift in the surprise's direction for
~60 trading days (Ball & Brown 1968; Bernard & Thomas 1989). Strategy (the video's
"by the book" long-only version):

  * Signal on each earnings event: if EPS beat consensus AND the price reacted up
    ("concordant"), go long. (Short leg documented to be weak post-2010 → omitted.)
  * Timing: after-close announcement reacts next day; before-open reacts same day.
    Enter at the OPEN of the session after the reaction; hold ``hold_days`` (60),
    no stop/target.
  * Sizing: fixed fraction of equity per position; cap concurrent positions.

Earnings data: yfinance (free; EPS estimate/actual/surprise + timestamp). Prices:
adjusted daily via our loader. Known caveats: survivorship (yfinance lists current
tickers only) and the consensus estimate may not be strictly point-in-time.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..data import load_ohlcv
from ..data.loader import CACHE_DIR
from .metrics import compute_metrics


def earnings_events(symbol: str, limit: int = 40, use_cache: bool = True) -> pd.DataFrame:
    """Past earnings with surprise % and an after-close (AMC) flag from the time.
    Cached to parquet so multi-config backtests don't re-hit yfinance per run.

    NB: ``symbol`` must be a single ticker string. Passing a list silently
    produces a cache filename containing the stringified list, which can
    exceed Linux's NAME_MAX (255 bytes) and crashes with `OSError: File
    name too long`. We validate up front so the failure mode is obvious.
    """
    if not isinstance(symbol, str):
        raise TypeError(
            f"earnings_events(symbol=...) expects a str ticker, got {type(symbol).__name__}. "
            "Iterate the universe and call this per-symbol."
        )
    cache_file = CACHE_DIR / f"earnings_{symbol}.parquet"
    if use_cache and cache_file.exists():
        return pd.read_parquet(cache_file)

    import yfinance as yf

    try:
        ed = yf.Ticker(symbol).get_earnings_dates(limit=limit)
    except Exception:
        return pd.DataFrame()
    if ed is None or ed.empty:
        return pd.DataFrame()
    rows = []
    for ts, r in ed.iterrows():
        if pd.isna(r.get("Reported EPS")) or pd.isna(r.get("EPS Estimate")):
            continue
        rows.append({
            "date": pd.Timestamp(ts).tz_localize(None).normalize(),
            "amc": pd.Timestamp(ts).hour >= 12,          # afternoon = after close
            "surprise": float(r.get("Surprise(%)", 0.0) or 0.0),
        })
    out = pd.DataFrame(rows).sort_values("date").reset_index(drop=True) if rows else pd.DataFrame()
    if use_cache and not out.empty:
        out.to_parquet(cache_file)
    return out


def _trades_for_symbol(symbol, prices, events, hold_days, min_surprise, concordant, long_only):
    o, c = prices["open"].to_numpy(), prices["close"].to_numpy()
    idx = prices.index
    trades = []
    for _, e in events.iterrows():
        if abs(e["surprise"]) < min_surprise:
            continue
        pos = idx.searchsorted(e["date"])                # first trading day >= announce
        if pos >= len(idx):
            continue
        react = pos + 1 if e["amc"] else pos             # reaction day
        entry_i = react + 1                              # enter next open
        exit_i = entry_i + hold_days
        if exit_i >= len(idx) or react < 1:
            continue
        reaction = c[react] / c[react - 1] - 1.0
        long = e["surprise"] > 0 and (reaction > 0 or not concordant)
        short = e["surprise"] < 0 and (reaction < 0 or not concordant)
        direction = 1 if long else (-1 if (short and not long_only) else 0)
        if direction == 0:
            continue
        trades.append({
            "symbol": symbol, "entry_i": entry_i, "exit_i": exit_i,
            "entry_date": idx[entry_i], "exit_date": idx[exit_i],
            "entry": o[entry_i], "exit": c[exit_i], "direction": direction,
            "ret": direction * (c[exit_i] / o[entry_i] - 1.0),
        })
    return trades


def pead_backtest(
    universe: list[str], start: str, end: str, *,
    hold_days: int = 60, position_frac: float = 0.10, max_positions: int = 10,
    min_surprise: float = 0.0, concordant: bool = True, long_only: bool = True,
    cost_bps: float = 5.0, initial: float = 100_000.0,
) -> dict:
    prices, all_trades = {}, []
    for s in universe:
        try:
            df = load_ohlcv(s, start, end)
        except Exception:
            continue
        # Series.asof requires a monotonic index. Cached vendor frames can be
        # returned newest-first, and duplicate session rows occasionally appear
        # after a partial-day refresh. Normalize once at the boundary so every
        # later search/asof operation is deterministic.
        df = df.sort_index()
        df = df.loc[~df.index.duplicated(keep="last")]
        ev = earnings_events(s)
        if df.empty or ev.empty:
            continue
        prices[s] = df
        all_trades += _trades_for_symbol(s, df, ev, hold_days, min_surprise, concordant, long_only)

    # Master trading-day calendar (union of all symbols).
    cal = sorted(set().union(*[set(df.index) for df in prices.values()]))
    cal = pd.DatetimeIndex(cal)
    by_entry: dict = {}
    for t in all_trades:
        by_entry.setdefault(t["entry_date"], []).append(t)

    cash = initial
    open_pos: list[dict] = []          # {symbol, alloc, entry_price, exit_date, direction}
    equity_curve = []
    n_open_series = []                 # how many positions are open at end of each day
    deployed_frac_series = []          # fraction of running equity that's in positions
    closed = []
    for day in cal:
        # close due positions
        still = []
        for p in open_pos:
            if day >= p["exit_date"]:
                px = prices[p["symbol"]]["close"].asof(day)
                pnl = p["alloc"] * p["direction"] * (px / p["entry_price"] - 1.0)
                cost = p["alloc"] * 2 * cost_bps / 10_000.0
                cash += p["alloc"] + pnl - cost
                closed.append({**{k: p[k] for k in ("symbol", "direction")},
                               "exit_date": day, "pnl": pnl - cost})
            else:
                still.append(p)
        open_pos = still
        # open new entries (fixed fraction of starting capital per position)
        for t in by_entry.get(day, []):
            if len(open_pos) >= max_positions or cash <= 0:
                break
            alloc = min(position_frac * initial, cash)
            if alloc <= 0:
                continue
            cash -= alloc
            open_pos.append({"symbol": t["symbol"], "alloc": alloc, "entry_price": t["entry"],
                             "exit_date": t["exit_date"], "direction": t["direction"]})
        # mark to market
        mtm = sum(p["alloc"] * (1 + p["direction"] * (prices[p["symbol"]]["close"].asof(day) / p["entry_price"] - 1.0))
                  for p in open_pos)
        total = cash + mtm
        equity_curve.append(total)
        n_open_series.append(len(open_pos))
        deployed_frac_series.append(mtm / total if total > 0 else 0.0)

    eq = pd.Series(equity_curve, index=cal)
    ret = eq.pct_change().fillna(0.0)
    metrics = compute_metrics(ret, eq, (ret != 0).astype(float), 252)
    cdf = pd.DataFrame(closed)
    wins = (cdf["pnl"] > 0).mean() if not cdf.empty else 0.0
    pf = (cdf[cdf.pnl > 0].pnl.sum() / abs(cdf[cdf.pnl < 0].pnl.sum())) if (not cdf.empty and (cdf.pnl < 0).any()) else float("inf")
    return {
        "equity": eq, "returns": ret, "metrics": metrics, "trades": cdf,
        "n_open_positions": pd.Series(n_open_series, index=cal),
        "deployed_frac": pd.Series(deployed_frac_series, index=cal),
        "n_trades": len(closed), "win_rate": float(wins), "profit_factor": float(pf),
        "calmar": float(metrics.cagr / abs(metrics.max_drawdown)) if metrics.max_drawdown else float("nan"),
        "n_symbols": len(prices),
    }
