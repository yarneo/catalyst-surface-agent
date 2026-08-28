from trading_bot.tournament.catalyst import CatalystAssessment, CatalystFact
from trading_bot.tournament.featherless import CommitteeResult
from trading_bot.tournament.integrity import evaluate_event_integrity


def fact(headline="Broadcom will report results after the close", summary=""):
    return CatalystFact("official:avgo", "2026-08-28T15:00:00Z", headline,
                        summary, ("AVGO",), "alpaca_news")


def assessment(*, primary=("AVGO",), novelty=0.8, surprise=0.8,
               confidence=0.8):
    return CatalystAssessment(
        catalyst_type="scheduled earnings", factual_summary="Event remains scheduled.",
        novelty=novelty, surprise=surprise, direction="unknown",
        expected_half_life_minutes=300, primary_tickers=primary,
        secondary_tickers=(), causal_links=(), confidence=confidence,
        invalidation="Results are released early.",
        source_fact_ids=("official:avgo",))


def result(value=None, reason="2/3 agree unknown"):
    return CommitteeResult((), value, 1.0 if value else 0.0, reason)


def test_valid_grounded_neutral_quorum_clears_an_unresolved_event():
    decision = evaluate_event_integrity(result(assessment()), [fact()])
    assert decision.clear
    assert decision.cited_fact_ids == ("official:avgo",)


def test_no_model_quorum_blocks_entry_instead_of_bypassing_featherless():
    decision = evaluate_event_integrity(result(), [fact()])
    assert not decision.clear
    assert "quorum unavailable" in decision.reason


def test_strong_grounded_preannouncement_vetoes_the_straddle():
    early = fact("Broadcom announced quarterly results early")
    decision = evaluate_event_integrity(result(assessment()), [early])
    assert not decision.clear
    assert "already be resolved" in decision.reason


def test_weak_model_language_cannot_turn_a_phrase_into_a_veto():
    early = fact("Broadcom announced quarterly results early")
    weak = assessment(novelty=0.4, surprise=0.4, confidence=0.4)
    assert evaluate_event_integrity(result(weak), [early]).clear


def test_assessment_about_another_ticker_cannot_clear_avgo():
    decision = evaluate_event_integrity(
        result(assessment(primary=("NVDA",))), [fact()])
    assert not decision.clear
    assert "AVGO as primary" in decision.reason
