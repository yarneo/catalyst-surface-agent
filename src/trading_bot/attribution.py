"""Audit-grade attribution helpers for live decisions and portfolio risk."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


AI_DECISION_LEDGER = Path(__file__).resolve().parents[2] / "data" / "ai_decisions.jsonl"


def append_ai_decision(row: dict, path: Path = AI_DECISION_LEDGER) -> None:
    """Append one executed-cycle decision; contains targets, never credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **row,
    }
    from .runtime_state import append_record
    append_record(path, payload)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def summarize_ai_decisions(rows: list[dict], *, cost_bps: float = 5.0) -> dict:
    """Mark the AI-adjusted target against a no-AI target at the next decision.

    This is a target-policy counterfactual, not broker accounting: for each
    interval it asks how the two target quantities would have moved at the same
    observed prices. Estimated turnover costs are charged to both branches;
    the reported value-add is net of their difference.
    """
    legacy_count = sum(r.get("evidence_version") != "ai-v2" for r in rows)
    versioned = [r for r in rows if r.get("evidence_version") == "ai-v2" and r.get("cohort")]
    if versioned:
        latest_cohort = max(versioned, key=lambda r: r["ts"])["cohort"]
        rows = [r for r in versioned if r["cohort"] == latest_cohort]
    # A repeated run in one session is one decision, not another independent bet.
    rows = list({(r.get("cohort", "legacy"), r.get("symbol"), r.get("session_date") or str(r.get("ts", ""))[:10]): r for r in rows}.values())
    ordered = sorted(rows, key=lambda r: (str(r.get("symbol", "")), str(r.get("ts", ""))))
    by_symbol: dict[str, list[dict]] = {}
    for row in ordered:
        if row.get("symbol") and row.get("price") is not None:
            by_symbol.setdefault(str(row["symbol"]), []).append(row)

    per_symbol = {}
    independent = {}
    cohorts = set(r.get("cohort") for r in rows if r.get("evidence_version") == "ai-v2")
    total_gross_value_add = 0.0
    total_cost_difference = 0.0
    marked_intervals = 0
    for symbol, observations in by_symbol.items():
        gross_value_add = 0.0
        intervals = 0
        for previous, current in zip(observations, observations[1:]):
            if previous.get("cohort") != current.get("cohort"):
                continue
            move = float(current["price"]) - float(previous["price"])
            with_ai = float(previous.get("target_qty_with_ai", 0.0))
            without_ai = float(previous.get("target_qty_without_ai", 0.0))
            gross_value_add += (with_ai - without_ai) * move
            if previous.get("evidence_version") == "ai-v2" and with_ai != without_ai:
                session = previous.get("session_date") or previous["ts"][:10]
                # Conservative round-trip cost for the changed quantity; cluster
                # all correlated symbols into one session observation.
                value = (with_ai-without_ai)*move - abs(with_ai-without_ai)*float(previous["price"])*2*cost_bps/10000
                independent[session] = independent.get(session, 0.0) + value
            intervals += 1
        ai_cost = no_ai_cost = 0.0
        prior_ai = prior_no_ai = 0.0
        for observation in observations:
            price = float(observation["price"])
            ai_target = float(observation.get("target_qty_with_ai", 0.0))
            no_ai_target = float(observation.get("target_qty_without_ai", 0.0))
            ai_cost += abs(ai_target - prior_ai) * price * cost_bps / 10_000.0
            no_ai_cost += abs(no_ai_target - prior_no_ai) * price * cost_bps / 10_000.0
            prior_ai, prior_no_ai = ai_target, no_ai_target
        cost_difference = ai_cost - no_ai_cost
        net_value_add = gross_value_add - cost_difference
        changed = sum(
            abs(float(r.get("target_qty_with_ai", 0.0)) -
                float(r.get("target_qty_without_ai", 0.0))) >= 1e-9
            for r in observations
        )
        per_symbol[symbol] = {
            "decisions": len(observations),
            "altered_decisions": changed,
            "marked_intervals": intervals,
            "gross_value_add": round(gross_value_add, 2),
            "incremental_estimated_cost": round(cost_difference, 2),
            "estimated_value_add": round(net_value_add, 2),
        }
        total_gross_value_add += gross_value_add
        total_cost_difference += cost_difference
        marked_intervals += intervals

    return {
        "evidence_version": "ai-v2" if cohorts else "legacy",
        "excluded_legacy_decisions": legacy_count if cohorts else 0,
        "cohort_count": len(cohorts),
        "independent_changed_sessions": len(independent),
        "value_add_lower_bound": (float(np.mean(list(independent.values())) - 3*np.std(list(independent.values()), ddof=1)/np.sqrt(len(independent))) if len(independent) > 1 else None),
        "method": "next_decision_target_mark",
        "costs_included": True,
        "assumed_cost_bps": float(cost_bps),
        "decisions": sum(len(v) for v in by_symbol.values()),
        "altered_decisions": sum(v["altered_decisions"] for v in per_symbol.values()),
        "marked_intervals": marked_intervals,
        "gross_value_add": round(total_gross_value_add, 2),
        "incremental_estimated_cost": round(total_cost_difference, 2),
        "estimated_value_add": round(total_gross_value_add - total_cost_difference, 2),
        "by_symbol": per_symbol,
    }


