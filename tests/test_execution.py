"""Execution-layer tests, with a broker that misbehaves the way real ones do.

This module previously had NO tests — 33% line coverage, every covered line a
dataclass or a constant. An audit showed it could be mutated to submit
`sell_to_open` on a close, doubling a position instead of flattening it, and the
whole suite stayed green.

The fake below reproduces the three behaviours that turned the escalation ladder
into a position-doubler on a live account: a cancel rejected with 422, a fill
landing during the cancel, and an order cancelled after a partial fill.
"""
import pytest

from trading_bot.options.execution import (EXIT_LADDER, Fill, cancel_and_confirm,
                                           close_spread, open_spread)
from trading_bot.options.iv import Quote
from trading_bot.options.spreads import Leg

LEGS = (Leg("SPY260903C00780000", "sell"), Leg("SPY260903C00790000", "buy"),
        Leg("SPY260903P00750000", "sell"), Leg("SPY260903P00740000", "buy"))


def quote_fn(syms):
    return {s: Quote(1.00, 1.06) for s in syms}


class FakeBroker:
    """Scriptable broker. `script` maps rung index -> (status, filled_qty)."""

    def __init__(self, script, *, cancel_works=True, poll_raises=False,
                 non_dict=False, fill_on_cancel=False,
                 raise_after_submit=False):
        self.script = script
        self.cancel_works = cancel_works
        self.poll_raises = poll_raises
        self.non_dict = non_dict
        self.fill_on_cancel = fill_on_cancel
        self.raise_after_submit = raise_after_submit
        self.submitted = []          # (coid, qty)
        self.limits = []             # limit price of each order, in order
        self.cancels = []
        self.orders = {}

    # --- order entry
    def close_spread(self, legs, qty, *, limit_price=None, client_order_id=None):
        self.limits.append(limit_price)
        return self._place(client_order_id, qty)

    def place_spread(self, legs, qty, *, limit_price=None, client_order_id=None):
        return self._place(client_order_id, qty)

    def _place(self, coid, qty):
        tail = coid.rsplit("-", 1)[-1]
        rung = int(tail) if tail.isdigit() else 0
        status, filled = self.script.get(rung, ("new", 0))
        self.submitted.append((coid, qty))
        # Shaped like a real Alpaca payload, deliberately:
        #   * `qty` IS present. Omitting it hid a defect where `_filled_qty`
        #     fell back to the requested size, so a rejected order read as
        #     fully filled.
        #   * `filled_qty` is NOT pre-clamped to what was submitted. Brokers
        #     do occasionally over-report, and clamping in the fake made the
        #     code's own clamp untestable.
        self.orders[coid] = {"id": f"id-{coid}", "client_order_id": coid,
                             "qty": str(qty), "status": status,
                             "filled_qty": str(filled),
                             "filled_avg_price": "1.05" if filled else None}
        if self.raise_after_submit:
            raise RuntimeError("reply lost after broker accepted order")
        return dict(self.orders[coid])

    # --- status
    def order_by_client_id(self, coid):
        if self.poll_raises:
            raise RuntimeError("server blip")
        if self.non_dict:
            return "not json"
        # A COPY, never the stored object. Returning the same dict by reference
        # meant `cancel()` mutating it in place made the pre-cancel and
        # post-cancel views identical — so the fill-during-cancellation race
        # could not be observed, and a mutation reading the fill from the stale
        # poll survived the test named for it.
        return dict(self.orders[coid])

    def cancel(self, oid):
        self.cancels.append(oid)
        if not self.cancel_works:
            raise RuntimeError("422 order is not cancelable")
        coid = oid.replace("id-", "")
        if self.fill_on_cancel:
            self.orders[coid]["status"] = "filled"
            self.orders[coid]["filled_qty"] = str(self.submitted[-1][1])
            self.orders[coid]["filled_avg_price"] = "1.05"
        else:
            self.orders[coid]["status"] = "canceled"

    def cancel_by_client_id(self, coid):
        if coid not in self.orders:
            raise RuntimeError("unknown client order id")
        self.cancel(self.orders[coid]["id"])

    @property
    def total_submitted(self):
        return sum(q for _, q in self.submitted)


FAST = dict(timeout_s=0.3, poll_s=0.05, cancel_timeout_s=0.3)


# --- the ladder must not escalate over a live order ---------------------

def test_a_rejected_cancel_stops_the_ladder():
    """Alpaca returns 422 for an order already pending_new or partially filled.
    The old code swallowed it and submitted the next rung anyway — with two
    rungs filling, a 3-lot close became a 3-lot short spread the wrong way."""
    b = FakeBroker({}, cancel_works=False)
    f = close_spread(b, LEGS, 3, quote_fn, **FAST)
    assert not f.filled
    assert f.how == "stuck"
    assert len(b.submitted) == 1, f"escalated over a live order: {b.submitted}"


def test_polls_that_all_raise_do_not_leave_four_orders_resting():
    """`_poll` returning None used to mean `_cancel(None)` returned instantly:
    zero cancels issued, four rungs live, four times the intended close."""
    b = FakeBroker({}, poll_raises=True)
    f = close_spread(b, LEGS, 2, quote_fn, **FAST)
    assert len(b.submitted) == 1
    assert f.how == "stuck"


