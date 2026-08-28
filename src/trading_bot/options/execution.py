"""Getting spreads filled, and — more importantly — getting out of them.

Entry and exit are not symmetric. An entry that does not fill costs nothing: the
signal is there tomorrow and skipping a trade is free. An exit that does not
fill costs everything the position can still lose. So the entry tries once and
the exit escalates.

Rewritten after an audit found the escalation itself was the danger. The old
ladder cancelled fire-and-forget — `mcp.cancel(...)`, sleep one second, swallow
every exception — and then submitted the next rung regardless. Three routine
broker behaviours turned that into a doubled position:

  * Alpaca rejects a cancel with 422 for an order already `pending_new` or
    partially filled. The exception was swallowed and the ladder escalated on
    top of a live order.
  * If every status poll raised for the whole timeout, `_poll` returned None and
    `_cancel(None)` returned immediately — zero cancels issued, four rungs
    resting, four times the intended close.
  * `_is_filled` tested `status == "filled"` only, so an order cancelled after a
    partial fill read as unfilled and the next rung resubmitted the FULL size.
    Measured: a five-lot close that put through seven.

Two rules follow, and they are the reason this module exists:

**Never escalate over an order that has not been confirmed dead.** Cancellation
is polled to a terminal state. If it cannot be confirmed, the ladder stops.

**Track what actually filled, rung by rung.** Each rung submits only the
remainder, so a partial fill reduces the next order instead of duplicating it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .iv import Quote
from .spreads import Leg, Spread, marketable_limit

TERMINAL = ("filled", "canceled", "cancelled", "rejected", "expired", "done_for_day")


@dataclass(frozen=True)
class Fill:
    filled: bool
    qty: int
    avg_price: float | None
    attempts: int
    how: str          # "limit" | "escalated" | "market" | "unfilled" | "stuck"

    def __bool__(self) -> bool:
        return self.filled


# Concessions tried in order when exiting, in dollars per contract on top of the
# already-marketable price. The final None means "market order" — deliberate: a
# market order on a four-leg spread can fill badly, but "badly" is bounded by
# the spread while "still holding it" is bounded by the max loss.
EXIT_LADDER: tuple[float | None, ...] = (0.02, 0.10, 0.25, None)


def _status(o) -> str:
    return (o or {}).get("status", "") if isinstance(o, dict) else ""


def _filled_qty(o) -> int:
    if not isinstance(o, dict):
        return 0
    try:
        return int(float(o.get("filled_qty") or 0))
    except (TypeError, ValueError):
        return 0


def _price(o) -> float | None:
    if not isinstance(o, dict):
        return None
    p = o.get("filled_avg_price")
    try:
        return float(p) if p is not None else None
    except (TypeError, ValueError):
        return None


def _poll(mcp, coid: str, timeout_s: float, poll_s: float):
    """Poll until terminal or timeout. Returns the last payload seen, or None.

    A non-dict payload is treated as "no information", not as a status. The MCP
    layer returns a bare string when a text block is not JSON, and the old code
    called `.get` on it outside its try — the AttributeError escaped the whole
    close and left the order uncancelled.
    """
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        time.sleep(poll_s)
        try:
            o = mcp.order_by_client_id(coid)
        except Exception:  # noqa: BLE001 — a lookup blip is not a failed order
            continue
        if isinstance(o, dict):
            last = o
            if _status(o) in TERMINAL:
                return o
    return last


def cancel_by_id(mcp, coid: str, *, timeout_s: float = 20.0,
                 poll_s: float = 2.0) -> tuple[bool, dict | None]:
    """Cancel by client id, for when polling never produced an order payload.

    `_poll` returns None if every lookup raises or every payload is a non-dict
    text block, and `cancel_and_confirm(None)` then returns without issuing a
    single cancel — leaving the order live. That was the original defect, and
    returning early on a non-dict merely stopped it escalating on top. We always
    know the client id we submitted under, so a cancel is always possible.
    """
    try:
        mcp.cancel_by_client_id(coid)
    except Exception:  # noqa: BLE001
        try:
            o = mcp.order_by_client_id(coid)
            if isinstance(o, dict) and o.get("id"):
                mcp.cancel(o["id"])
        except Exception:  # noqa: BLE001
            return False, None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_s)
        try:
            o = mcp.order_by_client_id(coid)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(o, dict) and _status(o) in TERMINAL:
            return True, o
    return False, None


def cancel_and_confirm(mcp, order, *, timeout_s: float = 20.0,
                       poll_s: float = 2.0) -> tuple[bool, dict | None]:
    """Cancel, then poll until the order is provably terminal.

    Returns (confirmed, final_state). `confirmed` False means the caller must
    NOT submit another order for the same legs, because both could fill.

    The final state is returned rather than a bare boolean because an order can
    FILL during its own cancellation — a documented race, and one the caller has
    to account for or it will re-submit size that has already traded.
    """
    if not isinstance(order, dict):
        return False, None
    if _status(order) in TERMINAL:
        return True, order
    oid, coid = order.get("id"), order.get("client_order_id")
    try:
        mcp.cancel(oid)
    except Exception:  # noqa: BLE001 — a rejected cancel is normal; confirm below
        pass
    deadline = time.time() + timeout_s
    last = order
    while time.time() < deadline:
        time.sleep(poll_s)
        try:
            o = mcp.order_by_client_id(coid) if coid else None
        except Exception:  # noqa: BLE001
            continue
        if isinstance(o, dict):
            last = o
            if _status(o) in TERMINAL:
                return True, o
    return False, last


def _settle_submitted_order(mcp, coid: str, *, timeout_s: float,
                            poll_s: float,
                            cancel_timeout_s: float) -> tuple[bool, dict | None]:
    """Find and settle an order even when its submission call raised.

    A transport timeout does not mean Alpaca rejected the order; it may have
    accepted or even filled it before the reply was lost. The client order ID
    is the durable handle. Never report the attempt as failed until that handle
    is terminal, because continuing would create an unregistered position.
    """
    o = _poll(mcp, coid, timeout_s, poll_s)
    if o is None:
        return cancel_by_id(mcp, coid, timeout_s=cancel_timeout_s,
                            poll_s=poll_s)
    if _status(o) not in TERMINAL:
        return cancel_and_confirm(mcp, o, timeout_s=cancel_timeout_s,
                                  poll_s=poll_s)
    return True, o


def open_spread(mcp, spread: Spread, qty: int, quotes: dict[str, Quote], *,
                buffer: float = 0.02, timeout_s: float = 60.0,
                poll_s: float = 3.0, cancel_timeout_s: float = 20.0,
                client_order_id: str | None = None) -> Fill:
    """Try once, at a price that should fill. Give up quietly if it does not.

    No escalation: chasing an entry means overpaying for an edge measured at the
    mid, and an edge you have to overpay for is not an edge.
    """
    coid = client_order_id or f"vrp-{uuid.uuid4().hex[:12]}"
    limit = marketable_limit(spread.legs, quotes, buffer=buffer)
    try:
        mcp.place_spread([l.as_mcp() for l in spread.legs], qty,
                         limit_price=limit, client_order_id=coid)
    except Exception:  # noqa: BLE001 — state is recovered by client order ID
        pass
    confirmed, o = _settle_submitted_order(
        mcp, coid, timeout_s=timeout_s, poll_s=poll_s,
        cancel_timeout_s=cancel_timeout_s)
    got = _filled_qty(o)
    if not confirmed:
        return Fill(got > 0, got, _price(o), 1, "stuck")
    if got > 0:
        # A partial entry is still a position and must be reported as one; the
        # old code returned filled=False and wrote no registry row, leaving the
        # broker holding contracts nothing was managing.
        return Fill(True, got, _price(o), 1,
                    "limit" if got == qty else "partial")
    return Fill(False, 0, None, 1, "unfilled")


def close_spread(mcp, legs: tuple[Leg, ...], qty: int, quote_fn, *,
                 timeout_s: float = 45.0, poll_s: float = 3.0,
                 ladder: tuple[float | None, ...] = EXIT_LADDER,
                 cancel_timeout_s: float = 20.0,
                 client_order_id: str | None = None) -> Fill:
    """Exit, escalating until filled or the ladder is exhausted.

    `quote_fn(symbols) -> dict[str, Quote]` is re-called at every rung, because
    the usual reason a rung failed is that the market moved, and re-pricing off
    the stale quotes that already failed just fails again.
    """
    base = client_order_id or f"vrpx-{uuid.uuid4().hex[:12]}"
    syms = [l.symbol for l in legs]
    mcp_legs = [l.as_mcp("close") for l in legs]

    remaining = qty
    filled_total = 0
    cost = 0.0
    for i, concession in enumerate(ladder):
        if remaining <= 0:
            break
        coid = f"{base}-{i}"
        limit = None
        if concession is not None:
            try:
                limit = marketable_limit(legs, quote_fn(syms), intent="close",
                                         buffer=concession)
            except Exception:  # noqa: BLE001
                # No usable quote for some leg — routine near expiry, when the
                # broker stops publishing them. A position that cannot be PRICED
                # must still be EXITABLE: falling through to a market order is
                # the whole reason the ladder ends in one. Raising here instead
                # meant an unpriceable position could not be closed at all,
                # which is the failure mode this module exists to prevent.
                limit = None
        try:
            mcp.close_spread(mcp_legs, remaining, limit_price=limit,
                             client_order_id=coid)
        except Exception:  # noqa: BLE001 — state is recovered by client order ID
            pass
        confirmed, o = _settle_submitted_order(
            mcp, coid, timeout_s=timeout_s, poll_s=poll_s,
            cancel_timeout_s=cancel_timeout_s)

        got = min(_filled_qty(o), remaining)
        if got > 0:
            px = _price(o)
            if px is not None:
                cost += px * got
            filled_total += got
            remaining -= got
        if remaining <= 0:
            how = "market" if concession is None else ("limit" if i == 0 else "escalated")
            avg = cost / filled_total if filled_total else _price(o)
            return Fill(True, filled_total, avg, i + 1, how)

        if not confirmed:
            # Could not prove the order is dead. Submitting the next rung on top
            # of a live order is how a close becomes a reversal.
            avg = cost / filled_total if filled_total else None
            return Fill(filled_total > 0, filled_total, avg, i + 1, "stuck")

    avg = cost / filled_total if filled_total else None
    return Fill(filled_total > 0, filled_total, avg, len(ladder),
                "partial" if filled_total else "unfilled")
