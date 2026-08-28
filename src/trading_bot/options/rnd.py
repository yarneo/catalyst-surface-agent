"""What the market believes, recovered from the option chain.

Many trading systems predict a *price*. An option chain
contains something strictly richer: the market's entire probability
distribution for the underlying at expiry — its centre, its skew, and its
tails. Breeden and Litzenberger (1978) showed the risk-neutral density is the
second derivative of the call price with respect to strike:

    q(K) = e^{rT} * d2C/dK2

That identity is exact, and it is also numerically hostile. Second differences
of noisy mid-quotes produce garbage: a penny of bid-ask jitter at adjacent
strikes turns into a density with negative lobes. The fix is standard practice
and is where most of the care in this module lives — do not differentiate the
prices, differentiate a smooth *surface* fitted through them:

    1. invert every usable quote to implied vol
    2. fit a smooth curve to IV against log-moneyness, which is the space where
       the smile is nearly quadratic and a spline behaves
    3. reprice a dense synthetic call ladder off that curve
    4. differentiate the ladder

Step 2 is doing real work. IV varies slowly and smoothly across strikes while
price varies fast and convexly, so smoothing in vol space removes quote noise
without smearing the shape of the distribution.

The output is the input to the only question this agent asks: where does the
market's distribution disagree with ours, and is the disagreement big enough to
pay for the spread we have to cross to trade it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.interpolate import UnivariateSpline

from .iv import (IVUnavailable, Quote, bs_price_forward, implied_forward,
                 implied_vol_forward)


@dataclass(frozen=True)
class Smile:
    """The fitted implied-vol curve for one expiry, in forward moneyness.

    Parameterised by the FORWARD rather than spot. Moneyness is what the smile
    is actually a function of, and the forward is where the distribution is
    centred — using spot instead shifts the whole curve by the carry and makes
    the call and put wings disagree about where the money is.
    """
    spot: float
    forward: float
    T: float
    r: float
    strikes: np.ndarray        # observed strikes that survived filtering
    ivs: np.ndarray            # their implied vols
    _spline: object

    def iv_at(self, K: float | np.ndarray) -> np.ndarray:
        """IV at arbitrary strikes, clamped to the observed range.

        Extrapolating a spline past the last quoted strike is how a smile turns
        into a negative variance three strikes out. Beyond the data we hold the
        edge value flat, which is wrong in a known direction rather than wrong
        in an unbounded one.
        """
        k = np.log(np.atleast_1d(np.asarray(K, dtype=float)) / self.forward)
        lo = np.log(self.strikes[0] / self.forward)
        hi = np.log(self.strikes[-1] / self.forward)
        return np.asarray(self._spline(np.clip(k, lo, hi)), dtype=float)

    @property
    def atm_iv(self) -> float:
        """Vol at the forward — the true at-the-money, not at-the-spot."""
        return float(self.iv_at(self.forward)[0])

    @property
    def skew(self) -> float:
        """IV 5% below the forward minus IV 5% above.

        Positive is the normal equity shape: downside puts bid up relative to
        upside calls, because everyone is hedging the same crash. It is also
        the number that says which side of the distribution is expensive — so
        it must be measured against the forward, or the carry leaks into it and
        fabricates skew that is not there.
        """
        return float(self.iv_at(self.forward * 0.95)[0]
                     - self.iv_at(self.forward * 1.05)[0])


@dataclass(frozen=True)
class Density:
    """A probability density over terminal price, on a fixed grid."""
    prices: np.ndarray
    pdf: np.ndarray

    def __post_init__(self) -> None:
        # Cache the CDF once. Every probability question below is answered by
        # interpolating it, never by masking the grid: a mask truncates to the
        # nearest grid points and silently drops the partial cells at each
        # boundary, which cost 0.8% of total mass in testing. That error would
        # flow straight into a position size.
        c = np.concatenate([[0.0], np.cumsum(np.diff(self.prices)
                                             * (self.pdf[1:] + self.pdf[:-1]) / 2)])
        object.__setattr__(self, "_cdf", c / c[-1] if c[-1] > 0 else c)

    def cdf_at(self, k: float) -> float:
        return float(np.interp(k, self.prices, self._cdf,
                               left=0.0, right=1.0))

    def prob_between(self, lo: float, hi: float) -> float:
        return max(0.0, self.cdf_at(hi) - self.cdf_at(lo))

    def prob_above(self, k: float) -> float:
        return 1.0 - self.cdf_at(k)

    def prob_below(self, k: float) -> float:
        return self.cdf_at(k)

    @property
    def mean(self) -> float:
        return float(np.trapezoid(self.prices * self.pdf, self.prices))

    def quantile(self, q: float) -> float:
        return float(np.interp(q, self._cdf, self.prices))


def fit_smile(spot: float, T: float, calls: dict[float, Quote],
              puts: dict[float, Quote] | None = None, *,
              r: float = 0.04, forward: float | None = None,
              max_spread_pct: float = 0.35, min_points: int = 6,
              smoothing: float = 2e-4) -> Smile:
    """Fit implied vol against log forward-moneyness.

    `calls` and `puts` map strike -> Quote for a SINGLE expiry.

    When puts are supplied the forward is read off put-call parity rather than
    assumed, and the smile is built from **out-of-the-money options only**:
    puts below the forward, calls above. This is standard practice for two
    reasons that both bit us on live data. An in-the-money option is mostly
    intrinsic value, so its vol is a small residual on a large number and the
    bid-ask swamps it. And an assumed carry — r with no dividend — puts the
    call and put wings on different forwards, which showed up as put-implied
    vol sitting 0.8 points above call-implied vol at every strike on QQQ. That
    gap is not skew. It is a modelling error that manufactures a bullish signal.

    With no puts, the forward falls back to spot*e^{rT} and the fit uses calls
    alone, which is correct for a synthetic dividend-free chain and wrong for a
    real one — hence the fallback is not the default path.
    """
    if forward is None:
        if puts:
            try:
                forward = implied_forward(T, calls, puts, spot=spot, r=r)
            except IVUnavailable:
                forward = spot * math.exp(r * T)
        else:
            forward = spot * math.exp(r * T)

    ks, vs = [], []
    sources: list[tuple[float, Quote, bool]] = []
    if puts:
        sources += [(K, q, False) for K, q in puts.items() if K < forward]
        sources += [(K, q, True) for K, q in calls.items() if K >= forward]
    else:
        sources += [(K, q, True) for K, q in calls.items()]

    for K, q, is_call in sorted(sources):
        if not q.usable(max_spread_pct=max_spread_pct):
            continue
        try:
            v = implied_vol_forward(q.mid, forward, K, T, r, call=is_call)
        except IVUnavailable:
            continue
        if 0.01 < v < 3.0:
            ks.append(float(K))
            vs.append(float(v))
    if len(ks) < min_points:
        raise IVUnavailable(
            f"only {len(ks)} usable strikes, need {min_points} to fit a smile")

    ks_a, vs_a = np.array(ks), np.array(vs)
    x = np.log(ks_a / forward)
    # s scales with the number of points: UnivariateSpline's smoothing factor is
    # a total squared-error budget, so a fixed s over-smooths a sparse chain and
    # under-smooths a dense one.
    spline = UnivariateSpline(x, vs_a, k=3, s=smoothing * len(x))
    return Smile(spot, float(forward), T, r, ks_a, vs_a, spline)


def risk_neutral_density(smile: Smile, *, n: int = 400,
                         width: float = 4.0,
                         max_forward_error: float = 0.01) -> Density:
    """Breeden-Litzenberger on a synthetic call ladder priced off the smile.

    `width` is how many forecast standard deviations of terminal price to cover.
    The grid must extend well past the traded strikes or the density gets
    truncated before its tails integrate to anything sensible, but the smile is
    held flat out there (see `Smile.iv_at`), so the far tail is Black-Scholes
    lognormal rather than a spline hallucination.

    The result is clipped at zero and renormalised. Clipping is a real
    admission: a perfectly arbitrage-free smile cannot produce negative density,
    so any negative lobe means the fit still carries quote noise. Zeroing it is
    the honest minimum — it does not pretend the artefact was information.
    """
    F, T = smile.forward, smile.T
    sd = smile.atm_iv * math.sqrt(max(T, 1e-6)) * F
    # Do not report density where there are no strikes. Past the last quoted
    # strike the smile is held flat, so the density there is a Black-Scholes
    # tail bolted onto a fitted middle, and the join shows as a step — visible
    # mass in a region no option was ever quoted. Clipping to the observed range
    # keeps the distribution to what the market actually priced. The result is a
    # truncated density, renormalised, which is an honest description of a chain
    # that only spans so far.
    lo = max(1e-6, F - width * sd, float(smile.strikes[0]))
    hi = min(F + width * sd, float(smile.strikes[-1]))
    if not (hi > lo):
        raise IVUnavailable(
            f"fitted strikes {smile.strikes[0]:.0f}-{smile.strikes[-1]:.0f} do "
            f"not span the forward {F:.2f}")
    K = np.linspace(lo, hi, n)
    iv = smile.iv_at(K)
    C = np.array([bs_price_forward(F, float(k), T, smile.r, float(v), True)
                  for k, v in zip(K, iv)])

    d2 = np.gradient(np.gradient(C, K), K)
    pdf = np.exp(smile.r * T) * d2
    pdf = np.clip(pdf, 0.0, None)
    area = np.trapezoid(pdf, K)
    if not (area > 0 and math.isfinite(area)):
        raise IVUnavailable("degenerate risk-neutral density (zero mass)")
    d = Density(K, pdf / area)

    # Under the risk-neutral measure E[S_T] must equal the forward. Nothing here
    # enforces that — it holds only if the smile fit, the repricing and the
    # second derivative are all sound — which makes it the one free check on the
    # entire pipeline. A density that fails it is distorted somewhere, usually in
    # a clamped tail, and every probability read off it is wrong by an unknown
    # amount. Refuse rather than hand a quietly-broken distribution to a sizer.
    err = abs(d.mean - smile.forward) / smile.forward
    if err > max_forward_error:
        raise IVUnavailable(
            f"density mean {d.mean:.2f} is {err:.2%} off the forward "
            f"{smile.forward:.2f} — the extraction is distorted, not tradeable")
    return d


def lognormal_density(spot: float, T: float, sigma: float, *, r: float = 0.04,
                      drift: float = 0.0, n: int = 400,
                      width: float = 4.0) -> Density:
    """Our own forecast density: lognormal at `sigma` with an explicit drift.

    Deliberately the same functional form the market is using, so that a
    comparison between the two isolates the *parameters* — how wide, how
    skewed — rather than confounding them with a difference in model. When we
    say the market's distribution is too wide, that claim should not depend on
    us having picked a fatter-tailed family.
    """
    sd = sigma * math.sqrt(max(T, 1e-6)) * spot
    K = np.linspace(max(1e-6, spot - width * sd), spot + width * sd, n)
    mu = math.log(spot) + (drift - 0.5 * sigma ** 2) * T
    sig = sigma * math.sqrt(max(T, 1e-9))
    with np.errstate(divide="ignore", invalid="ignore"):
        pdf = np.where(K > 0,
                       np.exp(-((np.log(np.maximum(K, 1e-12)) - mu) ** 2) / (2 * sig ** 2))
                       / (np.maximum(K, 1e-12) * sig * math.sqrt(2 * math.pi)),
                       0.0)
    area = np.trapezoid(pdf, K)
    return Density(K, pdf / area)


def regrid(d: Density, prices: np.ndarray) -> Density:
    """Put a density on someone else's grid so the two can be compared."""
    pdf = np.interp(prices, d.prices, d.pdf, left=0.0, right=0.0)
    area = np.trapezoid(pdf, prices)
    return Density(prices, pdf / area if area > 0 else pdf)
