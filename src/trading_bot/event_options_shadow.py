"""Read-only reporting for the isolated event-options shadow sleeve."""

from __future__ import annotations

from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .options.book import Book
from .strategy_evidence import (
    execution_quality,
    forecasts_from_audit,
    observations_from_audit,
    promotion_scorecard,
)
from .tournament.audit import AuditLedger
from .tournament.weekly_plan import read_plan


def _closed_pnl(entry) -> float:
    if entry.exit is None:
        return 0.0
    return -(entry.exit + entry.entry) * 100.0 * entry.qty


def shadow_snapshot(data_dir: str | Path) -> dict[str, Any]:
    """Summarize sealed decisions and virtual fills without broker access."""
    root = Path(data_dir)
    result: dict[str, Any] = {
        "status": "AWAITING_FIRST_PLAN",
        "order_authority": False,
        "capital_at_risk": 0.0,
        "plan": None,
        "open_positions": [],
        "closed_positions": [],
        "realized_pnl": 0.0,
        "evidence_rows": 0,
        "evidence_chain": "EMPTY",
        "latest_evidence": None,
        "plan_archive_count": 0,
        "execution_quality": execution_quality([]),
        "promotion_evidence": promotion_scorecard(
            [], evidence_chain_verified=False),
        "surface_quality": {
            "status": "AWAITING_SURFACE_GRID",
            "crossable_violations": 0,
            "method": "bid/ask static-arbitrage bounds; diagnostic only",
        },
    }
    plan_path = root / "weekly_plan.json"
    try:
        result["plan_archive_count"] = sum(
            1 for path in (root / "plans").glob("*.json") if path.is_file())
    except OSError:
        pass
    if plan_path.exists():
        try:
            plan = read_plan(plan_path)
            events = plan.get("events", [])
            calendar_health = plan.get("calendar") or {}
            candidate_details = []
            for row in events:
                promotion = row.get("promotion") or {}
                calendar = row.get("calendar") or {}
                semantic = row.get("semantic") or {}
                if promotion.get("promoted"):
                    stage = "PREQUALIFIED"
                elif not calendar.get("confirmed"):
                    stage = "CALENDAR"
                elif semantic.get("status") != "upcoming":
                    stage = "SEMANTIC"
                elif not row.get("schedule"):
                    stage = "SCHEDULE"
                elif not row.get("replay"):
                    stage = "REPLAY"
                else:
                    stage = "ECONOMIC_GATES"
                candidate_details.append({
                    "symbol": row.get("symbol"),
                    "promoted": bool(promotion.get("promoted")),
                    "reasons": list(promotion.get("reasons") or []),
                    "blocked_stage": stage,
                    "calendar_confirmed": bool(calendar.get("confirmed")),
                    "calendar_reasons": list(calendar.get("reasons") or []),
                    "news_fact_count": int(row.get("news_fact_count") or 0),
                    "news_error": row.get("news_error"),
                    "replay_sample_size": (
                        (((row.get("replay") or {}).get("summary") or {}).get(
                            "sample_size"))),
                    "schedule": row.get("schedule"),
                })
            result["plan"] = {
                "generated_at": plan.get("generated_at"),
                "window": plan.get("window"),
                "universe_size": plan.get("universe_size", 0),
                "candidates": len(events),
                "prequalified": sum(bool((row.get("promotion") or {}).get("promoted"))
                                    for row in events),
                "candidate_details": candidate_details,
                "risk": plan.get("portfolio", {}),
                "profile": plan.get("deployment_profile"),
                "calendar_health": {
                    "provider_fact_counts": dict(
                        calendar_health.get("provider_fact_counts") or {}),
                    "source_warnings": list(
                        calendar_health.get("source_warnings") or []),
                    "source_errors": list(
                        calendar_health.get("source_errors") or []),
                    "discovered_fact_count": int(
                        calendar_health.get("discovered_fact_count") or 0),
                    "enriched_schedule_fact_count": int(
                        calendar_health.get("enriched_schedule_fact_count") or 0),
                },
            }
            result["status"] = "MONITORING"
        except Exception as exc:
            result["status"] = "PLAN_ERROR"
            result["error"] = f"{type(exc).__name__}: {exc}"

    try:
        book = Book(root / "virtual_book.json")
        for entry in book.entries:
            row = {
                "symbol": entry.underlying, "structure": entry.structure,
                "qty": entry.qty, "entry_price": entry.entry,
                "max_loss_usd": entry.max_loss * 100.0 * entry.qty,
                "opened_at": entry.opened_at, "closed_at": entry.closed_at,
                "exit_price": entry.exit, "close_reason": entry.close_reason,
            }
            if entry.is_open:
                result["open_positions"].append(row)
                result["capital_at_risk"] += row["max_loss_usd"]
            else:
                row["pnl"] = _closed_pnl(entry)
                result["closed_positions"].append(row)
        result["realized_pnl"] = book.realised_pnl()
        if result["open_positions"]:
            result["status"] = "VIRTUAL_POSITION_OPEN"
    except Exception as exc:
        result["status"] = "BOOK_ERROR"
        result["error"] = f"{type(exc).__name__}: {exc}"

    try:
        rows = AuditLedger(root / "evidence.jsonl").read()
        result["evidence_rows"] = len(rows)
        result["evidence_chain"] = "VERIFIED" if rows else "EMPTY"
        if rows:
            latest = rows[-1]
            result["latest_evidence"] = {
                "sequence": latest.sequence,
                "recorded_at": latest.recorded_at,
                "event_type": latest.event_type,
            }
        # A handled quote error is visible and recoverable, not a silent
        # operational failure. Only an unresolved broker state blocks the
        # promotion evidence gate here.
        failures = sum(
            row.event_type.endswith("_result")
            and isinstance(row.payload.get("fill"), dict)
            and row.payload["fill"].get("how") == "stuck"
            for row in rows)
        result["execution_quality"] = execution_quality(
            observations_from_audit(rows))
        result["promotion_evidence"] = promotion_scorecard(
            forecasts_from_audit(rows),
            evidence_chain_verified=bool(rows), silent_failures=failures)
        latest_surface = next((
            row.payload.get("surface_quality") for row in reversed(rows)
            if isinstance(row.payload.get("surface_quality"), dict)
        ), None)
        if latest_surface is not None:
            result["surface_quality"] = latest_surface
        unique = {}
        for row in rows:
            payload = row.payload or {}
            identity = payload.get("event_id")
            if identity and ("reject" in row.event_type or "skip" in row.event_type or "error" in row.event_type):
                unique[str(identity)] = payload
        failures_by_kind = Counter()
        for payload in unique.values():
            explanation = str(payload.get("reason") or payload.get("reasons") or payload.get("error") or "").lower()
            kind = "data_or_infrastructure" if any(term in explanation for term in
                ("stale", "quote", "timeout", "calendar", "unavailable", "missing", "error")) else "strategy_or_schedule"
            failures_by_kind[kind] += 1
        stages = Counter(r["blocked_stage"] for r in (result.get("plan") or {}).get("candidate_details", []))
        result["rejection_funnel"] = {
            "current_plan_stages": dict(stages), "unique_rejected_events": len(unique),
            "rejection_classes": dict(failures_by_kind),
            "valid_entries": len(observations_from_audit(rows)),
            "completed_outcomes": result["promotion_evidence"]["independent_outcomes"],
            "unpriced_rejections_have_pnl": False,
            "interpretation": "Missing data is not an economic rejection. No executable quotes means no valid hypothetical P&L.",
        }
        stamp = (result.get("plan") or {}).get("generated_at")
        if stamp:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
            result["plan_freshness"] = {"age_hours": round(age/3600,1), "status": "STALE" if age > 8*86400 or age < -300 else "CURRENT"}
    except Exception as exc:
        result["status"] = "EVIDENCE_ERROR"
        result["evidence_chain"] = "INVALID"
        result["error"] = f"{type(exc).__name__}: {exc}"

    result["capital_at_risk"] = round(result["capital_at_risk"], 2)
    result["realized_pnl"] = round(result["realized_pnl"], 2)
    return result
