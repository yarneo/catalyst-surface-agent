"""Compare an independent simulated ledger/decision cycle with broker evidence."""
from copy import deepcopy
import json
from pathlib import Path
import pandas as pd

from .evidence import decode
from .replay import replay_cycle, verify_recorded_cycle

VERSION="observed-fills-independent-ledger-v1"
MODEL_TOLERANCE_BPS=5.
CASH_TOLERANCE_USD=.05


def compare_cycle(folder):
    exact=verify_recorded_cycle(folder)  # validates all archived hashes first
    folder=Path(folder)
    meta=json.loads((folder/"metadata.json").read_text())
    report={"version":VERSION,"cycle":folder.name,"session":exact.get("session"),
        "engine_hash":meta.get("engine_hash"),"passed":False,"order_authority":False,
        "network_access":False,"exact_replay":exact["status"],"status":"AWAITING_COMPLETE_CAPTURE"}
    if not all(k in meta for k in ("cash","positions","final_account","final_positions")):return report
    if exact["status"] not in {"MATCHED","MATCHED_FAILED_EXECUTION"}:
        return {**report,"status":"REPLAY_MISMATCH"}
    rows=[json.loads(line) for line in (folder/"broker.jsonl").read_text().splitlines()]
    requests=[];terminal={};quotes={};latest_quote={}
    for row in rows:
        if row["method"]=="quote" and "result" in row:
            symbol=row["args"][0];q=decode(row["result"])
            if "bid" in q:
                quotes.setdefault(symbol,[]).append(q);latest_quote[symbol]=q
        if row["method"]=="submit_order":
            request=decode(row["args"])[0]
            requests.append({"symbol":request.symbol,"side":request.side,"qty":float(request.qty),
                "client_id":request.client_order_id,"quote":deepcopy(latest_quote.get(request.symbol))})
        result=decode(row.get("result"))
        if hasattr(result,"client_order_id") and hasattr(result,"filled_qty"):
            terminal[result.client_order_id]=result
    ledger={s:{"qty":float(p["qty"]),"avg_entry":float(p["avg_entry"])} for s,p in meta["positions"].items()}
    cash=float(meta["cash"]);fills=[];slippage=[]
    for request in requests:
        fill=terminal.get(request["client_id"])
        if fill is None or fill.status not in {"filled","canceled","expired","rejected"}:
            return {**report,"status":"UNRESOLVED_FILL"}
        qty=float(fill.filled_qty or 0)
        price=float(fill.filled_avg_price or 0)
        fills.append({**request,"filled_qty":qty,"price":price,"status":fill.status})
        if qty<=0:continue
        symbol,side=request["symbol"],request["side"]
        old=ledger.get(symbol,{"qty":0.,"avg_entry":price})
        if side=="buy":
            cash-=qty*price
            ledger[symbol]={"qty":old["qty"]+qty,"avg_entry":(old["qty"]*old["avg_entry"]+qty*price)/(old["qty"]+qty)}
        else:
            cash+=qty*price;ledger[symbol]={**old,"qty":old["qty"]-qty}
        q=request["quote"]
        if q:
            reference=float(q["ask" if side=="buy" else "bid"])
            slippage.append((price/reference-1)*(1 if side=="buy" else -1)*10000)
    expected=meta["final_positions"]
    symbols=set(ledger)|set(expected)
    qty_errors={s:ledger.get(s,{}).get("qty",0.)-float(expected.get(s,{}).get("qty",0.)) for s in symbols}
    cash_error=cash-float(meta["final_account"]["cash"])
    basis_errors={s:ledger[s]["avg_entry"]-float(expected[s]["avg_entry"]) for s in symbols
        if s in ledger and s in expected and float(expected[s]["qty"])>1e-8}
    ledger_pass=abs(cash_error)<=CASH_TOLERANCE_USD and all(abs(x)<=1e-7 for x in qty_errors.values()) and all(abs(x)<=.01 for x in basis_errors.values())
    report.update(status="LEDGER_MISMATCH",ledger_passed=ledger_pass,cash_error_usd=cash_error,
        quantity_errors=qty_errors,basis_errors=basis_errors,orders=len(requests),
        cash_tolerance_usd=CASH_TOLERANCE_USD,model_tolerance_bps=MODEL_TOLERANCE_BPS,
        maximum_adverse_slippage_bps=max(slippage,default=None))
    if not ledger_pass:return report
    # Use observed fill prices, but let the simulated account independently
    # calculate quantities/cash/equity after each order. Broker account balances
    # are never injected into the simulated ledger after initialization.
    frames={s:pd.DataFrame(r["data"],columns=r["columns"],index=pd.to_datetime(r["index"]))
            for s,r in json.loads((folder/"signals.json").read_text()).items()}
    prices={s:float(p["price"]) for s,p in meta["positions"].items()}
    prices.update({s:(float(q[0]["bid"])+float(q[0]["ask"]))/2 for s,q in quotes.items() if s not in prices})
    snapshot={**meta,"state":meta["initial_state"]}
    # Protective-only opens may not have read allocation inputs at all.
    snapshot.setdefault("allocations",{});snapshot.setdefault("policy",{})
    tolerance=max(1.,(float(meta["cash"])+sum(float(p["qty"])*float(p["price"]) for p in meta["positions"].values()))*MODEL_TOLERANCE_BPS/10000)
    try:
        sim=replay_cycle(snapshot,frames,prices,cost_bps=0,quotes=quotes,fills=fills,
            externals=[r for r in rows if r["method"] in {"overlay","ai_authority"}],
            allow_failure=meta["status"]=="FAILED")
    except Exception as exc:
        return {**report,"status":"SIMULATOR_MISMATCH","error_type":type(exc).__name__,"error":str(exc)[:240]}
    errors={s:(float(sim["positions"].get(s,{}).get("qty",0))-float(expected.get(s,{}).get("qty",0)))
              *float(expected.get(s,{}).get("price",prices.get(s,0))) for s in set(sim["positions"])|set(expected)}
    state=meta.get("final_state",meta["initial_state"])
    state_errors={k:{"simulated":sim["state"].get(k),"observed":v} for k,v in state.items()
        if k!="updated_at" and (abs(float(sim["state"].get(k,0))-v)>tolerance
            if k in {"peak_equity","day_start_equity"} else sim["state"].get(k)!=v)}
    delta=sim["cash"]-float(meta["final_account"]["cash"])
    matched=(sim["error_type"]==meta.get("error_type") and len(sim["orders"])==len(fills) and abs(delta)<=tolerance
             and all(abs(x)<=tolerance for x in errors.values()) and not state_errors)
    costs_ok=len(slippage)==sum(f["filled_qty"]>0 for f in fills) and all(x<=25 for x in slippage)
    return {**report,"status":"MATCHED" if matched and costs_ok else "COST_MODEL_EXCEEDED" if matched else "SIMULATOR_MISMATCH",
        "passed":matched and costs_ok,"simulator_cash_error_usd":delta,"holding_value_errors":errors,
        "risk_state_errors":state_errors,"model_tolerance_usd":tolerance,"cost_envelope_passed":costs_ok,
        "scope":"production open decisions, independent cash/holdings ledger and risk state; observed fills and quotes; forward impact remains modeled"}
