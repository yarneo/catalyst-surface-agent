#!/usr/bin/env python3
"""Run the production event-options observer with no order-capable interface.

This wrapper intentionally exposes no ``--enable-orders`` argument. It owns a
separate sealed plan, virtual book, audit ledger, and lock under
``data/event_options_shadow`` and cannot mutate the live strategy's book.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA = "data/event_options_shadow"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    shared = [
        "--env", ".env", "--featherless-env", ".env",
        "--production-shadow",
    ]
    if args.plan_only:
        command = [
            sys.executable, str(ROOT / "scripts" / "build_weekly_event_plan.py"),
            *shared,
            "--output", f"{DATA}/weekly_plan.json",
            "--ledger", f"{DATA}/evidence.jsonl",
            "--archive-dir", f"{DATA}/plans",
        ]
    else:
        command = [
            sys.executable, str(ROOT / "scripts" / "run_weekly_event_agent.py"),
            *shared,
            "--plan", f"{DATA}/weekly_plan.json",
            "--book", f"{DATA}/virtual_book.json",
            "--ledger", f"{DATA}/evidence.jsonl",
            "--planner-ledger", f"{DATA}/evidence.jsonl",
            "--planner-archive-dir", f"{DATA}/plans",
            "--lock", f"{DATA}/agent.lock",
        ]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
