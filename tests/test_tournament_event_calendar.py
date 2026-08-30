import datetime as dt

from trading_bot.options.clock import ET
from trading_bot.tournament import event_calendar
from trading_bot.tournament.weekly import CalendarFact, EventTiming


def test_nasdaq_rows_are_normalized_with_source_provenance(monkeypatch):
    monkeypatch.setattr(event_calendar, "_nasdaq_payload", lambda day, **kwargs: {
        "data": {"rows": [
            {"symbol": "AVGO", "time": "time-after-hours",
             "marketCap": "$1,700,000,000,000", "fiscalQuarterEnding": "Jul/2026"},
            {"symbol": "TINY", "time": "time-pre-market",
             "marketCap": "$100,000", "fiscalQuarterEnding": "Jun/2026"},
        ]}})
    rows = event_calendar.nasdaq_earnings_facts(
        dt.date(2026, 9, 2), dt.date(2026, 9, 2))
    assert len(rows) == 1
    assert rows[0].symbol == "AVGO"
    assert rows[0].timing is EventTiming.AFTER_CLOSE
    assert rows[0].source == "nasdaq_calendar"


def test_discovery_preserves_one_feed_when_the_other_fails(monkeypatch):
    row = CalendarFact(
        "AVGO", dt.date(2026, 9, 2), EventTiming.AFTER_CLOSE,
        "nasdaq_calendar", "nasdaq:avgo", "scheduled")
    monkeypatch.setattr(
        event_calendar, "yahoo_earnings_facts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(
        event_calendar, "nasdaq_earnings_facts",
        lambda *args, **kwargs: (row,))
    result = event_calendar.discover_earnings_calendar(
        dt.date(2026, 9, 1), dt.date(2026, 9, 3))
    assert result.facts == (row,)
    assert len(result.errors) == 1 and "yahoo" in result.errors[0]


def test_calendar_and_alpaca_news_become_bounded_catalyst_facts():
    observed = dt.datetime(2026, 8, 29, 12, tzinfo=ET)
    calendar = CalendarFact(
        "AVGO", dt.date(2026, 9, 2), EventTiming.AFTER_CLOSE,
        "nasdaq_calendar", "nasdaq:avgo", "scheduled")
    fact = event_calendar.calendar_catalyst_fact(calendar, observed_at=observed)
    assert fact.symbols == ("AVGO",) and fact.source == "nasdaq_calendar"
    news = event_calendar.alpaca_news_facts({"news": [{
        "id": 3, "created_at": observed.isoformat(),
        "headline": "AVGO schedules quarterly results",
        "summary": "Earnings are due after close.", "symbols": ["AVGO"],
        "source": "wire",
    }]}, symbol="AVGO")
    assert len(news) == 1 and news[0].fact_id == "alpaca:3"
