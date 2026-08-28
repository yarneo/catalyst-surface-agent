"""Frozen AVGO scheduled-event policy and deterministic surface gates.

The model does not predict the earnings sign and cannot waive any rule here.
This module turns untrusted Alpaca MCP snapshot fields into one typed straddle,
or a list of explicit no-trade reasons, and owns the predeclared lifecycle.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

from trading_bot.options.clock import ET
from trading_bot.options.iv import Quote
from trading_bot.options.occ import BadOCC, parse
from trading_bot.options.spreads import Spread, build_long_straddle


@dataclass(frozen=True)
class ScheduledEventPolicy:
    event_id: str = "avgo-q3-fy2026"
    underlying: str = "AVGO"
    expiry: str = "2026-09-04"
    entry_start: dt.datetime = dt.datetime(2026, 9, 2, 15, 20, tzinfo=ET)
    entry_end: dt.datetime = dt.datetime(2026, 9, 2, 15, 40, tzinfo=ET)
    event_at: dt.datetime = dt.datetime(2026, 9, 2, 16, 0, tzinfo=ET)
    exit_at: dt.datetime = dt.datetime(2026, 9, 3, 9, 45, tzinfo=ET)
    emergency_flat_by: dt.datetime = dt.datetime(2026, 9, 3, 15, 30, tzinfo=ET)
    max_premium_to_spot: float = 0.085
    max_total_spread_pct: float = 0.05
    max_leg_spread_pct: float = 0.15
    max_quote_age_s: float = 90.0
    max_quote_skew_s: float = 5.0
    max_strike_distance_pct: float = 0.0075
    min_quote_size: int = 1
    order_buffer: float = 0.02

    def __post_init__(self) -> None:
        stamps = (self.entry_start, self.entry_end, self.event_at,
                  self.exit_at, self.emergency_flat_by)
        if any(value.tzinfo is None or value.utcoffset() is None for value in stamps):
            raise ValueError("scheduled policy datetimes must be timezone-aware")
        if not self.entry_start < self.entry_end < self.event_at < self.exit_at \
                < self.emergency_flat_by:
            raise ValueError("scheduled policy timestamps are not ordered")
        for name in ("max_premium_to_spot", "max_total_spread_pct",
                     "max_leg_spread_pct", "max_strike_distance_pct"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value < 1:
                raise ValueError(f"{name} must be in (0, 1)")
        if self.max_quote_age_s <= 0 or self.max_quote_skew_s < 0:
            raise ValueError("quote timing limits must be non-negative")
        if self.min_quote_size < 1 or self.order_buffer < 0:
            raise ValueError("invalid size or order buffer")


@dataclass(frozen=True)
class SurfaceLeg:
    symbol: str
    right: str
    strike: float
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    quoted_at: dt.datetime
    implied_volatility: float | None

    @property
    def quote(self) -> Quote:
        return Quote(self.bid, self.ask)


@dataclass(frozen=True)
class StraddleSurface:
    underlying: str
    spot: float
    call: SurfaceLeg
    put: SurfaceLeg
    observed_at: dt.datetime

    @property
    def strike(self) -> float:
        return self.call.strike

    @property
    def total_bid(self) -> float:
        return self.call.bid + self.put.bid

    @property
    def total_ask(self) -> float:
        return self.call.ask + self.put.ask

    @property
    def total_spread_pct(self) -> float:
        return ((self.total_ask - self.total_bid) / self.total_ask
                if self.total_ask > 0 else float("inf"))

    @property
    def quote_age_s(self) -> float:
        oldest = min(self.call.quoted_at, self.put.quoted_at)
        return (self.observed_at - oldest).total_seconds()

    @property
    def quote_skew_s(self) -> float:
        return abs((self.call.quoted_at - self.put.quoted_at).total_seconds())

    def executable_debit(self, buffer: float) -> float:
        return self.total_ask + buffer

    def spread(self, policy: ScheduledEventPolicy) -> Spread:
        dte = (dt.date.fromisoformat(policy.expiry) - self.observed_at.date()).days
        return build_long_straddle(
            underlying=self.underlying, call_symbol=self.call.symbol,
            put_symbol=self.put.symbol, strike=self.strike,
            quotes={self.call.symbol: self.call.quote,
                    self.put.symbol: self.put.quote},
            expiry=policy.expiry, dte=dte, buffer=policy.order_buffer)


@dataclass(frozen=True)
class ScheduledEntryDecision:
    eligible: bool
    reasons: tuple[str, ...]
    surface: StraddleSurface | None
    spread: Spread | None


def _timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError("quote timestamp is missing")
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("quote timestamp is not timezone-aware")
    return stamp.astimezone(ET)


def _finite_float(name: str, value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is missing or invalid") from exc
    if not math.isfinite(out):
        raise ValueError(f"{name} is not finite")
    return out


def surface_from_mcp(*, payload: Any, spot: float, observed_at: dt.datetime,
                     policy: ScheduledEventPolicy = ScheduledEventPolicy()
                     ) -> StraddleSurface:
    """Select the closest common strike from a narrow MCP option-chain payload."""
    if not math.isfinite(spot) or spot <= 0:
        raise ValueError("spot must be finite and positive")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshots"), dict):
        raise ValueError("option-chain payload has no snapshots object")

    by_strike: dict[float, dict[str, SurfaceLeg]] = {}
    for symbol, row in payload["snapshots"].items():
        if not isinstance(row, dict):
            continue
        try:
            contract = parse(symbol)
        except BadOCC:
            continue
        if contract.root != policy.underlying or \
                contract.expiry.isoformat() != policy.expiry:
            continue
        quote = row.get("latestQuote")
        if not isinstance(quote, dict):
            continue
        right = "call" if contract.right == "C" else "put"
        try:
            leg = SurfaceLeg(
                symbol=symbol, right=right, strike=contract.strike,
                bid=_finite_float("bid", quote.get("bp")),
                ask=_finite_float("ask", quote.get("ap")),
                bid_size=int(quote.get("bs") or 0),
                ask_size=int(quote.get("as") or 0),
                quoted_at=_timestamp(quote.get("t")),
                implied_volatility=(
                    _finite_float("implied volatility", row["impliedVolatility"])
                    if row.get("impliedVolatility") is not None else None),
            )
        except (TypeError, ValueError):
            continue
        by_strike.setdefault(contract.strike, {})[right] = leg

    common = [strike for strike, sides in by_strike.items()
              if {"call", "put"} <= sides.keys()]
    if not common:
        raise ValueError("no common call/put strike in option-chain payload")
    strike = min(common, key=lambda value: (abs(value - spot), value))
    sides = by_strike[strike]
    return StraddleSurface(policy.underlying, spot, sides["call"], sides["put"],
                           observed_at.astimezone(ET))


def evaluate_entry(*, now: dt.datetime, surface: StraddleSurface,
                   policy: ScheduledEventPolicy = ScheduledEventPolicy()
                   ) -> ScheduledEntryDecision:
    reasons: list[str] = []
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(ET)
    if not policy.entry_start <= now <= policy.entry_end:
        reasons.append("outside frozen entry window")
    if surface.underlying != policy.underlying:
        reasons.append("wrong underlying")
    if abs(surface.strike - surface.spot) / surface.spot \
            > policy.max_strike_distance_pct:
        reasons.append("nearest common strike is too far from spot")
    for name, leg in (("call", surface.call), ("put", surface.put)):
        if not leg.quote.usable(policy.max_leg_spread_pct):
            reasons.append(f"{name} quote is unusable or too wide")
        if min(leg.bid_size, leg.ask_size) < policy.min_quote_size:
            reasons.append(f"{name} displayed size is too small")
    if surface.total_spread_pct > policy.max_total_spread_pct:
        reasons.append("combined bid/ask width exceeds frozen limit")
    if surface.quote_age_s < -5:
        reasons.append("option quote timestamp is in the future")
    elif surface.quote_age_s > policy.max_quote_age_s:
        reasons.append("option quote is stale")
    if surface.quote_skew_s > policy.max_quote_skew_s:
        reasons.append("call/put quote timestamps are not synchronized")

    spread = None
    try:
        spread = surface.spread(policy)
    except (ValueError, RuntimeError) as exc:
        reasons.append(f"straddle construction failed: {exc}")
    if spread is not None and spread.max_loss / surface.spot \
            > policy.max_premium_to_spot:
        reasons.append("executable premium exceeds frozen spot ratio")
    return ScheduledEntryDecision(not reasons, tuple(reasons), surface,
                                  spread if not reasons else None)


def lifecycle_action(*, now: dt.datetime, has_position: bool,
                     entry_was_attempted: bool,
                     policy: ScheduledEventPolicy = ScheduledEventPolicy()) -> str:
    """Return WAIT, ENTER, HOLD, EXIT, or DONE under the frozen event clock."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(ET)
    if has_position:
        return "EXIT" if now >= policy.exit_at else "HOLD"
    if entry_was_attempted or now > policy.entry_end:
        return "DONE"
    if policy.entry_start <= now <= policy.entry_end:
        return "ENTER"
    return "WAIT"
