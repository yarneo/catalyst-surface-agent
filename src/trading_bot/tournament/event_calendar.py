"""Automatic earnings-calendar discovery with explicit source provenance.

Yahoo's universe calendar is the broad discovery feed.  Nasdaq's date-specific
calendar is the independent confirmation feed.  Neither source alone can
promote an event; :mod:`trading_bot.tournament.weekly` owns that quorum.
Alpaca MCP news is converted to bounded facts for the Featherless semantic
committee, not parsed into orders or trusted as instructions.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

from trading_bot.options.clock import ET
from trading_bot.tournament.catalyst import CatalystFact
from trading_bot.tournament.weekly import CalendarFact, EventTiming


class CalendarSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CalendarDiscovery:
    facts: tuple[CalendarFact, ...]
    errors: tuple[str, ...]


def _timing(value: Any) -> EventTiming:
    text = str(value or "").strip().lower().replace("_", "-")
    if text in {"amc", "after market close", "after-hours", "time-after-hours"}:
        return EventTiming.AFTER_CLOSE
    if text in {"bmo", "before market open", "pre-market", "time-pre-market"}:
        return EventTiming.BEFORE_OPEN
    return EventTiming.UNKNOWN


def yahoo_earnings_facts(
    start: dt.date, end: dt.date, *, minimum_market_cap: float = 2_000_000_000,
    universe: Iterable[str] | None = None,
) -> tuple[CalendarFact, ...]:
    """Fetch the current Yahoo earnings calendar through yfinance 1.4+."""
    try:
        import pandas as pd
        import yfinance as yf

        frame = yf.Calendars(start, end).get_earnings_calendar(
            market_cap=minimum_market_cap, limit=100)
    except Exception as exc:  # noqa: BLE001 — normalized at the provider edge
        raise CalendarSourceError(f"Yahoo calendar failed: {type(exc).__name__}: {exc}") from exc
    if frame is None or frame.empty:
        return ()
    allowed = set(universe) if universe is not None else None
    output: list[CalendarFact] = []
    for symbol, row in frame.iterrows():
        symbol = str(symbol).upper().strip()
        if allowed is not None and symbol not in allowed:
            continue
        try:
            market_cap = float(row.get("Marketcap") or 0.0)
            stamp = pd.Timestamp(row["Event Start Date"])
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize(ET)
            stamp = stamp.tz_convert(ET)
            timing = _timing(row.get("Timing"))
        except (KeyError, TypeError, ValueError):
            continue
        if market_cap < minimum_market_cap or not start <= stamp.date() <= end:
            continue
        output.append(CalendarFact(
            symbol=symbol, event_date=stamp.date(), timing=timing,
            source="yahoo_calendar",
            fact_id=f"yahoo:{symbol}:{stamp.date().isoformat()}",
            summary=(
                f"Yahoo Finance calendar lists {str(row.get('Event Name') or 'earnings')} "
                f"for {symbol} on {stamp.date().isoformat()} in the {timing.value} session."),
        ))
    return tuple(output)


def _nasdaq_payload(day: dt.date, *, timeout_s: float) -> dict[str, Any]:
    query = urllib.parse.urlencode({"date": day.isoformat()})
    request = urllib.request.Request(
        f"https://api.nasdaq.com/api/calendar/earnings?{query}",
        headers={
            "User-Agent": "Mozilla/5.0 CatalystSurfaceAgent/1.0",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/market-activity/earnings",
        })
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError,
            urllib.error.URLError) as exc:
        raise CalendarSourceError(
            f"Nasdaq calendar failed for {day}: {type(exc).__name__}: {exc}") from exc
    return value if isinstance(value, dict) else {}


def _market_cap(value: Any) -> float:
    text = str(value or "").replace("$", "").replace(",", "").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def nasdaq_earnings_facts(
    start: dt.date, end: dt.date, *, minimum_market_cap: float = 2_000_000_000,
    universe: Iterable[str] | None = None, timeout_s: float = 15.0,
) -> tuple[CalendarFact, ...]:
    """Fetch Nasdaq's public earnings calendar one bounded day at a time."""
    if end < start or (end - start).days > 14:
        raise ValueError("Nasdaq calendar range must be 0..14 days")
    allowed = set(universe) if universe is not None else None
    output: list[CalendarFact] = []
    day = start
    while day <= end:
        payload = _nasdaq_payload(day, timeout_s=timeout_s)
        rows = (((payload.get("data") or {}).get("rows") or [])
                if isinstance(payload, dict) else [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper().strip()
            if not symbol or (allowed is not None and symbol not in allowed):
                continue
            if _market_cap(row.get("marketCap")) < minimum_market_cap:
                continue
            timing = _timing(row.get("time"))
            output.append(CalendarFact(
                symbol=symbol, event_date=day, timing=timing,
                source="nasdaq_calendar",
                fact_id=f"nasdaq:{symbol}:{day.isoformat()}",
                summary=(
                    f"Nasdaq earnings calendar lists {symbol} on {day.isoformat()} "
                    f"in the {timing.value} session; fiscal quarter "
                    f"{str(row.get('fiscalQuarterEnding') or 'unspecified')}."),
            ))
        day += dt.timedelta(days=1)
    return tuple(output)


def discover_earnings_calendar(
    start: dt.date, end: dt.date, *, minimum_market_cap: float = 2_000_000_000,
    universe: Iterable[str] | None = None,
) -> CalendarDiscovery:
    """Run both feeds independently and preserve a partial result for audit."""
    facts: list[CalendarFact] = []
    errors: list[str] = []
    for name, provider in (("yahoo", yahoo_earnings_facts),
                           ("nasdaq", nasdaq_earnings_facts)):
        try:
            facts.extend(provider(
                start, end, minimum_market_cap=minimum_market_cap,
                universe=universe))
        except Exception as exc:  # noqa: BLE001 — each provider is isolated
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return CalendarDiscovery(tuple(facts), tuple(errors))


def calendar_catalyst_fact(fact: CalendarFact, *, observed_at: dt.datetime) -> CatalystFact:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return CatalystFact(
        fact.fact_id, observed_at.astimezone(ET).isoformat(),
        f"{fact.symbol} scheduled earnings calendar fact", fact.summary,
        (fact.symbol,), fact.source)


def alpaca_news_facts(payload: Any, *, symbol: str) -> tuple[CatalystFact, ...]:
    """Normalize named Alpaca news fields; article text remains untrusted data."""
    rows = payload.get("news", []) if isinstance(payload, dict) else []
    relevant_words = (
        "earnings", "results", "report", "guidance", "conference call",
        "quarter", "fiscal", "investor relations",
    )
    relevant: list[CatalystFact] = []
    context: list[CatalystFact] = []
    for row in rows:
        if not isinstance(row, dict) or symbol not in (row.get("symbols") or []):
            continue
        try:
            fact = CatalystFact(
                fact_id=f"alpaca:{row['id']}",
                published_at=str(row.get("created_at") or row.get("updated_at")),
                headline=str(row["headline"])[:500],
                summary=str(row.get("summary") or row.get("content") or "")[:4000],
                symbols=(symbol,), source=str(row.get("source") or "alpaca_news"))
        except (KeyError, TypeError, ValueError):
            continue
        haystack = f"{fact.headline} {fact.summary}".lower()
        (relevant if any(word in haystack for word in relevant_words)
         else context).append(fact)
    return tuple([*relevant[:8], *context[:2]])
