"""StockTwits collector — no credentials required.

Reddit closed unauthenticated access entirely (every JSON endpoint 403s as of
2026-08) and its script-app flow now requires separate API registration. For
attention data StockTwits is the better source anyway, for three reasons:

* **Tickers are platform-tagged.** Every message carries the symbols it refers
  to, assigned by StockTwits itself. The whole ambiguity problem that forced a
  stop-list and a no-single-letters rule for Reddit text simply does not exist
  here. ``tickers.py`` still applies to free text but is not load-bearing.
* **Sentiment is self-labelled.** Users optionally tag Bullish or Bearish. That
  is free labelled training data, of a kind you would normally pay for or infer
  with a model that then has to be validated.
* **It is finance-native.** No filtering general chatter down to market talk.

The trade-off, stated plainly: StockTwits skews retail, promotional and
small-cap-heavy. That is a *feature* for an attention signal — it is where
institutional NLP is thinnest — but it means the data is noisy and gameable, and
that is exactly why the collector records distinct author counts and why the
feature layer refuses single-author spikes.

Rate limits are unpublished but roughly 200 requests/hour unauthenticated, so
the collector is deliberately slow and capped.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from .store import Post

BASE = "https://api.stocktwits.com/api/2"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class StockTwitsUnavailable(RuntimeError):
    """Rate-limited or blocked. Record the day as empty; never invent data."""


def _get(path: str, timeout: int = 15) -> dict:
    r = requests.get(f"{BASE}{path}", headers={"User-Agent": UA}, timeout=timeout)
    if r.status_code == 429:
        raise StockTwitsUnavailable("rate limited (429) — back off and retry later")
    if r.status_code != 200:
        raise StockTwitsUnavailable(f"HTTP {r.status_code} for {path}")
    return r.json()


def trending_symbols() -> list[str]:
    """What the platform itself says is being talked about right now.

    Worth storing on its own: it is an attention ranking computed over the full
    firehose, which we cannot reconstruct from the sampled streams we can fetch.
    """
    try:
        return [s["symbol"] for s in _get("/trending/symbols.json").get("symbols", [])]
    except StockTwitsUnavailable:
        return []


def _to_post(msg: dict, symbol: str, now: float) -> Post:
    ents = msg.get("entities") or {}
    sent = ((ents.get("sentiment") or {}) or {}).get("basic") or ""
    syms = {s["symbol"] for s in (msg.get("symbols") or [])} or {symbol}
    user = msg.get("user") or {}
    return Post(
        id=f"stocktwits_{msg['id']}",
        source="stocktwits",
        channel=symbol,
        kind="message",
        created_utc=_parse_ts(msg.get("created_at")),
        collected_utc=now,
        score=int(((msg.get("likes") or {}).get("total")) or 0),
        num_comments=0,
        author=str(user.get("username") or "[unknown]"),
        title="",
        body=msg.get("body") or "",
        url=f"https://stocktwits.com/message/{msg['id']}",
        # Platform-assigned symbols go in `cashtags`: they carry cashtag-level
        # precision because StockTwits tagged them, not a regex.
        cashtags={s: 1 for s in syms},
        bare={},
        sentiment=sent,
    )


def _parse_ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def collect(
    symbols: list[str],
    *,
    include_trending: bool = True,
    max_symbols: int = 60,
    pause_s: float = 1.2,
) -> tuple[list[Post], list[str]]:
    """Fetch recent messages for ``symbols`` (plus whatever is trending).

    Returns (posts, trending). Trending is returned separately so it can be
    stored as its own record — it is a ranking over data we never see.
    """
    now = datetime.now(timezone.utc).timestamp()
    trending = trending_symbols() if include_trending else []

    # Trending first: those are the names with an attention event today, which
    # is precisely what we are trying to catch.
    ordered = list(dict.fromkeys(trending + list(symbols)))[:max_symbols]

    posts: list[Post] = []
    for sym in ordered:
        try:
            data = _get(f"/streams/symbol/{sym}.json")
        except StockTwitsUnavailable as exc:
            print(f"  ⚠ {sym}: {exc}")
            if "rate limited" in str(exc):
                break          # stop cleanly rather than hammering
            continue
        for msg in data.get("messages", []):
            try:
                posts.append(_to_post(msg, sym, now))
            except Exception:  # noqa: BLE001 — one malformed message must not end the run
                continue
        time.sleep(pause_s)

    return posts, trending
