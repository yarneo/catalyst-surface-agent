"""End-to-end tests for run_options_agent.py — the layer that has never had any.

Three independent audits have now found their worst defect in this one file, and
each time it was the same shape: a guard returning early and taking the deadline
flatten down with it. The first version used `return 2` on a reconcile mismatch.
The rebuild used `return 4` on an unmarkable position — one guard later, after
documenting the first as a lesson.

These drive `main()` against a fake broker and a fake chain at specific
simulated instants, and assert on what the agent DOES, not on what it logs.
"""
import datetime as dt
import importlib.util
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_bot.options.book import Book, BookEntry  # noqa: E402
from trading_bot.options.iv import Quote  # noqa: E402

ET = ZoneInfo("America/New_York")
LEGS = [{"symbol": "SPY260903C00790000", "side": "sell", "ratio_qty": 1},
        {"symbol": "SPY260903C00800000", "side": "buy", "ratio_qty": 1},
        {"symbol": "SPY260903P00740000", "side": "sell", "ratio_qty": 1},
        {"symbol": "SPY260903P00730000", "side": "buy", "ratio_qty": 1}]


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location(
        "run_options_agent_under_test", ROOT / "scripts" / "run_options_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeMCP:
    def __init__(self, positions=None, equity=100_000.0,
                 account_number="PA-TEST", options_level=3):
        self._positions = positions if positions is not None else []
        self.equity = equity
        self.account_number = account_number
        self.options_level = options_level
        self.closed, self.opened = [], []

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def account(self):
        return {"equity": str(self.equity),
                "account_number": self.account_number,
                "options_approved_level": self.options_level,
                "options_trading_level": self.options_level,
                "trading_blocked": False, "account_blocked": False}
    def positions(self): return self._positions

    def close_spread(self, legs, qty, **kw):
        self.closed.append((legs, qty))
        return {"id": "x", "client_order_id": kw.get("client_order_id"),
                "status": "filled", "filled_qty": str(qty),
                "filled_avg_price": "0.40"}

    def place_spread(self, legs, qty, **kw):
        self.opened.append((legs, qty))
        return {"id": "y", "client_order_id": kw.get("client_order_id"),
                "status": "filled", "filled_qty": str(qty),
                "filled_avg_price": "-2.00"}

    def order_by_client_id(self, coid):
        return {"id": "x", "client_order_id": coid, "status": "filled",
                "filled_qty": "99", "filled_avg_price": "0.40"}

    def cancel(self, oid): pass


# Quotes for a condor part-way through its life. Priced per leg, not uniformly:
# a fake that quotes every leg identically makes the structure look ~94%
# profitable, so the profit target fires and no test can observe anything else.
MID_FLIGHT = {
    "SPY260903C00790000": Quote(1.00, 1.05),      # short call, still has value
    "SPY260903C00800000": Quote(0.40, 0.45),      # long wing
    "SPY260903P00740000": Quote(0.95, 1.00),      # short put
    "SPY260903P00730000": Quote(0.35, 0.40),      # long wing
}


class FakeChain:
    """Quotes everything, or nothing — `quotable` toggles the failure the
    audits found: expired contracts have no quotes at all."""

    def __init__(self, quotable=True):
        self.quotable = quotable

    def quotes(self, syms):
        if not self.quotable:
            return {}
        return {s: MID_FLIGHT.get(s, Quote(1.00, 1.06)) for s in syms}

    def spot(self, sym): return 766.0
    def expiries(self, sym, spot, **kw): return []


def seed_book(tmp_path, expiry_legs=LEGS, qty=4):
    b = Book(tmp_path / "options_book.json")
    b.add(BookEntry(id="SPY-1", underlying="SPY", structure="iron_condor",
                    legs=expiry_legs, qty=qty, entry=-2.0, max_profit=2.0,
                    max_loss=8.0, opened_at="2026-08-28T12:00:00"))
    return b


def drive(runner, monkeypatch, tmp_path, *, now, positions=None, quotable=True,
          argv=None, env=None, equity=100_000.0):
    """Run main() at a simulated instant with fakes wired in."""
    mcp = FakeMCP(positions=positions, equity=equity)
    chain = FakeChain(quotable=quotable)
    monkeypatch.setattr(runner, "MCPClient", lambda *a, **k: mcp)
    monkeypatch.setattr(runner, "ChainClient", lambda **k: chain)
    monkeypatch.setattr(runner, "now_et", lambda: now)
    monkeypatch.setattr(runner, "dotenv_values",
                        lambda p: env or {"ALPACA_API_KEY": "k",
                                          "ALPACA_SECRET_KEY": "s"})
    monkeypatch.setattr(sys, "argv",
                        ["run", "--book", str(tmp_path / "options_book.json")]
                        + (argv or []))
    rc = runner.main()
    return rc, mcp


def test_verify_account_works_while_market_is_closed(runner, monkeypatch, tmp_path):
    env = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s",
           "ALPACA_ACCOUNT_NUMBER": "PA-TEST",
           "ALPACA_INITIAL_EQUITY": "100000", "ALPACA_OPTIONS_LEVEL": "3"}
    rc, mcp = drive(
        runner, monkeypatch, tmp_path,
        now=dt.datetime(2026, 8, 28, 5, 0, tzinfo=ET),
        argv=["--verify-account"], env=env)
    assert rc == 0
    assert mcp.opened == [] and mcp.closed == []


