"""Implied volatility, computed here rather than fetched.

Alpaca serves option *quotes* on the free `indicative` feed but no greeks and no
implied vol — those need an OPRA agreement (`feed=opra` returns 403 without
one). So this inverts Black-Scholes against the quote mid instead. Verified
against live SPY chains: 23-DTE near-ATM calls solve to ~14.3%, consistent
across adjacent strikes, which is the sanity check that matters — a broken
solver produces strike-to-strike noise, not a smooth surface.

Doing it ourselves is not a workaround, it is the strategy's core measurement.
Everything downstream is a comparison between this number and a realised-vol
forecast, so it has to be ours and it has to be honest about when it fails.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.optimize import brentq
from scipy.stats import norm


class IVUnavailable(RuntimeError):
    """No usable implied vol. Callers must skip the contract, never guess —
    a fabricated IV feeds straight into a position size."""


def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             call: bool = True) -> float:
    """Black-Scholes European price. No dividend term: these are short-dated
    index/equity options and the yield over 30 days is dwarfed by the bid-ask
    spread we are already tolerating."""
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def implied_vol(price: float, S: float, K: float, T: float, r: float = 0.04,
                call: bool = True, lo: float = 1e-4, hi: float = 5.0) -> float:
    """Invert Black-Scholes for sigma. Raises IVUnavailable if it cannot.

    Rejects prices outside the no-arbitrage bounds BEFORE solving. A quote below
    intrinsic value has no implied vol at all, and brentq handed such a price
    either fails obscurely or returns a boundary value that looks like a real
    number — the second is far more dangerous.
    """
    if T <= 0:
        raise IVUnavailable(f"expired or same-day: T={T}")
    if price <= 0:
        raise IVUnavailable(f"non-positive price {price}")

    intrinsic = max(0.0, (S - K * math.exp(-r * T)) if call else (K * math.exp(-r * T) - S))
    upper = S if call else K * math.exp(-r * T)
    if price < intrinsic - 1e-8:
        raise IVUnavailable(f"price {price:.4f} below intrinsic {intrinsic:.4f}")
    if price > upper:
        raise IVUnavailable(f"price {price:.4f} above upper bound {upper:.4f}")

    def f(sig: float) -> float:
        return bs_price(S, K, T, r, sig, call) - price

    try:
        if f(lo) > 0 or f(hi) < 0:
            raise IVUnavailable(
                f"no root in [{lo}, {hi}] for price {price:.4f} "
                f"(S={S:.2f} K={K:.2f} T={T:.4f})")
        return float(brentq(f, lo, hi, maxiter=100, xtol=1e-6))
    except IVUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise IVUnavailable(f"solver failed: {exc}") from exc


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        """Bid-ask as a fraction of mid. The single best liquidity filter for
        options: a 40%-wide spread means the mid is fiction and any IV derived
        from it is fiction too."""
        m = self.mid
        return (self.ask - self.bid) / m if m > 0 else float("inf")

    def usable(self, max_spread_pct: float = 0.20) -> bool:
        return (self.bid > 0 and self.ask > self.bid
                and self.spread_pct <= max_spread_pct)


# --- forward-based pricing ----------------------------------------------
#
# Everything above prices off spot with a rate and no dividend. That is wrong
# in a way that matters, and the market says so out loud: on a live QQQ chain,
# put-implied vol sat ~0.8 points ABOVE call-implied vol at every single strike.
# A genuine surface cannot do that — put-call parity forces one vol per strike.
# The gap was our forward. We assumed S*e^{rT} at r=4%; parity said the market's
# forward implied 2.86%, and the 1.14% difference is QQQ's dividend yield.
#
# The consequence was not academic. Understating the forward makes calls look
# cheap and puts look rich, which manufactures a bullish skew signal out of
# nothing — precisely the trade the search engine wanted to put on.
#
# So stop assuming. Parity gives the forward directly from prices, and pricing
# off that forward removes the rate and the dividend from the model at once.


def bs_price_forward(F: float, K: float, T: float, r: float, sigma: float,
                     call: bool = True) -> float:
    """Black-76: an option on a forward. No spot, no dividend, no carry
    assumption — F already contains all of it."""
    if T <= 0 or sigma <= 0:
        return math.exp(-r * T) * max(0.0, (F - K) if call else (K - F))
    d1 = (math.log(F / K) + sigma * sigma * T / 2) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    disc = math.exp(-r * T)
    if call:
        return disc * (F * norm.cdf(d1) - K * norm.cdf(d2))
    return disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def implied_vol_forward(price: float, F: float, K: float, T: float,
                        r: float = 0.04, call: bool = True,
                        lo: float = 1e-4, hi: float = 5.0) -> float:
    """Invert Black-76 for sigma."""
    disc = math.exp(-r * T)
    intrinsic = disc * max(0.0, (F - K) if call else (K - F))
    cap = disc * (F if call else K)
    if not (intrinsic - 1e-9 <= price <= cap + 1e-9):
        raise IVUnavailable(
            f"price {price:.4f} outside no-arbitrage [{intrinsic:.4f}, {cap:.4f}]")
    f = lambda s: bs_price_forward(F, K, T, r, s, call) - price  # noqa: E731
    try:
        if f(lo) * f(hi) > 0:
            raise IVUnavailable(f"no sign change for price {price:.4f} at K={K}")
        return float(brentq(f, lo, hi, xtol=1e-8, maxiter=200))
    except IVUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise IVUnavailable(f"solver failed at K={K}: {exc}") from exc


class ForwardUnavailable(IVUnavailable):
    """Not enough paired call/put quotes to read the forward off the market."""


def implied_forward(T: float, calls: dict[float, "Quote"], puts: dict[float, "Quote"],
                    *, spot: float, r: float = 0.04, band: float = 0.03,
                    min_pairs: int = 3) -> float:
    """Read the forward out of put-call parity: F = K + (C - P)e^{rT}.

    Uses the MEDIAN across near-the-money strikes, not the mean. Parity is
    exact, so every usable strike should give the same answer and any that does
    not is a stale quote — the median ignores those, a mean would be dragged by
    them.

    Restricted to a narrow band around spot because C-P is a small difference
    of two large numbers far from the money, where the bid-ask noise swamps the
    signal it is supposed to carry.
    """
    fs = []
    for K in sorted(set(calls) & set(puts)):
        if abs(K - spot) / spot > band:
            continue
        c, p = calls[K], puts[K]
        if not (c.usable() and p.usable()):
            continue
        fs.append(K + (c.mid - p.mid) * math.exp(r * T))
    if len(fs) < min_pairs:
        raise ForwardUnavailable(
            f"only {len(fs)} usable call/put pairs within {band:.0%} of spot, "
            f"need {min_pairs}")
    fs.sort()
    n = len(fs)
    return fs[n // 2] if n % 2 else (fs[n // 2 - 1] + fs[n // 2]) / 2
