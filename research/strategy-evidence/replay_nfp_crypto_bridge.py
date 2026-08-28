"""Replay the 08:30–09:25 ET NFP-to-BTC cutoff bridge through Alpaca MCP.

The Sep 4 jobs report is released one hour before the 09:30 grading cutoff.
Options cannot trade in that hour; Alpaca crypto can. This script tests a simple
long-only policy on prior official release dates: buy BTC at the 08:32 bar only
when payrolls miss consensus by at least 50k AND BTC confirms upward, then mark
an exit at 09:25. It prints gross and conservative 30bp round-trip results.

This is a tiny scheduled-event study, not sufficient activation evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import re
from statistics import mean, median
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from trading_bot.options.mcp import MCPClient


# Official BLS release dates. October 2025 was not published during the lapse;
# September and November releases moved to Nov 20 and Dec 16 respectively.
RELEASE_DATES = (
    "2025-01-10", "2025-02-07", "2025-03-07", "2025-04-04",
    "2025-05-02", "2025-06-06", "2025-07-03", "2025-08-01",
    "2025-09-05", "2025-11-20", "2025-12-16",
    "2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03",
    "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07",
)

NFP = re.compile(
    r"(?:Nonfarm Payrolls|Non-Farm Payrolls).*?\s"
    r"(?P<actual>[+\-]?\d+(?:\.\d+)?[KMB]?)\s+(?:Vs|vs)\s+"
    r"(?P<estimate>[+\-]?\d+(?:\.\d+)?[KMB]?)\s+Est", re.I)
ET = ZoneInfo("America/New_York")


def number(value: str) -> float:
    scale = {"K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}
    suffix = value[-1].upper() if value[-1].isalpha() else ""
    base = float(value[:-1] if suffix else value)
    return base * scale.get(suffix, 1.0)


def price_at(rows: list[dict], utc_suffix: str) -> float | None:
    for row in rows:
        if row["t"].endswith(utc_suffix):
            return float(row["c"])
    return None


def nfp_release(mcp: MCPClient, date: str) -> tuple[float, float, str] | None:
    page = mcp.news(
        start=f"{date}T08:25:00-04:00", end=f"{date}T08:40:00-04:00",
        sort="asc", limit=50, include_content=False)
    for article in page.get("news", []):
        match = NFP.search(article.get("headline", ""))
        if match:
            return (number(match.group("actual")), number(match.group("estimate")),
                    article["headline"])
    return None


def btc_window(mcp: MCPClient, date: str) -> list[dict]:
    payload = mcp.call_tool("get_crypto_bars", {
        "symbols": "BTC/USD", "timeframe": "1Min",
        "start": f"{date}T08:25:00-04:00",
        "end": f"{date}T09:26:00-04:00", "limit": 1000, "sort": "asc",
    })
    return payload.get("bars", {}).get("BTC/USD", [])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--miss-threshold-k", type=float, default=50.0)
    parser.add_argument("--round-trip-bps", type=float, default=30.0)
    args = parser.parse_args()
    env = dotenv_values(args.env)
    key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit(f"missing Alpaca credentials in {args.env}")

    rows = []
    with MCPClient(key, secret, live=False, timeout=90) as mcp:
        for date in RELEASE_DATES:
            release = nfp_release(mcp, date)
            bars = btc_window(mcp, date)
            if not release or not bars:
                rows.append((date, None, None, None, False, None, "missing"))
                continue
            actual, estimate, headline = release
            # ET is UTC-5 in Jan/Feb/Nov/Dec, UTC-4 otherwise. Use row-local
            # chronological positions by parsing timestamps instead of fixed UTC.
            parsed = [(dt.datetime.fromisoformat(row["t"].replace("Z", "+00:00")), row)
                      for row in bars]
            def at(hour: int, minute: int):
                candidates = [row for stamp, row in parsed
                              if stamp.astimezone(ET).time() == dt.time(hour, minute)]
                return float(candidates[0]["c"]) if candidates else None

            p829, entry, exit_price = at(8, 29), at(8, 32), at(9, 25)
            if None in (p829, entry, exit_price):
                rows.append((date, actual, estimate, None, False, None, "missing bars"))
                continue
            surprise_k = (actual - estimate) / 1_000.0
            confirmation = entry > p829
            trade = surprise_k <= -args.miss_threshold_k and confirmation
            gross = exit_price / entry - 1.0
            net = gross - args.round_trip_bps / 10_000.0 if trade else 0.0
            rows.append((date, actual, estimate, surprise_k, trade, net, headline))

    print("Alpaca MCP NFP -> BTC cutoff-bridge replay")
    print(f"policy: miss <= -{args.miss_threshold_k:.0f}k, BTC up 08:29->08:32, "
          f"exit 09:25, cost={args.round_trip_bps:.0f}bp")
    print("date        actual  estimate  surprise    trade   net")
    returns = []
    for date, actual, estimate, surprise, trade, net, note in rows:
        if surprise is None:
            print(f"{date}  {'-':>7}  {'-':>8}  {'-':>8}    no    {note}")
            continue
        print(f"{date}  {actual/1000:7.0f}k {estimate/1000:7.0f}k "
              f"{surprise:+8.0f}k   {'yes' if trade else ' no'}  "
              f"{net:+7.3%}" if net is not None else "")
        if trade and net is not None:
            returns.append(net)

    if returns:
        print(f"\ntrades={len(returns)} mean={mean(returns):+.3%} "
              f"median={median(returns):+.3%} "
              f"wins={sum(value > 0 for value in returns)/len(returns):.1%} "
              f"compounded={math.prod(1 + value for value in returns)-1:+.3%}")
    else:
        print("\nNo historical event met the policy.")
    print("LIMITATIONS: tiny non-independent sample; headline consensus may differ "
          "by source; consolidated crypto bars can show zero volume; fixed cost; "
          "long-only; no orderbook replay. Do not activate from this result alone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
