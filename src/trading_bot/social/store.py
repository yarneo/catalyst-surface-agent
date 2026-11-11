"""Append-only raw storage for social posts.

The whole premise of this collector is that the past cannot be re-collected —
Pushshift closed public Reddit archives in 2023 and X's historical API is priced
out of reach. So the rules here are strict:

* **Append only.** Nothing is ever rewritten. A bug in feature extraction must
  never be able to destroy the raw record it was derived from.
* **Store the full text.** Not just a sentiment score. Today's analysis method
  will look primitive in a year, and re-analysis is only possible if the text
  survived.
* **Two timestamps, always.** ``created_utc`` is when the post was written;
  ``collected_utc`` is when we saw it. Only the second one is safe for
  point-in-time work — Reddit can surface old threads, edits happen, and a
  backtest that assumes we knew something at ``created_utc`` when we actually
  saw it three days later is inventing look-ahead.
* **Idempotent.** Re-running a day merges on post id rather than duplicating,
  so a retry after a crash is harmless.

One JSONL file per UTC day, gzipped. Cheap to append, trivial to inspect, and
no schema migration to get wrong.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "social" / "raw"


@dataclass
class Post:
    id: str                     # platform-unique, used for dedup
    source: str                 # e.g. "reddit"
    channel: str                # subreddit / feed name
    kind: str                   # "submission" | "comment"
    created_utc: float          # when it was WRITTEN
    collected_utc: float        # when WE saw it — the point-in-time-safe field
    score: int
    num_comments: int
    author: str
    title: str
    body: str
    url: str = ""
    cashtags: dict[str, int] = field(default_factory=dict)
    bare: dict[str, int] = field(default_factory=dict)
    # Self-declared "Bullish"/"Bearish" where the platform offers it
    # (StockTwits does; Reddit does not). Free labelled data — but it is the
    # author's claim about their own view, not a verified outcome, so it is
    # stored as-is and never trusted as ground truth.
    sentiment: str = ""

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}".strip()


def _path_for(day: str) -> Path:
    return RAW_DIR / f"{day}.jsonl.gz"


def append(posts: list[Post], day: str | None = None) -> dict:
    """Merge ``posts`` into the day's file, deduplicating on id."""
    day = day or datetime.now(timezone.utc).date().isoformat()
    path = _path_for(day)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, dict] = {}
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    existing[rec["id"]] = rec

    before = len(existing)
    for p in posts:
        rec = asdict(p)
        # Never let a re-run move collected_utc later; the earliest sighting is
        # the honest one for point-in-time purposes.
        if p.id in existing:
            rec["collected_utc"] = min(
                existing[p.id].get("collected_utc", p.collected_utc), p.collected_utc)
        existing[p.id] = rec

    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        for rec in existing.values():
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(path)          # atomic; a crash mid-write cannot truncate history

    return {"day": day, "existing": before, "new": len(existing) - before,
            "total": len(existing), "path": str(path)}


def load_day(day: str) -> list[dict]:
    path = _path_for(day)
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def available_days() -> list[str]:
    if not RAW_DIR.exists():
        return []
    return sorted(p.name.replace(".jsonl.gz", "") for p in RAW_DIR.glob("*.jsonl.gz"))


def stats() -> dict:
    days = available_days()
    if not days:
        return {"days": 0, "posts": 0}
    total = sum(len(load_day(d)) for d in days)
    return {"days": len(days), "first": days[0], "last": days[-1], "posts": total}
