"""Calendar-aware operational status, based on completed work."""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from .sessions import session_bounds

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc


def _next_cycle(now):
    now = now.astimezone(ET)
    for offset in range(10):
        day = now.date() + dt.timedelta(days=offset)
        bounds = session_bounds(day)
        if bounds is None:
            continue
        opening, closing = bounds
        candidates = [(opening - dt.timedelta(minutes=30), "premarket"),
                      (opening + dt.timedelta(minutes=5), "open"),
                      (closing + dt.timedelta(minutes=5), "close")]
        if opening <= now < closing:
            candidates.append((now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1), "risk"))
        future = [(at, phase) for at, phase in candidates if at > now]
        if future:
            return min(future)
    raise RuntimeError("exchange calendar has no upcoming session")


def automation_snapshot(state_path, *, now=None):
    observed = now or dt.datetime.now(UTC)
    if observed.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    next_at, phase = _next_cycle(observed)
    rebalance = None
    for offset in range(10):
        bounds = session_bounds(observed.astimezone(ET).date() + dt.timedelta(days=offset))
        if bounds and bounds[0] + dt.timedelta(minutes=5) > observed:
            rebalance = (bounds[0] + dt.timedelta(minutes=5)).isoformat()
            break
    output = {"status": "UNKNOWN", "session_date": None, "last_state_write": None,
              "halted_today": None, "killed": None,
              "next_cycle": {"phase": phase, "at": next_at.isoformat()},
              "next_rebalance": {"phase": "open", "at": rebalance},
              "phases": {}, "issues": []}
    path = Path(state_path)
    if not path.exists():
        return output
    try:
        state = json.loads(path.read_text())
        output.update(session_date=state.get("session_date"), halted_today=bool(state.get("halted_today")),
                      killed=bool(state.get("killed")),
                      last_state_write=dt.datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat())
        cycle_path = path.parent / "portfolio_cycles.json"
        if not cycle_path.exists():
            output["status"] = "UNVERIFIED"
            output["issues"].append("No completed cycle heartbeat recorded")
        else:
            phases = json.loads(cycle_path.read_text()).get("phases", {})
            output["phases"] = phases
            release_path = path.parent / "release.json"
            release = json.loads(release_path.read_text()) if release_path.exists() else {}
            deployed = dt.datetime.fromisoformat(release.get("deployed_at") or "1970-01-01T00:00:00+00:00")
            output["release"] = release
            bounds = session_bounds(observed.astimezone(ET).date())
            if bounds:
                opening, closing = bounds
                due = {"premarket": opening - dt.timedelta(minutes=30),
                       "open": opening + dt.timedelta(minutes=5),
                       "close": closing + dt.timedelta(minutes=5)}
                for name, scheduled in due.items():
                    if scheduled < deployed or observed < scheduled + dt.timedelta(minutes=10):
                        continue
                    row = phases.get(name, {})
                    completed = dt.datetime.fromisoformat(row.get("last_success_at") or "1970-01-01T00:00:00+00:00")
                    if row.get("status") == "FAILED" or completed < scheduled:
                        output["issues"].append(name + " cycle failed or overdue")
                if opening + dt.timedelta(minutes=5) <= observed < closing:
                    row = phases.get("risk", {})
                    completed = dt.datetime.fromisoformat(row.get("last_success_at") or "1970-01-01T00:00:00+00:00")
                    if (observed - completed).total_seconds() > 300 or row.get("status") == "FAILED":
                        output["issues"].append("Intraday protection failed or heartbeat older than five minutes")
            output["status"] = "DEGRADED" if output["issues"] else "HEALTHY"
        if state.get("killed"):
            output["status"] = "KILLED"
        elif state.get("halted_today") and state.get("session_date") == observed.astimezone(ET).date().isoformat():
            output["status"] = "HALTED_TODAY"
    except (OSError, ValueError, TypeError, KeyError):
        output["status"] = "STATE_ERROR"
        output["issues"].append("Operational state is unreadable")
    return output
