"""Run a fixed, auditable research family; write private results, never orders."""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import pandas as pd
from trading_bot.allocations import read_allocations
from trading_bot.data import load_ohlcv
from trading_bot.learning import ChampionStore
from trading_bot.portfolio_policy import load_policy
from trading_bot.portfolio_research import (EXPERIMENTS, MODEL_VERSION, fingerprint,
    forward_gate, performance, simulate, regime_states, target_weights)
from trading_bot.forward_lab import seal_decisions, settle_session, observations, ForwardEvidenceError
from trading_bot.risk import VolatilityTargeted
from trading_bot.runtime_state import DATA, atomic_json, append_record, execution_lock
from trading_bot.sessions import last_completed_session, iso_now, now_et, session_bounds
from trading_bot.strategies import REGISTRY
from trading_bot.config import load_config
from trading_bot.broker.evidence import engine_fingerprint
from trading_bot.portfolio_simulator import frame_data, VERSION as SIMULATOR_VERSION


def run():
    with execution_lock(DATA / "portfolio_lab.lock"):
        return _run()


def freeze_manifest(cohort, config, end, at=None):
    at = at or iso_now()
    frozen_day = datetime.fromisoformat(at).astimezone(ZoneInfo("America/New_York")).date()
    # An intraday freeze cannot claim the already elapsed opening fill as a
    # forward observation. Start after the entire freeze session.
    return {"cohort": cohort, "frozen_at": at, "start_after": str(max(end, frozen_day)), **config}


def context():
    folder = DATA / "research"
    folder.mkdir(parents=True, exist_ok=True)
    end = last_completed_session()
    policy = load_policy()
    if policy["status"] == "INVALID_FALLBACK":
        raise RuntimeError("invalid live policy")
    champions = ChampionStore().all()
    root = Path(__file__).resolve().parents[1]
    # Reporting/transport repairs must not restart a trading-model cohort.
    source_paths = [Path(__file__).resolve(), *(root / "src/trading_bot" / p for p in
        ("portfolio_research.py", "forward_lab.py", "shadow_regime.py", "ownership.py",
         "live/managed_futures_executor.py", "risk/sizing.py", "portfolio_simulator.py", "simulation_policy.py", "pead_shadow.py", "market_tape.py")),
        *sorted((root / "src/trading_bot/strategies").glob("*.py"))]
    source_hash = fingerprint([(p.relative_to(root).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()) for p in source_paths])
    cohort_config = {"model": MODEL_VERSION,"simulator":SIMULATOR_VERSION,"engine_hash":engine_fingerprint(root),
                     "runtime_config":load_config(), "policy": policy["config"],
                     "source_hash": source_hash,
                     "champions": {s: c.as_dict() for s,c in champions.items()},
                     "experiments": [asdict(x) for x in EXPERIMENTS], "cost_bps": [5, 25]}
    cohort = fingerprint(cohort_config)
    manifest_path = folder / "cohorts" / (cohort + ".json")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = freeze_manifest(cohort, cohort_config, end)
        atomic_json(manifest_path, manifest)
    atomic_json(folder/"active_cohort.json",{"cohort":cohort,"start_after":manifest["start_after"]})
    return folder, end, policy, champions, cohort, manifest


def inputs(champions, end):
    symbols = sorted(set(champions) | {"QQQ", "QLD", "DBMF", "KMLM", "BIL", "SPY"})
    frames = {s: load_ohlcv(s, "2006-01-01", str(end + timedelta(days=1))) for s in symbols}
    for s,frame in frames.items():
        if frame.empty or frame.index[-1].date() != end:
            raise RuntimeError(s + ": stale research market data")
    opens = pd.DataFrame({s: f["open"] for s,f in frames.items()}).dropna()
    closes = pd.DataFrame({s: f["close"] for s,f in frames.items()}).reindex(opens.index)
    signals = {}
    for s,c in champions.items():
        strat = VolatilityTargeted(REGISTRY[c.strategy](**c.params), target_ann_vol=.10, max_leverage=1.)
        signals[s] = strat.generate_signals(frames[s]).clip(lower=0, upper=.60) / max(1,len(champions))
    etf = pd.DataFrame(signals).reindex(opens.index).fillna(0.)
    return frames, opens, closes, etf


