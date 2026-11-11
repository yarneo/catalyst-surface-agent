import pandas as pd
import pytest

from trading_bot.attribution import (
    ai_authority_gate,
    component_risk_contributions,
    portfolio_tail_risk,
    summarize_execution_quality,
    summarize_ai_decisions,
)


def test_ai_counterfactual_rewards_dampening_before_a_drop():
    rows = [
        {"ts": "2026-09-01T13:35:00Z", "symbol": "IWM", "price": 100,
         "target_qty_with_ai": 5, "target_qty_without_ai": 10},
        {"ts": "2026-09-02T13:35:00Z", "symbol": "IWM", "price": 90,
         "target_qty_with_ai": 8, "target_qty_without_ai": 8},
    ]
    result = summarize_ai_decisions(rows)
    assert result["altered_decisions"] == 1
    assert result["marked_intervals"] == 1
    assert result["costs_included"] is True
    assert result["estimated_value_add"] == pytest.approx(50.21, abs=0.01)


def test_ai_authority_requires_enough_positive_cost_adjusted_evidence():
    blocked = ai_authority_gate({
        "altered_decisions": 3, "marked_intervals": 10,
        "estimated_value_add": 50, "costs_included": True,
    })
    assert blocked["eligible"] is False
    ready = ai_authority_gate({
        "altered_decisions": 30, "marked_intervals": 31,
        "estimated_value_add": 1, "costs_included": True,
    })
    assert ready["eligible"] is False  # repeated targets alone cannot grant order authority
    ready = ai_authority_gate({
        "altered_decisions": 30, "marked_intervals": 31,
        "estimated_value_add": 10, "costs_included": True,
        "evidence_version": "ai-v2", "cohort_count": 1,
        "independent_changed_sessions": 30, "value_add_lower_bound": 1,
    })
    assert ready["eligible"] is True


def test_execution_quality_reports_signed_slippage_and_sleeves():
    intents = [
        {"order_id": "a", "symbol": "QLD", "side": "buy", "qty": 10,
         "reference_price": 100, "sleeve": "regime"},
        {"order_id": "b", "symbol": "BIL", "side": "sell", "qty": 5,
         "reference_price": 90, "sleeve": "cash_park"},
    ]
    fills = [
        {"order_id": "a", "filled_avg_price": 100.10, "filled_qty": 10},
        {"order_id": "b", "filled_avg_price": 90.05, "filled_qty": 5},
    ]
    result = summarize_execution_quality(intents, fills)
    assert result["measured_orders"] == 2
    assert result["estimated_slippage_usd"] == pytest.approx(0.75)
    assert result["by_sleeve"]["regime"]["slippage_bps"] == pytest.approx(10.0)


def test_component_risk_contributions_sum_to_one():
    returns = pd.DataFrame({
        "A": [0.01, -0.01, 0.02, -0.02],
        "B": [0.005, -0.004, 0.006, -0.005],
    })
    result = component_risk_contributions({"A": 0.5, "B": 0.5}, returns)
    assert sum(result.values()) == pytest.approx(1.0)
    assert result["A"] > result["B"]


def test_portfolio_tail_risk_reports_var_and_expected_shortfall_in_dollars():
    returns = pd.DataFrame({
        "A": [-0.10, -0.04, -0.02, 0.00, 0.01, 0.02],
        "B": [-0.02, -0.01, 0.00, 0.01, 0.01, 0.02],
    })
    result = portfolio_tail_risk(
        {"A": 0.6, "B": 0.2}, returns, equity=100_000,
        confidence=0.80, minimum_observations=5)
    assert result["status"] == "MEASURING"
    assert result["gross_market_exposure"] == pytest.approx(0.8)
    assert result["var_usd"] > 0
    assert result["expected_shortfall_usd"] >= result["var_usd"]
    assert result["worst_observed_usd"] == pytest.approx(6400)


def test_portfolio_tail_risk_refuses_tiny_sample():
    result = portfolio_tail_risk(
        {"A": 1.0}, pd.DataFrame({"A": [0.01, -0.01]}),
        equity=100_000, minimum_observations=20)
    assert result["status"] == "BUILDING"
    assert result["observations"] == 2
