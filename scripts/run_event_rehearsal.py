#!/usr/bin/env python3
"""Run the production shadow path and named failure drills, then audit them."""

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
            "name": drill.name,
            "passed": result.returncode == 0,
            "return_code": result.returncode,
            "elapsed_s": round(time.monotonic() - started, 3),
            "proves": drill.proves,
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
            "output_tail": output[-800:],
        }
    except subprocess.TimeoutExpired as exc:
        output = f"{exc.stdout or ''}\n{exc.stderr or ''}".strip()
        return {
            "name": drill.name,
            "passed": False,
            "return_code": None,
            "elapsed_s": round(time.monotonic() - started, 3),
            "proves": drill.proves,
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
            "output_tail": "hard rehearsal timeout",
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--featherless-env", default=".env.local")
    parser.add_argument("--ledger", default="data/preflight_evidence.jsonl")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="csa-rehearsal-") as temp:
        temp_path = Path(temp)
        runner = str(ROOT / "scripts" / "run_event_agent.py")
        common = (
            sys.executable, runner, "--env", args.env,
            "--featherless-env", args.featherless_env,
        )
        drills = [
            Drill(
                "replacement_account_pin",
                (*common, "--verify-account"),
                "The configured paper account is pinned, flat, enabled, and unblocked.",
            ),
            Drill(
                "real_mcp_shadow_cycle",
                (*common, "--book", str(temp_path / "shadow_book.json"),
                 "--ledger", str(temp_path / "shadow_evidence.jsonl")),
                "The real Alpaca MCP lifecycle completes read-only with no order flag.",
            ),
            _pytest(
                "entry_window_end_to_end_shadow",
                "At the synthetic entry clock all surface, Featherless, integrity, risk, "
                "and planning stages run but no order intent is consumed.",
                "tests/test_tournament_runner.py::test_entry_window_shadow_runs_all_gates_without_consuming_live_attempt",
            ),
            _pytest(
                "scheduled_and_emergency_exit",
                "Both the 09:45 exit and 15:30 emergency deadline execute despite reconciliation disagreement.",
                "tests/test_tournament_runner.py::test_exit_clock_still_runs_when_reconciliation_blocks_new_entries",
                "tests/test_tournament_runner.py::test_emergency_flat_deadline_is_explicit_and_still_retries_exit",
            ),
            _pytest(
                "order_interlocks",
                "The command-line order flag cannot bypass the frozen environment switch.",
                "tests/test_tournament_runner.py::test_order_enable_flag_also_requires_frozen_environment_switch",
                "tests/test_mcp_client.py::test_orders_are_refused_unless_live",
            ),
            _pytest(
                "stale_and_malformed_market_data",
                "Stale stock data and malformed or stale option data fail closed.",
                "tests/test_tournament_runner.py::test_latest_stock_trade_must_be_fresh",
                "tests/test_tournament_scheduled.py::test_malformed_or_wrong_expiry_payload_never_creates_a_surface",
                "tests/test_tournament_scheduled.py::test_each_surface_gate_has_an_explicit_no_trade_reason",
            ),
            _pytest(
                "broker_failure_lifecycle",
                "Rejected cancels, lost replies, partial fills, and unpollable orders cannot duplicate exposure.",
                "tests/test_execution.py",
            ),
            _pytest(
                "featherless_fail_closed",
                "Malformed, ungrounded, timed-out, or quorumless model output cannot authorize entry.",
                "tests/test_featherless_router.py",
                "tests/test_tournament_integrity.py",
            ),
        ]

        ledger = AuditLedger(ROOT / args.ledger)
        results = []
        for drill in drills:
            result = _run(drill, args.timeout)
            results.append(result)
            ledger.append("rehearsal_drill", result, recorded_at=now_et())
            state = "PASS" if result["passed"] else "FAIL"
            print(f"{state}  {drill.name} ({result['elapsed_s']:.3f}s)")

        passed = sum(bool(result["passed"]) for result in results)
        summary = {
            "passed": passed == len(results),
            "passed_count": passed,
            "total_count": len(results),
            "order_gate_changed": False,
            "mode": "real read-only MCP plus deterministic production-path drills",
            "results": [{"name": row["name"], "passed": row["passed"]}
                        for row in results],
        }
        row = ledger.append("rehearsal_summary", summary, recorded_at=now_et())
        print(f"REHEARSAL {passed}/{len(results)} — audit=#{row.sequence}")
        return 0 if summary["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