def prepare():
    with execution_lock(DATA / "portfolio_lab.lock"):
        now = now_et()
        bounds = session_bounds(now.date())
        if bounds is None or now >= bounds[0]:
            print(json.dumps({"status":"OUTSIDE_PREOPEN_WINDOW", "order_authority":False}))
            return
        folder, end, policy, champions, cohort, manifest = context()
        frames, opens, closes, etf = inputs(champions, end)
        allocation = read_allocations(require_fresh=True)
        verdict = allocation.get("verdicts", {}).get("regime", "UNKNOWN")
        qqq = closes.QQQ
        sma = qqq.rolling(policy["config"]["ma_window"]).mean()
        vol = float(qqq.pct_change().tail(20).std() * (252**.5))
        targets = {spec.name:target_weights(
            regime_on=bool(regime_states(qqq,sma,spec)[-1]), qqq_vol=vol, verdict=verdict,
            etf_targets=etf.iloc[-1].to_dict(), spec=spec, policy=policy["config"])
            for spec in EXPERIMENTS}
        # Targets are descriptive diagnostics. Execution uses the frozen real
        # orchestrator inputs and observed quotes, not this approximate vector.
        pead_state=json.loads((DATA/"pead_shadow/state.json").read_text())
        collected=datetime.fromisoformat(pead_state["last_collection_at"])
        if (now-collected).total_seconds()>86400 or pead_state.get("collection_errors"):
            raise ForwardEvidenceError("fresh, complete PEAD collection required before freezing")
        symbols=set(closes.columns)-{"SPY"} if "SPY" not in champions else set(closes.columns)
        symbols |= {e["symbol"] for e in pead_state["events"].values()
                    if e["status"]=="PREQUALIFIED" and e.get("entry_session")==str(now.date())}
        for cost in (5,25):
            for prior in observations(folder/"cohorts"/cohort,"pead_10pct",cost)[-1:]:
                symbols |= {s for s,p in prior["book_after"]["positions"].items() if float(p["qty"])>1e-8}
        execution={"frames":frame_data({s:f.tail(600) for s,f in frames.items()}),
            "config":load_config(),"champions":{s:c.as_dict() for s,c in champions.items()},
            "allocations":allocation,"policy":policy,"arguments":{"no_enrich":True},"version":SIMULATOR_VERSION}
        pead={"events":pead_state["events"],"collected_at":pead_state["last_collection_at"],"frozen_at":iso_now(),
              "base_bil_buffer":policy["config"]["bil_buffer"]}
        plan = seal_decisions(folder/"cohorts"/cohort, manifest, targets, symbols, at=iso_now(),execution=execution,pead=pead)
        print(json.dumps({"status":"PREOPEN_FROZEN", "session":plan["session"], "cohort":cohort,
                          "candidates":len(targets), "order_authority":False}))


def raw_marks(symbols, day):
    import yfinance as yf
    marks = {}
    for symbol in symbols:
        frame = yf.Ticker(symbol).history(start=str(day), end=str(day+timedelta(days=1)),
                                          auto_adjust=False, actions=True)
        rows = frame.loc[frame.index.date == day]
        if len(rows) != 1:
            raise ForwardEvidenceError(symbol+": completed raw mark unavailable")
        row = rows.iloc[0]
        marks[symbol] = {"open":float(row.Open), "close":float(row.Close),
                         "dividend":float(row.get("Dividends",0)), "split":float(row.get("Stock Splits",0))}
    return marks


