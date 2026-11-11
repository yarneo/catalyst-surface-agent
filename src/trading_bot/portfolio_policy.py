"""Durable runtime policy for autonomously validated portfolio structure.

The file is mutable deployment state under ``data/`` and is never published.
Only a narrow, validated schema can reach live executors; research metadata is
kept alongside it for auditability but cannot add new instruments or leverage.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


POLICY_FILE = Path(__file__).resolve().parents[2] / "data" / "research" / "portfolio_policy.json"
DEFAULT_CONFIG = {
    "idle_rule": "split",
    "ma_window": 200,
    "bil_buffer": 0.10,
    "slow_streak": 6,
    "slow_regime_share": 0.0,
}
DEPLOYABLE_IDLE_RULES = frozenset({"split", "regime_qqq", "mf_always"})


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized config or fail closed on an unsupported candidate."""
    config = {**DEFAULT_CONFIG, **{key: value.get(key) for key in DEFAULT_CONFIG
                                  if value.get(key) is not None}}
    rule = str(config["idle_rule"])
    ma = int(config["ma_window"])
    buffer = float(config["bil_buffer"])
    streak = int(config["slow_streak"])
    regime_share = float(config["slow_regime_share"])
    if rule not in DEPLOYABLE_IDLE_RULES:
        raise ValueError(f"idle rule {rule!r} has no production executor")
    if not 100 <= ma <= 300:
        raise ValueError("MA window must be in [100, 300]")
    if not math.isfinite(buffer) or not 0.05 <= buffer <= 0.20:
        raise ValueError("BIL buffer must be in [0.05, 0.20]")
    if not 4 <= streak <= 12:
        raise ValueError("slow streak must be in [4, 12]")
    # The live allocation writer deliberately routes slow-freed weight entirely
    # to the idle router. Reintroducing direct leveraged-regime fallback needs a
    # separate executor/risk review and cannot be smuggled in by research data.
    if regime_share != 0.0:
        raise ValueError("non-zero slow_regime_share is not deployable")
    return {
        "idle_rule": rule, "ma_window": ma, "bil_buffer": buffer,
        "slow_streak": streak, "slow_regime_share": regime_share,
    }


def default_policy() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "DEFAULT",
        "updated_at": None,
        "source": "code_default",
        "config": dict(DEFAULT_CONFIG),
        "previous_config": None,
        "promotion_evidence": None,
    }


def load_policy(path: str | Path = POLICY_FILE) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        return default_policy()
    try:
        value = json.loads(source.read_text())
        if value.get("schema_version") != 1:
            raise ValueError("unsupported policy schema")
        value["config"] = validate_config(value.get("config") or {})
        return value
    except Exception as exc:
        # A corrupt mutable policy must never create an unintended allocation.
        result = default_policy()
        result.update(status="INVALID_FALLBACK", error=f"{type(exc).__name__}: {exc}")
        return result


def write_policy(
    config: dict[str, Any], *, previous_config: dict[str, Any] | None,
    status: str, source: str, evidence: dict[str, Any],
    path: str | Path = POLICY_FILE,
) -> dict[str, Any]:
    if status == "AUTO_PROMOTED_PAPER":
        if not (evidence.get("production_parity_verified") is True
                and evidence.get("frozen_cohort")
                and int(evidence.get("forward_sessions", 0)) >= 60
                and int(evidence.get("weekly_blocks", 0)) >= 12
                and float(evidence.get("cost_adjusted_lower_bound", 0)) > 0):
            raise ValueError("automatic promotion requires verified production parity and frozen forward evidence")
    normalized = validate_config(config)
    previous = validate_config(previous_config) if previous_config else None
    payload = {
        "schema_version": 1,
        "status": str(status),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(source),
        "config": normalized,
        "previous_config": previous,
        "promotion_evidence": evidence,
    }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return payload
