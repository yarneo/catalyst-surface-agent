"""Parse OCC option symbols.

    QQQ260925C00765000
    |__|_____|_|_______|
    root  exp  R  strike x 1000

Needed because a Leg carries only its symbol, and every payoff calculation
needs the strike and the right. Parsing is safer than threading them alongside:
one source of truth, and the symbol is the thing the broker actually fills.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

# Roots may contain digits. A corporate action — split, special
# dividend, merger — creates an adjusted series like RUT1 or SPY2, and
# `[A-Z]{1,6}` rejects them. `parse` raising inside `payoff_at` takes
# down the whole payoff calculation for a position we hold.
_PAT = re.compile(r"^(?P<root>[A-Z][A-Z0-9]{0,5})(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})"
                  r"(?P<right>[CP])(?P<strike>\d{8})$")


class BadOCC(ValueError):
    pass


@dataclass(frozen=True)
class Contract:
    root: str
    expiry: dt.date
    right: str          # "C" | "P"
    strike: float

    @property
    def is_call(self) -> bool:
        return self.right == "C"


def parse(symbol: str) -> Contract:
    m = _PAT.match(symbol.strip().upper())
    if not m:
        raise BadOCC(f"not an OCC option symbol: {symbol!r}")
    g = m.groupdict()
    return Contract(g["root"], dt.date(2000 + int(g["y"]), int(g["m"]), int(g["d"])),
                    g["right"], int(g["strike"]) / 1000.0)
