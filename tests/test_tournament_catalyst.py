import pytest

from trading_bot.tournament.catalyst import (
    CatalystAssessment,
    CatalystFact,
    CatalystValidationError,
)


FACT = CatalystFact(
    fact_id="news:1",
    published_at="2026-08-28T15:51:56Z",
    headline="Chip designer raises guidance after a new customer contract",
    summary="The company raised revenue guidance and named a new cloud customer.",
    symbols=("AMD",),
)


def raw(**changes):
    base = {
        "catalyst_type": "guidance_and_contract",
        "factual_summary": "AMD raised guidance after a cloud contract.",
        "novelty": 0.90,
        "surprise": 0.80,
        "direction": "bullish",
        "expected_half_life_minutes": 240,
        "primary_tickers": ["AMD"],
        "secondary_tickers": ["NVDA"],
        "causal_links": [{
            "source_ticker": "AMD",
            "target_ticker": "NVDA",
            "direction": "bullish",
            "mechanism": "The cited cloud demand may apply to the close peer.",
            "confidence": 0.65,
            "source_fact_ids": ["news:1"],
        }],
        "confidence": 0.82,
        "invalidation": "AMD loses the post-news VWAP on abnormal volume.",
        "source_fact_ids": ["news:1"],
    }
    base.update(changes)
    return base


def assess(value=None):
    return CatalystAssessment.from_model(
        value or raw(), facts=[FACT], allowed_tickers=["AMD", "NVDA", "QQQ"])


def test_valid_grounded_assessment_crosses_the_boundary():
    got = assess()
    assert got.direction == "bullish"
    assert got.primary_tickers == ("AMD",)
    assert got.secondary_tickers == ("NVDA",)
    assert got.causal_links[0].source_fact_ids == ("news:1",)


def test_primary_ticker_must_be_named_in_a_supplied_fact():
    with pytest.raises(CatalystValidationError, match="not named"):
        assess(raw(primary_tickers=["QQQ"]))


def test_model_cannot_invent_a_tradable_secondary_ticker():
    bad = raw(secondary_tickers=["FAKE"])
    bad["causal_links"][0]["target_ticker"] = "FAKE"
    with pytest.raises(CatalystValidationError, match="eligible universe"):
        assess(bad)


def test_every_secondary_requires_a_cited_causal_link():
    with pytest.raises(CatalystValidationError, match="every secondary"):
        assess(raw(causal_links=[]))


def test_absent_fact_citation_is_rejected():
    with pytest.raises(CatalystValidationError, match="supplied facts"):
        assess(raw(source_fact_ids=["news:999"]))


def test_extra_fields_cannot_smuggle_an_order_through_the_schema():
    with pytest.raises(CatalystValidationError, match="unknown fields"):
        assess(raw(order={"symbol": "AMD", "qty": 1000}))


def test_scores_must_be_finite_and_bounded():
    for value in (-0.01, 1.01, float("nan"), True):
        with pytest.raises(CatalystValidationError):
            assess(raw(confidence=value))


def test_fact_timestamp_must_be_timezone_aware():
    with pytest.raises(ValueError, match="timezone-aware"):
        CatalystFact("x", "2026-08-28T12:00:00", "headline", "", ("AMD",))
