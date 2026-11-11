"""Collect point-in-time earnings evidence and a separate, quote-priced book."""
from __future__ import annotations
import argparse
import json
from datetime import timedelta
import pandas as pd
from trading_bot.config import load_pead_universe
from trading_bot import pead_shadow as engine
from trading_bot.runtime_state import DATA, atomic_json, append_record, execution_lock
from trading_bot.sessions import iso_now, now_et


def run(phase="collect"):
    import yfinance as yf
    root=DATA/"pead_shadow"
    with execution_lock(root/"observer.lock"):
        path=root/"state.json"
        state=json.loads(path.read_text()) if path.exists() else engine.initial_state()
        errors={}
        if phase in {"collect","close"}:
            for symbol in load_pead_universe():
                try:
                    frame=yf.Ticker(symbol).get_earnings_dates(limit=12)
                    if frame is None or frame.empty:raise ValueError("no earnings records")
                    rows=[{"scheduled_at":pd.Timestamp(ts).isoformat(),
                        "estimate":float(r["EPS Estimate"]) if engine.finite(r.get("EPS Estimate")) else None,
                        "actual":float(r["Reported EPS"]) if engine.finite(r.get("Reported EPS")) else None}
                        for ts,r in frame.iterrows()]
                    at=iso_now()
                    append_record(root/"earnings_observations.jsonl",{"symbol":symbol,"observed_at":at,"source":"yahoo_earnings","rows":rows})
                    engine.observe_earnings(state,symbol,rows,observed_at=at)
                except Exception as exc:errors[symbol]=type(exc).__name__
            state["last_collection_at"]=iso_now()
            state["collection_errors"]=errors.copy()
        else:
            stamp=state.get("last_collection_at")
            if not stamp or (now_et()-engine.instant(stamp)).total_seconds()>86400:
                errors["collector"]="fresh earnings collection required"
        symbols=set(state["positions"])|{e["symbol"] for e in state["events"].values()
            if e["status"] in {"AWAITING_REACTION","PREQUALIFIED"}}
        frames={}
        for symbol in sorted(symbols):
            try:
                frames[symbol]=yf.Ticker(symbol).history(start=str(now_et().date()-timedelta(days=30)),
                    end=str(now_et().date()+timedelta(days=1)),auto_adjust=False,actions=True)
                if frames[symbol].empty:raise ValueError("empty price history")
            except Exception as exc:errors["prices:"+symbol]=type(exc).__name__
        at=iso_now()
        engine.confirm_reactions(state,frames,at=at)
        try:
            engine.begin_day(state,frames,at=at)
            if phase=="observe" and not errors:
                # This client has market-data methods only, no trading API.
                from alpaca.data.historical import StockHistoricalDataClient
                from alpaca.data.requests import StockLatestQuoteRequest
                from alpaca.data.enums import DataFeed
                from dotenv import dotenv_values
                secrets=dotenv_values(DATA.parent/".env")
                client=StockHistoricalDataClient(secrets["ALPACA_API_KEY"],secrets["ALPACA_SECRET_KEY"])
                def quote(symbol):
                    q=client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol,feed=DataFeed.IEX))[symbol]
                    return {"bid":float(q.bid_price),"ask":float(q.ask_price),"at":q.timestamp.isoformat(),"feed":"iex"}
                engine.observe_quotes(state,quote,at=at)
            if phase=="close":engine.mark_close(state,frames,at=at)
        except Exception as exc:errors["virtual_book"]=type(exc).__name__
        state["feed_errors"]={**state.get("collection_errors",{}),**errors}
        atomic_json(path,state)
        report=engine.summary(state,at=iso_now())
        atomic_json(root/"summary.json",report)
        print(json.dumps({k:report[k] for k in ("status","forecast_events","open_positions","completed_outcomes","feed_errors")}))
        return report


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--phase",choices=("collect","observe","close"),default="collect")
    run(parser.parse_args().phase)
