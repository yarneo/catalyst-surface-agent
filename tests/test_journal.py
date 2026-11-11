"""The journal must be incapable of breaking anything. These tests are mostly
about failure: no key, bad key, network down, corrupt file — the dashboard and
the runner must shrug every time."""
import json

from trading_bot.options.journal import Entry, append, narrate, read_all

FACTS = {"when": "2026-08-28T11:05", "equity": 100_000.0,
         "skipped": [{"symbol": "NFLX", "reason": "event in window"}],
         "opened": [{"symbol": "SPY", "qty": 9, "credit": 2115.0,
                     "risk": 5085.0, "strikes": "758/776", "expiry": "2026-09-03"}],
         "closed": [], "summary": "0 to close, 1 to open"}


def test_no_api_key_falls_back_to_facts(monkeypatch):
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    e = narrate(FACTS)
    assert not e.generated
    assert "SPY" in e.text
    assert "$2,115" in e.text
    assert "$5,085" in e.text


def test_a_dead_endpoint_falls_back_not_raises(monkeypatch):
    import trading_bot.options.journal as j
    monkeypatch.setattr(j, "URL", "http://127.0.0.1:1/nope")
    e = narrate(FACTS, api_key="rc_fake", timeout_s=0.5)
    assert not e.generated
    assert "SPY" in e.text


def test_fallback_mentions_skips_and_closes():
    facts = dict(FACTS, opened=[], closed=[{"symbol": "AMD", "qty": 2,
                                            "reason": "profit target"}])
    e = narrate(dict(facts), api_key=None)
    assert "NFLX" in e.text
    assert "AMD" in e.text
    assert "profit target" in e.text


def test_quiet_cycle_still_produces_an_entry():
    e = narrate({"summary": "0 to close, 0 to open"}, api_key=None)
    assert "No action" in e.text


def test_round_trips_through_disk(tmp_path):
    p = tmp_path / "j.jsonl"
    append(p, Entry("2026-08-28T11:05", "hello", True))
    append(p, Entry("2026-08-28T11:20", "again", False))
    got = read_all(p)
    assert [e.text for e in got] == ["hello", "again"]
    assert got[0].generated and not got[1].generated


def test_a_corrupt_line_loses_one_entry_not_the_page(tmp_path):
    p = tmp_path / "j.jsonl"
    append(p, Entry("t1", "good", True))
    with p.open("a") as fh:
        fh.write("{broken json\n")
    append(p, Entry("t2", "also good", True))
    assert [e.text for e in read_all(p)] == ["good", "also good"]


def test_missing_file_is_an_empty_journal(tmp_path):
    assert read_all(tmp_path / "absent.jsonl") == []
