"""Isolated replay of the real portfolio orchestrator with an in-memory broker.

The production decision code is executed unchanged. Every external boundary
(broker, data, state writes and notifications) is replaced inside one dedicated
research process. Use archived inputs to establish parity; daily-bar proxy
returns alone never count as a matching production replay.
"""
from __future__ import annotations
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import io
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
from unittest.mock import patch

from .alpaca_client import AlpacaBroker
from .orders import OrderCoordinator


class MemoryClient:
    def __init__(self, snapshot, prices, *, cost_bps=5, quotes=None, fills=None, require_quotes=False):
        self.cash = float(snapshot["cash"])
        self.last_equity = float(snapshot["last_equity"])
        self.prices, self.cost_bps = prices, cost_bps
        self.positions = deepcopy(snapshot.get("positions", {}))
        self.orders = {}
        self.quotes = quotes or {}
        self.fills = fills
        self.fill_index = 0
        self.require_quotes = require_quotes

    def get_account(self):
        equity = self.cash + sum(float(p["qty"]) * self.prices[s] for s,p in self.positions.items() if float(p["qty"])>1e-8)
        return NS(cash=self.cash, equity=equity, portfolio_value=equity,
                  last_equity=self.last_equity, buying_power=max(0,self.cash))

    def get_open_position(self, symbol):
        row = self.positions[symbol]
        qty, entry, price = float(row["qty"]), float(row["avg_entry"]), self.prices[symbol]
        return NS(symbol=symbol, qty=str(qty), qty_available=str(qty), current_price=price,
                  market_value=qty*price, avg_entry_price=entry,
                  unrealized_plpc=price/entry-1 if entry else 0)

    def get_all_positions(self):
        return [self.get_open_position(s) for s,p in self.positions.items() if float(p["qty"])>1e-8]

    def get_clock(self):
        return NS(is_open=True)

    def get_orders(self, **kwargs):
        return []  # virtual orders fill synchronously

    def get_order_by_id(self, identifier):
        return next(o for o in self.orders.values() if o.id == identifier)

    def get_order_by_client_id(self, identifier):
        return self.orders[identifier]

    def submit_order(self, request):
        if request.client_order_id in self.orders:
            return self.orders[request.client_order_id]
        symbol, qty = request.symbol, float(request.qty)
        side = str(getattr(request.side,"value",request.side))
        quote = self.quotes.get(symbol, {})
        if self.require_quotes and not quote:
            raise RuntimeError("simulated fill requires a fresh observed quote")
        reference = float(quote.get("ask" if side=="buy" else "bid", self.prices[symbol]))
        price = reference * (1 + self.cost_bps/10000 * (1 if side=="buy" else -1))
        if self.fills is not None:
            if self.fill_index >= len(self.fills):
                raise RuntimeError("simulator requested an extra order")
            fill = self.fills[self.fill_index]
            self.fill_index += 1
            if (symbol,side) != (fill["symbol"],fill["side"]):
                raise RuntimeError("simulator order differs from the observed request")
            price = fill["price"]
            # Preserve the observed terminal fill fraction, including canceled
            # partial fills. The decision quantity is still computed here.
            qty *= fill["filled_qty"] / fill["qty"]
        existing = self.positions.get(symbol, {"qty":0, "avg_entry":price})
        old_qty = float(existing["qty"])
        if qty <= 0:
            pass
        elif side == "buy":
            if qty*price > self.cash+.001:
                raise RuntimeError("virtual purchase exceeded cash")
            self.cash -= qty*price
            self.positions[symbol] = {"qty":old_qty+qty,
                "avg_entry":(old_qty*float(existing["avg_entry"])+qty*price)/(old_qty+qty)}
        else:
            if qty > old_qty+1e-8:
                raise RuntimeError("virtual short sale attempted")
            self.cash += qty*price
            self.positions[symbol] = {"qty":max(0,old_qty-qty), "avg_entry":existing["avg_entry"]}
        order = NS(id=request.client_order_id, client_order_id=request.client_order_id,
                   symbol=symbol, side=side, status="filled", filled_qty=qty,
                   filled_avg_price=price, qty=float(request.qty))
        if self.fills is not None:
            order.status = fill.get("status", "filled")
        self.orders[request.client_order_id] = order
        return order


