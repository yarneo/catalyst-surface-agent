"""Reddit collector.

Deliberately collects BROADLY — every ticker-shaped mention is kept, not just
symbols in the current trading universe. Two reasons, both about optionality:

* The interesting signal is plausibly in names we do *not* trade yet. Filtering
  at collection time to today's universe would guarantee we can only ever
  rediscover what we already watch.
* Attention data is most valuable where institutional NLP is thinnest — small
  caps. Restricting to large caps would discard exactly the part with a chance
  of being uncrowded.

Filtering happens at feature time, which is reversible. Collection is not.

Requires a free Reddit "script" app: set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET
in .env. No Reddit password or user account is needed for read-only access.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from .store import Post
from .tickers import extract

# Retail-heavy finance subs. smallstreetbets / pennystocks are deliberately
# included: that is where attention is least likely to be already arbitraged.
DEFAULT_SUBREDDITS = [
    "wallstreetbets", "stocks", "investing", "StockMarket", "options",
    "smallstreetbets", "pennystocks", "Shortsqueeze", "Daytrading",
    "SecurityAnalysis", "ValueInvesting",
]


class RedditUnavailable(RuntimeError):
    """Credentials missing or the API refused us. Never fabricate a fallback —
    an empty day must be recorded as empty, not silently filled."""


def _client():
    try:
        import praw
    except ImportError as exc:  # pragma: no cover
        raise RedditUnavailable(f"praw not installed: {exc}") from exc

    cid = os.environ.get("REDDIT_CLIENT_ID")
    secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        raise RedditUnavailable(
            "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set. Create a free "
            "'script' app at https://www.reddit.com/prefs/apps (read-only; no "
            "Reddit password required) and add both to .env."
        )
    return praw.Reddit(client_id=cid, client_secret=secret,
                       user_agent="trading-bot:attention-collector:v1 (by /u/unknown)",
                       check_for_async=False)


def collect(
    subreddits: list[str] | None = None,
    *,
    limit_per_sub: int = 100,
    include_comments: bool = True,
    comments_per_post: int = 30,
    known_tickers: frozenset[str] | None = None,
) -> list[Post]:
    """Pull recent posts (and top comments) and tag ticker mentions."""
    reddit = _client()
    subs = subreddits or DEFAULT_SUBREDDITS
    now = datetime.now(timezone.utc).timestamp()
    out: list[Post] = []

    for sub in subs:
        try:
            listing = reddit.subreddit(sub).new(limit=limit_per_sub)
            for s in listing:
                text = f"{s.title}\n{s.selftext or ''}"
                m = extract(text, known_tickers)
                out.append(Post(
                    id=f"reddit_{s.id}", source="reddit", channel=sub,
                    kind="submission", created_utc=float(s.created_utc),
                    collected_utc=now, score=int(s.score),
                    num_comments=int(s.num_comments),
                    author=str(s.author) if s.author else "[deleted]",
                    title=s.title or "", body=s.selftext or "",
                    url=f"https://reddit.com{s.permalink}",
                    cashtags=m.cashtags, bare=m.bare,
                ))
                if include_comments and comments_per_post:
                    try:
                        s.comments.replace_more(limit=0)
                        for c in s.comments.list()[:comments_per_post]:
                            cm = extract(c.body or "", known_tickers)
                            if not cm.all_symbols:
                                continue          # comments are noisy; keep the ones that matter
                            out.append(Post(
                                id=f"reddit_{c.id}", source="reddit", channel=sub,
                                kind="comment", created_utc=float(c.created_utc),
                                collected_utc=now, score=int(c.score),
                                num_comments=0,
                                author=str(c.author) if c.author else "[deleted]",
                                title="", body=c.body or "",
                                url=f"https://reddit.com{s.permalink}",
                                cashtags=cm.cashtags, bare=cm.bare,
                            ))
                    except Exception:  # noqa: BLE001 — a bad thread must not kill the run
                        continue
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ r/{sub}: {type(exc).__name__}: {exc}")
            continue

    return out
