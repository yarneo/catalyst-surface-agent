"""Generic historical next-session ATM straddle replay through Alpaca MCP.

The replay deliberately reports two proxies because Alpaca's historical option
surface exposes trades, not historical NBBO quotes: a last-trade path and an
adverse entry-high/exit-low envelope.  Missing either leg excludes the event.
The output is evidence for the frozen promotion rule; it never places orders.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from typing import Any, Iterable

from trading_bot.options.clock import ET
from trading_bot.tournament.weekly import EventTiming, ReplaySummary


@dataclass(frozen=True)
class HistoricalEvent:
    event_date: dt.date
    timing: EventTiming


@dataclass(frozen=True)
class EventReplayRow:
    event_date: str
    timing: str
    entry_date: str | None = None
    exit_date: str | None = None
    expiry: str | None = None
    spot: float | None = None
    strike: float | None = None
    call_symbol: str | None = None
    put_symbol: str | None = None
    entry_last: float | None = None
    exit_last: float | None = None
    premium_to_spot: float | None = None
    last_return: float | None = None
    entry_adverse: float | None = None
    exit_adverse: float | None = None
    adverse_return: float | None = None
    minimum_entry_trades: int | None = None
    minimum_exit_trades: int | None = None
    reason: str | None = None

    @property
    def complete(self) -> bool:
        return self.last_return is not None and self.adverse_return is not None


@dataclass(frozen=True)
class ReplayReport:
    symbol: str
    requested: int
    rows: tuple[EventReplayRow, ...]
    summary: ReplaySummary | None
    limitations: tuple[str, ...] = (
        "Historical option trades are not quotes, NBBOs, or fills.",
        "Call and put observations may occur at different instants.",
        "The adverse envelope intentionally overstates execution cost.",
        "Missing either leg excludes and reports the event.",
    )


def historical_earnings_events(
    symbol: str, *, before: dt.date, limit: int = 12,
) -> tuple[HistoricalEvent, ...]:
    """Get dated historical earnings automatically; never include the target event."""
    if limit < 1 or limit > 100:
        raise ValueError("historical earnings limit must be in [1, 100]")
    try:
        import pandas as pd
        import yfinance as yf

        frame = yf.Ticker(symbol).get_earnings_dates(limit=min(100, limit * 3))
    except Exception:
        return ()
    if frame is None or frame.empty:
        return ()
    output: list[HistoricalEvent] = []
    seen: set[dt.date] = set()
    for value in frame.index:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(ET)
        stamp = stamp.tz_convert(ET)
        day = stamp.date()
        if day >= before or day in seen:
            continue
        timing = (EventTiming.BEFORE_OPEN if stamp.hour < 12
                  else EventTiming.AFTER_CLOSE)
        output.append(HistoricalEvent(day, timing))
        seen.add(day)
    output.sort(key=lambda row: row.event_date, reverse=True)
    return tuple(output[:limit])


def _parse_time(value: str) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("market timestamp must be timezone-aware")
    return stamp.astimezone(ET)


def _sessions(payload: Any) -> tuple[dt.date, ...]:
    rows = payload if isinstance(payload, list) else []
    return tuple(sorted({dt.date.fromisoformat(str(row["date"])) for row in rows
                         if isinstance(row, dict) and row.get("date")}))


def _event_sessions(event: HistoricalEvent, sessions: tuple[dt.date, ...]) \
        -> tuple[dt.date, dt.date]:
    if event.event_date not in sessions:
        raise ValueError("event date is not an exchange session")
    position = sessions.index(event.event_date)
    if event.timing is EventTiming.AFTER_CLOSE:
        if position + 1 >= len(sessions):
            raise ValueError("next exchange session is unavailable")
        return event.event_date, sessions[position + 1]
    if position == 0:
        raise ValueError("previous exchange session is unavailable")
    return sessions[position - 1], event.event_date


def _stock_closes(mcp: Any, symbol: str, start: dt.date,
                  end: dt.date) -> dict[dt.date, float]:
    payload = mcp.stock_bars(
        symbol, timeframe="1Day", start=start.isoformat(), end=end.isoformat(),
        feed="iex", adjustment="raw", limit=10000, sort="asc")
    rows = payload.get("bars", {}).get(symbol, []) if isinstance(payload, dict) else []
    output = {}
    for row in rows:
        try:
            output[_parse_time(str(row["t"])).date()] = float(row["c"])
        except (KeyError, TypeError, ValueError):
            continue
    return output


def _atm_contracts(
    mcp: Any, symbol: str, *, exit_day: dt.date, spot: float,
) -> tuple[dt.date, float, str, str] | None:
    payload = mcp.option_contracts(
        underlying_symbols=symbol, status="inactive",
        expiration_date_gte=exit_day.isoformat(),
        expiration_date_lte=(exit_day + dt.timedelta(days=8)).isoformat(),
        strike_price_gte=spot * 0.90, strike_price_lte=spot * 1.10,
        limit=10000)
    rows = payload.get("option_contracts", []) if isinstance(payload, dict) else []
    by_expiry: dict[dt.date, dict[float, dict[str, str]]] = {}
    for row in rows:
        try:
            if str(row.get("multiplier")) != "100":
                continue
            expiry = dt.date.fromisoformat(str(row["expiration_date"]))
            strike = float(row["strike_price"])
            kind = str(row["type"])
            contract = str(row["symbol"])
        except (KeyError, TypeError, ValueError):
            continue
        by_expiry.setdefault(expiry, {}).setdefault(strike, {})[kind] = contract
    for expiry in sorted(by_expiry):
        common = [strike for strike, sides in by_expiry[expiry].items()
                  if {"call", "put"} <= sides.keys()]
        if common:
            strike = min(common, key=lambda value: (abs(value - spot), value))
            sides = by_expiry[expiry][strike]
            return expiry, strike, sides["call"], sides["put"]
    return None


def _stamp(day: dt.date, hour: int, minute: int) -> str:
    return dt.datetime.combine(day, dt.time(hour, minute), ET).isoformat()


def _option_windows(
    mcp: Any, *, entry_day: dt.date, exit_day: dt.date,
    symbols: tuple[str, str],
) -> dict[str, dict[str, float | int]]:
    payload = mcp.option_bars(
        ",".join(symbols), "5Min", start=_stamp(entry_day, 15, 20),
        end=_stamp(exit_day, 10, 0), limit=10000, sort="asc")
    bars = payload.get("bars", {}) if isinstance(payload, dict) else {}
    output: dict[str, dict[str, float | int]] = {}
    for symbol in symbols:
        rows = bars.get(symbol, []) if isinstance(bars, dict) else []
        entry = [row for row in rows
                 if _parse_time(str(row["t"])).date() == entry_day
                 and dt.time(15, 30) <= _parse_time(str(row["t"])).time()
                 <= dt.time(15, 55)]
        exits = [row for row in rows
                 if _parse_time(str(row["t"])).date() == exit_day
                 and dt.time(9, 40) <= _parse_time(str(row["t"])).time()
                 <= dt.time(9, 50)]
        if not entry or not exits:
            continue
        output[symbol] = {
            "entry_last": float(entry[-1]["c"]),
            "exit_last": float(exits[-1]["c"]),
            "entry_adverse": max(float(row["h"]) for row in entry),
            "exit_adverse": min(float(row["l"]) for row in exits),
            "entry_trades": sum(int(row.get("n") or 0) for row in entry),
            "exit_trades": sum(int(row.get("n") or 0) for row in exits),
        }
    return output


def summarize_replays(rows: Iterable[EventReplayRow]) -> ReplaySummary | None:
    complete = [row for row in rows if row.complete]
    if not complete:
        return None
    last = [float(row.last_return) for row in complete]
    adverse = [float(row.adverse_return) for row in complete]
    premiums = sorted(float(row.premium_to_spot) for row in complete
                      if row.premium_to_spot is not None)
    if not premiums:
        return None
    position = 0.75 * (len(premiums) - 1)
    lower, upper = math.floor(position), math.ceil(position)
    p75 = (premiums[lower] if lower == upper else
           premiums[lower] * (upper - position) + premiums[upper] * (position - lower))
    return ReplaySummary(
        sample_size=len(complete),
        last_mean=statistics.mean(last), last_median=statistics.median(last),
        last_win_rate=sum(value > 0 for value in last) / len(last),
        adverse_mean=statistics.mean(adverse),
        adverse_median=statistics.median(adverse),
        adverse_win_rate=sum(value > 0 for value in adverse) / len(adverse),
        premium_median=statistics.median(premiums), premium_p75=p75)


def replay_long_straddles(
    mcp: Any, symbol: str, events: Iterable[HistoricalEvent],
) -> ReplayReport:
    event_rows = tuple(events)
    if not event_rows:
        return ReplayReport(symbol, 0, (), None)
    first = min(row.event_date for row in event_rows) - dt.timedelta(days=10)
    last = max(row.event_date for row in event_rows) + dt.timedelta(days=10)
    sessions = _sessions(mcp.calendar(first.isoformat(), last.isoformat()))
    closes = _stock_closes(mcp, symbol, first, last)
    rows: list[EventReplayRow] = []
    for event in sorted(event_rows, key=lambda row: row.event_date):
        base = {"event_date": event.event_date.isoformat(),
                "timing": event.timing.value}
        try:
            entry_day, exit_day = _event_sessions(event, sessions)
            spot = closes.get(entry_day)
            if spot is None or not math.isfinite(spot) or spot <= 0:
                raise ValueError("raw entry-session stock close is unavailable")
            selected = _atm_contracts(
                mcp, symbol, exit_day=exit_day, spot=spot)
            if selected is None:
                raise ValueError("expired ATM call/put pair is unavailable")
            expiry, strike, call, put = selected
            windows = _option_windows(
                mcp, entry_day=entry_day, exit_day=exit_day,
                symbols=(call, put))
            if call not in windows or put not in windows:
                missing = "call" if call not in windows else "put"
                raise ValueError(f"{missing} trade window is unavailable")
            entry_last = float(windows[call]["entry_last"]) + float(windows[put]["entry_last"])
            exit_last = float(windows[call]["exit_last"]) + float(windows[put]["exit_last"])
            entry_bad = float(windows[call]["entry_adverse"]) + float(windows[put]["entry_adverse"])
            exit_bad = float(windows[call]["exit_adverse"]) + float(windows[put]["exit_adverse"])
            if min(entry_last, entry_bad) <= 0:
                raise ValueError("historical entry premium is non-positive")
            rows.append(EventReplayRow(
                **base, entry_date=entry_day.isoformat(), exit_date=exit_day.isoformat(),
                expiry=expiry.isoformat(), spot=spot, strike=strike,
                call_symbol=call, put_symbol=put,
                entry_last=entry_last, exit_last=exit_last,
                premium_to_spot=entry_last / spot,
                last_return=exit_last / entry_last - 1.0,
                entry_adverse=entry_bad, exit_adverse=exit_bad,
                adverse_return=exit_bad / entry_bad - 1.0,
                minimum_entry_trades=min(int(windows[call]["entry_trades"]),
                                         int(windows[put]["entry_trades"])),
                minimum_exit_trades=min(int(windows[call]["exit_trades"]),
                                        int(windows[put]["exit_trades"]))))
        except Exception as exc:  # noqa: BLE001 — missing events are evidence
            rows.append(EventReplayRow(**base,
                                       reason=f"{type(exc).__name__}: {exc}"))
    return ReplayReport(symbol, len(event_rows), tuple(rows), summarize_replays(rows))
