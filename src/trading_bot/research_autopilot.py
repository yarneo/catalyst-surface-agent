"""Forward-only state machine for autonomous paper-strategy promotion.

Historical research may nominate a candidate, but it cannot directly rewrite
production. A strict survivor first accumulates non-overlapping weekly shadow
outcomes. Promotion is permitted only after return, calibration, cost and risk
gates pass; deployed candidates retain an automatic rollback comparison.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .portfolio_policy import validate_config
from .strategy_evidence import brier_score, expected_calibration_error


STATE_FILE = Path(__file__).resolve().parents[2] / "data" / "research" / "autopilot.json"
MIN_SHADOW_WEEKS = 8
MIN_WIN_RATE = 0.55
MAX_CALIBRATION_ERROR = 0.20
RISK_TOLERANCE = 0.01
ROLLBACK_WEEKS = 4
ROLLBACK_EXCESS_RETURN = -0.02
ROLLBACK_RISK_GAP = 0.02


def candidate_id(config: dict[str, Any]) -> str:
    encoded = json.dumps(validate_config(config), sort_keys=True,
                         separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def load_state(path: str | Path = STATE_FILE) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        return {"schema_version": 1, "shadow": None, "deployment": None, "history": []}
    try:
        value = json.loads(source.read_text())
        if value.get("schema_version") != 1:
            raise ValueError("unsupported autopilot schema")
        return value
    except Exception as exc:
        return {"schema_version": 1, "shadow": None, "deployment": None,
                "history": [], "state_error": f"{type(exc).__name__}: {exc}"}


def save_state(value: dict[str, Any], path: str | Path = STATE_FILE) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _weekly(series: pd.Series) -> pd.Series:
    clean = series.dropna().sort_index()
    if clean.empty:
        return clean
    weekly = (1.0 + clean).resample("W-FRI").prod() - 1.0
    # A Monday-through-Thursday tail is labelled with the *future* Friday by
    # pandas.  Never count that partial period as forward evidence.  Sunday
    # research runs still include the just-completed Friday normally.
    last_observation = pd.Timestamp(clean.index.max())
    return weekly.loc[weekly.index <= last_observation]


def latest_week(candidate: pd.Series, champion: pd.Series) -> dict[str, Any] | None:
    frame = pd.concat({"candidate": _weekly(candidate),
                       "champion": _weekly(champion)}, axis=1).dropna()
    if frame.empty:
        return None
    stamp, row = frame.iloc[-1].name, frame.iloc[-1]
    return {
        "week": pd.Timestamp(stamp).date().isoformat(),
        "candidate_return": float(row["candidate"]),
        "champion_return": float(row["champion"]),
        "excess_return": float(row["candidate"] - row["champion"]),
        "costs_included": True,
    }


def historical_outperformance_probability(
    candidate: pd.Series, champion: pd.Series, *, trailing_weeks: int = 104,
) -> float:
    frame = pd.concat({"candidate": _weekly(candidate),
                       "champion": _weekly(champion)}, axis=1).dropna().tail(trailing_weeks)
    if len(frame) < 8:
        return 0.5
    wins = float((frame["candidate"] > frame["champion"]).sum())
    # Beta(1,1) smoothing avoids claiming certainty from a finite backtest.
    return float((wins + 1.0) / (len(frame) + 2.0))


def shadow_metrics(observations: list[dict[str, Any]]) -> dict[str, Any]:
    if not observations:
        return {
            "weeks": 0, "mean_excess_return": None, "cumulative_excess_return": None,
            "win_rate": None, "brier_score": None, "calibration_error": None,
            "candidate_worst_week": None, "champion_worst_week": None,
            "eligible": False,
            "reasons": [f"0 of {MIN_SHADOW_WEEKS} forward weeks collected"],
        }
    excess = np.array([float(row["excess_return"]) for row in observations])
    candidate = np.array([float(row["candidate_return"]) for row in observations])
    champion = np.array([float(row["champion_return"]) for row in observations])
    probabilities = [float(row["forecast_probability"]) for row in observations]
    outcomes = [value > 0 for value in excess]
    n = len(observations)
    mean_excess = float(excess.mean())
    cumulative = float(np.prod(1 + candidate) / np.prod(1 + champion) - 1)
    win_rate = float(np.mean(outcomes))
    brier = brier_score(probabilities, outcomes)
    ece = expected_calibration_error(probabilities, outcomes)
    candidate_worst = float(candidate.min())
    champion_worst = float(champion.min())
    reasons = []
    if n < MIN_SHADOW_WEEKS:
        reasons.append(f"{n} of {MIN_SHADOW_WEEKS} forward weeks collected")
    if mean_excess <= 0 or cumulative <= 0:
        reasons.append("forward cost-adjusted excess return is not positive")
    if win_rate < MIN_WIN_RATE:
        reasons.append(f"weekly win rate is below {MIN_WIN_RATE:.0%}")
    if brier >= 0.25:
        reasons.append("probability forecast does not beat a 50/50 baseline")
    if ece > MAX_CALIBRATION_ERROR:
        reasons.append(f"calibration error exceeds {MAX_CALIBRATION_ERROR:.0%}")
    if candidate_worst < champion_worst - RISK_TOLERANCE:
        reasons.append("worst forward week is more than 1 percentage point worse")
    return {
        "weeks": n,
        "mean_excess_return": round(mean_excess, 8),
        "cumulative_excess_return": round(cumulative, 8),
        "win_rate": round(win_rate, 6),
        "brier_score": round(brier, 6),
        "calibration_error": round(ece, 6),
        "candidate_worst_week": round(candidate_worst, 8),
        "champion_worst_week": round(champion_worst, 8),
        "eligible": not reasons,
        "reasons": reasons or ["all forward promotion gates pass"],
    }


def advance_shadow(
    state: dict[str, Any], *, candidate: dict[str, Any] | None,
    candidate_returns: pd.Series | None, champion_returns: pd.Series,
    observed_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Start, update, reject, or promote one strict-survivor shadow."""
    shadow = state.get("shadow")
    if candidate is None:
        if shadow and shadow.get("status") == "SHADOWING":
            shadow["status"] = "REJECTED"
            shadow["rejected_at"] = observed_at
            shadow["reason"] = "candidate no longer clears the strict historical gauntlet"
            state.setdefault("history", []).append(dict(shadow))
            state["shadow"] = None
            return state, {"action": "SHADOW_REJECTED", "candidate": shadow.get("name")}
        return state, {"action": "NO_SURVIVOR"}

    try:
        config = validate_config(candidate.get("config") or {})
    except ValueError as exc:
        return state, {"action": "UNDEPLOYABLE_SURVIVOR", "candidate": candidate.get("name"),
                       "reason": str(exc)}
    if candidate_returns is None:
        return state, {"action": "DATA_ERROR", "reason": "candidate returns unavailable"}
    identity = candidate_id(config)
    latest = latest_week(candidate_returns, champion_returns)
    probability = historical_outperformance_probability(candidate_returns, champion_returns)
    if latest is None:
        return state, {"action": "DATA_ERROR", "reason": "weekly return panel is empty"}

    if not shadow or shadow.get("candidate_id") != identity or shadow.get("status") != "SHADOWING":
        state["shadow"] = {
            "status": "SHADOWING", "candidate_id": identity,
            "name": str(candidate.get("name") or identity), "config": config,
            "started_at": observed_at, "last_observed_week": latest["week"],
            "pending_forecast": probability, "observations": [],
            "metrics": shadow_metrics([]),
        }
        return state, {"action": "SHADOW_STARTED", "candidate": state["shadow"]["name"],
                       "minimum_weeks": MIN_SHADOW_WEEKS}

    if latest["week"] != shadow.get("last_observed_week"):
        latest["forecast_probability"] = float(shadow.get("pending_forecast", 0.5))
        shadow.setdefault("observations", []).append(latest)
        shadow["last_observed_week"] = latest["week"]
    shadow["pending_forecast"] = probability
    shadow["last_checked_at"] = observed_at
    shadow["metrics"] = shadow_metrics(shadow.get("observations") or [])
    if shadow["metrics"]["eligible"]:
        shadow["status"] = "READY_TO_PROMOTE"
        return state, {"action": "PROMOTE", "candidate": shadow["name"],
                       "config": config, "evidence": shadow["metrics"]}
    return state, {"action": "SHADOW_PROGRESS", "candidate": shadow["name"],
                   "evidence": shadow["metrics"]}


