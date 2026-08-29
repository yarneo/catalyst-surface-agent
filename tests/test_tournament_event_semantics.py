import json

from trading_bot.tournament.catalyst import CatalystFact
from trading_bot.tournament.event_semantics import EventSemanticClassifier


FACTS = (
    CatalystFact("news:1", "2026-08-28T17:00:00Z",
                 "AAA slides ahead of earnings",
                 "AAA is scheduled to report on September 2 after the close.",
                 ("AAA",)),
    CatalystFact("surface:BBB", "2026-08-28T20:00:00Z",
                 "BBB option surface has a term bump",
                 "The term bump does not identify an event.", ("BBB",),
                 "alpaca_option_surface"),
)
CANDIDATES = ({"ticker": "AAA", "term_ratio": 1.3},
              {"ticker": "BBB", "term_ratio": 1.2})


def response(*, aaa_status="upcoming", date="2026-09-02T16:00:00-04:00"):
    value = {"events": [
        {"ticker": "AAA", "event_type": "earnings", "status": aaa_status,
         "scheduled_datetime": date, "factual_summary": "AAA earnings are scheduled.",
         "confidence": 0.8, "source_fact_ids": ["news:1"],
         "invalidation": "A cited schedule change."},
        {"ticker": "BBB", "event_type": "unclear", "status": "unclear",
         "scheduled_datetime": None, "factual_summary": "Only a term bump is supplied.",
         "confidence": 0.3, "source_fact_ids": ["surface:BBB"],
         "invalidation": "A sourced event is identified."},
    ]}
    return {"choices": [{"message": {"content": json.dumps(value)}}],
            "usage": {"total_tokens": 10}}


def test_event_semantic_quorum_preserves_unknown_instead_of_inventing_event():
    client = EventSemanticClassifier(
        "key", models=("a", "b", "c"), transport=lambda *_: response())
    result = client.analyze(FACTS, candidates=CANDIDATES)
    accepted = result.by_ticker()
    assert accepted["AAA"].event_type == "earnings"
    assert accepted["AAA"].datetime_quorum is True
    assert accepted["BBB"].status == "unclear"
    assert accepted["BBB"].scheduled_datetime is None
    assert all(not event.order_enabled for event in accepted.values())


def test_disagreement_fails_the_ticker_closed():
    def transport(model, *_):
        return response(aaa_status={"a": "upcoming", "b": "occurred", "c": "unclear"}[model])

    result = EventSemanticClassifier(
        "key", models=("a", "b", "c"), transport=transport,
    ).analyze(FACTS, candidates=CANDIDATES)
    assert "AAA" not in result.by_ticker()
    assert "BBB" in result.by_ticker()


def test_datetime_needs_its_own_quorum():
    dates = {"a": "2026-09-02T16:00:00-04:00",
             "b": "2026-09-03T16:00:00-04:00", "c": None}

    def transport(model, *_):
        return response(date=dates[model])

    result = EventSemanticClassifier(
        "key", models=("a", "b", "c"), transport=transport,
    ).analyze(FACTS, candidates=CANDIDATES)
    event = result.by_ticker()["AAA"]
    assert event.status == "upcoming"
    assert event.scheduled_datetime is None
    assert event.datetime_quorum is False


def test_datetime_quorum_normalizes_equivalent_timezones():
    dates = {"a": "2026-09-02T16:00:00-04:00",
             "b": "2026-09-02T20:00:00Z", "c": None}

    def transport(model, *_):
        return response(date=dates[model])

    result = EventSemanticClassifier(
        "key", models=("a", "b", "c"), transport=transport,
    ).analyze(FACTS, candidates=CANDIDATES)
    event = result.by_ticker()["AAA"]
    assert event.scheduled_datetime == "2026-09-02T20:00:00Z"
    assert event.datetime_quorum is True


def test_extra_order_field_invalidates_only_that_ticker_without_accepting_order_data():
    def transport(*_):
        value = response()
        parsed = json.loads(value["choices"][0]["message"]["content"])
        parsed["events"][0]["qty"] = 100
        value["choices"][0]["message"]["content"] = json.dumps(parsed)
        return value

    result = EventSemanticClassifier(
        "key", models=("a", "b"), transport=transport,
    ).analyze(FACTS, candidates=CANDIDATES)
    assert "AAA" not in result.by_ticker()
    assert result.by_ticker()["BBB"].status == "unclear"
    assert all(attempt.error for attempt in result.attempts)
