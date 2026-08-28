"""Hash-chained JSONL evidence ledger for research, decisions, orders, and P&L."""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any


class AuditCorrupt(RuntimeError):
    """The append-only result record was truncated, edited, or reordered."""


_SENSITIVE = ("secret", "password", "api_key", "apikey", "authorization",
              "credential", "private_key", "access_token", "refresh_token")


def _safe(value: Any, *, key: str = "") -> Any:
    if any(part in key.lower() for part in _SENSITIVE):
        return "<redacted>"
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(k): _safe(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


@dataclass(frozen=True)
class AuditRow:
    sequence: int
    recorded_at: str
    event_type: str
    payload: dict[str, Any]
    previous_hash: str
    hash: str


class AuditLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> list[AuditRow]:
        if not self.path.exists():
            return []
        rows: list[AuditRow] = []
        for line_no, line in enumerate(self.path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(AuditRow(**json.loads(line)))
            except (json.JSONDecodeError, TypeError) as exc:
                raise AuditCorrupt(
                    f"invalid audit row at line {line_no}: {exc}") from exc
        self._verify(rows)
        return rows

    @staticmethod
    def _verify(rows: list[AuditRow]) -> None:
        previous = "GENESIS"
        for expected, row in enumerate(rows, 1):
            if row.sequence != expected:
                raise AuditCorrupt(
                    f"audit sequence {row.sequence} should be {expected}")
            if row.previous_hash != previous:
                raise AuditCorrupt(f"audit chain broken at sequence {expected}")
            body = {
                "sequence": row.sequence,
                "recorded_at": row.recorded_at,
                "event_type": row.event_type,
                "payload": row.payload,
                "previous_hash": row.previous_hash,
            }
            actual = hashlib.sha256(_canonical(body)).hexdigest()
            if row.hash != actual:
                raise AuditCorrupt(f"audit hash mismatch at sequence {expected}")
            previous = row.hash

    def append(self, event_type: str, payload: Any, *,
               recorded_at: dt.datetime) -> AuditRow:
        if not event_type or len(event_type) > 100:
            raise ValueError("event_type must be non-empty and <= 100 characters")
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        safe_payload = _safe(payload)
        if not isinstance(safe_payload, dict):
            safe_payload = {"value": safe_payload}

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Append and verify while holding an advisory lock. launchd should run
        # one process, but the evidence chain must remain correct if a manual
        # read-only cycle overlaps it.
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            existing: list[AuditRow] = []
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    existing.append(AuditRow(**json.loads(line)))
                except (json.JSONDecodeError, TypeError) as exc:
                    raise AuditCorrupt(
                        f"invalid audit row at line {line_no}: {exc}") from exc
            self._verify(existing)
            sequence = len(existing) + 1
            previous = existing[-1].hash if existing else "GENESIS"
            body = {
                "sequence": sequence,
                "recorded_at": recorded_at.isoformat(timespec="microseconds"),
                "event_type": event_type,
                "payload": safe_payload,
                "previous_hash": previous,
            }
            digest = hashlib.sha256(_canonical(body)).hexdigest()
            row = AuditRow(**body, hash=digest)
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(asdict(row), sort_keys=True,
                                    separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return row
