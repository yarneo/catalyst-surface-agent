"""Weekly update of the adaptive sleeve allocations — HYBRID H_12_1w policy.

Backtest bake-off (2018-2026, see scripts/tripwire_compare.py and the
robustness study) showed this hybrid is the only tripwire that strictly
beats the no-tripwire static 33/33/33 baseline on ALL FOUR axes:
  Sharpe 1.22 → 1.38   CAGR +21.5% → +23.3%   Vol 17.2% → 16.2%   MaxDD -23.9% → -18.6%
  Deflated Sharpe 94% (corrected for ~75 policies tested).

Two trip conditions per sleeve, OR-combined:
  SLOW (mean-revert)
    - signal: rolling 60-day cumulative return < -3%
    - confirmation: 8 consecutive weekly readings (W-FRI)
    - fallback: 30% of freed weight to QLD-regime overlay, 70% to BIL (idle cash)
  FAST (panic / crash protection)
    - signal: rolling 20-day cumulative return < -12%
    - confirmation: 1 weekly reading (no extra confirmation)
    - fallback: 100% of freed weight to BIL (cash)

The SLOW trigger handles slow strategy decay (months of underperformance).
The FAST trigger catches severe crashes the slow window can't react to in
time — fires ~3 times in the 8-year backtest (Q4 2018, COVID 2020, 2022).

"Freed weight to BIL" is realized as idle cash in the allocation file; the
cash_park executor sweeps idle cash into BIL on its own daily run, so we
do NOT need a separate BIL allocation key. The allocation file's weights
may sum to <1.0 — the deficit is intentional and represents cash.
"""
from __future__ import annotations

import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from trading_bot.allocations import (
    DEFAULT_SLEEVE_ENABLEMENT,
    LIVE_SLEEVE_TARGETS,
    write_allocations,
)
from trading_bot.backtest.basket import trend_basket_panels, vol_targeted_basket_returns
from trading_bot.backtest.pead import pead_backtest
from trading_bot.config import load_crypto_universe, load_pead_universe
from trading_bot.data import load_ohlcv
from trading_bot.notify import send_telegram
from trading_bot.portfolio_policy import load_policy
from trading_bot.strategies import EnsembleTrend

warnings.filterwarnings("ignore")

# SLOW trigger params (mean-revert / slow decay)
SLOW_WINDOW_D     = 60
SLOW_THRESHOLD    = -0.03
SLOW_STREAK_WEEKS = 6   # 8→6 (2026-06-22): closes the shallow-persistent-grind gap the
                        # crypto sleeve fell into (bleed too shallow for the −12%/20d fast
                        # cut, too slow for an 8wk slow cut). Backtested Pareto: full Sharpe
                        # 1.50→1.51, 2025 0.81→0.96, bear flat (−0.29→−0.31), no whipsaw
                        # (streak=4 craters the bear to −0.49). See scripts/crypto_decision.py.
# Slow fallback split: when a sleeve trips on the slow rule, freed weight
# splits between regime (high-Sharpe equity-with-trend-filter) and BIL (cash).
# Where a slow-freed third goes. CHANGED 2026-07-18: 100% idle (→ regime-aware
# idle router via cash_park/run_managed_futures). Was {"regime": 0.30, "bil": 0.70},
# which raised leveraged QLD as a "risk-off" response — backwards.
SLOW_FALLBACK = {"regime": 0.0, "bil": 1.0}

# FAST trigger params (panic / crash protection)
FAST_WINDOW_D     = 20
FAST_THRESHOLD    = -0.12
FAST_STREAK_WEEKS = 1


def _rolling_cumret(ret: pd.Series, window: int) -> pd.Series:
    eq = (1 + ret.fillna(0)).cumprod()
    return (eq / eq.shift(window) - 1).fillna(0)


def _trip_streak(ret: pd.Series, window: int, threshold: float) -> int:
    """Count trailing consecutive weekly readings where rolling N-day cumret
    was below `threshold`. Each weekly reading is a COMPLETED Friday close.

    The trailing partial week is dropped: crypto trades Saturdays, so on a
    Sunday run the last W-FRI bucket (labelled the NEXT Friday) can contain a
    single Saturday bar — counting it let a 1-day observation confirm (or
    reset!) a "week", which fired the 2026-07-12 crypto trip after only 5
    completed weeks instead of the documented 6."""
    cumret = _rolling_cumret(ret, window)
    weekly = cumret.resample("W-FRI").last().dropna()
    # keep only buckets whose Friday label has already passed (completed weeks)
    last_obs = ret.index.max()
    weekly = weekly[weekly.index <= last_obs]
    streak = 0
    for v in reversed(weekly.values):
        if v < threshold:
            streak += 1
        else:
            break
    return streak


