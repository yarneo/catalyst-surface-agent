"""Tests for the MCP execution client.

The failure that matters: an order reaching the broker when it should not, or
reaching it twice. Both are unrecoverable in a way a wrong signal is not.

Run: uv run python -m pytest tests/ -q
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from trading_bot.options.mcp import MCPClient, MCPError

LEGS = [{"symbol": "SPY260918C00500000", "side": "sell", "ratio_qty": "1"},
        {"symbol": "SPY260918C00510000", "side": "buy", "ratio_qty": "1"}]


def _client(live=False):
    c = MCPClient("k", "s", live=live)
    c._proc = object()          # pretend it started; no tool call is made below
    return c


# --------------------------------------------------- read-only by default


def test_orders_are_refused_unless_live():
    """The whole agent must be runnable end-to-end against real market data
    without the ability to trade. Read-only is the default, not a mode."""
    with pytest.raises(MCPError, match="read-only"):
        _client(live=False).place_spread(LEGS, 1)


def test_cancel_is_also_refused_unless_live():
    with pytest.raises(MCPError, match="read-only"):
        _client(live=False).cancel("abc")


def test_live_flag_must_be_explicit():
    assert MCPClient("k", "s").live is False


# ------------------------------------------------------------ idempotency


def test_every_order_carries_a_client_order_id(monkeypatch):
    """A timed-out request that is retried without an idempotency key can
    double-fill. A four-leg condor submitted twice is not a duplicate — it is
    twice the size with none of the intended risk."""
    seen = {}
    c = _client(live=True)
    monkeypatch.setattr(c, "call_tool", lambda n, a: seen.update(a) or {"ok": True})
    c.place_spread(LEGS, 1)
    assert seen["client_order_id"].startswith("vrp-")


def test_supplied_idempotency_key_is_preserved(monkeypatch):
    """Retrying must reuse the SAME key, or it is not a retry."""
    seen = {}
    c = _client(live=True)
    monkeypatch.setattr(c, "call_tool", lambda n, a: seen.update(a) or {})
    c.place_spread(LEGS, 1, client_order_id="fixed-key-1")
    assert seen["client_order_id"] == "fixed-key-1"


def test_two_orders_get_different_keys(monkeypatch):
    keys = []
    c = _client(live=True)
    monkeypatch.setattr(c, "call_tool",
                        lambda n, a: keys.append(a["client_order_id"]) or {})
    c.place_spread(LEGS, 1)
    c.place_spread(LEGS, 1)
    assert keys[0] != keys[1]


# ------------------------------------------------------------- leg limits


def test_more_than_four_legs_is_rejected():
    """Alpaca caps multi-leg at 4. Sending 5 fails at the broker, but by then
    the intent is ambiguous — catch it here."""
    c = _client(live=True)
    with pytest.raises(MCPError, match="1-4 legs"):
        c.place_spread(LEGS * 3, 1)


def test_empty_legs_rejected():
    c = _client(live=True)
    with pytest.raises(MCPError, match="1-4 legs"):
        c.place_spread([], 1)


# ------------------------------------------------------------ order shape


def test_limit_orders_send_a_price_and_market_orders_do_not(monkeypatch):
    seen = {}
    c = _client(live=True)
    monkeypatch.setattr(c, "call_tool", lambda n, a: seen.update(a) or {})
    c.place_spread(LEGS, 2, limit_price=1.234)
    assert seen["type"] == "limit" and seen["limit_price"] == "1.23"
    assert seen["qty"] == "2" and seen["order_class"] == "mleg"

    seen.clear()
    c.place_spread(LEGS, 1)
    assert seen["type"] == "market" and "limit_price" not in seen


def test_time_in_force_is_day(monkeypatch):
    """Options support only 'day'. Anything else is rejected by the broker."""
    seen = {}
    c = _client(live=True)
    monkeypatch.setattr(c, "call_tool", lambda n, a: seen.update(a) or {})
    c.place_spread(LEGS, 1)
    assert seen["time_in_force"] == "day"


def test_calling_before_start_raises():
    with pytest.raises(MCPError, match="not started"):
        MCPClient("k", "s")._rpc("tools/list", {})


def test_rpc_timeout_is_real_when_the_child_is_silent():
    """A blocking pipe read used to make the advertised timeout unreachable."""
    class Sink:
        def write(self, value): pass
        def flush(self): pass

    class Silent:
        def readline(self):
            time.sleep(1.0)
            return ""

    c = MCPClient("k", "s", timeout=0.05)
    c._proc = SimpleNamespace(stdin=Sink(), stdout=Silent())
    c._drain_stdout()
    started = time.monotonic()
    with pytest.raises(MCPError, match="timed out"):
        c._rpc("tools/list", {})
    assert time.monotonic() - started < 0.25


# ------------------------------------------------ errors hidden in payloads


def test_payload_error_raises_even_though_the_call_succeeded(monkeypatch):
    """Alpaca returns HTTP 200 with isError UNSET and the failure inside the
    payload. Observed live: a duplicate client_order_id came back as
    {"error": {"http_status": 422, "detail": {"message": "client_order_id must
    be unique"}}}. Without this check a REJECTED order reads as a filled one,
    and the agent sizes its next trade against a position it does not hold."""
    c = MCPClient("k", "s", live=True)
    c._proc = object()
    monkeypatch.setattr(c, "_rpc", lambda m, p: {
        "structuredContent": {"data": {"error": {
            "message": "API rejected the order", "http_status": 422,
            "detail": {"code": 40010001, "message": "client_order_id must be unique"}}}}})
    with pytest.raises(MCPError, match="client_order_id must be unique"):
        c.call_tool("place_option_order", {})


def test_successful_payload_still_returns_normally(monkeypatch):
    c = MCPClient("k", "s")
    c._proc = object()
    monkeypatch.setattr(c, "_rpc", lambda m, p: {
        "structuredContent": {"data": {"id": "abc", "status": "new"}}})
    assert c.call_tool("get_order_by_id", {})["id"] == "abc"


def test_tournament_read_wrappers_use_the_sponsor_tool_surface(monkeypatch):
    calls = []
    c = _client()
    monkeypatch.setattr(c, "call_tool",
                        lambda name, args=None: calls.append((name, args or {})) or {})
    c.market_clock()
    c.news(limit=5, symbols="AMD")
    c.market_movers(market_type="stocks", top=20)
    c.most_active(by="trades", top=15)
    c.stock_bars("AMD,NVDA", timeframe="5Min", feed="iex")
    c.stock_snapshot("AMD", feed="iex")
    c.option_chain("AMD", expiration_date="2026-09-04")
    c.option_contracts(underlying_symbols="AMD", status="inactive")
    c.option_bars("AMD260904C00100000", "5Min", limit=100)
    c.option_trades("AMD260904C00100000", limit=100)
    c.option_latest_quote("AMD260904C00100000")
    c.option_latest_trade("AMD260904C00100000")
    c.option_snapshot("AMD260904C00100000")
    c.portfolio_history(period="1D", timeframe="5Min")
    c.account_activities(activity_types=["FILL"])
    assert [name for name, _ in calls] == [
        "get_clock", "get_news", "get_market_movers", "get_most_active_stocks",
        "get_stock_bars", "get_stock_snapshot", "get_option_chain",
        "get_option_contracts", "get_option_bars", "get_option_trades",
        "get_option_latest_quote", "get_option_latest_trade",
        "get_option_snapshot", "get_portfolio_history", "get_account_activities",
    ]
    assert calls[6][1]["underlying_symbol"] == "AMD"
    assert calls[8][1]["timeframe"] == "5Min"


def test_a_field_literally_named_error_that_is_not_a_dict_is_not_treated_as_failure(monkeypatch):
    """Only a structured error object counts. A string field called 'error'
    must not abort an otherwise-valid response."""
    c = MCPClient("k", "s")
    c._proc = object()
    monkeypatch.setattr(c, "_rpc", lambda m, p: {
        "structuredContent": {"data": {"id": "x", "error": None}}})
    assert c.call_tool("get_order_by_id", {})["id"] == "x"


def test_close_spread_rejects_opening_intent():
    """A closing order carrying to_open doubles the position instead of
    flattening it — the single most expensive way this can go wrong."""
    import pytest
    from trading_bot.options.mcp import MCPClient, MCPError
    c = MCPClient("k", "s", live=True)
    legs = [{"symbol": "X", "side": "buy", "ratio_qty": "1",
             "position_intent": "buy_to_open"}]
    with pytest.raises(MCPError, match="non-closing intent"):
        c.close_spread(legs, 1, limit_price=0.20)
