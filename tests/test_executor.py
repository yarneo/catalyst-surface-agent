"""Orchestration tests with a fake broker.

The last audit of this repo found all six real bugs in orchestration code that
no test covered, because covering it meant touching a broker. That recurred
here: six defects in the inline version, none reachable by any test. So the
broker is faked and the failures it actually produces — partial fills,
no-fills, exceptions — are simulated deliberately.
"""
import datetime as dt

import pytest

from trading_bot.options.condor import Condor
from trading_bot.options.book import Book, BookEntry
from trading_bot.options.execution import Fill
from trading_bot.options.executor import ExecutionReport, execute_plan
from trading_bot.options.iv import Quote
from trading_bot.options.session import Holding, SessionPlan
from trading_bot.options.spreads import Leg

LEGS = (Leg("SPY260904C00770000", "sell"), Leg("SPY260904C00780000", "buy"))


def quote_fn(syms):
    return {s: Quote(1.00, 1.04) for s in syms}


class FakeBroker:
    """Records calls and returns whatever the test scripted."""

    def __init__(self, open_result=None, close_result=None, raises=None):
        self.open_result = open_result
        self.close_result = close_result
        self.raises = raises
        self.opens, self.closes = [], []


def patched(monkeypatch, *, open_result=None, close_result=None,
            open_raises=None, close_raises=None):
    import trading_bot.options.executor as ex
    calls = {"open": [], "close": []}

    def fake_open(mcp, spread, qty, quotes, **kw):
        calls["open"].append((spread.legs, qty))
        if open_raises:
            raise open_raises
        return open_result

    def fake_close(mcp, legs, qty, qfn, **kw):
        calls["close"].append((legs, qty))
        if close_raises:
            raise close_raises
        return close_result

    monkeypatch.setattr(ex, "open_spread", fake_open)
    monkeypatch.setattr(ex, "close_spread", fake_close)
    return calls


def cand(name="SPY", max_profit=1.0, max_loss=9.0, entry=-1.0):
    return Condor(name, LEGS, entry, max_loss, "2026-09-03", 6, 770.0, 750.0, 10.0)


def seeded_book(tmp_path, qty=5):
    b = Book(tmp_path / "b.json")
    b.add(BookEntry(id="e1", underlying="SPY", structure="call spread",
                    legs=[{"symbol": l.symbol, "side": l.side, "ratio_qty": 1}
                          for l in LEGS],
                    qty=qty, entry=-1.0, max_profit=1.0, max_loss=9.0,
                    opened_at="2026-08-28T12:00:00"))
    return b


def holding(entry_id="e1", qty=5):
    return Holding(entry_id, "SPY", LEGS, qty, -1.0, 0.3, 9.0)


# --- what actually filled -----------------------------------------------

def test_registry_records_the_filled_quantity_not_the_requested_one(tmp_path, monkeypatch):
    """A partial fill on a four-leg order is a different position. Recording
    the intended size makes every later cycle size against risk we do not hold."""
    patched(monkeypatch, open_result=Fill(True, 3, -1.05, 1, "limit"))
    b = Book(tmp_path / "b.json")
    plan = SessionPlan(open=[(cand(), 10)])
    execute_plan(None, quote_fn, b, plan)
    assert b.open_entries[0].qty == 3, "recorded the requested size, not the fill"


def test_registry_records_the_actual_fill_price(tmp_path, monkeypatch):
    patched(monkeypatch, open_result=Fill(True, 5, -1.23, 1, "limit"))
    b = Book(tmp_path / "b.json")
    execute_plan(None, quote_fn, b, SessionPlan(open=[(cand(), 5)]))
    assert b.open_entries[0].entry == -1.23


def test_max_profit_recorded_is_the_credit(tmp_path, monkeypatch):
    """Every structure this agent opens is a credit structure, so max profit is
    the credit and is always finite. The previous design admitted unbounded-
    profit ratio spreads and stored max_profit as 0.0, which read downstream as
    "cannot profit" and disabled the profit target on exactly the positions it
    most needed to manage."""
    patched(monkeypatch, open_result=Fill(True, 2, -1.35, 1, "limit"))
    b = Book(tmp_path / "b.json")
    execute_plan(None, quote_fn, b, SessionPlan(open=[(cand(entry=-1.35), 2)]))
    e = b.open_entries[0]
    assert e.max_profit == pytest.approx(1.35)
    assert e.max_loss == pytest.approx(9.0)


