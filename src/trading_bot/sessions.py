"""NYSE sessions, including holidays and early closes; all instants are aware."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")


@lru_cache(maxsize=4)
def calendar(year: int):
    import exchange_calendars as xc
    return xc.get_calendar("XNYS", start=f"{year-2}-01-01", end=f"{year+2}-12-31")


def now_et() -> datetime:
    return datetime.now(ET)


def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    cal = calendar(day.year)
    if not cal.is_session(pd.Timestamp(day)):
        return None
    return (cal.session_open(day.isoformat()).to_pydatetime().astimezone(ET),
            cal.session_close(day.isoformat()).to_pydatetime().astimezone(ET))


def previous_session(day: date) -> date:
    return calendar(day.year).date_to_session(
        pd.Timestamp(day - timedelta(days=1)), direction="previous").date()


def last_completed_session(now: datetime | None = None) -> date:
    now = now or now_et()
    if now.tzinfo is None:
        raise ValueError("aware market time required")
    local = now.astimezone(ET)
    bounds = session_bounds(local.date())
    # Give daily bars five minutes after the exchange close to settle.
    if bounds and local >= bounds[1] + timedelta(minutes=5):
        return local.date()
    return previous_session(local.date())


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
