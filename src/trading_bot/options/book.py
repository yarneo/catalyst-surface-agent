"""A registry of the structures we opened.

The broker reports *legs*. It has no idea that four of them are one iron
condor, and it cannot: an account holding a short 765 call and a long 775 call
looks identical whether those were opened together as a spread or separately by
accident. Every management decision this agent makes — profit target, loss
limit, deadline exit — is a decision about a structure, so the grouping has to
be ours and it has to be durable across restarts.

This is the same lesson the equity side of this repo learned the hard way. When
nothing recorded which sleeve owned which position, 63% of the book ended up
orphaned: held by the account, managed by nobody. A registry is what prevents
the same failure here, where the units are spreads rather than shares.

Reconciliation is deliberately loud. If the registry and the account disagree,
that is not a rounding difference to paper over — it means an order filled that
we do not know about, or a leg was assigned, and either way the agent's picture
of its own risk is wrong.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .clock import now_et
from .occ import parse
from .spreads import Leg


class BookCorrupt(RuntimeError):
    """The registry cannot be read. Never trade past this."""


@dataclass
class BookEntry:
    id: str
    underlying: str
    structure: str
    legs: list[dict]              # {"symbol", "side", "ratio_qty"}
    qty: int
    entry: float                  # net price per share, mleg convention
    max_profit: float
    max_loss: float
    opened_at: str
    closed_at: str | None = None
    exit: float | None = None
    close_reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def as_legs(self) -> tuple[Leg, ...]:
        return tuple(Leg(l["symbol"], l["side"], int(l.get("ratio_qty", 1)))
                     for l in self.legs)

    def leg_exposure(self) -> dict[str, int]:
        """Signed contract count per option symbol, as the broker would see it."""
        out: dict[str, int] = {}
        for l in self.legs:
            n = int(l.get("ratio_qty", 1)) * self.qty
            out[l["symbol"]] = out.get(l["symbol"], 0) + (n if l["side"] == "buy" else -n)
        return out


class Book:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: list[BookEntry] = []
        self.load()

    def load(self) -> "Book":
        """Read the registry, or refuse to start.

        A truncated file raises JSONDecodeError and an unknown field raises
        TypeError, both from the constructor at import-time in the runner and
        both uncaught. Failing with a clear message beats a stack trace, because
        the operator's next move — restore or repair — depends on knowing which
        happened.
        """
        if not self.path.exists():
            return self
        text = self.path.read_text().strip() or "[]"
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BookCorrupt(
                f"{self.path} is not valid JSON ({exc}). It was likely truncated "
                f"by a crash mid-write. Restore it or move it aside; do not "
                f"trade against a registry that cannot be read.") from exc
        try:
            self.entries = [BookEntry(**e) for e in raw]
        except TypeError as exc:
            raise BookCorrupt(
                f"{self.path} has rows this version cannot read ({exc}). The "
                f"schema changed under an existing book.") from exc
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Per-process temp name. A fixed ".tmp" is shared by every instance, so
        # a second process renaming it away between our write and our replace
        # raises FileNotFoundError — reproduced 8 times out of 8 in audit, from
        # inside `book.add`, which sits outside the executor's try. The cycle
        # then died by exception AFTER the order filled, with nothing recorded.
        tmp = self.path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps([asdict(e) for e in self.entries], indent=2))
        # Atomic replace: a half-written registry read back after a crash would
        # silently orphan whatever it truncated.
        tmp.replace(self.path)

    @property
    def open_entries(self) -> list[BookEntry]:
        return [e for e in self.entries if e.is_open]

    def add(self, entry: BookEntry) -> BookEntry:
        self.entries.append(entry)
        self.save()
        return entry

    def close(self, entry_id: str, *, exit_price: float, reason: str,
              when: str | None = None) -> None:
        for e in self.entries:
            if e.id == entry_id and e.is_open:
                e.closed_at = when or now_et().isoformat(timespec="seconds")
                e.exit = exit_price
                e.close_reason = reason
                self.save()
                return
        raise KeyError(f"no open entry {entry_id}")

    def close_partial(self, entry_id: str, *, qty: int, exit_price: float,
                      reason: str, when: str | None = None) -> BookEntry:
        """Book a partial exit: split the row, close the traded piece, keep the
        rest live.

        A partial fill on a multi-leg order is a different position, not a
        rounding difference. Marking the whole row closed would hide a live
        position from every later cycle — the account would hold risk that
        nothing was managing, which is the exact failure the registry exists to
        prevent.
        """
        for e in self.entries:
            if e.id == entry_id and e.is_open:
                if qty >= e.qty:
                    self.close(entry_id, exit_price=exit_price, reason=reason,
                               when=when)
                    return e
                if qty <= 0:
                    raise ValueError(f"partial close qty must be positive, got {qty}")
                remaining = e.qty - qty
                closed = BookEntry(
                    id=f"{e.id}-p{qty}", underlying=e.underlying,
                    structure=e.structure, legs=list(e.legs), qty=qty,
                    entry=e.entry, max_profit=e.max_profit, max_loss=e.max_loss,
                    opened_at=e.opened_at,
                    closed_at=when or now_et().isoformat(timespec="seconds"),
                    exit=exit_price, close_reason=reason)
                e.qty = remaining
                self.entries.append(closed)
                self.save()
                return closed
        raise KeyError(f"no open entry {entry_id}")

    def expire_passed(self, today: "dt.date") -> list[str]:
        """Close out rows whose contracts have already expired.

        Without this an expired-worthless condor — the most common outcome of
        the strategy — leaves four legs in the registry against an empty
        account, so `reconcile` reports a mismatch on every subsequent cycle and
        the agent can never open again. Settled at zero is the correct booking:
        the structure expired inside its wings and the credit was kept.
        """
        done = []
        for e in self.open_entries:
            exp = max(parse(l["symbol"]).expiry for l in e.legs)
            if exp < today:
                e.closed_at = now_et().isoformat(timespec="seconds")
                e.exit = 0.0
                e.close_reason = f"expired {exp}"
                done.append(e.id)
        if done:
            self.save()
        return done

    def expected_exposure(self) -> dict[str, int]:
        """Net signed contracts per option symbol across the whole open book.

        Netting is what the ACCOUNT does, so this is the right comparison for
        `reconcile`. It is not sufficient on its own — two rows that offset each
        other exactly are invisible here — which is why `phantom_rows` exists.
        """
        out: dict[str, int] = {}
        for e in self.open_entries:
            for sym, n in e.leg_exposure().items():
                out[sym] = out.get(sym, 0) + n
        return {k: v for k, v in out.items() if v != 0}

    def phantom_rows(self, actual: dict[str, int]) -> list[str]:
        """Open rows whose legs are entirely absent from the account.

        Net exposure can agree while the registry is wrong: two rows holding
        offsetting positions in the same contract sum to zero and vanish from
        `expected_exposure`, so `reconcile` reports nothing while the agent goes
        on computing profit targets and deadline exits for two structures that
        do not exist.
        """
        return [e.id for e in self.open_entries
                if not any(actual.get(sym, 0) for sym in e.leg_exposure())]

    def reconcile(self, actual: dict[str, int]) -> dict[str, tuple[int, int]]:
        """Compare the registry against the account.

        Returns {symbol: (expected, actual)} for every symbol that disagrees.
        An empty result is the only acceptable state; anything else means the
        agent is reasoning about risk it does not have, or holding risk it is
        not managing.
        """
        expected = self.expected_exposure()
        syms = set(expected) | set(k for k, v in actual.items() if v != 0)
        diff = {s: (expected.get(s, 0), actual.get(s, 0))
                for s in sorted(syms)
                if expected.get(s, 0) != actual.get(s, 0)}
        for row in self.phantom_rows(actual):
            diff.setdefault(f"<row {row}>", (1, 0))
        return diff

    def realised_pnl(self) -> float:
        """Dollars booked on closed structures."""
        total = 0.0
        for e in self.entries:
            if not e.is_open and e.exit is not None:
                total += -(e.exit + e.entry) * 100.0 * e.qty
        return total
