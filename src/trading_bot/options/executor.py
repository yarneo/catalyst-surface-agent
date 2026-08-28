"""Carrying out a plan, and surviving the ways that goes wrong.

This exists as a module rather than as a loop inside the runner script for one
reason: the last audit of this repo found every real bug living in
orchestration code that no test covered, because testing it meant touching a
broker. The same thing had already happened again here — six defects in the
inline version, none of them reachable by any test, all of them in the handful
of lines between "decide" and "recorded".

The invariants, in the order they matter:

**Record what filled, not what was requested.** A partial fill on a four-leg
order is not a rounding difference; it is a different position. Writing the
intended quantity into the registry makes the agent size against risk it does
not hold, and the error compounds on every later cycle.

**One failure must not abort the cycle.** Orders are independent. A timeout on
the third close cannot be allowed to skip the fourth, or to leave the book
half-managed with no record of why.

**Close before open, always.** Freeing risk before adding it is the difference
between a book at its limit and a book at twice it.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass, field

from .book import Book, BookEntry
from .clock import now_et
from .execution import Fill, close_spread, open_spread
from .condor import Condor
from .session import Holding, SessionPlan


@dataclass
class ExecutionReport:
    closed: list[tuple[str, str, Fill]] = field(default_factory=list)
    # Keep the exact registry row, not just its ID.  Callers must report what
    # actually filled; pairing this shorter list back to plan.open by position
    # mislabels every later success when an earlier order does not fill.
    opened: list[tuple[BookEntry, Fill]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"{len(self.closed)} closed", f"{len(self.opened)} opened"]
        if self.failures:
            bits.append(f"{len(self.failures)} FAILED")
        if self.orphans:
            bits.append(f"{len(self.orphans)} ORPHANED")
        return ", ".join(bits)

    def requires_attention(self) -> bool:
        """Whether the scheduler should surface this cycle as unhealthy.

        A confirmed, canceled entry non-fill is a routine strategy outcome:
        opening is optional and a later cycle may try again. Close failures,
        unknown states, exceptions, over-fills, and orphaned rows are not.
        """
        return bool(self.orphans or any(
            reason != "open did not fill" for _what, reason in self.failures))


class _LegsOnly:
    """`open_spread` takes a Spread but only reads `.legs`."""

    def __init__(self, legs):
        self.legs = legs


def execute_plan(mcp, quote_fn, book: Book, plan: SessionPlan, *,
                 log=lambda m: None, now: dt.datetime | None = None
                 ) -> ExecutionReport:
    rep = ExecutionReport()
    # ET, always. A registry stamped in machine-local time is how a
    # post-mortem of a US-market timing failure gets misdiagnosed.
    stamp = (now or now_et()).isoformat(timespec="seconds")

    # --- close first, so the risk is freed before anything is added
    for h, why in plan.close:
        entry = next((e for e in book.open_entries if e.id == h.entry_id), None)
        if entry is None:
            # The plan referenced a structure the registry no longer has. Do
            # not trade blind on it — say so and move on.
            rep.orphans.append(h.entry_id)
            log(f"  no registry row {h.entry_id}; skipping close")
            continue
        try:
            f = close_spread(mcp, h.legs, h.qty, quote_fn)
        except Exception as exc:  # noqa: BLE001
            rep.failures.append((h.entry_id, f"close raised: {exc}"))
            log(f"  {h.entry_id}: close raised {type(exc).__name__}: {exc}")
            continue
        if f.how == "stuck":
            rep.failures.append((h.entry_id, "close order state is unknown"))
            log(f"  {h.entry_id}: ORDER STATE UNKNOWN — stopping all execution")
            return rep
        if not f:
            rep.failures.append((h.entry_id, "close did not fill"))
            log(f"  {h.entry_id}: DID NOT FILL — still open")
            continue
        price = f.avg_price if f.avg_price is not None else h.mark
        if f.qty and f.qty < entry.qty:
            # Partial exit. Book the piece that traded and leave the remainder
            # on the registry, because the account still holds it. Marking the
            # whole row closed would hide a live position from every subsequent
            # cycle.
            book.close_partial(entry.id, qty=f.qty, exit_price=price,
                               reason=why, when=stamp)
            log(f"  {h.entry_id}: PARTIAL close {f.qty}/{entry.qty} at {price}")
        else:
            book.close(entry.id, exit_price=price, reason=why, when=stamp)
            log(f"  {h.entry_id}: closed at {price} ({f.how}, {f.attempts} tries)")
        rep.closed.append((entry.id, why, f))

    # --- then open, but only if the closes actually happened
    #
    # Ordering alone is not enough. `plan_session` sized these positions
    # assuming the closing rows' risk was freed, so opening on top of closes
    # that FAILED puts the book at survivors + failed-closes + new-opens, which
    # is the doubled book the ordering exists to prevent.
    if rep.failures and plan.open:
        log(f"  {len(rep.failures)} close(s) failed — not opening; the new "
            f"positions were sized against risk that was not freed")
        return rep

    for c, qty in plan.open:
        legs = c.legs
        try:
            quotes = quote_fn([l.symbol for l in legs])
            f = open_spread(mcp, _LegsOnly(legs), qty, quotes)
        except Exception as exc:  # noqa: BLE001
            rep.failures.append((c.underlying, f"open raised: {exc}"))
            log(f"  {c.underlying}: raised {type(exc).__name__}: {exc}")
            continue
        if f.how == "stuck":
            rep.failures.append((c.underlying, "open order state is unknown"))
            log(f"  {c.underlying}: ORDER STATE UNKNOWN — stopping all execution")
            return rep
        if not f:
            rep.failures.append((c.underlying, "open did not fill"))
            log(f"  {c.underlying}: did not fill")
            continue
        filled = f.qty or qty
        if filled > qty:
            # A broker reporting more filled than requested is a data error or
            # a units mismatch, not a windfall. Recording it would size every
            # later decision against exposure we did not ask for.
            rep.failures.append(
                (c.underlying, f"broker reported {filled} filled of {qty} requested"))
            log(f"  {c.underlying}: OVER-FILL {filled}/{qty} — recording the "
                f"request and flagging; reconcile will catch the truth")
            filled = qty
        elif filled != qty:
            log(f"  {c.underlying}: PARTIAL {filled}/{qty}")
        entry = BookEntry(
            id=f"{c.underlying}-{uuid.uuid4().hex[:8]}",
            underlying=c.underlying, structure="iron_condor",
            legs=[{"symbol": l.symbol, "side": l.side, "ratio_qty": l.ratio_qty}
                  for l in legs],
            # The FILLED quantity, never the requested one.
            qty=filled,
            entry=f.avg_price if f.avg_price is not None else c.entry,
            max_profit=c.max_profit, max_loss=c.max_loss, opened_at=stamp)
        book.add(entry)
        rep.opened.append((entry, f))
        log(f"  {c.underlying} x{filled} filled at {f.avg_price}")

    return rep
