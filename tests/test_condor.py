"""Tests for the simplified structure builder.

Written against the audit's central lesson: the previous suite passed 307 tests
while nearly every guard its docstrings called load-bearing could be deleted
silently. The cause was comfortable fixtures — arbitrage-free chains, symmetric
wings, zero-ruin candidates — so guards never activated and tests passed down a
different branch.

So the fixtures here are hostile by construction, `exact_max_loss` is checked
against brute force rather than against itself, and no assertion contains `or`.
"""
import math

import numpy as np
import pytest

from trading_bot.options.condor import (Condor, Unbounded, build_condor,
                                        exact_max_loss, _delta_strike)
from trading_bot.options.iv import IVUnavailable, Quote, bs_price_forward
from trading_bot.options.occ import parse
from trading_bot.options.spreads import Leg, payoff_at

F, T, R = 600.0, 7 / 365, 0.04


def sym(k, right):
    return f"XYZ260903{right}{int(round(k * 1000)):08d}"


def brute_force_max_loss(legs, entry, hi_mult=6.0, n=400_001):
    """Ground truth: scan the whole plausible price line at fine resolution.

    Deliberately independent of `exact_max_loss` — a dense sweep, not a clever
    argument about kinks. If the two disagree, the clever argument is wrong.
    """
    top = max(parse(l.symbol).strike for l in legs) * hi_mult
    grid = np.linspace(0.0, top, n)
    return -float(np.min(payoff_at(legs, grid) - entry))


def condor_legs(sp, sc, width):
    return (Leg(sym(sc, "C"), "sell"), Leg(sym(sc + width, "C"), "buy"),
            Leg(sym(sp, "P"), "sell"), Leg(sym(sp - width, "P"), "buy"))


# --- exact_max_loss vs brute force ---------------------------------------

def test_condor_max_loss_matches_brute_force():
    legs = condor_legs(560.0, 640.0, 10.0)
    for entry in (-2.5, -0.5, 0.0, 1.0):
        assert exact_max_loss(legs, entry) == pytest.approx(
            brute_force_max_loss(legs, entry), abs=0.02)


def test_condor_max_loss_is_width_minus_credit():
    legs = condor_legs(560.0, 640.0, 10.0)
    assert exact_max_loss(legs, -2.5) == pytest.approx(10.0 - 2.5)


def test_unbalanced_butterfly_the_old_grid_understated_by_8x():
    """The exact structure from the risk audit: an unbalanced fly whose true
    worst case sits at the OUTER strike, outside any density grid clipped to the
    liquid strike range. Reported $240/contract; true $2,040."""
    legs = (Leg(sym(540, "P"), "buy"), Leg(sym(579, "P"), "sell", 2),
            Leg(sym(600, "P"), "buy"))
    entry = 2.40
    got = exact_max_loss(legs, entry)
    assert got == pytest.approx(brute_force_max_loss(legs, entry), abs=0.02)
    assert got == pytest.approx(20.40, abs=0.05)
    # And the number a density grid clipped to the liquid strike range would
    # have produced. The audit measured 8.5x on a live chain; this fixture's
    # grid is a little wider, so it reproduces 3.4x. The magnitude depends on
    # how far the grid falls short of the strikes — the defect does not.
    narrow = np.linspace(F * 0.924, F * 1.035, 400)
    on_grid = -float(np.min(payoff_at(legs, narrow) - entry))
    assert on_grid == pytest.approx(6.0, abs=0.1)
    assert got > on_grid * 3, "fixture no longer reproduces the understatement"


def test_deep_put_loss_is_found_at_zero():
    """A short put's worst case is at S=0 and nowhere else. A grid that stops at
    −7% cannot see it."""
    legs = (Leg(sym(560, "P"), "sell"), Leg(sym(700, "C"), "buy"))
    got = exact_max_loss(legs, -1.0)
    assert got == pytest.approx(brute_force_max_loss(legs, -1.0), abs=0.05)
    assert got == pytest.approx(559.0, abs=0.05)


def test_ratio_backspread_max_loss_matches_brute_force():
    legs = (Leg(sym(600, "C"), "sell"), Leg(sym(620, "C"), "buy", 2))
    for entry in (-1.0, 0.0, 2.0):
        assert exact_max_loss(legs, entry) == pytest.approx(
            brute_force_max_loss(legs, entry), abs=0.02)


def test_ratio_qty_is_honoured_in_the_worst_case():
    one = exact_max_loss((Leg(sym(560, "P"), "sell"), Leg(sym(700, "C"), "buy")), 0.0)
    five = exact_max_loss((Leg(sym(560, "P"), "sell", 5), Leg(sym(700, "C"), "buy")), 0.0)
    assert five == pytest.approx(one * 5, rel=1e-9)


