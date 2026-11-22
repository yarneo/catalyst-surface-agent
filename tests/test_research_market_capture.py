from copy import deepcopy
from types import SimpleNamespace as NS
import json
import requests
import pytest

from scripts import capture_research_market as collector
from trading_bot.market_tape import capture_tick, quote_coverage, quote_problem


AT="2026-11-12T14:35:03+00:00"


def quote(at=AT):
    return {"bid":100.,"ask":100.01,"at":at,"feed":"iex"}


def test_feed_probe_remembers_missing_entitlement_without_repeated_requests(tmp_path,monkeypatch):
    monkeypatch.setattr(collector,"DATA",tmp_path)
    monkeypatch.delenv("RESEARCH_QUOTE_FEED",raising=False)
    calls=[]
    def fetch(symbols,*,feed):
        calls.append(feed)
        raise requests.HTTPError(response=NS(status_code=403))
    monkeypatch.setattr(collector,"fetch_quotes",fetch)
    feed,quotes,access=collector.select_feed(["TEST"],at=AT)
    assert feed=="iex" and quotes is None
    assert access["capabilities"]["sip"]=="SUBSCRIPTION_REQUIRED"
    assert collector.select_feed(["TEST"],at=AT)[0]=="iex"
    assert calls==["sip"]


def test_available_sip_probe_is_used_without_duplicate_request(tmp_path,monkeypatch):
    monkeypatch.setattr(collector,"DATA",tmp_path)
    monkeypatch.delenv("RESEARCH_QUOTE_FEED",raising=False)
    monkeypatch.setattr(collector,"fetch_quotes",lambda symbols,feed:{"TEST":quote()})
    feed,initial,access=collector.select_feed(["TEST"],at=AT)
    assert feed=="sip" and initial["TEST"]["bid"]==100.
    assert access["capabilities"]["sip"]=="AVAILABLE"


def test_retry_keeps_real_exchange_time_and_does_not_accept_stale_quote(monkeypatch):
    stale=quote("2026-11-12T14:30:00+00:00")
    calls=[]
    def fetch(symbols,*,feed):
        calls.append(symbols)
        return {"TEST":deepcopy(stale)}
    monkeypatch.setattr(collector,"fetch_quotes",fetch)
    prices,at,details=collector.observe_quotes(["TEST"],feed="iex",initial={"TEST":stale},now=lambda:AT,sleep=lambda _:None)
    assert prices["TEST"]["at"]==stale["at"]
    assert quote_problem(prices["TEST"],at=at)=="STALE_QUOTE"
    assert calls==[["TEST"]]


def test_retry_recovers_only_bad_symbols(monkeypatch):
    calls=[]
    def fetch(symbols,*,feed):
        calls.append(symbols);return {"BAD":quote()}
    monkeypatch.setattr(collector,"fetch_quotes",fetch)
    prices,at,details=collector.observe_quotes(["GOOD","BAD"],feed="iex",initial={"GOOD":quote()},now=lambda:AT,sleep=lambda _:None)
    assert calls==[["BAD"]]
    assert all(quote_problem(q,at=at) is None for q in prices.values())


def test_delayed_feed_cannot_be_used_as_fresh_execution_evidence():
    with pytest.raises(ValueError,match="real-time"):
        collector.fetch_quotes(["TEST"],feed="delayed_sip")


def test_healthy_last_tick_cannot_erase_earlier_quote_gaps(tmp_path):
    plan={"session":"2026-11-12","market_open":"2026-11-12T09:30:00-05:00",
          "market_close":"2026-11-12T16:00:00-05:00","symbols":["TEST"],"plan_hash":"synthetic"}
    capture_tick(tmp_path,plan,{"TEST":quote("2026-11-12T14:30:00+00:00")},at=AT)
    later="2026-11-12T14:36:03+00:00"
    capture_tick(tmp_path,plan,{"TEST":quote(later)},at=later)
    result=quote_coverage(tmp_path,plan,through=later)
    assert result["collected_minutes"]==result["expected_minutes"]==2
    assert result["missing_tick_minutes"]==0
    assert result["problems_by_symbol"]["TEST"]=={"STALE_QUOTE":1}


def test_changed_old_quote_is_not_silently_recaptured(tmp_path):
    plan={"session":"2026-11-12","market_open":"2026-11-12T09:30:00-05:00",
          "market_close":"2026-11-12T16:00:00-05:00","symbols":["TEST"],"plan_hash":"synthetic"}
    old=capture_tick(tmp_path,plan,{"TEST":quote("2026-11-12T14:30:00+00:00")},at=AT)
    assert capture_tick(tmp_path,plan,{"TEST":quote()},at=AT)==old


def test_dashboard_cannot_call_incomplete_validation_ready():
    from trading_bot.research_health import apply_research_health
    report={"engineering":{"status":"READY"},"status":"COLLECTING",
        "production_verification":{"legacy_precision_sessions":["2026-11-12"],"matching_sessions":[]},
        "market_collection":{"session_coverage":{"quote_gap_minutes_by_symbol":{"KMLM":8}},
            "feed_access":{"capabilities":{"sip":"SUBSCRIPTION_REQUIRED"}}}}
    out=apply_research_health(report)
    assert out["status"]=="DATA_BLOCKED"
    assert out["pipeline"]["status"]=="ACTION_REQUIRED"
    assert "KMLM: 8" in out["next_action"]
    assert "five fresh lossless opens" in out["next_action"]
    assert "cannot guarantee" in out["next_action"]


def test_old_rounding_alone_is_waiting_for_evidence_not_a_new_engine_failure():
    from trading_bot.research_health import apply_research_health
    out=apply_research_health({"production_verification":{"legacy_precision_sessions":["2026-11-12"]}})
    assert out["pipeline"]["status"]=="AWAITING_OBSERVATIONS"
    assert not out["pipeline"]["issues"]


def test_bil_valuation_exception_does_not_claim_session_failed():
    from trading_bot.research_health import apply_research_health
    out=apply_research_health({"forward_collection":{"status":"OBSERVED"},
        "market_collection":{"session_coverage":{"quote_gap_minutes_by_symbol":{"BIL":200}}}})
    assert out["pipeline"]["status"]=="AWAITING_OBSERVATIONS"
