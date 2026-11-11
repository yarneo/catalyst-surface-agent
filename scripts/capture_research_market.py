"""Observe the frozen research universe; this client has no trading methods."""
from datetime import timedelta
import json
from trading_bot.runtime_state import DATA, atomic_json, execution_lock, ExecutionBusy
from trading_bot.sessions import now_et, iso_now, session_bounds
from trading_bot.portfolio_simulator import capture_tick


def run():
    now=now_et();bounds=session_bounds(now.date())
    if bounds is None or not bounds[0]+timedelta(minutes=5)<=now<bounds[1]:return {"status":"OUTSIDE_WINDOW"}
    active=json.loads((DATA/"research/active_cohort.json").read_text())
    if str(now.date())<=active["start_after"]:
        report={"status":"AWAITING_FIRST_SESSION","at":iso_now(),"cohort":active["cohort"],
            "first_session_after":active["start_after"],"order_authority":False}
        atomic_json(DATA/"research/market_health.json",report)
        return report
    folder=DATA/"research/cohorts"/active["cohort"]
    path=folder/"decisions"/(str(now.date())+".json")
    if not path.exists():raise RuntimeError("pre-open research plan is missing")
    plan=json.loads(path.read_text())
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest
    from alpaca.data.enums import DataFeed
    from dotenv import dotenv_values
    secrets=dotenv_values(DATA.parent/".env")
    client=StockHistoricalDataClient(secrets["ALPACA_API_KEY"],secrets["ALPACA_SECRET_KEY"])
    raw=client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=plan["symbols"],feed=DataFeed.IEX))
    quotes={s:{"bid":float(q.bid_price),"ask":float(q.ask_price),"at":q.timestamp.isoformat(),"feed":"iex"} for s,q in raw.items()}
    row=capture_tick(folder,plan,quotes,at=iso_now())
    health={"status":"DATA_BLOCKED" if row and row["errors"] else "COLLECTING",
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