# --- unbounded -----------------------------------------------------------

def test_net_short_call_is_refused():
    with pytest.raises(Unbounded):
        exact_max_loss((Leg(sym(620, "C"), "sell"),), -3.0)


def test_two_short_one_long_call_is_refused():
    with pytest.raises(Unbounded):
        exact_max_loss((Leg(sym(610, "C"), "sell", 2), Leg(sym(640, "C"), "buy")), 0.0)


def test_covered_short_call_is_allowed():
    legs = (Leg(sym(610, "C"), "sell"), Leg(sym(640, "C"), "buy"))
    assert exact_max_loss(legs, -1.0) == pytest.approx(29.0)


# --- the builder ---------------------------------------------------------

def chain(iv=0.20, lo=480.0, hi=720.0, step=5.0, spread_pct=0.03, skew=0.0):
    strikes, occ, quotes = [], {}, {}
    k = lo
    while k <= hi:
        strikes.append(k)
        v = iv + skew * (F - k) / F
        for right, is_call in (("C", True), ("P", False)):
            s = sym(k, right)
            occ[(k, right)] = s
            p = bs_price_forward(F, k, T, R, v, is_call)
            if p < 0.05:
                continue
            half = max(0.01, p * spread_pct / 2)
            quotes[s] = Quote(round(p - half, 2), round(p + half, 2))
        k += step
    return strikes, occ, quotes


def test_builder_produces_equal_wings():
    strikes, occ, quotes = chain()
    c = build_condor("XYZ", F, 0.20, T, "2026-09-03", 7, strikes, occ, quotes)
    ks = sorted(parse(l.symbol).strike for l in c.legs)
    assert (ks[1] - ks[0]) == (ks[3] - ks[2])


def test_builder_max_loss_matches_brute_force():
    strikes, occ, quotes = chain()
    c = build_condor("XYZ", F, 0.20, T, "2026-09-03", 7, strikes, occ, quotes)
    assert c.max_loss == pytest.approx(
        brute_force_max_loss(c.legs, c.entry), abs=0.02)


def test_shorts_straddle_the_forward():
    strikes, occ, quotes = chain()
    c = build_condor("XYZ", F, 0.20, T, "2026-09-03", 7, strikes, occ, quotes)
    assert c.short_put < F < c.short_call


def test_wings_scale_with_volatility_not_with_spot():
    """The reason single names are admissible. A fixed percent-of-spot wing is
    half a sigma on a 13-vol index and a tenth of a sigma on a 59-vol single
    name — the same parameter building structures with nothing in common."""
    lo_strikes, lo_occ, lo_q = chain(iv=0.15)
    hi_strikes, hi_occ, hi_q = chain(iv=0.60)
    a = build_condor("LO", F, 0.15, T, "2026-09-03", 7, lo_strikes, lo_occ, lo_q)
    b = build_condor("HI", F, 0.60, T, "2026-09-03", 7, hi_strikes, hi_occ, hi_q)
    assert b.width > a.width * 2.5
    # and the shorts move out with vol too
    assert (b.short_call - F) > (a.short_call - F) * 2.5


def test_higher_delta_shorts_sit_closer_to_the_money():
    strikes, occ, quotes = chain()
    near = build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes, short_delta=0.30)
    far = build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes, short_delta=0.08)
    assert near.short_call < far.short_call
    assert near.short_put > far.short_put
    assert near.credit > far.credit


def test_delta_strike_inverts_correctly():
    """Independent check: the Black-76 delta of the returned strike must equal
    the delta requested."""
    from scipy.stats import norm
    for delta in (0.08, 0.16, 0.30):
        for call in (True, False):
            k = _delta_strike(F, 0.20, T, delta, call)
            v = 0.20 * math.sqrt(T)
            d1 = (math.log(F / k) + 0.5 * v * v) / v
            got = norm.cdf(d1) if call else norm.cdf(-d1)
            assert got == pytest.approx(delta, abs=1e-6)


# --- hostile chains ------------------------------------------------------

def test_a_chain_with_no_wing_beyond_the_shorts_is_refused():
    strikes, occ, quotes = chain(lo=560.0, hi=640.0)
    with pytest.raises(IVUnavailable):
        build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes, short_delta=0.02)


def test_an_unusable_leg_quote_refuses_the_whole_structure():
    strikes, occ, quotes = chain()
    c = build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes)
    quotes[c.legs[0].symbol] = Quote(0.05, 4.00)      # 195% spread
    with pytest.raises(IVUnavailable, match="unusable quote"):
        build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes)


def test_credit_exceeding_width_is_refused_as_a_stale_quote():
    strikes, occ, quotes = chain()
    c = build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes)
    # make the sold legs absurdly rich so the credit exceeds the wing width
    for leg in (c.legs[0], c.legs[2]):
        quotes[leg.symbol] = Quote(60.0, 60.2)
    with pytest.raises(IVUnavailable, match="stale quote"):
        build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes)


