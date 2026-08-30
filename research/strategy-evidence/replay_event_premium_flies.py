#!/usr/bin/env python3
"""Replay defined-risk short ATM iron flies around the live event set.

The center is the nearest common call/put strike.  Wings are the smallest
symmetric listed width at least as wide as the event-day ATM straddle premium.
This makes the structure deterministic and prevents a hindsight search over
strikes.  Alpaca MCP five-minute trades provide both a last-trade proxy and a
deliberately adverse four-leg envelope.  Neither is an NBBO or fill backtest,
and this research script has no order path.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import tempfile
import os
from pathlib import Path
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from trading_bot.options.clock import now_et
from trading_bot.options.condor import exact_max_loss
from trading_bot.options.mcp import MCPClient
from trading_bot.options.spreads import Leg
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


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
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
    payload = mcp.stock_bars(
        symbol, timeframe="1Day", start="2024-08-01", end="2026-08-29",
        feed="iex", adjustment="raw", limit=1000, sort="asc")
    return {
        parse_time(row["t"]).date(): float(row["c"])
        for row in payload.get("bars", {}).get(symbol, [])
    }


def contract_book(mcp: MCPClient, symbol: str, day: dt.date,
                  spot: float) -> dict[tuple[float, str], str]:
    payload = mcp.option_contracts(
        underlying_symbols=symbol, status="inactive",
        expiration_date=friday_on_or_after(day).isoformat(),
        strike_price_gte=spot * 0.75, strike_price_lte=spot * 1.25, limit=1000)
    out = {}
    for row in payload.get("option_contracts", []):
        if str(row.get("multiplier")) != "100":
            continue
        right = "C" if row.get("type") == "call" else "P"
        out[(float(row["strike_price"]), right)] = row["symbol"]
    return out


def windows(mcp: MCPClient, event_day: dt.date,
            symbols: tuple[str, ...]) -> dict[str, dict[str, float]]:
    payload = mcp.option_bars(
        ",".join(symbols), "5Min", start=stamp(event_day, 15, 25),
        end=stamp(next_weekday(event_day), 10, 0), limit=1000, sort="asc")
    out = {}
    for symbol in symbols:
        rows = payload.get("bars", {}).get(symbol, [])
        entry = [row for row in rows
                 if parse_time(row["t"]).date() == event_day
                 and dt.time(15, 30) <= parse_time(row["t"]).time() <= dt.time(15, 55)]
        exits = [row for row in rows
                 if parse_time(row["t"]).date() == next_weekday(event_day)
                 and dt.time(9, 40) <= parse_time(row["t"]).time() <= dt.time(9, 50)]
        if not entry or not exits:
            continue
        out[symbol] = {
            "entry_last": float(entry[-1]["c"]),
            "entry_low": min(float(row["l"]) for row in entry),
            "entry_high": max(float(row["h"]) for row in entry),
            "exit_last": float(exits[-1]["c"]),
            "exit_low": min(float(row["l"]) for row in exits),
            "exit_high": max(float(row["h"]) for row in exits),
            "entry_trades": sum(int(row.get("n") or 0) for row in entry),
            "exit_trades": sum(int(row.get("n") or 0) for row in exits),
        }
    return out


def choose_center(book: dict[tuple[float, str], str], spot: float) -> float | None:
    common = {strike for strike, right in book if right == "C"} & {
        strike for strike, right in book if right == "P"
    }
    return min(common, key=lambda strike: (abs(strike - spot), strike)) if common else None


def choose_wings(book: dict[tuple[float, str], str], center: float,
                 target: float) -> tuple[float, str, str] | None:
    call_widths = {
        strike - center for strike, right in book if right == "C" and strike > center
    }
    put_widths = {
        center - strike for strike, right in book if right == "P" and strike < center
    }
    common = sorted(call_widths & put_widths)
    if not common:
        return None
    qualifying = [width for width in common if width >= target]
    width = qualifying[0] if qualifying else common[-1]
    return width, book[(center + width, "C")], book[(center - width, "P")]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [row for row in rows if row.get("last_return_on_risk") is not None]
    adverse = [row["adverse_return_on_risk"] for row in complete
               if row.get("adverse_return_on_risk") is not None]
    if not complete:
        return {"n": 0, "adverse_n": 0}
    returns = [row["last_return_on_risk"] for row in complete]
    return {
        "n": len(complete),
        "last_mean": mean(returns), "last_median": median(returns),
        "last_win_rate": sum(value > 0 for value in returns) / len(returns),
        "last_worst": min(returns), "last_best": max(returns),
        "adverse_n": len(adverse),
        "adverse_mean": mean(adverse) if adverse else None,
        "adverse_median": median(adverse) if adverse else None,
        "adverse_win_rate": (sum(value > 0 for value in adverse) / len(adverse)
                             if adverse else None),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--output", default="data/event_premium_flies_replay.json")
    parser.add_argument("--ledger", default="data/event_premium_evidence.jsonl")
    args = parser.parse_args()
    config = dotenv_values(ROOT / args.env)
    key, secret = config.get("ALPACA_API_KEY"), config.get("ALPACA_SECRET_KEY")
    expected_account = str(config.get("ALPACA_ACCOUNT_NUMBER") or "")
    if not key or not secret or not expected_account:
        print("event iron-fly replay failed: configuration is incomplete")
        return 5

    observed_at = now_et()
    results: dict[str, list[dict[str, Any]]] = {}
    try:
        with MCPClient(str(key), str(secret), live=False, paper=True, timeout=90) as mcp:
            tools_available = mcp.tools()
            account = mcp.account()
            if str(account.get("account_number") or "") != expected_account:
                raise RuntimeError("paper-account pin mismatch")
            for symbol, dates in EVENT_DATES.items():
                closes = historical_closes(mcp, symbol)
                symbol_rows = []
                for index, value in enumerate(dates, 1):
                    day = dt.date.fromisoformat(value)
                    spot = closes.get(day)
                    book = contract_book(mcp, symbol, day, spot) if spot else {}
                    center = choose_center(book, spot) if spot else None
                    if center is None:
                        symbol_rows.append({"date": value, "reason": "missing spot/center"})
                        print(f"{symbol} [{index}/8] missing spot/center", flush=True)
                        continue
                    short_call, short_put = book[(center, "C")], book[(center, "P")]
                    center_windows = windows(mcp, day, (short_call, short_put))
                    if not {short_call, short_put} <= center_windows.keys():
                        symbol_rows.append({"date": value, "reason": "missing center window"})
                        print(f"{symbol} [{index}/8] missing center window", flush=True)
                        continue
                    atm_premium = (center_windows[short_call]["entry_last"]
                                   + center_windows[short_put]["entry_last"])
                    selected = choose_wings(book, center, atm_premium)
                    if selected is None:
                        symbol_rows.append({"date": value, "reason": "missing symmetric wings"})
                        print(f"{symbol} [{index}/8] missing symmetric wings", flush=True)
                        continue
                    width, long_call, long_put = selected
                    symbols = (short_call, short_put, long_call, long_put)
                    observed = windows(mcp, day, symbols)
                    if not set(symbols) <= observed.keys():
                        symbol_rows.append({"date": value, "center": center,
                                            "width": width,
                                            "reason": "missing four-leg window"})
                        print(f"{symbol} [{index}/8] missing four-leg window", flush=True)
                        continue

                    credit = (observed[short_call]["entry_last"]
                              + observed[short_put]["entry_last"]
                              - observed[long_call]["entry_last"]
                              - observed[long_put]["entry_last"])
                    exit_raw = (observed[short_call]["exit_last"]
                                + observed[short_put]["exit_last"]
                                - observed[long_call]["exit_last"]
                                - observed[long_put]["exit_last"])
                    legs = (
                        Leg(short_call, "sell"), Leg(short_put, "sell"),
                        Leg(long_call, "buy"), Leg(long_put, "buy"),
                    )
                    if not 0 < credit < width:
                        symbol_rows.append({"date": value, "center": center,
                                            "width": width, "credit": credit,
                                            "reason": "invalid last-trade credit"})
                        print(f"{symbol} [{index}/8] invalid credit", flush=True)
                        continue
                    max_loss = exact_max_loss(legs, -credit)
                    exit_debit = min(width, max(0.0, exit_raw))

                    bad_credit = (observed[short_call]["entry_low"]
                                  + observed[short_put]["entry_low"]
                                  - observed[long_call]["entry_high"]
                                  - observed[long_put]["entry_high"])
                    bad_exit_raw = (observed[short_call]["exit_high"]
                                    + observed[short_put]["exit_high"]
                                    - observed[long_call]["exit_low"]
                                    - observed[long_put]["exit_low"])
                    bad_return = None
                    if 0 < bad_credit < width:
                        bad_loss = exact_max_loss(legs, -bad_credit)
                        bad_exit = min(width, max(0.0, bad_exit_raw))
                        bad_return = (bad_credit - bad_exit) / bad_loss

                    symbol_rows.append({
                        "date": value, "spot": spot, "center": center,
                        "wing_width": width, "atm_straddle_premium": atm_premium,
                        "entry_credit": credit, "exit_debit": exit_debit,
                        "max_loss": max_loss,
                        "last_return_on_risk": (credit - exit_debit) / max_loss,
                        "adverse_entry_credit": bad_credit,
                        "adverse_exit_debit_raw": bad_exit_raw,
                        "adverse_return_on_risk": bad_return,
                        "minimum_entry_trades": min(
                            int(observed[item]["entry_trades"]) for item in symbols),
                        "minimum_exit_trades": min(
                            int(observed[item]["exit_trades"]) for item in symbols),
                    })
                    print(f"{symbol} [{index}/8] complete", flush=True)
                results[symbol] = symbol_rows
        summaries = {symbol: summarize(rows) for symbol, rows in results.items()}
        output = {
            "generated_at": observed_at.isoformat(),
            "mode": "SHADOW_RESEARCH_ONLY", "order_enabled": False,
            "structure": "short_atm_iron_fly",
            "wing_rule": "smallest symmetric listed width >= ATM straddle premium",
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
                "Four leg observations may occur at different instants.",
                "The adverse envelope can make an otherwise tradable credit unavailable.",
                "Missing any leg excludes and reports the event.",
                "Eight dates per name are insufficient to validate a rich residual.",
                "No result has order authority.",
            ],
        }
        path = ROOT / args.output
        _atomic_json(path, output)
        audit = AuditLedger(ROOT / args.ledger).append(
            "event_premium_iron_fly_replay",
            {"output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
             "summaries": summaries, "limitations": output["limitations"],
             "source": output["source"]}, recorded_at=observed_at)
    except Exception as exc:  # noqa: BLE001 — one safe research command
        print(f"event iron-fly replay failed: {type(exc).__name__}: {exc}")
        return 2

    print()
    print("symbol  n  last mean  last median  win rate  adverse n  adverse mean")
    for symbol, row in summaries.items():
        if not row["n"]:
            print(f"{symbol:<6}  0")
            continue
        bad = "n/a" if row["adverse_mean"] is None else f"{row['adverse_mean']:+.1%}"
        print(f"{symbol:<6} {row['n']:2d} {row['last_mean']:+10.1%} "
              f"{row['last_median']:+12.1%} {row['last_win_rate']:9.0%} "
              f"{row['adverse_n']:10d} {bad:>13}")
    print(f"IRON-FLY REPLAY COMPLETE — orders enabled=False, audit=#{audit.sequence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
