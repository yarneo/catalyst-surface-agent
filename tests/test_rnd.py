"""The density extraction has an analytical ground truth, so use it.

If the implied-vol smile is flat at sigma, Black-Scholes IS the market's model,
and the risk-neutral density recovered from the chain must be the lognormal
density at that sigma. Any error in the smoothing, the repricing or the second
derivative shows up as a mismatch against a closed form. That is a much
stronger check than asserting the output "looks like a distribution".
"""
import math

import numpy as np
import pytest

from trading_bot.options.iv import IVUnavailable, Quote, bs_price
from trading_bot.options.rnd import (Density, fit_smile, lognormal_density,
                                     regrid, risk_neutral_density)

SPOT, T, R = 100.0, 30 / 365, 0.04


def chain(sigma_fn, *, lo=80.0, hi=120.0, step=1.0, spread=0.02):
    """Synthetic quotes generated from a known vol function."""
    q = {}
    K = lo
    while K <= hi:
        v = sigma_fn(K)
        p = bs_price(SPOT, K, T, R, v, True)
        q[K] = Quote(round(max(p - spread / 2, 0.01), 4), round(p + spread / 2, 4))
        K += step
    return q


def test_flat_smile_recovers_the_lognormal_density():
    sigma = 0.20
    smile = fit_smile(SPOT, T, chain(lambda K: sigma), r=R)
    rnd = risk_neutral_density(smile)
    truth = regrid(lognormal_density(SPOT, T, sigma, r=R, drift=R), rnd.prices)
    # compare where the mass actually is
    m = truth.pdf > truth.pdf.max() * 0.01
    err = np.abs(rnd.pdf[m] - truth.pdf[m]).max() / truth.pdf.max()
    assert err < 0.10, f"density off closed form by {err:.1%}"


def test_flat_smile_fits_the_right_level():
    smile = fit_smile(SPOT, T, chain(lambda K: 0.20), r=R)
    assert abs(smile.atm_iv - 0.20) < 0.005
    assert abs(smile.skew) < 0.01, "a flat smile must have no skew"


def test_density_integrates_to_one_and_is_non_negative():
    smile = fit_smile(SPOT, T, chain(lambda K: 0.25), r=R)
    d = risk_neutral_density(smile)
    assert (d.pdf >= 0).all()
    assert abs(np.trapezoid(d.pdf, d.prices) - 1.0) < 1e-6
    assert abs(d.prob_below(d.prices[-1]) - 1.0) < 1e-3


def test_skewed_smile_is_detected_with_the_right_sign():
    """Equity skew: downside puts carry higher IV. `skew` must come out
    positive, because the sign is what decides which side we sell."""
    smile = fit_smile(SPOT, T, chain(lambda K: 0.20 + 0.30 * (SPOT - K) / SPOT), r=R)
    assert smile.skew > 0.02


def test_skew_moves_probability_mass_to_the_downside():
    """Not just a vol number — the recovered DISTRIBUTION must be left-skewed,
    since that is the claim the trade is sized against."""
    flat = risk_neutral_density(fit_smile(SPOT, T, chain(lambda K: 0.20), r=R))
    skewed = risk_neutral_density(
        fit_smile(SPOT, T, chain(lambda K: 0.20 + 0.30 * (SPOT - K) / SPOT), r=R))
    assert skewed.prob_below(90.0) > flat.prob_below(90.0)


def test_wider_vol_widens_the_density():
    narrow = risk_neutral_density(fit_smile(SPOT, T, chain(lambda K: 0.15), r=R))
    wide = risk_neutral_density(fit_smile(SPOT, T, chain(lambda K: 0.30), r=R))
    span = lambda d: d.quantile(0.9) - d.quantile(0.1)
    assert span(wide) > span(narrow) * 1.5


def test_refuses_a_chain_that_is_too_thin_to_fit():
    with pytest.raises(IVUnavailable, match="usable strikes"):
        fit_smile(SPOT, T, chain(lambda K: 0.20, lo=99.0, hi=101.0, step=1.0))


