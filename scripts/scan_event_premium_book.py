#!/usr/bin/env python3
"""Run the Event Premium Book as a read-only, shadow-only research scan.

The scanner deliberately contains no order flag and constructs no order.  It
uses Alpaca MCP for account identity, market clock, stock snapshots, and both
option-expiry surfaces; writes a local JSON snapshot; and appends a hash-chained
research row.  A rich/cheap label is a hypothesis, never trading authority.
"""

from __future__ import annotations

import argparse
import datetime as dt
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

from trading_bot.options.clock import ET, now_et  # noqa: E402
from trading_bot.options.mcp import MCPClient  # noqa: E402
from trading_bot.tournament.audit import AuditLedger  # noqa: E402
from trading_bot.tournament.event_premium import (  # noqa: E402
    EventPremiumUnavailable,
    decompose_event_premium,
    rank_cross_section,
    surface_from_mcp,
)


# Stable liquid anchors make the weekend scan reproducible.  MCP most-active
# and mover lists are intentionally not the universe: on the measured Friday
# they were dominated by penny stocks, warrants, and leveraged products.
UNIVERSE: dict[str, str] = {
    "AAPL": "technology", "MSFT": "technology", "NVDA": "technology",
    "AVGO": "technology", "AMD": "technology", "INTC": "technology",
    "MU": "technology", "QCOM": "technology", "MRVL": "technology",
    "ORCL": "technology", "CRM": "technology", "ADBE": "technology",
    "IBM": "technology", "CSCO": "technology", "DELL": "technology",
    "HPE": "technology", "SNOW": "technology", "PLTR": "technology",
    "CRWD": "technology", "PANW": "technology", "ZS": "technology",
    "NOW": "technology", "DDOG": "technology", "NET": "technology",
    "GOOGL": "communication", "META": "communication",
    "NFLX": "communication", "DIS": "communication",
    "AMZN": "consumer_discretionary", "TSLA": "consumer_discretionary",
    "HD": "consumer_discretionary", "LOW": "consumer_discretionary",
    "MCD": "consumer_discretionary", "SBUX": "consumer_discretionary",
    "NKE": "consumer_discretionary", "LULU": "consumer_discretionary",
    "BKNG": "consumer_discretionary", "ABNB": "consumer_discretionary",
    "UBER": "consumer_discretionary", "COST": "consumer_staples",
    "WMT": "consumer_staples", "PG": "consumer_staples",
    "JPM": "financials", "BAC": "financials", "GS": "financials",
    "MS": "financials", "V": "financials", "MA": "financials",
    "PYPL": "financials", "COIN": "financials", "HOOD": "financials",
    "UNH": "healthcare", "LLY": "healthcare", "JNJ": "healthcare",
    "MRK": "healthcare", "PFE": "healthcare", "ABBV": "healthcare",
    "CAT": "industrials", "BA": "industrials", "GE": "industrials",
    "RTX": "industrials", "FDX": "industrials", "XOM": "energy",
    "CVX": "energy",
}


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         default=str).encode()
    return hashlib.sha256(payload).hexdigest()


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


