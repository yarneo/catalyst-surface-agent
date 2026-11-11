"""Prospective earnings-drift evidence. This module has no order interface.

An EPS estimate must be observed before the reported result. Old downloaded
surprises remain retrospective research and cannot enter this virtual book.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timedelta
import math

from .sessions import ET, calendar, previous_session, session_bounds
from .market_tape import validate_quote

INITIAL = 10000.0
HOLD_SESSIONS = 60
MAX_POSITIONS = 2


def initial_state():
    return {"schema_version":1,"events":{},"cash":INITIAL,"positions":{},"closed":[],"days":{},"feed_errors":{}}


def instant(value):
    stamp=datetime.fromisoformat(str(value).replace("Z","+00:00"))
    if stamp.tzinfo is None:raise ValueError("earnings evidence requires aware timestamps")
    return stamp.astimezone(ET)


def finite(value):
    try:return value is not None and math.isfinite(float(value))
    except (TypeError,ValueError):return False


def observe_earnings(state, symbol, rows, *, observed_at):
    now=instant(observed_at)
    for raw in rows:
        scheduled=instant(raw["scheduled_at"])
        key=symbol+":"+str(scheduled.date())
        event=state["events"].setdefault(key,{"symbol":symbol,"scheduled_at":scheduled.isoformat(),"forecasts":[],"status":"AWAITING_ESTIMATE"})
        estimate,actual=raw.get("estimate"),raw.get("actual")
        if finite(actual):
            if "reported" in event:
                if float(actual)!=event["reported"]["actual"]:
                    event["revision_detected"]=True
                continue
            event["reported"]={"actual":float(actual),"first_observed_at":observed_at,
                               "timestamp_basis":"first provider observation, not a claimed release timestamp"}
            forecasts=[f for f in event["forecasts"] if instant(f["observed_at"])<now]
            if not forecasts or not finite(forecasts[-1]["estimate"]) or forecasts[-1]["estimate"]==0:
                event["status"]="RETROSPECTIVE_ONLY"
                event["reason"]="no estimate archived before the reported result"
                continue
            event["estimate_used"]=forecasts[-1]
            event["surprise"]=(float(actual)-forecasts[-1]["estimate"])/abs(forecasts[-1]["estimate"])
            cal=calendar(now.year)
            reaction=cal.date_to_session(str(now.date()),direction="next").date()
            if session_bounds(reaction)[1]<=now:
                reaction=cal.next_session(str(reaction)).date()
            event["reaction_session"]=str(reaction)
            event["entry_session"]=str(cal.next_session(str(reaction)).date())
            event["status"]="AWAITING_REACTION"
        elif finite(estimate) and scheduled>now and "reported" not in event:
            if not event["forecasts"] or event["forecasts"][-1]["estimate"]!=float(estimate):
                event["forecasts"].append({"estimate":float(estimate),"observed_at":observed_at,"scheduled_at":raw["scheduled_at"]})
            event["status"]="FORECAST_ARCHIVED"
        event["last_seen_at"]=observed_at


def confirm_reactions(state, frames, *, at):
    now=instant(at)
    for event in state["events"].values():
        if event["status"]!="AWAITING_REACTION":continue
        reaction=datetime.fromisoformat(event["reaction_session"]).date()
        if now < session_bounds(reaction)[1]+timedelta(minutes=5):continue
        frame=frames.get(event["symbol"])
        if frame is None:continue
        dates={str(d.date()):row for d,row in frame.iterrows()}
        prior=str(previous_session(reaction))
        if prior not in dates or str(reaction) not in dates:continue
        before=float(dates[prior].get("Adj Close",dates[prior]["Close"]))
        after=float(dates[str(reaction)].get("Adj Close",dates[str(reaction)]["Close"]))
        if not all(math.isfinite(x) and x>0 for x in (before,after)):continue
        move=after/before-1
        event.update(reaction_return=move,signal_observed_at=at,
                     status="PREQUALIFIED" if event["surprise"]>0 and move>0 else "REJECTED_ECONOMIC")
        if str(now.date())>event["entry_session"]:
            event["status"]="MISSED_ENTRY"


def begin_day(state, frames, *, at):
    now=instant(at);day=str(now.date())
    if day in state["days"]:return
    if session_bounds(now.date()) is None:return
    if now<session_bounds(now.date())[0]:return
    prior = max(state["days"], default=None)
    if state["positions"] and prior != str(previous_session(now.date())):
        raise ValueError("held-book observation gap; corporate actions require reconciliation")
    # Apply explicit split/dividend economics once, on the ex-date. These are
    # virtual cash flows, never broker credits.
    actions = {}
    for symbol,p in state["positions"].items():
        frame=frames[symbol]
        rows=frame.loc[frame.index.date==now.date()]
        if rows.empty:raise ValueError("missing held-asset corporate-action data")
        row=rows.iloc[-1]
        split=float(row.get("Stock Splits",0) or 1)
        dividend=float(row.get("Dividends",0) or 0)
        if not math.isfinite(split) or split<=0 or not math.isfinite(dividend) or dividend<0:
            raise ValueError("invalid corporate action")
        actions[symbol]=(split,dividend)
    # Validate the entire book before mutating any position, so a failed
    # second symbol cannot apply the first symbol's dividend twice on retry.
    for symbol,(split,dividend) in actions.items():
        p=state["positions"][symbol]
        p["qty"]*=split
        state["cash"]+=p["qty"]*dividend
        p["income"]=p.get("income",0)+p["qty"]*dividend
    state["days"][day]={"opening_cash":state["cash"],"opening_positions":deepcopy(state["positions"])}


def observe_quotes(state, quote_fetcher, *, at):
    """Virtual fills only; the injected interface can retrieve quotes, not trade."""
    now=instant(at);bounds=session_bounds(now.date())
    if bounds is None or not bounds[0]+timedelta(minutes=5)<=now<=bounds[0]+timedelta(minutes=15):
        return
    if str(now.date()) not in state["days"]:raise ValueError("day inputs must be captured before virtual execution")
    cal=calendar(now.year)
    for symbol,p in list(state["positions"].items()):
        if len(cal.sessions_in_range(p["entry_session"],str(now.date())))-1 < HOLD_SESSIONS:continue
        quote=quote_fetcher(symbol);bid,_=validate_quote(quote,at=at)
        price=bid*(1-.0005)
        state["cash"]+=p["qty"]*price
        state["closed"].append({**p,"symbol":symbol,"exit_at":at,"exit_quote":quote,"exit_price":price,
                                "pnl":p["qty"]*price-p["cost"]+p.get("income",0)})
        del state["positions"][symbol]
    for key,event in sorted(state["events"].items()):
        if event["status"]!="PREQUALIFIED" or event.get("revision_detected"):continue
        if event["entry_session"]<str(now.date()):
            event["status"]="MISSED_ENTRY";continue
        if event["entry_session"]!=str(now.date()):continue
        symbol=event["symbol"]
        if len(state["positions"])>=MAX_POSITIONS or symbol in state["positions"]:continue
        quote=quote_fetcher(symbol);_,ask=validate_quote(quote,at=at)
        price=ask*(1+.0005)
        notional=min(state["cash"],INITIAL/MAX_POSITIONS)
        qty=math.floor(notional/price*1000)/1000
        if qty<=0:continue
        state["cash"]-=qty*price
        state["positions"][symbol]={"event_id":key,"entry_session":str(now.date()),"entry_at":at,
            "entry_quote":quote,"entry_price":price,"qty":qty,"cost":qty*price,"income":0}
        event["status"]="VIRTUAL_POSITION_OPEN"


def mark_close(state, frames, *, at):
    now=instant(at);day=str(now.date());bounds=session_bounds(now.date())
    if bounds is None or now<bounds[1]+timedelta(minutes=5):return
    record=state["days"][day]
    if "close_equity" in record:return
    def value(positions,column):
        total=0
        for symbol,p in positions.items():
            rows=frames[symbol].loc[frames[symbol].index.date==now.date()]
            if rows.empty:raise ValueError("missing completed PEAD price")
            price=float(rows.iloc[-1][column])
            if not math.isfinite(price) or price<=0:raise ValueError("invalid PEAD mark")
            total+=p["qty"]*price
        return total
    record.update(open_equity=record["opening_cash"]+value(record["opening_positions"],"Open"),
                  close_equity=state["cash"]+value(state["positions"],"Close"),observed_at=at)


def summary(state, *, at):
    from collections import Counter
    counts=Counter(e["status"] for e in state["events"].values())
    forecasts=sum(bool(e["forecasts"]) for e in state["events"].values())
    days=[{"date":day,**r} for day,r in sorted(state["days"].items()) if "close_equity" in r]
    return {"status":"DATA_BLOCKED" if state.get("feed_errors") else "COLLECTING", "at":at,
        "order_authority":False,"initial_virtual_capital":INITIAL,"cash":state["cash"],
        "open_positions":len(state["positions"]),"completed_outcomes":len(state["closed"]),
        "realized_pnl":sum(r["pnl"] for r in state["closed"]),"forecast_events":forecasts,
        "event_stages":dict(counts),"feed_errors":state.get("feed_errors",{}),
        "hold_sessions":HOLD_SESSIONS,"daily_marks":days,
        "method":"estimate observed before result; completed positive reaction; subsequent fresh IEX ask/bid virtual fills plus 5bp impact",
        "limitation":"Provider observation timestamps establish what was known to this collector; they do not prove the issuer's exact publication time."}
