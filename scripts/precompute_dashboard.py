"""Nightly pre-compute for the FastAPI dashboard.

Heavy backtests (team_vs_hold, crash_perf) run here and get written to
``data/dashboard/*.json``. The dashboard reads those files at request time
so cold loads are <1s and survive service restarts.

Cron: ``0 12 * * * cron_run.sh precompute_dashboard``  (12:00 UTC daily)
"""
from __future__ import annotations

import json
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from trading_bot.backtest.basket import trend_basket_panels, vol_targeted_basket_returns
from trading_bot.backtest.metrics import compute_metrics
from trading_bot.backtest.pead import pead_backtest
from trading_bot.config import load_crypto_universe, load_pead_universe
from trading_bot.data import load_ohlcv
from trading_bot.strategies import EnsembleTrend

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
OUT  = ROOT / "data" / "dashboard"
OUT.mkdir(parents=True, exist_ok=True)


# ---------------------------- helpers ----------------------------
def _eq_curve(ret: pd.Series, start_value: float = 100_000) -> list[dict]:
    eq = (1 + ret.dropna()).cumprod() * start_value
    return [{"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
            for d, v in eq.items()]


def _metrics_dict(ret: pd.Series, periods: int = 252) -> dict:
    r = ret.dropna()
    if len(r) < 30:
        return {"sharpe": 0, "cagr": 0, "vol": 0, "max_dd": 0}
    m = compute_metrics(r, (1 + r).cumprod(), (r != 0).astype(float), periods)
    return {
        "sharpe": round(float(m.sharpe), 2),
        "cagr":   round(float(m.cagr), 4),
        "vol":    round(float(m.ann_volatility), 4),
        "max_dd": round(float(m.max_drawdown), 4),
    }


def _rolling_cumret(ret: pd.Series, window: int = 60) -> pd.Series:
    eq = (1 + ret.fillna(0)).cumprod()
    return (eq / eq.shift(window) - 1).fillna(0)


def _adaptive_weights4(qqq_r, pead_r, crypto_r, regime_r, *,
                       slow_window=60, slow_threshold=-0.03, slow_streak=6,
                       slow_regime_share=0.0,
                       fast_window=20, fast_threshold=-0.12, fast_streak=1) -> pd.DataFrame:
    """HYBRID H_12_1w tripwire — mirrors the live rule in update_allocations.py.

    Per sleeve:
      FAST trip = 20d cumret < -12% × 1wk → weight 0 (freed third → idle router)
      SLOW trip = 60d cumret < -3% × 6wk → weight 0 (freed third → idle router)
    FAST > SLOW. CHANGED 2026-07-18 (user-approved, matches live): a slow-freed
    third used to send 30% to the regime sleeve — raising 2× QLD as a
    "risk-off" response. Now ALL freed weight flows to the regime-aware idle
    router via the deficit; `slow_regime_share` defaults 0 (kept as a knob for
    weekly_research to gauntlet).
    """
    third = 1 / 3.0

    def _streak_signal(ret, window, threshold, streak_weeks):
        cumret = _rolling_cumret(ret, window).shift(1)
        weekly = cumret.resample("W-FRI").last()
        neg = (weekly < threshold).astype(int)
        s = pd.Series(0, index=weekly.index)
        run = 0
        for i, v in enumerate(neg.values):
            run = run + 1 if v == 1 else 0
            s.iat[i] = run
        return (s.reindex(qqq_r.index, method="ffill").fillna(0) >= streak_weeks).astype(float)

    sleeves = ("pead", "crypto", "regime")
    rets = {"pead": pead_r, "crypto": crypto_r, "regime": regime_r}
    slow_t = {k: _streak_signal(rets[k], slow_window, slow_threshold, slow_streak) for k in sleeves}
    fast_t = {k: _streak_signal(rets[k], fast_window, fast_threshold, fast_streak) for k in sleeves}

    # First compute each sleeve's own weight (zero if tripped on either trigger).
    any_t = {k: ((fast_t[k] + slow_t[k]) > 0).astype(float) for k in sleeves}
    self_w = {k: third * (1 - any_t[k]) for k in sleeves}

    # Optional regime bonus on slow-only trips (guarded on regime being healthy).
    # slow_regime_share is 0 since 2026-07-18 — the freed third flows entirely to
    # the idle router via the deficit — but the knob stays so weekly_research can
    # keep the old 0.30 policy in its candidate gauntlet.
    regime_active = 1.0 - ((fast_t["regime"] + slow_t["regime"]) > 0).astype(float)
    regime_bonus = pd.Series(0.0, index=qqq_r.index)
    if slow_regime_share > 0:
        for k in ("regime", "crypto"):
            slow_only = (slow_t[k] - fast_t[k]).clip(lower=0)
            regime_bonus = regime_bonus + slow_only * third * slow_regime_share * regime_active

    w_pead   = pd.Series(0.0, index=qqq_r.index)  # PEAD retired 2026-06-26 → weight 0 (matches live)
    w_crypto = self_w["crypto"]
    w_regime = self_w["regime"] + regime_bonus
    w_qqq    = pd.Series(0.0, index=qqq_r.index)

    return pd.DataFrame({"qqq": w_qqq, "regime": w_regime, "pead": w_pead, "crypto": w_crypto},
                        index=qqq_r.index)


def _regime_sleeve_returns(start: str, end: str) -> pd.Series:
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
    out = (gross - flips * 5.0 / 10_000.0).loc[start:]
    if out.index.tz is not None: out.index = out.index.tz_localize(None)
    return out


def _mf_sleeve_returns(end: str, td, bil_td) -> pd.Series:
    """Managed-futures sleeve daily returns aligned to index `td`: mean of
    whichever of {DBMF, KMLM} exists on each date (50/50 once both do, DBMF
    alone 2019-05..2020-12), falling back to BIL before either fund existed
    — matching how idle capital behaved pre-sleeve. Mirrors the live
    ManagedFuturesExecutor (DBMF+KMLM 50/50)."""
    def _r(sym, since):
        try:
            s = load_ohlcv(sym, since, end)["close"].pct_change()
            if s.index.tz is not None: s.index = s.index.tz_localize(None)
            return s.reindex(td)
        except Exception:
            return pd.Series(index=td, dtype=float)
    both = pd.concat([_r("DBMF", "2019-01-01"), _r("KMLM", "2020-01-01")], axis=1)
    mf = both.mean(axis=1, skipna=True)
    return mf.fillna(bil_td).fillna(0.0)


# ----------------------- team_vs_hold snapshot -----------------------
def build_team_vs_hold() -> dict:
    start = "2018-01-01"
    end = (pd.Timestamp.utcnow().date() + pd.Timedelta(days=1)).isoformat()

    print(f"  PEAD backtest…")
    pead_res = pead_backtest(load_pead_universe(), start, end, hold_days=60,
                              concordant=True, long_only=True, cost_bps=5.0)
    pead = pead_res["returns"]
    if pead.index.tz is not None: pead.index = pead.index.tz_localize(None)

    print(f"  Crypto trend backtest…")
    data = {c: load_ohlcv(c, start, end) for c in load_crypto_universe()}
    R, W = trend_basket_panels(data, EnsembleTrend())
    crypto, _ = vol_targeted_basket_returns(R, W, target_ann_vol=0.20, lookback=30,
                                             periods_per_year=365, max_leverage=1.0, cost_bps=20)
    crypto_eq = (1 + crypto.fillna(0)).cumprod()
    if crypto_eq.index.tz is not None: crypto_eq.index = crypto_eq.index.tz_localize(None)

    print(f"  ETF data (SPY/QQQ/BND/BIL)…")
    px = yf.download(["SPY", "QQQ", "BND", "BIL"], start=start, auto_adjust=True,
                     progress=False, group_by="ticker", threads=True)
    spy_r = px["SPY"]["Close"].dropna().pct_change()
    qqq_r = px["QQQ"]["Close"].dropna().pct_change()
    bnd_r = px["BND"]["Close"].dropna().pct_change()
    bil_r = px["BIL"]["Close"].dropna().pct_change()
    common = spy_r.index.intersection(qqq_r.index).intersection(bnd_r.index).intersection(bil_r.index)
    spy_r, qqq_r, bnd_r, bil_r = spy_r.loc[common], qqq_r.loc[common], bnd_r.loc[common], bil_r.loc[common]
    bogle_r = 0.6 * spy_r + 0.4 * bnd_r

    td = common.tz_localize(None) if common.tz is not None else common
    crypto_td = crypto_eq.reindex(td, method="ffill").pct_change()
    pead_td   = pead.reindex(td).fillna(0)

    print(f"  QLD-regime sleeve backtest…")
    regime_td = _regime_sleeve_returns(start, end).reindex(td).fillna(0)

    bil_td = bil_r.reindex(td).fillna(0)
    mf_td = _mf_sleeve_returns(end, td, bil_td)
    comp = pd.DataFrame({"spy": spy_r, "qqq": qqq_r, "bogle": bogle_r,
                          "pead": pead_td, "crypto": crypto_td, "regime": regime_td,
                          "bil": bil_td, "mf": mf_td}).dropna(how="any")
    third = 1 / 3.0

    # Mixes (comparison benchmarks)
    mix_old    = third * (comp["qqq"] + comp["pead"] + comp["crypto"])  # pre-regime-sleeve era (historical)
    # "Same sleeves, fixed ⅓ each" — the bot's CURRENT active sleeves (PEAD retired 2026-06-26) at naive fixed weights
    mix_regime = third * (comp["regime"] + comp["crypto"] + comp["qqq"])

    aw4 = _adaptive_weights4(comp["qqq"], comp["pead"], comp["crypto"], comp["regime"]).loc[comp.index]
    # PEAD RETIRED 2026-06-26 (excluded from weights → its third becomes idle). Idle
    # routes 50/50 QQQ/MF in a bull, all managed futures in a bear (SPLIT — final choice
    # 2026-06-27 after fixing the look-ahead bug; split beats pure-QQQ lag-corrected).
    deficit = (1.0 - (aw4["qqq"] + aw4["regime"] + aw4["crypto"])).clip(lower=0)
    bil_weight = deficit.clip(upper=0.10)
    cash_weight = (deficit - bil_weight).clip(upper=0.02)  # 2% free cash (~0 return) — live cash_buffer_pct
    idle_weight = (deficit - bil_weight - cash_weight).clip(lower=0)
    # Warm the 200-DMA on QQQ history PREDATING the backtest start — else the SMA is
    # NaN for the first ~200 trading days of 2018 and bull is wrongly forced False
    # (zeroing the early-2018 idle-router QQQ leg, understating 2018). The live bot
    # fetches 400d of QQQ so its gate is always warm; mirror that here (and how
    # _regime_sleeve_returns already loads QQQ from 2009).
    _qc = load_ohlcv("QQQ", "2016-06-01", end)["close"]
    if _qc.index.tz is not None: _qc.index = _qc.index.tz_localize(None)
    bull = (_qc > _qc.rolling(200).mean()).shift(1).reindex(comp.index).fillna(False).astype(bool)  # prior close, no look-ahead
    idle_qqq = (0.5 * idle_weight).where(bull, 0.0)
    idle_mf = (0.5 * idle_weight).where(bull, 0.0) + idle_weight.where(~bull, 0.0)
    adaptive4 = (aw4["qqq"] * comp["qqq"] + aw4["regime"] * comp["regime"]
                 + aw4["crypto"] * comp["crypto"]
                 + bil_weight * comp["bil"] + idle_qqq * comp["qqq"] + idle_mf * comp["mf"])

    series = {
        "mix_regime":       _eq_curve(mix_regime),
        "adaptive4":        _eq_curve(adaptive4),
        "mix_old":          _eq_curve(mix_old),
        "regime_standalone": _eq_curve(comp["regime"]),
        "qqq":              _eq_curve(comp["qqq"]),
        "spy":              _eq_curve(comp["spy"]),
        "bogle":            _eq_curve(comp["bogle"]),
    }
    metrics = {
        "mix_regime":        _metrics_dict(mix_regime),
        "adaptive4":         _metrics_dict(adaptive4),
        "mix_old":           _metrics_dict(mix_old),
        "regime_standalone": _metrics_dict(comp["regime"]),
        "qqq":               _metrics_dict(comp["qqq"]),
        "spy":               _metrics_dict(comp["spy"]),
        "bogle":             _metrics_dict(comp["bogle"]),
    }

    # Adaptive weights stack (downsampled to monthly). Include BIL share so
    # the stacked chart sums to 100% — the BIL slice = "idle cash" from trips.
    aw_full = aw4.copy()
    aw_full["pead"] = 0.0  # PEAD retired — its third flows to the idle router (QQQ bull / MF bear)
    _def = (1.0 - aw_full[["qqq","regime","crypto"]].sum(axis=1)).clip(lower=0)
    _bil = _def.clip(upper=0.10)
    _cash = (_def - _bil).clip(upper=0.02)            # 2% free cash reserve (live cash_buffer_pct)
    aw_full["bil"] = _bil + _cash                     # reserve slice = BIL buffer + free cash
    _idle = (_def - _bil - _cash).clip(lower=0)
    aw_full["qqq"] = aw_full["qqq"] + (0.5 * _idle).where(bull, 0.0)            # idle -> 50% QQQ in a bull
    aw_full["mf"]  = (0.5 * _idle).where(bull, 0.0) + _idle.where(~bull, 0.0)   # 50% MF in bull, all MF in bear
    aw_monthly = aw_full.resample("ME").last()
    weights_series = {
        "dates":  [d.strftime("%Y-%m-%d") for d in aw_monthly.index],
        "qqq":    [round(float(v), 4) for v in aw_monthly["qqq"]],
        "regime": [round(float(v), 4) for v in aw_monthly["regime"]],
        "pead":   [round(float(v), 4) for v in aw_monthly["pead"]],
        "crypto": [round(float(v), 4) for v in aw_monthly["crypto"]],
        "mf":     [round(float(v), 4) for v in aw_monthly["mf"]],
        "bil":    [round(float(v), 4) for v in aw_monthly["bil"]],
    }

    # Rolling 60d annualized vol (downsampled to weekly for payload)
    def _vol_weekly(r: pd.Series) -> dict:
        v = (r.rolling(60).std() * np.sqrt(252) * 100).dropna().resample("W-FRI").last().dropna()
        return {"dates": [d.strftime("%Y-%m-%d") for d in v.index],
                "values": [round(float(x), 3) for x in v.values]}

    vol_series = {
        "adaptive4":  _vol_weekly(adaptive4),
        "qqq":        _vol_weekly(comp["qqq"]),
        "spy":        _vol_weekly(comp["spy"]),
        "bogle":      _vol_weekly(comp["bogle"]),
        # Keep the old keys around so older snapshots / external readers
        # don't break, but the dashboard chart uses adaptive4 + benchmarks above.
        "mix_regime": _vol_weekly(mix_regime),
        "mix_old":    _vol_weekly(mix_old),
    }

    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "start": start, "end": end, "n_bars": len(comp),
        "series": series,
        "metrics": metrics,
        "weights": weights_series,
        "vol":     vol_series,
    }