def test_a_non_dict_payload_does_not_crash_or_orphan_the_order():
    """MCP returns a bare string when a text block is not JSON. `o.get(...)`
    outside the try raised AttributeError out of close_spread, leaving the order
    uncancelled."""
    b = FakeBroker({}, non_dict=True)
    f = close_spread(b, LEGS, 2, quote_fn, **FAST)
    assert isinstance(f, Fill)
    assert len(b.submitted) == 1


def test_total_contracts_submitted_never_exceeds_the_request():
    """The invariant that matters. Across every rung, we must never work more
    contracts than we set out to close."""
    for script in ({}, {0: ("canceled", 1)}, {0: ("new", 0), 1: ("canceled", 2)},
                   {0: ("canceled", 2), 1: ("filled", 3)}):
        b = FakeBroker(script)
        close_spread(b, LEGS, 5, quote_fn, **FAST)
        for coid, qty in b.submitted:
            assert qty <= 5, f"{script}: submitted {qty} of a 5-lot"


# --- partial fills -------------------------------------------------------

def test_a_partial_fill_reduces_the_next_rung():
    """An order cancelled after a partial fill used to read as unfilled, so the
    next rung resubmitted the FULL size. Measured: a five-lot close that put
    through seven, leaving the account short two spreads the wrong way."""
    b = FakeBroker({0: ("canceled", 2), 1: ("filled", 3)})
    f = close_spread(b, LEGS, 5, quote_fn, **FAST)
    assert f.filled
    assert f.qty == 5
    assert b.submitted[0][1] == 5
    assert b.submitted[1][1] == 3, f"resubmitted the full size: {b.submitted}"
    assert b.total_submitted == 8      # 5 offered, 2 taken, 3 offered
    assert sum(1 for _ in b.submitted) == 2


def test_partial_fills_accumulate_across_rungs():
    b = FakeBroker({0: ("canceled", 1), 1: ("canceled", 1), 2: ("filled", 2)})
    f = close_spread(b, LEGS, 4, quote_fn, **FAST)
    assert f.qty == 4
    assert [q for _, q in b.submitted] == [4, 3, 2]


def test_a_close_that_only_partially_fills_reports_the_partial():
    b = FakeBroker({i: ("canceled", 1) for i in range(len(EXIT_LADDER))})
    f = close_spread(b, LEGS, 9, quote_fn, **FAST)
    assert f.filled
    assert f.qty == len(EXIT_LADDER)
    assert f.how == "partial"


def test_a_fill_landing_during_the_cancel_is_counted():
    """The order fills while we are cancelling it. The old code returned
    filled=False while the broker showed it done."""
    b = FakeBroker({}, fill_on_cancel=True)
    f = close_spread(b, LEGS, 2, quote_fn, **FAST)
    assert f.filled
    assert f.qty == 2


# --- the happy path ------------------------------------------------------

def test_first_rung_fill_is_reported_as_a_limit():
    b = FakeBroker({0: ("filled", 4)})
    f = close_spread(b, LEGS, 4, quote_fn, **FAST)
    assert (f.filled, f.qty, f.attempts, f.how) == (True, 4, 1, "limit")
    assert len(b.submitted) == 1


def test_a_later_rung_is_reported_as_escalated():
    b = FakeBroker({0: ("canceled", 0), 1: ("filled", 4)})
    f = close_spread(b, LEGS, 4, quote_fn, **FAST)
    assert f.how == "escalated"
    assert f.attempts == 2


def test_the_last_rung_is_a_market_order():
    assert EXIT_LADDER[-1] is None
    b = FakeBroker({i: ("canceled", 0) for i in range(3)} | {3: ("filled", 2)})
    f = close_spread(b, LEGS, 2, quote_fn, **FAST)
    assert f.how == "market"


def test_the_ladder_concedes_monotonically():
    concessions = [c for c in EXIT_LADDER if c is not None]
    assert concessions == sorted(concessions)


# --- entry ---------------------------------------------------------------

def test_an_entry_tries_once_and_does_not_escalate():
    b = FakeBroker({0: ("new", 0)})
    from trading_bot.options.condor import Condor
    spread = Condor("SPY", LEGS, -2.0, 8.0, "2026-09-03", 6, 780.0, 750.0, 10.0)
    f = open_spread(b, spread, 3, quote_fn([l.symbol for l in LEGS]), **FAST)
    assert not f.filled
    assert len(b.submitted) == 1


def test_a_partial_entry_is_reported_as_a_position():
    """The old code returned filled=False on a partial and wrote no registry
    row, leaving the broker holding contracts nothing was managing."""
    b = FakeBroker({0: ("canceled", 2)})
    from trading_bot.options.condor import Condor
    spread = Condor("SPY", LEGS, -2.0, 8.0, "2026-09-03", 6, 780.0, 750.0, 10.0)
    f = open_spread(b, spread, 5, quote_fn([l.symbol for l in LEGS]), **FAST)
    assert f.filled
    assert f.qty == 2
    assert f.how == "partial"


