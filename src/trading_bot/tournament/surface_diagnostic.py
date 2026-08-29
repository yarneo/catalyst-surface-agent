"""Multi-strike option-surface diagnostics that never authorize a trade.

The execution policy deliberately consumes only the nearest common-strike
straddle. This module inspects a wider slice of the same Alpaca MCP chain so we
can explain what the market looked like without silently adding a new gate.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from trading_bot.options.clock import ET
from trading_bot.options.occ import BadOCC, parse

from .scheduled import ScheduledEventPolicy, surface_from_mcp


@dataclass(frozen=True)
class SmilePoint:
    strike: float
    log_moneyness_pct: float
    call_iv: float
    put_iv: float
    mean_iv: float


@dataclass(frozen=True)
class SurfaceDiagnostic:
    underlying: str
    expiry: str
    spot: float
    observed_at: dt.datetime
    quote_start: dt.datetime
    quote_end: dt.datetime
    max_quote_age_s: float
    point_count: int
    strike_min: float
    strike_max: float
    nearest_strike: float
    nearest_observed_iv: float
    fitted_atm_iv: float
    atm_skew_per_log_moneyness_pct: float
    quadratic_curvature_per_log_moneyness_pct2: float
    atm_second_derivative: float
    fit_rmse: float
    shape: str
    executable_premium_to_spot: float
    total_spread_pct: float
    points: tuple[SmilePoint, ...]
    diagnostic_only: bool = True
    policy_gate_changed: bool = False


def _timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError("option quote timestamp is missing")
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("option quote timestamp is not timezone-aware")
    return stamp.astimezone(ET)


def _iv(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("implied volatility is invalid") from exc
    if not math.isfinite(result) or not 0 < result < 10:
        raise ValueError("implied volatility is outside the diagnostic range")
    return result


def diagnose_surface(*, payload: Any, spot: float, observed_at: dt.datetime,
                     policy: ScheduledEventPolicy = ScheduledEventPolicy(),
                     max_abs_log_moneyness_pct: float = 10.0,
                     min_points: int = 5) -> SurfaceDiagnostic:
    """Fit a transparent quadratic to paired call/put IV observations.

    ``x`` is 100 * log(strike / spot), so slope and curvature are expressed per
    log-moneyness percentage point. The sign is descriptive only; the result is
    never passed to ``evaluate_entry`` or the order planner.
    """
    if not math.isfinite(spot) or spot <= 0:
        raise ValueError("spot must be finite and positive")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if min_points < 3:
        raise ValueError("min_points must be at least three")
    if max_abs_log_moneyness_pct <= 0:
        raise ValueError("moneyness window must be positive")
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshots"), dict):
        raise ValueError("option-chain payload has no snapshots object")

    by_strike: dict[float, dict[str, tuple[float, dt.datetime]]] = {}
    for symbol, row in payload["snapshots"].items():
        if not isinstance(row, dict):
            continue
        try:
            contract = parse(symbol)
        except BadOCC:
            continue
        if (contract.root != policy.underlying
                or contract.expiry.isoformat() != policy.expiry):
            continue
        x = 100.0 * math.log(contract.strike / spot)
        if abs(x) > max_abs_log_moneyness_pct:
            continue
        quote = row.get("latestQuote")
        if not isinstance(quote, dict):
            continue
        try:
            value = _iv(row.get("impliedVolatility"))
            stamp = _timestamp(quote.get("t"))
        except ValueError:
            continue
        right = "call" if contract.right == "C" else "put"
        by_strike.setdefault(contract.strike, {})[right] = (value, stamp)

    points: list[SmilePoint] = []
    stamps: list[dt.datetime] = []
    for strike, sides in sorted(by_strike.items()):
        if not {"call", "put"} <= sides.keys():
            continue
        call_iv, call_stamp = sides["call"]
        put_iv, put_stamp = sides["put"]
        x = 100.0 * math.log(strike / spot)
        points.append(SmilePoint(
            strike, x, call_iv, put_iv, (call_iv + put_iv) / 2.0))
        stamps.extend((call_stamp, put_stamp))
    if len(points) < min_points:
        raise ValueError(
            f"surface diagnostic needs {min_points} paired strikes; got {len(points)}")

    x_values = np.asarray([point.log_moneyness_pct for point in points], dtype=float)
    y_values = np.asarray([point.mean_iv for point in points], dtype=float)
    curvature, slope, intercept = np.polyfit(x_values, y_values, 2)
    fitted = curvature * x_values ** 2 + slope * x_values + intercept
    rmse = float(np.sqrt(np.mean((y_values - fitted) ** 2)))
    curvature_value = float(curvature)
    if curvature_value > 1e-4:
        shape = "convex smile"
    elif curvature_value < -1e-4:
        shape = "concave smile"
    else:
        shape = "approximately flat"

    nearest = min(points, key=lambda point: (abs(point.strike - spot), point.strike))
    selected = surface_from_mcp(
        payload=payload, spot=spot, observed_at=observed_at, policy=policy)
    observed_et = observed_at.astimezone(ET)
    quote_start, quote_end = min(stamps), max(stamps)
    return SurfaceDiagnostic(
        underlying=policy.underlying,
        expiry=policy.expiry,
        spot=spot,
        observed_at=observed_et,
        quote_start=quote_start,
        quote_end=quote_end,
        max_quote_age_s=(observed_et - quote_start).total_seconds(),
        point_count=len(points),
        strike_min=points[0].strike,
        strike_max=points[-1].strike,
        nearest_strike=nearest.strike,
        nearest_observed_iv=nearest.mean_iv,
        fitted_atm_iv=float(intercept),
        atm_skew_per_log_moneyness_pct=float(slope),
        quadratic_curvature_per_log_moneyness_pct2=curvature_value,
        atm_second_derivative=2.0 * curvature_value,
        fit_rmse=rmse,
        shape=shape,
        executable_premium_to_spot=(
            selected.executable_debit(policy.order_buffer) / spot),
        total_spread_pct=selected.total_spread_pct,
        points=tuple(points),
    )