# ----------------------- crash_perf snapshot -----------------------
def build_crash_perf() -> dict:
    """Drawdown analysis: our portfolio vs QQQ during major drawdowns.

    Two parts:
      1. Real 2018-now: every QQQ DD>=15%, compute our portfolio's DD in the
         same window (live mix: Regime/QLD + Crypto + idle router; PEAD retired).
      2. Real regime-gate defense 2006-now: hold QLD (2x Nasdaq) when QQQ is above
         its 200-DMA, else BIL cash, with a 5bp flip cost — the ACTUAL crash-defense
         layer back to QLD's inception (NOT a synthetic QQQ/bond/gold proxy). The
         full multi-sleeve bot only has data from 2018 (part 1); pre-2018 crash
         rows therefore show this regime gate alone, flagged coverage="regime_only".
    """
    start = "2018-01-01"
    end = (pd.Timestamp.utcnow().date() + pd.Timedelta(days=1)).isoformat()

    # part 1: build the new live mix on real 2018+ data
    print(f"  building live mix returns…")
    pead = pead_backtest(load_pead_universe(), start, end, hold_days=60,
                         concordant=True, long_only=True, cost_bps=5.0)["returns"]
    if pead.index.tz is not None: pead.index = pead.index.tz_localize(None)
    data = {c: load_ohlcv(c, start, end) for c in load_crypto_universe()}
    R, W = trend_basket_panels(data, EnsembleTrend())
    crypto, _ = vol_targeted_basket_returns(R, W, target_ann_vol=0.20, lookback=30,
                                             periods_per_year=365, max_leverage=1.0, cost_bps=20)
    crypto_eq = (1 + crypto.fillna(0)).cumprod()
    if crypto_eq.index.tz is not None: crypto_eq.index = crypto_eq.index.tz_localize(None)
    px = yf.download(["QQQ", "BND", "BIL"], start=start, auto_adjust=True,
                     progress=False, group_by="ticker", threads=True)
    qqq = px["QQQ"]["Close"].dropna().pct_change()
    bil = px["BIL"]["Close"].dropna().pct_change()
    td = qqq.index
    if td.tz is not None: td = td.tz_localize(None)
    qqq_r = qqq.reindex(td).fillna(0)
    bil_td = bil.reindex(td).fillna(0)
    crypto_td = crypto_eq.reindex(td, method="ffill").pct_change().fillna(0)
    pead_td   = pead.reindex(td).fillna(0)
    regime_td = _regime_sleeve_returns(start, end).reindex(td).fillna(0)
    mf_td = _mf_sleeve_returns(end, td, bil_td)
    third = 1 / 3.0
    # "This bot" = the deployed adaptive mix. PEAD RETIRED 2026-06-26 (excluded → its
    # third becomes idle); idle routed to a 10% BIL buffer then SPLIT 50/50 QQQ/MF in a
    # bull, all managed futures in a bear (final choice 2026-06-27, look-ahead corrected).
    _aw = _adaptive_weights4(qqq_r, pead_td, crypto_td, regime_td).reindex(td).fillna(0)
    _def = (1.0 - _aw[["qqq","regime","crypto"]].sum(axis=1)).clip(lower=0)
    _bilw = _def.clip(upper=0.10)
    _cashw = (_def - _bilw).clip(upper=0.02)          # 2% free cash (~0 return) — live cash_buffer_pct
    _idle = (_def - _bilw - _cashw).clip(lower=0)
    _qc = load_ohlcv("QQQ", "2016-06-01", end)["close"]  # warm 200-DMA (predates 2018 start), matches live
    if _qc.index.tz is not None: _qc.index = _qc.index.tz_localize(None)
    _bull = (_qc > _qc.rolling(200).mean()).shift(1).reindex(td).fillna(False).astype(bool)  # prior close, no look-ahead
    _idle_qqq = (0.5 * _idle).where(_bull, 0.0)
    _idle_mf = (0.5 * _idle).where(_bull, 0.0) + _idle.where(~_bull, 0.0)
    live_mix = (_aw["qqq"]*qqq_r + _aw["regime"]*regime_td
                + _aw["crypto"]*crypto_td + _bilw*bil_td + _idle_qqq*qqq_r + _idle_mf*mf_td)
    static_mix = third * (qqq_r + regime_td + crypto_td)  # naive fixed-⅓ of current sleeves (PEAD retired)

    # Find crashes in QQQ
    qqq_eq = (1 + qqq_r).cumprod() * 100_000
    crashes = _find_crashes(qqq_eq, min_dd=0.15)
    def _dd(ret: pd.Series, start, end) -> float:
        seg = ret.loc[start:end].dropna()
        if seg.empty: return 0.0
        eq = (1 + seg).cumprod()
        return float((eq / eq.cummax() - 1.0).min())

    def _days_to_recover(ret: pd.Series, peak_date) -> int | None:
        """Days from `peak_date` until the equity recovers to its peak-date
        value AFTER first hitting a trough. Returns None if it never recovers."""
        seg = ret.loc[peak_date:].dropna()
        if len(seg) < 2: return None
        eq = (1 + seg).cumprod()
        if eq.min() >= 1.0:
            return 0   # never dropped below peak in this window
        trough_d = eq.idxmin()
        after_trough = eq.loc[trough_d:]
        recovered = after_trough[after_trough >= 1.0]
        if recovered.empty: return None
        recover_d = recovered.index[0]
        return int((recover_d - peak_date).days)

    crash_rows = []
    for c in crashes:
        crash_rows.append({
            "start":         c["start"].strftime("%Y-%m-%d"),
            "trough":        c["trough"].strftime("%Y-%m-%d"),
            "recovered":     (c["recovered"].strftime("%Y-%m-%d") if c["recovered"] is not None else None),
            "qqq_dd":        round(c["dd"], 4),
            "live_dd":       round(_dd(live_mix,   c["start"], c["trough"]), 4),
            "static_dd":     round(_dd(static_mix, c["start"], c["trough"]), 4),
            "qqq_days_to_recover":  _days_to_recover(qqq_r,    c["start"]),
            "live_days_to_recover": _days_to_recover(live_mix, c["start"]),
        })

    # Drawdown overlay timeseries (downsample weekly)
    def _dd_series(ret: pd.Series) -> dict:
        eq = (1 + ret).cumprod()
        dd = (eq / eq.cummax() - 1.0) * 100
        dd_w = dd.resample("W-FRI").last().dropna()
        return {"dates": [d.strftime("%Y-%m-%d") for d in dd_w.index],
                "values": [round(float(v), 3) for v in dd_w.values]}

    overlay = {
        "qqq":    _dd_series(qqq_r),
        "live":   _dd_series(live_mix),
        "static": _dd_series(static_mix),
    }

    # Part 2: REAL regime-gate defense backtest, QLD inception (2006) -> now. This is
    # our ACTUAL crash-protection logic — hold QLD (2x Nasdaq) when QQQ is above its
    # 200-DMA, sit in cash (BIL) when below — NOT a synthetic QQQ/bond/gold proxy. (The
    # full multi-sleeve bot only has data from 2018, shown in the "real" section above;
    # the regime gate is the layer that drives crash behaviour and it runs back to 2006.)
    print(f"  regime-gate defense backtest (2006-now)…")
    hist_px = yf.download(["QQQ", "QLD", "BIL"], start="2004-01-01",
                          auto_adjust=True, progress=False, group_by="ticker", threads=True)
    h_qqq_c = hist_px["QQQ"]["Close"].dropna()
    h_qld_r = hist_px["QLD"]["Close"].dropna().pct_change()
    # ACT on YESTERDAY's signal (shift 1) + a 5bp flip cost — SAME convention as the
    # live regime sleeve. Using same-day close would be LOOK-AHEAD (dodging the very
    # crash days you couldn't have seen yet), which hugely overstates the protection.
    h_pos = (h_qqq_c > h_qqq_c.rolling(200).mean()).astype(float).shift(1).reindex(h_qld_r.index).fillna(0.0)
    if "BIL" in hist_px.columns.levels[0]:
        h_cash = hist_px["BIL"]["Close"].dropna().pct_change().reindex(h_qld_r.index).fillna(0.0)
    else:
        h_cash = pd.Series(0.0, index=h_qld_r.index)
    h_flips = h_pos.diff().abs().fillna(0.0)
    # the regime defense line (keep the "proxy" output key for the frontend)
    h_proxy = (h_pos * h_qld_r + (1 - h_pos) * h_cash - h_flips * 5.0 / 10_000.0).dropna()
    h_qqq = hist_px["QQQ"]["Close"].dropna().pct_change().reindex(h_proxy.index).fillna(0.0)

    hist_metrics = {
        "qqq":   _metrics_dict(h_qqq),
        "proxy": _metrics_dict(h_proxy),
    }
    h_overlay = {
        "qqq":   _dd_series(h_qqq),
        "proxy": _dd_series(h_proxy),
    }

    # Historical major crashes table (QQQ-driven)
    h_qqq_eq = (1 + h_qqq).cumprod() * 100_000
    h_crashes = _find_crashes(h_qqq_eq, min_dd=0.20)
    # The live window where we have the REAL full multi-sleeve bot (crypto + MF idle
    # router + regime). Anything inside it should show the ACTUAL bot drawdown — not
    # the regime-gate-only line, which is just one (leveraged) sleeve and looks far
    # worse than the diversified whole. Pre-2018 crashes (e.g. 2008) have no crypto/MF
    # data, so there we fall back to the regime gate alone and flag it.
    live_start = pd.Timestamp(start)
    h_crash_rows = []
    for c in h_crashes:
        has_full_bot = c["start"] >= live_start and not live_mix.loc[c["start"]:c["trough"]].dropna().empty
        if has_full_bot:
            defense_dd = round(_dd(live_mix, c["start"], c["trough"]), 4)
            defense_recover = _days_to_recover(live_mix, c["start"])
        else:
            defense_dd = round(_dd(h_proxy, c["start"], c["trough"]), 4)
            defense_recover = _days_to_recover(h_proxy, c["start"])
        h_crash_rows.append({
            "start":         c["start"].strftime("%Y-%m-%d"),
            "trough":        c["trough"].strftime("%Y-%m-%d"),
            "recovered":     (c["recovered"].strftime("%Y-%m-%d") if c["recovered"] is not None else None),
            "qqq_dd":        round(c["dd"], 4),
            "qqq_days_to_recover":   _days_to_recover(h_qqq, c["start"]),
            # `proxy_dd` keeps its name for the frontend, but now holds the FULL BOT
            # drawdown when we have it (2018+) and the regime-gate-only line otherwise.
            "proxy_days_to_recover": defense_recover,
            "proxy_dd":      defense_dd,
            "coverage":      "full_bot" if has_full_bot else "regime_only",
        })

    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "live_window": {"start": start, "end": end},
        "real": {
            "crashes": crash_rows,
            "overlay": overlay,
        },
        "historical_2004": {
            "metrics":  hist_metrics,
            "crashes":  h_crash_rows,
            "overlay":  h_overlay,
        },
    }


