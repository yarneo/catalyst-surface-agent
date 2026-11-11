"""Weekly PORTFOLIO-LEVEL research gauntlet — the autonomous "find a better
structure" loop that runs unattended and sends status-only evidence reports.

WHY THIS EXISTS (vs. revalidate.py):
  revalidate.py inspects each sleeve INTERNALLY (is crypto's/PEAD's edge intact;
  is there a better single strategy for that sleeve). It is structurally BLIND to
  portfolio-level wins — sleeve weights, idle-capital ROUTING, regime gates, the
  tripwire that throttles a decaying sleeve — which is where every real win has
  actually come from (the managed-futures sleeve, the 2026-06-22 regime-aware idle
  router, the 8→6 crypto slow-tripwire). This script searches THAT space.

WHAT IT DOES (research never places orders):
  1. Champion = the CURRENT live config (idle router: regime-gated QQQ/MF, MA=200,
     BIL buffer 10%, slow tripwire streak=6).
  2. Candidates = a curated set of principled STRUCTURAL variants (MA window, BIL
     buffer, idle destination, slow-tripwire streak) + anything queued in
     data/research/queue.json.
  3. Each is simulated at the PORTFOLIO level over the exact live sleeve returns
     across full / 2022-bear / recent-252d windows.
  4. TWO-TIER GAUNTLET:
       · SURVIVOR: beats champion full Sharpe by
         >= MARGIN_SHARPE AND no worse on recent / 2022-bear / maxDD.
       · REVIEW (kept as research): doesn't clear the strict bar, but is
         a near-Pareto REGIME-ADAPTATION edge — materially better recent (>= REVIEW_
         RECENT_MARGIN) while not materially worse on full / bear / maxDD. This tier
         exists because a pure full-Sharpe gate MISSES fixes that help the CURRENT
         regime without moving the multi-year average (e.g. the 8→6 tripwire: full
         +0.01 but 2025 Sharpe 0.81→0.96). It remains visible but cannot deploy.
  5. Writes data/research/pending.json + appends data/research/weekly.jsonl, and
     Telegrams ONE digest that leads with the decision.
  6. Strict survivors enter an automatic forward shadow. Only return, execution-
     cost, calibration and risk gates can promote one to the paper runtime policy;
     a post-promotion monitor can roll it back. Reviews never auto-promote.
  uv run python scripts/weekly_research.py            # run the gauntlet
  uv run python scripts/weekly_research.py --no-telegram
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
try:
    from dotenv import load_dotenv; load_dotenv()
except Exception:
    pass

import numpy as np
import pandas as pd
import yfinance as yf

from trading_bot.backtest.basket import trend_basket_panels, vol_targeted_basket_returns
from trading_bot.backtest.pead import pead_backtest
from trading_bot.config import load_crypto_universe, load_pead_universe
from trading_bot.data import load_ohlcv
from trading_bot.notify import send_telegram
from trading_bot.portfolio_policy import (
    DEFAULT_CONFIG, load_policy, validate_config, write_policy,
)
from trading_bot.research_autopilot import (
    advance_rollback, advance_shadow, candidate_id, latest_week,
    load_state, recover_deployment, save_state,
)
from trading_bot.strategies import EnsembleTrend
from scripts.precompute_dashboard import (_regime_sleeve_returns, _mf_sleeve_returns,
                                          _adaptive_weights4)
import warnings; warnings.filterwarnings("ignore")

OUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "data" / "research"
PENDING = OUT_DIR / "pending.json"
JOURNAL = OUT_DIR / "weekly.jsonl"
QUEUE = OUT_DIR / "queue.json"          # human/LLM-added candidate ideas

# Gauntlet thresholds (deliberately strict — most weeks NOTHING should clear).
MARGIN_SHARPE = 0.05        # full-period Sharpe edge a SURVIVOR needs over champion
DD_TOLERANCE = 0.01         # may not worsen max drawdown by more than 1 percentage pt
RECENT_DAYS = 252           # out-of-sample window (most recent ~1y)
# REVIEW tier (regime-adaptation edge worth a human look, short of a survivor):
REVIEW_RECENT_MARGIN = 0.05   # materially-better recent Sharpe to earn a review
FULL_TOL = 0.03               # ...while full Sharpe is no more than this worse
BEAR_TOL = 0.05               # ...and 2022-bear Sharpe no more than this worse
DEFAULT_SLOW_STREAK = 6       # current live slow-tripwire streak (champion)

_AW_CACHE: dict[tuple, pd.DataFrame] = {}


def _metrics(ret: pd.Series) -> dict:
    eq = (1 + ret.fillna(0)).cumprod()
    n = len(ret); yrs = n / 252 if n else 0
    cagr = (eq.iloc[-1] ** (1 / yrs) - 1) if yrs > 0 else 0.0
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0.0
    dd = ((eq / eq.cummax()) - 1).min()
    tot = eq.iloc[-1] - 1
    return {"tot": float(tot), "cagr": float(cagr), "sharpe": float(sharpe), "dd": float(dd)}


def build_panel():
    """Build the exact live sleeve return panel + bull signal (daily data, VM-safe)."""
    start = "2018-01-01"
    end = (pd.Timestamp.utcnow().date() + pd.Timedelta(days=1)).isoformat()
    print("  building sleeves (PEAD + crypto + regime + MF)…", file=sys.stderr)
    pead = pead_backtest(load_pead_universe(), start, end, hold_days=60, concordant=True,
                         long_only=True, cost_bps=5.0)["returns"]
    if pead.index.tz is not None:
        pead.index = pead.index.tz_localize(None)
    data = {c: load_ohlcv(c, start, end) for c in load_crypto_universe()}
    R, W = trend_basket_panels(data, EnsembleTrend())
    crypto, _ = vol_targeted_basket_returns(R, W, target_ann_vol=0.20, lookback=30,
                                            periods_per_year=365, max_leverage=1.0, cost_bps=20)
    crypto_eq = (1 + crypto.fillna(0)).cumprod()
    if crypto_eq.index.tz is not None:
        crypto_eq.index = crypto_eq.index.tz_localize(None)
    px = yf.download(["SPY", "QQQ", "BIL"], start=start, auto_adjust=True,
                     progress=False, group_by="ticker", threads=True)
    spy_r = px["SPY"]["Close"].dropna().pct_change()
    qqq_c = px["QQQ"]["Close"].dropna(); qqq_r = qqq_c.pct_change()
    bil_r = px["BIL"]["Close"].dropna().pct_change()
    common = spy_r.index.intersection(qqq_r.index).intersection(bil_r.index)
    td = common.tz_localize(None) if common.tz is not None else common
    qqq_c.index = qqq_c.index.tz_localize(None) if qqq_c.index.tz is not None else qqq_c.index
    crypto_td = crypto_eq.reindex(td, method="ffill").pct_change()
    pead_td = pead.reindex(td).fillna(0)
    regime_td = _regime_sleeve_returns(start, end).reindex(td).fillna(0)
    bil_td = bil_r.reindex(td).fillna(0)
    mf_td = _mf_sleeve_returns(end, td, bil_td)
    comp = pd.DataFrame({"spy": spy_r.loc[common].values, "qqq": qqq_r.loc[common].values,
                         "pead": pead_td.values, "crypto": crypto_td.values,
                         "regime": regime_td.values, "bil": bil_td.values, "mf": mf_td.values},
                        index=td).dropna(how="any")
    # WARM signal history for the bull MA: the panel starts 2018, so a rolling
    # MA computed on panel-start data is NaN for the first ~ma_window days →
    # forced risk-OFF, for a DIFFERENT number of days per MA window (unfair
    # MA150-vs-MA250 comparisons + an unreal 2018 for every variant). Pull QQQ
    # from 2015 so every window is warm on day one (same fix as
    # _regime_sleeve_returns loading from 2009).
    qqq_sig = yf.download("QQQ", start="2015-01-01", auto_adjust=True, progress=False)["Close"].dropna()
    if isinstance(qqq_sig, pd.DataFrame):
        qqq_sig = qqq_sig.iloc[:, 0]
    if qqq_sig.index.tz is not None:
        qqq_sig.index = qqq_sig.index.tz_localize(None)
    return comp, qqq_sig


def _weights(comp, slow_streak: int, slow_regime_share: float = 0.0) -> pd.DataFrame:
    """Adaptive sleeve weights for a given slow-tripwire streak (cached)."""
    key = (slow_streak, slow_regime_share)
    if key not in _AW_CACHE:
        _AW_CACHE[key] = _adaptive_weights4(
            comp["qqq"], comp["pead"], comp["crypto"], comp["regime"],
            slow_streak=slow_streak, slow_regime_share=slow_regime_share).loc[comp.index]
    return _AW_CACHE[key]


def simulate(cfg: dict, comp, qqq_c) -> pd.Series:
    """Portfolio daily returns under a structural config.
    cfg keys: idle_rule ('regime_qqq'|'mf_always'|'qld_bull'|'split'),
              ma_window (int), bil_buffer (float), slow_streak (int),
              slow_regime_share (float, 0 = live policy since 2026-07-18)."""
    aw = _weights(comp, cfg.get("slow_streak", DEFAULT_SLOW_STREAK),
                  cfg.get("slow_regime_share", 0.0))
    # PEAD retired 2026-06-26 — excluded from the weights (its third becomes idle).
    deficit = (1.0 - (aw["qqq"] + aw["regime"] + aw["crypto"])).clip(lower=0)
    bilw = deficit.clip(upper=cfg["bil_buffer"])
    idle = (deficit - bilw).clip(lower=0)
    base = (aw["qqq"] * comp["qqq"] + aw["regime"] * comp["regime"]
            + aw["crypto"] * comp["crypto"] + bilw * comp["bil"])
    bull = (qqq_c > qqq_c.rolling(cfg["ma_window"]).mean()).shift(1) \
        .reindex(comp.index).fillna(False).astype(bool)  # prior close, no look-ahead
    # The separate regime sleeve always uses its own production 200-DMA signal.
    # A candidate MA changes only the idle router; using that candidate signal
    # here would falsely assume that QLD entered/exited on the same experimental
    # window and would misprice the anti-concentration guard.
    production_regime_bull = (qqq_c > qqq_c.rolling(200).mean()).shift(1) \
        .reindex(comp.index).fillna(False).astype(bool)
    rule = cfg["idle_rule"]
    # Build explicit per-destination idle weights so we can charge ROUTING
    # TURNOVER. Switching was previously free, giving flip-heavy rules/windows
    # (regime_qqq/split/MA150) a structural advantage over mf_always in the
    # champion comparison — on the same order as the 0.05 Sharpe survivor margin.
    zero = pd.Series(0.0, index=comp.index)
    # Match the production concentration guard: if the regime sleeve is already
    # long QLD on the same bull signal, do not stack another Nasdaq allocation.
    regime_nasdaq = production_regime_bull & (aw["regime"] > 0)
    bull_without_regime = bull & ~regime_nasdaq
    if rule == "mf_always":
        legs = {"mf": idle}
    elif rule == "regime_qqq":
        legs = {"qqq": idle.where(bull_without_regime, 0.0),
                "mf": idle.where(~bull_without_regime, 0.0)}
    elif rule == "qld_bull":
        legs = {"regime": idle.where(bull, 0.0), "mf": idle.where(~bull, 0.0)}
    elif rule == "split":
        legs = {"qqq": (0.5 * idle).where(bull_without_regime, 0.0),
                "mf": ((0.5 * idle).where(bull_without_regime, 0.0)
                       + idle.where(~bull_without_regime, 0.0))}
    else:
        raise ValueError(f"unknown idle_rule {rule}")
    idle_ret = zero.copy()
    for k, w in legs.items():
        idle_ret = idle_ret + w * comp[k]
    switch_cost = sum(w.diff().abs().fillna(0.0) for w in legs.values()) * 5.0 / 10_000.0
    idle_ret = idle_ret - switch_cost
    return pd.Series(base.values + idle_ret.values, index=comp.index)


# Champion = the CURRENT live config: PEAD retired, idle SPLIT 50% QQQ / 50% MF in bull
# (final choice 2026-06-27, look-ahead corrected), MA=200, 10% BIL, slow-tripwire streak=6.
CHAMPION = {"name": "champion (live: PEAD-retired, idle split QQQ/MF, MA200, streak6)",
            **DEFAULT_CONFIG}

_CFG_KEYS = ("idle_rule", "ma_window", "bil_buffer", "slow_streak", "slow_regime_share")


def default_candidates(base_config: dict | None = None) -> list[dict]:
    """Principled, small, each gauntletted — NOT a grid sweep. Each varies ONE axis."""
    # slow_regime_share must be present: _CFG_KEYS lists it and the record
    # builder indexes cfg[k] directly, so any candidate without it raised
    # KeyError and killed the whole weekly run (seen 2026-08-09). Everywhere
    # else reads it with .get(..., 0.0) — 0 is the live policy since 2026-07-18.
    base = {**DEFAULT_CONFIG, **(base_config or {})}
    cands = [
        {**base, "name": "MA150 regime gate", "ma_window": 150},
        {**base, "name": "MA250 regime gate", "ma_window": 250},
        {**base, "name": "BIL buffer 5%",      "bil_buffer": 0.05},
        {**base, "name": "BIL buffer 15%",     "bil_buffer": 0.15},
        {**base, "name": "idle->pure QQQ", "idle_rule": "regime_qqq"},
        {**base, "name": "idle->MF always", "idle_rule": "mf_always"},
        {**base, "name": "idle->QLD in bull",  "idle_rule": "qld_bull"},
        {**base, "name": "regime-blind (old idle->MF)", "idle_rule": "mf_always"},
        {**base, "name": "tripwire streak=8 (old)", "slow_streak": 8},
        {**base, "name": "tripwire streak=5",       "slow_streak": 5},
        {**base, "name": "tripwire streak=4",       "slow_streak": 4},
        # Old freed-weight policy (pre-2026-07-18): 30% of a slow-freed third to
        # the regime sleeve. Kept gauntleted so the change stays evidence-backed.
        {**base, "name": "trip 30%->regime (old)",  "slow_regime_share": 0.30},
    ]
    if QUEUE.exists():
        try:
            for q in json.loads(QUEUE.read_text()):
                if "name" in q and any(k in q for k in _CFG_KEYS):
                    cands.append({**base, **{k: v for k, v in q.items() if k in _CFG_KEYS or k == "name"}})
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ queue.json unreadable: {exc}", file=sys.stderr)
    return cands


def classify(champ_m: dict, cand_m: dict) -> tuple[str, list[str]]:
    """Return ('survivor'|'review'|'reject', notes)."""
    cf, cr, cb = cand_m["full"], cand_m["recent"], cand_m["bear"]
    hf, hr, hb = champ_m["full"], champ_m["recent"], champ_m["bear"]
    dd_ok = cf["dd"] >= hf["dd"] - DD_TOLERANCE

    # SURVIVOR: clears the strict full-Sharpe bar without giving up recent/bear/dd.
    if (cf["sharpe"] >= hf["sharpe"] + MARGIN_SHARPE and cr["sharpe"] >= hr["sharpe"] - 1e-9
            and cb["sharpe"] >= hb["sharpe"] - 1e-9 and dd_ok):
        return "survivor", [f"full Sharpe {cf['sharpe']:.2f} ≥ champ+{MARGIN_SHARPE}, no worse elsewhere"]

    # REVIEW: regime-adaptation edge — materially better recent, not materially worse else.
    if (cr["sharpe"] >= hr["sharpe"] + REVIEW_RECENT_MARGIN
            and cf["sharpe"] >= hf["sharpe"] - FULL_TOL
            and cb["sharpe"] >= hb["sharpe"] - BEAR_TOL and dd_ok):
        return "review", [f"recent Sharpe {cr['sharpe']:.2f} (champ {hr['sharpe']:.2f}, +{cr['sharpe']-hr['sharpe']:.2f}); "
                          f"full {cf['sharpe']:.2f} vs {hf['sharpe']:.2f}, bear {cb['sharpe']:.2f} vs {hb['sharpe']:.2f}"]

    # REJECT — say why (first binding reason).
    reasons = []
    if cf["sharpe"] < hf["sharpe"] + MARGIN_SHARPE:
        reasons.append(f"full Sharpe {cf['sharpe']:.2f} < champ+{MARGIN_SHARPE}")
    if cr["sharpe"] < hr["sharpe"] - 1e-9:
        reasons.append(f"recent {cr['sharpe']:.2f} < champ {hr['sharpe']:.2f}")
    if cb["sharpe"] < hb["sharpe"] - BEAR_TOL:
        reasons.append(f"bear {cb['sharpe']:.2f} < champ−tol")
    if not dd_ok:
        reasons.append(f"maxDD {cf['dd']*100:.0f}% worse")
    return "reject", reasons or ["no edge"]


def windows(comp):
    return {"full": comp.index, "bear": comp.index[comp.index.year == 2022],
            "recent": comp.index[-RECENT_DAYS:]}


def metric_set(ret: pd.Series, wins: dict) -> dict:
    return {w: _metrics(ret.loc[wi]) for w, wi in wins.items()}


ROLL_WINDOW = 252   # 1-year rolling windows
ROLL_STEP = 21      # ~monthly step thins the overlap (adjacent daily windows share 99% of data)


def rolling_robustness(ret: pd.Series, bench: pd.Series) -> dict | None:
    """Performance DISTRIBUTION across rolling 1y windows — CONSISTENCY, not just the
    full-period average. A strong full Sharpe paired with a weak median, an ugly worst
    window, or a low %-beat-QQQ is a fragility / overfit smell (the full number was
    carried by one lucky stretch). Windows overlap, so n_windows is NOT independent
    samples — read it as 'in how much of history did this hold', not a t-stat."""
    n = len(ret)
    if n < ROLL_WINDOW + ROLL_STEP:
        return None
    sh, beat = [], []
    for i in range(0, n - ROLL_WINDOW + 1, ROLL_STEP):
        w = ret.iloc[i:i + ROLL_WINDOW]; bw = bench.iloc[i:i + ROLL_WINDOW]
        sd = w.std()
        sh.append(float(w.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0)
        beat.append(float((1 + w).prod() > (1 + bw).prod()))
    sh = np.array(sh)
    return {"n_windows": len(sh),
            "median_sharpe": round(float(np.median(sh)), 2),
            "worst_sharpe": round(float(np.percentile(sh, 5)), 2),   # bad-case (5th pct), not the single min
            "pct_positive": round(float((sh > 0).mean() * 100)),
            "pct_beat_qqq": round(float(np.mean(beat) * 100))}


def _legacy_main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    comp, qqq_c = build_panel()
    wins = windows(comp)

    policy = load_policy()
    live_config = validate_config(policy.get("config") or DEFAULT_CONFIG)
    champion = {"name": f"champion (live policy: {policy.get('status', 'DEFAULT')})",
                **live_config}
    champ_ret = simulate(champion, comp, qqq_c)
    champ_m = metric_set(champ_ret, wins)
    champ_roll = rolling_robustness(champ_ret, comp["qqq"])

    survivors, reviews, evaluated = [], [], []
    candidate_returns: dict[str, pd.Series] = {}
    for cfg in default_candidates(live_config):
        cret = simulate(cfg, comp, qqq_c)
        candidate_returns[cfg["name"]] = cret
        cm = metric_set(cret, wins)
        tier, notes = classify(champ_m, cm)
        rec = {"name": cfg["name"],
               "config": {k: cfg.get(k) for k in _CFG_KEYS},
               "metrics": cm, "tier": tier, "notes": notes}
        if tier in ("survivor", "review"):  # rolling robustness for the ones we surface
            rec["rolling"] = rolling_robustness(cret, comp["qqq"])
        evaluated.append(rec)
        if tier == "survivor":
            survivors.append(rec)
        elif tier == "review":
            reviews.append(rec)

    survivors.sort(key=lambda r: r["metrics"]["full"]["sharpe"], reverse=True)
    reviews.sort(key=lambda r: r["metrics"]["recent"]["sharpe"], reverse=True)
    ts = datetime.now(timezone.utc).isoformat()

    state = load_state()
    autopilot_events = []
    if state.get("state_error"):
        autopilot_events.append({"action": "STATE_ERROR", "reason": state["state_error"]})
    else:
        if policy.get("previous_config"):
            baseline_ret = simulate(policy["previous_config"], comp, qqq_c)
            state, recovered = recover_deployment(
                state, policy=policy, candidate_returns=champ_ret,
                baseline_returns=baseline_ret, observed_at=ts)
            if recovered:
                autopilot_events.append(recovered)

        deployment = state.get("deployment")
        rolled_back = False
        if deployment and policy.get("previous_config"):
            baseline_ret = simulate(policy["previous_config"], comp, qqq_c)
            deployment, rollback = advance_rollback(
                deployment, candidate_returns=champ_ret,
                baseline_returns=baseline_ret, observed_at=ts)
            state["deployment"] = deployment
            autopilot_events.append(rollback)
            if rollback["action"] == "ROLLBACK":
                write_policy(
                    policy["previous_config"], previous_config=None,
                    status="AUTO_ROLLED_BACK", source="weekly_research_rollback",
                    evidence=rollback["evidence"])
                state.setdefault("history", []).append(dict(deployment))
                state["deployment"] = None
                rolled_back = True

        deployable_rows = []
        undeployable_rows = []
        for row in survivors:
            try:
                validate_config(row["config"])
            except ValueError as exc:
                undeployable_rows.append((row, str(exc)))
                continue
            deployable_rows.append(row)
        # Keep collecting the same valid shadow when it remains a survivor;
        # leaderboard rank changes must not reset forward evidence every week.
        deployable = None
        shadow_identity = (state.get("shadow") or {}).get("candidate_id")
        if shadow_identity:
            deployable = next(
                (row for row in deployable_rows
                 if candidate_id(row["config"]) == shadow_identity), None)
        deployable = deployable or (deployable_rows[0] if deployable_rows else None)

        if rolled_back:
            shadow_event = {"action": "SHADOW_SKIPPED_AFTER_ROLLBACK"}
        else:
            state, shadow_event = advance_shadow(
                state, candidate=deployable,
                candidate_returns=(candidate_returns.get(deployable["name"])
                                   if deployable else None),
                champion_returns=champ_ret, observed_at=ts)
        autopilot_events.append(shadow_event)
        if not deployable and undeployable_rows:
            row, reason = undeployable_rows[0]
            autopilot_events.append({
                "action": "UNDEPLOYABLE_SURVIVOR", "candidate": row["name"],
                "reason": reason,
            })
        if shadow_event["action"] == "PROMOTE":
            promoted_config = shadow_event["config"]
            promotion_evidence = {**shadow_event["evidence"],
                                  "candidate": deployable["name"]}
            write_policy(
                promoted_config, previous_config=live_config,
                status="AUTO_PROMOTED_PAPER", source="weekly_research_autopilot",
                evidence=promotion_evidence)
            last = latest_week(candidate_returns[deployable["name"]], champ_ret)
            promoted_shadow = dict(state["shadow"])
            promoted_shadow["status"] = "PROMOTED"
            promoted_shadow["promoted_at"] = ts
            state.setdefault("history", []).append(promoted_shadow)
            state["deployment"] = {
                "status": "DEPLOYED", "candidate_id": candidate_id(promoted_config),
                "name": deployable["name"], "config": promoted_config,
                "baseline_config": live_config, "promoted_at": ts,
                "last_observed_week": last["week"] if last else None,
                "observations": [], "metrics": {"weeks": 0, "rollback": False},
            }
            state["shadow"] = None
        save_state(state)

    record = {"ts": ts, "champion": {"name": champion["name"], "config": live_config,
                                       "metrics": champ_m, "rolling": champ_roll},
              "n_evaluated": len(evaluated), "n_survivors": len(survivors),
              "n_reviews": len(reviews), "survivors": survivors, "reviews": reviews,
              "evaluated": evaluated, "autopilot": autopilot_events}

    with JOURNAL.open("a") as f:
        f.write(json.dumps(record) + "\n")
    PENDING.write_text(json.dumps({"ts": ts, "survivors": survivors,
                                   "reviews": reviews,
                                   "autopilot": autopilot_events}, indent=2))

    print(f"\n=== WEEKLY PORTFOLIO RESEARCH | {ts[:10]} ===")
    print(f"  champion: full Sharpe {champ_m['full']['sharpe']:.2f} | CAGR {champ_m['full']['cagr']*100:.0f}% "
          f"| recent {champ_m['recent']['sharpe']:.2f} | bear {champ_m['bear']['sharpe']:.2f} | DD {champ_m['full']['dd']*100:.0f}%")
    if champ_roll:
        print(f"  champion rolling-1y (n={champ_roll['n_windows']}): median Sharpe {champ_roll['median_sharpe']} | "
              f"worst(5%) {champ_roll['worst_sharpe']} | {champ_roll['pct_positive']}% windows positive | "
              f"{champ_roll['pct_beat_qqq']}% beat QQQ")
    print(f"  evaluated {len(evaluated)} | survivors {len(survivors)} | reviews {len(reviews)}")
    for r in evaluated:
        m = r["metrics"]
        flag = {"survivor": "✅ SURVIVOR", "review": "🔎 REVIEW", "reject": "—"}[r["tier"]]
        print(f"    {flag:<12} {r['name']:<32} full {m['full']['sharpe']:.2f} "
              f"recent {m['recent']['sharpe']:.2f} bear {m['bear']['sharpe']:.2f} DD {m['full']['dd']*100:.0f}%")
        if r.get("rolling"):
            rr = r["rolling"]
            print(f"                 rolling-1y: median {rr['median_sharpe']}, worst(5%) {rr['worst_sharpe']}, "
                  f"{rr['pct_beat_qqq']}% beat QQQ")
        if r["tier"] == "reject" and r["notes"]:
            print(f"                 ↳ {r['notes'][0]}")

    if not args.no_telegram:
        try:
            send_telegram(_digest(record))
        except Exception:
            pass


def _digest(record: dict) -> str:
    ns, nr = record["n_survivors"], record["n_reviews"]
    cm = record["champion"]["metrics"]
    head = "🔬 *Weekly research* · " + record["ts"][:10]
    cr = record["champion"].get("rolling")
    roll = lambda r: (f" · rolling-1y med {r['rolling']['median_sharpe']}, {r['rolling']['pct_beat_qqq']}% beat QQQ"
                      if r.get("rolling") else "")
    champ_line = (f"Live: full Sharpe {cm['full']['sharpe']:.2f} · CAGR {cm['full']['cagr']*100:.0f}% · "
                  f"recent {cm['recent']['sharpe']:.2f} · DD {cm['full']['dd']*100:.0f}%"
                  + (f"\nrolling-1y: median {cr['median_sharpe']}, 5th-pct {cr['worst_sharpe']}, "
                     f"{cr['pct_beat_qqq']}% of overlapping 1y windows beat QQQ" if cr else ""))
    auto = record.get("autopilot") or []
    event = next((row for row in auto
                  if row.get("action") in {"ROLLBACK", "PROMOTE", "STATE_ERROR"}),
                 auto[-1] if auto else {"action": "NO_SURVIVOR"})
    action = event.get("action")
    if ns == 0 and nr == 0 and action not in {"ROLLBACK", "PROMOTE", "STATE_ERROR"}:
        return (f"{head}\n\n✅ *Champion held* — {record['n_evaluated']} variant(s) tested, none beat live.\n"
                f"{champ_line}\n\nNo action required.")
    lines = [head, ""]
    if ns:
        lines.append(f"🚦 *{ns} strict survivor(s)* (beat live on every historical gate):")
        for r in record["survivors"][:5]:
            m = r["metrics"]
            lines.append(f"• *{r['name']}* — full {m['full']['sharpe']:.2f} (champ {cm['full']['sharpe']:.2f}), "
                         f"recent {m['recent']['sharpe']:.2f}, DD {m['full']['dd']*100:.0f}%{roll(r)}")
    if nr:
        lines.append("")
        lines.append(f"🔎 *{nr} to REVIEW* (recent-regime edge, short of strict bar):")
        for r in record["reviews"][:5]:
            m = r["metrics"]
            lines.append(f"• *{r['name']}* — recent {m['recent']['sharpe']:.2f} (champ {cm['recent']['sharpe']:.2f}), "
                         f"full {m['full']['sharpe']:.2f}, bear {m['bear']['sharpe']:.2f}{roll(r)}")
    lines.append("")
    if action == "SHADOW_STARTED":
        lines.append(f"🧪 *Automatic shadow started:* {event.get('candidate')} · "
                     f"0/{event.get('minimum_weeks', 8)} forward weeks.")
    elif action == "SHADOW_PROGRESS":
        evidence = event.get("evidence") or {}
        lines.append(f"🧪 *Automatic shadow:* {event.get('candidate')} · "
                     f"{evidence.get('weeks', 0)}/8 forward weeks.")
    elif action == "PROMOTE":
        lines.append(f"✅ *Auto-promoted for paper trading:* {event.get('candidate')} · "
                     "rollback monitor active.")
    elif action == "ROLLBACK":
        lines.append(f"↩️ *Automatically rolled back:* {event.get('candidate')} · "
                     + "; ".join((event.get("evidence") or {}).get("reasons") or []))
    elif action == "UNDEPLOYABLE_SURVIVOR":
        lines.append(f"🧱 Survivor remains research-only: {event.get('reason')}")
    elif action == "STATE_ERROR":
        lines.append("🛑 Autopilot state is unreadable; production was left unchanged.")
    else:
        lines.append("The autonomous evidence state was updated.")
    lines.append(f"\n{champ_line}\n\nStatus only — no response is required. Reviews remain research-only.")
    return "\n".join(lines)


def main() -> None:
    # The legacy equal-third model is not the deployed portfolio. It must not
    # write policy or recycle historical wins into forward promotion evidence.
    from portfolio_lab import run
    run()


if __name__ == "__main__":
    main()
