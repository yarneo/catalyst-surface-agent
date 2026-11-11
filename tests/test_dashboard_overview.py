import datetime as dt
import json
from zoneinfo import ZoneInfo

from trading_bot.dashboard_overview import automation_snapshot


ET = ZoneInfo("America/New_York")


def test_automation_snapshot_reports_breakers_and_next_cycle(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "session_date": "2026-09-04", "halted_today": False,
        "killed": False, "peak_equity": 100_000,
        "day_start_equity": 99_000,
    }))
    value = automation_snapshot(
        state, now=dt.datetime(2026, 9, 4, 10, 0, tzinfo=ET))
    assert value["status"] == "UNVERIFIED"
    assert value["session_date"] == "2026-09-04"
    assert value["next_cycle"] == {
        "phase": "risk", "at": "2026-09-04T10:01:00-04:00"}


def test_automation_snapshot_rolls_over_weekend(tmp_path):
    value = automation_snapshot(
        tmp_path / "missing.json",
        now=dt.datetime(2026, 9, 4, 17, 0, tzinfo=ET))
    assert value["status"] == "UNKNOWN"
    assert value["next_cycle"] == {
        "phase": "premarket", "at": "2026-09-08T09:00:00-04:00"}
