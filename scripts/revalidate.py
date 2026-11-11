"""Disciplined weekly re-validation (READ-ONLY — never trades or rewires the bots).

For each live sleeve it asks two questions on fresh data:
  (a) Is the edge still holding? — compares full-history vs. the recent window and
      flags HEALTHY / WATCH / DECAYING.
  (b) Does any challenger beat the incumbent out-of-sample AND clear the Deflated
      Sharpe gate? — if so it RECOMMENDS the swap (for human review); otherwise
      "keep incumbent". Nothing is applied automatically.

Writes data/revalidation.jsonl and prints a report (lands in logs/scheduled.log).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from trading_bot.backtest import deflated_sharpe
from trading_bot.backtest.basket import trend_basket_panels, vol_targeted_basket_returns
from trading_bot.backtest.metrics import compute_metrics
from trading_bot.backtest.pead import pead_backtest
from trading_bot.config import load_crypto_universe, load_pead_universe
from trading_bot.data import load_ohlcv
from trading_bot.notify import send_telegram
from trading_bot.strategies import EnsembleTrend
from trading_bot.strategies.breakout import DonchianBreakout, KeltnerBreakout
from trading_bot.strategies.momentum import MACD, SMACrossover, TimeSeriesMomentum

REPORT = Path(__file__).resolve().parents[1] / "data" / "revalidation.jsonl"
DSR_GATE = 0.90          # confidence a challenger must clear to be recommended
MARGIN = 0.20            # recent-Sharpe edge a challenger needs over the incumbent
RECENT_DAYS = 365
TRIPWIRE_WEEKS = 4       # consecutive DECAYING weekly checks before recommending a halt


def _verdict(recent_sharpe: float, full_sharpe: float) -> str:
    if recent_sharpe < 0:
        return "DECAYING"
    if recent_sharpe < 0.3 or (full_sharpe > 0 and recent_sharpe < 0.5 * full_sharpe):
        return "WATCH"
    return "HEALTHY"


def _decaying_streak(prior: list, sleeve: str, this_verdict: str) -> int:
    """Count trailing consecutive DECAYING verdicts, ONE PER ISO WEEK.

    Raw record-counting had three bugs: (a) a manual rerun in the same week
    counted as two "consecutive checks"; (b) a missed cron week was invisible
    (a 26-day gap counted as consecutive); (c) a transient error record
    (verdict=None) RESET a real streak. Now: dedupe to the last verdict per
    ISO week, SKIP error records (no verdict) rather than resetting, and only
    count strictly consecutive calendar weeks."""
    by_week: dict = {}
    for r in prior:
        v = r.get(sleeve, {}).get("verdict")
        if v is None:
            continue  # error record — skip, don't reset
        ts = r.get("ts", "")
        try:
            wk = datetime.fromisoformat(ts).isocalendar()[:2]  # (year, week)
        except Exception:
            continue
        by_week[wk] = v  # last verdict of that week wins
    now_wk = datetime.now(timezone.utc).isocalendar()[:2]
    by_week[now_wk] = this_verdict
    streak = 0
    y, w = now_wk
    while by_week.get((y, w)) == "DECAYING":
        streak += 1
        # previous ISO week
        prev = datetime.fromisocalendar(y, w, 1) - timedelta(days=7)
        y, w = prev.isocalendar()[:2]
    return streak


def _metrics(net: pd.Series, ppy: int):
    cut = pd.Timestamp(datetime.now(timezone.utc).date() - timedelta(days=RECENT_DAYS))
    if net.index.tz is not None:
        cut = cut.tz_localize(net.index.tz)
    rec = net[net.index >= cut]
    full_m = compute_metrics(net, (1 + net).cumprod(), (net != 0).astype(float), ppy)
    rec_m = compute_metrics(rec, (1 + rec).cumprod(), (rec != 0).astype(float), ppy) if len(rec) > 5 else full_m
    return full_m, rec_m, rec


def _trial_std(ann_sharpes: list[float], ppy: int) -> float | None:
    """Per-period std-dev of the candidates' (annualized) Sharpes — the ACTUAL
    multiple-testing dispersion for the Deflated Sharpe gate. None if degenerate
    (<2 finite values or zero spread), so deflated_sharpe falls back to its
    conservative 1/√n proxy."""
    s = pd.Series([x for x in ann_sharpes if x == x])  # drop NaN
    if len(s) < 2:
        return None
    std_pp = float(s.std(ddof=1)) / (ppy ** 0.5)
    return std_pp if std_pp > 0 else None


def crypto_section(n_candidates: int) -> dict:
    coins = load_crypto_universe()
    end = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    data = {}
    for c in coins:
        try:
            data[c] = load_ohlcv(c, "2018-01-01", end)
        except Exception:
            pass
    candidates = {
        "ensemble [DEPLOYED]": EnsembleTrend(),
        "keltner": KeltnerBreakout(ema=20, atr_period=10, mult=2.0),
        "donchian": DonchianBreakout(entry=50, exit=20),
        "macd": MACD(),
        "ts_momentum": TimeSeriesMomentum(lookback=90),
        "sma_50_200": SMACrossover(fast=50, slow=200),
    }
    rows = {}
    recents = {}  # name -> recent-window return series
    for name, strat in candidates.items():
        R, W = trend_basket_panels(data, strat)
        net, _ = vol_targeted_basket_returns(R, W, target_ann_vol=0.20, lookback=30,
                                             periods_per_year=365, max_leverage=1.0, cost_bps=20)
        full_m, rec_m, rec = _metrics(net, 365)
        recents[name] = rec
        rows[name] = {"full_sharpe": round(full_m.sharpe, 2),
                      "recent_sharpe": round(rec_m.sharpe, 2)}
    trial_std = _trial_std([m["recent_sharpe"] for m in rows.values()], 365)
    for name, rec in recents.items():
        rows[name]["recent_dsr"] = round(deflated_sharpe(
            rec, n_trials=len(candidates), trial_sharpe_std=trial_std, periods_per_year=365), 2)
    return rows


def pead_section() -> dict:
    uni = load_pead_universe()
    end = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    rows = {}
    recents = {}
    # PEAD was RETIRED 2026-06-26 (redundant equity beta) — we still grade it in
    # case it earns its way back, but it does NOT trade. Label accordingly.
    holds = [("hold 60d [RETIRED — monitoring only]", 60), ("hold 40d", 40), ("hold 80d", 80)]
    for name, hold in holds:
        r = pead_backtest(uni, "2018-01-01", end, hold_days=hold, concordant=True, long_only=True, cost_bps=5.0)
        full_m, rec_m, rec = _metrics(r["returns"], 252)
        recents[name] = rec
        rows[name] = {"full_sharpe": round(full_m.sharpe, 2),
                      "recent_sharpe": round(rec_m.sharpe, 2)}
    trial_std = _trial_std([m["recent_sharpe"] for m in rows.values()], 252)
    for name, rec in recents.items():
        rows[name]["recent_dsr"] = round(deflated_sharpe(
            rec, n_trials=len(holds), trial_sharpe_std=trial_std, periods_per_year=252), 2)
    return rows


def _report(title: str, rows: dict) -> dict:
    # Retired sleeves are still monitored with their last production rule as
    # the incumbent. Requiring the literal word DEPLOYED made PEAD fail as soon
    # as its label was honestly changed to RETIRED.
    deployed = next(k for k in rows if "DEPLOYED" in k or "RETIRED" in k)
    inc = rows[deployed]
    verdict = _verdict(inc["recent_sharpe"], inc["full_sharpe"])
    # gated promotion recommendation
    rec_msg = "keep incumbent"
    for name, m in rows.items():
        if name == deployed:
            continue
        if m["recent_dsr"] >= DSR_GATE and m["recent_sharpe"] > inc["recent_sharpe"] + MARGIN:
            rec_msg = f"RECOMMEND review: '{name}' clears DSR {m['recent_dsr']:.0%} & beats incumbent recent Sharpe"
            break

    print(f"\n=== {title} ===")
    print(f"{'candidate':<22}{'full Sharpe':>12}{'recent Sharpe':>14}{'recent DSR':>12}")
    for name, m in rows.items():
        print(f"{name:<22}{m['full_sharpe']:>12.2f}{m['recent_sharpe']:>14.2f}{m['recent_dsr']:>11.0%}")
    print(f"  incumbent verdict: {verdict}")
    print(f"  promotion: {rec_msg}")
    return {"verdict": verdict, "incumbent": inc, "recommendation": rec_msg, "candidates": rows}


def main() -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"\n########## RE-VALIDATION {ts} (read-only) ##########")
    record = {"ts": ts}
    try:
        record["crypto"] = _report("CRYPTO trend sleeve", crypto_section(6))
    except Exception as exc:  # noqa: BLE001
        print(f"crypto revalidation failed: {exc}"); record["crypto"] = {"error": str(exc)}
    try:
        record["pead"] = _report("PEAD equity sleeve", pead_section())
    except Exception as exc:  # noqa: BLE001
        print(f"pead revalidation failed: {exc}"); record["pead"] = {"error": str(exc)}

    # --- consecutive-DECAYING tripwire (escalate, don't auto-halt) ---
    prior = []
    if REPORT.exists():
        prior = [json.loads(l) for l in REPORT.read_text().splitlines() if l.strip()]
    for sleeve in ("crypto", "pead"):
        s = record.get(sleeve, {})
        if "verdict" not in s:
            continue
        streak = _decaying_streak(prior, sleeve, s["verdict"])
        s["decaying_streak"] = streak
        if streak >= TRIPWIRE_WEEKS:
            s["tripwire"] = f"TRIPPED — {streak} consecutive DECAYING checks; halt/retire recommended (review)"
            print(f"\n🚨 {sleeve.upper()} TRIPWIRE TRIPPED: {streak} consecutive DECAYING checks "
                  f"(>= {TRIPWIRE_WEEKS}) → recommend halting this sleeve. (Not auto-halted.)")
        else:
            s["tripwire"] = "ok"
            if s["verdict"] == "DECAYING":
                print(f"  {sleeve}: DECAYING streak {streak}/{TRIPWIRE_WEEKS} — watching (tripwire not reached).")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"\nWrote verdict to {REPORT.name}. (No live changes made — recommendations are for review.)")

    # Telegram weekly verdict
    try:
        send_telegram(_format_revalidation_msg(record))
    except Exception:
        pass


_BADGES = {"HEALTHY": "🟢", "WATCH": "🟡", "DECAYING": "🔴"}


def _format_revalidation_msg(record: dict) -> str:
    lines = [f"🩺 *Weekly re-validation* · {record['ts'][:10]}", ""]
    tripped = []
    for sleeve, label in (("crypto", "Crypto trend"), ("pead", "PEAD (retired — monitoring only)")):
        s = record.get(sleeve, {})
        if "verdict" not in s:
            lines.append(f"⚠ {label}: {s.get('error', 'no data')}")
            continue
        v = s["verdict"]
        inc = s.get("incumbent", {})
        streak = s.get("decaying_streak", 0)
        tw = s.get("tripwire", "ok")
        line = (f"{_BADGES.get(v,'⚪')} *{label}*: {v}  "
                f"(recent Sharpe {inc.get('recent_sharpe','—')} / full {inc.get('full_sharpe','—')})")
        if v == "DECAYING":
            line += f"\n    streak {streak}/{TRIPWIRE_WEEKS}"
        if tw and tw != "ok":
            line += "\n    🚨 HALT RECOMMENDED — review dashboard"
            tripped.append(label)
        lines.append(line)
        rec = s.get("recommendation", "")
        if rec and rec != "keep incumbent":
            lines.append(f"    → {rec}")
    if tripped:
        lines.append("")
        lines.append(f"⚠ Action needed on: {', '.join(tripped)}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
