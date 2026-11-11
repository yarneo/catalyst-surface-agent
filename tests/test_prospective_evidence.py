from datetime import date
from pathlib import Path
from dataclasses import asdict
import hashlib
import json
import numpy as np
import pandas as pd
import pytest

from trading_bot.forward_lab import ForwardEvidenceError, advance, observations, seal_decisions, settle_session
from trading_bot import pead_shadow as pead


MANIFEST={"cohort":"test","start_after":"2026-11-11"}
TARGETS={"baseline_70":{"QLD":.70,"BIL":.28}}
MARKS={s:{"open":100.,"close":110. if s=="QLD" else 100.,"dividend":0.,"split":0.} for s in ("QLD","BIL")}


def test_no_forward_plan_can_be_backdated_after_the_open(tmp_path):
    with pytest.raises(ForwardEvidenceError,match="before"):
        seal_decisions(tmp_path,MANIFEST,TARGETS,MARKS,at="2026-11-12T15:00:00+00:00")


def test_forward_books_preserve_shares_and_ignore_later_price_revisions(tmp_path):
    plan=seal_decisions(tmp_path,MANIFEST,TARGETS,MARKS,at="2026-11-12T13:00:00+00:00")
    settle_session(tmp_path,MANIFEST,plan,MARKS,observed_at="2026-11-12T21:10:00+00:00")
    first=observations(tmp_path,"baseline_70")[0]
    revised={s:{**row,"close":999.} for s,row in MARKS.items()}
    assert settle_session(tmp_path,MANIFEST,plan,revised,observed_at="2026-11-13T21:10:00+00:00")==0
    assert observations(tmp_path,"baseline_70")[0]==first
    second_plan=seal_decisions(tmp_path,MANIFEST,TARGETS,MARKS,at="2026-11-13T13:00:00+00:00")
    settle_session(tmp_path,MANIFEST,second_plan,MARKS,observed_at="2026-11-13T21:10:00+00:00")
    second=observations(tmp_path,"baseline_70")[1]
    assert second["book_before"]==first["book_after"]


def test_missing_session_cannot_be_backfilled_as_forward(tmp_path):
    plan=seal_decisions(tmp_path,MANIFEST,TARGETS,MARKS,at="2026-11-13T13:00:00+00:00")
    with pytest.raises(ForwardEvidenceError,match="missed"):
        settle_session(tmp_path,MANIFEST,plan,MARKS,observed_at="2026-11-13T21:10:00+00:00")


def test_split_and_dividend_do_not_create_phantom_losses():
    book={"shares":{"QLD":100.},"cash":0.,"previous_equity":10000.,"peak":10000.,"killed":False}
    after,row=advance(book,{"QLD":.98},{"QLD":{"open":50.,"close":50.,"split":2.,"dividend":1.}},session="2026-11-12",cost_bps=0)
    assert row["equity"]==pytest.approx(10200)
    assert row["return"]==pytest.approx(.02)
    assert not after["killed"]


def reported_event():
    state=pead.initial_state()
    row={"scheduled_at":"2026-11-12T16:10:00-05:00","estimate":1.,"actual":None}
    pead.observe_earnings(state,"AAA",[row],observed_at="2026-11-11T15:00:00+00:00")
    pead.observe_earnings(state,"AAA",[{**row,"actual":1.2}],observed_at="2026-11-12T21:20:00+00:00")
    frames={"AAA":pd.DataFrame({"Close":[100.,104.]},index=pd.to_datetime(["2026-11-12","2026-11-13"]))}
    pead.confirm_reactions(state,frames,at="2026-11-13T21:10:00+00:00")
    return state


def test_historical_eps_cannot_invent_a_prospective_trade():
    state=pead.initial_state()
    pead.observe_earnings(state,"AAA",[{"scheduled_at":"2026-11-10T16:00:00-05:00","estimate":1.,"actual":1.2}],observed_at="2026-11-11T15:00:00+00:00")
    assert next(iter(state["events"].values()))["status"]=="RETROSPECTIVE_ONLY"
    assert not state["positions"]


def test_pead_waits_for_complete_reaction_and_next_session_quote():
    state=reported_event();event=next(iter(state["events"].values()))
    assert event["reaction_session"]=="2026-11-13"
    assert event["entry_session"]=="2026-11-16"
    at="2026-11-16T14:40:00+00:00"
    pead.begin_day(state,{},at=at)
    quote=lambda s:{"bid":104.,"ask":104.1,"at":at,"feed":"iex"}
    pead.observe_quotes(state,quote,at=at)
    first=state["cash"]
    pead.observe_quotes(state,quote,at=at)
    assert state["cash"]==first and len(state["positions"])==1
    assert len(state["closed"])==0


