"""Evidence scorecards for strategies that are not yet allowed to trade.

These helpers deliberately measure without promoting anything.  A strategy may
be statistically interesting and still be operationally unready; keeping the
scorecard separate from allocation prevents a small, lucky sample from quietly
turning into live risk.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable


def brier_score(probabilities: Iterable[float], outcomes: Iterable[bool]) -> float:
    """Mean squared probability error; zero is perfect and one is worst."""
    pairs = [(float(p), bool(y)) for p, y in zip(probabilities, outcomes)]
    if not pairs:
        raise ValueError("at least one probability/outcome pair is required")
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p, _ in pairs):
        raise ValueError("probabilities must be finite and in [0, 1]")
    return sum((p - float(y)) ** 2 for p, y in pairs) / len(pairs)


def expected_calibration_error(
    probabilities: Iterable[float], outcomes: Iterable[bool], *, bins: int = 5,
) -> float:
    """Equal-width expected calibration error."""
    if bins < 1:
        raise ValueError("bins must be positive")
    pairs = [(float(p), bool(y)) for p, y in zip(probabilities, outcomes)]
    if not pairs:
        raise ValueError("at least one probability/outcome pair is required")
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p, _ in pairs):
        raise ValueError("probabilities must be finite and in [0, 1]")
    total = len(pairs)
    error = 0.0
    for bucket in range(bins):
        low, high = bucket / bins, (bucket + 1) / bins
        rows = [(p, y) for p, y in pairs
                if low <= p <= high and (bucket == bins - 1 or p < high)]
        if rows:
            confidence = sum(p for p, _ in rows) / len(rows)
            frequency = sum(float(y) for _, y in rows) / len(rows)
            error += len(rows) / total * abs(confidence - frequency)
    return error


def execution_quality(
    observations: Iterable[dict[str, Any]], *, prior_capture: float = 0.90,
    prior_weight: float = 5.0,
) -> dict[str, Any]:
    """Summarize debit-entry capture, with small samples shrunk to a prior.

    Capture is ``quoted midpoint / actual debit``.  One means a midpoint fill,
    less than one means spread/slippage was paid, and greater than one means
    price improvement.  The shrunk value prevents one unusually good fill from
    being mistaken for a durable execution advantage.
    """
    if not 0 < prior_capture <= 1 or prior_weight < 0:
        raise ValueError("invalid execution-quality prior")
    valid = []
    buckets: dict[str, list[float]] = defaultdict(list)
    slippage = []
    for row in observations:
        try:
            midpoint = float(row["quoted_mid"])
            fill = float(row["fill_debit"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(map(math.isfinite, (midpoint, fill))) or midpoint <= 0 or fill <= 0:
            continue
        capture = midpoint / fill
        valid.append(capture)
        slippage.append(fill - midpoint)
        buckets[str(row.get("bucket") or "unclassified")].append(capture)

    count = len(valid)
    raw = sum(valid) / count if count else None
    shrunk = ((prior_capture * prior_weight + sum(valid)) /
              (prior_weight + count)) if count else None
    return {
        "status": "MEASURING" if count else "COLLECTING",
        "observations": count,
        "raw_capture": round(raw, 6) if raw is not None else None,
        "shrunk_capture": round(shrunk, 6) if shrunk is not None else None,
        "mean_slippage_per_share": (
            round(sum(slippage) / count, 4) if count else None),
        "method": "entry midpoint divided by actual debit; five-fill shrinkage prior",
        "by_bucket": {
            key: {"observations": len(values),
                  "raw_capture": round(sum(values) / len(values), 6),
                  "shrunk_capture": round(
                      (prior_capture * prior_weight + sum(values)) /
                      (prior_weight + len(values)), 6)}
            for key, values in sorted(buckets.items())
        },
    }


def observations_from_audit(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Pair audited entry intents with real or virtual fills."""
    intents: dict[str, dict[str, Any]] = {}
    fills: dict[str, float] = {}
    for row in rows:
        event_type = getattr(row, "event_type", None)
        payload = getattr(row, "payload", None)
        if not isinstance(payload, dict):
            continue
        event_id = str(payload.get("event_id") or "")
        if not event_id:
            continue
        if event_type in {"weekly_entry_intent", "weekly_shadow_entry"}:
            intents[event_id] = payload
            if event_type == "weekly_shadow_entry":
                try:
                    fills[event_id] = float(payload["quoted_executable"])
                except (KeyError, TypeError, ValueError):
                    pass
        elif event_type == "weekly_entry_result":
            fill = payload.get("fill") or {}
            try:
                if fill.get("filled") and fill.get("avg_price") is not None:
                    fills[event_id] = float(fill["avg_price"])
            except (AttributeError, TypeError, ValueError):
                pass
    output = []
    for event_id, intent in intents.items():
        if event_id not in fills:
            continue
        output.append({
            "event_id": event_id,
            "quoted_mid": intent.get("quoted_mid"),
            "fill_debit": fills[event_id],
            "bucket": intent.get("execution_bucket") or "unclassified",
        })
    return output


