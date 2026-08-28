import datetime as dt

import numpy as np
import pytest

from trading_bot.options.occ import BadOCC, parse
from trading_bot.options.spreads import Leg, payoff_at


def test_parses_a_real_symbol_we_traded():
    c = parse("QQQ260925C00765000")
    assert c.root == "QQQ"
    assert c.expiry == dt.date(2026, 9, 25)
    assert c.right == "C" and c.is_call
    assert c.strike == 765.0


def test_parses_fractional_strikes():
    assert parse("SPY260904P00766500").strike == 766.5


def test_rejects_junk_rather_than_guessing():
    for bad in ("QQQ", "AAPL", "QQQ260925X00765000", "QQQ26092C00765000", ""):
        with pytest.raises(BadOCC):
            parse(bad)


def test_condor_payoff_is_flat_in_the_middle_and_capped_outside():
    legs = (Leg("QQQ260925C00765000", "sell"), Leg("QQQ260925C00775000", "buy"),
            Leg("QQQ260925P00671000", "sell"), Leg("QQQ260925P00660000", "buy"))
    p = payoff_at(legs, np.array([600.0, 660.0, 671.0, 715.0, 765.0, 775.0, 900.0]))
    assert p[3] == 0.0                      # between the shorts: nothing owed
    assert p[0] == pytest.approx(-11.0)     # far below: put wing caps the loss
    assert p[-1] == pytest.approx(-10.0)    # far above: call wing caps it
    assert p[0] == p[1] and p[-1] == p[-2]  # capped, not still falling


def test_long_call_payoff_is_convex_and_unbounded_above():
    p = payoff_at((Leg("SPY260904C00770000", "buy"),),
                  np.array([700.0, 770.0, 800.0, 900.0]))
    assert list(p) == [0.0, 0.0, 30.0, 130.0]


def test_sold_leg_is_a_liability_at_expiry():
    p = payoff_at((Leg("SPY260904C00770000", "sell"),), np.array([800.0]))
    assert p[0] == -30.0


def test_adjusted_series_roots_containing_digits_are_parsed():
    """A corporate action creates roots like RUT1 or SPY2. Rejecting them makes
    `parse` raise inside `payoff_at`, taking down the payoff calculation for a
    position we actually hold."""
    for sym, root, strike in (("RUT1260918C00050000", "RUT1", 50.0),
                              ("SPY2260904P00766500", "SPY2", 766.5),
                              ("A260904C00100000", "A", 100.0)):
        c = parse(sym)
        assert c.root == root
        assert c.strike == strike


def test_a_root_starting_with_a_digit_is_still_rejected():
    with pytest.raises(BadOCC):
        parse("1SPY260904C00766000")
