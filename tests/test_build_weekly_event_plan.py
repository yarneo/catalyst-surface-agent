import datetime as dt

import scripts.build_weekly_event_plan as planner
from trading_bot.options.clock import ET
from trading_bot.tournament.event_calendar import CalendarDiscovery
from trading_bot.tournament.weekly import CalendarFact, EventTiming, WeeklyWindow


class _MCP:
    def __init__(self):
        self.news_symbols = []

    def calendar(self, start, end):
        return []

    def news(self, *, symbols, **kwargs):
        self.news_symbols.append(symbols)
        return {"news": [{
            "id": 17,
            "created_at": "2026-09-01T12:00:00-04:00",
            "headline": "AVGO earnings preview",
            "summary": "Analysts discuss the coming quarter.",
            "symbols": ["AVGO"],
            "source": "wire",
        }]}

    def account(self):
        return {"equity": "100000", "account_number": "redacted"}


def test_single_calendar_source_still_triggers_independent_news_research(monkeypatch):
    fact = CalendarFact(
        "AVGO", dt.date(2026, 9, 2), EventTiming.AFTER_CLOSE,
        "nasdaq_calendar", "nasdaq:avgo", "scheduled")
    monkeypatch.setattr(
        planner, "discover_earnings_calendar",
        lambda *args, **kwargs: CalendarDiscovery(
            (fact,), (), (("yahoo", 0), ("nasdaq", 1)),
            ("yahoo returned no candidates",)))
    monkeypatch.setattr(planner, "_committee", lambda *args, **kwargs: ({}, []))

    mcp = _MCP()
    plan = planner.build_plan(
        mcp=mcp,
        featherless_key="test",
        window=WeeklyWindow(
            dt.datetime(2026, 8, 31, 9, 30, tzinfo=ET),
            dt.datetime(2026, 9, 4, 15, 30, tzinfo=ET)),
        observed_at=dt.datetime(2026, 8, 31, 10, tzinfo=ET),
        symbols=("AVGO",),
        historical_events=8,
        minimum_market_cap=2_000_000_000,
    )

    assert mcp.news_symbols == ["AVGO"]
    assert plan["events"][0]["news_fact_count"] == 1
    assert not plan["events"][0]["calendar"]["confirmed"]
    assert plan["calendar"]["provider_fact_counts"] == {
        "yahoo": 0, "nasdaq": 1,
    }
