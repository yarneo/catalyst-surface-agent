"""Verify a published evidence chain. No credentials, no network.

The ledger is hash-chained: every row carries the hash of the row before it, so
altering or removing any row invalidates every hash after it. This recomputes
the whole chain from the file on disk and reports what it found.

    python scripts/verify_evidence.py

It also re-checks the two claims the chain is published to support: that no
sensitive field survived redaction, and that no row silently changed a policy
gate. A judge should be able to run this on a fresh clone and get the same
answer we do.

Exits non-zero if the chain does not verify, so it is usable in CI.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_bot.tournament.audit import AuditCorrupt, AuditLedger  # noqa: E402

CANDIDATES = (
    ROOT / "evidence" / "preflight_evidence.jsonl",
    ROOT / "data" / "preflight_evidence.jsonl",
)

SENSITIVE = ("secret", "password", "api_key", "apikey", "authorization",
             "credential", "account_number", "access_token", "token_id")


def _sensitive_fields(value, key: str = "") -> list[str]:
    """Any surviving key whose *name* implies a credential, redacted or not."""
    found: list[str] = []
    if any(word in key.lower() for word in SENSITIVE) and value != "<redacted>":
        found.append(key)
    if isinstance(value, dict):
        for k, v in value.items():
            found += _sensitive_fields(v, str(k))
    elif isinstance(value, list):
        for item in value:
            found += _sensitive_fields(item, key)
    return found


def _resolve(given: Path | None) -> Path:
    if given is not None:
        return given
    for candidate in CANDIDATES:
        if candidate.exists():
            return candidate
    raise SystemExit("no evidence chain found; pass a path explicitly")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=None)
    args = parser.parse_args()
    path = _resolve(args.path)

    try:
        rows = AuditLedger(path).read()
    except AuditCorrupt as exc:
        print(f"FAILED — {path.name} does not verify: {exc}")
        return 1

    if not rows:
        print(f"FAILED — {path} is empty; there is nothing to verify")
        return 1

    print(f"chain      {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    print(f"rows       {len(rows)} · sequence 1..{rows[-1].sequence} unbroken")
    print(f"span       {rows[0].recorded_at} -> {rows[-1].recorded_at}")
    print(f"terminal   {rows[-1].hash}")

    print("\ncontents")
    for event_type, count in sorted(Counter(r.event_type for r in rows).items()):
        print(f"  {count:3d}  {event_type}")

    leaked = sorted({field for row in rows
                     for field in _sensitive_fields(row.payload)})
    changed = [r.sequence for r in rows
               if r.payload.get("authority", {}).get("policy_gate_changed")]

    print("\nassertions")
    print(f"  {'PASS' if not leaked else 'FAIL'}  no unredacted credential field "
          f"({len(leaked)} found)")
    print(f"  {'PASS' if not changed else 'FAIL'}  no row changed a policy gate "
          f"({len(changed)} found)")

    if leaked:
        print("\n  surviving sensitive field names:", ", ".join(leaked))
    if changed:
        print("\n  rows that changed a gate:", changed)
    if leaked or changed:
        return 1

    print(f"\nVERIFIED — {len(rows)} rows, hash chain intact, no credential "
          f"field, no gate change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
