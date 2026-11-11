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
from datetime import datetime, timezone
import io
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
from unittest.mock import patch

from .alpaca_client import AlpacaBroker
from .orders import OrderCoordinator


class MemoryClient:
    def __init__(self, snapshot, prices, *, cost_bps=5):
        self.cash = float(snapshot["cash"])
        self.last_equity = float(snapshot["last_equity"])
        self.prices, self.cost_bps = prices, cost_bps
        self.positions = deepcopy(snapshot.get("positions", {}))
        self.orders = {}

    def get_account(self):
        equity = self.cash + sum(float(p["qty"]) * self.prices[s] for s,p in self.positions.items())
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
        price = self.prices[symbol] * (1 + self.cost_bps/10000 * (1 if side=="buy" else -1))
        existing = self.positions.get(symbol, {"qty":0, "avg_entry":price})
        old_qty = float(existing["qty"])
        if side == "buy":
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
                   filled_avg_price=price)
        self.orders[request.client_order_id] = order
        return order


def replay_cycle(snapshot, frames, prices, *, cost_bps=5):
    """Replay one archived open; neither broker access nor private writes exist."""
    import importlib.util
    import sys
    import requests
    from .. import allocations, marketdata
    from ..learning import Champion
    from ..live.executor import ExecutorState, PaperExecutor
    from ..live import executor, regime_executor, managed_futures_executor, cash_park_executor
    from ..sessions import ET

    root = Path(__file__).resolve().parents[3]
    path = root / "scripts/run_portfolio.py"
    spec = importlib.util.spec_from_file_location("isolated_portfolio_replay",path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    state = ExecutorState(**snapshot["state"])
    moment = datetime.fromisoformat(snapshot["at"]).astimezone(ET)
    signal_day = datetime.fromisoformat(snapshot["signal_session"]).date()
    saved = []
    client = MemoryClient(snapshot,prices,cost_bps=cost_bps)
    champions = {s:Champion(**v) for s,v in snapshot["champions"].items()}
    def load_state(*args, **kwargs):
        return deepcopy(saved[-1] if saved else state)
    def save_state(value,*args,**kwargs):
        saved.append(deepcopy(value))
    def series(symbol, period="400d", min_bars=1):
        out = marketdata.completed_bars(frames[symbol], symbol)["close"]
        if len(out)<min_bars:
            raise marketdata.PriceUnavailable("replay history too short")
        return out
    def quote(symbol):
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
        stack.enter_context(patch.object(runner,"ChampionStore",lambda:NS(path=path, all=lambda:champions, get=champions.get)))
        stack.enter_context(patch.object(runner,"now_et",lambda:moment))
        stack.enter_context(patch.object(executor,"now_et",lambda:moment))
        stack.enter_context(patch.object(ExecutorState,"load",side_effect=load_state))
        stack.enter_context(patch.object(ExecutorState,"save",save_state))
        stack.enter_context(patch.object(PaperExecutor,"_recent_bars",lambda self,s:marketdata.completed_bars(frames[s],s)))
        stack.enter_context(patch.object(allocations,"read_allocations",lambda **kw:snapshot["allocations"]))
        stack.enter_context(patch.object(runner,"load_policy",lambda:snapshot["policy"]))
        stack.enter_context(patch.object(marketdata,"close_series",series))
        stack.enter_context(patch.object(executor,"append_ai_decision",lambda *a,**kw:None))
        stack.enter_context(patch.object(executor,"read_jsonl",lambda *a,**kw:[]))
        stack.enter_context(patch.object(regime_executor,"record_shadow_portfolios",lambda *a,**kw:{}))
        for module in (regime_executor,managed_futures_executor,cash_park_executor):
            stack.enter_context(patch.object(module,"send_telegram",lambda *a,**kw:None))
        stack.enter_context(patch("trading_bot.broker.alpaca_client.append_execution_intent",lambda *a,**kw:None))
        stack.enter_context(patch.object(runner,"atomic_json",lambda *a,**kw:None))
        stack.enter_context(patch.object(requests.sessions.Session,"request",side_effect=RuntimeError("network disabled in replay")))
        argv=[str(path),"--phase","open","--live","--no-enrich"]
        for name in ("stop_loss","daily_loss_limit","max_drawdown","max_position_weight","target_vol","max_leverage"):
            if name in snapshot.get("arguments", {}):
                argv += ["--"+name.replace("_","-"), str(snapshot["arguments"][name])]
        stack.enter_context(patch.object(sys,"argv",argv))
        with marketdata.signal_session(signal_day):
            runner._run()
    account=client.get_account()
    return {"orders":[vars(o) for o in client.orders.values()], "cash":account.cash,
            "equity":account.portfolio_value,"positions":client.positions,
            "state":asdict(saved[-1] if saved else state),
            "network_access":False,"order_authority":False,
            "engine":"production_orchestrator","fill_model":f"{cost_bps}bp modeled venue quotes"}
