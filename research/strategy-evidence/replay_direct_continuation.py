"""Timestamp-respecting first-pass replay of direct news continuation.

This is deliberately a falsification harness, not an options backtest. It uses
Alpaca MCP news and IEX 5-minute bars, enters no earlier than the first completed
bar at/after an article timestamp, subtracts contemporaneous SPY return, and
reports whether a conservative directional headline screen continued afterward.

It does NOT have historical option NBBO, does not model a vertical fill, and does
not test causal spillover. Those limitations are printed with the result.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import re
from collections import defaultdict
from statistics import mean, median
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from trading_bot.options.mcp import MCPClient


ET = ZoneInfo("America/New_York")
DEFAULT_SYMBOLS = (
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    "CRM", "AVGO", "NFLX", "ORCL", "PLTR", "INTC",
)

NOISY = re.compile(
    r"price target|maintains|reiterates|initiates coverage|upgrades?|downgrades?|"
    r"jim cramer|unusual options|whale|what'?s going on|why .* stock|"
    r"stock (?:climbs|rises|falls|drops|surges|slides)", re.I)
BULLISH = re.compile(
    r"raises? (?:its )?(?:revenue |profit )?guidance|boosts? (?:its )?forecast|"
    r"beats? (?:earnings|revenue|estimates)|tops? estimates|"
    r"wins? (?:a )?(?:contract|deal)|secures? (?:a )?(?:contract|deal)|"
    r"fda (?:approves?|approval)|receives? approval|positive (?:trial|data)|"
    r"announces? (?:a )?(?:partnership|buyback)|expands? partnership", re.I)
BEARISH = re.compile(
    r"cuts? (?:its )?(?:revenue |profit )?guidance|lowers? (?:its )?forecast|"
    r"misses? (?:earnings|revenue|estimates)|falls? short of estimates|"
    r"fda (?:rejects?|rejection)|negative (?:trial|data)|"
    r"(?:sec|doj|ftc) investigation|recalls?|files? for (?:an )?offering|"
    r"public offering|private placement|bankruptcy|data breach", re.I)


def direction(headline: str) -> int:
    if NOISY.search(headline):
        return 0
    bull, bear = bool(BULLISH.search(headline)), bool(BEARISH.search(headline))
    return 1 if bull and not bear else -1 if bear and not bull else 0


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ET)


def fetch_news(mcp: MCPClient, *, start: str, end: str, symbols: tuple[str, ...],
               max_pages: int) -> list[dict]:
    rows: dict[int, dict] = {}
    first, final = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    chunk_start = first
    while chunk_start <= final:
        chunk_end = min(final, chunk_start + dt.timedelta(days=6))
        token = None
        for _ in range(max_pages):
            args = {
                "start": chunk_start.isoformat(), "end": chunk_end.isoformat(),
                "sort": "asc", "symbols": ",".join(symbols), "limit": 50,
                "include_content": False,
            }
            if token:
                args["page_token"] = token
            page = mcp.news(**args)
            for item in page.get("news", []):
                rows[item["id"]] = item
            token = page.get("next_page_token")
            if not token:
                break
        chunk_start = chunk_end + dt.timedelta(days=1)
    return sorted(rows.values(), key=lambda row: row["created_at"])


def fetch_bars(mcp: MCPClient, *, start: str, end: str,
               symbols: tuple[str, ...]) -> dict[str, list[dict]]:
    keyed: dict[str, dict[str, dict]] = defaultdict(dict)
    requested = tuple(dict.fromkeys((*symbols, "SPY")))
    first, final = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    chunk_start = first
    # The MCP response exposes a next_page_token that its own input schema does
    # not accept. Small date chunks avoid the server-side truncation without
    # bypassing MCP for the research path.
    while chunk_start <= final:
        chunk_end = min(final, chunk_start + dt.timedelta(days=4))
        for offset in range(0, len(requested), 3):
            batch = requested[offset:offset + 3]
            payload = mcp.stock_bars(
                ",".join(batch), timeframe="5Min",
                start=chunk_start.isoformat(),
                end=(chunk_end + dt.timedelta(days=1)).isoformat(),
                feed="iex", adjustment="all", limit=10_000, sort="asc")
            for symbol, rows in payload.get("bars", {}).items():
                for row in rows:
                    keyed[symbol][row["t"]] = row
        chunk_start = chunk_end + dt.timedelta(days=1)
    out = {symbol: sorted(rows.values(), key=lambda row: row["t"])
           for symbol, rows in keyed.items()}
    return out


def next_index(rows: list[dict], stamp: dt.datetime) -> int | None:
    target = stamp.astimezone(dt.timezone.utc)
    for index, row in enumerate(rows):
        if parse_time(row["t"]).astimezone(dt.timezone.utc) >= target:
            return index
    return None


def close_after(rows: list[dict], index: int, minutes: int) -> float | None:
    steps = math.ceil(minutes / 5)
    target = index + steps
    if target >= len(rows):
        return None
    a, b = parse_time(rows[index]["t"]), parse_time(rows[target]["t"])
    if a.date() != b.date() or (b - a) > dt.timedelta(minutes=minutes + 5):
        return None
    return float(rows[target]["c"])


def aligned_excess(event: dict, bars: dict[str, list[dict]],
                   minutes: int) -> float | None:
    symbol, sign = event["symbol"], event["direction"]
    stamp = event["time"]
    rows, spy = bars.get(symbol, []), bars.get("SPY", [])
    i, j = next_index(rows, stamp), next_index(spy, stamp)
    if i is None or j is None:
        return None
    stock_exit, spy_exit = close_after(rows, i, minutes), close_after(spy, j, minutes)
    if stock_exit is None or spy_exit is None:
        return None
    stock_return = stock_exit / float(rows[i]["c"]) - 1.0
    spy_return = spy_exit / float(spy[j]["c"]) - 1.0
    return sign * (stock_return - spy_return)


def select_events(news: list[dict]) -> list[dict]:
    selected: list[dict] = []
    last_by_symbol: dict[str, dt.datetime] = {}
    for item in news:
        sign = direction(item.get("headline", ""))
        if not sign:
            continue
        stamp = parse_time(item["created_at"])
        if stamp.weekday() >= 5 or not dt.time(9, 35) <= stamp.time() <= dt.time(14, 45):
            continue
        for symbol in item.get("symbols", []):
            if symbol not in DEFAULT_SYMBOLS:
                continue
            previous = last_by_symbol.get(symbol)
            if previous and stamp - previous < dt.timedelta(minutes=60):
                continue
            last_by_symbol[symbol] = stamp
            selected.append({
                "news_id": item["id"], "symbol": symbol, "time": stamp,
                "direction": sign, "headline": item["headline"],
            })
    return selected


def summarize(values: list[float]) -> str:
    if not values:
        return "n=0"
    return (f"n={len(values):3d} mean={mean(values):+7.3%} "
            f"median={median(values):+7.3%} win={sum(v > 0 for v in values)/len(values):6.1%}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-08-28")
    parser.add_argument("--max-pages", type=int, default=12,
                        help="maximum 50-row news pages per seven-day chunk")
    args = parser.parse_args()

    env = dotenv_values(args.env)
    key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit(f"missing Alpaca credentials in {args.env}")

    # Add a day so the final requested session's regular-hours bars are included.
    bar_end = (dt.date.fromisoformat(args.end) + dt.timedelta(days=1)).isoformat()
    with MCPClient(key, secret, live=False, timeout=90) as mcp:
        news = fetch_news(mcp, start=args.start, end=args.end,
                          symbols=DEFAULT_SYMBOLS, max_pages=args.max_pages)
        events = select_events(news)
        bars = fetch_bars(mcp, start=args.start, end=bar_end,
                          symbols=tuple(sorted({e["symbol"] for e in events})))

    print("Alpaca MCP direct-catalyst continuation replay")
    print(f"news rows={len(news)}  screened events={len(events)}  "
          f"symbols={len({e['symbol'] for e in events})}")
    for horizon in (30, 60, 120, 240):
        values = [value for event in events
                  if (value := aligned_excess(event, bars, horizon)) is not None]
        print(f"{horizon:>3}m direction-aligned excess over SPY: {summarize(values)}")

    print("\nExamples (screen input, not winners selected after the fact):")
    for event in events[:12]:
        print(f"{event['time']:%Y-%m-%d %H:%M}  {event['symbol']:<5} "
              f"{'BULL' if event['direction'] > 0 else 'BEAR'}  "
              f"{event['headline'][:105]}")
    print("\nLIMITATIONS: headline regex labels; IEX-only stock bars; beta assumed 1; "
          "no historical option NBBO/fill model; no Featherless ablation; no "
          "causal-spillover test. This can reject a broad thesis, not activate it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
