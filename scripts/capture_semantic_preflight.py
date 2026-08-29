#!/usr/bin/env python3
"""Audit the live Featherless surprise vector without granting trade authority."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from dotenv import dotenv_values  # noqa: E402

from scripts.run_event_agent import (ELIGIBLE_TICKERS, _fact_hash,  # noqa: E402
                                     _news_facts, _official_fact)
from trading_bot.options.clock import now_et  # noqa: E402
from trading_bot.options.mcp import MCPClient  # noqa: E402
from trading_bot.tournament.audit import AuditLedger  # noqa: E402
from trading_bot.tournament.featherless import FeatherlessClient  # noqa: E402
from trading_bot.tournament.integrity import evaluate_event_integrity  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--featherless-env", default=".env.local")
    parser.add_argument("--ledger", default="data/preflight_evidence.jsonl")
    args = parser.parse_args()

    config = dict(dotenv_values(ROOT / args.featherless_env))
    config.update(dotenv_values(ROOT / args.env))
    key = config.get("ALPACA_API_KEY")
    secret = config.get("ALPACA_SECRET_KEY")
    featherless_key = config.get("FEATHERLESS_API_KEY")
    if not key or not secret or not featherless_key:
        print("semantic preflight failed: missing configured credentials")
        return 5

    now = now_et()
    try:
        with MCPClient(str(key), str(secret), live=False, paper=True) as mcp:
            available_tools = mcp.tools()
            clock = mcp.market_clock()
            news = mcp.news(symbols="AVGO", start="2026-08-28", limit=50,
                            sort="desc", include_content=False)
            server_info = getattr(mcp, "server_info", {})
        facts = [_official_fact(), *_news_facts(news)]
        result = FeatherlessClient(str(featherless_key)).analyze(
            facts, eligible_tickers=ELIGIBLE_TICKERS,
            require_actionable_direction=False)
        integrity = evaluate_event_integrity(result, facts)
        accepted = result.accepted
        vector = None if accepted is None else {
            "novelty": accepted.novelty,
            "surprise": accepted.surprise,
            "confidence": accepted.confidence,
            "direction": accepted.direction,
            "expected_half_life_minutes": accepted.expected_half_life_minutes,
            "primary_tickers": accepted.primary_tickers,
            "secondary_tickers": accepted.secondary_tickers,
            "causal_links": accepted.causal_links,
            "invalidation": accepted.invalidation,
            "source_fact_ids": accepted.source_fact_ids,
        }
        payload = {
            "fact_hash": _fact_hash(facts),
            "facts": facts,
            "surprise_vector": vector,
            "committee": {
                "valid": result.valid,
                "agreement": result.agreement,
                "reason": result.reason,
                "attempts": [{
                    "model": attempt.model,
                    "valid": attempt.assessment is not None,
                    "elapsed_s": attempt.elapsed_s,
                    "error": attempt.error,
                    "repairs": attempt.repairs,
                    "usage": attempt.usage,
                } for attempt in result.attempts],
            },
            "event_integrity": integrity,
            "authority": {
                "mode": "non-expansive veto only",
                "entry_authorized_by_this_artifact": False,
                "policy_gate_changed": False,
            },
            "mcp_lifecycle": {
                "server": server_info,
                "tools_available": len(available_tools),
                "tools_used": ["tools/list", "get_clock", "get_news"],
                "market_open": bool(clock.get("is_open"))
                if isinstance(clock, dict) else None,
                "fact_payload_sha256": hashlib.sha256(
                    _fact_hash(facts).encode()).hexdigest(),
            },
        }
        row = AuditLedger(ROOT / args.ledger).append(
            "featherless_preflight", payload, recorded_at=now)
    except Exception as exc:  # noqa: BLE001 — evidence capture must fail clearly
        print(f"semantic preflight failed: {type(exc).__name__}: {exc}")
        return 2

    valid_models = sum(attempt.assessment is not None for attempt in result.attempts)
    surprise = "n/a" if accepted is None else f"{accepted.surprise:.2f}"
    state = "CLEAR" if integrity.clear else "FAIL-CLOSED"
    print(
        f"SEMANTIC {state} — quorum={valid_models}/{len(result.attempts)}, "
        f"agreement={result.agreement:.0%}, surprise={surprise}, "
        f"integrity_clear={integrity.clear}, MCP tools={len(available_tools)}, "
        f"audit=#{row.sequence}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