def test_empty_chain_is_refused():
    with pytest.raises(IVUnavailable):
        build_condor("X", F, 0.20, T, "e", 7, [], {}, {})


# --- sizing --------------------------------------------------------------

def test_sizing_is_off_the_exact_worst_case():
    strikes, occ, quotes = chain()
    c = build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes)
    per = c.max_loss * 100.0
    assert c.contracts_for_risk(per * 3) == 3
    assert c.contracts_for_risk(per * 0.9) == 0


def test_sizing_never_exceeds_the_budget():
    strikes, occ, quotes = chain()
    c = build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes)
    for budget in (500.0, 2_500.0, 10_000.0, 25_000.0):
        n = c.contracts_for_risk(budget)
        assert n * c.max_loss * 100.0 <= budget + 1e-9


# --- uneven strike spacing ----------------------------------------------
# The first version of test_builder_produces_equal_wings used a uniform $5 grid,
# where equalised wings and raw one-strike-out wings are indistinguishable — so
# the mutation that skips equalisation survived it. Real chains are not uniform:
# QQQ lists $1 strikes near the money and $5 further out, which is exactly how
# the original build produced a 5-wide call wing against a 1-wide put wing.

def uneven_chain(iv=0.20):
    """$1 spacing below the forward, $20 above.

    The spacing has to be lopsided enough that the RAW wings differ. An earlier
    version used $1/$5, where both sides happened to land 10 wide — so
    equalisation was a no-op and every mutation that skipped it survived the
    test named for it. A mutation loop that is not re-run after the fix is not
    a mutation loop.
    """
    strikes, occ, quotes = [], {}, {}
    # 628 is a deliberate near miss: it sits just SHORT of the wing target, so
    # `_nearest` would pick it and build a wing narrower than the volatility
    # implies, while `_at_least` steps past to 640. Without a near miss the two
    # are indistinguishable and the choice between them is untested.
    ks = ([float(k) for k in range(500, 601)] + [628.0]
          + [float(k) for k in range(620, 761, 20)])
    for k in ks:
        strikes.append(k)
        for right, is_call in (("C", True), ("P", False)):
            s = sym(k, right)
            occ[(k, right)] = s
            p = bs_price_forward(F, k, T, R, iv, is_call)
            if p < 0.05:
                continue
            half = max(0.01, p * 0.03 / 2)
            quotes[s] = Quote(round(p - half, 2), round(p + half, 2))
    return strikes, occ, quotes


def test_wings_are_equal_on_an_unevenly_spaced_chain():
    strikes, occ, quotes = uneven_chain()
    c = build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes,
                     short_delta=0.25)
    ks = sorted(parse(l.symbol).strike for l in c.legs)
    put_w, call_w = ks[1] - ks[0], ks[3] - ks[2]
    assert call_w == put_w, f"unequal wings on an uneven chain: {put_w} vs {call_w}"

    # Prove the fixture DISCRIMINATES: without equalisation the two sides would
    # differ, so a mutation that skips it must change the answer. The previous
    # fixture failed this — both raw wings were 10, so the test passed with or
    # without the code under test.
    sp, sc = ks[1], ks[2]
    raw_call = min(k for k in strikes if k > sc + 1e-9 and k >= sc + call_w * 0.5)
    raw_put = max(k for k in strikes if k < sp - 1e-9 and k <= sp - call_w * 0.5)
    assert (raw_call - sc) != (sp - raw_put), \
        "fixture does not discriminate: raw wings are already equal"

    # And the wing must REACH the width the volatility implies, not merely land
    # near it. A wing narrower than intended understates max loss, which
    # oversizes the position.
    move = F * 0.20 * math.sqrt(T)
    assert call_w >= move * 0.6 * 0.99, \
        f"wing {call_w} falls short of the {move * 0.6:.1f} the vol implies"


def flipped_chain(iv=0.45):
    """$10 spacing BELOW the forward, $1 above — the mirror of `uneven_chain`.

    Higher vol than the other fixtures on purpose: at 20% the put wing lands
    far enough out that it has no quote at all, and the build refuses for an
    unrelated reason.

    Both orientations are needed. On a chain where the CALL side is coarser, the
    raw call wing is already the wider of the two, so `width = lc - sc` and
    `width = max(...)` agree and a mutation between them is invisible. Only the
    mirrored chain separates them.
    """
    strikes, occ, quotes = [], {}, {}
    ks = [float(k) for k in range(460, 601, 10)] + [float(k) for k in range(601, 761)]
    for k in ks:
        strikes.append(k)
        for r, is_call in (("C", True), ("P", False)):
            px = bs_price_forward(F, k, T, R, iv, is_call)
            if px < 0.05:
                continue
            half = max(0.01, px * 0.03 / 2)
            occ[(k, r)] = sym(k, r)
            quotes[occ[(k, r)]] = Quote(round(px - half, 2), round(px + half, 2))
    for k in ks:
        for r in ("C", "P"):
            occ.setdefault((k, r), sym(k, r))
    return strikes, occ, quotes


