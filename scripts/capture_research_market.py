"""Observe the frozen research universe; this client has no trading methods."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import os
import time
from trading_bot.runtime_state import DATA, atomic_json, execution_lock, ExecutionBusy
from trading_bot.market_tape import capture_tick, fingerprint, instant, quote_problem, quote_coverage


def iso_now():
    return datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")


def fetch_quotes(symbols, *, feed="iex"):
    from trading_bot.data.quotes import fetch_quotes as fetch
    return fetch(symbols, feed=feed)


def select_feed(symbols, *, at):
    from trading_bot.data.quotes import select_feed as select, credentials
    import hashlib
    identity = hashlib.sha256(str(credentials().get("ALPACA_API_KEY", "")).encode()).hexdigest()[:16]
    return select(symbols, at=at, fetch=fetch_quotes, data=DATA, identity=identity)


def observe_quotes(symbols, *, feed, initial=None, now=iso_now, sleep=time.sleep):
    """One bounded same-minute retry; retain actual exchange timestamps."""
    quotes=initial if initial is not None else fetch_quotes(symbols,feed=feed)
    at=now()
    bad=[s for s in symbols if quote_problem(quotes.get(s),at=at)]
    retry_error=None
    if bad and instant(at).second<40:
        sleep(2)
        try:
            refreshed=fetch_quotes(bad,feed=feed)
            for symbol,q in refreshed.items():
                # A provider regression must not replace a newer observation.
                try:
                    newer=symbol not in quotes or instant(q["at"])>=instant(quotes[symbol]["at"])
                except (KeyError,TypeError,ValueError):newer=True
                if newer:quotes[symbol]=q
        except Exception as exc:retry_error=type(exc).__name__
        at=now()
    return quotes,at,{"retried_symbols":bad,"retry_error":retry_error}


def run():
    now=datetime.now(ZoneInfo("America/New_York"))
    if now.weekday()>=5 or not (9,35)<=(now.hour,now.minute)<(16,0):return {"status":"OUTSIDE_WINDOW"}
    active=json.loads((DATA/"research/active_cohort.json").read_text())
    if str(now.date())<=active["start_after"]:
        report={"status":"AWAITING_FIRST_SESSION","at":iso_now(),"cohort":active["cohort"],
            "first_session_after":active["start_after"],"order_authority":False}
        atomic_json(DATA/"research/market_health.json",report)
        return report
    folder=DATA/"research/cohorts"/active["cohort"]
    path=folder/"decisions"/(str(now.date())+".json")
    if not path.exists():
        from trading_bot.sessions import session_bounds
        if session_bounds(now.date()) is None:return {"status":"OUTSIDE_WINDOW"}
        raise RuntimeError("pre-open research plan is missing")
    plan=json.loads(path.read_text())
    if fingerprint({k:v for k,v in plan.items() if k!="plan_hash"})!=plan["plan_hash"]:
        raise RuntimeError("invalid sealed plan")
    if not instant(plan["market_open"])+timedelta(minutes=5)<=now<instant(plan["market_close"]):return {"status":"OUTSIDE_WINDOW"}
    feed,initial,capabilities=select_feed(plan["symbols"],at=iso_now())
    quotes,at,retry=observe_quotes(plan["symbols"],feed=feed,initial=initial)
    row=capture_tick(folder,plan,quotes,at=at)
    if row is None:return {"status":"OUTSIDE_WINDOW"}
    coverage=quote_coverage(folder,plan,through=at)
    health={"status":"DATA_BLOCKED" if not quotes else "QUOTE_GAPS" if coverage["quote_gap_minutes_by_symbol"] or coverage["missing_tick_minutes"] else "COLLECTING",
        "at":at,"session":str(now.date()),"cohort":active["cohort"],"feed":feed,
        "feed_access":capabilities,"session_coverage":coverage,"retry":retry,
        "symbols":len(quotes),"errors":row["errors"] if row else {},"order_authority":False}
    atomic_json(DATA/"research/market_health.json",health)
    return health


if __name__=="__main__":
    try:
        with execution_lock(DATA/"research_market.lock"):
            try:report=run()
            except Exception as exc:
                report={"status":"DATA_BLOCKED","at":iso_now(),"error_type":type(exc).__name__,"order_authority":False}
                atomic_json(DATA/"research/market_health.json",report)
                print(json.dumps(report))
                raise
        print(json.dumps(report))
        if report["status"]=="DATA_BLOCKED":raise SystemExit(1)
    except ExecutionBusy:
        print(json.dumps({"status":"PREVIOUS_OBSERVER_RUNNING"}))
