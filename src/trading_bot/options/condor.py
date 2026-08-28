"""One structure, built directly. No search.

This replaces the candidate search, the scenario engine and the probability-of-
target optimiser that six independent audits took apart. Those found ~50
defects, and the worst of them shared a single root: `max_loss` was read off a
probability grid that was narrower than the strikes being traded, so a structure
risking 98% of the account reported 11.5% and passed every cap.

The fix is not a better grid. An option payoff at expiry is piecewise linear
with kinks only at strikes, so the exact worst case is a minimum over a handful
of points and needs no distribution at all. `exact_max_loss` computes it. That
single change removes the dependency that made the risk number wrong.

What is left is deliberately conventional: sell a delta-selected strangle, buy
wings, size off the true worst case, hold to an expiry that settles before we
are measured. An audit also showed this earns more than the convex book it
replaces — 4% to 16% in the contest window against a claimed 2.45% ceiling that
turned out to be an artefact of measuring a 30-day structure over seven days.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import norm

from .iv import IVUnavailable, Quote
from .occ import parse
from .spreads import Leg, marketable_limit, payoff_at


class Unbounded(ValueError):
    """The structure loses without limit. Never sizeable, at any price."""


def exact_max_loss(legs: tuple[Leg, ...], entry: float) -> float:
    """Worst case per share over the whole price line, computed exactly.

    The payoff is piecewise linear and its kinks are exactly the strikes, so the
    minimum lies at a strike, at zero, or at infinity. Checking those points is
    both exact and cheap — no grid, no density, no discretisation.

    This is the correction for the single worst defect found in audit: the old
    implementation took `min` over the risk-neutral density's price grid, which
    is clipped to the range of strikes that have usable quotes. Structures are
    built from a wider strike pool than that, so the loss region simply was not
    on the grid. A butterfly whose true worst case was $2,040 reported $240,
    ranked FIRST because understating the loss inflates edge-per-risk in both
    the numerator and the denominator, and sized to 98% of the account.

    Raises `Unbounded` when the payoff falls without limit as price rises, which
    no expected value may override.
    """
    if not legs:
        raise ValueError("no legs")
    net_call = sum((1 if l.side == "buy" else -1) * l.ratio_qty
                   for l in legs if parse(l.symbol).is_call)
    if net_call < 0:
        raise Unbounded(f"net short {-net_call} call(s): loss is unbounded above")

    strikes = sorted({parse(l.symbol).strike for l in legs})
    # Zero covers the deepest put loss; one step past the top strike covers the
    # region beyond the last kink, where the payoff is already linear.
    probes = np.array([0.0] + strikes + [strikes[-1] * 2.0 + 1.0])
    pnl = payoff_at(legs, probes) - entry
    worst = float(np.min(pnl))
    return -worst


def _delta_strike(forward: float, iv: float, T: float, delta: float,
                  call: bool) -> float:
    """Strike whose Black-76 delta is `delta`. Off the FORWARD, not spot."""
    if not (0 < delta < 1) or iv <= 0 or T <= 0:
        raise IVUnavailable(f"bad delta inputs: delta={delta} iv={iv} T={T}")
    z = norm.ppf(delta if call else 1.0 - delta)
    v = iv * math.sqrt(T)
    return forward * math.exp(-z * v + 0.5 * v * v)


@dataclass(frozen=True)
class Condor:
    underlying: str
    legs: tuple[Leg, ...]
    entry: float            # marketable net price; negative = credit received
    max_loss: float         # exact, per share
    expiry: str
    dte: int
    short_call: float
    short_put: float
    width: float

    @property
    def credit(self) -> float:
        return -self.entry

    @property
    def max_profit(self) -> float:
        return self.credit

    @property
    def credit_to_risk(self) -> float:
        return self.credit / self.max_loss if self.max_loss > 0 else 0.0

    def contracts_for_risk(self, budget_usd: float) -> int:
        per = self.max_loss * 100.0
        return max(0, int(budget_usd // per)) if per > 0 else 0

    def describe(self) -> str:
        return (f"{self.underlying} {self.expiry} condor "
                f"{self.short_put - self.width:.0f}/{self.short_put:.0f}-"
                f"{self.short_call:.0f}/{self.short_call + self.width:.0f} "
                f"credit {self.credit:.2f} risk {self.max_loss:.2f} "
                f"({self.credit_to_risk:.0%})")


def build_condor(underlying: str, forward: float, iv: float, T: float,
                 expiry: str, dte: int, strikes: list[float], occ: dict,
                 quotes: dict[str, Quote], *, short_delta: float = 0.16,
                 wing_sigmas: float = 0.60, buffer: float = 0.02) -> Condor:
    """Short strangle at `short_delta`, wings `wing_sigmas` of one expected move
    further out.

    Wings are measured in VOLATILITY, not in percent of spot. The same "1.5% of
    spot" builds a half-sigma wing on SPY at 13 vol and a tenth-of-a-sigma wing
    on NVDA at 59 — structures with nothing in common but a parameter. Expressed
    in expected moves the geometry is the same across every underlying, which is
    what makes a single-name book comparable to an index one instead of secretly
    forty times more levered.
    """
    if not strikes:
        raise IVUnavailable(f"{underlying}: no strikes")
    move = forward * iv * math.sqrt(max(T, 1e-9))
    sc = _nearest(strikes, _delta_strike(forward, iv, T, short_delta, True))
    sp = _nearest(strikes, _delta_strike(forward, iv, T, short_delta, False))
    if not (sp < forward < sc):
        raise IVUnavailable(
            f"{underlying}: shorts {sp:.0f}/{sc:.0f} do not straddle the "
            f"forward {forward:.2f}")

    target = max(move * wing_sigmas, _min_gap(strikes))
    # At least `target`, never merely the nearest strike. `_nearest` on an
    # unevenly spaced ladder happily returns the adjacent strike — an audit
    # produced a $1-wide condor whose intended wings were $7.50, sized at 147
    # contracts because the tiny max_loss passed every cap.
    lc = _at_least([k for k in strikes if k > sc], sc + target)
    lp = _at_most([k for k in strikes if k < sp], sp - target)
    if lc is None or lp is None:
        raise IVUnavailable(
            f"{underlying}: no wing beyond {sp:.0f}/{sc:.0f} within the chain")

    # Equal width both sides. Unequal wings put max loss on one side while the
    # credit comes from both, which an audit showed silently oversizes whenever
    # strike spacing is uneven.
    # Equal width both sides, chosen as the WIDER of the two candidates so the
    # floor above is preserved; then require both sides to reach it exactly.
    width = max(lc - sc, sp - lp)
    lc_e = _nearest([k for k in strikes if k > sc], sc + width)
    lp_e = _nearest([k for k in strikes if k < sp], sp - width)
    if abs((lc_e - sc) - (sp - lp_e)) < 1e-9:
        lc, lp = lc_e, lp_e
        width = lc - sc
    else:
        width = min(lc - sc, sp - lp)
    if width < target * 0.5:
        raise IVUnavailable(
            f"{underlying}: widest equal wings available are {width:.2f}, "
            f"under half the {target:.2f} the vol implies — the chain is too "
            f"coarse here to build the intended structure")

    legs = tuple(Leg(s, side) for s, side in (
        (_sym(occ, sc, "C"), "sell"), (_sym(occ, sc + width, "C"), "buy"),
        (_sym(occ, sp, "P"), "sell"), (_sym(occ, sp - width, "P"), "buy")))
    for l in legs:
        if l.symbol is None:
            raise IVUnavailable(f"{underlying}: missing contract for a leg")
        q = quotes.get(l.symbol)
        if q is None or not q.usable(max_spread_pct=0.35):
            raise IVUnavailable(f"{underlying}: unusable quote for {l.symbol}")

    entry = marketable_limit(legs, quotes, buffer=buffer)
    max_loss = exact_max_loss(legs, entry)
    if max_loss <= 0:
        raise IVUnavailable(
            f"{underlying}: max loss {max_loss:.2f} — credit exceeds width, "
            f"which is a stale quote and not an arbitrage")
    return Condor(underlying, legs, entry, max_loss, expiry, dte,
                  float(sc), float(sp), float(width))


def _at_least(seq, target):
    """Smallest element >= target, else the largest available."""
    seq = sorted(seq)
    above = [k for k in seq if k >= target]
    return above[0] if above else (seq[-1] if seq else None)


def _at_most(seq, target):
    """Largest element <= target, else the smallest available."""
    seq = sorted(seq)
    below = [k for k in seq if k <= target]
    return below[-1] if below else (seq[0] if seq else None)


def _nearest(seq, target):
    seq = list(seq)
    return min(seq, key=lambda k: abs(k - target)) if seq else None


def _min_gap(strikes: list[float]) -> float:
    s = sorted(strikes)
    gaps = [b - a for a, b in zip(s, s[1:]) if b > a]
    return min(gaps) if gaps else 1.0


def _sym(occ: dict, k: float, right: str):
    return occ.get((float(k), right))
