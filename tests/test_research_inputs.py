from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
import json
import time
import requests
import pytest

from trading_bot.signals import news, sentiment
from trading_bot.data import quotes
from trading_bot.research_health import research_inputs

NOW = datetime(2026, 11, 22, 13, tzinfo=timezone.utc)
AT = NOW.isoformat()


def article(**updates):
    return {"id": 1, "created_at": AT, "updated_at": AT, "headline": "Company reports new contract",
        "summary": "A cited company announcement.", "symbols": ["XOM"], **updates}


def assessment(**updates):
    return {"score": 0.0, "confidence": 0.4, "dominant_event_type": "NOISE",
        "disconfirming": "none supplied", "rationale": "The supplied sector news is mixed.",
        "source_fact_ids": ["alpaca:1"], **updates}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sentiment, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(sentiment, "HEALTH_PATH", tmp_path / "research/sentiment_health.json")
    monkeypatch.setattr(sentiment, "model_key", lambda: "synthetic")
    monkeypatch.setattr(quotes, "DATA", tmp_path)
    monkeypatch.delenv("RESEARCH_QUOTE_FEED", raising=False)
    return tmp_path


def test_sector_context_is_labeled_and_future_unrelated_duplicate_articles_are_excluded():
    rows = [article(), article(), article(id=2, created_at=(NOW+timedelta(seconds=1)).isoformat()),
        article(id=3, symbols=["OTHER"]), None, article(id=4, symbols="XLE"),
        article(id=5, headline="ETF fund announcement", symbols=["XLE"])]
    selected, rejected = news.select_articles(rows, "XLE", now=NOW)
    assert len(selected) == 2 and rejected == {"timestamp": 2, "unrelated": 2, "empty": 0, "duplicate": 1}
    assert selected[0]["scope"] == "sector_context" and selected[0]["context_for"] == "XLE"
    assert selected[1]["scope"] == "direct"


def test_news_queries_fixed_proxies_with_explicit_observation_end():
    observed = {}
    def get(url, **kw):
        observed.update(kw)
        return NS(ok=True, json=lambda: {"news": [article()]})
    result = news.fetch_news("XLE", now=NOW, get=get, secrets={"ALPACA_API_KEY":"test", "ALPACA_SECRET_KEY":"test"})
    assert result["status"] == "READY" and result["context_articles"] == 1 and result["direct_articles"] == 0
    assert observed["params"]["symbols"] == "XLE,XOM,CVX"
    assert observed["params"]["end"] == AT


def test_missing_news_is_not_a_neutral_model_vote(isolated, monkeypatch):
    monkeypatch.setattr(sentiment, "fetch_news", lambda *a, **k:{"status":"NO_RECENT_NEWS", "articles":[]})
    monkeypatch.setattr(sentiment, "bounded_request", lambda *a: pytest.fail("no facts must not invoke a model"))
    result = sentiment.get_daily_sentiment("XLE", now=NOW)
    assert result["input_status"] == "NO_RECENT_NEWS" and result["source"] == "fallback"
    assert not sentiment.CACHE_PATH.exists()
    assert research_inputs(isolated, now=NOW)["sentiment"]["XLE"]["input_status"] == "NO_RECENT_NEWS"


def wire_model(monkeypatch, raw):
    articles, _ = news.select_articles([article()], "XLE", now=NOW)
    monkeypatch.setattr(sentiment, "fetch_news", lambda *a, **k:{"status":"READY", "articles":articles, "context_articles":1})
    monkeypatch.setattr(sentiment, "bounded_request", lambda *a:{"choices":[{"message":{"content":json.dumps(raw)}}]})


def test_valid_neutral_assessment_is_different_from_missing_inputs_and_cached(isolated, monkeypatch):
    wire_model(monkeypatch, assessment())
    first = sentiment.get_daily_sentiment("XLE", now=NOW)
    assert first["input_status"] == "READY" and first["source"] == "llm" and first["score"] == 0
    assert first["news_articles"][0]["scope"] == "sector_context"
    monkeypatch.setattr(sentiment, "bounded_request", lambda *a: pytest.fail("valid cache expected"))
    assert sentiment.get_daily_sentiment("XLE", now=NOW)["source"] == "cache"
    assert research_inputs(isolated, now=NOW+timedelta(days=2))["sentiment"]["XLE"]["stale"]


@pytest.mark.parametrize("bad", [assessment(score=float("nan")), assessment(source_fact_ids=["invented"]),
    assessment(score=True), assessment(order="buy"), assessment(rationale="")])
def test_invalid_model_output_has_no_effect_and_is_not_cached(isolated, monkeypatch, bad):
    wire_model(monkeypatch, bad)
    result = sentiment.get_daily_sentiment("XLE", now=NOW)
    assert result["input_status"] == "MODEL_UNAVAILABLE" and result["score"] == result["confidence"] == 0
    assert not sentiment.CACHE_PATH.exists()


def test_model_change_and_expired_or_future_cache_require_new_evidence(isolated, monkeypatch):
    wire_model(monkeypatch, assessment(score=-0.4))
    first = sentiment.get_daily_sentiment("XLE", now=NOW)
    assert sentiment.get_daily_sentiment("XLE", now=NOW, model="different")["source"] == "llm"
    assert sentiment.get_daily_sentiment("XLE", now=NOW+timedelta(hours=5), model="different")["source"] == "llm"
    assert sentiment.get_daily_sentiment("XLE", now=NOW, model="different")["source"] == "llm"


def test_quote_access_is_rechecked_after_upgrade_and_distinguishes_auth_error(isolated):
    calls = []
    def denied(symbols, feed):
        calls.append(feed)
        raise requests.HTTPError(response=NS(status_code=403,text="subscription does not permit querying recent SIP data"))
    a = quotes.select_feed(["TEST"], at=AT, fetch=denied)
    assert a[0] == "iex" and a[2]["capabilities"]["sip"] == "SUBSCRIPTION_REQUIRED"
    quotes.select_feed(["TEST"], at=AT, fetch=denied)
    assert calls == ["sip"]
    later = (NOW+timedelta(minutes=16)).isoformat()
    b = quotes.select_feed(["TEST"], at=later, fetch=lambda *a, **k:{"TEST":{"feed":"sip"}})
    assert b[0] == "sip" and b[1]["TEST"]["feed"] == "sip"
    def auth(*a, **k):raise requests.HTTPError(response=NS(status_code=403,text="invalid credentials"))
    c = quotes.select_feed(["TEST"], at=later, fetch=auth, force=True)
    assert c[2]["capabilities"]["sip"] == "ACCESS_DENIED"


def test_broker_accepts_selected_sip_but_never_stale_quotes(monkeypatch):
    from trading_bot.broker.alpaca_client import AlpacaBroker
    from trading_bot.broker.orders import ExecutionFailure
    broker = object.__new__(AlpacaBroker)
    broker.config = NS(api_key="test", secret_key="test")
    quote = {"bid":100, "ask":100.01, "at":datetime.now(timezone.utc).isoformat(), "feed":"sip"}
    monkeypatch.setattr(quotes, "latest_quotes", lambda *a, **k:({"TEST":quote}, {}))
    assert broker.quote("TEST")["feed"] == "sip"
    quote["at"] = (datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
    with pytest.raises(ExecutionFailure, match="STALE_QUOTE"):broker.quote("TEST")


def test_no_delayed_feed_even_if_requested(monkeypatch, isolated):
    monkeypatch.setenv("RESEARCH_QUOTE_FEED", "delayed_sip")
    with pytest.raises(ValueError):quotes.select_feed(["TEST"], at=AT)