def test_profit_is_measured_against_the_credit_received():
    """Simplification: every structure this agent opens is a credit structure,
    so the denominator is always the credit and is always finite. The old code
    stored unbounded profit as 0.0, which read downstream as "cannot profit"
    and silently disabled the profit target."""
    h = Holding("e1", "SPY", LEGS, 1, -1.0, 0.30, 9.0)
    assert h.credit == pytest.approx(1.0)
    assert h.profit_captured == pytest.approx(0.70)


# --- partial closes ------------------------------------------------------

def test_partial_close_keeps_the_remainder_live(tmp_path, monkeypatch):
    """Closing 3 of 5 and marking the row closed hides a live position from
    every later cycle."""
    patched(monkeypatch, close_result=Fill(True, 3, 0.40, 1, "limit"))
    b = seeded_book(tmp_path, qty=5)
    plan = SessionPlan(close=[(holding(qty=5), "profit target")])
    execute_plan(None, quote_fn, b, plan)
    live = b.open_entries
    assert len(live) == 1 and live[0].qty == 2, f"expected 2 left, got {live}"
    assert b.expected_exposure() == {"SPY260904C00770000": -2,
                                     "SPY260904C00780000": 2}


def test_full_close_empties_the_registry(tmp_path, monkeypatch):
    patched(monkeypatch, close_result=Fill(True, 5, 0.40, 1, "limit"))
    b = seeded_book(tmp_path, qty=5)
    execute_plan(None, quote_fn, b,
                 SessionPlan(close=[(holding(qty=5), "profit target")]))
    assert b.open_entries == []
    assert b.expected_exposure() == {}


def test_partial_close_books_only_the_traded_piece(tmp_path, monkeypatch):
    patched(monkeypatch, close_result=Fill(True, 2, 0.40, 1, "limit"))
    b = seeded_book(tmp_path, qty=5)
    execute_plan(None, quote_fn, b,
                 SessionPlan(close=[(holding(qty=5), "x")]))
    # opened -1.00, closed 0.40, two contracts -> +$120
    assert b.realised_pnl() == pytest.approx(120.0)


# --- failure isolation ---------------------------------------------------

def test_a_raising_close_does_not_skip_the_next_close(tmp_path, monkeypatch):
    """Failure isolation among closes: a broker timeout on one exit must not
    leave the next one unattempted.

    (It DOES now stop the opens — see test_failed_closes_prevent_the_opens.
    An earlier version of this test asserted the opposite, which was the bug:
    the new positions are sized assuming the closing rows' risk was freed.)
    """
    calls = patched(monkeypatch, close_raises=RuntimeError("broker timeout"))
    b = seeded_book(tmp_path)
    b.add(BookEntry(id="e2", underlying="SPY", structure="call spread",
                    legs=[{"symbol": l.symbol, "side": l.side, "ratio_qty": 1}
                          for l in LEGS],
                    qty=5, entry=-1.0, max_profit=1.0, max_loss=9.0,
                    opened_at="2026-08-29T12:00:00"))
    plan = SessionPlan(close=[(holding("e1"), "deadline"),
                              (holding("e2"), "deadline")])
    rep = execute_plan(None, quote_fn, b, plan)
    assert len(calls["close"]) == 2, "a failed close skipped the next one"
    assert len(rep.failures) == 2


def test_a_raising_open_does_not_abort_later_opens(tmp_path, monkeypatch):
    patched(monkeypatch, open_raises=RuntimeError("nope"))
    b = Book(tmp_path / "b.json")
    plan = SessionPlan(open=[(cand("AAA"), 1),
                             (cand("BBB"), 1)])
    rep = execute_plan(None, quote_fn, b, plan)
    assert len(rep.failures) == 2
    assert b.open_entries == []


def test_a_close_that_does_not_fill_leaves_the_entry_open(tmp_path, monkeypatch):
    patched(monkeypatch, close_result=Fill(False, 0, None, 4, "unfilled"))
    b = seeded_book(tmp_path)
    rep = execute_plan(None, quote_fn, b, SessionPlan(close=[(holding(), "x")]))
    assert b.open_entries and b.open_entries[0].qty == 5
    assert rep.failures


def test_an_open_that_does_not_fill_writes_nothing(tmp_path, monkeypatch):
    patched(monkeypatch, open_result=Fill(False, 0, None, 1, "unfilled"))
    b = Book(tmp_path / "b.json")
    rep = execute_plan(None, quote_fn, b,
                       SessionPlan(open=[(cand(), 3)]))
    assert b.entries == [], "recorded a position that never filled"
    assert rep.failures


