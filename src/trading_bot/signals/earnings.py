"""Earnings-date awareness.

Holding into an earnings release is a coin-flip on a binary event — exactly the
kind of un-edged risk a disciplined system avoids. We use upcoming earnings
dates to *blackout* new exposure shortly before the event.

NOTE: ETFs (SPY, QQQ — our current universe) have no earnings, so these return
None and the blackout is a no-op. This activates when we trade individual
stocks, which is also when the user's "quarterly earnings" requirement bites.
Source is yfinance (free); all functions are best-effort and never raise.
"""

from __future__ import annotations

from datetime import date


def next_earnings_date(symbol: str) -> date | None:
    """Next scheduled earnings date at/after today, or None (e.g. ETFs)."""
    try:
        import yfinance as yf

        ed = yf.Ticker(symbol).get_earnings_dates(limit=16)
    except Exception:
        return None
    if ed is None or ed.empty:
        return None
    today = date.today()
    future = [idx.date() for idx in ed.index if idx.date() >= today]
    return min(future) if future else None


def days_until_earnings(symbol: str) -> int | None:
    nxt = next_earnings_date(symbol)
    return (nxt - date.today()).days if nxt else None


def in_earnings_blackout(symbol: str, blackout_days: int = 3) -> bool:
    """True if earnings fall within ``blackout_days`` from today."""
    d = days_until_earnings(symbol)
    return d is not None and 0 <= d <= blackout_days
