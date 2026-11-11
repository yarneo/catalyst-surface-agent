from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from decimal import Decimal
import json
import pytest

from trading_bot.broker.history import all_orders, holdings_reconciliation
from trading_bot.broker.alpaca_client import AlpacaBroker
from trading_bot.live.executor import ExecutorState
from trading_bot.attribution import summarize_execution_quality


def test_order_pagination_overlaps_and_deduplicates_boundary():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [NS(id=str(i), submitted_at=start+timedelta(seconds=i)) for i in range(12)]
    class Client:
        def get_orders(self, filter):
            return list(reversed([r for r in rows if filter.until is None or r.submitted_at < filter.until]))[:filter.limit]
    assert len(all_orders(Client(), page_size=5)) == 12


def test_ambiguous_pagination_fails_instead_of_claiming_complete():
    rows = [NS(id=str(i), submitted_at=datetime(2026,1,1,tzinfo=timezone.utc)) for i in range(5)]
    with pytest.raises(RuntimeError, match="stalled"):
        all_orders(NS(get_orders=lambda **kw: rows), page_size=5)


def test_holdings_mismatch_is_not_hidden():
    result = holdings_reconciliation([{"symbol":"QQQ", "side":"buy", "qty":1.0001}], [NS(symbol="QQQ",qty="1")])
    assert result["status"] == "MISMATCH"


def test_partial_canceled_order_retains_all_actual_fills():
    from trading_bot.broker.history import fill_ledger
    base={"activity_type":"FILL","order_id":"a","symbol":"QQQ","side":"buy",
          "transaction_time":"2026-11-11T15:00:00Z"}
    rows=fill_ledger([{**base,"qty":".25","price":"100"},
                     {**base,"qty":".5","price":"103"}], [NS(id="a",status="canceled")])
    assert rows[0]["qty"] == .75
    assert rows[0]["price"] == 102
    assert rows[0]["status"] == "canceled"


def test_execution_coverage_reports_missing_old_fills_and_deduplicates():
    intent = {"order_id":"a", "reference_price":100, "side":"buy"}
    fills = [{"order_id":key,"filled_avg_price":101,"filled_qty":1} for key in ("a","b")]
    result = summarize_execution_quality([intent,intent], fills)
    assert result["measured_orders"] == 1
    assert result["coverage_fraction"] == .5
    assert result["uncovered_filled_orders"] == 1


def test_partial_state_corruption_recovers_without_authorizing_buys(tmp_path):
    p = tmp_path / "state.json"
    ExecutorState(peak_equity=123456, session_date="2026-11-11").save(p)
    p.write_text("broken")
    state = ExecutorState.load(p, require_existing=True)
    assert state.peak_equity == 123456
    assert state.halted_today


def test_buy_cap_clamps_instead_of_rejecting_rounding_at_boundary():
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.get_account = lambda: NS(portfolio_value="100000", cash="80000")
    broker.get_positions = lambda: [NS(symbol="QLD",market_value="20000")]
    broker.open_orders = lambda: []
    broker.cash_reserve, broker.bil_reserve, broker.max_leverage = .02,.10,1
    qty = broker._approve_buy("QLD", Decimal(500), 100)
    assert float(qty) * 100 * 1.005 + 20000 <= 70000.00001
    assert qty > 490


def test_fractional_exit_uses_exact_available_broker_quantity(monkeypatch):
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._client=NS(get_open_position=lambda s:NS(qty="41.7601",qty_available="41.7601",current_price="100"))
    broker.quote=lambda s:{"bid":99.99,"ask":100.01}
    requests=[]
    broker.orders=NS(execute=lambda request,**kw:(requests.append(request) or NS(id="test")))
    monkeypatch.setattr("trading_bot.broker.alpaca_client.append_execution_intent",lambda *a,**kw:None)
    broker.close_position("QQQ")
    assert Decimal(str(requests[0].qty)) == Decimal("41.7601")


def test_failed_bil_funding_cannot_continue_to_purchase():
    from trading_bot.broker.orders import ExecutionFailure
    broker=AlpacaBroker.__new__(AlpacaBroker)
    broker.cash_reserve,broker.bil_reserve,broker.max_leverage=.02,.1,1
    broker.get_account=lambda:NS(portfolio_value=100000,cash=1000)
    broker.get_positions=lambda:[NS(symbol="BIL",market_value=30000)]
    broker.open_orders=lambda:[]
    broker._client=NS(get_open_position=lambda s:NS(qty_available=300))
    broker.quote=lambda s:{"bid":100,"ask":100}
    sides=[]
    def rejected(symbol,qty,side,**kw):
        sides.append((symbol,side))
        raise ExecutionFailure("funding rejected")
    broker.submit_market_order=rejected
    with pytest.raises(ExecutionFailure,match="funding rejected"):
        broker._approve_buy("QLD",Decimal(100),100)
    assert sides == [("BIL","sell")]


