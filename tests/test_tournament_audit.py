import datetime as dt
import json

import pytest

from trading_bot.options.clock import ET
from trading_bot.tournament.audit import AuditCorrupt, AuditLedger


NOW = dt.datetime(2026, 9, 2, 15, 30, tzinfo=ET)


def test_audit_rows_are_ordered_hash_chained_and_reloadable(tmp_path):
    ledger = AuditLedger(tmp_path / "evidence.jsonl")
    first = ledger.append("surface", {"premium": 29.9}, recorded_at=NOW)
    second = ledger.append("decision", {"eligible": True}, recorded_at=NOW)
    rows = ledger.read()
    assert [row.sequence for row in rows] == [1, 2]
    assert first.previous_hash == "GENESIS"
    assert second.previous_hash == first.hash


def test_sensitive_keys_are_redacted_recursively(tmp_path):
    ledger = AuditLedger(tmp_path / "evidence.jsonl")
    ledger.append("config", {
        "api_key": "do-not-store",
        "nested": {"secret_key": "also-no", "safe": "AVGO"},
    }, recorded_at=NOW)
    text = ledger.path.read_text()
    assert "do-not-store" not in text and "also-no" not in text
    row = ledger.read()[0]
    assert row.payload["api_key"] == "<redacted>"
    assert row.payload["nested"]["safe"] == "AVGO"


def test_editing_a_past_payload_breaks_verification(tmp_path):
    ledger = AuditLedger(tmp_path / "evidence.jsonl")
    ledger.append("decision", {"eligible": False}, recorded_at=NOW)
    row = json.loads(ledger.path.read_text())
    row["payload"]["eligible"] = True
    ledger.path.write_text(json.dumps(row) + "\n")
    with pytest.raises(AuditCorrupt, match="hash mismatch"):
        ledger.read()


def test_truncated_last_line_is_corrupt_not_ignored(tmp_path):
    ledger = AuditLedger(tmp_path / "evidence.jsonl")
    ledger.append("decision", {"eligible": False}, recorded_at=NOW)
    with ledger.path.open("a") as handle:
        handle.write('{"sequence":2')
    with pytest.raises(AuditCorrupt, match="invalid audit row"):
        ledger.read()


def test_timestamp_must_be_timezone_aware(tmp_path):
    with pytest.raises(ValueError, match="timezone-aware"):
        AuditLedger(tmp_path / "x").append(
            "event", {}, recorded_at=dt.datetime(2026, 9, 2, 15, 30))
