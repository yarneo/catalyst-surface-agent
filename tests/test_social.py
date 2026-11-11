"""Tests for the social attention collector.

The data this collects cannot be re-collected — public Reddit archives closed in
2023. So the failure that matters most is not "wrong number", it is "silently
lost or corrupted history", and most of these tests are about that.

Run: uv run python -m pytest tests/ -q
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_bot.social import extract
from trading_bot.social.store import Post, append, load_day
from trading_bot.social.tickers import STOPWORDS

KNOWN = frozenset({"AAPL", "NVDA", "TSLA", "IT", "A", "DD", "GME", "F", "ON"})


# ------------------------------------------------------------ extraction


def test_cashtags_are_always_accepted():
    m = extract("$AAPL to the moon", KNOWN)
    assert m.cashtags == {"AAPL": 1}


def test_english_words_that_are_real_tickers_are_excluded_from_bare():
    """IT, A, DD, ON and F are all genuinely listed. Matching them bare turns
    every post into noise that correlates with writing style — structured
    garbage a model will happily fit."""
    m = extract("IT is A good DD ON F stock", KNOWN)
    assert m.bare == {}


def test_single_letter_tickers_are_never_matched_bare():
    """F, T, X, K, A are all real tickers and all appear constantly as ordinary
    text. No stop-list separates them, so the rule is structural: length >= 2."""
    m = extract("F T X K A are words here", frozenset({"F", "T", "X", "K", "A"}))
    assert m.bare == {}
    assert extract("$F is Ford", frozenset({"F"})).cashtags == {"F": 1}


def test_cashtag_overrides_the_stoplist():
    """Someone writing $IT means the ticker. The stop-list is about ambiguity,
    and the dollar sign removes it."""
    m = extract("$IT is undervalued", KNOWN)
    assert m.cashtags == {"IT": 1}


def test_bare_matching_requires_a_known_universe():
    """Without a ticker list, guessing is worse than abstaining — a wrong symbol
    attributes attention to the wrong company."""
    assert extract("NVDA is up", None).bare == {}
    assert extract("NVDA is up", KNOWN).bare == {"NVDA": 1}


def test_unknown_symbols_are_not_invented():
    assert extract("ZZZZ mooning", KNOWN).bare == {}


def test_repeat_mentions_are_counted():
    m = extract("$GME $GME $GME", KNOWN)
    assert m.cashtags == {"GME": 3}


def test_stoplist_covers_common_finance_jargon():
    for junk in ("DD", "YOLO", "ATH", "CEO", "FOMC", "ETF", "IMO"):
        assert junk in STOPWORDS


def test_empty_text_is_safe():
    m = extract("", KNOWN)
    assert m.cashtags == {} and m.bare == {}


# ------------------------------------------------------- storage integrity


def _post(pid: str, collected: float, score: int = 1) -> Post:
    return Post(id=pid, source="reddit", channel="stocks", kind="submission",
                created_utc=1_700_000_000.0, collected_utc=collected,
                score=score, num_comments=0, author="u1",
                title="t", body="b", cashtags={"AAPL": 1})


def test_append_is_idempotent(tmp_path, monkeypatch):
    """A retry after a crash must not duplicate the day's history."""
    monkeypatch.setattr("trading_bot.social.store.RAW_DIR", tmp_path)
    append([_post("a", 100.0), _post("b", 100.0)], day="2026-08-05")
    res = append([_post("a", 100.0), _post("b", 100.0)], day="2026-08-05")
    assert res["total"] == 2 and res["new"] == 0


def test_append_merges_new_posts_without_dropping_old(tmp_path, monkeypatch):
    monkeypatch.setattr("trading_bot.social.store.RAW_DIR", tmp_path)
    append([_post("a", 100.0)], day="2026-08-05")
    res = append([_post("b", 100.0)], day="2026-08-05")
    assert res["total"] == 2
    assert {r["id"] for r in load_day("2026-08-05")} == {"a", "b"}


def test_collected_utc_never_moves_later(tmp_path, monkeypatch):
    """Point-in-time integrity: the EARLIEST sighting is the honest one. If a
    re-run could push collected_utc forward, a backtest would think we learned
    something later than we did — and that quietly hides look-ahead."""
    monkeypatch.setattr("trading_bot.social.store.RAW_DIR", tmp_path)
    append([_post("a", 100.0)], day="2026-08-05")
    append([_post("a", 999.0)], day="2026-08-05")
    assert load_day("2026-08-05")[0]["collected_utc"] == 100.0


def test_raw_text_is_preserved(tmp_path, monkeypatch):
    """Storing only a sentiment score would make re-analysis impossible, and
    today's method will look primitive in a year."""
    monkeypatch.setattr("trading_bot.social.store.RAW_DIR", tmp_path)
    p = _post("a", 100.0)
    p.body = "the full original text"
    append([p], day="2026-08-05")
    assert load_day("2026-08-05")[0]["body"] == "the full original text"


def test_both_timestamps_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr("trading_bot.social.store.RAW_DIR", tmp_path)
    append([_post("a", 100.0)], day="2026-08-05")
    rec = load_day("2026-08-05")[0]
    assert rec["created_utc"] == 1_700_000_000.0
    assert rec["collected_utc"] == 100.0


def test_missing_day_returns_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr("trading_bot.social.store.RAW_DIR", tmp_path)
    assert load_day("1999-01-01") == []


# ---------------------------------------------------------------- panel


def test_single_author_spam_is_not_attention(tmp_path, monkeypatch):
    """One prolific poster mentioning a ticker fifty times is one person, not a
    crowd. Astroturfing tone is cheap; faking many independent voices is not."""
    monkeypatch.setattr("trading_bot.social.store.RAW_DIR", tmp_path)
    from trading_bot.social.features import _day_frame
    recs = [{"cashtags": {"GME": 10}, "bare": {}, "author": "spammer",
             "score": 1, "kind": "submission"} for _ in range(20)]
    assert _day_frame(recs, min_authors=2).empty

    recs += [{"cashtags": {"GME": 1}, "bare": {}, "author": f"u{i}",
              "score": 1, "kind": "submission"} for i in range(3)]
    out = _day_frame(recs, min_authors=2)
    assert len(out) == 1 and out.iloc[0]["authors"] == 4


def test_nothing_here_feeds_the_executor():
    """Collect now, trade later. An unvalidated signal reaching live orders is
    exactly what the 0.30 DSR gate did."""
    import subprocess
    hits = subprocess.run(
        ["grep", "-rl", "trading_bot.social", "src/trading_bot/live",
         "scripts/run_portfolio.py"],
        capture_output=True, text=True).stdout.strip()
    assert hits == "", f"social data must not reach execution: {hits}"
