from trading_bot.allocations import (
    DEFAULT_WEIGHTS,
    LIVE_SLEEVE_TARGETS,
    effective_target,
    resolve_allocations,
)
from trading_bot.ownership import build_sleeves


def base():
    return {
        "weights": {"qqq": 0.1, "regime": 0.3333, "crypto": 0.3333, "pead": 0.0},
        "verdicts": {"regime": "DECAYING", "crypto": "HEALTHY", "pead": "HEALTHY"},
    }


def test_tripped_revalidation_overrides_healthy_policy_and_zeroes_weight():
    resolved = resolve_allocations(
        base(),
        revalidation={"crypto": {"verdict": "DECAYING", "tripwire": "TRIPPED — 14 weeks"}},
    )
    assert resolved["verdicts"]["crypto"] == "TRIPPED"
    assert resolved["weights"]["crypto"] == 0.0
    assert resolved["policy_weights"]["crypto"] == 0.3333
    assert resolved["governance"]["status"] == "DEGRADED"


def test_validation_error_is_fail_closed():
    resolved = resolve_allocations(base(), revalidation={"pead": {"error": "bad index"}})
    assert resolved["verdicts"]["pead"] == "ERROR"
    assert resolved["weights"]["pead"] == 0.0


def test_disabled_deployment_is_the_final_authority():
    resolved = resolve_allocations(
        base(), revalidation={"crypto": {"verdict": "HEALTHY"}},
        enabled_sleeves={"regime": True, "crypto": False, "pead": False,
                         "managed_futures": True},
    )
    assert resolved["verdicts"]["crypto"] == "DISABLED"
    assert resolved["weights"]["crypto"] == 0.0
    assert resolved["governance"]["status"] == "OK"
    assert resolved["governance"]["overrides"]


def test_qqq_enablement_follows_the_idle_router():
    resolved = resolve_allocations(
        base(), enabled_sleeves={"regime": True, "crypto": False, "pead": False,
                                 "managed_futures": True},
    )
    assert resolved["verdicts"]["qqq"] == "HEALTHY"
    assert resolved["weights"]["qqq"] == 0.1


def test_production_regime_target_has_one_explicit_source():
    assert LIVE_SLEEVE_TARGETS["regime"] == 0.70
    assert DEFAULT_WEIGHTS == LIVE_SLEEVE_TARGETS
    regime = next(s for s in build_sleeves() if s.name == "regime")
    assert regime.max_sleeve_weight == LIVE_SLEEVE_TARGETS["regime"]


def test_decaying_is_warning_only_until_the_documented_tripwire():
    assert effective_target(0.70, 0.70, "DECAYING") == 0.70
    assert effective_target(0.70, 0.70, "TRIPPED") == 0.0
