import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

from trading_bot.options.book import Book, BookEntry
from trading_bot.options.clock import ET
from trading_bot.options.execution import Fill
from trading_bot.tournament.audit import AuditLedger
from trading_bot.tournament.catalyst import CatalystAssessment, CatalystFact
from trading_bot.tournament.featherless import CommitteeResult


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location(
        "run_tournament_agent_under_test",
        ROOT / "scripts" / "run_event_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeMCP:
    def __init__(self, now, positions=None):
        self.now = now
        self._positions = positions or []
        self.opened = []

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def account(self):
        return {
            "account_number": "TEST-ACCOUNT", "equity": "100000",
            "options_approved_level": 3, "options_trading_level": 3,
            "trading_blocked": False, "account_blocked": False,
        }
    def positions(self): return self._positions
    def market_clock(self): return {"is_open": True}
    def stock_snapshot(self, symbol, **filters):
        return {symbol: {"latestTrade": {
            "p": 366.0, "t": (self.now - dt.timedelta(seconds=2)).isoformat()}}}
    def option_chain(self, underlying, **filters):
        def row(right, bid, ask):
            symbol = f"AVGO260904{right}00365000"
            return symbol, {"latestQuote": {
                "bp": bid, "ap": ask, "bs": 20, "as": 20,
                "t": (self.now - dt.timedelta(seconds=2)).isoformat()},
                "impliedVolatility": 0.72}
        call = row("C", 14.0, 14.4)
        put = row("P", 14.2, 14.6)
        return {"snapshots": {call[0]: call[1], put[0]: put[1]}}
    def portfolio_history(self, **filters): return {"equity": [100000]}
    def account_activities(self, **filters): return []
    def order_by_client_id(self, coid): raise RuntimeError("not found")


def config(enable=False):
    values = {
        "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s",
        "ALPACA_ACCOUNT_NUMBER": "TEST-ACCOUNT", "ALPACA_INITIAL_EQUITY": "100000",
        "ALPACA_OPTIONS_LEVEL": "3", "FEATHERLESS_API_KEY": "f",
    }
    if enable:
        values["TOURNAMENT_ENABLE_ORDERS"] = "YES"
    return values


def assessment():
    return CatalystAssessment(
        catalyst_type="scheduled earnings",
        factual_summary="AVGO remains scheduled to report.", novelty=0.5,
        surprise=0.2, direction="unknown", expected_half_life_minutes=300,
        primary_tickers=("AVGO",), secondary_tickers=(), causal_links=(),
        confidence=0.8, invalidation="Results are released early.",
        source_fact_ids=("official:avgo-q3-fy2026",))


def drive(runner, monkeypatch, tmp_path, now, *, positions=None, argv=None,
          env=None):
    fake = FakeMCP(now, positions)
    monkeypatch.setattr(runner, "MCPClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(runner, "now_et", lambda: now)
    monkeypatch.setattr(runner, "dotenv_values", lambda path: env or config())
    book = tmp_path / "book.json"
    ledger = tmp_path / "evidence.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "run", "--book", str(book), "--ledger", str(ledger), *(argv or [])])
    return runner.main(), fake, Book(book), AuditLedger(ledger)


def test_wait_cycle_is_read_only_and_records_alpaca_outcome(
        runner, monkeypatch, tmp_path):
    now = dt.datetime(2026, 9, 1, 12, 0, tzinfo=ET)
    rc, fake, book, ledger = drive(runner, monkeypatch, tmp_path, now)
    assert rc == 0 and fake.opened == [] and book.open_entries == []
    assert [row.event_type for row in ledger.read()] == ["cycle", "alpaca_outcome"]


def test_entry_window_shadow_runs_all_gates_without_consuming_live_attempt(
        runner, monkeypatch, tmp_path):
    now = dt.datetime(2026, 9, 2, 15, 30, tzinfo=ET)
    fact = CatalystFact(
        "official:avgo-q3-fy2026", "2026-08-28T00:00:00-04:00",
        "Broadcom to announce results", "Event remains scheduled.", ("AVGO",))
    result = CommitteeResult((), assessment(), 1.0, "2/2 agree unknown")
    monkeypatch.setattr(runner, "_committee",
                        lambda *args, **kwargs: ([fact], result, "hash"))
    rc, _, book, ledger = drive(runner, monkeypatch, tmp_path, now)
    types = [row.event_type for row in ledger.read()]
    assert rc == 0 and book.open_entries == []
    assert "surface_decision" in types and "event_integrity" in types
    assert "entry_plan" in types and "shadow_entry" in types
    assert "entry_intent" not in types
    assert not runner._entry_was_attempted(ledger)
    shadow = next(row for row in ledger.read() if row.event_type == "shadow_entry")
    assert shadow.payload["qty"] == 13
    assert shadow.payload["total_max_loss_usd"] == pytest.approx(37_726)


def test_exit_clock_still_runs_when_reconciliation_blocks_new_entries(
        runner, monkeypatch, tmp_path):
    now = dt.datetime(2026, 9, 3, 9, 45, tzinfo=ET)
    book_path = tmp_path / "book.json"
    Book(book_path).add(BookEntry(
        id="avgo-1", underlying="AVGO", structure="long_straddle",
        legs=[{"symbol": "AVGO260904C00365000", "side": "buy", "ratio_qty": 1},
              {"symbol": "AVGO260904P00365000", "side": "buy", "ratio_qty": 1}],
        qty=1, entry=29.0, max_profit=58.0, max_loss=29.0,
        opened_at="2026-09-02T15:30:00-04:00"))
    called = []
    monkeypatch.setattr(
        runner, "_committee",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("deadline exit must not call Featherless")))
    monkeypatch.setattr(
        runner, "_latest_spot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("deadline exit must not inspect stock timestamps")))
    monkeypatch.setattr(runner, "_exit",
                        lambda *args, **kwargs: called.append(kwargs["reason"]) or 0)
    # Broker reports only one of two legs: mismatch must block entry, not exit.
    positions = [{"symbol": "AVGO260904C00365000", "qty": "1"}]
    exit_env = config()
    exit_env.pop("FEATHERLESS_API_KEY")
    rc, _, _, ledger = drive(
        runner, monkeypatch, tmp_path, now, positions=positions, env=exit_env)
    assert rc == 0 and called == ["frozen post-earnings 09:45 exit"]
    assert any(row.event_type == "reconciliation_mismatch" for row in ledger.read())


def test_emergency_flat_deadline_is_explicit_and_still_retries_exit(
        runner, monkeypatch, tmp_path):
    now = runner.POLICY.emergency_flat_by
    book_path = tmp_path / "book.json"
    legs = [
        {"symbol": "AVGO260904C00365000", "side": "buy", "ratio_qty": 1},
        {"symbol": "AVGO260904P00365000", "side": "buy", "ratio_qty": 1},
    ]
    Book(book_path).add(BookEntry(
        id="avgo-1", underlying="AVGO", structure="long_straddle", legs=legs,
        qty=1, entry=29.0, max_profit=58.0, max_loss=29.0,
        opened_at="2026-09-02T15:30:00-04:00"))
    called = []
    monkeypatch.setattr(runner, "_exit",
                        lambda *args, **kwargs: called.append(kwargs["reason"]) or 2)
    positions = [{"symbol": leg["symbol"], "qty": "1"} for leg in legs]
    rc, _, _, ledger = drive(
        runner, monkeypatch, tmp_path, now, positions=positions)
    cycle = next(row for row in ledger.read() if row.event_type == "cycle")
    assert rc == 2
    assert called == ["emergency flat-by deadline"]
    assert cycle.payload["exit_deadline_state"] == "emergency_flat_by"


def test_completed_unfilled_exit_gets_fresh_client_order_ids(
        runner, monkeypatch, tmp_path):
    now = runner.POLICY.exit_at
    book = Book(tmp_path / "book.json")
    book.add(BookEntry(
        id="avgo-1", underlying="AVGO", structure="long_straddle",
        legs=[{"symbol": "AVGO260904C00365000", "side": "buy", "ratio_qty": 1},
              {"symbol": "AVGO260904P00365000", "side": "buy", "ratio_qty": 1}],
        qty=1, entry=29.0, max_profit=58.0, max_loss=29.0,
        opened_at="2026-09-02T15:30:00-04:00"))
    ledger = AuditLedger(tmp_path / "evidence.jsonl")
    client_ids = []
    fills = [Fill(False, 0, None, 4, "unfilled"),
             Fill(True, 1, 30.0, 1, "market")]

    def close(*args, **kwargs):
        client_ids.append(kwargs["client_order_id"])
        return fills.pop(0)

    monkeypatch.setattr(runner, "close_spread", close)
    assert runner._exit(FakeMCP(now), book, ledger, now,
                        enable_orders=True, reason="test") == 2
    assert runner._exit(FakeMCP(now), book, ledger,
                        now + dt.timedelta(minutes=1),
                        enable_orders=True, reason="test") == 0
    assert client_ids[0] != client_ids[1]
    assert not book.open_entries


def test_unknown_exit_order_reuses_client_order_ids_until_resolved(
        runner, monkeypatch, tmp_path):
    now = runner.POLICY.exit_at
    book = Book(tmp_path / "book.json")
    book.add(BookEntry(
        id="avgo-1", underlying="AVGO", structure="long_straddle",
        legs=[{"symbol": "AVGO260904C00365000", "side": "buy", "ratio_qty": 1},
              {"symbol": "AVGO260904P00365000", "side": "buy", "ratio_qty": 1}],
        qty=1, entry=29.0, max_profit=58.0, max_loss=29.0,
        opened_at="2026-09-02T15:30:00-04:00"))
    ledger = AuditLedger(tmp_path / "evidence.jsonl")
    client_ids = []
    fills = [Fill(False, 0, None, 1, "stuck"),
             Fill(True, 1, 30.0, 1, "market")]

    def close(*args, **kwargs):
        client_ids.append(kwargs["client_order_id"])
        return fills.pop(0)

    monkeypatch.setattr(runner, "close_spread", close)
    assert runner._exit(FakeMCP(now), book, ledger, now,
                        enable_orders=True, reason="test") == 2
    assert runner._exit(FakeMCP(now), book, ledger,
                        now + dt.timedelta(minutes=1),
                        enable_orders=True, reason="test") == 0
    assert client_ids[0] == client_ids[1]
    assert any(row.event_type == "exit_retry" for row in ledger.read())
    assert not book.open_entries


def test_post_event_hold_records_featherless_semantics_without_changing_exit(
        runner, monkeypatch, tmp_path):
    now = dt.datetime(2026, 9, 2, 16, 30, tzinfo=ET)
    book_path = tmp_path / "book.json"
    legs = [
        {"symbol": "AVGO260904C00365000", "side": "buy", "ratio_qty": 1},
        {"symbol": "AVGO260904P00365000", "side": "buy", "ratio_qty": 1},
    ]
    Book(book_path).add(BookEntry(
        id="avgo-1", underlying="AVGO", structure="long_straddle", legs=legs,
        qty=1, entry=29.0, max_profit=58.0, max_loss=29.0,
        opened_at="2026-09-02T15:30:00-04:00"))
    facts = [CatalystFact(
        "official:avgo-q3-fy2026", "2026-08-28T00:00:00-04:00",
        "Broadcom reported quarterly results", "Guidance was issued.", ("AVGO",))]
    result = CommitteeResult((), assessment(), 1.0, "2/2 agree unknown")
    monkeypatch.setattr(runner, "_committee",
                        lambda *args, **kwargs: (facts, result, "post-hash"))
    positions = [{"symbol": leg["symbol"], "qty": "1"} for leg in legs]
    rc, _, book, ledger = drive(
        runner, monkeypatch, tmp_path, now, positions=positions)
    assert rc == 0 and len(book.open_entries) == 1
    row = next(row for row in ledger.read()
               if row.event_type == "post_event_semantics")
    assert row.payload["fact_hash"] == "post-hash"


def test_order_enable_flag_also_requires_frozen_environment_switch(
        runner, monkeypatch, tmp_path):
    now = dt.datetime(2026, 9, 2, 15, 30, tzinfo=ET)
    rc, fake, _, _ = drive(
        runner, monkeypatch, tmp_path, now, argv=["--enable-orders"], env=config())
    assert rc == 5 and fake.opened == []


def test_live_entry_records_actual_fill_before_next_cycle(
        runner, monkeypatch, tmp_path):
    now = dt.datetime(2026, 9, 2, 15, 30, tzinfo=ET)
    fact = CatalystFact(
        "official:avgo-q3-fy2026", "2026-08-28T00:00:00-04:00",
        "Broadcom to announce results", "Event remains scheduled.", ("AVGO",))
    result = CommitteeResult((), assessment(), 1.0, "2/2 agree unknown")
    monkeypatch.setattr(runner, "_committee",
                        lambda *args, **kwargs: ([fact], result, "hash"))
    monkeypatch.setattr(
        runner, "open_spread",
        lambda *args, **kwargs: Fill(True, 7, 28.95, 1, "partial"))
    rc, _, book, ledger = drive(
        runner, monkeypatch, tmp_path, now, argv=["--enable-orders"],
        env=config(enable=True))
    assert rc == 0 and len(book.open_entries) == 1
    assert book.open_entries[0].qty == 7
    assert book.open_entries[0].entry == 28.95
    types = [row.event_type for row in ledger.read()]
    assert "entry_intent" in types and "entry_result" in types
    assert "entry_booked" in types and "shadow_entry" not in types


def test_latest_stock_trade_must_be_fresh(runner):
    old = dt.datetime(2026, 9, 2, 15, 20, tzinfo=ET)
    now = dt.datetime(2026, 9, 2, 15, 30, tzinfo=ET)
    with pytest.raises(ValueError, match="stale"):
        runner._latest_spot({"AVGO": {"latestTrade": {
            "p": 366, "t": old.isoformat()}}}, now=now)


def test_latest_stock_trade_allows_bounded_forward_clock_skew(runner):
    now = dt.datetime(2026, 9, 2, 15, 20, tzinfo=ET)
    snapshot = {"AVGO": {"latestTrade": {
        "p": 366, "t": (now + dt.timedelta(seconds=3)).isoformat()}}}

    assert runner._latest_spot(snapshot, now=now) == 366

    snapshot["AVGO"]["latestTrade"]["t"] = (
        now + dt.timedelta(seconds=11)).isoformat()
    with pytest.raises(ValueError, match="future-dated"):
        runner._latest_spot(snapshot, now=now)