def test_wings_are_equal_when_the_put_side_is_the_coarse_one():
    strikes, occ, quotes = flipped_chain()
    c = build_condor("X", F, 0.45, T, "e", 7, strikes, occ, quotes,
                     short_delta=0.25)
    ks = sorted(parse(l.symbol).strike for l in c.legs)
    put_w, call_w = ks[1] - ks[0], ks[3] - ks[2]
    assert call_w == put_w, f"unequal wings: put {put_w} vs call {call_w}"


def test_a_chain_too_coarse_for_the_intended_wings_is_refused():
    """A wing must reach the width the volatility implies. `_nearest` on a
    lopsided ladder returns whatever is adjacent — an audit produced a $1-wide
    condor sized at 147 contracts, because a tiny max_loss passes every cap."""
    # Strikes clustered right against the shorts and nothing beyond, so the
    # widest EQUAL wings available are $1 where the vol implies about $10.
    strikes = [586.0, 587.0, 588.0, 589.0, 611.0, 612.0, 613.0, 614.0]
    occ = {(k, r): f"XYZ260903{r}{int(k * 1000):08d}" for k in strikes
           for r in ("C", "P")}
    quotes = {}
    for k in strikes:
        for r, is_call in (("C", True), ("P", False)):
            px = bs_price_forward(F, k, T, R, 0.20, is_call)
            if px < 0.05:
                continue
            quotes[occ[(k, r)]] = Quote(round(px * 0.99, 2), round(px * 1.01, 2))
    # 25-delta puts the shorts at 589/611, so wings exist but the furthest
    # available is $3 out where the volatility implies about $10. The refusal
    # must be for THAT reason — an earlier fixture had the shorts landing on the
    # outermost strikes, which refuses for "no wing beyond" instead and tests
    # nothing about width.
    with pytest.raises(IVUnavailable, match="too coarse"):
        build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes,
                     short_delta=0.25)


def test_uneven_chain_max_loss_still_matches_brute_force():
    strikes, occ, quotes = uneven_chain()
    c = build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes)
    assert c.max_loss == pytest.approx(
        brute_force_max_loss(c.legs, c.entry), abs=0.02)


def test_unequal_wings_would_understate_risk():
    """Why equalisation matters: max loss comes from the WIDER side while the
    credit is collected on both, so an unbalanced condor sized off the narrow
    wing is silently oversized."""
    balanced = condor_legs(560.0, 640.0, 10.0)
    lopsided = (Leg(sym(640, "C"), "sell"), Leg(sym(660, "C"), "buy"),
                Leg(sym(560, "P"), "sell"), Leg(sym(558, "P"), "buy"))
    assert exact_max_loss(lopsided, -2.5) > exact_max_loss(balanced, -2.5)
    assert exact_max_loss(lopsided, -2.5) == pytest.approx(17.5)


def test_shorts_on_the_wrong_side_of_the_forward_are_refused():
    """A chain listing strikes only above the forward makes the 'short put' land
    above it, which is not a condor at all."""
    strikes, occ, quotes = chain(lo=610.0, hi=760.0)
    with pytest.raises(IVUnavailable, match="straddle the forward"):
        build_condor("X", F, 0.20, T, "e", 7, strikes, occ, quotes)


def test_wing_selection_reaches_past_the_target_not_merely_near_it():
    """`_at_least` / `_at_most`, unit-tested directly.

    `_nearest` returns whatever is closest, which on a ladder with a strike just
    SHORT of the target picks the narrow side — and a wing narrower than
    intended understates max loss, which oversizes the position. The difference
    only shows on a ladder that has a near miss below the target, so it is
    tested here rather than hoped for from a chain fixture.
    """
    from trading_bot.options.condor import _at_least, _at_most, _nearest
    ladder = [608.0, 628.0, 640.0]
    assert _nearest(ladder, 630.0) == 628.0        # closest, but too narrow
    assert _at_least(ladder, 630.0) == 640.0       # first that actually reaches
    assert _at_most([592.0, 572.0, 560.0], 570.0) == 560.0
    assert _nearest([592.0, 572.0, 560.0], 570.0) == 572.0
    # falls back to the widest available rather than returning nothing
    assert _at_least([605.0, 610.0], 700.0) == 610.0
    assert _at_most([595.0, 590.0], 500.0) == 590.0