def test_entry_submission_error_recovers_fill_by_client_id():
    """The broker accepted and filled; only the submission reply was lost."""
    from trading_bot.options.condor import Condor
    b = FakeBroker({0: ("filled", 3)}, raise_after_submit=True)
    spread = Condor("SPY", LEGS, -2.0, 8.0, "2026-09-03", 6, 780.0, 750.0, 10.0)
    f = open_spread(b, spread, 3, quote_fn([l.symbol for l in LEGS]), **FAST)
    assert f.filled and f.qty == 3
    assert len(b.submitted) == 1


def test_close_submission_error_recovers_fill_by_client_id():
    b = FakeBroker({0: ("filled", 3)}, raise_after_submit=True)
    f = close_spread(b, LEGS, 3, quote_fn, **FAST)
    assert f.filled and f.qty == 3
    assert len(b.submitted) == 1


# --- cancel_and_confirm --------------------------------------------------

def test_confirm_returns_true_for_an_already_terminal_order():
    b = FakeBroker({})
    ok, final = cancel_and_confirm(b, {"id": "x", "status": "filled"})
    assert ok is True
    assert final["status"] == "filled"
    assert b.cancels == []


def test_confirm_returns_false_when_it_cannot_prove_death():
    b = FakeBroker({}, cancel_works=False)
    b.orders["c"] = {"id": "id-c", "client_order_id": "c", "status": "new"}
    ok, _ = cancel_and_confirm(b, b.orders["c"], timeout_s=0.2, poll_s=0.05)
    assert ok is False


def test_confirm_returns_false_for_a_non_dict():
    assert cancel_and_confirm(FakeBroker({}), "nonsense") == (False, None)


# --- gaps the second mutation audit found -------------------------------

def test_a_rejected_order_is_not_read_as_filled():
    """`_filled_qty` must read `filled_qty`, never fall back to `qty`. The old
    fake omitted `qty` from its payload, so a mutation doing exactly that
    survived — on a real payload it would report a rejected order as complete."""
    b = FakeBroker({0: ("rejected", 0)})
    f = close_spread(b, LEGS, 5, quote_fn, **FAST)
    assert not f.filled
    assert f.qty == 0


def test_an_over_reported_fill_is_clamped_to_what_was_requested():
    """Brokers occasionally report more filled than submitted. The old fake
    clamped it itself, which made the code's own clamp untestable."""
    b = FakeBroker({0: ("filled", 500)})
    f = close_spread(b, LEGS, 5, quote_fn, **FAST)
    assert f.qty == 5, f"recorded {f.qty} of a 5-lot"


def test_an_unfilled_entry_order_is_cancelled():
    """An entry limit left resting can fill later, unrecorded and unmanaged —
    the entry-side twin of the exit-ladder bug. The old test asserted only that
    one order was submitted, never that it was cancelled."""
    from trading_bot.options.condor import Condor
    b = FakeBroker({0: ("new", 0)})
    spread = Condor("SPY", LEGS, -2.0, 8.0, "2026-09-03", 6, 780.0, 750.0, 10.0)
    f = open_spread(b, spread, 3, quote_fn([l.symbol for l in LEGS]), **FAST)
    assert not f.filled
    assert b.cancels, "left an unfilled entry order resting in the book"


def test_a_cancel_is_issued_even_when_polling_never_returned_a_payload():
    """`_poll` returns None if every lookup raises. The old code then issued
    ZERO cancels — the original defect, which returning early on a non-dict
    merely stopped escalating on top of."""
    b = FakeBroker({}, poll_raises=True)
    close_spread(b, LEGS, 2, quote_fn, **FAST)
    assert b.cancels, "no cancel issued for an order we could not poll"


def test_each_rung_concedes_more_than_the_last():
    """The ladder must re-price per rung, conceding progressively. Nothing
    checked the limit prices that actually reached the broker, so a mutation
    pricing every rung at the first rung's concession survived."""
    b = FakeBroker({i: ("canceled", 0) for i in range(3)} | {3: ("filled", 2)})
    close_spread(b, LEGS, 2, quote_fn, **FAST)
    limits = b.limits
    assert len(limits) == 4, f"expected one order per rung, got {limits}"
    assert limits[-1] is None, "the last rung must be a market order"
    priced = [x for x in limits if x is not None]
    assert priced == sorted(priced), \
        f"concessions did not increase monotonically: {priced}"
    assert priced[0] != priced[1], "every rung priced identically — no re-quote"


def test_the_first_rung_is_the_least_generous():
    b = FakeBroker({0: ("filled", 2)})
    close_spread(b, LEGS, 2, quote_fn, **FAST)
    cheap = b.limits[0]
    b2 = FakeBroker({i: ("canceled", 0) for i in range(3)} | {3: ("filled", 2)})
    close_spread(b2, LEGS, 2, quote_fn, **FAST)
    assert b2.limits[0] == cheap
    assert b2.limits[1] > cheap, "second rung was not more generous"