def ai_authority_gate(
    summary: dict,
    *,
    minimum_altered_decisions: int = 30,
    minimum_marked_intervals: int = 30,
) -> dict:
    """Evidence gate for granting the AI overlay order authority.

    Until every requirement passes, the overlay remains a measured shadow
    proposal and the quantitative target is what reaches the risk manager.
    """
    reasons = []
    if summary.get("evidence_version") != "ai-v2" or summary.get("cohort_count", 0) != 1:
        reasons.append("one frozen model/strategy cohort required")
    if summary.get("independent_changed_sessions", 0) < minimum_altered_decisions:
        reasons.append("insufficient independent changed sessions")
    if (summary.get("value_add_lower_bound") or 0) <= 0:
        reasons.append("positive session-cluster confidence bound required")
    altered = int(summary.get("altered_decisions", 0) or 0)
    marked = int(summary.get("marked_intervals", 0) or 0)
    value_add = float(summary.get("estimated_value_add", 0.0) or 0.0)
    if altered < minimum_altered_decisions:
        reasons.append(f"{altered}/{minimum_altered_decisions} altered decisions")
    if marked < minimum_marked_intervals:
        reasons.append(f"{marked}/{minimum_marked_intervals} marked intervals")
    if not summary.get("costs_included"):
        reasons.append("execution costs not included")
    if value_add <= 0:
        reasons.append(f"net value add is ${value_add:,.2f}")
    return {
        "eligible": not reasons,
        "mode": "order_authority" if not reasons else "shadow_advisory",
        "reasons": reasons,
        "minimum_altered_decisions": minimum_altered_decisions,
        "minimum_marked_intervals": minimum_marked_intervals,
    }


def summarize_execution_quality(intents: list[dict], fills: list[dict]) -> dict:
    """Join order intents to fills and estimate signed implementation shortfall.

    Positive dollars/bps are costs; negative values are price improvement. The
    reference is the strategy's decision price, not an exchange-arrival NBBO,
    so the result is explicitly diagnostic rather than a best-execution claim.
    """
    fills = [f for f in fills if float(f.get("filled_qty") or 0) > 0]
    fill_by_id = {str(row.get("order_id")): row for row in fills if row.get("order_id")}
    intents = list({str(r["order_id"]): r for r in intents if r.get("order_id")}.values())
    covered = set(str(r["order_id"]) for r in intents) & fill_by_id.keys()
    quote_covered = sum(1 for r in intents if str(r["order_id"]) in covered and (r.get("quote") or {}).get("bid"))
    measured = []
    by_sleeve: dict[str, dict] = {}
    pending = 0
    for intent in intents:
        reference = intent.get("reference_price")
        fill = fill_by_id.get(str(intent.get("order_id")))
        if not reference or not fill or not fill.get("filled_avg_price"):
            pending += 1
            continue
        reference = float(reference)
        fill_price = float(fill["filled_avg_price"])
        qty = float(fill.get("filled_qty") or intent.get("qty") or 0.0)
        side = str(intent.get("side", "buy")).lower()
        signed_per_share = fill_price - reference if side == "buy" else reference - fill_price
        cost = signed_per_share * qty
        notional = abs(reference * qty)
        sleeve = str(intent.get("sleeve") or "unknown")
        measured.append((cost, notional))
        bucket = by_sleeve.setdefault(sleeve, {
            "orders": 0, "reference_notional": 0.0, "estimated_slippage_usd": 0.0,
        })
        bucket["orders"] += 1
        bucket["reference_notional"] += notional
        bucket["estimated_slippage_usd"] += cost

    total_cost = sum(cost for cost, _ in measured)
    total_notional = sum(notional for _, notional in measured)
    for bucket in by_sleeve.values():
        notional = bucket["reference_notional"]
        bucket["slippage_bps"] = round(
            bucket["estimated_slippage_usd"] / notional * 10_000, 4
        ) if notional else None
        bucket["reference_notional"] = round(notional, 2)
        bucket["estimated_slippage_usd"] = round(bucket["estimated_slippage_usd"], 2)
    return {
        "method": "fill versus strategy decision price; positive is cost",
        "orders_with_intent": len(intents),
        "total_filled_orders": len(fills),
        "covered_filled_orders": len(covered),
        "uncovered_filled_orders": len(fill_by_id) - len(covered),
        "coverage_fraction": len(covered) / len(fill_by_id) if fill_by_id else None,
        "quote_covered_orders": quote_covered,
        "quote_basis": "IEX venue quote, not consolidated NBBO",
        "measured_orders": len(measured),
        "pending_or_unpriced": pending,
        "reference_notional": round(total_notional, 2),
        "estimated_slippage_usd": round(total_cost, 2),
        "slippage_bps": round(total_cost / total_notional * 10_000, 4)
        if total_notional else None,
        "by_sleeve": by_sleeve,
    }


