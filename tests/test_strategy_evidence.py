import datetime as dt

import pytest

from trading_bot.strategy_evidence import (
    brier_score,
    execution_quality,
    expected_calibration_error,
    forecasts_from_audit,
    observations_from_audit,
    promotion_scorecard,
)
from trading_bot.tournament.audit import AuditLedger


def test_execution_capture_is_shrunk_and_bucketed():
    result = execution_quality([
        {"quoted_mid": 10.0, "fill_debit": 10.5, "bucket": "XYZ|2d|close"},
        {"quoted_mid": 10.0, "fill_debit": 10.0, "bucket": "XYZ|2d|close"},
    ], prior_capture=.90, prior_weight=2)
    assert result["observations"] == 2
    assert result["raw_capture"] == pytest.approx((10 / 10.5 + 1) / 2)
    assert result["shrunk_capture"] < result["raw_capture"]
    assert result["by_bucket"]["XYZ|2d|close"]["observations"] == 2


def test_audit_pairing_builds_execution_and_forecast_observations(tmp_path):
    ledger = AuditLedger(tmp_path / "evidence.jsonl")
    stamp = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
    ledger.append("weekly_shadow_entry", {
        "event_id": "xyz", "quoted_mid": 9.5, "quoted_executable": 10.0,
        "execution_bucket": "XYZ|2d|close", "forecast_probability": .7,
    }, recorded_at=stamp)
    ledger.append("weekly_shadow_exit", {
        "event_id": "xyz", "realized_pnl": 250,
    }, recorded_at=stamp + dt.timedelta(days=1))
    rows = ledger.read()
    assert observations_from_audit(rows) == [{
        "event_id": "xyz", "quoted_mid": 9.5, "fill_debit": 10.0,
        "bucket": "XYZ|2d|close",
    }]
    assert forecasts_from_audit(rows) == [{
        "event_id": "xyz", "probability": .7, "net_pnl": 250.0,
    }]


def test_promotion_requires_calibration_expectancy_and_enough_outcomes():
    forecasts = [
        {"event_id": str(i), "probability": .95 if i % 2 else .05,
         "net_pnl": 100 if i % 2 else -50}
        for i in range(30)
    ]
    result = promotion_scorecard(forecasts, evidence_chain_verified=True)
    assert result["eligible_for_review"] is True
    assert result["automatic_promotion"] is False
    assert result["brier_skill_score"] > 0
    assert result["net_expectancy_usd"] == 25

    too_small = promotion_scorecard(
        forecasts[:3], evidence_chain_verified=True)
    assert too_small["eligible_for_review"] is False
    assert "3 of 30" in too_small["reasons"][0]


def test_calibration_metrics_are_zero_for_perfect_forecasts():
    assert brier_score([0, 1], [False, True]) == 0
    assert expected_calibration_error([0, 1], [False, True]) == 0
