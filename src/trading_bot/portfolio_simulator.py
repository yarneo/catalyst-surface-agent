"""Prospective production-rule simulation using observed minute quotes.

The real orchestrator/guard sizes every order. Only broker fills, persistence
and notifications are replaced. Quotes are observed; impact is modeled.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timedelta
import json
import math

import pandas as pd

from .broker.replay import replay_cycle
from .live.executor import ExecutorState
from .portfolio_research import EXPERIMENTS, fingerprint
from .sessions import ET, session_bounds
from .simulation_policy import reconcile_pead
from .pead_shadow import validate_quote
from .market_tape import capture_tick, market_tape
from .market_tape import instant

VERSION="production-rules-observed-minutes-v1"


def frame_data(frames):
    return {s:json.loads(f.to_json(orient="split",date_format="iso")) for s,f in frames.items()}


def read_frames(raw):
    return {s:pd.DataFrame(r["data"],columns=r["columns"],index=pd.to_datetime(r["index"])) for s,r in raw.items()}


def new_book():
    return {"cash":100000.,"positions":{},"previous_equity":100000.,
        "risk_state":asdict(ExecutorState(peak_equity=100000.,day_start_equity=100000.)),
        "pead_state":{"positions":{},"closed":[],"consumed_events":[]}}


def apply_actions(book,marks):
    for symbol,p in book["positions"].items():
        if float(p["qty"])<=1e-8:continue
        row=marks[symbol]
        split=float(row.get("split",0) or 1);dividend=float(row.get("dividend",0) or 0)
        if not math.isfinite(split) or split<=0 or not math.isfinite(dividend) or dividend<0:
            raise ValueError("invalid corporate action")
        p["qty"]=float(p["qty"])*split;p["avg_entry"]=float(p["avg_entry"])/split
        book["cash"]+=p["qty"]*dividend
        pead=book["pead_state"]["positions"].get(symbol)
        if pead:
            pead["qty"]*=split;pead["entry_price"]/=split;pead["income"]+=p["qty"]*dividend


def advance_production(book,plan,marks,tape,spec,cost_bps):
    context=plan["execution"]
    if context["version"]!=VERSION:raise ValueError("unsupported simulation model")
    if not 0<=cost_bps<=25:raise ValueError("unsupported cost assumption")
    for symbol in set(plan["symbols"])|set(book["positions"]):
        if symbol not in marks or any(not math.isfinite(float(marks[symbol][k])) or float(marks[symbol][k])<=0 for k in ("open","close")):
            raise ValueError("invalid or missing raw market mark: "+symbol)
    frames=read_frames(context["frames"])
    result=deepcopy(book)
    apply_actions(result,marks)
    day=datetime.fromisoformat(plan["session"]).date()
    opened,closed=session_bounds(day)
    scheduled_open=opened+timedelta(minutes=5)
    pead_time=opened+timedelta(minutes=10)
    ticks={}
    for row in tape:
        stamp=datetime.fromisoformat(row["at"])
        if stamp.tzinfo is None or row.get("session")!=plan["session"]:
            raise ValueError("foreign or undated market observation")
        minute=stamp.astimezone(ET).replace(second=0,microsecond=0)
        if minute in ticks:raise ValueError("duplicate minute observation")
        if not scheduled_open<=minute<closed:raise ValueError("market observation outside session")
        for symbol in plan["symbols"]:
            if symbol not in row.get("errors",{}):validate_quote(row["quotes"][symbol],at=row["at"])
        ticks[minute]=row
    orders=[];missing=[];failures=[];carried_bil_marks=0;open_done=False;pead_done=not bool(spec.pead_weight)
    moment=scheduled_open
    while moment<closed:
        row=ticks.get(moment)
        if row is None:
            missing.append(moment.isoformat());moment+=timedelta(minutes=1);continue
        quotes=row["quotes"]
        prices={}
        for symbol,q in quotes.items():
            try:
                bid,ask=float(q["bid"]),float(q["ask"])
                age=(instant(row["at"])-instant(q["at"])).total_seconds()
                if all(math.isfinite(x) and x>0 for x in (bid,ask)) and ask>=bid and -5<=age<=86400:
                    prices[symbol]=(bid+ask)/2
            except (KeyError,TypeError,ValueError):pass
        held={s for s,p in result["positions"].items() if float(p["qty"])>1e-8}
        # BIL is exempt from strategy price stops. Its last quoted value may
        # mark an existing cash-equivalent holding, but never price a fill.
        # Every other held asset needs a fresh mark for minute-level protection.
        if (held-prices.keys()) or ((held-{"BIL"}) & row.get("errors",{}).keys()):
            missing.append(moment.isoformat());moment+=timedelta(minutes=1);continue
        carried_bil_marks += int("BIL" in held and "BIL" in row.get("errors",{}))
        # A missed opening quote never becomes a backdated fill. It is a
        # no-entry day. Existing holdings still receive later protection ticks.
        phase="open" if moment==scheduled_open else "risk"
        if spec.pead_weight and moment==pead_time and open_done:phase="pead"
        snapshot={"cash":result["cash"],"last_equity":book["previous_equity"],
            "positions":result["positions"],"state":result["risk_state"],"pead_state":result["pead_state"],
            "at":row["at"],"signal_session":plan["signal_session"],
            **{k:deepcopy(context[k]) for k in ("config","champions","allocations","policy","arguments")}}
        cycle=replay_cycle(snapshot,frames,prices,cost_bps=cost_bps,quotes=quotes,
            experiment=spec,pead_inputs=plan.get("pead"),phase=phase,allow_failure=True)
        result.update(cash=cycle["cash"],positions=cycle["positions"],risk_state=cycle["state"],pead_state=cycle["pead_state"])
        reconcile_pead(result["pead_state"],cycle["orders"],result["positions"],row["at"])
        orders.extend({**o,"at":row["at"],"reference_mid":prices[o["symbol"]]} for o in cycle["orders"])
        if cycle["error_type"]:
            failures.append({"at":row["at"],"error_type":cycle["error_type"]})
            missing.append(moment.isoformat())
        else:
            open_done |= phase=="open"
            pead_done |= phase=="pead"
        moment+=timedelta(minutes=1)
    result["positions"]={s:p for s,p in result["positions"].items() if float(p["qty"])>1e-8}
    equity=result["cash"]+sum(float(p["qty"])*float(marks[s]["close"]) for s,p in result["positions"].items())
    costs=sum(float(o["filled_qty"])*(float(o["filled_avg_price"])-o["reference_mid"])*(1 if o["side"]=="buy" else -1) for o in orders)
    turnover=sum(float(o["filled_qty"])*float(o["filled_avg_price"]) for o in orders)
    result["risk_state"]["peak_equity"]=max(result["risk_state"]["peak_equity"],equity)
    result.update(previous_equity=equity,last_session=plan["session"])
    row={"date":plan["session"],"equity":equity,"return":equity/book["previous_equity"]-1,
        "cash":result["cash"],"cost":costs,"turnover":turnover,
        "halted":result["risk_state"]["halted_today"],"killed":result["risk_state"]["killed"],
        "data_complete":not missing and open_done and pead_done,"missing_minutes":len(missing),
        "carried_bil_mark_minutes":carried_bil_marks,"execution_failures":len(failures),
        "open_observed":open_done,"pead_window_observed":pead_done,
        "pead_open_positions":len(result["pead_state"]["positions"]),
        "pead_completed_outcomes":len(result["pead_state"]["closed"])}
    return result,row,{"orders":orders,"missing_minutes":missing,"execution_failures":failures,"quote_tape_hash":fingerprint(tape),"model":VERSION}