def test_unknown_open_order_state_stops_later_orders(tmp_path, monkeypatch):
    calls = patched(monkeypatch,
                    open_result=Fill(False, 0, None, 1, "stuck"))
    b = Book(tmp_path / "b.json")
    rep = execute_plan(None, quote_fn, b,
                       SessionPlan(open=[(cand("AAA"), 1),
                                         (cand("BBB"), 1)]))
    assert len(calls["open"]) == 1
    assert rep.failures == [("AAA", "open order state is unknown")]
    assert b.entries == []


# --- identity ------------------------------------------------------------

def test_the_right_row_is_closed_when_two_are_identical(tmp_path, monkeypatch):
    """Two entries can share legs and quantity — the same structure opened on
    two cycles. Matching on legs closes whichever comes first and leaves the
    other claiming exposure that is gone."""
    patched(monkeypatch, close_result=Fill(True, 5, 0.40, 1, "limit"))
    b = seeded_book(tmp_path, qty=5)
    b.add(BookEntry(id="e2", underlying="SPY", structure="call spread",
                    legs=[{"symbol": l.symbol, "side": l.side, "ratio_qty": 1}
                          for l in LEGS],
                    qty=5, entry=-1.0, max_profit=1.0, max_loss=9.0,
                    opened_at="2026-08-29T12:00:00"))
    execute_plan(None, quote_fn, b, SessionPlan(close=[(holding("e2"), "x")]))
    assert [e.id for e in b.open_entries] == ["e1"]


def test_a_plan_referencing_a_missing_row_is_reported_not_traded(tmp_path, monkeypatch):
    calls = patched(monkeypatch, close_result=Fill(True, 5, 0.4, 1, "limit"))
    b = Book(tmp_path / "b.json")
    rep = execute_plan(None, quote_fn, b, SessionPlan(close=[(holding("ghost"), "x")]))
    assert rep.orphans == ["ghost"]
    assert calls["close"] == [], "traded against a structure the registry lost"


# --- ordering ------------------------------------------------------------

def test_closes_happen_before_opens(tmp_path, monkeypatch):
    order = []
    import trading_bot.options.executor as ex
    monkeypatch.setattr(ex, "close_spread",
                        lambda *a, **k: (order.append("close"),
                                         Fill(True, 5, 0.4, 1, "limit"))[1])
    monkeypatch.setattr(ex, "open_spread",
                        lambda *a, **k: (order.append("open"),
                                         Fill(True, 1, -1.0, 1, "limit"))[1])
    b = seeded_book(tmp_path)
    execute_plan(None, quote_fn, b,
                 SessionPlan(close=[(holding(), "x")],
                             open=[(cand(), 1)]))
    assert order == ["close", "open"], "opened before freeing risk"


def test_failed_closes_prevent_the_opens(tmp_path, monkeypatch):
    """Ordering alone is not enough: the new positions were sized assuming the
    closing rows' risk was freed, so opening on top of a failed close puts the
    book at survivors + failed-closes + new-opens."""
    patched(monkeypatch, close_result=Fill(False, 0, None, 4, "unfilled"),
            open_result=Fill(True, 1, -1.0, 1, "limit"))
    b = seeded_book(tmp_path)
    plan = SessionPlan(close=[(holding(), "profit target")],
                       open=[(cand(), 1)])
    rep = execute_plan(None, quote_fn, b, plan)
    assert rep.failures
    assert rep.opened == [], "opened on top of a close that did not happen"


def test_successful_closes_still_allow_the_opens(tmp_path, monkeypatch):
    patched(monkeypatch, close_result=Fill(True, 5, 0.40, 1, "limit"),
            open_result=Fill(True, 1, -1.0, 1, "limit"))
    b = seeded_book(tmp_path)
    plan = SessionPlan(close=[(holding(), "profit target")],
                       open=[(cand(), 1)])
    rep = execute_plan(None, quote_fn, b, plan)
    assert rep.failures == []
    assert len(rep.opened) == 1
    assert rep.opened[0][0].underlying == "SPY"


def test_only_a_confirmed_entry_nonfill_is_a_healthy_cycle():
    benign = ExecutionReport(failures=[("QQQ", "open did not fill")])
    assert benign.requires_attention() is False

    assert ExecutionReport(
        failures=[("QQQ", "open order state is unknown")]
    ).requires_attention() is True
    assert ExecutionReport(
        failures=[("SPY-1", "close did not fill")]
    ).requires_attention() is True
    assert ExecutionReport(orphans=["SPY-1"]).requires_attention() is True
