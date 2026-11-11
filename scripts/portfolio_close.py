"""Run the reporting close once after the actual exchange close, including half days."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
from trading_bot.runtime_state import DATA
from trading_bot.sessions import now_et, session_bounds


def main():
    now = now_et()
    bounds = session_bounds(now.date())
    if bounds is None or now < bounds[1] + timedelta(minutes=5):
        return 0
    path = DATA / "portfolio_cycles.json"
    phases = json.loads(path.read_text()).get("phases", {}) if path.exists() else {}
    last = phases.get("close", {}).get("last_success_at")
    if last and datetime.fromisoformat(last) >= bounds[1]:
        return 0
    root = Path(__file__).resolve().parents[1]
    return subprocess.run([sys.executable, str(root / "scripts/run_portfolio.py"),
                           "--phase", "close", "--no-enrich"], cwd=root).returncode


if __name__ == "__main__":
    raise SystemExit(main())
