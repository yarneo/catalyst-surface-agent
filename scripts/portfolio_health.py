"""An independent scheduled watchdog; status is consumed by existing cron alerts."""
import json
from trading_bot.dashboard_overview import automation_snapshot
from trading_bot.runtime_state import DATA, atomic_json
from trading_bot.sessions import iso_now


def main():
    status = automation_snapshot(DATA / "state.json")
    status["checked_at"] = iso_now()
    atomic_json(DATA / "watchdog.json", status)
    print(json.dumps(status))
    if status["status"] in {"UNKNOWN", "UNVERIFIED", "STATE_ERROR", "DEGRADED"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
