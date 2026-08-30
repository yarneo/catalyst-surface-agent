#!/usr/bin/env python3
"""Distil the shadow scan and classifier into one publishable selection funnel.

The agent places at most one order this week, which reads as a one-trade bot
unless you can see what it declined. This reduces two recorded shadow artifacts
to the funnel that produced that decision — universe, measurable, event-like,
semantically confirmed, order-enabled — and the reason each name dropped out.

Reads only committed research artifacts. No credentials, no network, and it
cannot place or authorize anything. Regenerate with:

    python scripts/build_event_premium_funnel.py

The final stage is not derived from the scan: replay outcomes and dispositions
are quoted from the postmortem, with the source named in the artifact, so a
reader can check the claim against the document that decided it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Quoted from research/.../POSTMORTEM_AND_HYPOTHESES.md, the generalized
# multi-name long-straddle replay (eight events per name, seven for HPE) and
# the decisions recorded beneath it. Kept as data so the artifact carries the
# numbers that produced the outcome, not just the outcome.
REPLAY = {
    "AVGO": dict(n=8, mean=+0.470, median=+0.457, wins=0.62, adverse_mean=+0.196,
                 adverse_median=+0.177, disposition="ORDER-ENABLED",
                 reason="Strongest and most robust: positive on both the "
                        "last-trade proxy and the adverse envelope, in mean "
                        "and median."),
    "SNOW": dict(n=8, mean=+0.381, median=+0.096, wins=0.62, adverse_mean=+0.146,
                 adverse_median=-0.097, disposition="SHADOW ONLY",
                 reason="Positive on average, but the adverse median is "
                        "negative and its closed-market spread exceeded the "
                        "frozen gate. Allocating would dilute stronger evidence."),
    "LULU": dict(n=8, mean=+0.191, median=+0.149, wins=0.62, adverse_mean=+0.042,
                 adverse_median=+0.069, disposition="EXCLUDED",
                 reason="Operational: the Thursday after-close release resolves "
                        "at the exact Friday 09:30 scoring boundary, leaving no "
                        "reliable liquid exit before measurement."),
    "DELL": dict(n=8, mean=+0.215, median=-0.106, wins=0.50, adverse_mean=+0.050,
                 adverse_median=-0.229, disposition="EXCLUDED",
                 reason="Negative median on both the proxy and the adverse "
                        "envelope; a positive mean carried by the tail."),
    "HPE": dict(n=7, mean=-0.095, median=0.0, wins=0.43, adverse_mean=-0.231,
                adverse_median=-0.200, disposition="EXCLUDED",
                reason="Negative expectancy, sub-coin-flip win rate, and the "
                       "classifier could not establish a datetime quorum."),
    "PANW": dict(n=8, mean=-0.329, median=-0.402, wins=0.25, adverse_mean=-0.440,
                 adverse_median=-0.426, disposition="EXCLUDED",
                 reason="Worst long-vol history in the set. Its short-fly "
                        "proxy was favourable but the four-leg adverse "
                        "envelope averaged -25.5% on a 15.3% combined spread."),
}

def _source_doc() -> str:
    """The postmortem sits under a different folder in the public export."""
    for rel in ("research/strategy-evidence/POSTMORTEM_AND_HYPOTHESES.md",):
        if (ROOT / rel).exists():
            return rel
    return "POSTMORTEM_AND_HYPOTHESES.md"


def _short(reason: str) -> str:
    """Group a skip reason into something countable."""
    if "does not imply a shared jump" in reason:
        return "no term-structure jump"
    if "usable call/put pairs" in reason:
        return "too few usable strike pairs near spot"
    if "EventPremiumUnavailable" in reason:
        return reason.split(":", 1)[-1].strip()[:60]
    return reason[:60]


def build(scan: dict, semantic: dict) -> dict:
    event_like = [c["ticker"] for c in semantic["candidates"]]
    accepted = semantic["accepted"]
    earnings = sorted(t for t, v in accepted.items()
                      if v.get("event_type") == "earnings")

    stages = [
        dict(stage="universe", count=scan["universe_size"],
             note="Stable liquid anchors, fixed before the scan so the "
                  "universe cannot be chosen after seeing the answer."),
        dict(stage="measurable", count=scan["measured_count"],
             note="A parity-corrected front/back surface could be built. "
                  f"{scan['universe_size'] - scan['measured_count']} names "
                  "could not be measured."),
        dict(stage="event-like", count=scan["event_like_count"],
             note="Front/back ATM term ratio implied a shared jump.",
             names=sorted(event_like)),
        dict(stage="event confirmed", count=len(earnings),
             note="A grounded Featherless committee confirmed a scheduled "
                  "earnings event with a datetime quorum.",
             names=earnings),
        dict(stage="order-enabled", count=1,
             note="Survived the direct historical option replay and the "
                  "execution-envelope and exit-timing gates.",
             names=["AVGO"]),
    ]

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": scan["mode"],
        "order_enabled": scan["order_enabled"] or semantic["order_enabled"],
        "scan_generated_at": scan["generated_at"],
        "classified_at": semantic["generated_at"],
        "front_expiry": scan["front_expiry"],
        "back_expiry": scan["back_expiry"],
        "market_open_during_scan": scan["market_open"],
        "stages": stages,
        "skip_reasons": [
            {"reason": reason, "count": count}
            for reason, count in Counter(
                _short(r) for r in scan["skipped"].values()).most_common()
        ],
        "candidates": sorted(
            [{
                "ticker": c["ticker"],
                "term_ratio": round(c["term_ratio"], 4),
                "implied_event_move": round(c["implied_event_move"], 4),
                "event_type": accepted.get(c["ticker"], {}).get("event_type", "unclassified"),
                "confidence": accepted.get(c["ticker"], {}).get("confidence"),
                "datetime_quorum": accepted.get(c["ticker"], {}).get("datetime_quorum"),
                "replay": REPLAY.get(c["ticker"]),
            } for c in semantic["candidates"]],
            key=lambda row: -row["term_ratio"]),
        "replay_source": _source_doc(),
        "limitations": scan["limitations"] + [
            "Replay rows are quoted from the postmortem, not recomputed here.",
            "This artifact records a selection process. It is not a backtest "
            "of the selected trade.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=Path, default=None)
    parser.add_argument("--semantic", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    def pick(given: Path | None, *candidates: Path) -> Path:
        if given is not None:
            return given
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise SystemExit(f"none of these exist: {[str(c) for c in candidates]}")

    scan_path = pick(args.scan,
                     ROOT / "evidence" / "event_premium_shadow_parity.json",
                     ROOT / "data" / "event_premium_shadow_parity.json")
    semantic_path = pick(args.semantic,
                         ROOT / "evidence" / "event_premium_semantic.json",
                         ROOT / "data" / "event_premium_semantic.json")
    out = args.out or (ROOT / "evidence" / "event_premium_funnel.json")

    funnel = build(json.loads(scan_path.read_text()),
                   json.loads(semantic_path.read_text()))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(funnel, indent=1, sort_keys=True) + "\n")

    def show(path: Path) -> str:
        try:
            return str(path.relative_to(ROOT))
        except ValueError:
            return str(path)

    print(f"scan       {show(scan_path)}")
    print(f"classifier {show(semantic_path)}")
    print(f"funnel     {show(out)}\n")
    for stage in funnel["stages"]:
        names = stage.get("names")
        shown = f"  {', '.join(names)}" if names else ""
        print(f"  {stage['count']:3d}  {stage['stage']:<18}{shown}")
    if funnel["order_enabled"]:
        print("\nWARNING: a source artifact reported order_enabled=true")
        return 1
    print("\norder_enabled=false in every source artifact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
