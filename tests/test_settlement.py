"""Tests for order settlement between sleeves.

Found on the fresh $100k account (2026-08-04): sleeves run sequentially and each
sizes itself from account state, but Alpaca's ``cash`` is the *settled* balance —
it does not drop when a buy is submitted, only when it fills. So every sleeve
allocated against the same unspent $100k and collectively ordered $206k:

    regime      $20,000   (20% of equity)
    router      $88,000   (idle = equity - deployed(0) - buffers)
    cash-park   $98,000   (cash still read as $100,000)

Paper margin is 4x, so nothing would have been rejected — the account would
simply have opened at 2x leverage.

Run: uv run python -m pytest tests/ -q
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_portfolio.py"
spec = importlib.util.spec_from_file_location("run_portfolio", SCRIPT)
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


class _Order:
    def __init__(self, symbol):
        self.symbol = symbol


class SettlingBroker:
    """Reports open orders for the first `fills_after` polls, then none."""

    def __init__(self, fills_after: int, symbols=("QLD",)):
        self.fills_after = fills_after
        self.symbols = symbols
        self.polls = 0

    def open_orders(self):
        self.polls += 1
        if self.polls > self.fills_after:
            return []
        return [_Order(s) for s in self.symbols]


class StuckBroker:
    def __init__(self):
        self.polls = 0

    def open_orders(self):
        self.polls += 1
        return [_Order("QLD")]


class BrokenBroker:
    def open_orders(self):
        raise ConnectionError("alpaca unreachable")


def _noop_sleep(_seconds):
    """Keep tests instant — the poll interval is irrelevant to the logic."""


def test_dry_run_never_waits():
    """Nothing was submitted, so there is nothing to settle."""
    broker = SettlingBroker(fills_after=99)
    assert rp.settle_orders(broker, live=False, sleep=_noop_sleep) is True
    assert broker.polls == 0


def test_returns_true_once_orders_fill():
    broker = SettlingBroker(fills_after=2)
    assert rp.settle_orders(broker, live=True, sleep=_noop_sleep) is True
    assert broker.polls == 3          # two polls saw orders, the third saw none


def test_already_settled_returns_immediately():
    broker = SettlingBroker(fills_after=0)
    assert rp.settle_orders(broker, live=True, sleep=_noop_sleep) is True
    assert broker.polls == 1


def test_timeout_reports_failure_rather_than_proceeding():
    """The caller stops the cycle on False. Returning True here would let the
    next sleeve size off a balance with orders still in flight — the exact
    $206k bug."""
    broker = StuckBroker()
    assert rp.settle_orders(
        broker, live=True, timeout_s=5, poll_s=1, sleep=_noop_sleep) is False
    assert broker.polls >= 5


def test_broker_error_is_treated_as_unsettled():
    """If we cannot confirm the book is clear, we must assume it is not."""
    assert rp.settle_orders(
        BrokenBroker(), live=True, sleep=_noop_sleep) is False


def test_timeout_is_bounded():
    """A stuck order must not hang the scheduled job forever."""
    broker = StuckBroker()
    slept = []
    rp.settle_orders(broker, live=True, timeout_s=10, poll_s=2,
                     sleep=slept.append)
    assert sum(slept) <= 10
