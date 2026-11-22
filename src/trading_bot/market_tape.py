"""Lightweight, immutable quote capture; no broker SDK or dataframe imports."""
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo
from collections import Counter

from .runtime_state import atomic_json

ET = ZoneInfo("America/New_York")


def instant(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("quote observation requires an aware timestamp")
    return stamp.astimezone(ET)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:20]


def validate_quote(quote, *, at):
    bid, ask = float(quote["bid"]), float(quote["ask"])
    age = (instant(at) - instant(quote["at"])).total_seconds()
    if (not all(math.isfinite(x) and x > 0 for x in (bid, ask)) or ask < bid
            or not -5 <= age <= 120 or (ask - bid) / ((ask + bid) / 2) > .02):
        raise ValueError("missing, stale or wide executable quote")
    return bid, ask


def quote_problem(quote, *, at):
    """Explain coverage failures without changing executable-quote limits."""
    if quote is None:return "MISSING_QUOTE"
    try:
        bid,ask=float(quote["bid"]),float(quote["ask"])
        age=(instant(at)-instant(quote["at"])).total_seconds()
        if not all(math.isfinite(x) and x>0 for x in (bid,ask)) or ask<bid:return "INVALID_QUOTE"
        if age < -5:return "FUTURE_QUOTE"
        if age > 120:return "STALE_QUOTE"
        if (ask-bid)/((ask+bid)/2)>.02:return "WIDE_QUOTE"
        return None
    except (KeyError,TypeError,ValueError,OverflowError):return "INVALID_QUOTE"


def quote_coverage(folder, plan, *, through):
    """Session totals survive a healthy last tick; gaps are never overwritten."""
    rows=market_tape(folder,plan)
    first=instant(plan["market_open"])+timedelta(minutes=5)
    end=min(instant(through).replace(second=0,microsecond=0)+timedelta(minutes=1),instant(plan["market_close"]))
    expected=max(0,int((end-first).total_seconds()/60))
    by_symbol={s:Counter() for s in plan["symbols"]}
    for row in rows:
        for symbol in plan["symbols"]:
            problem=quote_problem(row.get("quotes",{}).get(symbol),at=row["at"])
            if problem:by_symbol[symbol][problem]+=1
    return {"session":plan["session"],"expected_minutes":expected,"collected_minutes":len(rows),
        "missing_tick_minutes":max(0,expected-len(rows)),
        "quote_gap_minutes_by_symbol":{s:sum(v.values()) for s,v in by_symbol.items() if v},
        "problems_by_symbol":{s:dict(v) for s,v in by_symbol.items() if v}}


def capture_tick(folder, plan, quotes, *, at):
    stamp = instant(at)
    # Calendar bounds were validated and sealed before open. Using them avoids
    # importing the research/dataframe engine in every minute's observer.
    opened, closed = instant(plan["market_open"]), instant(plan["market_close"])
    if not opened + timedelta(minutes=5) <= stamp < closed:
        return None
    if plan["session"] != str(stamp.date()):
        raise ValueError("quote session differs from sealed plan")
    path = Path(folder) / "market" / plan["session"] / (stamp.strftime("%H%M") + ".json")
    if path.exists():
        return json.loads(path.read_text())
    errors = {}
    for symbol in plan["symbols"]:
        problem=quote_problem(quotes.get(symbol),at=at)
        if problem:errors[symbol]=problem
    row = {"session": plan["session"], "at": at, "plan_hash": plan["plan_hash"],
           "quotes": quotes, "errors": errors}
    row["sha256"] = fingerprint(row)
    atomic_json(path, row)
    return row


def market_tape(folder, plan):
    rows = []
    for path in sorted((Path(folder) / "market" / plan["session"]).glob("*.json")):
        row = json.loads(path.read_text())
        if (row.get("plan_hash") != plan["plan_hash"]
                or fingerprint({k: v for k, v in row.items() if k != "sha256"}) != row.get("sha256")):
            raise ValueError("altered or foreign quote tape")
        rows.append(row)
    return rows