def _run():
    from verify_portfolio_replays import verify_all
    verification = verify_all()
    folder, end, policy, champions, cohort, manifest = context()
    frames, opens, closes, etf = inputs(champions, end)
    data_hash = hashlib.sha256(pd.util.hash_pandas_object(pd.concat([opens, closes], axis=1), index=True).values.tobytes()).hexdigest()
    panel_path = folder / "panels" / (data_hash + ".parquet")
    panel_path.parent.mkdir(exist_ok=True)
    if not panel_path.exists():
        pd.concat({"open": opens, "close": closes}, axis=1).to_parquet(panel_path)
    # Explicitly conditional historical proxy. We do not invent an archived
    # allocation verdict for days before we recorded it.
    health = pd.Series("HEALTHY", index=opens.index)
    health_log = folder / "allocation_evidence.jsonl"
    allocation = read_allocations()
    observed = {"observed_at": iso_now(), "signal_session": str(end),
                "regime_verdict": allocation.get("verdicts", {}).get("regime", "ERROR")}
    append_record(health_log, observed)
    for line in health_log.read_text().splitlines():
        row = json.loads(line)
        health.loc[health.index >= pd.Timestamp(row["signal_session"])] = row["regime_verdict"]
    comparison=verification.get("simulator_comparison",{})
    parity = {"passed":verification["status"]=="VERIFIED" and comparison.get("status")=="VERIFIED",
        "blockers":[] if verification["status"]=="VERIFIED" and comparison.get("status")=="VERIFIED" else
            ["five distinct production opens with matching replay, independent simulator accounting and observed execution costs are required"],
        "shared_rules":["actual production orchestrator", "actual portfolio guard at observed minute quotes", "funded PEAD adapter"],
        "legacy_proxy_quarantined":True,"comparison":comparison}
    rows, by_name, high_cost = [], {}, {}
    benchmark = closes.QQQ.pct_change()
    for spec in EXPERIMENTS:
        history = simulate(opens, closes, etf, health, spec, policy["config"])
        if history is None:
            rows.append({"name": spec.name, "status": "FORWARD_ONLY", "reason": "historical PEAD inputs were not archived; funded forward execution is wired", "order_authority": False})
            continue
        stressed = simulate(opens, closes, etf, health, spec, policy["config"], cost_bps=25)
        by_name[spec.name] = history
        high_cost[spec.name] = stressed
        rows.append({"name": spec.name, "status": "CONDITIONAL_RESEARCH", "historical": performance(history, benchmark),
                     "high_cost": performance(stressed, benchmark), "forward": [], "order_authority": False})
    forward_root = folder / "cohorts" / cohort
    plan_path = forward_root / "decisions" / (str(end)+".json")
    collection = {"status":"AWAITING_PREOPEN_PLAN", "session":str(end), "method":"persistent shares; immutable decisions frozen before open"}
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text())
            settle_session(forward_root,manifest,plan,raw_marks(plan["symbols"],end),observed_at=iso_now())
            collection["status"] = "OBSERVED"
        except Exception as exc:
            collection.update(status="BLOCKED", reason=str(exc))
    elif str(end) > manifest["start_after"]:
        collection.update(status="BLOCKED", reason="completed session has no pre-open decision record")
    forward = {spec.name:[x["row"] for x in observations(forward_root,spec.name)] for spec in EXPERIMENTS}
    stressed_forward = {spec.name:[x["row"] for x in observations(forward_root,spec.name,25)] for spec in EXPERIMENTS}
    if collection["status"]=="OBSERVED":
        latest=[r[-1] for r in (*forward.values(),*stressed_forward.values()) if r]
        if any(not r.get("data_complete",False) for r in latest):
            collection.update(status="DATA_INCOMPLETE",reason="missing observed minutes; P&L retained, session excluded from complete-coverage count",
                missing_minutes=max(r.get("missing_minutes",0) for r in latest))
    for row in rows:
        series = forward[row["name"]]
        row["forward"] = performance(series)
        row["latest_market_coverage"] = {k:series[-1].get(k) for k in
            ("data_complete","missing_minutes","carried_bil_mark_minutes","execution_failures")} if series else {}
        row["forward_high_cost"] = performance(stressed_forward[row["name"]])
        row["promotion"] = forward_gate(series, forward["baseline_70"], parity=parity)
        row["high_cost_gate"] = forward_gate(stressed_forward[row["name"]],stressed_forward["baseline_70"],parity=parity)
        if not row["high_cost_gate"]["eligible"]:
            row["promotion"]["eligible"]=False
            if row["promotion"]["status"]=="ELIGIBLE_FOR_REPLAY_REVIEW":row["promotion"]["status"]="COLLECTING"
            row["promotion"]["reasons"].append("25bp stress-cost comparison must also pass")
        if row["name"]=="pead_10pct":
            completed=series[-1].get("pead_completed_outcomes",0) if series else 0
            row["pead_completed_outcomes"]=completed
            row["pead_open_positions"]=series[-1].get("pead_open_positions",0) if series else 0
            if not completed:
                row["promotion"]["eligible"]=False
                if row["promotion"]["status"]!="RETIRED":row["promotion"]["status"]="AWAITING_PEAD_OUTCOME"
                row["promotion"]["reasons"].append("at least one completed funded PEAD outcome required; a cash-only sleeve cannot validate earnings drift")
        # Rebase only the displayed curve; forward returns remain unaltered.
        eq = 100000.
        row["curve"] = []
        for r in series:
            eq *= 1+r["return"]
            row["curve"].append({"date":r["date"], "equity":eq})
    baseline = by_name["baseline_70"]
    crises = []
    for name,start,finish in [("GFC","2008-01-01","2009-03-10"), ("Q4 2018","2018-10-01","2018-12-31"),
                               ("COVID","2020-02-19","2020-04-01"), ("2022","2022-01-01","2022-12-31")]:
        sample = [r for r in baseline if start <= r["date"] <= finish]
        qqq_window = frames["QQQ"].loc[start:finish, "close"]
        crises.append({"name":name, "status":"COVERED_PROXY" if sample else "UNAVAILABLE_FUND_HISTORY",
                       "metrics": performance(sample),
                       "qqq_return": float(qqq_window.iloc[-1]/qqq_window.iloc[0]-1) if len(qqq_window)>1 else None})
    output = {"status":"DATA_BLOCKED" if collection["status"] in {"BLOCKED","DATA_INCOMPLETE"} else "COLLECTING", "at":iso_now(), "as_of":str(end), "cohort":cohort,
              "frozen_at":manifest["frozen_at"], "forward_start_after":manifest["start_after"], "data_hash":data_hash, "parity":parity,
              "method":"frozen production rules; observed 09:35 and subsequent minute quotes; funded PEAD at 09:40; 5/25bp impact",
              "engineering":{"status":"READY","simulator":SIMULATOR_VERSION,"pead_integration":"FUNDED_COMMON_BOOK","remaining_implementation":[]},
              "candidates":rows, "historical_crises":crises,
              "forward_collection":collection,
              "production_verification":verification,
              "promotion_authority":False, "crypto_enabled":False,
              "next_action":"scheduled collectors supply real quotes and paper cycles; all comparison and integration paths are wired"}
    atomic_json(folder / "portfolio_lab.json", output)
    print(json.dumps({"status":output["status"], "cohort":cohort, "candidates":len(rows), "as_of":str(end), "parity_passed":parity["passed"]}))
    return output


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true", help="freeze decisions before the exchange open")
    args = parser.parse_args()
    if args.prepare:
        prepare()
    elif run()["status"] == "DATA_BLOCKED":
        raise SystemExit(1)
