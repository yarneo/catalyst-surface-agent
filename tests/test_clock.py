"""Four date/time incidents in this repo. These tests exist so there is not a fifth.

Each one below corresponds to a defect that actually shipped or was caught in
audit, and the fixtures use instants where the Israel date and the New York date
DIFFER, because that is where every one of them lived.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from trading_bot.options.clock import (CLOSE, ET, OPEN, flat_by, is_market_open,
                                       is_trading_day,
                                       last_expiry_settling_before,
                                       previous_trading_day, require_et,
                                       session_fraction,
                                       trading_minutes_between)

IL = ZoneInfo("Asia/Jerusalem")
DEADLINE = dt.datetime(2026, 9, 4, 9, 30, tzinfo=ET)
START = dt.datetime(2026, 8, 31, 9, 30, tzinfo=ET)


def et(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=ET)


# --- naive datetimes -----------------------------------------------------

def test_naive_datetimes_are_refused_everywhere():
    naive = dt.datetime(2026, 9, 4, 9, 30)
    for fn in (lambda: require_et("x", naive),
               lambda: is_market_open(naive),
               lambda: flat_by(naive),
               lambda: last_expiry_settling_before(naive)):
        with pytest.raises(ValueError, match="timezone-aware"):
            fn()


def test_the_deadline_is_one_instant_in_both_zones():
    assert DEADLINE == dt.datetime(2026, 9, 4, 16, 30, tzinfo=IL)


# --- flat_by must land in a live market ---------------------------------

def test_flat_by_uses_the_prior_liquid_session_for_an_opening_cutoff():
    """A 09:30 cutoff has no same-day options exit window. The prior version
    returned 09:45, which is liquid but fifteen minutes after grading."""
    fb = flat_by(DEADLINE)
    assert fb == dt.datetime(2026, 9, 3, 15, 30, tzinfo=ET)
    assert is_market_open(fb)
    assert fb < DEADLINE


def test_flat_by_leaves_at_least_an_hour_before_grading():
    assert (DEADLINE - flat_by(DEADLINE)) >= dt.timedelta(minutes=60)


def test_flat_by_on_a_late_deadline_still_leaves_margin():
    late = dt.datetime(2026, 9, 4, 15, 30, tzinfo=ET)
    assert (late - flat_by(late)) >= dt.timedelta(minutes=60)
    assert is_market_open(flat_by(late))


# --- expiry must SETTLE before grading ----------------------------------

def test_same_day_expiry_is_not_safe_for_a_morning_deadline():
    """Options settle at 16:00 ET. A 09:30 deadline arrives before settlement,
    so a same-day contract is still open, 100% extrinsic, and contributes a
    broker model rather than a realised number."""
    assert last_expiry_settling_before(DEADLINE) == dt.date(2026, 9, 3)


def test_same_day_expiry_is_safe_once_the_close_has_passed():
    after = dt.datetime(2026, 9, 4, 16, 0, tzinfo=ET)
    assert last_expiry_settling_before(after) == dt.date(2026, 9, 4)


def test_settling_expiry_skips_back_over_a_weekend():
    monday = dt.datetime(2026, 8, 31, 11, 0, tzinfo=ET)
    assert last_expiry_settling_before(monday) == dt.date(2026, 8, 28)


def test_settling_expiry_skips_back_over_labor_day():
    tue = dt.datetime(2026, 9, 8, 11, 0, tzinfo=ET)
    assert last_expiry_settling_before(tue) == dt.date(2026, 9, 4)


# --- the contest window --------------------------------------------------

def test_labor_day_is_after_the_deadline():
    assert dt.date(2026, 9, 7).weekday() == 0
    assert not is_trading_day(dt.date(2026, 9, 7))
    assert dt.date(2026, 9, 7) > DEADLINE.date()


def test_every_day_in_the_window_is_a_full_session():
    d = START.date()
    days = []
    while d <= DEADLINE.date():
        if is_trading_day(d):
            days.append(d)
        d += dt.timedelta(days=1)
    assert len(days) == 5      # Aug 31, Sep 1, 2, 3, 4


def test_window_has_the_measured_amount_of_tradable_time():
    """The cutoff is Friday's open, so only Mon-Thu are tradable."""
    assert trading_minutes_between(START, DEADLINE) == pytest.approx(1560.0)


# --- wall clock vs sessions ---------------------------------------------

def test_overnight_hours_burn_no_tradable_time():
    mon_close = et("2026-08-31 16:00")
    tue_open = et("2026-09-01 09:30")
    assert trading_minutes_between(mon_close, tue_open) == 0.0
    assert session_fraction(mon_close, START, DEADLINE) == \
        pytest.approx(session_fraction(tue_open, START, DEADLINE))


def test_wall_clock_and_session_fraction_are_not_interchangeable():
    wed_open = et("2026-09-02 09:30")
    wall = (wed_open - START).total_seconds() / (DEADLINE - START).total_seconds()
    sess = session_fraction(wed_open, START, DEADLINE)
    assert wall == pytest.approx(0.50, abs=0.01)
    assert sess == pytest.approx(0.50, abs=0.01)


def test_session_fraction_spans_zero_to_one():
    assert session_fraction(START, START, DEADLINE) == 0.0
    assert session_fraction(DEADLINE, START, DEADLINE) == 1.0
    assert session_fraction(et("2026-08-20 12:00"), START, DEADLINE) == 0.0


# --- market hours --------------------------------------------------------

def test_market_hours_boundaries():
    assert not is_market_open(et("2026-08-27 09:29"))
    assert is_market_open(et("2026-08-27 09:30"))
    assert is_market_open(et("2026-08-27 15:59"))
    assert not is_market_open(et("2026-08-27 16:00"))
    assert not is_market_open(et("2026-08-29 12:00"))     # Saturday


def test_an_evening_israel_run_is_the_previous_new_york_date():
    """The `dt.date.today()` bug: at 02:00 Israel it is 19:00 ET the day before,
    so the machine's date is a day ahead and every DTE came out one short."""
    il = dt.datetime(2026, 8, 28, 2, 0, tzinfo=IL)
    assert il.date() == dt.date(2026, 8, 28)
    assert il.astimezone(ET).date() == dt.date(2026, 8, 27)
    assert not is_market_open(il)


def test_no_module_asks_the_machine_for_the_date():
    """Every date/time incident in this repo began with code asking the local
    machine what day it is. `today_et` is the only permitted source."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "trading_bot" / "options"
    offenders = []
    for f in src.glob("*.py"):
        if f.name == "clock.py":
            continue
        for i, line in enumerate(f.read_text().split("\n"), 1):
            if line.lstrip().startswith("#"):
                continue          # comments may name the thing they forbid
            for pattern in ("date.today()", "datetime.now()"):
                if pattern in line:
                    offenders.append(f"{f.name}:{i} {pattern}")
    assert offenders == [], f"local-clock calls outside clock.py: {offenders}"
