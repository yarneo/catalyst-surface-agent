"""Market time, kept separate so nothing has to guess.

This repo has had four date/time incidents: a VM on UTC firing the open job
four hours early; a chart anchored to the wrong launch date; a naive deadline
that put the flatten trigger at 02:00 ET because the machine runs Israel time;
and a `dt.date.today()` returning tomorrow's date every evening, which shortened
every DTE by a day and inflated measured implied vol by 9-44%.

The common cause each time was code asking the machine what day it is. Nothing
below asks. Every function takes or produces an ET-aware instant, and
`today_et()` is the only date source the rest of the package may use.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

OPEN = dt.time(9, 30)
CLOSE = dt.time(16, 0)

# Verified against the 2026 NYSE calendar for the contest window: Labor Day is
# Mon 2026-09-07, three days AFTER the deadline, and no half-day falls between
# Aug 31 and Sep 4. The measurable liquid window is four full sessions because
# the Friday cutoff is exactly at the 09:30 ET open.
HOLIDAYS_2026 = frozenset({
    dt.date(2026, 1, 1), dt.date(2026, 1, 19), dt.date(2026, 2, 16),
    dt.date(2026, 4, 3), dt.date(2026, 5, 25), dt.date(2026, 6, 19),
    dt.date(2026, 7, 3), dt.date(2026, 9, 7), dt.date(2026, 11, 26),
    dt.date(2026, 12, 25),
})


def now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def today_et() -> dt.date:
    """The trading date in New York. The only date source in the package.

    `dt.date.today()` returns the machine's local date. On a box seven hours
    ahead of New York that is tomorrow's date from 17:00 ET onward, which made
    every evening run compute a DTE one day short.
    """
    return now_et().date()


def require_et(name: str, value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{name} must be timezone-aware; a naive datetime here means the "
            f"deadline is whatever the machine's clock happens to say")
    return value.astimezone(ET)


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS_2026


def is_market_open(when: dt.datetime) -> bool:
    w = require_et("when", when)
    return is_trading_day(w.date()) and OPEN <= w.time() < CLOSE


def previous_trading_day(d: dt.date) -> dt.date:
    p = d - dt.timedelta(days=1)
    while not is_trading_day(p):
        p -= dt.timedelta(days=1)
    return p


def last_expiry_settling_before(deadline: dt.datetime) -> dt.date:
    """The last expiry whose settlement is a FACT by the time we are graded.

    Options settle at 16:00 ET on their expiry date. A deadline at 11:00 ET
    therefore arrives five hours BEFORE a same-day expiry settles, so such a
    position is still open, still 100% extrinsic, and contributes a broker model
    rather than a realised number — swinging ±80% on a 1% tape move. An audit
    found the previous code treating same-day expiry as safe and, worse,
    selecting it preferentially as the latest expiry in the window.

    So: same-day counts only if the deadline falls at or after the close.
    """
    d = require_et("deadline", deadline)
    if d.time() >= CLOSE and is_trading_day(d.date()):
        return d.date()
    return previous_trading_day(d.date())


def close_positions_by(expiry: dt.date, *, hour: int = 14) -> dt.datetime:
    """When to be flat on expiry day — hours BEFORE the contracts expire.

    Holding a short option to expiry is not neutral. If it finishes in the money
    it is exercised, and a condor with one short leg assigned becomes a large
    stock position with no defined risk at all: an audit simulated 4,000 short
    SPY shares, $2.6M of notional against $100,000 of equity, found by an agent
    that could then only say "resolve by hand".

    Expiry also destroys the agent's own bookkeeping. An expired contract has no
    quote, so the position cannot be marked — and the runner treated an
    unmarkable position as a hard stop, which killed the deadline flatten for
    every remaining cycle. An expired-worthless row also stays open in the
    registry forever, permanently blocking new positions.

    Closing early costs a few hours of time decay and retires all three.
    """
    return dt.datetime.combine(expiry, dt.time(hour, 0), tzinfo=ET)


def flat_by(deadline: dt.datetime, *, minutes_after_open: int = 15,
            margin_minutes: int = 60) -> dt.datetime:
    """When to stop holding anything, on a clock that has a live market in it.

    The previous implementation subtracted two hours from the deadline, which on
    an 11:00 ET deadline landed at 09:00 — thirty minutes before the options
    market opens. There is no pre-market options session, so the trigger fired
    into a closed book: every close walked its escalation ladder into nothing,
    ended in an unfilled market order, and the position stayed live.

    This anchors to the OPEN instead: fifteen minutes in, once the auction has
    settled and there is a book to trade against, and no later than an hour
    before grading.
    """
    d = require_et("deadline", deadline)
    if not is_trading_day(d.date()):
        prev = previous_trading_day(d.date())
        return dt.datetime.combine(prev, CLOSE, tzinfo=ET) - dt.timedelta(minutes=30)

    after_open = dt.datetime.combine(d.date(), OPEN, tzinfo=ET) \
        + dt.timedelta(minutes=minutes_after_open)
    latest = d - dt.timedelta(minutes=margin_minutes)
    if latest >= after_open:
        return after_open
    # A deadline before 10:45 leaves no same-day window that is both liquid and
    # at least an hour before grading. Returning 09:45 for a 09:30 deadline is
    # not a conservative compromise — it is an order submitted after the score
    # is final. Use the prior liquid session instead.
    prev = previous_trading_day(d.date())
    return dt.datetime.combine(prev, CLOSE, tzinfo=ET) - dt.timedelta(minutes=30)


def trading_minutes_between(a: dt.datetime, b: dt.datetime) -> float:
    """Minutes of regular trading between two instants.

    Wall-clock elapsed is not a measure of opportunity: the contest window is
    168 hours of which only 32.5 are tradable, and a weekend burns 28.6% of the
    clock while offering nothing. Anything pacing itself against the deadline
    has to count sessions, not hours.
    """
    a, b = require_et("a", a), require_et("b", b)
    if b <= a:
        return 0.0
    total, day = 0.0, a.date()
    while day <= b.date():
        if is_trading_day(day):
            o = dt.datetime.combine(day, OPEN, tzinfo=ET)
            c = dt.datetime.combine(day, CLOSE, tzinfo=ET)
            lo, hi = max(a, o), min(b, c)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60.0
        day += dt.timedelta(days=1)
    return total


def session_fraction(now: dt.datetime, start: dt.datetime,
                     deadline: dt.datetime) -> float:
    """How much of the TRADABLE window is gone, in [0, 1]."""
    total = trading_minutes_between(start, deadline)
    if total <= 0:
        return 1.0
    return min(1.0, max(0.0, trading_minutes_between(start, now) / total))
