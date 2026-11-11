import json

import pytest

from trading_bot.portfolio_policy import (
    DEFAULT_CONFIG,
    load_policy,
    validate_config,
    write_policy,
)


def test_policy_defaults_when_file_is_missing(tmp_path):
    result = load_policy(tmp_path / "missing.json")
    assert result["status"] == "DEFAULT"
    assert result["config"] == DEFAULT_CONFIG


def test_policy_rejects_research_only_configuration():
    with pytest.raises(ValueError, match="no production executor"):
        validate_config({**DEFAULT_CONFIG, "idle_rule": "qld_bull"})
    with pytest.raises(ValueError, match="slow_regime_share"):
        validate_config({**DEFAULT_CONFIG, "slow_regime_share": .3})


def test_policy_write_is_readable_and_keeps_rollback_config(tmp_path):
    path = tmp_path / "policy.json"
    promoted = {**DEFAULT_CONFIG, "ma_window": 150}
    value = write_policy(
        promoted, previous_config=DEFAULT_CONFIG,
        status="AUTO_PROMOTED_PAPER", source="test",
        evidence={"production_parity_verified": True, "frozen_cohort": "test",
                  "forward_sessions": 60, "weekly_blocks": 12,
                  "cost_adjusted_lower_bound": .001}, path=path)
    assert value["config"]["ma_window"] == 150
    assert load_policy(path)["previous_config"] == DEFAULT_CONFIG
    assert json.loads(path.read_text())["promotion_evidence"]["forward_sessions"] == 60


def test_legacy_backtest_cannot_promote_even_if_old_week_gate_passes(tmp_path):
    with pytest.raises(ValueError, match="production parity"):
        write_policy(DEFAULT_CONFIG, previous_config=None, status="AUTO_PROMOTED_PAPER",
                     source="legacy", evidence={"weeks": 100}, path=tmp_path / "policy.json")


def test_corrupt_policy_falls_back_without_risk_change(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text("not json")
    result = load_policy(path)
    assert result["status"] == "INVALID_FALLBACK"
    assert result["config"] == DEFAULT_CONFIG
