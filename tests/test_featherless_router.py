from trading_bot.tournament.catalyst import CatalystFact
from trading_bot.tournament.featherless import FeatherlessClient


FACT = CatalystFact("news:1", "2026-08-28T15:00:00Z", "AMD raises guidance",
                    "Revenue guidance increased.", ("AMD",))


def slow_process_worker(out, model, api_key, payload, timeout_s):
    """Top-level so the production-like spawn context can import it."""
    import time
    time.sleep(2.0)


def output(direction="bullish", confidence=0.8):
    return {
        "catalyst_type": "guidance",
        "factual_summary": "AMD raised guidance.",
        "novelty": 0.8,
        "surprise": 0.7,
        "direction": direction,
        "expected_half_life_minutes": 180,
        "primary_tickers": ["AMD"],
        "secondary_tickers": [],
        "causal_links": [],
        "confidence": confidence,
        "invalidation": "The move reverses below VWAP.",
        "source_fact_ids": ["news:1"],
    }


def response(value):
    import json
    return {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(value)}}], "usage": {"total_tokens": 10}}


def test_two_valid_models_form_a_direction_quorum():
    def transport(model, payload, timeout):
        return response(output(confidence={"a": 0.9, "b": 0.7, "c": 0.8}[model]))

    client = FeatherlessClient("key", models=("a", "b", "c"), transport=transport)
    result = client.analyze([FACT], eligible_tickers=["AMD", "NVDA"])
    assert result.valid
    assert result.agreement == 1.0
    # The skeptical agreeing assessment is preserved rather than averaged away.
    assert result.accepted.confidence == 0.7


def test_one_failed_model_does_not_destroy_a_two_model_quorum():
    def transport(model, payload, timeout):
        if model == "c":
            return {"error": "capacity"}
        return response(output())

    result = FeatherlessClient(
        "key", models=("a", "b", "c"), transport=transport
    ).analyze([FACT], eligible_tickers=["AMD"])
    assert result.valid
    assert result.agreement == 1.0
    assert result.attempts[2].error


def test_one_valid_output_is_not_a_quorum():
    def transport(model, payload, timeout):
        if model == "a":
            return response(output())
        return {"choices": [{"message": {"content": ""}}]}

    result = FeatherlessClient(
        "key", models=("a", "b", "c"), transport=transport
    ).analyze([FACT], eligible_tickers=["AMD"])
    assert not result.valid
    assert "quorum unavailable" in result.reason


def test_direction_disagreement_is_uncertainty_not_a_coin_flip():
    directions = {"a": "bullish", "b": "bearish", "c": "unknown"}

    def transport(model, payload, timeout):
        return response(output(direction=directions[model]))

    result = FeatherlessClient(
        "key", models=("a", "b", "c"), transport=transport
    ).analyze([FACT], eligible_tickers=["AMD"])
    assert not result.valid
    assert "no actionable direction quorum" in result.reason


def test_direction_neutral_quorum_can_validate_scheduled_event_integrity():
    def transport(model, payload, timeout):
        return response(output(direction="unknown"))

    client = FeatherlessClient("key", models=("a", "b"), transport=transport)
    directional = client.analyze([FACT], eligible_tickers=["AMD"])
    integrity = client.analyze(
        [FACT], eligible_tickers=["AMD"], require_actionable_direction=False)
    assert not directional.valid
    assert integrity.valid and integrity.accepted.direction == "unknown"


def test_reasoning_without_final_content_is_recorded_as_failure():
    def transport(model, payload, timeout):
        if model == "c":
            return {"choices": [{"message": {"content": "", "reasoning": "x" * 40}}]}
        return response(output())

    result = FeatherlessClient(
        "key", models=("a", "b", "c"), transport=transport
    ).analyze([FACT], eligible_tickers=["AMD"])
    assert result.valid
    assert "reasoning chars" in result.attempts[2].error


def test_invalid_schema_is_recorded_and_never_accepted():
    def transport(model, payload, timeout):
        bad = output()
        bad["qty"] = 1000
        return response(bad)

    result = FeatherlessClient(
        "key", models=("a", "b"), transport=transport
    ).analyze([FACT], eligible_tickers=["AMD"])
    assert not result.valid
    assert all(attempt.error for attempt in result.attempts)


def test_non_expansive_self_link_repair_is_visible_in_the_audit():
    def transport(model, payload, timeout):
        value = output()
        value["causal_links"] = [{
            "source_ticker": "AMD", "target_ticker": "AMD",
            "direction": "bullish", "mechanism": "same company",
            "confidence": 0.9, "source_fact_ids": ["news:1"],
        }]
        return response(value)

    result = FeatherlessClient(
        "key", models=("a", "b"), transport=transport
    ).analyze([FACT], eligible_tickers=["AMD"])
    assert result.valid
    assert all("removed causal self-link" in attempt.repairs
               for attempt in result.attempts)


def test_repair_never_whitelists_an_invented_ticker():
    def transport(model, payload, timeout):
        value = output()
        value["secondary_tickers"] = ["FAKE"]
        value["causal_links"] = [{
            "source_ticker": "AMD", "target_ticker": "FAKE",
            "direction": "bullish", "mechanism": "invented",
            "confidence": 0.9, "source_fact_ids": ["news:1"],
        }]
        return response(value)

    result = FeatherlessClient(
        "key", models=("a", "b"), transport=transport
    ).analyze([FACT], eligible_tickers=["AMD"])
    assert not result.valid


def test_real_transport_path_enforces_a_hard_wall_clock(monkeypatch):
    """A requests timeout is per socket operation, not a total request budget.
    The live probe exceeded timeout=60 by 37.6 seconds; production workers must
    be killable rather than trusted to return cooperatively."""
    import time
    started = time.monotonic()
    result = FeatherlessClient(
        "key", models=("a", "b"), timeout_s=0.10,
        process_worker=slow_process_worker, process_start_method="spawn",
    ).analyze([FACT], eligible_tickers=["AMD"])
    assert time.monotonic() - started < 1.50
    assert not result.valid
    assert all("hard timeout" in attempt.error for attempt in result.attempts)
