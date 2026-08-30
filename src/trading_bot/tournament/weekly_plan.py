"""Serialization, integrity, and rollover helpers for autonomous weekly plans."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


PLAN_SCHEMA_VERSION = 1


def plan_digest(plan: dict[str, Any]) -> str:
    value = dict(plan)
    value.pop("plan_sha256", None)
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
        allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def seal_plan(plan: dict[str, Any]) -> dict[str, Any]:
    output = dict(plan)
    output["schema_version"] = PLAN_SCHEMA_VERSION
    output["plan_sha256"] = plan_digest(output)
    return output


def verify_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValueError("weekly plan must be one JSON object")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError("unsupported weekly plan schema")
    supplied = plan.get("plan_sha256")
    if not isinstance(supplied, str) or supplied != plan_digest(plan):
        raise ValueError("weekly plan digest mismatch")
    window = plan.get("window")
    if not isinstance(window, dict) or set(window) != {"start", "deadline"}:
        raise ValueError("weekly plan window is invalid")
    if not isinstance(plan.get("events"), list):
        raise ValueError("weekly plan events must be a list")
    if plan.get("order_enabled") is not False:
        raise ValueError("research plan must never carry order authority")
    return plan


def read_plan(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text())
    return verify_plan(value)


def atomic_write_plan(path: str | Path, plan: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        seal_plan(plan), indent=2, sort_keys=True, default=str,
        allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