def promotion_scorecard(
    forecasts: Iterable[dict[str, Any]], *, evidence_chain_verified: bool,
    silent_failures: int = 0, minimum_outcomes: int = 30,
    maximum_ece: float = 0.10,
) -> dict[str, Any]:
    """Return evidence needed for human promotion review, never auto-promote.

    Outcomes must use independent event IDs.  Net P/L is the actual or virtual
    result after executable entry/exit prices, so expectancy cannot hide costs.
    """
    if minimum_outcomes < 1 or not 0 <= maximum_ece <= 1:
        raise ValueError("invalid promotion threshold")
    unique: dict[str, dict[str, Any]] = {}
    for row in forecasts:
        key = str(row.get("event_id") or "")
        if key:
            unique[key] = row
    complete = []
    for row in unique.values():
        try:
            probability = float(row["probability"])
            pnl = float(row["net_pnl"])
        except (KeyError, TypeError, ValueError):
            continue
        if (math.isfinite(probability) and 0 <= probability <= 1
                and math.isfinite(pnl)):
            complete.append((probability, pnl > 0, pnl))

    probabilities = [row[0] for row in complete]
    outcomes = [row[1] for row in complete]
    pnls = [row[2] for row in complete]
    count = len(complete)
    brier = brier_score(probabilities, outcomes) if count else None
    ece = expected_calibration_error(probabilities, outcomes) if count else None
    # Climatology is a deliberately hard baseline: did the probabilities add
    # information beyond always predicting the observed win frequency?
    baseline = None
    skill = None
    if count:
        win_rate = sum(outcomes) / count
        baseline = brier_score([win_rate] * count, outcomes)
        skill = 0.0 if baseline == 0 else 1.0 - brier / baseline
    expectancy = sum(pnls) / count if count else None
    reasons = []
    if count < minimum_outcomes:
        reasons.append(f"{count} of {minimum_outcomes} independent outcomes collected")
    if count and (skill is None or skill <= 0):
        reasons.append("probabilities do not beat the observed-base-rate forecast")
    if count and ece is not None and ece > maximum_ece:
        reasons.append(f"calibration error exceeds {maximum_ece:.0%}")
    if expectancy is None or expectancy <= 0:
        reasons.append("cost-adjusted expectancy is not yet positive")
    if not evidence_chain_verified:
        reasons.append("evidence chain is not verified")
    if silent_failures:
        reasons.append(f"{silent_failures} silent or unresolved operational failures")
    eligible = not reasons
    return {
        "status": "ELIGIBLE_FOR_REVIEW" if eligible else "COLLECTING",
        "automatic_promotion": False,
        "eligible_for_review": eligible,
        "independent_outcomes": count,
        "minimum_outcomes": minimum_outcomes,
        "brier_score": round(brier, 6) if brier is not None else None,
        "baseline_brier": round(baseline, 6) if baseline is not None else None,
        "brier_skill_score": round(skill, 6) if skill is not None else None,
        "calibration_error": round(ece, 6) if ece is not None else None,
        "net_expectancy_usd": round(expectancy, 2) if expectancy is not None else None,
        "evidence_chain_verified": evidence_chain_verified,
        "silent_failures": silent_failures,
        "reasons": reasons or ["all measurement gates pass; human review is still required"],
    }


def forecasts_from_audit(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Pair audited forecasts and close P/L by event ID."""
    probabilities: dict[str, float] = {}
    outcomes: dict[str, float] = {}
    for row in rows:
        event_type = getattr(row, "event_type", None)
        payload = getattr(row, "payload", None)
        if not isinstance(payload, dict):
            continue
        event_id = str(payload.get("event_id") or "")
        if not event_id:
            continue
        if event_type in {"weekly_entry_intent", "weekly_shadow_entry"}:
            try:
                probabilities[event_id] = float(payload["forecast_probability"])
            except (KeyError, TypeError, ValueError):
                pass
        elif event_type == "weekly_shadow_exit":
            try:
                outcomes[event_id] = float(payload["realized_pnl"])
            except (KeyError, TypeError, ValueError):
                pass
    return [{"event_id": key, "probability": probability,
             "net_pnl": outcomes[key]}
            for key, probability in probabilities.items() if key in outcomes]
