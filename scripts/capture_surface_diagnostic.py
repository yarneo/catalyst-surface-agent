#!/usr/bin/env python3
"""Capture a read-only Alpaca MCP option-smile artifact in the audit chain."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import dotenv_values  # noqa: E402

from trading_bot.options.clock import ET, now_et  # noqa: E402
from trading_bot.options.mcp import MCPClient  # noqa: E402
from trading_bot.tournament.audit import AuditLedger  # noqa: E402
from trading_bot.tournament.scheduled import ScheduledEventPolicy  # noqa: E402
from trading_bot.tournament.surface_diagnostic import diagnose_surface  # noqa: E402


POLICY = ScheduledEventPolicy()


def _spot(snapshot: Any) -> tuple[float, dt.datetime]:
    try:
        trade = snapshot[POLICY.underlying]["latestTrade"]
        price = float(trade["p"])
        stamp = dt.datetime.fromisoformat(
            str(trade["t"]).replace("Z", "+00:00")).astimezone(ET)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("stock snapshot lacks a valid latest AVGO trade") from exc
    if price <= 0:
        raise ValueError("stock snapshot price is non-positive")
    return price, stamp


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--ledger", default="data/preflight_evidence.jsonl")
    args = parser.parse_args()

    config = dotenv_values(ROOT / args.env)
    key, secret = config.get("ALPACA_API_KEY"), config.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("surface diagnostic failed: missing Alpaca credentials")
        return 5

    observed_at = now_et()
    try:
        with MCPClient(str(key), str(secret), live=False, paper=True) as mcp:
            clock = mcp.market_clock()
            snapshot = mcp.stock_snapshot(POLICY.underlying, feed="iex")
            spot, spot_at = _spot(snapshot)
            chain = mcp.option_chain(
                POLICY.underlying, feed="indicative", limit=1000,
                expiration_date=POLICY.expiry,
                strike_price_gte=spot * 0.90,
                strike_price_lte=spot * 1.10,
            )
        diagnostic = diagnose_surface(
            payload=chain, spot=spot, observed_at=observed_at, policy=POLICY)
        market_open = bool(clock.get("is_open")) if isinstance(clock, dict) else False
        freshness_eligible = (
            market_open
            and 0 <= (observed_at - spot_at).total_seconds() <= 90
            and 0 <= diagnostic.max_quote_age_s <= POLICY.max_quote_age_s
        )
        payload = {
            "diagnostic": diagnostic,
            "market_open": market_open,
            "spot_observed_at": spot_at,
            "fresh_for_entry": freshness_eligible,
            "source": {
                "transport": "Alpaca MCP",
                "tools": ["get_clock", "get_stock_snapshot", "get_option_chain"],
                "stock_feed": "iex",
                "option_feed": "indicative",
                "raw_payload_sha256": _digest({"snapshot": snapshot, "chain": chain}),
            },
            "interpretation": (
                "Closed-market or stale observations remain useful as a dated "
                "preflight diagnostic but can never satisfy the frozen entry gate."
                if not freshness_eligible else
                "Observation was fresh when captured; curvature remains diagnostic-only."
            ),
        }
        row = AuditLedger(ROOT / args.ledger).append(
            "surface_diagnostic", payload, recorded_at=observed_at)
    except Exception as exc:  # noqa: BLE001 — command returns a clear failed artifact
        print(f"surface diagnostic failed: {type(exc).__name__}: {exc}")
        return 2

    print(
        f"SURFACE OK — {diagnostic.point_count} paired strikes, "
        f"{diagnostic.shape}, curvature="
        f"{diagnostic.quadratic_curvature_per_log_moneyness_pct2:+.6f}, "
        f"premium/spot={diagnostic.executable_premium_to_spot:.2%}, "
        f"fresh_for_entry={freshness_eligible}, audit=#{row.sequence}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
