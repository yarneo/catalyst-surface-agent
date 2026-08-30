#!/usr/bin/env python3
"""Audit the reusable weekly engine and its named failure drills."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_bot.options.clock import now_et  # noqa: E402
from trading_bot.tournament.audit import AuditLedger  # noqa: E402


@dataclass(frozen=True)
class Drill:
    name: str
    command: tuple[str, ...]
    proves: str


def _pytest(name: str, proves: str, *selectors: str) -> Drill:
    return Drill(name, (sys.executable, "-m", "pytest", "-q", *selectors), proves)


def _run(drill: Drill, timeout_s: float) -> dict:
    started = time.monotonic()
    try:
        result = subprocess.run(
            drill.command, cwd=ROOT, capture_output=True, text=True,
            timeout=timeout_s, check=False)
        output = (result.stdout + "\n" + result.stderr).strip()
        return {
            "name": drill.name, "passed": result.returncode == 0,
            "return_code": result.returncode,
            "elapsed_s": round(time.monotonic() - started, 3),
            "proves": drill.proves,
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
            "output_tail": output[-800:],
        }
    except subprocess.TimeoutExpired:
        return {
            "name": drill.name, "passed": False, "return_code": None,
            "elapsed_s": round(time.monotonic() - started, 3),
            "proves": drill.proves, "output_sha256": None,
            "output_tail": "hard rehearsal timeout",
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default="data/weekly_event_rehearsal_evidence.jsonl")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--real", action="store_true",
                        help="also build and run a real read-only one-symbol plan")
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--featherless-env", default=".env.local")
    args = parser.parse_args()

    drills = [
        _pytest(
            "calendar_and_schedule_quorum",
            "Independent sources must agree and exchange sessions own every clock.",
            "tests/test_tournament_weekly.py",
            "tests/test_tournament_event_calendar.py"),
        _pytest(
            "automatic_historical_replay",
            "Generic expired option bars produce last-trade and adverse envelopes.",
            "tests/test_tournament_event_replay.py"),
        _pytest(
            "sealed_rollover_plan",
            "Plan edits and embedded order authority are rejected before execution.",
            "tests/test_tournament_weekly_plan.py"),
        _pytest(
            "multi_event_lifecycle",
            "The executor reads a sealed plan, rechecks entry, and exits despite mismatch.",
            "tests/test_weekly_event_runner.py"),
        _pytest(
            "broker_failure_lifecycle",
            "Unknown, partial, rejected, and cancel-race orders cannot duplicate risk.",
            "tests/test_execution.py"),
        _pytest(
            "semantic_fail_closed",
            "Malformed, ungrounded, timed-out, or quorumless model output cannot promote.",
            "tests/test_tournament_event_semantics.py"),
        _pytest(
            "publication_secret_scan",
            "Tracked source remains free of credential patterns.",
            "tests/test_check_no_secrets.py"),
    ]

    with tempfile.TemporaryDirectory(prefix="weekly-event-rehearsal-") as temp:
        if args.real:
            folder = Path(temp)
            plan = folder / "plan.json"
            evidence = folder / "evidence.jsonl"
            drills.extend([
                Drill(
                    "real_read_only_plan",
                    (sys.executable, str(ROOT / "scripts" / "build_weekly_event_plan.py"),
                     "--env", args.env, "--featherless-env", args.featherless_env,
                     "--symbols", "AVGO", "--output", str(plan),
                     "--ledger", str(evidence)),
                    "Yahoo, Nasdaq, Featherless, and Alpaca MCP complete with no order path."),
                Drill(
                    "real_read_only_cycle",
                    (sys.executable, str(ROOT / "scripts" / "run_weekly_event_agent.py"),
                     "--env", args.env, "--featherless-env", args.featherless_env,
                     "--plan", str(plan), "--book", str(folder / "book.json"),
                     "--ledger", str(evidence), "--lock", str(folder / "lock"),
                     "--no-auto-plan"),
                    "The real account/MCP lifecycle consumes the sealed plan without orders."),
            ])

        ledger = AuditLedger(ROOT / args.ledger)
        results = []
        for drill in drills:
            result = _run(drill, args.timeout)
            results.append(result)
            ledger.append("weekly_rehearsal_drill", result, recorded_at=now_et())
            print(f"{'PASS' if result['passed'] else 'FAIL'}  "
                  f"{drill.name} ({result['elapsed_s']:.3f}s)")
        passed = sum(result["passed"] for result in results)
        summary = {
            "passed": passed == len(results), "passed_count": passed,
            "total_count": len(results), "order_gate_changed": False,
            "current_avgo_runner_changed": False,
            "results": [{"name": row["name"], "passed": row["passed"]}
                        for row in results],
        }
        audit = ledger.append(
            "weekly_rehearsal_summary", summary, recorded_at=now_et())
        print(f"WEEKLY REHEARSAL {passed}/{len(results)} — audit=#{audit.sequence}")
        return 0 if summary["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
