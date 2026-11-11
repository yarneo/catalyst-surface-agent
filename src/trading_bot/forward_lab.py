"""Prospective virtual books: freeze before open, settle once after close.

Past market panels may be revised by a provider. They must not rewrite either
an already observed return or the shares used for the next observation.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
import math
from pathlib import Path

from .portfolio_research import fingerprint
from .runtime_state import atomic_json
from .sessions import ET, calendar, previous_session, session_bounds


class ForwardEvidenceError(RuntimeError):
    pass


def seal_decisions(folder, manifest, targets, symbols, *, at):
    now = datetime.fromisoformat(at)
    if now.tzinfo is None:
        raise ForwardEvidenceError("forward timestamps require a timezone")
    now = now.astimezone(ET)
    bounds = session_bounds(now.date())
    if bounds is None or now >= bounds[0]:
        raise ForwardEvidenceError("forward decisions must be frozen before the exchange open")
    if str(now.date()) <= manifest["start_after"]:
        raise ForwardEvidenceError("the freeze session is not a forward session")
    result = {"cohort": manifest["cohort"], "session": str(now.date()),
              "prepared_at": at, "signal_session": str(previous_session(now.date())),
              "symbols": sorted(symbols), "targets": targets,
              "method": "pre-open frozen decisions; subsequent daily open/close proxy; modeled costs"}
    result["plan_hash"] = fingerprint(result)
    path = Path(folder) / "decisions" / (result["session"] + ".json")
    if path.exists():
        old = json.loads(path.read_text())
        if old["targets"] != targets or old["cohort"] != manifest["cohort"]:
            raise ForwardEvidenceError("an existing forward plan cannot be changed")
        return old
    atomic_json(path, result)
    return result


def advance(book, targets, marks, *, session, cost_bps):
    """Advance one day using stored shares and raw prices plus explicit actions."""
    if (any(not math.isfinite(w) or w < 0 for w in targets.values())
            or sum(targets.values()) > .980001 or not 0 <= cost_bps <= 10000):
        raise ForwardEvidenceError("invalid target weights or cost assumption")
    result = deepcopy(book)
    shares = result.setdefault("shares", {})
    for symbol, qty in list(shares.items()):
        if qty and symbol not in marks:
            raise ForwardEvidenceError("missing held-asset mark")
    for symbol, row in marks.items():
        if any(not math.isfinite(float(row[k])) or float(row[k]) <= 0 for k in ("open", "close")):
            raise ForwardEvidenceError("invalid raw market prices")
        split = float(row.get("split", 0) or 1)
        dividend = float(row.get("dividend", 0) or 0)
        if split <= 0 or not math.isfinite(split) or not math.isfinite(dividend) or dividend < 0:
            raise ForwardEvidenceError("invalid corporate action")
        shares[symbol] = shares.get(symbol, 0.) * split
        result["cash"] += shares[symbol] * dividend
    opening = result["cash"] + sum(qty * marks[s]["open"] for s, qty in shares.items())
    result["peak"] = max(result["peak"], opening)
    result["killed"] |= opening / result["peak"] - 1 <= -.20
    halted = opening / result["previous_equity"] - 1 <= -.03
    deltas = {}
    for symbol in sorted(set(shares) | set(targets)):
        if symbol not in marks:
            raise ForwardEvidenceError("missing target-asset mark")
        value = shares.get(symbol, 0) * marks[symbol]["open"]
        target = 0. if result["killed"] else targets.get(symbol, 0.) * opening
        if halted:
            target = min(target, value)
        threshold = .05 * target if symbol == "QLD" else .05 * opening if symbol in {"QQQ", "DBMF", "KMLM"} else 0
        if target > 0 and abs(target-value) < threshold:
            target = value
        deltas[symbol] = target - value
    cost, turnover = 0., 0.
    for symbol in sorted(deltas, key=lambda s: (deltas[s] > 0, s)):
        delta = deltas[symbol]
        if delta > 0:
            delta = min(delta, max(0, result["cash"] - opening*.02)/(1+cost_bps/10000))
        if abs(delta) < 1:
            continue
        charge = abs(delta) * cost_bps / 10000
        shares[symbol] = shares.get(symbol, 0.) + delta / marks[symbol]["open"]
        result["cash"] -= delta + charge
        cost += charge
        turnover += abs(delta)
    equity = result["cash"] + sum(qty * marks[s]["close"] for s, qty in shares.items())
    row = {"date": session, "equity": equity, "return": equity/result["previous_equity"]-1,
           "cash": result["cash"], "cost": cost, "turnover": turnover,
           "halted": halted, "killed": result["killed"]}
    result.update(previous_equity=equity, peak=max(result["peak"], equity), last_session=session)
    return result, row


def observations(folder, name, cost_bps=5):
    paths = sorted((Path(folder)/"observations"/str(cost_bps)/name).glob("*.json"))
    return [json.loads(p.read_text()) for p in paths]


def settle_session(folder, manifest, plan, marks, *, observed_at):
    now = datetime.fromisoformat(observed_at).astimezone(ET)
    day = datetime.fromisoformat(plan["session"]).date()
    bounds = session_bounds(day)
    content = {k:v for k,v in plan.items() if k != "plan_hash"}
    if fingerprint(content) != plan.get("plan_hash") or plan["cohort"] != manifest["cohort"]:
        raise ForwardEvidenceError("invalid forward plan fingerprint")
    prepared = datetime.fromisoformat(plan["prepared_at"])
    if (bounds is None or prepared.tzinfo is None or prepared >= bounds[0]
            or prepared.astimezone(ET).date() != day
            or plan.get("signal_session") != str(previous_session(day))):
        raise ForwardEvidenceError("plan was not prepared before the open")
    if now < bounds[1] + timedelta(minutes=5):
        raise ForwardEvidenceError("session has not completed")
    if set(plan["symbols"]) - marks.keys():
        raise ForwardEvidenceError("incomplete market marks")
    first_day = calendar(day.year).date_to_session(
        manifest["start_after"], direction="next").date()
    if str(first_day) <= manifest["start_after"]:
        first_day = calendar(day.year).next_session(str(first_day)).date()
    count = 0
    for cost_bps in (5,25):
        for name, targets in plan["targets"].items():
            path = Path(folder)/"observations"/str(cost_bps)/name/(plan["session"]+".json")
            if path.exists():
                if json.loads(path.read_text())["plan_hash"] != plan["plan_hash"]:
                    raise ForwardEvidenceError("conflicting immutable forward observation")
                continue
            prior = observations(folder,name,cost_bps)
            if prior:
                book = prior[-1]["book_after"]
                if book["last_session"] != str(previous_session(day)):
                    raise ForwardEvidenceError("forward observation gap; cannot invent missing sessions")
            else:
                if day != first_day:
                    raise ForwardEvidenceError("initial forward session was missed")
                book = {"shares":{},"cash":100000.,"previous_equity":100000.,"peak":100000.,"killed":False}
            after, row = advance(book,targets,marks,session=plan["session"],cost_bps=cost_bps)
            atomic_json(path, {"cohort":manifest["cohort"], "plan_hash":plan["plan_hash"],
                "observed_at":observed_at, "marks_hash":fingerprint(marks), "marks":marks,
                "cost_bps":cost_bps, "book_before":book, "book_after":after, "row":row,
                "dividend_basis":"modeled cash on ex-date; distinct from broker credited income"})
            count += 1
    return count