def test_wrong_account_is_refused_before_trading(runner, monkeypatch, tmp_path):
    env = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s",
           "ALPACA_ACCOUNT_NUMBER": "PA-JUDGED"}
    rc, mcp = drive(
        runner, monkeypatch, tmp_path,
        now=dt.datetime(2026, 8, 28, 12, 0, tzinfo=ET), env=env)
    assert rc == 5
    assert mcp.opened == [] and mcp.closed == []


def test_verify_account_requires_exact_initial_equity(runner, monkeypatch,
                                                       tmp_path):
    env = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s",
           "ALPACA_ACCOUNT_NUMBER": "PA-TEST",
           "ALPACA_INITIAL_EQUITY": "100000", "ALPACA_OPTIONS_LEVEL": "3"}
    rc, _ = drive(
        runner, monkeypatch, tmp_path,
        now=dt.datetime(2026, 8, 28, 5, 0, tzinfo=ET),
        argv=["--verify-account"], env=env, equity=99_999.99)
    assert rc == 5


def test_plan_refreshes_time_after_a_scan_crosses_start(runner, monkeypatch,
                                                        tmp_path):
    """A scan takes minutes. A cycle beginning at 10:59 must not keep using
    10:59 after the clock has crossed the 11:00 judged start."""
    before = dt.datetime(2026, 8, 28, 10, 59, tzinfo=ET)
    after = dt.datetime(2026, 8, 28, 11, 1, tzinfo=ET)
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        return before if calls == 1 else after

    seen = {}
    original = runner.plan_session

    def capture(**kwargs):
        seen["now"] = kwargs["now"]
        return original(**kwargs)

    mcp = FakeMCP()
    monkeypatch.setattr(runner, "MCPClient", lambda *a, **k: mcp)
    monkeypatch.setattr(runner, "ChainClient", lambda **k: FakeChain())
    monkeypatch.setattr(runner, "now_et", clock)
    monkeypatch.setattr(runner, "plan_session", capture)
    monkeypatch.setattr(runner, "dotenv_values",
                        lambda p: {"ALPACA_API_KEY": "k",
                                   "ALPACA_SECRET_KEY": "s"})
    monkeypatch.setattr(sys, "argv",
                        ["run", "--dry-run", "--book",
                         str(tmp_path / "options_book.json")])
    assert runner.main() == 0
    assert seen["now"] == after


def test_journal_facts_name_the_fill_after_an_earlier_nonfill(runner):
    """A failed first order must not relabel the second fill as the first."""
    from trading_bot.options.execution import Fill

    entry = BookEntry(
        id="IWM-filled", underlying="IWM", structure="iron_condor",
        legs=[
            {"symbol": "IWM260903C00301000", "side": "sell", "ratio_qty": 1},
            {"symbol": "IWM260903C00304000", "side": "buy", "ratio_qty": 1},
            {"symbol": "IWM260903P00296000", "side": "sell", "ratio_qty": 1},
            {"symbol": "IWM260903P00293000", "side": "buy", "ratio_qty": 1},
        ],
        qty=1, entry=-1.08, max_profit=1.08, max_loss=1.92,
        opened_at="2026-08-28T11:09:11-04:00")

    facts = runner._opened_facts(
        [(entry, Fill(True, 1, -1.08, 1, "limit"))])

    assert facts == [{
        "symbol": "IWM", "qty": 1, "credit": 108.0, "risk": 192.0,
        "strikes": "296/301", "expiry": dt.date(2026, 9, 3),
    }]


# --- the defect all three audits found ----------------------------------

def test_an_unmarkable_position_is_closed_not_halted(runner, monkeypatch, tmp_path):
    """The strategy holds contracts whose quotes vanish near expiry. Treating
    that as a hard stop killed the deadline flatten in both previous versions —
    every cycle died before `plan_session` was ever reached."""
    seed_book(tmp_path)
    account = [{"symbol": l["symbol"], "qty": str(-4 if l["side"] == "sell" else 4),
                "market_value": "0", "unrealized_pl": "0"} for l in LEGS]
    rc, mcp = drive(runner, monkeypatch, tmp_path, quotable=False,
                    positions=account,
                    now=dt.datetime(2026, 9, 3, 14, 30, tzinfo=ET))
    assert rc != 4, "still halting on an unmarkable position"
    assert mcp.closed, "an unpriceable position was neither priced nor closed"


