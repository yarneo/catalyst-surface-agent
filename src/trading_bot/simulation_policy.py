"""Explicit experiment overrides, reachable only with an in-memory broker."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from .pead_shadow import HOLD_SESSIONS, validate_quote, instant
from .sessions import calendar, session_bounds
from datetime import date, timedelta


def require_memory(broker):
    from .broker.replay import MemoryClient
    from .broker.evidence import RecordingClient
    client=broker._client
    if isinstance(client,RecordingClient):client=client.client
    if not isinstance(client,MemoryClient):
        raise RuntimeError("experimental policies require the isolated memory broker")


def configure_experiment(stack,runner,broker,spec,frames,pead_state,pead_inputs,moment):
    require_memory(broker)
    from . import ownership, marketdata
    from .portfolio_research import regime_states
    from .shadow_regime import propose_shadow_targets
    original=runner.RegimeExecutor
    if spec.cap!="production_70" or spec.band or spec.confirm!=1 or spec.cooldown or spec.recovery!=1:
        def regime(**kwargs):
            instance=original(**kwargs)
            close=marketdata.completed_bars(frames[instance.filter_symbol],instance.filter_symbol)["close"]
            sma=close.rolling(instance.sma_n).mean()
            active=bool(regime_states(close,sma,spec)[-1])
            rv=marketdata.realized_vol(instance.filter_symbol,instance.vol_window)
            scale=min(1.,instance.target_vol/rv) if instance.target_vol else 1.
            targets=propose_shadow_targets(live_allocation=instance.allocation,vol_scale=scale,
                realized_vol=rv*2,regime_on=active,health_verdict=instance.health_verdict)
            instance.allocation=targets[spec.cap]
            instance.target_vol=None
            instance._filter_state=lambda:(active,float(close.iloc[-1]),float(sma.iloc[-1]))
            return instance
        stack.enter_context(patch.object(runner,"RegimeExecutor",regime))
    if spec.pead_weight:
        original_sleeves=ownership.build_sleeves
        def sleeves(enabled=None):
            return [replace(s,max_sleeve_weight=spec.pead_weight,
                    max_position_weight=spec.pead_weight/2) if s.name=="pead" else s
                    for s in original_sleeves(enabled)]
        stack.enter_context(patch.object(ownership,"build_sleeves",sleeves))
        stack.enter_context(patch.object(runner,"build_sleeves",sleeves))
        # The ordinary PEAD executor is never invoked by research. The adapter
        # below uses only timestamped collector inputs and the virtual broker.
        stack.enter_context(patch.object(runner,"PeadExecutor",lambda **kw:SimpleNamespace(run=lambda:{"actions":[]})))
        original_policy=runner.load_policy
        def reserve_policy():
            policy=deepcopy(original_policy())
            acct=broker.get_account()
            names=next(s.symbols for s in sleeves() if s.name=="pead")
            deployed=sum(float(p.market_value) for p in broker.get_positions() if p.symbol in names)
            reserve=max(0.,spec.pead_weight-deployed/float(acct.portfolio_value))
            policy["config"]["bil_buffer"]+=reserve
            return policy
        stack.enter_context(patch.object(runner,"load_policy",reserve_policy))


def execute_pead(broker,state,inputs,weight,moment):
    """Fund stock entries from the common portfolio; no standalone P&L injection."""
    require_memory(broker)
    if inputs is None:raise ValueError("prospective PEAD inputs missing")
    events=inputs["events"]
    day=moment.date().isoformat()
    frozen=instant(inputs["frozen_at"])
    if not instant(inputs["collected_at"])<=frozen<moment:raise ValueError("PEAD inputs were not observed before execution")
    positions={p.symbol:p for p in broker.get_positions()}
    # Release only the PEAD reserve; the ordinary BIL/cash floors still apply.
    broker.bil_reserve=float(inputs["base_bil_buffer"])
    cal=calendar(moment.year)
    for symbol,p in list(state["positions"].items()):
        if symbol not in positions:continue
        held=len(cal.sessions_in_range(p["entry_session"],day))-1
        if held>=HOLD_SESSIONS:
            validate_quote(broker.quote(symbol),at=moment.isoformat())
            broker.close_position(symbol,sleeve="pead")
    positions={p.symbol:p for p in broker.get_positions()}
    held={s for s in state["positions"] if s in positions and float(positions[s].qty)>0}
    for event_id,event in sorted(events.items()):
        if len(held)>=2:break
        if (event_id in state["consumed_events"] or event["status"]!="PREQUALIFIED"
                or event.get("entry_session")!=day or event.get("revision_detected")):
            continue
        symbol=event["symbol"]
        # A just-closed event is reconciled after this cycle; do not overwrite
        # its cost basis with another event for the same symbol.
        if symbol in state["positions"]:continue
        estimate=event.get("estimate_used",{})
        reported=event.get("reported",{})
        reaction=date.fromisoformat(event["reaction_session"])
        reaction_close=session_bounds(reaction)[1]
        if not (instant(estimate["observed_at"])<instant(reported["first_observed_at"])
                <=instant(event["signal_observed_at"])<=frozen<moment
                and reaction_close+timedelta(minutes=5)<=instant(event["signal_observed_at"])
                and str(cal.next_session(str(reaction)).date())==day
                and event.get("surprise",0)>0 and event.get("reaction_return",0)>0):
            raise ValueError("PEAD event lacks ordered prospective evidence")
        quote=broker.quote(symbol)
        _,ask=validate_quote(quote,at=moment.isoformat())
        equity=float(broker.get_account().portfolio_value)
        deployed=sum(float(p.market_value) for p in broker.get_positions() if p.symbol in held)
        budget=min(equity*weight/2,max(0.,equity*weight-deployed))
        if budget<1:continue
        order=broker.submit_market_order(symbol,budget/(ask*1.005),"buy",sleeve="pead",reference_price=ask)
        state["positions"][symbol]={"event_id":event_id,"entry_session":day,"entry_at":moment.isoformat(),
            "entry_price":float(order.filled_avg_price),"qty":float(order.filled_qty),"income":0.,"realized_pnl":0.}
        state["consumed_events"].append(event_id)
        held.add(symbol)


def reconcile_pead(state,orders,positions,at):
    """Include risk-guard trims/exits in the same event's realized outcome."""
    for order in orders:
        p=state["positions"].get(order["symbol"])
        if p is not None and order["side"]=="sell":
            p["realized_pnl"]+=float(order["filled_qty"])*(float(order["filled_avg_price"])-p["entry_price"])
    for symbol,p in list(state["positions"].items()):
        qty=float(positions.get(symbol,{}).get("qty",0.))
        if qty<=1e-8:
            state["closed"].append({**p,"symbol":symbol,"exit_at":at,"pnl":p["realized_pnl"]+p["income"]})
            del state["positions"][symbol]
        else:p["qty"]=qty
