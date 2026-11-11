from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import Mock
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from trading_bot.broker.orders import ExecutionFailure, OrderCoordinator
from trading_bot.cycle import SessionState, plan_cycle
from trading_bot.risk import RiskLimits
from trading_bot.ownership import Sleeve
from trading_bot.marketdata import PriceUnavailable, signal_session, completed_bars
from trading_bot.live.managed_futures_executor import ManagedFuturesExecutor
from trading_bot.runtime_state import execution_lock
from trading_bot.sessions import ET, session_bounds, last_completed_session


class Client:
    def __init__(self, statuses=("filled",)):
        self.statuses = list(statuses)
        self.submissions = []
        self.orders = {}

    def submit_order(self, req):
        self.submissions.append(req)
        order = NS(id=str(len(self.submissions)), status=self.statuses.pop(0),
                   filled_qty=req.qty, filled_avg_price="100", client_order_id=req.client_order_id)
        self.orders[req.client_order_id] = order
        return order

    def get_order_by_client_id(self, coid):
        return self.orders[coid]

    def get_order_by_id(self, oid):
        order = next(o for o in self.orders.values() if o.id == oid)
        if self.statuses:
            order.status = self.statuses.pop(0)
        return order


def request():
    return MarketOrderRequest(symbol="QQQ", qty=Decimal("1.123456789"),
                              side=OrderSide.SELL, time_in_force=TimeInForce.DAY)


def test_confirmed_fill_replayed_without_duplicate_submission(tmp_path):
    client = Client()
    a = OrderCoordinator(client, tmp_path / "orders.json", "day:open")
    a.execute(request(), sleeve="router", reference=100)
    # A new process, the same intent, and the broker's existing fill.
    b = OrderCoordinator(client, tmp_path / "orders.json", "day:open")
    b.execute(request(), sleeve="router", reference=100)
    assert len(client.submissions) == 1


def test_waits_for_terminal_fill_and_preserves_fractional_quantity(tmp_path):
    client = Client(("accepted", "partially_filled", "filled"))
    sleeps = []
    flow = OrderCoordinator(client, tmp_path / "orders.json", "day:open", sleep=sleeps.append)
    result = flow.execute(request(), sleeve="router", reference=100)
    assert sleeps == [1, 1]
    assert float(result.filled_qty) == pytest.approx(1.123456789)


@pytest.mark.parametrize("terminal", ["rejected", "canceled", "expired"])
def test_terminal_nonfills_are_failures(tmp_path, terminal):
    flow = OrderCoordinator(Client((terminal,)), tmp_path / "orders.json", "day:open")
    with pytest.raises(ExecutionFailure):
        flow.execute(request(), sleeve="router", reference=100)


def test_timeout_blocks_dependent_order(tmp_path):
    flow = OrderCoordinator(Client(("accepted",)), tmp_path / "orders.json", "day:open",
                            timeout=2, sleep=lambda _: None)
    with pytest.raises(ExecutionFailure, match="timed out"):
        flow.execute(request(), sleeve="router", reference=100)
    other = request(); other.symbol = "DBMF"
    with pytest.raises(ExecutionFailure, match="unresolved"):
        flow.execute(other, sleeve="router", reference=100)
    assert len(flow.client.submissions) == 1


def test_timeout_after_acceptance_recovers_by_durable_client_id(tmp_path):
    client = Client()
    submit = client.submit_order
    def ambiguous(req):
        submit(req)
        raise TimeoutError("response lost")
    client.submit_order = ambiguous
    flow = OrderCoordinator(client, tmp_path / "orders.json", "day:open")
    with pytest.raises(ExecutionFailure):
        flow.execute(request(), sleeve="router", reference=100)
    assert flow.reconcile() == []
    flow.execute(request(), sleeve="router", reference=100)
    assert len(client.submissions) == 1


def test_missing_and_partial_daily_bars_cannot_flip_regime():
    with signal_session(date(2026, 11, 10)):
        old = pd.DataFrame({"close": [100]}, index=pd.to_datetime(["2020-01-01"]))
        with pytest.raises(PriceUnavailable):
            completed_bars(old, "QQQ")
        partial = pd.DataFrame({"close": [100, 200]}, index=pd.to_datetime(["2026-11-10", "2026-11-11"]))
        assert completed_bars(partial, "QQQ").iloc[-1]["close"] == 100