def test_positions_are_flattened_on_expiry_day(runner, monkeypatch, tmp_path):
    """Never hold through expiry: an in-the-money short leg is assigned, and one
    assignment turns a defined-risk condor into naked stock."""
    seed_book(tmp_path)
    account = [{"symbol": l["symbol"], "qty": str(-4 if l["side"] == "sell" else 4),
                "market_value": "0", "unrealized_pl": "0"} for l in LEGS]
    rc, mcp = drive(runner, monkeypatch, tmp_path, positions=account,
                    now=dt.datetime(2026, 9, 3, 14, 5, tzinfo=ET))
    assert mcp.closed, "carried a position into expiry"


def test_before_the_close_out_time_the_position_is_held(runner, monkeypatch, tmp_path):
    seed_book(tmp_path)
    account = [{"symbol": l["symbol"], "qty": str(-4 if l["side"] == "sell" else 4),
                "market_value": "0", "unrealized_pl": "0"} for l in LEGS]
    rc, mcp = drive(runner, monkeypatch, tmp_path, positions=account,
                    now=dt.datetime(2026, 9, 3, 11, 0, tzinfo=ET))
    assert mcp.closed == [], "closed hours earlier than necessary"


def test_an_expired_row_is_retired_and_does_not_block_forever(runner, monkeypatch,
                                                              tmp_path):
    """An expired-worthless condor is the most common outcome of this strategy.
    Leaving its legs in the registry against an empty account made `reconcile`
    mismatch on every later cycle, permanently blocking new positions."""
    seed_book(tmp_path)
    rc, mcp = drive(runner, monkeypatch, tmp_path, positions=[],
                    now=dt.datetime(2026, 9, 4, 10, 0, tzinfo=ET))
    b = Book(tmp_path / "options_book.json")
    assert b.open_entries == [], "expired row still open"
    assert b.reconcile({}) == {}, "expired row still blocks reconciliation"


# --- guards that must still fire ----------------------------------------

def test_assignment_halts_the_agent(runner, monkeypatch, tmp_path):
    seed_book(tmp_path)
    rc, mcp = drive(runner, monkeypatch, tmp_path,
                    positions=[{"symbol": "SPY", "qty": "-400",
                                "market_value": "-306400", "unrealized_pl": "0"}],
                    now=dt.datetime(2026, 9, 2, 12, 0, tzinfo=ET))
    assert rc == 3
    assert mcp.opened == [], "opened new risk while holding assigned stock"


def test_a_closed_market_places_no_orders(runner, monkeypatch, tmp_path):
    seed_book(tmp_path)
    account = [{"symbol": l["symbol"], "qty": str(-4 if l["side"] == "sell" else 4),
                "market_value": "0", "unrealized_pl": "0"} for l in LEGS]
    rc, mcp = drive(runner, monkeypatch, tmp_path, positions=account,
                    now=dt.datetime(2026, 9, 2, 17, 30, tzinfo=ET))
    assert mcp.closed == [] and mcp.opened == []


def test_nothing_opens_before_the_window_starts(runner, monkeypatch, tmp_path):
    rc, mcp = drive(runner, monkeypatch, tmp_path, positions=[],
                    now=dt.datetime(2026, 8, 27, 12, 0, tzinfo=ET))
    assert mcp.opened == []


def test_a_corrupt_registry_fails_loudly(runner, monkeypatch, tmp_path):
    (tmp_path / "options_book.json").write_text('[{"id": "a", "trunc')
    from trading_bot.options.book import BookCorrupt
    with pytest.raises(BookCorrupt):
        drive(runner, monkeypatch, tmp_path, positions=[],
              now=dt.datetime(2026, 9, 2, 12, 0, tzinfo=ET))


def test_tests_do_not_write_the_production_journal(runner, monkeypatch, tmp_path):
    """Found live: the journal path was hardcoded to ROOT/data, so every test
    run of main() appended its fixture trades to the production journal, and
    the dashboard displayed closes that never happened."""
    prod = ROOT / "data" / "journal.jsonl"
    before = prod.read_text() if prod.exists() else None
    seed_book(tmp_path)
    drive(runner, monkeypatch, tmp_path, positions=[],
          now=dt.datetime(2026, 9, 2, 12, 0, tzinfo=ET))
    after = prod.read_text() if prod.exists() else None
    assert before == after, "a test wrote into the production journal"
    assert (tmp_path / "journal.jsonl").exists(), \
        "the journal should follow the registry's directory"
