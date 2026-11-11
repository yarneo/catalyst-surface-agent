"""Turn raw social posts into a (date, symbol) panel.

Deliberately builds ATTENTION features, not sentiment ones. The evidence for
attention (how many people are suddenly talking about X) is considerably better
than for polarity (are they bullish), and attention is much harder to game: you
can astroturf tone with a hundred posts, but a genuine mention spike across many
distinct authors is expensive to fake. Sentiment can be layered on later from
the raw text, which is exactly why the text is preserved.

Everything is bucketed by ``collected_utc``, never ``created_utc``. Reddit
surfaces old threads and edits happen; using the write time would credit us with
knowing things before we saw them, which is look-ahead wearing a plausible
disguise.

Output matches the shape ``trading_bot.ml`` already consumes, so once enough
history accumulates the existing purged walk-forward harness can score it
against the same 90% Deflated-Sharpe gate as everything else.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .store import available_days, load_day


def _day_frame(recs: list[dict], min_authors: int = 2) -> pd.DataFrame:
    """One row per symbol for a single collection day."""
    rows: dict[str, dict] = {}
    for r in recs:
        counts: dict[str, int] = {}
        for sym, n in (r.get("cashtags") or {}).items():
            counts[sym] = counts.get(sym, 0) + n
        for sym, n in (r.get("bare") or {}).items():
            counts[sym] = counts.get(sym, 0) + n
        for sym, n in counts.items():
            d = rows.setdefault(sym, {
                "mentions": 0, "posts": 0, "authors": set(),
                "score_sum": 0, "cashtag_mentions": 0, "comment_mentions": 0,
            })
            d["mentions"] += n
            d["posts"] += 1
            d["authors"].add(r.get("author", "?"))
            d["score_sum"] += max(0, int(r.get("score", 0)))
            d["cashtag_mentions"] += (r.get("cashtags") or {}).get(sym, 0)
            if r.get("kind") == "comment":
                d["comment_mentions"] += n

    out = []
    for sym, d in rows.items():
        n_auth = len(d["authors"])
        # A "spike" driven by one prolific poster is not attention, it is one
        # person. Require independent voices before the row counts at all.
        if n_auth < min_authors:
            continue
        out.append({
            "symbol": sym,
            "mentions": d["mentions"],
            "posts": d["posts"],
            "authors": n_auth,
            "score_sum": d["score_sum"],
            "cashtag_frac": d["cashtag_mentions"] / max(1, d["mentions"]),
            "comment_frac": d["comment_mentions"] / max(1, d["mentions"]),
        })
    return pd.DataFrame(out)


def build_attention_panel(
    days: list[str] | None = None,
    *,
    min_authors: int = 2,
    spike_window: int = 20,
) -> pd.DataFrame:
    """Panel indexed by (date, symbol) with attention features.

    The headline feature is ``mention_z`` — today's mentions against that
    symbol's own recent baseline. Raw counts are useless cross-sectionally
    (AAPL always out-mentions everything); the *surprise* is the signal.
    """
    days = days or available_days()
    frames = []
    for day in days:
        recs = load_day(day)
        if not recs:
            continue
        f = _day_frame(recs, min_authors=min_authors)
        if f.empty:
            continue
        f["date"] = pd.Timestamp(day)
        frames.append(f)
    if not frames:
        return pd.DataFrame()

    panel = pd.concat(frames).set_index(["date", "symbol"]).sort_index()

    # per-symbol rolling baseline -> spike score
    wide = panel["mentions"].unstack("symbol").fillna(0.0)
    mu = wide.rolling(spike_window, min_periods=5).mean()
    sd = wide.rolling(spike_window, min_periods=5).std().replace(0, np.nan)
    z = ((wide - mu) / sd).stack(future_stack=True).rename("mention_z")
    growth = (wide / mu.replace(0, np.nan)).stack(future_stack=True).rename("mention_ratio")

    share = wide.div(wide.sum(axis=1).replace(0, np.nan), axis=0)
    share_z = ((share - share.rolling(spike_window, min_periods=5).mean())
               / share.rolling(spike_window, min_periods=5).std().replace(0, np.nan))
    share_z = share_z.stack(future_stack=True).rename("share_z")

    panel = panel.join([z, growth, share_z], how="left")
    panel["authors_per_post"] = panel["authors"] / panel["posts"].clip(lower=1)
    panel["avg_score"] = panel["score_sum"] / panel["posts"].clip(lower=1)
    return panel


def coverage_report() -> dict:
    """How close is the archive to being testable?

    Answers the only question that matters early on: is it worth looking yet.
    Roughly a year of daily observations is the minimum for a purged
    walk-forward with a holdout to say anything trustworthy.
    """
    days = available_days()
    if not days:
        return {"days": 0, "ready": False,
                "note": "no data yet — run the collector daily"}
    first = datetime.fromisoformat(days[0]).replace(tzinfo=timezone.utc)
    span = (datetime.now(timezone.utc) - first).days
    panel = build_attention_panel()
    return {
        "days_collected": len(days),
        "calendar_span_days": span,
        "symbols_seen": int(panel.index.get_level_values("symbol").nunique())
                        if not panel.empty else 0,
        "rows": int(len(panel)),
        "ready_to_test": len(days) >= 250,
        "note": ("enough history to attempt a purged walk-forward"
                 if len(days) >= 250 else
                 f"keep collecting — {250 - len(days)} more trading days before "
                 f"a holdout test means anything"),
    }
