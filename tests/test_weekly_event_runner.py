import datetime as dt
import hashlib
import importlib.util
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from trading_bot.options.book import Book, BookEntry
from trading_bot.options.clock import ET
from trading_bot.options.execution import Fill
from trading_bot.tournament.audit import AuditLedger
from trading_bot.tournament.weekly import (
    CalendarFact,
    EventTiming,
    ReplaySummary,
    WeeklyWindow,
    calendar_consensus,
    evaluate_promotion,
    schedule_event,
)
from trading_bot.tournament.weekly_plan import atomic_write_plan


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location(
        "run_weekly_event_agent_under_test",
        ROOT / "scripts" / "run_weekly_event_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NOW = dt.datetime(2026, 9, 2, 15, 30, tzinfo=ET)


class FakeMCP:
    def __init__(self, now, positions=None):
        self.now = now
        self._positions = positions or []

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def account(self):
        return {"account_number": "PA-WEEKLY", "equity": "100000"}
    def positions(self): return self._positions
    def order_by_client_id(self, coid): raise RuntimeError("not found")
    def market_clock(self): return {"is_open": True}
    def stock_snapshot(self, symbol, **kwargs):
        return {symbol: {"latestTrade": {
            "p": 366.0, "t": (self.now - dt.timedelta(seconds=2)).isoformat()}}}
    def option_chain(self, symbol, **kwargs):
        def option(right, bid, ask):
            key = f"AVGO260904{right}00365000"
            return key, {"latestQuote": {
                "bp": bid, "ap": ask, "bs": 20, "as": 20,
                "t": (self.now - dt.timedelta(seconds=2)).isoformat()},
                "impliedVolatility": .7}
        call, put = option("C", 14.0, 14.4), option("P", 14.2, 14.6)
        return {"snapshots": {call[0]: call[1], put[0]: put[1]}}
    def portfolio_history(self, **kwargs): return {"equity": [100000]}
    def account_activities(self, **kwargs): return []


def config(enable=False):
    values = {
        "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s",
        "ALPACA_ACCOUNT_NUMBER": "PA-WEEKLY", "FEATHERLESS_API_KEY": "f",
    }
    if enable:
        values["WEEKLY_EVENT_ENABLE_ORDERS"] = "YES"
    return values


def make_plan(path):
    window = WeeklyWindow(
        dt.datetime(2026, 8, 31, 9, 30, tzinfo=ET),
        dt.datetime(2026, 9, 4, 9, 30, tzinfo=ET))
    facts = [
        CalendarFact("AVGO", dt.date(2026, 9, 2), EventTiming.AFTER_CLOSE,
                     source, f"{source}:avgo", "AVGO earnings schedule")
        for source in ("yahoo_calendar", "nasdaq_calendar")]
    consensus = calendar_consensus(facts)[0]
    schedule = schedule_event(
        consensus,
        sessions=["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02",
                  "2026-09-03", "2026-09-04"],
        expiries=["2026-09-04"], window=window)
    replay = ReplaySummary(
        8, .47, .45, .625, .196, .177, .625, .07, .08)
    promotion = evaluate_promotion(
        consensus=consensus, semantic_confirmed=True, replay=replay,
        schedule=schedule, current_premium_to_spot=.075,
        current_total_spread_pct=.03)
    plan = {
        "mode": "AUTONOMOUS_WEEKLY_RESEARCH_PLAN", "order_enabled": False,
        "generated_at": dt.datetime(2026, 8, 29, 12, tzinfo=ET).isoformat(),
        "window": {"start": window.start.isoformat(),
                   "deadline": window.deadline.isoformat()},
        "account_sha256": hashlib.sha256(b"PA-WEEKLY").hexdigest(),
        "events": [{
            "symbol": "AVGO", "calendar": asdict(consensus),
            "calendar_facts": [asdict(value) for value in facts],
            "schedule": asdict(schedule),
            "semantic": {"event_type": "earnings", "status": "upcoming"},
            "replay": {"summary": asdict(replay)},
            "promotion": asdict(promotion), "max_loss_budget_usd": 25_000,
        }],
    }
    atomic_write_plan(path, plan)


def drive(runner, monkeypatch, tmp_path, now, *, positions=None, enable=False):
    plan = tmp_path / "plan.json"
    make_plan(plan)
    fake = FakeMCP(now, positions)
    monkeypatch.setattr(runner, "MCPClient", lambda *args, **kwargs: fake)
    monkeypatch.setattr(runner, "now_et", lambda: now)
    monkeypatch.setattr(runner, "dotenv_values", lambda path: config(enable))
    book = tmp_path / "book.json"
    ledger = tmp_path / "evidence.jsonl"
    argv = [
        "run", "--plan", str(plan), "--book", str(book),
        "--ledger", str(ledger), "--lock", str(tmp_path / "lock"),
        "--no-auto-plan",
    ]
    if enable:
        argv.append("--enable-orders")
    monkeypatch.setattr(sys, "argv", argv)
    return fake, book, ledger


def test_wait_cycle_reads_sealed_plan_and_records_sponsor_outcome(
        runner, monkeypatch, tmp_path):
    now = dt.datetime(2026, 9, 1, 12, tzinfo=ET)
    _, book, ledger = drive(runner, monkeypatch, tmp_path, now)
    assert runner.main() == 0
    assert Book(book).open_entries == []
    types = [row.event_type for row in AuditLedger(ledger).read()]
    assert types == ["weekly_cycle", "weekly_alpaca_outcome"]


def test_live_entry_rechecks_surface_semantics_and_books_exact_risk(
        runner, monkeypatch, tmp_path):
    _, book, ledger = drive(runner, monkeypatch, tmp_path, NOW, enable=True)
    monkeypatch.setattr(
        runner, "_semantic_recheck",
        lambda *args, **kwargs: (True, {"confirmed": True}))
    monkeypatch.setattr(
        runner, "open_spread",
        lambda *args, **kwargs: Fill(True, 7, 28.95, 1, "partial"))
    assert runner.main() == 0
    entries = Book(book).open_entries
    assert len(entries) == 1 and entries[0].underlying == "AVGO"
    assert entries[0].qty == 7 and entries[0].entry == 28.95
    types = [row.event_type for row in AuditLedger(ledger).read()]
    assert "weekly_surface_decision" in types
    assert "weekly_semantic_recheck" in types
    assert "weekly_live_promotion" in types
    assert "weekly_entry_intent" in types and "weekly_entry_booked" in types


def test_deadline_exit_runs_even_when_reconciliation_disagrees(
        runner, monkeypatch, tmp_path):
    now = dt.datetime(2026, 9, 4, 9, 30, tzinfo=ET)
    positions = [{"symbol": "AVGO260904C00365000", "qty": "1"}]
    _, book_path, ledger = drive(
        runner, monkeypatch, tmp_path, now, positions=positions)
    Book(book_path).add(BookEntry(
        id="avgo-earnings-2026-09-02", underlying="AVGO",
        structure="long_straddle",
        legs=[
            {"symbol": "AVGO260904C00365000", "side": "buy", "ratio_qty": 1},
            {"symbol": "AVGO260904P00365000", "side": "buy", "ratio_qty": 1},
        ], qty=1, entry=29, max_profit=58, max_loss=29,
        opened_at=NOW.isoformat()))
    calls = []
    monkeypatch.setattr(
        runner, "_exit_entry",
        lambda *args, **kwargs: calls.append(kwargs["reason"]) or 0)
    assert runner.main() == 0
    assert calls == ["global weekly deadline"]
    assert any(row.event_type == "weekly_reconciliation_mismatch"
               for row in AuditLedger(ledger).read())


def test_live_flag_still_needs_environment_interlock(runner, monkeypatch, tmp_path):
    drive(runner, monkeypatch, tmp_path, NOW, enable=False)
    sys.argv.append("--enable-orders")
    assert runner.main() == 5


def test_closed_entry_is_never_recovered_from_its_old_intent(
        runner, tmp_path, monkeypatch):
    book = Book(tmp_path / "book.json")
    entry = BookEntry(
        id="avgo-earnings-2026-09-02", underlying="AVGO",
        structure="long_straddle",
        legs=[{"symbol": "AVGO260904C00365000", "side": "buy", "ratio_qty": 1}],
        qty=1, entry=10, max_profit=20, max_loss=10,
        opened_at=NOW.isoformat())
    book.add(entry)
    book.close(entry.id, exit_price=12, reason="done", when=NOW.isoformat())
    ledger = AuditLedger(tmp_path / "ledger.jsonl")
    ledger.append("weekly_entry_intent", {
        "event_id": entry.id, "client_order_id": "entry", "symbol": "AVGO",
        "legs": entry.legs, "debit": 10, "max_profit": 20, "max_loss": 10,
    }, recorded_at=NOW)
    fake = FakeMCP(NOW)
    monkeypatch.setattr(fake, "order_by_client_id", lambda coid: {
        "filled_qty": "1", "filled_avg_price": "10"})
    runner._recover_entries(fake, book, ledger, [{"schedule": {
        "event_id": entry.id}}], NOW)
    assert book.open_entries == []


def test_exit_id_advances_only_after_a_terminal_result(runner, tmp_path):
    ledger = AuditLedger(tmp_path / "ledger.jsonl")
    event = "avgo-earnings-2026-09-02"
    first, fresh = runner._exit_coid(ledger, event)
    assert fresh
    ledger.append("weekly_exit_intent", {
        "event_id": event, "client_order_id": first}, recorded_at=NOW)
    retry, fresh = runner._exit_coid(ledger, event)
    assert retry == first and not fresh
    ledger.append("weekly_exit_result", {
        "event_id": event, "client_order_id": first,
        "fill": {"how": "unfilled"}}, recorded_at=NOW)
    second, fresh = runner._exit_coid(ledger, event)
    assert fresh and second != first