def _stock_rows(mcp: MCPClient, symbols: list[str]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for start in range(0, len(symbols), 35):
        payload = mcp.stock_snapshot(",".join(symbols[start:start + 35]), feed="iex")
        if isinstance(payload, dict):
            rows.update(payload)
    return rows


def _spot_and_liquidity(row: Any) -> tuple[float, float, str]:
    if not isinstance(row, dict):
        raise EventPremiumUnavailable("stock snapshot is missing")
    trade, bar = row.get("latestTrade"), row.get("dailyBar")
    if not isinstance(trade, dict) or not isinstance(bar, dict):
        raise EventPremiumUnavailable("stock snapshot lacks trade or daily bar")
    spot = float(trade["p"])
    dollar_volume = float(bar["v"]) * float(bar.get("vw") or bar["c"])
    stamp = str(trade["t"])
    if spot <= 0 or dollar_volume <= 0:
        raise EventPremiumUnavailable("stock price or dollar volume is non-positive")
    return spot, dollar_volume, stamp


def _parse_symbols(value: str | None, maximum: int) -> list[str]:
    if value:
        requested = [part.strip().upper() for part in value.split(",") if part.strip()]
        unknown = [symbol for symbol in requested if symbol not in UNIVERSE]
        if unknown:
            raise ValueError(f"symbols are not in the declared universe: {unknown}")
        return list(dict.fromkeys(requested))[:maximum]
    return list(UNIVERSE)[:maximum]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--front", default="2026-09-04")
    parser.add_argument("--back", default="2026-09-11")
    parser.add_argument("--symbols", help="comma-separated subset of the declared universe")
    parser.add_argument("--max-symbols", type=int, default=len(UNIVERSE))
    parser.add_argument("--min-dollar-volume", type=float, default=50_000_000)
    parser.add_argument("--output", default="data/event_premium_shadow.json")
    parser.add_argument("--ledger", default="data/event_premium_evidence.jsonl")
    args = parser.parse_args()

    config = dotenv_values(ROOT / args.env)
    key, secret = config.get("ALPACA_API_KEY"), config.get("ALPACA_SECRET_KEY")
    expected_account = str(config.get("ALPACA_ACCOUNT_NUMBER") or "")
    if not key or not secret or not expected_account:
        print("event-premium scan failed: credentials or account pin are missing")
        return 5
    try:
        front_date, back_date = dt.date.fromisoformat(args.front), dt.date.fromisoformat(args.back)
        symbols = _parse_symbols(args.symbols, args.max_symbols)
    except ValueError as exc:
        print(f"event-premium scan failed: {exc}")
        return 5
    if back_date <= front_date:
        print("event-premium scan failed: back expiry must follow front expiry")
        return 5

    observed_at = now_et()
    observations = []
    skipped: dict[str, str] = {}
    source_digests: dict[str, str] = {}
    try:
        with MCPClient(str(key), str(secret), live=False, paper=True, timeout=90) as mcp:
            tools_available = mcp.tools()
            clock = mcp.market_clock()
            account = mcp.account()
            if str(account.get("account_number") or "") != expected_account:
                raise RuntimeError("paper-account pin mismatch")
            stocks = _stock_rows(mcp, symbols)
            source_digests["stock_snapshots"] = _digest(stocks)
            for index, symbol in enumerate(symbols, 1):
                try:
                    spot, dollar_volume, _ = _spot_and_liquidity(stocks.get(symbol))
                    if spot < 10:
                        raise EventPremiumUnavailable("stock price below $10 floor")
                    if dollar_volume < args.min_dollar_volume:
                        raise EventPremiumUnavailable(
                            f"dollar volume ${dollar_volume:,.0f} below floor")
                    low, high = spot * 0.94, spot * 1.06
                    front_payload = mcp.option_chain(
                        symbol, feed="indicative", limit=1000,
                        expiration_date=args.front,
                        strike_price_gte=low, strike_price_lte=high)
                    back_payload = mcp.option_chain(
                        symbol, feed="indicative", limit=1000,
                        expiration_date=args.back,
                        strike_price_gte=low, strike_price_lte=high)
                    source_digests[f"{symbol}:front"] = _digest(front_payload)
                    source_digests[f"{symbol}:back"] = _digest(back_payload)
                    front = surface_from_mcp(
                        payload=front_payload, symbol=symbol, expiry=args.front,
                        spot=spot, observed_at=observed_at)
                    back = surface_from_mcp(
                        payload=back_payload, symbol=symbol, expiry=args.back,
                        spot=spot, observed_at=observed_at)
                    observations.append(decompose_event_premium(
                        front, back, sector=UNIVERSE[symbol]))
                    print(
                        f"[{index:02d}/{len(symbols):02d}] {symbol:<5} "
                        f"term={front.atm_iv / back.atm_iv:5.2f} "
                        f"front_spread={front.total_spread_pct:5.1%}",
                        flush=True)
                except Exception as exc:  # noqa: BLE001 — skip is audited below
                    skipped[symbol] = f"{type(exc).__name__}: {exc}"
                    print(f"[{index:02d}/{len(symbols):02d}] {symbol:<5} skipped", flush=True)

        ranked = rank_cross_section(observations) if len(observations) >= 3 else ()
        event_like = [row for row in ranked if row.observation.term_ratio >= 1.10]
        output = {
            "generated_at": observed_at.isoformat(),
            "mode": "SHADOW_RESEARCH_ONLY",
            "order_enabled": False,
            "validated_edge": False,
            "front_expiry": args.front,
            "back_expiry": args.back,
            "market_open": bool(clock.get("is_open")) if isinstance(clock, dict) else None,
            "universe_size": len(symbols),
            "measured_count": len(observations),
            "event_like_count": len(event_like),
            "skipped": skipped,
            "ranked": [asdict(row) for row in ranked],
            "source": {
                "transport": "Alpaca MCP",
                "tools_used": ["tools/list", "get_clock", "get_account_info",
                               "get_stock_snapshot", "get_option_chain"],
                "tools_available": len(tools_available),
                "payload_sha256": _digest(source_digests),
                "stock_feed": "iex",
                "option_feed": "indicative",
            },
            "limitations": [
                "Closed-market observations are stale and cannot authorize entry.",
                "A term bump does not prove the event identity or timestamp.",
                "A cross-sectional residual is not validated expected P&L.",
                "No candidate in this artifact has order authority.",
            ],
        }
        _atomic_json(ROOT / args.output, output)
        audit = AuditLedger(ROOT / args.ledger).append(
            "event_premium_shadow_scan", output, recorded_at=observed_at)
    except Exception as exc:  # noqa: BLE001 — command reports one safe failure
        print(f"event-premium scan failed: {type(exc).__name__}: {exc}")
        return 2

    print()
    print("symbol  term  event move  variance-days  residual pct  hypothesis")
    for row in sorted(event_like, key=lambda item: -item.percentile):
        observation = row.observation
        print(
            f"{observation.symbol:<6} {observation.term_ratio:5.2f} "
            f"{observation.implied_event_move:10.2%} "
            f"{observation.variance_days:13.2f} {row.percentile:12.0%} "
            f"{row.hypothesis}")
    print()
    print(
        f"SHADOW COMPLETE — measured {len(observations)}/{len(symbols)}, "
        f"event-like {len(event_like)}, skipped {len(skipped)}, "
        f"orders enabled=False, audit=#{audit.sequence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
