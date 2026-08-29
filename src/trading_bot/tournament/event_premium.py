"""Shadow-only cross-sectional event-premium measurements.

This module turns two option-expiry surfaces into an implied jump estimate.  It
has deliberately no broker client, order type, quantity, or execution hook: a
cross-sectional residual is a research hypothesis until historical outcomes
show that it predicts executable P&L.

For two expiries after the same event::

    iv_front**2 * T_front = base_var * T_front + jump_var
    iv_back**2  * T_back  = base_var * T_back  + jump_var

Subtracting the equations identifies the market-implied diffusion variance and
then the one-off jump variance.  The calculation assumes one common event and
roughly constant base variance across the two expiries.  Callers must verify
that assumption semantically before treating the result as an event.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, replace
from typing import Any, Iterable

import numpy as np

from trading_bot.options.clock import ET
from trading_bot.options.iv import (ForwardUnavailable, IVUnavailable, Quote,
                                    implied_forward, implied_vol_forward)
from trading_bot.options.occ import BadOCC, parse


class EventPremiumUnavailable(ValueError):
    """The supplied surface cannot support an auditable event measurement."""


@dataclass(frozen=True)
class EventSurfacePoint:
    strike: float
    log_moneyness_pct: float
    call_iv: float
    put_iv: float
    mean_iv: float
    call_bid: float
    call_ask: float
    put_bid: float
    put_ask: float
    quote_start: dt.datetime
    quote_end: dt.datetime


@dataclass(frozen=True)
class ExpirySurface:
    symbol: str
    expiry: str
    spot: float
    observed_at: dt.datetime
    atm_iv: float
    pair_count: int
    nearest_strike: float
    executable_straddle_ask: float
    total_spread_pct: float
    max_quote_age_s: float
    points: tuple[EventSurfacePoint, ...]


@dataclass(frozen=True)
class EventPremiumObservation:
    symbol: str
    sector: str
    event_type: str
    spot: float
    front_expiry: str
    back_expiry: str
    front_iv: float
    back_iv: float
    base_iv: float
    implied_event_move: float
    standardized_jump: float
    variance_days: float
    term_ratio: float
    executable_front_premium_to_spot: float
    front_total_spread_pct: float
    front_pair_count: int
    back_pair_count: int
    max_quote_age_s: float
    shadow_only: bool = True
    order_enabled: bool = False


@dataclass(frozen=True)
class RankedEventPremium:
    observation: EventPremiumObservation
    expected_log_variance_days: float
    residual: float
    percentile: float
    robust_z: float
    method: str
    hypothesis: str
    validated_edge: bool = False


@dataclass(frozen=True)
class HistoricalEventMoveComparison:
    symbol: str
    event_dates_requested: int
    sample_size: int
    absolute_gaps: tuple[float, ...]
    implied_event_move: float
    executable_premium_to_spot: float
    mean_absolute_gap: float
    median_absolute_gap: float
    p75_absolute_gap: float
    implied_move_to_median_gap: float
    premium_to_median_gap: float
    gap_exceeds_premium_rate: float
    intrinsic_floor_mean_return: float
    intrinsic_floor_median_return: float
    validated_edge: bool = False


def _timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise EventPremiumUnavailable("option quote timestamp is missing")
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventPremiumUnavailable("option quote timestamp is invalid") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise EventPremiumUnavailable("option quote timestamp is not timezone-aware")
    return stamp.astimezone(ET)


def _positive(name: str, value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EventPremiumUnavailable(f"{name} is invalid") from exc
    if not math.isfinite(result) or result <= 0:
        raise EventPremiumUnavailable(f"{name} must be finite and positive")
    return result


def _quote(row: dict[str, Any], *, max_spread_pct: float) -> tuple[float, float, dt.datetime]:
    quote = row.get("latestQuote")
    if not isinstance(quote, dict):
        raise EventPremiumUnavailable("latest option quote is missing")
    bid = _positive("bid", quote.get("bp"))
    ask = _positive("ask", quote.get("ap"))
    if ask <= bid:
        raise EventPremiumUnavailable("option quote is locked or crossed")
    mid = (bid + ask) / 2.0
    if (ask - bid) / mid > max_spread_pct:
        raise EventPremiumUnavailable("option quote is too wide")
    if int(quote.get("bs") or 0) < 1 or int(quote.get("as") or 0) < 1:
        raise EventPremiumUnavailable("option quote has no displayed size")
    return bid, ask, _timestamp(quote.get("t"))


def surface_from_mcp(
    *,
    payload: Any,
    symbol: str,
    expiry: str,
    spot: float,
    observed_at: dt.datetime,
    max_abs_log_moneyness_pct: float = 4.0,
    max_leg_spread_pct: float = 0.30,
    min_pairs: int = 3,
    atm_pairs: int = 5,
) -> ExpirySurface:
    """Extract a robust paired-call/put ATM IV from an Alpaca MCP chain."""
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshots"), dict):
        raise EventPremiumUnavailable("option-chain payload has no snapshots object")
    if not symbol or not expiry:
        raise EventPremiumUnavailable("symbol and expiry are required")
    spot = _positive("spot", spot)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise EventPremiumUnavailable("observed_at must be timezone-aware")
    if min_pairs < 1 or atm_pairs < 1:
        raise EventPremiumUnavailable("pair counts must be positive")

    expiry_date = dt.date.fromisoformat(expiry)
    days = (expiry_date - observed_at.astimezone(ET).date()).days
    if days <= 0:
        raise EventPremiumUnavailable("surface expiry must be in the future")
    time_to_expiry = days / 365.0

    # Build the market forward from paired call/put quotes before solving IV.
    # Alpaca's displayed IV can embody a spot/no-dividend assumption; around an
    # ex-dividend date that manufactures a term bump.  Parity absorbs carry,
    # dividends and borrow into the observed forward instead.
    raw: dict[float, dict[str, tuple[float, float, dt.datetime]]] = {}
    for occ_symbol, row in payload["snapshots"].items():
        if not isinstance(row, dict):
            continue
        try:
            contract = parse(occ_symbol)
        except BadOCC:
            continue
        if contract.root != symbol or contract.expiry.isoformat() != expiry:
            continue
        log_moneyness = 100.0 * math.log(contract.strike / spot)
        if abs(log_moneyness) > max_abs_log_moneyness_pct:
            continue
        try:
            bid, ask, stamp = _quote(row, max_spread_pct=max_leg_spread_pct)
        except EventPremiumUnavailable:
            continue
        side = "call" if contract.right == "C" else "put"
        raw.setdefault(contract.strike, {})[side] = (bid, ask, stamp)

    calls = {strike: Quote(*sides["call"][:2]) for strike, sides in raw.items()
             if "call" in sides}
    puts = {strike: Quote(*sides["put"][:2]) for strike, sides in raw.items()
            if "put" in sides}
    try:
        forward = implied_forward(
            time_to_expiry, calls, puts, spot=spot, band=0.035, min_pairs=min_pairs)
    except (ForwardUnavailable, IVUnavailable) as exc:
        raise EventPremiumUnavailable(f"{symbol} {expiry}: {exc}") from exc

    points: list[EventSurfacePoint] = []
    for strike, sides in sorted(raw.items()):
        if not {"call", "put"} <= sides.keys():
            continue
        call_bid, call_ask, call_stamp = sides["call"]
        put_bid, put_ask, put_stamp = sides["put"]
        try:
            call_iv = implied_vol_forward(
                (call_bid + call_ask) / 2.0, forward, strike,
                time_to_expiry, call=True)
            put_iv = implied_vol_forward(
                (put_bid + put_ask) / 2.0, forward, strike,
                time_to_expiry, call=False)
        except IVUnavailable:
            continue
        points.append(EventSurfacePoint(
            strike=strike,
            log_moneyness_pct=100.0 * math.log(strike / spot),
            call_iv=call_iv,
            put_iv=put_iv,
            mean_iv=(call_iv + put_iv) / 2.0,
            call_bid=call_bid,
            call_ask=call_ask,
            put_bid=put_bid,
            put_ask=put_ask,
            quote_start=min(call_stamp, put_stamp),
            quote_end=max(call_stamp, put_stamp),
        ))
    if len(points) < min_pairs:
        raise EventPremiumUnavailable(
            f"{symbol} {expiry}: only {len(points)} usable paired strikes; need {min_pairs}")

    nearest = sorted(points, key=lambda p: (abs(p.log_moneyness_pct), p.strike))
    selected = nearest[:min(atm_pairs, len(nearest))]
    atm_iv = float(statistics.median(point.mean_iv for point in selected))
    at_money = nearest[0]
    total_bid = at_money.call_bid + at_money.put_bid
    total_ask = at_money.call_ask + at_money.put_ask
    total_mid = (total_bid + total_ask) / 2.0
    spread = (total_ask - total_bid) / total_mid
    quote_start = min(point.quote_start for point in selected)
    return ExpirySurface(
        symbol=symbol,
        expiry=expiry,
        spot=spot,
        observed_at=observed_at.astimezone(ET),
        atm_iv=atm_iv,
        pair_count=len(points),
        nearest_strike=at_money.strike,
        executable_straddle_ask=total_ask,
        total_spread_pct=spread,
        max_quote_age_s=(observed_at.astimezone(ET) - quote_start).total_seconds(),
        points=tuple(points),
    )


def decompose_event_premium(
    front: ExpirySurface,
    back: ExpirySurface,
    *,
    sector: str = "unknown",
    event_type: str = "unverified",
) -> EventPremiumObservation:
    """Solve for base variance and a common jump carried by both expiries."""
    if front.symbol != back.symbol:
        raise EventPremiumUnavailable("front and back surfaces have different symbols")
    if not math.isclose(front.spot, back.spot, rel_tol=1e-9):
        raise EventPremiumUnavailable("front and back surfaces use different spot prices")
    front_date = dt.date.fromisoformat(front.expiry)
    back_date = dt.date.fromisoformat(back.expiry)
    asof = front.observed_at.astimezone(ET).date()
    front_days = (front_date - asof).days
    back_days = (back_date - asof).days
    if front_days <= 0 or back_days <= front_days:
        raise EventPremiumUnavailable("expiries must be ordered future dates")

    front_t, back_t = front_days / 365.0, back_days / 365.0
    front_total = front.atm_iv ** 2 * front_t
    back_total = back.atm_iv ** 2 * back_t
    base_variance = (back_total - front_total) / (back_t - front_t)
    if not math.isfinite(base_variance) or base_variance <= 0:
        raise EventPremiumUnavailable(
            "term structure implies non-positive forward base variance")
    jump_variance = front_total - base_variance * front_t
    # Tiny negative values can arise from rounding an almost-flat surface.  A
    # materially negative result means the shared-jump model is the wrong model.
    tolerance = max(front_total, back_total) * 1e-8
    if jump_variance < -tolerance:
        raise EventPremiumUnavailable("term structure does not imply a shared jump")
    jump_variance = max(0.0, jump_variance)
    base_iv = math.sqrt(base_variance)
    implied_move = math.sqrt(jump_variance)
    variance_days = jump_variance / (base_variance / 252.0)
    standardized_jump = math.sqrt(variance_days)
    return EventPremiumObservation(
        symbol=front.symbol,
        sector=sector or "unknown",
        event_type=event_type or "unverified",
        spot=front.spot,
        front_expiry=front.expiry,
        back_expiry=back.expiry,
        front_iv=front.atm_iv,
        back_iv=back.atm_iv,
        base_iv=base_iv,
        implied_event_move=implied_move,
        standardized_jump=standardized_jump,
        variance_days=variance_days,
        term_ratio=front.atm_iv / back.atm_iv,
        executable_front_premium_to_spot=(
            front.executable_straddle_ask / front.spot),
        front_total_spread_pct=front.total_spread_pct,
        front_pair_count=front.pair_count,
        back_pair_count=back.pair_count,
        max_quote_age_s=max(front.max_quote_age_s, back.max_quote_age_s),
    )


def with_event_type(observation: EventPremiumObservation,
                    event_type: str) -> EventPremiumObservation:
    """Attach a separately verified semantic classification immutably."""
    return replace(observation, event_type=event_type or "unverified")


def _fallback_predictions(values: np.ndarray, sectors: list[str]) -> np.ndarray:
    predictions = np.empty(len(values), dtype=float)
    for i, sector in enumerate(sectors):
        peers = [values[j] for j, peer in enumerate(sectors)
                 if j != i and peer == sector]
        if len(peers) < 3:
            peers = [values[j] for j in range(len(values)) if j != i]
        predictions[i] = float(np.median(peers)) if peers else float(values[i])
    return predictions


def _ridge_predictions(observations: list[EventPremiumObservation],
                       values: np.ndarray, *, alpha: float) -> tuple[np.ndarray, str]:
    """Leave-one-out ridge predictions with conservative categorical pooling."""
    sectors = [observation.sector or "unknown" for observation in observations]
    events = [observation.event_type or "unverified" for observation in observations]
    sector_counts = {name: sectors.count(name) for name in set(sectors)}
    event_counts = {name: events.count(name) for name in set(events)}
    pooled_sectors = [name if sector_counts[name] >= 3 else "other" for name in sectors]
    pooled_events = [name if event_counts[name] >= 3 else "other" for name in events]
    sector_levels = sorted(set(pooled_sectors) - {"other", "unknown"})
    event_levels = sorted(set(pooled_events) - {"other", "unverified"})

    base = np.asarray([math.log(observation.base_iv) for observation in observations])
    base = (base - np.mean(base)) / (np.std(base) or 1.0)
    columns = [np.ones(len(observations)), base]
    columns.extend(np.asarray([float(value == level) for value in pooled_sectors])
                   for level in sector_levels)
    columns.extend(np.asarray([float(value == level) for value in pooled_events])
                   for level in event_levels)
    matrix = np.column_stack(columns)
    feature_count = matrix.shape[1]
    if len(observations) < max(12, 3 * feature_count):
        return _fallback_predictions(values, sectors), "leave-one-out sector median"

    predictions = np.empty(len(observations), dtype=float)
    for held_out in range(len(observations)):
        keep = np.arange(len(observations)) != held_out
        x_train, y_train = matrix[keep], values[keep]
        penalty = np.eye(feature_count) * alpha
        penalty[0, 0] = 0.0
        beta = np.linalg.solve(x_train.T @ x_train + penalty, x_train.T @ y_train)
        predictions[held_out] = float(matrix[held_out] @ beta)
    return predictions, "leave-one-out ridge ranking"


def rank_cross_section(
    observations: Iterable[EventPremiumObservation], *, alpha: float = 2.0,
) -> tuple[RankedEventPremium, ...]:
    """Rank implied event variance residuals without claiming they are alpha."""
    rows = list(observations)
    if len(rows) < 3:
        raise EventPremiumUnavailable("cross-sectional ranking needs at least three names")
    if alpha <= 0:
        raise ValueError("alpha must be positive")
    values = np.asarray([math.log1p(row.variance_days) for row in rows], dtype=float)
    predictions, method = _ridge_predictions(rows, values, alpha=alpha)
    residuals = values - predictions
    center = float(np.median(residuals))
    mad = float(np.median(np.abs(residuals - center)))
    scale = 1.4826 * mad if mad > 1e-12 else 1.0

    ranked: list[RankedEventPremium] = []
    for index, observation in enumerate(rows):
        residual = float(residuals[index])
        less = int(np.sum(residuals < residual))
        equal = int(np.sum(np.isclose(residuals, residual, rtol=0, atol=1e-12)))
        percentile = (less + 0.5 * equal) / len(rows)
        if percentile >= 0.80:
            hypothesis = "SELL_VOL_RESEARCH"
        elif percentile <= 0.20:
            hypothesis = "BUY_VOL_RESEARCH"
        else:
            hypothesis = "OBSERVE"
        ranked.append(RankedEventPremium(
            observation=observation,
            expected_log_variance_days=float(predictions[index]),
            residual=residual,
            percentile=float(percentile),
            robust_z=(residual - center) / scale,
            method=method,
            hypothesis=hypothesis,
        ))
    ranked.sort(key=lambda row: (-abs(row.residual), row.observation.symbol))
    return tuple(ranked)


def compare_with_historical_gaps(
    observation: EventPremiumObservation,
    *,
    event_dates: Iterable[str],
    daily_bars: Iterable[dict[str, Any]],
    minimum_events: int = 3,
) -> HistoricalEventMoveComparison:
    """Compare today's event price with prior close-to-next-open moves.

    Daily prices remain an intentionally conservative proxy for a next-morning
    straddle: ``abs(gap) / premium - 1`` is the intrinsic-value floor and omits
    remaining time value.  It is not an option-fill replay and therefore never
    sets ``validated_edge``.
    """
    requested = tuple(dict.fromkeys(event_dates))
    rows: list[tuple[dt.date, float, float]] = []
    for row in daily_bars:
        try:
            stamp = _timestamp(row["t"]).date()
            close = _positive("daily close", row["c"])
            open_price = _positive("daily open", row["o"])
        except (KeyError, EventPremiumUnavailable):
            continue
        rows.append((stamp, open_price, close))
    rows.sort(key=lambda item: item[0])
    index = {day: i for i, (day, _, _) in enumerate(rows)}
    gaps = []
    for value in requested:
        try:
            event_day = dt.date.fromisoformat(value)
        except ValueError as exc:
            raise EventPremiumUnavailable(f"invalid event date: {value}") from exc
        position = index.get(event_day)
        if position is None or position + 1 >= len(rows):
            continue
        event_close = rows[position][2]
        next_open = rows[position + 1][1]
        gaps.append(abs(next_open / event_close - 1.0))
    if len(gaps) < minimum_events:
        raise EventPremiumUnavailable(
            f"{observation.symbol}: only {len(gaps)} historical gaps; need {minimum_events}")
    values = np.asarray(gaps, dtype=float)
    premium = observation.executable_front_premium_to_spot
    median_gap = float(np.median(values))
    if premium <= 0 or median_gap <= 0:
        raise EventPremiumUnavailable("premium and median gap must be positive")
    floors = values / premium - 1.0
    return HistoricalEventMoveComparison(
        symbol=observation.symbol,
        event_dates_requested=len(requested),
        sample_size=len(gaps),
        absolute_gaps=tuple(float(value) for value in gaps),
        implied_event_move=observation.implied_event_move,
        executable_premium_to_spot=premium,
        mean_absolute_gap=float(np.mean(values)),
        median_absolute_gap=median_gap,
        p75_absolute_gap=float(np.quantile(values, 0.75)),
        implied_move_to_median_gap=observation.implied_event_move / median_gap,
        premium_to_median_gap=premium / median_gap,
        gap_exceeds_premium_rate=float(np.mean(values > premium)),
        intrinsic_floor_mean_return=float(np.mean(floors)),
        intrinsic_floor_median_return=float(np.median(floors)),
    )
