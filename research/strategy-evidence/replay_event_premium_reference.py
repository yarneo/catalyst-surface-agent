"""Compare the six live event surfaces with prior earnings gaps via Alpaca MCP.

Event dates are frozen metadata; all historical prices come from adjusted Alpaca
IEX daily bars through MCP.  This is a stock-gap feasibility study, not an
option-fill backtest and not order authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from dotenv import dotenv_values

from trading_bot.options.mcp import MCPClient
from trading_bot.tournament.audit import AuditLedger
from trading_bot.tournament.event_premium import (
    EventPremiumObservation,
    compare_with_historical_gaps,
)
from trading_bot.options.clock import now_et


ROOT = Path(__file__).resolve().parents[2]
EVENT_DATES = {
    "PANW": ("2023-11-15", "2024-02-20", "2024-05-20", "2024-08-19",
             "2024-11-20", "2025-02-13", "2025-05-20", "2025-08-18",
             "2025-11-19", "2026-02-17", "2026-06-02"),
    "DELL": ("2023-11-30", "2024-02-29", "2024-05-30", "2024-08-29",
             "2024-11-26", "2025-02-27", "2025-05-29", "2025-08-28",
             "2025-11-25", "2026-02-26", "2026-05-28"),
    "SNOW": ("2023-11-29", "2024-02-28", "2024-05-22", "2024-08-21",
             "2024-11-20", "2025-02-26", "2025-05-21", "2025-08-27",
             "2025-12-03", "2026-02-25", "2026-05-27"),
    "HPE": ("2023-11-28", "2024-02-29", "2024-06-04", "2024-09-04",
            "2024-12-05", "2025-03-06", "2025-06-03", "2025-09-03",
            "2025-12-04", "2026-03-09", "2026-06-01"),
    "AVGO": ("2023-12-07", "2024-03-07", "2024-06-12", "2024-09-05",
             "2024-12-12", "2025-03-06", "2025-06-05", "2025-09-04",
             "2025-12-11", "2026-03-04", "2026-06-03"),
    "LULU": ("2023-12-07", "2024-03-21", "2024-06-05", "2024-08-29",
             "2024-12-05", "2025-03-27", "2025-06-05", "2025-09-04",
             "2025-12-11", "2026-03-17", "2026-06-04"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--scan", default="data/event_premium_shadow_parity.json")
    parser.add_argument("--ledger", default="data/event_premium_evidence.jsonl")
    args = parser.parse_args()
    config = dotenv_values(ROOT / args.env)
    key, secret = config.get("ALPACA_API_KEY"), config.get("ALPACA_SECRET_KEY")
    expected_account = str(config.get("ALPACA_ACCOUNT_NUMBER") or "")
    if not key or not secret or not expected_account:
        print("event reference replay failed: configuration is incomplete")
        return 5
    try:
        scan = json.loads((ROOT / args.scan).read_text())
        observations = {
            row["observation"]["symbol"]: EventPremiumObservation(**row["observation"])
            for row in scan["ranked"]
            if row["observation"]["symbol"] in EVENT_DATES
        }
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"event reference replay failed: invalid scan input: {exc}")
        return 5

    observed_at = now_et()
    comparisons = []
    hashes = {}
    try:
        with MCPClient(str(key), str(secret), live=False, paper=True, timeout=90) as mcp:
            tools_available = mcp.tools()
            account = mcp.account()
            if str(account.get("account_number") or "") != expected_account:
                raise RuntimeError("paper-account pin mismatch")
            for symbol, dates in EVENT_DATES.items():
                if symbol not in observations:
                    continue
                payload = mcp.stock_bars(
                    symbol, timeframe="1Day", start="2023-11-01",
                    end="2026-08-29", feed="iex", adjustment="all",
                    limit=1000, sort="asc")
                hashes[symbol] = hashlib.sha256(json.dumps(
                    payload, sort_keys=True, default=str).encode()).hexdigest()
                bars = payload.get("bars", {}).get(symbol, [])
                comparisons.append(compare_with_historical_gaps(
                    observations[symbol], event_dates=dates, daily_bars=bars))
        output = {
            "generated_at": observed_at.isoformat(),
            "mode": "SHADOW_RESEARCH_ONLY",
            "order_enabled": False,
            "comparisons": [asdict(row) for row in comparisons],
            "source": {
                "transport": "Alpaca MCP",
                "tools_used": ["tools/list", "get_account_info", "get_stock_bars"],
                "tools_available": len(tools_available),
                "raw_payload_sha256": hashlib.sha256(json.dumps(
                    hashes, sort_keys=True).encode()).hexdigest(),
                "stock_feed": "iex",
                "adjustment": "all",
            },
            "limitations": [
                "Historical stock gaps are not historical option quotes or fills.",
                "The intrinsic-return estimate omits remaining option time value.",
                "Eleven events per name are too few to validate a trading edge.",
                "No result in this replay has order authority.",
            ],
        }
        audit = AuditLedger(ROOT / args.ledger).append(
            "event_premium_gap_replay", output, recorded_at=observed_at)
    except Exception as exc:  # noqa: BLE001
        print(f"event reference replay failed: {type(exc).__name__}: {exc}")
        return 2

    print("symbol  n  premium  median gap  p75 gap  premium/median  P(gap>premium)  floor med")
    for row in comparisons:
        print(
            f"{row.symbol:<6} {row.sample_size:2d} {row.executable_premium_to_spot:8.2%} "
            f"{row.median_absolute_gap:11.2%} {row.p75_absolute_gap:8.2%} "
            f"{row.premium_to_median_gap:14.2f} {row.gap_exceeds_premium_rate:15.0%} "
            f"{row.intrinsic_floor_median_return:10.1%}")
    print(
        f"GAP REPLAY COMPLETE — {len(comparisons)} names, "
        f"orders enabled=False, audit=#{audit.sequence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
