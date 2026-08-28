"""What to do this cycle: a pure function from state to intentions.

Rewritten after audit. The previous version delegated sizing to a greedy
optimiser over a scenario engine; that path produced a position risking 98% of
the account while reporting 11.5%, and four of its guards could be deleted
without a single test noticing. None of it is here. Sizing is arithmetic against
an exact worst case, and every rule below is one a person can check by hand.

Ordered by cost of error, and the expensive ones are all about the clock:

    0. market closed         -> do nothing; a stale quote is not a price
    1. past the flat-by time -> close everything
    2. expiry does not settle before grading -> close
    3. profit target reached -> close
    4. loss limit breached   -> close

Opening happens last, with whatever risk budget the survivors leave.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from .clock import (close_positions_by, flat_by, is_market_open,
                    last_expiry_settling_before, require_et, session_fraction)
from .condor import Condor
from .occ import parse
from .spreads import Leg


@dataclass(frozen=True)
class Holding:
    """An options structure we already own."""
    entry_id: str
    underlying: str
    legs: tuple[Leg, ...]
    qty: int
    entry: float             # net price per share; negative = opened for credit
    mark: float              # what it costs to close now, same convention
    max_loss: float

    @property
    def credit(self) -> float:
        return -self.entry

    @property
    def pnl_per_share(self) -> float:
        """Closing now realises entry minus mark: opened for a credit
        (entry < 0), bought back for a debit (mark > 0)."""
        return -(self.mark + self.entry)

    @property
    def pnl_usd(self) -> float:
        return self.pnl_per_share * 100.0 * self.qty

    @property
    def risk_usd(self) -> float:
        return self.max_loss * 100.0 * self.qty

    @property
    def profit_captured(self) -> float:
        """Fraction of the credit already earned."""
        c = self.credit
        return self.pnl_per_share / c if c > 0 else 0.0

    @property
    def loss_multiple(self) -> float:
        """Loss as a multiple of the credit received — the standard way a
        credit structure is managed, and scale-free across underlyings."""
        c = self.credit
        return (-self.pnl_per_share / c) if c > 0 else 0.0

    @property
    def expiry(self) -> dt.date:
        return max(parse(l.symbol).expiry for l in self.legs)


@dataclass
class SessionPlan:
    close: list[tuple[Holding, str]] = field(default_factory=list)
    open: list[tuple[Condor, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    halted: bool = False

    def summary(self) -> str:
        bits = [f"{len(self.close)} to close",
                f"{len(self.open)} to open"]
        if self.halted:
            bits.append("HALTED")
        return ", ".join(bits)


def plan_session(*, now: dt.datetime, start: dt.datetime, deadline: dt.datetime,
                 equity: float, holdings: list[Holding],
                 candidates: list[Condor],
                 max_risk_pct: float = 0.25, per_name_pct: float = 0.08,
                 profit_target: float = 0.60, loss_limit: float = 2.0,
                 min_credit_to_risk: float = 0.12,
                 min_hold_hours: float = 6.0,
                 require_open_market: bool = True,
                 flatten: bool = False) -> SessionPlan:
    now = require_et("now", now)
    start = require_et("start", start)
    deadline = require_et("deadline", deadline)
    if not (equity > 0 and math.isfinite(equity)):
        raise ValueError(f"equity must be positive and finite, got {equity!r}")

    plan = SessionPlan()

    # --- 0. never act on a closed book
    if require_open_market and not is_market_open(now):
        plan.halted = True
        plan.notes.append(
            f"market closed at {now:%Y-%m-%d %H:%M %Z}: no orders. Marks are "
            f"stale and an exit ladder has nothing to fill against.")
        return plan

    # --- 1a. operator kill switch: close everything, open nothing.
    # Same code path as the deadline flatten, so exercising this exercises that.
    if flatten:
        plan.halted = True
        plan.notes.append("flatten requested: closing all, opening nothing")
        plan.close = [(h, "flatten requested") for h in holdings]
        return plan

    # --- 1. the deadline overrides everything
    fb = flat_by(deadline)
    if now >= fb:
        plan.halted = True
        plan.notes.append(f"past flat-by {fb:%Y-%m-%d %H:%M %Z}: closing all")
        plan.close = [(h, "deadline") for h in holdings]
        return plan

    last_safe = last_expiry_settling_before(deadline)

    # --- 2. do not open before the measurement window starts
    #
    # P&L earned or lost before the start does not count toward the measured
    # return, but the risk and the starting-equity baseline both do — so a loss
    # here begins the contest already behind, for nothing. Closing stays
    # enabled, because a position that exists must always be manageable.
    before_start = now < start
    if before_start:
        plan.notes.append(
            f"before the window opens at {start:%Y-%m-%d %H:%M %Z}: managing "
            f"existing positions only, opening nothing")

    survivors: list[Holding] = []
    for h in holdings:
        # Close before expiry, never through it. A short leg finishing in the
        # money is exercised, and one assigned leg turns a defined-risk condor
        # into a large naked stock position — an audit measured $2.6M of short
        # SPY notional against $100k of equity. Expiry also removes the quotes
        # the position needs to be managed at all.
        deadline_for_this = close_positions_by(h.expiry)
        if now >= deadline_for_this:
            plan.close.append(
                (h, f"expiry day: flat by {deadline_for_this:%H:%M %Z}"))
        elif h.expiry > last_safe:
            # Settled beats marked: a contract expiring after grading contributes
            # a broker model on a 100%-extrinsic position, not a realised number.
            plan.close.append((h, f"expires {h.expiry}, after {last_safe}"))
        elif h.profit_captured >= profit_target:
            plan.close.append((h, f"took {h.profit_captured:.0%} of credit"))
        elif h.loss_multiple >= loss_limit:
            plan.close.append((h, f"lost {h.loss_multiple:.1f}x the credit"))
        else:
            survivors.append(h)

    elapsed = session_fraction(now, start, deadline)
    held = sum(h.risk_usd for h in survivors)
    budget = equity * max_risk_pct - held
    plan.notes.append(
        f"{elapsed:.0%} of tradable window used; holding ${held:,.0f} of a "
        f"${equity * max_risk_pct:,.0f} budget")

    if before_start:
        return plan
    if budget < equity * 0.01:
        plan.notes.append("no meaningful room to add")
        return plan

    per_name = equity * per_name_pct
    by_name = {h.underlying: h.risk_usd for h in survivors}
    for c in sorted(candidates, key=lambda x: -x.credit_to_risk):
        # Never open something we would have to close again almost immediately.
        # Being past the close-out time is the obvious case; being an hour short
        # of it is the same mistake with extra steps, because a round trip costs
        # about 10% of the credit and an hour of decay is worth far less than
        # that. Measured on the dev account: opening and closing four structures
        # seven minutes apart cost $488.
        must_close = close_positions_by(max(parse(l.symbol).expiry for l in c.legs))
        hours_left = (must_close - now).total_seconds() / 3600.0
        if hours_left < min_hold_hours:
            plan.notes.append(
                f"{c.underlying}: only {max(hours_left, 0):.1f}h before its "
                f"close-out, under the {min_hold_hours:.0f}h minimum hold")
            continue
        if not all(parse(l.symbol).expiry <= last_safe for l in c.legs):
            plan.notes.append(f"{c.underlying}: expiry after {last_safe}, skipped")
            continue
        if c.credit_to_risk < min_credit_to_risk:
            plan.notes.append(
                f"{c.underlying}: {c.credit_to_risk:.0%} credit/risk below "
                f"{min_credit_to_risk:.0%}, skipped")
            continue
        room = min(budget, per_name - by_name.get(c.underlying, 0.0))
        qty = c.contracts_for_risk(room)
        if qty < 1:
            continue
        plan.open.append((c, qty))
        used = qty * c.max_loss * 100.0
        budget -= used
        by_name[c.underlying] = by_name.get(c.underlying, 0.0) + used

    if not plan.open:
        plan.notes.append("no candidate passed the gates")
    return plan