def test_pead_missing_quote_cannot_be_recorded_as_success():
    state=reported_event();at="2026-11-16T14:40:00+00:00"
    pead.begin_day(state,{},at=at)
    with pytest.raises(ValueError,match="stale"):
        pead.observe_quotes(state,lambda s:{"bid":104.,"ask":104.1,"at":"2026-11-16T14:30:00+00:00"},at=at)
    assert state["cash"]==pead.INITIAL and not state["positions"]


def test_pead_failed_second_asset_cannot_double_credit_first_dividend():
    state=pead.initial_state()
    state["days"]["2026-11-12"]={}
    state["positions"]={s:{"qty":10.,"income":0} for s in ("AAA","BBB")}
    frames={"AAA":pd.DataFrame({"Close":[100.],"Dividends":[1.]},index=pd.to_datetime(["2026-11-13"]))}
    with pytest.raises(KeyError):pead.begin_day(state,frames,at="2026-11-13T15:00:00+00:00")
    assert state["cash"]==pead.INITIAL
    assert state["positions"]["AAA"]["income"]==0


def test_pead_held_book_gap_requires_reconciliation():
    state=pead.initial_state();state["days"]["2026-11-11"]={}
    state["positions"]={"AAA":{"qty":10.,"income":0}}
    with pytest.raises(ValueError,match="gap"):
        pead.begin_day(state,{},at="2026-11-13T15:00:00+00:00")


def make_cycle(tmp_path):
    from trading_bot.broker.replay import replay_cycle
    from trading_bot.live.executor import ExecutorState
    from trading_bot.portfolio_policy import default_policy
    idx=pd.bdate_range("2025-01-01",periods=260)
    frames={s:pd.DataFrame({"open":np.linspace(100,130,len(idx)),"close":np.linspace(100,130,len(idx)),
                           "high":np.linspace(101,131,len(idx)),"low":np.linspace(99,129,len(idx)),"volume":100000},index=idx)
            for s in ("QQQ","QLD","DBMF","KMLM","BIL")}
    snapshot={"cash":100000,"last_equity":100000,"positions":{},"state":asdict(ExecutorState(peak_equity=100000,day_start_equity=100000)),
        "at":(idx[-1]+pd.Timedelta(days=1,hours=15)).isoformat()+"+00:00","signal_session":str(idx[-1].date()),"champions":{},
        "allocations":{"weights":{"regime":.7},"verdicts":{"regime":"HEALTHY"}},"policy":default_policy()}
    replay_cycle(snapshot,frames,{s:130 for s in frames},capture_to=tmp_path)
    return next((tmp_path/"production_cycles").iterdir())


def test_actual_orchestrator_requests_and_state_replay_exactly(tmp_path):
    from trading_bot.broker.replay import verify_recorded_cycle
    folder=make_cycle(tmp_path)
    result=verify_recorded_cycle(folder)
    assert result["status"]=="MATCHED", result
    assert result["order_requests_verified"]>0
    assert result["verified_calls"]==result["recorded_calls"]
    assert result["network_access"] is False


def test_replay_detects_changed_decisions_instead_of_accepting_new_fills(tmp_path):
    from trading_bot.broker.replay import verify_recorded_cycle
    folder=make_cycle(tmp_path)
    metadata=folder/"metadata.json";meta=json.loads(metadata.read_text())
    meta["arguments"]["max_leverage"]=.3
    metadata.write_text(json.dumps(meta))
    integrity=json.loads((folder/"integrity.json").read_text())
    integrity["metadata.json"]=hashlib.sha256(metadata.read_bytes()).hexdigest()
    (folder/"integrity.json").write_text(json.dumps(integrity))
    result=verify_recorded_cycle(folder)
    assert result["status"]=="MISMATCH"


def test_tampered_broker_tape_cannot_count_as_evidence(tmp_path):
    from trading_bot.broker.replay import verify_recorded_cycle
    from trading_bot.broker.evidence import ReplayMismatch
    folder=make_cycle(tmp_path)
    with (folder/"broker.jsonl").open("a") as f:f.write('{}\n')
    with pytest.raises(ReplayMismatch,match="altered"):
        verify_recorded_cycle(folder)