def test_overnight_loss_is_included_in_daily_breaker():
    plan = plan_cycle(phase="open", today="2026-11-11", equity=96000, positions={},
        state=SessionState(peak_equity=100000, session_date="2026-11-10"),
        sleeves=[], limits=RiskLimits(), prior_close_equity=100000)
    assert plan.guard.daily_pnl == pytest.approx(-0.04)
    assert plan.guard.halt_trading and not plan.run_sleeves


def test_current_session_baseline_is_migrated_once():
    plan = plan_cycle(phase="risk", today="2026-11-11", equity=95000, positions={},
        state=SessionState(peak_equity=100000, session_date="2026-11-11", day_start_equity=96000),
        sleeves=[], limits=RiskLimits(), prior_close_equity=100000)
    assert plan.guard.daily_pnl == pytest.approx(-0.05)
    assert plan.state.baseline_source == "broker_prior_close"


def test_router_does_not_buy_after_failed_liquidation(monkeypatch):
    position = NS(symbol="QQQ", market_value=30000, current_price=600,
                  qty_available="50", qty="50")
    broker = NS(get_account=lambda: NS(portfolio_value=100000, cash=2000),
                get_positions=lambda: [position], close_position=Mock(side_effect=ExecutionFailure("sell failed")),
                submit_market_order=Mock(), reconcile_orders=Mock(), quote=Mock())
    router = ManagedFuturesExecutor(broker=broker, max_sleeve_weight=.6, live=True)
    monkeypatch.setattr(router, "_qqq_bull", lambda *a: False)
    with pytest.raises(ExecutionFailure):
        router.run()
    broker.submit_market_order.assert_not_called()


def test_overlapping_cycles_cannot_execute(tmp_path):
    with execution_lock(tmp_path / "lock"):
        with pytest.raises(RuntimeError, match="another portfolio"):
            with execution_lock(tmp_path / "lock"):
                pytest.fail("second writer acquired lock")


def test_holiday_and_early_close_calendar():
    assert session_bounds(date(2026, 11, 26)) is None
    assert session_bounds(date(2026, 11, 27))[1].hour == 13
    assert last_completed_session(datetime(2026, 11, 27, 13, 6, tzinfo=ET)) == date(2026, 11, 27)


def test_etf_obeys_guard_defer(monkeypatch):
    import sys
    spec = importlib.util.spec_from_file_location("guard_orchestration", Path(__file__).resolve().parents[1] / "scripts/run_portfolio.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    position = NS(symbol="XLE", market_value=65000, unrealized_plpc=.01)
    broker = NS(bind_cycle=lambda _: None, get_clock=lambda: NS(is_open=True),
                reconcile_orders=lambda: None, open_orders=lambda: [],
                get_account=lambda: NS(portfolio_value=100000, last_equity=100000),
                get_positions=lambda: [position])
    state = NS(peak_equity=100000, day_start_equity=100000, session_date="2026-11-11",
               halted_today=False, killed=False, baseline_source="broker_prior_close", save=lambda: None)
    sleeves = [Sleeve("etf", frozenset({"XLE"}), True, max_position_weight=.60),
               Sleeve("crypto", frozenset(), False)]
    etf = Mock()
    monkeypatch.setattr(mod, "AlpacaBroker", lambda: broker)
    monkeypatch.setattr(mod, "ExecutorState", NS(load=lambda **kw: state))
    monkeypatch.setattr(mod, "ChampionStore", lambda: NS(all=lambda: {"XLE": 1}, path=NS(exists=lambda: True)))
    monkeypatch.setattr(mod, "build_sleeves", lambda _: sleeves)
    monkeypatch.setattr(mod, "load_crypto_universe", lambda: [])
    monkeypatch.setattr(mod, "PaperExecutor", etf)
    monkeypatch.setattr(mod, "_apply_guard_actions", Mock())
    monkeypatch.setattr(sys, "argv", ["run_portfolio", "--phase", "open", "--live"])
    mod._run()
    etf.assert_not_called()
