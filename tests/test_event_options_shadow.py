import datetime as dt

from trading_bot.event_options_shadow import shadow_snapshot
from trading_bot.options.book import Book, BookEntry
from trading_bot.tournament.audit import AuditLedger
from trading_bot.tournament.weekly_plan import atomic_write_plan


def test_snapshot_reports_sealed_plan_virtual_pnl_and_verified_evidence(tmp_path):
    atomic_write_plan(tmp_path / "weekly_plan.json", {
        "mode": "AUTONOMOUS_WEEKLY_RESEARCH_PLAN",
        "deployment_profile": "event_options_shadow",
        "order_enabled": False,
        "generated_at": "2026-09-06T13:15:00-04:00",
        "window": {"start": "2026-09-07T09:30:00-04:00",
                   "deadline": "2026-09-11T15:30:00-04:00"},
        "universe_size": 64,
        "calendar": {
            "provider_fact_counts": {"yahoo": 1, "nasdaq": 1},
            "source_warnings": [], "source_errors": [],
            "discovered_fact_count": 2,
            "enriched_schedule_fact_count": 0,
        },
        "portfolio": {"aggregate_risk_pct": .05,
                      "per_event_risk_pct": .02,
                      "singleton_risk_pct": .02},
        "events": [{"promotion": {"promoted": True},
                    "calendar": {"confirmed": True, "reasons": ["confirmed"]},
                    "semantic": {"status": "upcoming"},
                    "news_fact_count": 2,
                    "replay": {"summary": {"sample_size": 8}}}],
    })
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans" / "one.json").write_text("{}")
    book = Book(tmp_path / "virtual_book.json")
    book.add(BookEntry(
        id="x", underlying="XYZ", structure="long_straddle", legs=[], qty=1,
        entry=10.0, max_profit=20.0, max_loss=10.0,
        opened_at="2026-09-07T15:30:00-04:00"))
    book.close("x", exit_price=-12.0, reason="scheduled",
               when="2026-09-08T09:45:00-04:00")
    stamp = dt.datetime(2026, 9, 8, 9, 45, tzinfo=dt.timezone.utc)
    AuditLedger(tmp_path / "evidence.jsonl").append(
        "weekly_shadow_exit", {"event_id": "x"}, recorded_at=stamp)

    value = shadow_snapshot(tmp_path)
    assert value["status"] == "MONITORING"
    assert value["order_authority"] is False
    assert value["plan"]["prequalified"] == 1
    assert value["plan"]["candidate_details"] == [{
        "symbol": None, "promoted": True, "reasons": [],
        "blocked_stage": "PREQUALIFIED", "calendar_confirmed": True,
        "calendar_reasons": ["confirmed"], "news_fact_count": 2,
        "news_error": None, "replay_sample_size": 8, "schedule": None}]
    assert value["plan"]["calendar_health"] == {
        "provider_fact_counts": {"yahoo": 1, "nasdaq": 1},
        "source_warnings": [], "source_errors": [],
        "discovered_fact_count": 2,
        "enriched_schedule_fact_count": 0,
    }
    assert value["plan_archive_count"] == 1
    assert value["realized_pnl"] == 200.0
    assert value["evidence_chain"] == "VERIFIED"
    assert value["execution_quality"]["status"] == "COLLECTING"
    assert value["promotion_evidence"]["eligible_for_review"] is False
    assert value["surface_quality"]["status"] == "AWAITING_SURFACE_GRID"
