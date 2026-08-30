import json

import pytest

from trading_bot.tournament.weekly_plan import (
    atomic_write_plan,
    read_plan,
)


def base_plan():
    return {
        "mode": "AUTONOMOUS_WEEKLY_RESEARCH_PLAN",
        "order_enabled": False,
        "window": {"start": "2026-08-31T09:30:00-04:00",
                   "deadline": "2026-09-04T09:30:00-04:00"},
        "events": [],
    }


def test_plan_is_atomically_sealed_and_verified(tmp_path):
    path = tmp_path / "plan.json"
    atomic_write_plan(path, base_plan())
    value = read_plan(path)
    assert len(value["plan_sha256"]) == 64


def test_plan_edit_is_detected_before_execution(tmp_path):
    path = tmp_path / "plan.json"
    atomic_write_plan(path, base_plan())
    value = json.loads(path.read_text())
    value["events"].append({"symbol": "INJECTED"})
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="digest mismatch"):
        read_plan(path)


def test_plan_can_never_smuggle_order_authority(tmp_path):
    path = tmp_path / "plan.json"
    value = base_plan()
    value["order_enabled"] = True
    atomic_write_plan(path, value)
    with pytest.raises(ValueError, match="order authority"):
        read_plan(path)