def test_unusable_wide_quotes_are_filtered_not_fitted():
    """A wing quoted 0.05 x 0.80 inverts to noise. It must be dropped, because
    one such point drags the spline across the whole tail."""
    q = chain(lambda K: 0.20)
    for K in (80.0, 81.0, 82.0):
        q[K] = Quote(0.05, 0.80)
    smile = fit_smile(SPOT, T, q, r=R)
    assert 80.0 not in smile.strikes
    assert abs(smile.atm_iv - 0.20) < 0.01


def test_extrapolation_is_clamped_not_extended():
    """Past the last quoted strike the smile holds flat. Letting a cubic spline
    run free out there produces negative variance within a few strikes."""
    smile = fit_smile(SPOT, T, chain(lambda K: 0.20 + 0.30 * (SPOT - K) / SPOT), r=R)
    far = smile.iv_at(np.array([1.0, 10.0, 500.0]))
    assert (far > 0).all() and (far < 3.0).all()


def test_probability_helpers_agree_with_each_other():
    d = risk_neutral_density(fit_smile(SPOT, T, chain(lambda K: 0.22), r=R))
    assert abs(d.prob_above(100.0) + d.prob_below(100.0) - 1.0) < 1e-3
    assert d.quantile(0.5) == pytest.approx(d.prices[np.argmax(np.cumsum(d.pdf) >= np.cumsum(d.pdf)[-1] / 2)], rel=0.02)


def test_risk_neutral_mean_equals_the_forward():
    """A theoretical identity, and the single best check that the whole
    pipeline is right: under the risk-neutral measure E[S_T] = S*e^{rT}.

    Nothing in the code enforces this. It falls out only if the smile fit, the
    repricing and the second derivative are all correct, so a mismatch means a
    bug somewhere in the chain rather than a bad assumption.
    """
    for sigma in (0.15, 0.25, 0.40):
        smile = fit_smile(SPOT, T, chain(lambda K: sigma), r=R)
        rnd = risk_neutral_density(smile)
        fwd = SPOT * math.exp(R * T)
        assert abs(rnd.mean - fwd) / fwd < 0.01, f"sigma={sigma}: {rnd.mean} vs {fwd}"


def test_forward_identity_survives_a_skewed_smile():
    """Skew moves mass around but must not move the mean off the forward."""
    smile = fit_smile(SPOT, T, chain(lambda K: 0.22 + 0.30 * (SPOT - K) / SPOT), r=R)
    rnd = risk_neutral_density(smile)
    fwd = SPOT * math.exp(R * T)
    assert abs(rnd.mean - fwd) / fwd < 0.02


# --- forward / dividend handling ----------------------------------------
# Live QQQ showed put-implied vol 0.8 points above call-implied vol at every
# strike. Parity forbids that, so the error was our forward: we assumed 4% carry
# and no dividend, the market implied 2.86%, and the difference was the yield.
# Understating the forward makes calls look cheap and puts rich, which invents a
# bullish skew out of a modelling choice.

from trading_bot.options.iv import (bs_price_forward, implied_forward,
                                    implied_vol_forward)

DIV_Q = 0.012


def dividend_chain(sigma=0.20, lo=80.0, hi=120.0, step=1.0, spread_pct=0.02):
    """Calls AND puts generated from a forward that includes a dividend.

    The spread is proportional and options worth less than a nickel are simply
    not listed. An absolute spread floor around a near-worthless wing makes its
    mid a pure artefact — 0.01 against a true value of 0.0001 inverts to a 24%
    vol on a chain built at 20% — which is a property of the fixture, not of
    any market, and it would make this test fail for the wrong reason.
    """
    F = SPOT * math.exp((R - DIV_Q) * T)
    calls, puts = {}, {}
    K = lo
    while K <= hi:
        for book, is_call in ((calls, True), (puts, False)):
            px = bs_price_forward(F, K, T, R, sigma, is_call)
            if px < 0.05:
                continue
            half = max(0.005, px * spread_pct / 2)
            book[K] = Quote(round(px - half, 4), round(px + half, 4))
        K += step
    return F, calls, puts


def test_forward_is_recovered_from_parity_not_assumed():
    F, calls, puts = dividend_chain()
    got = implied_forward(T, calls, puts, spot=SPOT, r=R)
    assert abs(got - F) < 0.02, f"parity gave {got}, truth {F}"
    # a 1.2% yield over 30 days moves a $100 forward by ~$0.10 — small in
    # absolute terms and still enough to open an 0.8-vol-point put/call gap,
    # which is exactly why it went unnoticed on live data
    naive = SPOT * math.exp(R * T)
    assert abs(got - naive) > 0.05, f"fixture has no dividend effect to detect"