def test_account_uses_broker_equity_without_modeled_income(monkeypatch):
    from web import server
    monkeypatch.setattr(server, "_broker", lambda:NS(
        get_account=lambda:NS(portfolio_value="90000",last_equity="91000",cash="2000",buying_power="180000"),
        get_clock=lambda:NS(is_open=True)))
    monkeypatch.setattr(server, "_total_sim_div_to", lambda _: 900)
    server.account_snapshot.cache_clear()
    result = server.account_snapshot()
    assert result["equity"] == 90000
    assert result["last_equity"] == 91000


def test_cache_arguments_are_independent():
    from web.server import _ttl_cache
    @_ttl_cache(100, warm=False)
    def f(limit=25): return limit
    assert f() == 25
    assert f(500) == 500
    assert f.refresh() == 25
    assert f(500) == 500


def test_dashboard_regime_uses_completed_window(monkeypatch):
    import pandas as pd
    from web import server
    from trading_bot import marketdata
    end = pd.Timestamp("2026-11-10")
    idx = pd.bdate_range(end - pd.Timedelta(days=460), end)
    frame = pd.DataFrame({"Close": 100.0}, index=idx)
    frame.loc[end, "Close"] = 110.0
    frame.loc[end + pd.Timedelta(days=1), "Close"] = 1.0
    monkeypatch.setattr(marketdata, "last_completed_session", lambda: end.date())
    monkeypatch.setattr(marketdata.yf, "Ticker", lambda _: NS(history=lambda **kw: frame))
    server.regime_snapshot.cache_clear()
    result = server.regime_snapshot()
    assert result["as_of"] == "2026-11-10"
    assert result["regime_on"]
    assert result["close"] == 110


def test_release_allowlist_excludes_secrets_and_mutable_data(tmp_path):
    from scripts.build_portfolio_release import release_files
    (tmp_path / "src/trading_bot/data").mkdir(parents=True)
    (tmp_path / "src/trading_bot/data/loader.py").write_text("# code")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/book.json").write_text("{}")
    (tmp_path / ".env.secret").write_text("not-a-real-key")
    assert [p.relative_to(tmp_path).as_posix() for p in release_files(tmp_path)] == ["src/trading_bot/data/loader.py"]


def test_private_backup_restores_bytes_and_restricts_permissions(tmp_path):
    from scripts.portfolio_backup import backup
    root = tmp_path / "runtime"
    (root / "data").mkdir(parents=True)
    (root / "data/state.json").write_text('{"peak_equity":100}')
    (root / ".env").write_text("placeholder")
    result = backup(root, tmp_path / "backups")
    assert result["status"] == "RESTORE_VERIFIED"
    assert (root / ".env").stat().st_mode & 0o777 == 0o600


def test_release_install_and_rollback_preserve_credentials_and_state(tmp_path):
    import hashlib, tarfile, io
    from scripts.install_portfolio_release import install, rollback_release
    root = tmp_path / "runtime"
    (root / "scripts").mkdir(parents=True)
    (root / "data").mkdir()
    (root / ".env").write_text("private-placeholder")
    (root / "data/state.json").write_text('{"peak_equity":123456}')
    (root / "scripts/example.py").write_text("old = 1\n")
    code = b"new = 2\n"
    manifest={"release_id":"abcdef","source_commit":"test", "source_dirty":False,
              "files":{"scripts/example.py":hashlib.sha256(code).hexdigest()}}
    archive=tmp_path / "release.tar.gz"
    with tarfile.open(archive,"w:gz") as tar:
        for name,value in [("scripts/example.py",code),("release-manifest.json",json.dumps(manifest).encode())]:
            info=tarfile.TarInfo(name); info.size=len(value)
            tar.addfile(info,io.BytesIO(value))
    install(archive,root)
    assert (root / "scripts/example.py").read_bytes()==code
    rollback_release("abcdef",root)
    assert (root / "scripts/example.py").read_text()=="old = 1\n"
    assert (root / ".env").read_text()=="private-placeholder"
    assert json.loads((root / "data/state.json").read_text())["peak_equity"]==123456


def test_close_dispatch_follows_exchange_half_day(monkeypatch,tmp_path):
    from scripts import portfolio_close as close
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(close,"DATA",tmp_path)
    calls=[]
    monkeypatch.setattr(close.subprocess,"run",lambda *a,**kw:(calls.append(a) or NS(returncode=0)))
    monkeypatch.setattr(close,"now_et",lambda:datetime(2026,11,27,13,4,tzinfo=ZoneInfo("America/New_York")))
    assert close.main()==0 and not calls
    monkeypatch.setattr(close,"now_et",lambda:datetime(2026,11,27,13,5,tzinfo=ZoneInfo("America/New_York")))
    assert close.main()==0 and len(calls)==1
    (tmp_path / "portfolio_cycles.json").write_text(json.dumps({"phases":{"close":{"last_success_at":"2026-11-27T18:05:00+00:00"}}}))
    assert close.main()==0 and len(calls)==1
