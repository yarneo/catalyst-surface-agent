import datetime as dt

import pytest

from trading_bot.options.clock import ET
from trading_bot.tournament.event_replay import (
    EventReplayRow,
    HistoricalEvent,
    replay_long_straddles,
    summarize_replays,
)
from trading_bot.tournament.weekly import EventTiming


class FakeMCP:
    def calendar(self, start, end):
        return [{"date": value} for value in (
            "2026-05-27", "2026-05-28", "2026-05-29")]

    def stock_bars(self, symbol, **kwargs):
        return {"bars": {symbol: [{
            "t": dt.datetime(2026, 5, 28, 20, tzinfo=dt.timezone.utc).isoformat(),
            "c": 100.0,
        }]}}

    def option_contracts(self, **kwargs):
        return {"option_contracts": [
            {"symbol": "TEST260529C00100000", "expiration_date": "2026-05-29",
             "strike_price": "100", "type": "call", "multiplier": "100"},
            {"symbol": "TEST260529P00100000", "expiration_date": "2026-05-29",
             "strike_price": "100", "type": "put", "multiplier": "100"},
        ]}

    def option_bars(self, symbols, timeframe, **kwargs):
        entry = dt.datetime(2026, 5, 28, 15, 50, tzinfo=ET).isoformat()
        exit_at = dt.datetime(2026, 5, 29, 9, 45, tzinfo=ET).isoformat()
        return {"bars": {
            "TEST260529C00100000": [
                {"t": entry, "c": 3.0, "h": 3.2, "l": 2.9, "n": 20},
                {"t": exit_at, "c": 4.0, "h": 4.1, "l": 3.8, "n": 30},
            ],
            "TEST260529P00100000": [
                {"t": entry, "c": 2.0, "h": 2.2, "l": 1.9, "n": 18},
                {"t": exit_at, "c": 3.0, "h": 3.1, "l": 2.8, "n": 25},
            ],
        }}


def row(last, adverse, premium=.05):
    return EventReplayRow(
        "2026-01-01", "after_close", premium_to_spot=premium,
        last_return=last, adverse_return=adverse)


def test_replay_uses_exchange_sessions_and_reports_both_execution_proxies():
    report = replay_long_straddles(
        FakeMCP(), "TEST", [HistoricalEvent(
            dt.date(2026, 5, 28), EventTiming.AFTER_CLOSE)])
    result = report.rows[0]
    assert result.complete
    assert result.entry_date == "2026-05-28" and result.exit_date == "2026-05-29"
    assert result.premium_to_spot == pytest.approx(.05)
    assert result.last_return == pytest.approx(.40)
    assert result.adverse_return == pytest.approx(6.6 / 5.4 - 1)


def test_replay_summary_keeps_mean_median_win_rate_and_adverse_envelope():
    summary = summarize_replays([
        row(.5, .2, .06), row(.1, .05, .07), row(-.2, -.1, .08)])
    assert summary is not None and summary.sample_size == 3
    assert summary.last_median == pytest.approx(.1)
    assert summary.last_win_rate == pytest.approx(2 / 3)
    assert summary.adverse_median == pytest.approx(.05)
    assert summary.premium_median == pytest.approx(.07)
