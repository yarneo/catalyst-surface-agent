#!/usr/bin/env python3
"""Classify event-premium term bumps with a grounded Featherless quorum."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import dotenv_values  # noqa: E402

from trading_bot.options.clock import now_et  # noqa: E402
from trading_bot.options.mcp import MCPClient  # noqa: E402
from trading_bot.tournament.audit import AuditLedger  # noqa: E402
from trading_bot.tournament.catalyst import CatalystFact  # noqa: E402
from trading_bot.tournament.event_semantics import EventSemanticClassifier  # noqa: E402


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, indent=2, sort_keys=True, default=str,
                         allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _news_facts(payload: Any, *, ticker: str) -> list[CatalystFact]:
    rows = payload.get("news", []) if isinstance(payload, dict) else []
    relevant_words = (
        "earnings", "results", "report", "guidance", "ahead", "due",
        "investor day", "launch", "fda", "conference call",
    )
    relevant, context = [], []
    for row in rows:
        if not isinstance(row, dict) or ticker not in (row.get("symbols") or []):
            continue
        try:
            fact = CatalystFact(
                fact_id=f"alpaca:{row['id']}",
                published_at=str(row.get("created_at") or row.get("updated_at")),
                headline=str(row["headline"])[:500],
                summary=str(row.get("summary") or row.get("content") or "")[:4000],
                symbols=(ticker,), source=str(row.get("source") or "alpaca_news"))
            haystack = f"{fact.headline} {fact.summary}".lower()
            (relevant if any(word in haystack for word in relevant_words)
             else context).append(fact)
        except (KeyError, TypeError, ValueError):
            continue
    # Event facts dominate; two recent context rows remain so the model can
    # identify a sympathy move or ordinary news rather than forcing an event.
    return [*relevant[:6], *context[:2]]


def _surface_fact(candidate: dict[str, Any], generated_at: str) -> CatalystFact:
    ticker = candidate["ticker"]
    return CatalystFact(
        f"surface:{ticker}", generated_at,
        f"{ticker} option surface contains a front-expiry term bump",
        "Alpaca MCP option quotes imply a front/back ATM IV ratio of "
        f"{candidate['term_ratio']:.3f} and an event-move magnitude of "
        f"{candidate['implied_event_move']:.2%}. This measurement does not "
        "identify the event, its time, or whether it already occurred.",
        (ticker,), "alpaca_option_surface")


def _official_facts() -> dict[str, CatalystFact]:
    """Frozen primary-source facts for events inside the scoring window.

    The option surface discovers candidates.  These facts ground the semantic
    classification because an LLM cannot recover a schedule that is absent
    from the supplied Alpaca news payload.  A date without an official time is
    deliberately left date-only so the classifier cannot invent one.
    """
    rows = (
        (
            "PANW", "official:panw-fy2026", "2026-08-03T00:00:00-04:00",
            "Palo Alto Networks to announce fiscal 2026 results",
            "Palo Alto Networks investor relations schedules results after "
            "market close on 2026-09-01 and a webcast at 16:30 ET.",
            "investors.paloaltonetworks.com",
        ),
        (
            "DELL", "official:dell-q2-fy2027", "2026-08-18T00:00:00-04:00",
            "Dell Technologies fiscal 2027 second-quarter results",
            "Dell Technologies investor relations schedules its fiscal 2027 "
            "second-quarter results webcast for 2026-09-01 at 15:30 CDT, "
            "which is 16:30 ET.",
            "investors.delltechnologies.com",
        ),
        (
            "SNOW", "official:snow-q2-fy2027", "2026-08-03T00:00:00-04:00",
            "Snowflake to announce fiscal 2027 second-quarter results",
            "Snowflake schedules results after the U.S. market close on "
            "2026-09-02 and its conference call for 14:00 PT, which is "
            "17:00 ET.",
            "snowflake.com",
        ),
        (
            "HPE", "official:hpe-q3-fy2026", "2026-08-29T00:00:00-04:00",
            "HPE fiscal 2026 third-quarter earnings conference call",
            "HPE investor relations lists its fiscal 2026 third-quarter "
            "earnings conference call on 2026-09-02. The source does not "
            "state a time.",
            "investors.hpe.com",
        ),
        (
            "AVGO", "official:avgo-q3-fy2026", "2026-08-28T00:00:00-04:00",
            "Broadcom to announce Q3 FY2026 financial results",
            "Broadcom investor relations schedules results after market close "
            "on 2026-09-02 and the conference call for 17:00 ET.",
            "broadcom.com/company/investors",
        ),
        (
            "LULU", "official:lulu-q2-fy2026", "2026-08-20T00:00:00-04:00",
            "lululemon fiscal 2026 second-quarter earnings call",
            "lululemon schedules its fiscal 2026 second-quarter results for "
            "2026-09-03 and its conference call for 16:30 ET.",
            "corporate.lululemon.com",
        ),
        (
            "CRWD", "official:crwd-investor-briefing", "2026-08-26T00:00:00-04:00",
            "CrowdStrike to webcast investor briefing",
            "CrowdStrike schedules an investor briefing during Fal.Con for "
            "2026-09-02 at 11:30 PDT, which is 14:30 ET. Its earnings release "
            "occurred before this briefing was announced.",
            "ir.crowdstrike.com",
        ),
        (
            "CRM", "official:crm-product-webinar", "2026-08-26T00:00:00-04:00",
            "Salesforce Q2 product adoption and momentum webinar",
            "Salesforce schedules a Q2 fiscal 2027 Product Adoption and "
            "Momentum webinar for 2026-09-01 at 08:00 PT, which is 11:00 ET. "
            "Its second-quarter earnings results were already released.",
            "investor.salesforce.com",
        ),
    )
    return {
        ticker: CatalystFact(fact_id, published_at, headline, summary,
                             (ticker,), source)
        for ticker, fact_id, published_at, headline, summary, source in rows
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/event_premium_shadow_parity.json")
    parser.add_argument("--output", default="data/event_premium_semantic.json")
    parser.add_argument("--ledger", default="data/event_premium_evidence.jsonl")
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--featherless-env", default=".env.local")
    parser.add_argument("--minimum-term-ratio", type=float, default=1.10)
    parser.add_argument("--semantic-batch-size", type=int, default=3)
    args = parser.parse_args()
    if args.semantic_batch_size < 1:
        parser.error("--semantic-batch-size must be positive")

    config = dict(dotenv_values(ROOT / args.featherless_env))
    config.update(dotenv_values(ROOT / args.env))
    key, secret = config.get("ALPACA_API_KEY"), config.get("ALPACA_SECRET_KEY")
    featherless_key = config.get("FEATHERLESS_API_KEY")
    expected_account = str(config.get("ALPACA_ACCOUNT_NUMBER") or "")
    if not key or not secret or not featherless_key or not expected_account:
        print("event semantic classification failed: configuration is incomplete")
        return 5
    try:
        scan = json.loads((ROOT / args.input).read_text())
        candidates = [{
            "ticker": row["observation"]["symbol"],
            "term_ratio": row["observation"]["term_ratio"],
            "implied_event_move": row["observation"]["implied_event_move"],
            "front_expiry": row["observation"]["front_expiry"],
            "back_expiry": row["observation"]["back_expiry"],
        } for row in scan["ranked"]
            if row["observation"]["term_ratio"] >= args.minimum_term_ratio]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"event semantic classification failed: invalid scan input: {exc}")
        return 5
    if not candidates:
        print("event semantic classification failed: scan has no event-like candidates")
        return 5

    facts: list[CatalystFact] = []
    raw_hashes = {}
    observed_at = now_et()
    try:
        with MCPClient(str(key), str(secret), live=False, paper=True, timeout=90) as mcp:
            tools_available = mcp.tools()
            clock = mcp.market_clock()
            account = mcp.account()
            if str(account.get("account_number") or "") != expected_account:
                raise RuntimeError("paper-account pin mismatch")
            for candidate in candidates:
                ticker = candidate["ticker"]
                news = mcp.news(
                    symbols=ticker, start="2026-08-20", limit=20,
                    sort="desc", include_content=True)
                raw_hashes[ticker] = hashlib.sha256(json.dumps(
                    news, sort_keys=True, default=str).encode()).hexdigest()
                facts.extend(_news_facts(news, ticker=ticker))
                facts.append(_surface_fact(candidate, scan["generated_at"]))
            official = _official_facts()
            facts.extend(
                official[candidate["ticker"]]
                for candidate in candidates
                if candidate["ticker"] in official
            )
        # Shared articles can appear under several symbol queries.  The fact ID
        # is the stable source identity, so deduplicate before the model call.
        facts = list({fact.fact_id: fact for fact in facts}.values())
        classifier = EventSemanticClassifier(str(featherless_key))
        accepted = {}
        reasons: list[str] = []
        attempt_rows: list[dict[str, Any]] = []
        for offset in range(0, len(candidates), args.semantic_batch_size):
            batch = candidates[offset:offset + args.semantic_batch_size]
            batch_tickers = {candidate["ticker"] for candidate in batch}
            batch_facts = [
                fact for fact in facts
                if batch_tickers.intersection(fact.symbols)
            ]
            result = classifier.analyze(batch_facts, candidates=batch)
            accepted.update(result.by_ticker())
            reasons.extend(result.reasons)
            attempt_rows.extend({
                "batch": sorted(batch_tickers),
                "model": attempt.model,
                "elapsed_s": attempt.elapsed_s,
                "valid": bool(attempt.events),
                "error": attempt.error,
                "usage": attempt.usage,
            } for attempt in result.attempts)
        output = {
            "generated_at": observed_at.isoformat(),
            "mode": "SHADOW_RESEARCH_ONLY",
            "order_enabled": False,
            "candidates": candidates,
            "accepted": {ticker: asdict(event) for ticker, event in accepted.items()},
            "reasons": reasons,
            "attempts": attempt_rows,
            "source": {
                "transport": ["Alpaca MCP", "Featherless"],
                "mcp_tools_used": ["tools/list", "get_clock", "get_account_info",
                                   "get_news"],
                "mcp_tools_available": len(tools_available),
                "market_open": bool(clock.get("is_open"))
                if isinstance(clock, dict) else None,
                "news_payload_sha256": hashlib.sha256(json.dumps(
                    raw_hashes, sort_keys=True).encode()).hexdigest(),
            },
            "authority": {
                "semantic_output_can_create_trade": False,
                "semantic_output_can_increase_size": False,
                "missing_quorum": "fail closed",
                "missing_datetime_quorum": "not schedulable",
                "semantic_batch_size": args.semantic_batch_size,
            },
        }
        _atomic_json(ROOT / args.output, output)
        audit = AuditLedger(ROOT / args.ledger).append(
            "event_premium_semantic_scan", output, recorded_at=observed_at)
    except Exception as exc:  # noqa: BLE001 — one safe command failure
        print(f"event semantic classification failed: {type(exc).__name__}: {exc}")
        return 2

    print("symbol  event type       status      datetime quorum  confidence")
    for candidate in candidates:
        ticker = candidate["ticker"]
        event = accepted.get(ticker)
        if event is None:
            print(f"{ticker:<7} NO QUORUM")
        else:
            print(
                f"{ticker:<7} {event.event_type:<16} {event.status:<11} "
                f"{str(event.datetime_quorum):<16} {event.confidence:10.0%}")
    print(
        f"SEMANTIC SHADOW COMPLETE — {len(accepted)}/{len(candidates)} classified, "
        f"orders enabled=False, audit=#{audit.sequence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