def _regime_returns(start: str, end: str) -> pd.Series:
    """QLD-regime sleeve daily returns: hold QLD when QQQ>200dma, BIL otherwise."""
    qqq = load_ohlcv("QQQ", "2009-01-01", end)["close"]
    qld = load_ohlcv("QLD", "2009-01-01", end)["close"]
    bil = load_ohlcv("BIL", "2009-01-01", end)["close"]
    sma = qqq.rolling(200).mean()
    pos = (qqq > sma).astype(float).shift(1).fillna(0.0)
    on_r  = qld.pct_change().fillna(0.0)
    off_r = bil.pct_change().fillna(0.0)
    common = pos.index.intersection(on_r.index).intersection(off_r.index)
    pos = pos.loc[common]; on_r = on_r.loc[common]; off_r = off_r.loc[common]
    gross = pos * on_r + (1 - pos) * off_r
    flips = pos.diff().abs().fillna(pos.abs())
    return (gross - flips * 5.0 / 10_000.0).loc[start:]


def main() -> None:
    runtime_policy = load_policy()["config"]
    slow_streak_weeks = int(runtime_policy["slow_streak"])
    end = datetime.now(timezone.utc).date().isoformat()
    start = "2018-01-01"
    print("Computing adaptive allocations · HYBRID policy (slow Z30 + fast crash)")
    print(f"  SLOW: {SLOW_WINDOW_D}d cumret < {SLOW_THRESHOLD*100:.0f}% × {slow_streak_weeks}wk → "
          f"{int(SLOW_FALLBACK['regime']*100)}r/{int(SLOW_FALLBACK['bil']*100)}b")
    print(f"  FAST: {FAST_WINDOW_D}d cumret < {FAST_THRESHOLD*100:.0f}% × {FAST_STREAK_WEEKS}wk → BIL")

    # Build per-sleeve daily returns
    print("PEAD backtest…")
    uni = load_pead_universe()
    pead_r = pead_backtest(uni, start, end, hold_days=60, concordant=True,
                            long_only=True, cost_bps=5.0)["returns"]
    if pead_r.index.tz is not None: pead_r.index = pead_r.index.tz_localize(None)

    print("Crypto trend backtest…")
    coins = load_crypto_universe()
    data = {c: load_ohlcv(c, start, end) for c in coins}
    R, W = trend_basket_panels(data, EnsembleTrend())
    crypto_r, _ = vol_targeted_basket_returns(R, W, target_ann_vol=0.20, lookback=30,
                                              periods_per_year=365, max_leverage=1.0, cost_bps=20)
    if crypto_r.index.tz is not None: crypto_r.index = crypto_r.index.tz_localize(None)

    print("Regime sleeve (QLD-regime) backtest…")
    regime_r = _regime_returns(start, end)
    if regime_r.index.tz is not None: regime_r.index = regime_r.index.tz_localize(None)

    sleeve_rets = {"pead": pead_r, "crypto": crypto_r, "regime": regime_r}

    # Compute trip state + most-recent cumret per sleeve (for dashboard display)
    streaks = {"slow": {}, "fast": {}}
    trips   = {"slow": {}, "fast": {}}
    cumret  = {"slow": {}, "fast": {}}
    print()
    for k, r in sleeve_rets.items():
        slow_s = _trip_streak(r, SLOW_WINDOW_D, SLOW_THRESHOLD)
        fast_s = _trip_streak(r, FAST_WINDOW_D, FAST_THRESHOLD)
        # Latest rolling cumret (for "distance to trip" display)
        cr_slow = _rolling_cumret(r, SLOW_WINDOW_D)
        cr_fast = _rolling_cumret(r, FAST_WINDOW_D)
        streaks["slow"][k] = slow_s
        streaks["fast"][k] = fast_s
        cumret["slow"][k]  = round(float(cr_slow.iloc[-1] if len(cr_slow) else 0.0), 5)
        cumret["fast"][k]  = round(float(cr_fast.iloc[-1] if len(cr_fast) else 0.0), 5)
        trips["slow"][k] = slow_s >= slow_streak_weeks
        trips["fast"][k] = fast_s >= FAST_STREAK_WEEKS
        print(f"  {k:<6}  60d cumret {cumret['slow'][k]*100:>+6.2f}% (trip <-3%)  "
              f"20d cumret {cumret['fast'][k]*100:>+6.2f}% (trip <-12%)  "
              f"streak {slow_s}/{slow_streak_weeks} slow, {fast_s}/{FAST_STREAK_WEEKS} fast  "
              f"→ {'FAST-TRIPPED' if trips['fast'][k] else ('SLOW-TRIPPED' if trips['slow'][k] else 'healthy')}")

    # Compute weights from the same production targets the executors enforce.
    # If tripped, zero the sleeve and route the freed capital per the policy.
    # PEAD RETIRED 2026-06-26: validated (t-stat 2.5, wins 6/9yr, holds ex-2022) that
    # the portfolio is better WITHOUT it — PEAD is equity-correlated (0.76) and
    # bear-fragile, and its capital is better used by the split idle router (50% QQQ /
    # 50% MF). So PEAD starts at 0 weight; its third becomes idle → router. We still
    # compute its health above (display only) in case we ever revive it.
    weights = {
        sleeve: target if DEFAULT_SLEEVE_ENABLEMENT.get(sleeve, True) else 0.0
        for sleeve, target in LIVE_SLEEVE_TARGETS.items()
    }
    for k in ("regime", "crypto"):
        if trips["fast"][k] or trips["slow"][k]:
            # Tripped (either speed) → weight 0. ALL freed weight stays idle and
            # is picked up by the regime-aware idle router (50/50 QQQ/MF in a
            # bull, all-MF in a bear) after the BIL/cash buffers.
            # CHANGED 2026-07-18 (user-approved): the old rule sent 30% of a
            # slow-freed third to the REGIME sleeve, so a crypto trip RAISED 2×
            # QLD from 33%→43% — MORE leveraged Nasdaq as the "risk-off"
            # response (tripped-bull-day Nasdaq beta 1.05-1.09 vs 0.77 normal).
            # Routing everything through the diversified router: same CAGR,
            # lower vol (16.0 vs 16.2), saner crash exposure, one less special
            # case. (Backtest Sharpe 1.369 vs 1.352 — treated as a wash, the
            # change is beta-sanity, not alpha.)
            weights[k] = 0.0

    # Verdicts (for dashboard display)
    verdicts = {}
    for k in ("pead", "crypto", "regime"):
        if not DEFAULT_SLEEVE_ENABLEMENT.get(k, False):
            verdicts[k] = "DISABLED"
        elif trips["fast"][k]:
            verdicts[k] = "FAST_TRIPPED"
        elif trips["slow"][k]:
            verdicts[k] = "TRIPPED"
        elif streaks["slow"][k] > 0:
            verdicts[k] = "DECAYING"
        else:
            verdicts[k] = "HEALTHY"

    total = sum(weights.values())
    print(f"\nFinal weights: QQQ {weights['qqq']:.0%} · Regime {weights['regime']:.0%} · "
          f"PEAD {weights['pead']:.0%} (RETIRED) · Crypto {weights['crypto']:.0%}  "
          f"(sum {total:.0%}, idle {(1-total)*100:.0f}% → split router via run_managed_futures)")
    print(f"Verdicts: {verdicts}")

    write_allocations(
        weights=weights,
        policy="hybrid_h12_1w",
        streaks={"pead":   streaks["slow"]["pead"],
                 "crypto": streaks["slow"]["crypto"],
                 "regime": streaks["slow"]["regime"]},
        verdicts=verdicts,
        lookback_days=SLOW_WINDOW_D,
        streak_threshold_weeks=slow_streak_weeks,
    )
    # Also stash hybrid-specific state for dashboard / debugging
    import json
    from pathlib import Path
    f = Path(__file__).resolve().parents[1] / "data" / "sleeve_allocations.json"
    cur = json.loads(f.read_text())
    cur["hybrid"] = {
        "slow": {"window_d": SLOW_WINDOW_D, "threshold": SLOW_THRESHOLD,
                 "streak_weeks": slow_streak_weeks, "fallback": SLOW_FALLBACK,
                 "streaks": streaks["slow"], "trips": {k: bool(v) for k, v in trips["slow"].items()},
                 "cumret": cumret["slow"]},
        "fast": {"window_d": FAST_WINDOW_D, "threshold": FAST_THRESHOLD,
                 "streak_weeks": FAST_STREAK_WEEKS, "fallback": "BIL",
                 "streaks": streaks["fast"], "trips": {k: bool(v) for k, v in trips["fast"].items()},
                 "cumret": cumret["fast"]},
        "idle_to_bil_pct": round((1 - total) * 100, 1),
    }
    # Atomic (tmp+rename): a crash mid-write must not leave a truncated file.
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, indent=2))
    tmp.replace(f)
    print("\nSaved → data/sleeve_allocations.json")

    # Telegram alert if anything new tripped or recovered
    any_tripped = [k for k, v in verdicts.items() if v in ("TRIPPED", "FAST_TRIPPED")]
    if any_tripped:
        labels = ", ".join(f"{k} ({verdicts[k].split('_')[0].lower()})" for k in any_tripped)
        msg = (f"⚠️ *Adaptive allocation update (H_12_1w hybrid)*\n"
               f"Tripped: {labels}\n"
               f"Weights: QQQ {weights['qqq']:.0%} · Regime {weights['regime']:.0%} · "
               f"PEAD {weights['pead']:.0%} · Crypto {weights['crypto']:.0%}  "
               f"({(1-total)*100:.0f}% idle → BIL)")
        try: send_telegram(msg)
        except Exception: pass


if __name__ == "__main__":
    main()