def rollback_metrics(observations: list[dict[str, Any]]) -> dict[str, Any]:
    if not observations:
        return {"weeks": 0, "rollback": False, "reasons": []}
    recent = observations[-ROLLBACK_WEEKS:]
    candidate = np.array([float(row["candidate_return"]) for row in recent])
    baseline = np.array([float(row["champion_return"]) for row in recent])
    cumulative_excess = float(np.prod(1 + candidate) / np.prod(1 + baseline) - 1)
    risk_gap = float(candidate.min() - baseline.min())
    reasons = []
    if len(recent) >= ROLLBACK_WEEKS and cumulative_excess <= ROLLBACK_EXCESS_RETURN:
        reasons.append(f"{ROLLBACK_WEEKS}-week excess return <= {ROLLBACK_EXCESS_RETURN:.0%}")
    if risk_gap <= -ROLLBACK_RISK_GAP:
        reasons.append("worst weekly loss is at least 2 percentage points worse than baseline")
    return {"weeks": len(observations), "recent_weeks": len(recent),
            "recent_cumulative_excess_return": round(cumulative_excess, 8),
            "worst_week_risk_gap": round(risk_gap, 8),
            "rollback": bool(reasons), "reasons": reasons}


def advance_rollback(
    deployment: dict[str, Any] | None, *, candidate_returns: pd.Series,
    baseline_returns: pd.Series, observed_at: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not deployment or deployment.get("status") != "DEPLOYED":
        return deployment, {"action": "NO_DEPLOYMENT"}
    latest = latest_week(candidate_returns, baseline_returns)
    if latest and latest["week"] != deployment.get("last_observed_week"):
        deployment.setdefault("observations", []).append(latest)
        deployment["last_observed_week"] = latest["week"]
    deployment["last_checked_at"] = observed_at
    deployment["metrics"] = rollback_metrics(deployment.get("observations") or [])
    return deployment, {
        "action": "ROLLBACK" if deployment["metrics"]["rollback"] else "DEPLOYMENT_HEALTHY",
        "candidate": deployment.get("name"), "evidence": deployment["metrics"],
    }


def recover_deployment(
    state: dict[str, Any], *, policy: dict[str, Any],
    candidate_returns: pd.Series, baseline_returns: pd.Series,
    observed_at: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Rebuild rollback monitoring if policy persistence won a crash race.

    Policy and evidence state are separate atomic files.  If the process dies
    after writing the promoted policy but before saving the state, the next run
    reconstructs the monitor from the rollback config embedded in the policy.
    """
    if state.get("deployment"):
        return state, None
    if policy.get("status") != "AUTO_PROMOTED_PAPER" or not policy.get("previous_config"):
        return state, None
    config = validate_config(policy.get("config") or {})
    baseline = validate_config(policy["previous_config"])
    latest = latest_week(candidate_returns, baseline_returns)
    name = str((policy.get("promotion_evidence") or {}).get("candidate")
               or f"policy-{candidate_id(config)}")
    state["deployment"] = {
        "status": "DEPLOYED", "candidate_id": candidate_id(config),
        "name": name, "config": config, "baseline_config": baseline,
        "promoted_at": policy.get("updated_at"), "recovered_at": observed_at,
        "last_observed_week": latest["week"] if latest else None,
        "observations": [], "metrics": {"weeks": 0, "rollback": False},
    }
    return state, {"action": "DEPLOYMENT_RECOVERED", "candidate": name}
