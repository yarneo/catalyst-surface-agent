import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.weekly_research import _digest
from trading_bot.portfolio_policy import DEFAULT_CONFIG
from trading_bot.research_autopilot import (
    advance_rollback,
    advance_shadow,
    recover_deployment,
    rollback_metrics,
    shadow_metrics,
)


def _weeks(values, start="2026-01-02"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="W-FRI"))


def test_forward_shadow_requires_eight_cost_adjusted_weeks():
    observations = [{
        "week": f"2026-0{i + 1}-01", "candidate_return": .011,
        "champion_return": .01, "excess_return": .001,
        "forecast_probability": .81, "costs_included": True,
    } for i in range(8)]
    result = shadow_metrics(observations)
    assert result["eligible"] is True
    assert result["weeks"] == 8
    assert result["cumulative_excess_return"] > 0


def test_shadow_starts_without_backfilling_history_then_adds_next_week():
    state = {"schema_version": 1, "shadow": None, "deployment": None, "history": []}
    candidate = {"name": "MA150", "config": {**DEFAULT_CONFIG, "ma_window": 150}}
    champion_returns = _weeks([.01, .01])
    candidate_returns = _weeks([.011, .012])
    state, event = advance_shadow(
        state, candidate=candidate, candidate_returns=candidate_returns,
        champion_returns=champion_returns, observed_at="2026-01-11T00:00:00Z")
    assert event["action"] == "SHADOW_STARTED"
    assert state["shadow"]["observations"] == []

    state, event = advance_shadow(
        state, candidate=candidate,
        candidate_returns=_weeks([.011, .012, .013]),
        champion_returns=_weeks([.01, .01, .01]),
        observed_at="2026-01-18T00:00:00Z")
    assert event["action"] == "SHADOW_PROGRESS"
    assert len(state["shadow"]["observations"]) == 1
    assert state["shadow"]["observations"][0]["costs_included"] is True


def test_incomplete_week_is_not_counted_as_forward_evidence():
    state = {"schema_version": 1, "shadow": None, "deployment": None, "history": []}
    candidate = {"name": "MA150", "config": {**DEFAULT_CONFIG, "ma_window": 150}}
    friday = pd.Timestamp("2026-01-02")
    state, _ = advance_shadow(
        state, candidate=candidate,
        candidate_returns=pd.Series([.01], index=[friday]),
        champion_returns=pd.Series([0], index=[friday]),
        observed_at="2026-01-04T00:00:00Z")
    # Monday data belongs to the still-open Friday period and must not advance it.
    monday = pd.Timestamp("2026-01-05")
    state, event = advance_shadow(
        state, candidate=candidate,
        candidate_returns=pd.Series([.01, .02], index=[friday, monday]),
        champion_returns=pd.Series([0, 0], index=[friday, monday]),
        observed_at="2026-01-05T23:00:00Z")
    assert event["action"] == "SHADOW_PROGRESS"
    assert state["shadow"]["observations"] == []


def test_rollback_fires_after_four_week_two_percent_underperformance():
    observations = [{
        "week": str(i), "candidate_return": -.01, "champion_return": 0,
        "excess_return": -.01, "costs_included": True,
    } for i in range(4)]
    result = rollback_metrics(observations)
    assert result["rollback"] is True
    deployment = {
        "status": "DEPLOYED", "name": "candidate",
        "last_observed_week": "2026-01-23", "observations": observations[:3],
    }
    deployment, event = advance_rollback(
        deployment, candidate_returns=_weeks([-.01] * 5),
        baseline_returns=_weeks([0] * 5), observed_at="2026-02-01T00:00:00Z")
    assert event["action"] == "ROLLBACK"


def test_promoted_policy_recovers_missing_rollback_monitor():
    state = {"schema_version": 1, "shadow": None, "deployment": None, "history": []}
    promoted = {**DEFAULT_CONFIG, "ma_window": 150}
    policy = {
        "status": "AUTO_PROMOTED_PAPER", "updated_at": "2026-01-04T00:00:00Z",
        "config": promoted, "previous_config": DEFAULT_CONFIG,
        "promotion_evidence": {"candidate": "MA150"},
    }
    state, event = recover_deployment(
        state, policy=policy, candidate_returns=_weeks([.011, .012]),
        baseline_returns=_weeks([.01, .01]), observed_at="2026-01-05T00:00:00Z")
    assert event == {"action": "DEPLOYMENT_RECOVERED", "candidate": "MA150"}
    assert state["deployment"]["baseline_config"] == DEFAULT_CONFIG
    assert state["deployment"]["last_observed_week"] == "2026-01-09"


def test_telegram_digest_never_requests_a_reply_or_approval():
    record = {
        "ts": "2026-09-07T12:00:00Z", "n_survivors": 1, "n_reviews": 0,
        "n_evaluated": 12,
        "champion": {"metrics": {
            "full": {"sharpe": 1.2, "cagr": .2, "dd": -.1},
            "recent": {"sharpe": 1.1}, "bear": {"sharpe": .5}},
            "rolling": None},
        "survivors": [{"name": "MA150", "metrics": {
            "full": {"sharpe": 1.3, "dd": -.09},
            "recent": {"sharpe": 1.2}}, "rolling": None}],
        "reviews": [],
        "autopilot": [{"action": "SHADOW_STARTED", "candidate": "MA150",
                       "minimum_weeks": 8}],
    }
    message = _digest(record).lower()
    assert "no response is required" in message
    assert "reply" not in message
    assert "to approve" not in message
    assert "approval" not in message
