import json

import pytest

from trading_bot.shadow_regime import (
    propose_shadow_target,
    propose_shadow_targets,
    record_shadow_decision,
    record_shadow_portfolios,
)


def test_decaying_shadow_is_stricter_than_the_live_target():
    decision = propose_shadow_target(
        live_allocation=0.35, vol_scale=1.0, realized_vol=0.20,
        regime_on=True, health_verdict="DECAYING",
    )
    assert decision.live_target_weight == pytest.approx(0.35)
    assert decision.shadow_target_weight == pytest.approx(0.125)
    assert decision.limiting_reason == "decaying_throttle"


def test_volatility_budget_can_bind_before_the_capital_cap():
    decision = propose_shadow_target(
        live_allocation=0.35, vol_scale=1.0, realized_vol=0.40,
        regime_on=True, health_verdict="HEALTHY",
    )
    assert decision.shadow_target_weight == pytest.approx(0.15)
    assert decision.limiting_reason == "volatility_contribution_cap"


def test_shadow_is_flat_when_regime_is_off():
    decision = propose_shadow_target(
        live_allocation=0.35, vol_scale=1.0, realized_vol=0.20,
        regime_on=False, health_verdict="HEALTHY",
    )
    assert decision.shadow_target_weight == 0.0


def test_shadow_recorder_writes_auditable_jsonl(tmp_path):
    decision = propose_shadow_target(
        live_allocation=0.35, vol_scale=1.0, realized_vol=0.20,
        regime_on=True, health_verdict="DECAYING",
    )
    path = tmp_path / "shadow.jsonl"
    record_shadow_decision(decision, equity=100_000, realized_vol=0.20,
                           regime_on=True, health_verdict="DECAYING", path=path)
    row = json.loads(path.read_text())
    assert row["symbol"] == "QLD"
    assert row["shadow_target_usd"] == 12_500


def test_shadow_portfolios_mark_previous_weight_and_cost_turnover(tmp_path):
    ledger = tmp_path / "shadow.jsonl"
    state = tmp_path / "state.json"
    targets = {"production_70": 0.70, "cap_50": 0.50}
    first = record_shadow_portfolios(
        targets, price=100, observed_equity=99_000, realized_vol=0.30,
        regime_on=True, health_verdict="HEALTHY", session_date="2026-09-10",
        path=ledger, state_path=state,
    )
    assert first["variants"]["production_70"]["equity"] == pytest.approx(99_965.0)
    second = record_shadow_portfolios(
        targets, price=110, observed_equity=100_000, realized_vol=0.30,
        regime_on=True, health_verdict="HEALTHY", session_date="2026-09-11",
        path=ledger, state_path=state,
    )
    assert second["variants"]["production_70"]["daily_pnl"] == pytest.approx(6997.55, abs=0.02)
    assert second["variants"]["cap_50"]["daily_pnl"] == pytest.approx(4998.75, abs=0.02)


def test_shadow_portfolio_is_idempotent_within_session(tmp_path):
    ledger = tmp_path / "shadow.jsonl"
    state = tmp_path / "state.json"
    kwargs = dict(price=100, observed_equity=100_000, realized_vol=0.3,
                  regime_on=True, health_verdict="HEALTHY",
                  session_date="2026-09-10", path=ledger, state_path=state)
    record_shadow_portfolios({"cap_50": 0.5}, **kwargs)
    record_shadow_portfolios({"cap_50": 0.4}, **kwargs)
    assert len(ledger.read_text().splitlines()) == 1


def test_shadow_variants_include_requested_risk_budgets():
    targets = propose_shadow_targets(
        live_allocation=0.70, vol_scale=1.0, realized_vol=0.40,
        regime_on=True, health_verdict="HEALTHY",
    )
    assert targets["production_70"] == pytest.approx(0.70)
    assert targets["cap_60"] == pytest.approx(0.60)
    assert targets["cap_50"] == pytest.approx(0.50)
    assert targets["stress_70"] == pytest.approx(0.50)


def test_decaying_warning_does_not_fake_a_production_allocation_cut():
    targets = propose_shadow_targets(
        live_allocation=0.70, vol_scale=1.0, realized_vol=0.25,
        regime_on=True, health_verdict="DECAYING",
    )
    assert targets["production_70"] == pytest.approx(0.70)
    assert targets["cap_60"] == pytest.approx(0.60)
    assert targets["risk_cap_25"] < 0.25  # legacy challenger remains stricter