def _find_crashes(close: pd.Series, min_dd: float = 0.15) -> list[dict]:
    peak = close.cummax()
    out = []
    in_crash = False
    peak_d = peak_v = trough_d = trough_v = None
    for d, v, p in zip(close.index, close.values, peak.values):
        if not in_crash and v < p * (1 - min_dd):
            in_crash = True
            peak_d, peak_v = p_idx, p_val = close.index[close.index.get_loc(d) - 1], p
            # find the actual peak day
            mask = close.index <= d
            peak_d = close[mask].idxmax()
            peak_v = float(close[mask].max())
            trough_d, trough_v = d, float(v)
        elif in_crash:
            if v < trough_v:
                trough_d, trough_v = d, float(v)
            if v >= peak_v:
                out.append({"start": peak_d, "trough": trough_d, "recovered": d,
                            "dd": trough_v / peak_v - 1.0})
                in_crash = False
                peak_d = peak_v = trough_d = trough_v = None
    if in_crash:
        out.append({"start": peak_d, "trough": trough_d, "recovered": None,
                    "dd": trough_v / peak_v - 1.0})
    return out


# ----------------------- PEAD snapshot -----------------------
def build_pead():
    """Pre-compute the PEAD tab data (current positions + recent earnings +
    upcoming earnings). Same logic as the server's pead_snapshot but written
    to a file so the live endpoint can read it instantly."""
    import yfinance as yf
    from trading_bot.backtest.pead import earnings_events
    from trading_bot.config import load_pead_universe
    from datetime import timedelta

    universe = load_pead_universe()
    today = datetime.now(timezone.utc).date()
    cutoff_back = today - timedelta(days=21)
    cutoff_fwd = today + timedelta(days=21)

    # Open positions (state file only; the live server fills in Alpaca P&L on read)
    state_path = ROOT / "data" / "pead_state.json"
    positions = []
    if state_path.exists():
        try:
            st = json.loads(state_path.read_text())
            for p in st.get("positions", []):
                entry_d = p.get("entry_date", "")
                try:
                    days_held = (today - datetime.strptime(entry_d, "%Y-%m-%d").date()).days
                except Exception:
                    days_held = 0
                positions.append({
                    "symbol":      p.get("symbol"),
                    "entry_date":  entry_d,
                    "days_held":   days_held,
                    "days_to_exit": max(60 - days_held, 0),
                    "qty":         float(p.get("qty", 0)),
                    "entry_price": float(p.get("entry_price", 0)),
                })
        except Exception:
            pass

    recent, upcoming = [], []
    for sym in universe:
        try:
            ev = earnings_events(sym, use_cache=False)
        except Exception:
            ev = pd.DataFrame()
        if ev is not None and not ev.empty:
            past = ev[(ev["date"] >= pd.Timestamp(cutoff_back)) & (ev["date"] <= pd.Timestamp(today))]
            for _, e in past.iterrows():
                event_date = e["date"].date()
                try:
                    px = yf.Ticker(sym).history(period="3mo", auto_adjust=True)
                    if px.index.tz is not None:
                        px.index = px.index.tz_localize(None)
                except Exception:
                    px = None
                reaction = None
                if px is not None and not px.empty:
                    pos = px.index.searchsorted(pd.Timestamp(event_date))
                    react_pos = pos + 1 if e.get("amc", False) else pos
                    if 0 < react_pos < len(px):
                        try:
                            reaction = float(px["Close"].iloc[react_pos] / px["Close"].iloc[react_pos - 1] - 1)
                        except Exception:
                            reaction = None
                surprise = float(e.get("surprise", 0))
                concordant = (surprise > 0) and (reaction is not None and reaction > 0)
                recent.append({
                    "symbol":     sym,
                    "date":       event_date.isoformat(),
                    "amc":        bool(e.get("amc", False)),
                    "surprise":   round(surprise, 2),
                    "reaction":   round(reaction * 100, 2) if reaction is not None else None,
                    "concordant": bool(concordant),
                    "miss":       surprise < 0,
                })
        try:
            ed = yf.Ticker(sym).get_earnings_dates(limit=8)
            if ed is not None and not ed.empty:
                ed = ed[~ed.index.isna()]
                for ts, row in ed.iterrows():
                    if pd.isna(ts):
                        continue
                    d = pd.Timestamp(ts).tz_localize(None).normalize().date() \
                        if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts).normalize().date()
                    if d > today and d <= cutoff_fwd:
                        upcoming.append({
                            "symbol":   sym,
                            "date":     d.isoformat(),
                            "estimate": float(row.get("EPS Estimate", 0)) if not pd.isna(row.get("EPS Estimate", float("nan"))) else None,
                        })
                        break
        except Exception:
            pass
    recent.sort(key=lambda r: r["date"], reverse=True)
    upcoming.sort(key=lambda r: r["date"])
    return {
        "ts":            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of":         today.isoformat(),
        "universe_size": len(universe),
        "positions":     positions,
        "recent":        recent,
        "upcoming":      upcoming,
    }


# ----------------------- main -----------------------
def main():
    from trading_bot.runtime_state import atomic_json
    from portfolio_lab import run
    OUT.mkdir(parents=True, exist_ok=True)
    # Preserve archived legacy charts as research only. The UI now reads the
    # frozen portfolio lab and does not present these as a deployed champion.
    for name in ("team_vs_hold", "crash_perf"):
        path = OUT / (name + ".json")
        if path.exists():
            value = json.loads(path.read_text())
            value.update(production_parity=False, promotion_eligible=False,
                         label="Archived legacy research; not the deployed portfolio")
            atomic_json(path, value)
    run()
    atomic_json(OUT / "pead.json", build_pead())
    print("Completed frozen portfolio research and earnings-observer snapshots.")


if __name__ == "__main__":
    main()