def test_ignoring_dividends_biases_call_and_put_iv_apart():
    """Reproduce the live bug, so the fix has something to be a fix OF."""
    F, calls, puts = dividend_chain(sigma=0.20)
    naive_F = SPOT * math.exp(R * T)
    ivc = implied_vol_forward(calls[100.0].mid, naive_F, 100.0, T, R, call=True)
    ivp = implied_vol_forward(puts[100.0].mid, naive_F, 100.0, T, R, call=False)
    assert ivp - ivc > 0.005, "expected the put/call vol gap the wrong forward creates"
    # and with the right forward they agree
    ivc2 = implied_vol_forward(calls[100.0].mid, F, 100.0, T, R, call=True)
    ivp2 = implied_vol_forward(puts[100.0].mid, F, 100.0, T, R, call=False)
    assert abs(ivp2 - ivc2) < 0.002


def test_smile_from_a_dividend_chain_is_flat_and_unskewed():
    """A chain built at constant vol must fit as constant vol. Before the fix
    this produced a phantom positive skew equal to the carry error."""
    F, calls, puts = dividend_chain(sigma=0.20)
    smile = fit_smile(SPOT, T, calls, puts, r=R)
    assert abs(smile.forward - F) < 0.02
    assert abs(smile.atm_iv - 0.20) < 0.005
    assert abs(smile.skew) < 0.01, f"phantom skew {smile.skew:+.4f}"


def test_the_dividend_is_captured_in_the_forward():
    """Checked against the smile's forward, which comes from parity and is
    exact, rather than the density's mean. The density is clipped to the range
    of strikes that actually trade, and a truncated distribution has a slightly
    different mean by construction — a lognormal's left tail is further out in
    log terms, so trimming equally in price trims more from the left and nudges
    the mean up by ~0.05%. Small, real, and not a place to test a dividend."""
    F, calls, puts = dividend_chain(sigma=0.25)
    smile = fit_smile(SPOT, T, calls, puts, r=R)
    assert abs(smile.forward - F) < 0.02
    assert abs(smile.forward - SPOT * math.exp(R * T)) > 0.05


def test_density_mean_stays_close_to_the_forward_after_clipping():
    F, calls, puts = dividend_chain(sigma=0.25)
    rnd = risk_neutral_density(fit_smile(SPOT, T, calls, puts, r=R))
    assert abs(rnd.mean - F) / F < 0.01


def test_density_is_clipped_to_strikes_that_actually_trade():
    """Past the last quoted strike the smile is held flat, so the density there
    is a Black-Scholes tail bolted onto a fitted middle. The join showed as a
    visible step in the dashboard — mass in a region no option was quoted."""
    F, calls, puts = dividend_chain(sigma=0.25)
    smile = fit_smile(SPOT, T, calls, puts, r=R)
    rnd = risk_neutral_density(smile)
    assert rnd.prices[0] >= smile.strikes[0] - 1e-9
    assert rnd.prices[-1] <= smile.strikes[-1] + 1e-9


def test_thin_pairs_refuse_rather_than_guess_a_forward():
    from trading_bot.options.iv import ForwardUnavailable
    F, calls, puts = dividend_chain()
    thin = {k: v for k, v in puts.items() if k < 85.0}
    with pytest.raises(ForwardUnavailable, match="usable call/put pairs"):
        implied_forward(T, calls, thin, spot=SPOT, r=R)


def test_density_that_misses_the_forward_is_refused():
    """The forward identity is the only free correctness check on the whole
    pipeline, so a density that fails it must not reach a position sizer."""
    F, calls, puts = dividend_chain(sigma=0.25)
    smile = fit_smile(SPOT, T, calls, puts, r=R)
    with pytest.raises(IVUnavailable, match="off the forward"):
        risk_neutral_density(smile, max_forward_error=1e-9)
    # and it passes at a sane tolerance
    assert risk_neutral_density(smile, max_forward_error=0.01) is not None
