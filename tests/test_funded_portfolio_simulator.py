"""Synthetic acceptance drills; none of these observations count as evidence."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from trading_bot.broker.replay import replay_cycle, MemoryClient
from trading_bot.broker.comparison import compare_cycle
from trading_bot.config import load_config
from trading_bot.forward_lab import seal_decisions, settle_session, observations
from trading_bot.live.executor import ExecutorState
from trading_bot.portfolio_policy import default_policy
from trading_bot.portfolio_research import EXPERIMENTS, Experiment, fingerprint, forward_gate
from trading_bot.portfolio_simulator import (
    VERSION, advance_production, capture_tick, frame_data, market_tape, new_book,
)
from trading_bot.sessions import session_bounds, calendar, previous_session


def fixture(day="2026-11-12", pead=False):
    prior=previous_session(datetime.fromisoformat(day).date())
    idx=calendar(prior.year).sessions_window(str(prior),-260).tz_localize(None)
    values=np.linspace(100,130,len(idx))
    frames={s:pd.DataFrame({"open":values,"close":values,"high":values+1,
        "low":values-1,"volume":100000},index=idx) for s in ("QQQ","QLD","DBMF","KMLM","BIL")}
    config=deepcopy(load_config())
    config["pead_universe"]=["AAPL","MSFT"]
    execution={"frames":frame_data(frames),"config":config,"champions":{},
        "allocations":{"weights":{"regime":.7},"verdicts":{"regime":"HEALTHY"}},
        "policy":default_policy(),"arguments":{"no_enrich":True},"version":VERSION}
    plan={"cohort":"synthetic","session":day,"signal_session":str(idx[-1].date()),
          "symbols":list(frames),"execution":execution,"plan_hash":"synthetic"}
    bounds=session_bounds(datetime.fromisoformat(day).date())
    plan.update(market_open=bounds[0].isoformat(),market_close=bounds[1].isoformat())
    if pead:
        plan["symbols"] += ["AAPL","MSFT"]
        events={s+":event":{"symbol":s,"status":"PREQUALIFIED","entry_session":day,"reaction_session":str(prior),
            "estimate_used":{"estimate":1.,"observed_at":str(prior-timedelta(days=1))+"T14:00:00+00:00"},
            "reported":{"actual":1.2,"first_observed_at":str(prior)+"T12:00:00+00:00"},
            "signal_observed_at":str(prior)+"T21:10:00+00:00","surprise":.2,"reaction_return":.04}
            for s in ("AAPL","MSFT")}
        plan["pead"]={"events":events,"collected_at":day+"T13:00:00+00:00",
            "frozen_at":day+"T14:20:00+00:00","base_bil_buffer":.1}
    marks={s:{"open":130.,"close":130.,"dividend":0.,"split":0.} for s in plan["symbols"]}
    return plan,frames,marks


def tick(plan,minute,prices=None):
    at=plan["session"]+"T"+minute+":00-05:00"
    return {"session":plan["session"],"at":at,"errors":{},
        "quotes":{s:{"bid":(prices or {}).get(s,130.)-.01,"ask":(prices or {}).get(s,130.)+.01,"at":at,"feed":"synthetic"}
                  for s in plan["symbols"]}}


def snapshot(plan):
    return {"cash":100000.,"last_equity":100000.,"positions":{},
        "state":asdict(ExecutorState(peak_equity=100000.,day_start_equity=100000.)),
        "at":plan["session"]+"T09:35:00-05:00","signal_session":plan["signal_session"],
        **{k:plan["execution"][k] for k in ("config","champions","allocations","policy","arguments")}}


def capture(tmp_path):
    plan,frames,_=fixture()
    result=replay_cycle(snapshot(plan),frames,{s:130. for s in frames},
        quotes=tick(plan,"09:35")["quotes"],capture_to=tmp_path)
    return next((tmp_path/"production_cycles").iterdir()),result


def revise_metadata(folder,change):
    path=folder/"metadata.json"
    meta=json.loads(path.read_text());change(meta);path.write_text(json.dumps(meta))
    integrity=json.loads((folder/"integrity.json").read_text())
    integrity["metadata.json"]=hashlib.sha256(path.read_bytes()).hexdigest()
    (folder/"integrity.json").write_text(json.dumps(integrity))


def test_independent_simulator_matches_recorded_actual_orchestrator(tmp_path):
    folder,_=capture(tmp_path)
    result=compare_cycle(folder)
    assert result["passed"],result
    assert result["cash_error_usd"]==pytest.approx(0)
    assert result["maximum_adverse_slippage_bps"]==pytest.approx(5)


def test_observed_cost_outlier_blocks_simulator_validation(tmp_path):
    plan,frames,_=fixture()
    replay_cycle(snapshot(plan),frames,{s:130. for s in frames},cost_bps=30,
        quotes=tick(plan,"09:35")["quotes"],capture_to=tmp_path)
    result=compare_cycle(next((tmp_path/"production_cycles").iterdir()))
    assert result["status"]=="COST_MODEL_EXCEEDED" and result["ledger_passed"],result
    assert not result["passed"]


@pytest.mark.parametrize("field",["cash","qty","basis"])
def test_independent_ledger_detects_final_broker_difference(tmp_path,field):
    folder,_=capture(tmp_path)
    def change(meta):
        if field=="cash":meta["final_account"]["cash"]+=10
        else:meta["final_positions"]["QLD"]["qty" if field=="qty" else "avg_entry"]+=1
    revise_metadata(folder,change)
    result=compare_cycle(folder)
    assert result["status"]=="LEDGER_MISMATCH",result


def test_funded_pead_shares_one_cash_account_and_respects_caps():
    plan,_,marks=fixture(pead=True)
    tape=[tick(plan,"09:35"),tick(plan,"09:40")]
    spec=next(s for s in EXPERIMENTS if s.name=="pead_10pct")
    book,row,details=advance_production(new_book(),plan,marks,tape,spec,5)
    assert len(book["pead_state"]["positions"])==2
    equity=row["equity"]
    deployed=sum(book["positions"][s]["qty"]*130 for s in ("AAPL","MSFT"))
    assert .095*equity<deployed<=.10*equity
    assert all(book["positions"][s]["qty"]*130<=.05*equity for s in ("AAPL","MSFT"))
    assert book["cash"]>=.019*equity
    assert book["positions"]["BIL"]["qty"]*130>=.099*equity
    assert equity<100000
    assert sum(o["side"]=="sell" and o["symbol"]=="BIL" for o in details["orders"])>=1
    # Capital conservation includes bid/ask spread and modeled impact.
    assert equity==pytest.approx(100000-row["cost"])
    assert not row["data_complete"]  # two quotes are not a complete session


def test_intraday_halt_sells_and_never_reenters_after_price_recovers():
    plan,_,marks=fixture()
    tape=[tick(plan,"09:35"),tick(plan,"09:36",{"QLD":85.}),tick(plan,"09:40")]
    book,row,details=advance_production(new_book(),plan,marks,tape,Experiment("baseline_70"),5)
    assert row["halted"]
    later=[o for o in details["orders"] if o["at"]>tape[0]["at"]]
    assert later and all(o["side"]=="sell" for o in later)


def test_missing_open_never_invents_a_later_entry():
    plan,_,marks=fixture(pead=True)
    spec=next(s for s in EXPERIMENTS if s.name=="pead_10pct")
    book,row,details=advance_production(new_book(),plan,marks,[tick(plan,"09:40")],spec,5)
    assert not details["orders"] and book["cash"]==100000
    assert not row["open_observed"] and not row["data_complete"]


def test_future_quote_and_missing_pead_evidence_fail_closed():
    plan,_,marks=fixture(pead=True)
    bad=tick(plan,"09:35");bad["quotes"]["QLD"]["at"]=plan["session"]+"T10:00:00-05:00"
    with pytest.raises(ValueError,match="stale"):
        advance_production(new_book(),plan,marks,[bad],Experiment("baseline_70"),5)
    plan["pead"]["events"]["AAPL:event"]["estimate_used"]["observed_at"]="2026-11-11T14:00:00+00:00"
    spec=next(s for s in EXPERIMENTS if s.name=="pead_10pct")
    with pytest.raises(ValueError,match="ordered prospective"):
        advance_production(new_book(),plan,marks,[tick(plan,"09:35"),tick(plan,"09:40")],spec,5)


def test_market_tape_is_immutable_and_detects_tampering(tmp_path):
    plan,_,_=fixture()
    row=tick(plan,"09:35")
    first=capture_tick(tmp_path,plan,row["quotes"],at=row["at"])
    changed=deepcopy(row["quotes"]);changed["QLD"]["ask"]=999
    assert capture_tick(tmp_path,plan,changed,at=row["at"])==first
    path=next((tmp_path/"market"/plan["session"]).glob("*.json"))
    first["quotes"]["QLD"]["ask"]=999;path.write_text(json.dumps(first))
    with pytest.raises(ValueError,match="altered"):market_tape(tmp_path,plan)


def test_all_variants_execute_using_the_same_rules():
    plan,_,marks=fixture(pead=True)
    results={}
    for spec in EXPERIMENTS:
        book,row,details=advance_production(new_book(),plan,marks,[tick(plan,"09:35"),tick(plan,"09:40")],spec,5)
        assert book["cash"]>=0 and row["equity"]<=100000
        results[spec.name]=book["positions"].get("QLD",{}).get("qty",0)
    assert results["baseline_70"]>results["cap_60"]>results["cap_50"]


def test_incomplete_minutes_cannot_validate_a_candidate():
    rows=[{"date":str(i),"return":.001,"cost":0,"turnover":0,"data_complete":False} for i in range(100)]
    gate=forward_gate(rows,[{**r,"return":0} for r in rows],parity={"passed":True})
    assert not gate["eligible"] and gate["complete_sessions"]==0


def test_pead_guard_exit_is_an_outcome_in_the_common_book():
    plan,_,marks=fixture(pead=True)
    spec=next(s for s in EXPERIMENTS if s.name=="pead_10pct")
    tape=[tick(plan,"09:35"),tick(plan,"09:40"),tick(plan,"09:41",{"AAPL":110.}),tick(plan,"09:42")]
    book,row,details=advance_production(new_book(),plan,marks,tape,spec,5)
    assert "AAPL" not in book["pead_state"]["positions"]
    closed=book["pead_state"]["closed"]
    assert len(closed)==1 and closed[0]["pnl"]<0
    assert row["pead_completed_outcomes"]==1
    assert sum(o["symbol"]=="AAPL" and o["side"]=="buy" for o in details["orders"])==1


def test_pead_60_session_exit_records_income_and_releases_capital():
    from trading_bot.sessions import calendar
    plan,_,marks=fixture(pead=True)
    plan["pead"]["events"]={}
    spec=next(s for s in EXPERIMENTS if s.name=="pead_10pct")
    cal=calendar(2026)
    entered=str(cal.sessions_window(plan["session"],-61)[0].date())
    book=new_book();book["cash"]=97600.
    book["positions"]={"AAPL":{"qty":20.,"avg_entry":120.}}
    book["pead_state"]["positions"]={"AAPL":{"event_id":"old","qty":20.,"entry_price":120.,
        "entry_session":entered,"income":0.,"realized_pnl":0.}}
    marks["AAPL"].update(dividend=1.)
    after,row,details=advance_production(book,plan,marks,[tick(plan,"09:35"),tick(plan,"09:40")],spec,5)
    assert not after["pead_state"]["positions"]
    outcome=after["pead_state"]["closed"][0]
    assert outcome["income"]==20
    sell=next(o for o in details["orders"] if o["symbol"]=="AAPL" and o["side"]=="sell")
    assert outcome["pnl"]==pytest.approx(20*(sell["filled_avg_price"]-120)+20)


def test_split_updates_pead_quantity_and_cost_basis_without_phantom_stop():
    plan,_,marks=fixture(pead=True);plan["pead"]["events"]={}
    spec=next(s for s in EXPERIMENTS if s.name=="pead_10pct")
    book=new_book();book["cash"]=97400.
    book["positions"]={"AAPL":{"qty":20.,"avg_entry":130.}}
    book["pead_state"]["positions"]={"AAPL":{"event_id":"old","qty":20.,"entry_price":130.,
        "entry_session":"2026-11-11","income":0.,"realized_pnl":0.}}
    marks["AAPL"].update(open=65,close=65,split=2)
    tape=[tick(plan,"09:35",{"AAPL":65.}),tick(plan,"09:40",{"AAPL":65.})]
    after,row,_=advance_production(book,plan,marks,tape,spec,5)
    assert after["positions"]["AAPL"]=={"qty":40.,"avg_entry":65.}
    assert after["pead_state"]["positions"]["AAPL"]["entry_price"]==65.
    assert not after["pead_state"]["closed"] and not row["halted"]


def test_experimental_adapter_refuses_real_broker():
    from types import SimpleNamespace
    from trading_bot.simulation_policy import require_memory
    with pytest.raises(RuntimeError,match="isolated memory broker"):
        require_memory(SimpleNamespace(_client=object()))


def test_partial_terminal_fill_changes_cash_and_blocks_dependent_buys(tmp_path):
    from types import SimpleNamespace
    from trading_bot.broker.orders import OrderCoordinator, ExecutionFailure
    client=MemoryClient({"cash":1000.,"last_equity":1000.},{"AAPL":100.},fills=[
        {"symbol":"AAPL","side":"buy","qty":4.,"filled_qty":2.,"price":100.,"status":"canceled"}])
    coordinator=OrderCoordinator(client,tmp_path/"orders.json","test")
    request=SimpleNamespace(symbol="AAPL",side="buy",qty=4.,client_order_id=None)
    with pytest.raises(ExecutionFailure,match="canceled"):
        coordinator.execute(request,sleeve="pead",reference=100.)
    assert client.cash==800 and client.positions["AAPL"]["qty"]==2


def test_partial_fill_failure_replays_and_reconciles_without_hiding_the_failure(tmp_path):
    from trading_bot.broker.orders import ExecutionFailure
    from trading_bot.broker.replay import verify_recorded_cycle
    plan,frames,_=fixture()
    quotes=tick(plan,"09:35")["quotes"]
    baseline=replay_cycle(snapshot(plan),frames,{s:130. for s in frames},quotes=quotes)
    first=baseline["orders"][0]
    fill={"symbol":first["symbol"],"side":first["side"],"qty":first["qty"],
          "filled_qty":first["qty"]/2,"price":first["filled_avg_price"],"status":"canceled"}
    with pytest.raises(RuntimeError,match="execution failed"):
        replay_cycle(snapshot(plan),frames,{s:130. for s in frames},quotes=quotes,fills=[fill],capture_to=tmp_path)
    folder=next((tmp_path/"production_cycles").iterdir())
    exact=verify_recorded_cycle(folder)
    assert exact["status"]=="MATCHED_FAILED_EXECUTION",exact
    comparison=compare_cycle(folder)
    assert comparison["passed"] and comparison["exact_replay"]=="MATCHED_FAILED_EXECUTION",comparison


def test_complete_half_day_and_all_cost_books_settle_once(tmp_path):
    plan,_,marks=fixture(day="2026-11-27",pead=True)
    manifest={"cohort":"synthetic","start_after":"2026-11-26"}
    sealed=seal_decisions(tmp_path,manifest,{s.name:{} for s in EXPERIMENTS},plan["symbols"],
        at="2026-11-27T09:20:00-05:00",execution=plan["execution"],pead=plan["pead"])
    opened,closed=session_bounds(datetime.fromisoformat(plan["session"]).date())
    minute=opened+timedelta(minutes=5)
    while minute<closed:
        row=tick(sealed,minute.strftime("%H:%M"))
        capture_tick(tmp_path,sealed,row["quotes"],at=row["at"])
        minute+=timedelta(minutes=1)
    assert len(market_tape(tmp_path,sealed))==205
    count=settle_session(tmp_path,manifest,sealed,marks,observed_at="2026-11-27T13:10:00-05:00")
    assert count==22
    for spec in EXPERIMENTS:
        for cost in (5,25):
            record=observations(tmp_path,spec.name,cost)[0]
            assert record["row"]["data_complete"]
            assert record["book_after"]["cash"]>=0
    assert settle_session(tmp_path,manifest,sealed,marks,observed_at="2026-11-27T13:20:00-05:00")==0


def test_minute_collector_does_not_import_the_research_engine():
    import subprocess
    import sys
    subprocess.run([sys.executable,"-c",
        "import scripts.capture_research_market, sys; "
        "assert not {'pandas','numpy','alpaca','trading_bot.portfolio_simulator'} & sys.modules.keys()"],check=True)


def test_lightweight_quote_client_is_bounded_and_read_only(monkeypatch):
    from scripts import capture_research_market as collector
    from types import SimpleNamespace
    import requests
    import dotenv
    calls=[]
    monkeypatch.setattr(dotenv,"dotenv_values",lambda path:{"ALPACA_API_KEY":"fixture","ALPACA_SECRET_KEY":"fixture"})
    def get(url,**kwargs):
        calls.append((url,kwargs))
        return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{"quotes":{"AAPL":{"bp":100.,"ap":100.1,"t":"2026-11-12T14:35:00Z"}}})
    monkeypatch.setattr(requests,"get",get)
    assert collector.fetch_quotes(["AAPL"])["AAPL"]["bid"]==100.
    assert calls[0][0]=="https://data.alpaca.markets/v2/stocks/quotes/latest"
    assert calls[0][1]["timeout"]==(5,15) and calls[0][1]["params"]["feed"]=="iex"