def component_risk_contributions(
    weights: dict[str, float],
    returns: pd.DataFrame,
    *,
    annualization: int = 252,
) -> dict[str, float]:
    """Covariance-based component contributions, as shares of variance.

    Contributions sum to 1 (allowing negative diversifying contributions).
    Missing assets are omitted rather than assigned invented volatility.
    """
    symbols = [s for s in weights if s in returns.columns]
    clean = returns[symbols].dropna(how="any") if symbols else pd.DataFrame()
    if len(clean) < 2:
        return {}
    w = np.array([float(weights[s]) for s in symbols], dtype=float)
    cov = clean.cov().to_numpy(dtype=float) * annualization
    portfolio_variance = float(w @ cov @ w)
    if not np.isfinite(portfolio_variance) or portfolio_variance <= 0:
        return {}
    components = w * (cov @ w) / portfolio_variance
    return {
        symbol: float(value)
        for symbol, value in zip(symbols, components)
        if np.isfinite(value)
    }


def portfolio_tail_risk(
    weights: dict[str, float], returns: pd.DataFrame, *, equity: float,
    confidence: float = 0.99, minimum_observations: int = 20,
) -> dict:
    """Historical one-day VaR and expected shortfall for the current weights.

    This is telemetry, not a sizing rule. Cash absent from ``returns`` simply
    has zero market return; missing held assets make the result unavailable
    rather than assigning them invented volatility.
    """
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("equity must be finite and positive")
    symbols = [symbol for symbol in weights if symbol in returns.columns]
    missing = [s for s,w in weights.items() if abs(w) > 1e-6 and s not in returns.columns]
    if missing:
        return {"status": "UNAVAILABLE", "missing_assets": missing,
                "reason": "held assets missing from risk history"}
    clean = returns[symbols].dropna(how="any") if symbols else pd.DataFrame()
    if len(clean) < minimum_observations:
        return {
            "status": "BUILDING", "observations": len(clean),
            "minimum_observations": minimum_observations,
            "confidence": confidence,
        }
    vector = np.array([float(weights[symbol]) for symbol in symbols])
    portfolio = clean.to_numpy(dtype=float) @ vector
    portfolio = portfolio[np.isfinite(portfolio)]
    if len(portfolio) < minimum_observations:
        return {
            "status": "BUILDING", "observations": len(portfolio),
            "minimum_observations": minimum_observations,
            "confidence": confidence,
        }
    quantile = float(np.quantile(portfolio, 1 - confidence))
    tail = portfolio[portfolio <= quantile]
    var = max(0.0, -quantile)
    expected_shortfall = max(var, -float(np.mean(tail)))
    worst_day = max(0.0, -float(np.min(portfolio)))
    rng = np.random.default_rng(17)
    samples = rng.choice(portfolio, (400, len(portfolio)), replace=True)
    cutoffs = np.quantile(samples, 1-confidence, axis=1)
    es_samples = [-float(sample[sample <= cutoff].mean()) for sample,cutoff in zip(samples,cutoffs)]
    from .portfolio_research import stress_scenarios
    return {
        "status": "MEASURING",
        "method": "historical simulation on current weights; advisory only",
        "confidence": confidence,
        "observations": len(portfolio),
        "assets": symbols,
        "gross_market_exposure": round(float(np.abs(vector).sum()), 6),
        "var_fraction": round(var, 8),
        "var_usd": round(var * equity, 2),
        "expected_shortfall_fraction": round(expected_shortfall, 8),
        "expected_shortfall_usd": round(expected_shortfall * equity, 2),
        "worst_observed_fraction": round(worst_day, 8),
        "worst_observed_usd": round(worst_day * equity, 2),
        "tail_observations": len(tail),
        "tail_reliability": "LOW_SAMPLE" if len(tail) < 10 else "ESTIMATE",
        "es_bootstrap_90pct_interval": [float(x) for x in np.quantile(es_samples, [.05,.95])],
        "uncertainty_note": "Resampled historical range; does not bound unseen shocks or correlated crises.",
        "stress_scenarios": stress_scenarios(weights),
    }
