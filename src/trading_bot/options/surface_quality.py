"""Executable no-arbitrage diagnostics for option quote surfaces.

The checks use bid/ask bounds rather than midpoints.  A warning therefore means
the inconsistency survives the spread and is crossable in principle; harmless
midpoint noise is not mislabeled as an opportunity.  This module is diagnostic
only and has no order path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .occ import BadOCC, parse


@dataclass(frozen=True)
class SurfaceQuote:
    expiry: str
    strike: float
    right: str
    bid: float
    ask: float


def _quotes(payload: Any, *, underlying: str, expiry: str | None = None) \
        -> list[SurfaceQuote]:
    snapshots = payload.get("snapshots", {}) if isinstance(payload, dict) else {}
    output = []
    for symbol, row in snapshots.items():
        try:
            contract = parse(str(symbol))
            quote = row["latestQuote"]
            bid, ask = float(quote["bp"]), float(quote["ap"])
        except (BadOCC, KeyError, TypeError, ValueError):
            continue
        if contract.root != underlying or (expiry and contract.expiry.isoformat() != expiry):
            continue
        if not all(map(math.isfinite, (bid, ask))) or bid < 0 or ask <= bid:
            continue
        output.append(SurfaceQuote(
            contract.expiry.isoformat(), contract.strike, contract.right,
            bid, ask))
    return output


def static_arbitrage_report(
    payload: Any, *, underlying: str, expiry: str | None = None,
    tolerance: float = 0.01,
) -> dict[str, Any]:
    """Check monotonicity, vertical bounds, and convexity at each expiry."""
    quotes = _quotes(payload, underlying=underlying, expiry=expiry)
    violations: list[dict[str, Any]] = []
    for right in ("C", "P"):
        by_expiry: dict[str, list[SurfaceQuote]] = {}
        for quote in quotes:
            if quote.right == right:
                by_expiry.setdefault(quote.expiry, []).append(quote)
        for exp, rows in by_expiry.items():
            rows.sort(key=lambda q: q.strike)
            for low, high in zip(rows, rows[1:]):
                width = high.strike - low.strike
                if right == "C":
                    if high.bid > low.ask + tolerance:
                        violations.append({"type": "monotonicity", "right": right,
                                           "expiry": exp, "strikes": [low.strike, high.strike]})
                    if low.bid - high.ask > width + tolerance:
                        violations.append({"type": "vertical_bound", "right": right,
                                           "expiry": exp, "strikes": [low.strike, high.strike]})
                else:
                    if low.bid > high.ask + tolerance:
                        violations.append({"type": "monotonicity", "right": right,
                                           "expiry": exp, "strikes": [low.strike, high.strike]})
                    if high.bid - low.ask > width + tolerance:
                        violations.append({"type": "vertical_bound", "right": right,
                                           "expiry": exp, "strikes": [low.strike, high.strike]})
            for low, middle, high in zip(rows, rows[1:], rows[2:]):
                span = high.strike - low.strike
                if span <= 0:
                    continue
                low_weight = (high.strike - middle.strike) / span
                endpoint_ask = low_weight * low.ask + (1 - low_weight) * high.ask
                if middle.bid > endpoint_ask + tolerance:
                    violations.append({"type": "convexity", "right": right,
                                       "expiry": exp,
                                       "strikes": [low.strike, middle.strike, high.strike]})
    expiries = sorted({q.expiry for q in quotes})
    return {
        "status": ("INSUFFICIENT" if len(quotes) < 3 else
                   "WARN" if violations else "CLEAN"),
        "underlying": underlying,
        "expiries": expiries,
        "usable_quotes": len(quotes),
        "crossable_violations": len(violations),
        "violations": violations[:25],
        "method": "bid/ask static-arbitrage bounds; diagnostic only",
    }


def tp2_rectangle_report(
    points: Iterable[dict[str, Any]], *, tolerance: float = 0.0,
) -> dict[str, Any]:
    """Check TP2 rectangles on an already forward-normalized call surface.

    Each point needs ``tenor``, ``moneyness``, ``bid`` and ``ask``.  For near
    tenor n, far tenor f, low moneyness l and high moneyness h, TP2 requires
    ``C(n,l) * C(f,h) >= C(n,h) * C(f,l)``.  A violation is reported only when
    the unfavorable ask product is still below the opposing bid product.
    """
    grid: dict[tuple[float, float], tuple[float, float]] = {}
    for row in points:
        try:
            tenor, m = float(row["tenor"]), float(row["moneyness"])
            bid, ask = float(row["bid"]), float(row["ask"])
        except (KeyError, TypeError, ValueError):
            continue
        if (all(map(math.isfinite, (tenor, m, bid, ask))) and tenor > 0
                and bid >= 0 and ask > bid):
            grid[(tenor, m)] = (bid, ask)
    tenors = sorted({key[0] for key in grid})
    money = sorted({key[1] for key in grid})
    violations = []
    tested = 0
    for i, near in enumerate(tenors):
        for far in tenors[i + 1:]:
            for j, low in enumerate(money):
                for high in money[j + 1:]:
                    keys = ((near, low), (near, high), (far, low), (far, high))
                    if not all(key in grid for key in keys):
                        continue
                    tested += 1
                    a, b, c, d = (grid[key] for key in keys)
                    if a[1] * d[1] + tolerance < b[0] * c[0]:
                        violations.append({"near_tenor": near, "far_tenor": far,
                                           "low_moneyness": low, "high_moneyness": high})
    return {
        "status": "INSUFFICIENT" if not tested else "WARN" if violations else "CLEAN",
        "rectangles_tested": tested,
        "crossable_violations": len(violations),
        "violations": violations[:25],
        "method": "TP2 bid/ask rectangles on caller-supplied forward-normalized calls",
    }
