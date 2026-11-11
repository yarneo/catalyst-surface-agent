"""Order-construction tests for the guard's execution path.

``plan_cycle`` decides *what* to de-risk; ``_apply_guard_actions`` turns those
decisions into broker calls. That translation is where quantity maths and
Alpaca's per-endpoint symbol formats live, so a bug here places a wrong order
rather than merely reaching a wrong conclusion.

Uses a fake broker that records calls instead of sending them.

Run: uv run python -m pytest tests/ -q
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from trading_bot.risk import GuardAction, GuardResult

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_portfolio.py"


def _load_script():
    """scripts/ isn't a package, so load the module from its path."""
    spec = importlib.util.spec_from_file_location("run_portfolio", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rp = _load_script()


class _FakePosition:
    def __init__(self, price):
        self.current_price = price


class _FakeClient:
    def __init__(self, prices):
        self._prices = prices
        self.closed: list[str] = []

    def close_position(self, symbol):
        self.closed.append(symbol)

    def get_open_position(self, symbol):
        return _FakePosition(self._prices[symbol])


class FakeBroker:
    def __init__(self, prices: dict[str, float]):
        self._client = _FakeClient(prices)
        self.equity_orders: list[tuple] = []
        self.crypto_orders: list[tuple] = []

    def submit_market_order(self, symbol, qty, side, **_):
        self.equity_orders.append((symbol, qty, side))

    def submit_crypto_market_order(self, symbol, qty, side):
        self.crypto_orders.append((symbol, qty, side))

    @property
    def closed(self):
        return self._client.closed


def _result(*actions) -> GuardResult:
    return GuardResult(
        equity=100_000, peak_equity=100_000, day_start_equity=100_000,
        drawdown=0.0, daily_pnl=0.0, actions=list(actions),
    )


def _trim(symbol, current, target):
    return GuardAction(symbol=symbol, action="trim", reason="cap",
                       current_value=current, target_value=target)


def _close(symbol, current):
    return GuardAction(symbol=symbol, action="stop_loss", reason="stop",
                       current_value=current, target_value=0.0)


# ------------------------------------------------------------ sells only


def test_guard_never_buys():
    """The guard is a de-risking device; an action that would increase a
    position must be ignored, not executed."""
    broker = FakeBroker({"SPY": 100.0})
    grow = GuardAction(symbol="SPY", action="trim", reason="x",
                       current_value=10_000, target_value=20_000)
    acted = rp._apply_guard_actions(broker, _result(grow), live=True)
    assert acted == 0
    assert broker.equity_orders == [] and broker.closed == []


# --------------------------------------------------------- quantity maths


def test_equity_trim_can_sell_fractional_shares():
    broker = FakeBroker({"QLD": 83.67})
    # $11,386 to sell at $83.67 -> 136.08 shares -> 136 whole shares
    acted = rp._apply_guard_actions(
        broker, _result(_trim("QLD", 30_337, 18_951)), live=True)
    assert broker.equity_orders[0][0] == "QLD"
    assert broker.equity_orders[0][1] == pytest.approx(11386 / 83.67)
    assert acted == 1


def test_sub_one_share_trim_reduces_risk():
    broker = FakeBroker({"MDY": 689.0})
    acted = rp._apply_guard_actions(
        broker, _result(_trim("MDY", 10_000, 9_600)), live=True)   # $400 < 1 share
    assert broker.equity_orders[0][1] == pytest.approx(400 / 689)
    assert acted == 1


def test_zero_target_closes_rather_than_trims():
    broker = FakeBroker({"XLI": 180.0})
    rp._apply_guard_actions(broker, _result(_close("XLI", 3_480)), live=True)
    assert broker.closed == ["XLI"]
    assert broker.equity_orders == []


# ------------------------------------------------- alpaca symbol formats


def test_crypto_trim_uses_the_order_symbol_not_the_position_symbol():
    """Alpaca positions are 'BTCUSD' but orders must be 'BTC/USD'; sending the
    position form is rejected."""
    broker = FakeBroker({"BTCUSD": 63_500.0})
    rp._apply_guard_actions(
        broker, _result(_trim("BTCUSD", 10_000, 4_000)), live=False,
        crypto_symbols=frozenset({"BTCUSD"}), crypto_live=True,
        crypto_order_symbols={"BTCUSD": "BTC/USD"},
    )
    assert broker.crypto_orders and broker.crypto_orders[0][0] == "BTC/USD"
    assert broker.equity_orders == []


def test_crypto_trim_is_fractional_not_whole_units():
    broker = FakeBroker({"BTCUSD": 63_500.0})
    rp._apply_guard_actions(
        broker, _result(_trim("BTCUSD", 10_000, 4_000)), live=False,
        crypto_symbols=frozenset({"BTCUSD"}), crypto_live=True,
        crypto_order_symbols={"BTCUSD": "BTC/USD"},
    )
    qty = broker.crypto_orders[0][1]
    assert 0 < qty < 1                      # $6,000 of BTC is a fraction of a coin
    assert qty == pytest.approx(6_000 / 63_500, rel=1e-3)


def test_missing_crypto_mapping_does_not_send_a_malformed_order():
    broker = FakeBroker({"BTCUSD": 63_500.0})
    with pytest.raises(ValueError, match="no order symbol"):
        rp._apply_guard_actions(
            broker, _result(_trim("BTCUSD", 10_000, 4_000)), live=False,
            crypto_symbols=frozenset({"BTCUSD"}), crypto_live=True,
            crypto_order_symbols={},
        )
    assert broker.crypto_orders == [] and broker.equity_orders == []


# ------------------------------------------------------ live / dry-run gating


def test_dry_run_places_nothing():
    broker = FakeBroker({"QLD": 83.67})
    acted = rp._apply_guard_actions(
        broker, _result(_trim("QLD", 30_337, 18_951)), live=False)
    assert broker.equity_orders == [] and broker.closed == []
    assert acted == 1        # still reported, so sleeves defer in dry-run too


def test_crypto_trades_while_the_equity_market_is_closed():
    """A kill switch must still be able to liquidate crypto overnight."""
    broker = FakeBroker({"BTCUSD": 63_500.0, "SPY": 100.0})
    rp._apply_guard_actions(
        broker, _result(_close("BTCUSD", 5_000), _close("SPY", 10_000)),
        live=False,                                  # equity market shut
        crypto_symbols=frozenset({"BTCUSD"}), crypto_live=True,
        crypto_order_symbols={"BTCUSD": "BTC/USD"},
    )
    assert broker.closed == ["BTCUSD"]               # crypto closed
    assert "SPY" not in broker.closed                # equity deferred


def test_crypto_flag_does_not_leak_onto_equity_symbols():
    """Regression: `live` was reassigned inside the loop, so after one crypto
    symbol every later equity symbol inherited the crypto liveness flag."""
    broker = FakeBroker({"BTCUSD": 63_500.0, "SPY": 100.0, "XLI": 180.0})
    rp._apply_guard_actions(
        broker,
        _result(_close("BTCUSD", 5_000), _close("SPY", 10_000), _close("XLI", 3_480)),
        live=False, crypto_symbols=frozenset({"BTCUSD"}), crypto_live=True,
        crypto_order_symbols={"BTCUSD": "BTC/USD"},
    )
    assert broker.closed == ["BTCUSD"]


# --------------------------------------------------------------- resilience


def test_a_failing_order_aborts_cycle_and_cannot_report_success():
    class Exploding(FakeBroker):
        def submit_market_order(self, symbol, qty, side, **kwargs):
            if symbol == "SPY":
                raise RuntimeError("broker rejected")
            super().submit_market_order(symbol, qty, side, **kwargs)

    broker = Exploding({"SPY": 100.0, "XLI": 180.0})
    with pytest.raises(RuntimeError, match="broker rejected"):
        rp._apply_guard_actions(
            broker, _result(_trim("SPY", 10_000, 5_000), _trim("XLI", 10_000, 5_000)),
            live=True,
        )
    assert broker.equity_orders == []
