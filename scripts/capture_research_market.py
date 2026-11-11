"""Observe the frozen research universe; this client has no trading methods."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
from trading_bot.runtime_state import DATA, atomic_json, execution_lock, ExecutionBusy
from trading_bot.market_tape import capture_tick, fingerprint, instant


def iso_now():
    return datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")


def fetch_quotes(symbols):
    import requests
    from dotenv import dotenv_values
    secrets=dotenv_values(DATA.parent/".env")
    response=requests.get("https://data.alpaca.markets/v2/stocks/quotes/latest",
        params={"symbols":",".join(symbols),"feed":"iex"},
        headers={"APCA-API-KEY-ID":secrets["ALPACA_API_KEY"],"APCA-API-SECRET-KEY":secrets["ALPACA_SECRET_KEY"]},
        timeout=(5,15))
    response.raise_for_status()
    return {s:{"bid":float(q["bp"]),"ask":float(q["ap"]),"at":q["t"],"feed":"iex"}
            for s,q in response.json()["quotes"].items()}


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
    quotes=fetch_quotes(plan["symbols"])
    row=capture_tick(folder,plan,quotes,at=iso_now())
    health={"status":"DATA_BLOCKED" if not quotes else "QUOTE_GAPS" if row and row["errors"] else "COLLECTING",
        "at":iso_now(),"session":str(now.date()),"cohort":active["cohort"],
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