@lru_cache(maxsize=2)
def _runner(name):
    import importlib.util
    path = Path(__file__).resolve().parents[3] / "scripts/run_portfolio.py"
    spec = importlib.util.spec_from_file_location(name, path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def replay_cycle(snapshot, frames, prices, *, cost_bps=5, capture_to=None,
                 quotes=None, fills=None, experiment=None, pead_inputs=None,
                 phase="open", externals=None, allow_failure=False):
    """Replay one archived open; neither broker access nor private writes exist."""
    import importlib.util
    import sys
    import requests
    import socket
    from .. import allocations, marketdata, config, learning, ownership
    from ..learning import Champion
    from ..live.executor import ExecutorState, PaperExecutor
    from ..live import executor, regime_executor, managed_futures_executor, cash_park_executor
    from ..sessions import ET
    from . import evidence

    root = Path(__file__).resolve().parents[3]
    path = root / "scripts/run_portfolio.py"
    runner = _runner("isolated_portfolio_replay")
    state = ExecutorState(**snapshot["state"])
    moment = datetime.fromisoformat(snapshot["at"]).astimezone(ET)
    signal_day = datetime.fromisoformat(snapshot["signal_session"]).date()
    saved = []
    failure = None
    client = MemoryClient(snapshot,prices,cost_bps=cost_bps,fills=fills,require_quotes=quotes is not None and fills is None)
    quote_indices={}
    pead_state = deepcopy(snapshot.get("pead_state", {"positions":{},"closed":[],"consumed_events":[]}))
    champions = {s:Champion(**v) for s,v in snapshot["champions"].items()}
    def load_state(*args, **kwargs):
        return deepcopy(saved[-1] if saved else state)
    def save_state(value,*args,**kwargs):
        saved.append(deepcopy(value))
        evidence.capture_inputs(lambda: {"final_state":asdict(value)})
    def series(symbol, period="400d", min_bars=1):
        out = marketdata.completed_bars(frames[symbol], symbol)["close"]
        if len(out)<min_bars:
            raise marketdata.PriceUnavailable("replay history too short")
        return out
    def quote(symbol):
        if quotes is not None:
            if symbol not in quotes:raise RuntimeError(symbol+": observed quote is missing")
            value=quotes[symbol]
            if isinstance(value,list):
                index=quote_indices.get(symbol,0)
                if index>=len(value):raise RuntimeError("extra quote request for "+symbol)
                quote_indices[symbol]=index+1
                value=value[index]
            from ..market_tape import validate_quote
            validate_quote(value,at=snapshot["at"])
            client.quotes[symbol]=value
            if "bid" in value and "ask" in value:
                client.prices[symbol]=(float(value["bid"])+float(value["ask"]))/2
            return deepcopy(value)
        p=prices[symbol]
        return {"bid":p*(1-cost_bps/10000),"ask":p*(1+cost_bps/10000),
                "at":snapshot["at"],"feed":"modeled_replay"}
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack, redirect_stdout(io.StringIO()):
        broker=AlpacaBroker.__new__(AlpacaBroker)
        broker._client=client
        broker.orders=OrderCoordinator(client,Path(tmp)/"orders.json","replay")
        broker.cash_reserve,broker.bil_reserve,broker.max_leverage=.02,.10,1.
        broker.quote=quote
        stack.enter_context(patch.object(runner,"AlpacaBroker",lambda:broker))
        store=lambda:NS(path=path, all=lambda:champions, get=champions.get)
        stack.enter_context(patch.object(runner,"ChampionStore",store))
        stack.enter_context(patch.object(learning,"ChampionStore",store))
        if "config" in snapshot:
            stack.enter_context(patch.object(config,"load_config",lambda:deepcopy(snapshot["config"])))
        stack.enter_context(patch.object(runner,"now_et",lambda:moment))
        stack.enter_context(patch.object(runner,"last_completed_session",lambda:signal_day))
        stack.enter_context(patch.object(executor,"now_et",lambda:moment))
        stack.enter_context(patch.object(ExecutorState,"load",side_effect=load_state))
        stack.enter_context(patch.object(ExecutorState,"save",save_state))
        stack.enter_context(patch.object(PaperExecutor,"_recent_bars",lambda self,s:marketdata.completed_bars(
            frames[s].loc[str(moment.date()-timedelta(days=self.lookback_days)):],s)))
        stack.enter_context(patch.object(allocations,"read_allocations",lambda **kw:snapshot["allocations"]))
        stack.enter_context(patch.object(runner,"load_policy",lambda:snapshot["policy"]))
        stack.enter_context(patch.object(marketdata,"close_series",series))
        stack.enter_context(patch.object(executor,"append_ai_decision",lambda *a,**kw:None))
        stack.enter_context(patch.object(executor,"read_jsonl",lambda *a,**kw:[]))
        stack.enter_context(patch.object(regime_executor,"record_shadow_portfolios",lambda *a,**kw:{}))
        if externals:
            from ..signals.overlay import SignalOverlay
            from .evidence import decode
            overlay={r["args"][0]:decode(r["result"]) for r in externals if r["method"]=="overlay" and "result" in r}
            authority=next((decode(r["result"]) for r in externals if r["method"]=="ai_authority" and "result" in r),None)
            if overlay:stack.enter_context(patch.object(SignalOverlay,"apply",lambda self,s,w:deepcopy(overlay[s])))
            if authority:stack.enter_context(patch.object(executor,"ai_authority_gate",lambda *a,**kw:deepcopy(authority)))
        if experiment is not None:
            from ..simulation_policy import configure_experiment
            configure_experiment(stack,runner,broker,experiment,frames,pead_state,pead_inputs,moment)
        for module in (regime_executor,managed_futures_executor,cash_park_executor):
            stack.enter_context(patch.object(module,"send_telegram",lambda *a,**kw:None))
        stack.enter_context(patch("trading_bot.broker.alpaca_client.append_execution_intent",lambda *a,**kw:None))
        stack.enter_context(patch.object(runner,"atomic_json",lambda *a,**kw:None))
        stack.enter_context(patch.object(requests.sessions.Session,"request",side_effect=RuntimeError("network disabled in replay")))
        stack.enter_context(patch.object(socket.socket,"connect",side_effect=RuntimeError("network disabled in replay")))
        argv=[str(path),"--phase", "risk" if phase=="pead" else phase,"--live"]
        if not externals or snapshot.get("arguments",{}).get("no_enrich",True):argv.append("--no-enrich")
        if experiment is not None and not experiment.etf:argv += ["--disable","etf"]
        if experiment is not None and experiment.pead_weight:argv += ["--enable","pead"]
        for name in ("enable","disable"):
            selected=snapshot.get("arguments",{}).get(name,[])
            if selected:argv += ["--"+name,*selected]
        for name in ("skip_guard","wind_down_disabled"):
            if snapshot.get("arguments",{}).get(name):argv.append("--"+name.replace("_","-"))
        for name in ("stop_loss","daily_loss_limit","max_drawdown","max_position_weight","target_vol","max_leverage"):
            if name in snapshot.get("arguments", {}):
                argv += ["--"+name.replace("_","-"), str(snapshot["arguments"][name])]
        stack.enter_context(patch.object(sys,"argv",argv))
        if capture_to is not None:
            stack.enter_context(patch.object(evidence,"DATA",Path(capture_to)))
        with marketdata.signal_session(signal_day):
            with evidence.capture_cycle(capture_to is not None,root):
                try:
                    runner._run()
                except Exception as exc:
                    failure = type(exc).__name__
                    if not allow_failure:
                        if capture_to is not None:evidence.finish_capture()
                        raise
                current_state = saved[-1] if saved else state
                if (phase=="pead" and failure is None and not current_state.halted_today
                        and not current_state.killed and not client.orders):
                    from ..simulation_policy import execute_pead
                    broker.bind_cycle(moment.date().isoformat()+":pead")
                    execute_pead(broker,pead_state,pead_inputs,experiment.pead_weight,moment)
                if capture_to is not None:
                    evidence.finish_capture()
    account=client.get_account()
    return {"orders":[vars(o) for o in client.orders.values()], "cash":account.cash,
            "equity":account.portfolio_value,"positions":client.positions,
            "state":asdict(saved[-1] if saved else state),"pead_state":pead_state,"error_type":failure,
            "network_access":False,"order_authority":False,
            "engine":"production_orchestrator","fill_model":f"{cost_bps}bp modeled venue quotes"}


def verify_recorded_cycle(folder):
    """Re-run production against its exact broker reads, quotes and responses.

    Requests must match in sequence and quantity. Nothing is submitted, and a
    no-trade replay is explicitly distinguished from a verified fill path.
    """
    import hashlib
    import importlib.util
    import json
    import socket
    import sys
    import pandas as pd
    import requests
    from .evidence import PlaybackClient, ReplayMismatch, engine_fingerprint
    from .. import allocations, config, learning, marketdata
    from ..live import executor, regime_executor, managed_futures_executor, cash_park_executor
    from ..live.executor import ExecutorState, PaperExecutor
    from ..sessions import ET
    from ..signals.overlay import SignalOverlay

    folder=Path(folder)
    required=("metadata.json","broker.jsonl","signals.json")
    integrity=json.loads((folder/"integrity.json").read_text())
    if any(name not in integrity or hashlib.sha256((folder/name).read_bytes()).hexdigest()!=integrity[name] for name in required):
        raise ReplayMismatch("incomplete or altered production evidence")
    meta=json.loads((folder/"metadata.json").read_text())
    if meta.get("status") not in {"COMPLETED","FAILED"} or "initial_state" not in meta:
        return {"status":"INCOMPLETE_CAPTURE","order_authority":False,"network_access":False}
    rows=[json.loads(line) for line in (folder/"broker.jsonl").read_text().splitlines()]
    frames={s:pd.DataFrame(raw["data"],columns=raw["columns"],index=pd.to_datetime(raw["index"]))
            for s,raw in json.loads((folder/"signals.json").read_text()).items()}
    root=Path(__file__).resolve().parents[3]
    path=root/"scripts/run_portfolio.py"
    spec=importlib.util.spec_from_file_location("recorded_portfolio_replay",path)
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    client=PlaybackClient(rows)
    initial=ExecutorState(**meta["initial_state"])
    saved=[]
    champions={s:learning.Champion(**v) for s,v in meta["champions"].items()}
    moment=datetime.fromisoformat(meta["at"]).astimezone(ET)
    signal_day=datetime.fromisoformat(meta["signal_session"]).date()
    def series(symbol,period="400d",min_bars=1):
        out=marketdata.completed_bars(frames[symbol],symbol)["close"]
        if len(out)<min_bars:raise ReplayMismatch("recorded signal window is too short")
        return out
    def save_state(value,*a,**kw):saved.append(deepcopy(value))
    def metadata(key):
        if key not in meta:raise ReplayMismatch("missing archived "+key)
        return deepcopy(meta[key])
    error=None
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack, redirect_stdout(io.StringIO()):
        journal=Path(tmp)/"orders.json";journal.write_text(json.dumps(meta["initial_journal"]))
        broker=AlpacaBroker.__new__(AlpacaBroker);broker._client=client
        broker.orders=OrderCoordinator(client,journal,"replay",sleep=lambda _:None)
        broker.cash_reserve,broker.bil_reserve,broker.max_leverage=.02,.10,1.
        broker.quote=lambda symbol:client.consume("quote",symbol)
        store=NS(path=path,all=lambda:champions,get=champions.get)
        patches=[
            (runner,"AlpacaBroker",lambda:broker), (runner,"ChampionStore",lambda:store),
            (learning,"ChampionStore",lambda:store), (runner,"now_et",lambda:moment),
            (runner,"last_completed_session",lambda:signal_day), (executor,"now_et",lambda:moment),
            (runner,"DATA",Path(tmp)), (config,"load_config",lambda:metadata("config")),
            (ExecutorState,"load",lambda *a,**k:deepcopy(saved[-1] if saved else initial)),
            (ExecutorState,"save",save_state),
            (PaperExecutor,"_recent_bars",lambda self,s:marketdata.completed_bars(frames[s],s)),
            (allocations,"read_allocations",lambda **kw:metadata("allocations")),
            (runner,"load_policy",lambda:metadata("policy")), (marketdata,"close_series",series),
            (SignalOverlay,"apply",lambda self,s,w:client.consume("overlay",s,w)),
            (executor,"ai_authority_gate",lambda *a,**kw:client.consume("ai_authority")),
            (executor,"read_jsonl",lambda *a,**kw:[]), (executor,"append_ai_decision",lambda *a,**kw:None),
            (regime_executor,"record_shadow_portfolios",lambda *a,**kw:{}),
            (runner,"atomic_json",lambda *a,**kw:None),
        ]
        for obj,name,value in patches:stack.enter_context(patch.object(obj,name,value))
        for module in (regime_executor,managed_futures_executor,cash_park_executor):
            stack.enter_context(patch.object(module,"send_telegram",lambda *a,**kw:None))
        stack.enter_context(patch("trading_bot.broker.alpaca_client.append_execution_intent",lambda *a,**kw:None))
        stack.enter_context(patch.object(requests.sessions.Session,"request",side_effect=RuntimeError("network disabled in replay")))
        stack.enter_context(patch.object(socket.socket,"connect",side_effect=RuntimeError("network disabled in replay")))
        argv=[str(path)]
        for name,value in meta["arguments"].items():
            option="--"+name.replace("_","-")
            if isinstance(value,bool):
                if value:argv.append(option)
            elif isinstance(value,list):
                if value:argv += [option,*map(str,value)]
            elif value is not None:argv += [option,str(value)]
        stack.enter_context(patch.object(sys,"argv",argv))
        try:
            try:
                with marketdata.signal_session(signal_day):runner._run()
            except BaseException as exc:
                error=exc
            if "final_account" in meta:
                broker.get_account()
                broker.get_positions()
            client.assert_finished()
        except BaseException as exc:
            error=exc
    same_engine=meta.get("engine_hash")==engine_fingerprint(root)
    actual=asdict(saved[-1] if saved else initial)
    expected=meta.get("final_state",meta["initial_state"])
    different=[k for k in expected if k!="updated_at" and actual.get(k)!=expected[k]]
    status="MATCHED" if error is None and not different and meta["status"]=="COMPLETED" else "MISMATCH"
    if (meta["status"]=="FAILED" and error is not None and type(error).__name__==meta.get("error_type")
            and not different and client.index==len(rows) and not isinstance(error,ReplayMismatch)):
        status="MATCHED_FAILED_EXECUTION"
    if status=="MATCHED" and not same_engine:status="MATCHED_CHANGED_ENGINE"
    return {"status":status,"session":str(moment.date()),"cycle":folder.name,
        "engine_hash":meta.get("engine_hash"),"same_engine":same_engine,
        "recorded_calls":len(rows),"verified_calls":client.index,
        "order_requests_verified":sum(r["method"]=="submit_order" for r in rows) if status.startswith("MATCHED") else 0,
        "risk_state_differences":different,"error_type":type(error).__name__ if error else None,
        "error":str(error)[:300] if error else None,"network_access":False,"order_authority":False,
        "scope":"decision requests, archived broker responses and risk state; not independent alpha or daily-proxy validation"}
