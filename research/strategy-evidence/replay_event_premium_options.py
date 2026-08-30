"""Replay next-morning ATM straddles for the six live event-book names.

Uses Alpaca MCP expired-contract metadata and five-minute option trade bars.
Historical trades are not quotes or executable NBBOs, so both a last-trade proxy
and an intentionally adverse entry-high/exit-low envelope are reported.  The
result remains research-only and cannot authorize either long or short options.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from trading_bot.options.clock import now_et
from trading_bot.options.mcp import MCPClient
from trading_bot.tournament.audit import AuditLedger


ROOT = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")
EVENT_DATES = {
    "PANW": ("2024-08-19", "2024-11-20", "2025-02-13", "2025-05-20",
             "2025-08-18", "2025-11-19", "2026-02-17", "2026-06-02"),
    "DELL": ("2024-08-29", "2024-11-26", "2025-02-27", "2025-05-29",
             "2025-08-28", "2025-11-25", "2026-02-26", "2026-05-28"),
    "SNOW": ("2024-08-21", "2024-11-20", "2025-02-26", "2025-05-21",
             "2025-08-27", "2025-12-03", "2026-02-25", "2026-05-27"),
    "HPE": ("2024-09-04", "2024-12-05", "2025-03-06", "2025-06-03",
            "2025-09-03", "2025-12-04", "2026-03-09", "2026-06-01"),
    "AVGO": ("2024-09-05", "2024-12-12", "2025-03-06", "2025-06-05",
             "2025-09-04", "2025-12-11", "2026-03-04", "2026-06-03"),
    "LULU": ("2024-08-29", "2024-12-05", "2025-03-27", "2025-06-05",
             "2025-09-04", "2025-12-11", "2026-03-17", "2026-06-04"),
}


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ET)


def next_weekday(value: dt.date) -> dt.date:
    out = value + dt.timedelta(days=1)
    while out.weekday() >= 5:
        out += dt.timedelta(days=1)
    return out


def friday_on_or_after(value: dt.date) -> dt.date:
    return value + dt.timedelta(days=(4 - value.weekday()) % 7)


def stamp(day: dt.date, hour: int, minute: int) -> str:
    return dt.datetime.combine(day, dt.time(hour, minute), ET).isoformat()


def historical_closes(mcp: MCPClient, symbol: str) -> dict[dt.date, float]:
    # Raw prices match the strikes that existed at the event date. Adjusted
    # prices can make a pre-split $300 option look far from a $150 stock.
    payload = mcp.stock_bars(
        symbol, timeframe="1Day", start="2024-08-01", end="2026-08-29",
        feed="iex", adjustment="raw", limit=1000, sort="asc")
    rows = payload.get("bars", {}).get(symbol, [])
    return {parse_time(row["t"]).date(): float(row["c"]) for row in rows}


def atm_contracts(mcp: MCPClient, symbol: str, day: dt.date,
                  spot: float) -> tuple[float, str, str] | None:
    expiry = friday_on_or_after(day)
    payload = mcp.option_contracts(
        underlying_symbols=symbol, status="inactive",
        expiration_date=expiry.isoformat(), strike_price_gte=spot * 0.90,
        strike_price_lte=spot * 1.10, limit=1000)
    rows = payload.get("option_contracts", [])
    by_strike: dict[float, dict[str, str]] = {}
    for row in rows:
        if str(row.get("multiplier")) != "100":
            continue
        strike = float(row["strike_price"])
        by_strike.setdefault(strike, {})[row["type"]] = row["symbol"]
    common = [strike for strike, sides in by_strike.items()
              if {"call", "put"} <= sides.keys()]
    if not common:
        return None
    strike = min(common, key=lambda value: (abs(value - spot), value))
    return strike, by_strike[strike]["call"], by_strike[strike]["put"]


def option_windows(mcp: MCPClient, event_day: dt.date,
                   symbols: tuple[str, str]) -> dict[str, dict[str, float]]:
    trade_day = next_weekday(event_day)
    payload = mcp.option_bars(
        ",".join(symbols), "5Min", start=stamp(event_day, 15, 25),
        end=stamp(trade_day, 10, 0), limit=1000, sort="asc")
    out = {}
    for symbol in symbols:
        rows = payload.get("bars", {}).get(symbol, [])
        entry = [row for row in rows
                 if parse_time(row["t"]).date() == event_day
                 and dt.time(15, 30) <= parse_time(row["t"]).time() <= dt.time(15, 55)]
        exits = [row for row in rows
                 if parse_time(row["t"]).date() == trade_day
                 and dt.time(9, 40) <= parse_time(row["t"]).time() <= dt.time(9, 50)]
        if not entry or not exits:
            continue
        out[symbol] = {
            "entry_last": float(entry[-1]["c"]),
            "exit_last": float(exits[-1]["c"]),
            "entry_bad": max(float(row["h"]) for row in entry),
            "exit_bad": min(float(row["l"]) for row in exits),
            "entry_trades": sum(int(row.get("n") or 0) for row in entry),
            "exit_trades": sum(int(row.get("n") or 0) for row in exits),
        }
    return out


def summary(rows: list[dict]) -> dict:
    complete = [row for row in rows if "last_return" in row]
    if not complete:
        return {"n": 0}
    last = [row["last_return"] for row in complete]
    adverse = [row["adverse_return"] for row in complete]
    return {
        "n": len(complete),
        "last_mean": mean(last), "last_median": median(last),
        "last_win_rate": sum(value > 0 for value in last) / len(last),
        "last_worst": min(last), "last_best": max(last),
        "adverse_mean": mean(adverse), "adverse_median": median(adverse),
        "adverse_win_rate": sum(value > 0 for value in adverse) / len(adverse),
        "adverse_worst": min(adverse), "adverse_best": max(adverse),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--output", default="data/event_premium_options_replay.json")
    parser.add_argument("--ledger", default="data/event_premium_evidence.jsonl")
    args = parser.parse_args()
    config = dotenv_values(ROOT / args.env)
    key, secret = config.get("ALPACA_API_KEY"), config.get("ALPACA_SECRET_KEY")
    expected_account = str(config.get("ALPACA_ACCOUNT_NUMBER") or "")
    if not key or not secret or not expected_account:
        print("event option replay failed: configuration is incomplete")
        return 5

    observed_at = now_et()
    results: dict[str, list[dict]] = {}
    try:
        with MCPClient(str(key), str(secret), live=False, paper=True, timeout=90) as mcp:
            tools_available = mcp.tools()
            account = mcp.account()
            if str(account.get("account_number") or "") != expected_account:
                raise RuntimeError("paper-account pin mismatch")
            for symbol, date_values in EVENT_DATES.items():
                closes = historical_closes(mcp, symbol)
                symbol_rows = []
                for index, value in enumerate(date_values, 1):
                    day = dt.date.fromisoformat(value)
                    spot = closes.get(day)
                    selected = atm_contracts(mcp, symbol, day, spot) if spot else None
                    if not spot or not selected:
                        symbol_rows.append({"date": value, "reason": "missing spot/contracts"})
                        print(f"{symbol} [{index}/8] missing spot/contracts", flush=True)
                        continue
                    strike, call, put = selected
                    windows = option_windows(mcp, day, (call, put))
                    if call not in windows or put not in windows:
                        missing = "call" if call not in windows else "put"
                        symbol_rows.append({"date": value, "spot": spot,
                                            "strike": strike,
                                            "reason": f"missing {missing} trade window"})
                        print(f"{symbol} [{index}/8] missing {missing} window", flush=True)
                        continue
                    entry = windows[call]["entry_last"] + windows[put]["entry_last"]
                    exit_value = windows[call]["exit_last"] + windows[put]["exit_last"]
                    entry_bad = windows[call]["entry_bad"] + windows[put]["entry_bad"]
                    exit_bad = windows[call]["exit_bad"] + windows[put]["exit_bad"]
                    symbol_rows.append({
                        "date": value, "spot": spot, "strike": strike,
                        "entry_last": entry, "exit_last": exit_value,
                        "premium_to_spot": entry / spot,
                        "last_return": exit_value / entry - 1.0,
                        "entry_bad": entry_bad, "exit_bad": exit_bad,
                        "adverse_return": exit_bad / entry_bad - 1.0,
                        "minimum_entry_trades": min(
                            windows[call]["entry_trades"], windows[put]["entry_trades"]),
                        "minimum_exit_trades": min(
                            windows[call]["exit_trades"], windows[put]["exit_trades"]),
                    })
                    print(f"{symbol} [{index}/8] complete", flush=True)
                results[symbol] = symbol_rows
        summaries = {symbol: summary(rows) for symbol, rows in results.items()}
        output = {
            "generated_at": observed_at.isoformat(),
            "mode": "SHADOW_RESEARCH_ONLY", "order_enabled": False,
            "results": results, "summaries": summaries,
            "source": {
                "transport": "Alpaca MCP",
                "tools_used": ["tools/list", "get_account_info", "get_stock_bars",
                               "get_option_contracts", "get_option_bars"],
                "tools_available": len(tools_available),
                "stock_feed": "iex", "stock_adjustment": "raw",
            },
            "limitations": [
                "Historical option trades are not quotes, NBBOs, or fills.",
                "Call and put observations may occur at different instants.",
                "The adverse envelope intentionally overstates execution cost.",
                "Missing one leg excludes and reports the event.",
                "Eight dates per name are insufficient to validate a rich/cheap residual.",
                "No result has order authority.",
            ],
        }
        path = ROOT / args.output
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        audit = AuditLedger(ROOT / args.ledger).append(
            "event_premium_option_replay",
            {"output_sha256": digest, "summaries": summaries,
             "limitations": output["limitations"], "source": output["source"]},
            recorded_at=observed_at)
    except Exception as exc:  # noqa: BLE001
        print(f"event option replay failed: {type(exc).__name__}: {exc}")
        return 2

    print()
    print("symbol  n  last mean  last median  win rate  adverse mean  adverse median")
    for symbol, row in summaries.items():
        if not row["n"]:
            print(f"{symbol:<6}  0")
            continue
        print(
            f"{symbol:<6} {row['n']:2d} {row['last_mean']:+10.1%} "
            f"{row['last_median']:+12.1%} {row['last_win_rate']:9.0%} "
            f"{row['adverse_mean']:+13.1%} {row['adverse_median']:+15.1%}")
    print(f"OPTION REPLAY COMPLETE — orders enabled=False, audit=#{audit.sequence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
