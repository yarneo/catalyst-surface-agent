import numpy as np
import pandas as pd
from trading_bot.portfolio_research import Experiment, forward_gate, simulate, target_weights
from trading_bot.portfolio_policy import DEFAULT_CONFIG


def test_whole_portfolio_weights_keep_reserves_and_caps():
    w = target_weights(regime_on=True,qqq_vol=.18,verdict="HEALTHY", etf_targets={},spec=Experiment("x"),policy=DEFAULT_CONFIG)
    assert w["QLD"] == .7
    assert w.get("QQQ",0) == 0  # QLD concentration guard
    assert sum(w.values()) <= .980001
    assert w["BIL"] >= .099999


def test_live_health_block_applies_to_all_risk_budget_variants():
    for name in ("production_70","cap_50","cap_60","stress_70","risk_cap_25"):
        w = target_weights(regime_on=True,qqq_vol=.18,verdict="FAST_TRIPPED",etf_targets={},spec=Experiment(name,cap=name),policy=DEFAULT_CONFIG)
        assert w["QLD"] == 0


def test_research_is_causal_and_forward_starts_with_new_capital():
    idx = pd.bdate_range("2025-01-01",periods=230)
    prices = pd.DataFrame({s: np.linspace(100,130,len(idx)) for s in ("QQQ","QLD","DBMF","KMLM","BIL")},index=idx)
    health = pd.Series("HEALTHY",index=idx)
    args=(pd.DataFrame(index=idx),health,Experiment("x"),DEFAULT_CONFIG)
    first=simulate(prices,prices,*args)
    changed=prices.copy()
    changed.iloc[-1]*=.8
    second=simulate(changed,changed,*args)
    assert first[:-1] == second[:-1]
    forward=simulate(prices,prices,*args,start_after=str(idx[-2].date()))
    assert len(forward)==1
    assert forward[0]["equity"] < 100000  # first fill pays cost; no invented earlier profit
    assert forward[0]["cash"] >= 1990


def test_missing_pead_data_cannot_look_like_a_valid_zero_return_strategy():
    assert simulate(None,pd.DataFrame(),None,None,Experiment("pead",pead_weight=.1),DEFAULT_CONFIG) is None


def test_even_profitable_history_cannot_bypass_parity_gate():
    baseline=[{"date":str(i),"return":0,"cost":0,"turnover":0} for i in range(100)]
    candidate=[{**r,"return":.001} for r in baseline]
    gate=forward_gate(candidate,baseline,parity={"passed":False,"blockers":["missing production replay"]})
    assert not gate["eligible"]
    assert "missing production replay" in gate["reasons"]


def test_production_orchestrator_replay_cannot_access_network_or_private_state():
    from trading_bot.broker.replay import replay_cycle
    from trading_bot.live.executor import ExecutorState
    from dataclasses import asdict
    from trading_bot.portfolio_policy import default_policy
    idx=pd.bdate_range("2025-01-01",periods=260)
    frames={s:pd.DataFrame({"close":np.linspace(100,130,len(idx)),
                           "open":np.linspace(100,130,len(idx)),"volume":100000,
                           "high":np.linspace(101,131,len(idx)),"low":np.linspace(99,129,len(idx))},index=idx)
            for s in ("QQQ","QLD","DBMF","KMLM","BIL")}
    snapshot={"cash":100000,"last_equity":100000,"positions":{},
              "state":asdict(ExecutorState(peak_equity=100000,day_start_equity=100000)),
              "at":(idx[-1]+pd.Timedelta(days=1,hours=15)).isoformat()+"+00:00",
              "signal_session":str(idx[-1].date()),"champions":{},
              "allocations":{"weights":{"regime":.7},"verdicts":{"regime":"HEALTHY"}},
              "policy":default_policy()}
    result=replay_cycle(snapshot,frames,{s:130 for s in frames})
    assert result["network_access"] is False
    assert result["order_authority"] is False
    assert result["cash"] >= 1990
    assert result["positions"]["QLD"]["qty"] > 0
    assert result["orders"]
