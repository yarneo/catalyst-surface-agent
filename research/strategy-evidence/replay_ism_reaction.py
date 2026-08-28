"""Replay timestamped ISM releases and the post-release equity reaction.

The measured week contains Manufacturing PMI Tue Sep 1 and Services PMI Thu
Sep 3 at 10:00 ET. This study asks one predeclared question through Alpaca MCP:
after a >=10bp SPY move from 09:59 to 10:03, does that direction continue or
fade through 10:30/11:00? Headlines verify the scheduled release and retain the
reported actual/estimate surprise. Stock returns are feasibility evidence only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from statistics import mean, median
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from trading_bot.options.mcp import MCPClient


ET = ZoneInfo("America/New_York")
HOLIDAYS = {
    dt.date(2025, 1, 1), dt.date(2025, 1, 20), dt.date(2025, 2, 17),
    dt.date(2025, 4, 18), dt.date(2025, 5, 26), dt.date(2025, 6, 19),
    dt.date(2025, 7, 4), dt.date(2025, 9, 1), dt.date(2025, 11, 27),
    dt.date(2025, 12, 25),
    dt.date(2026, 1, 1), dt.date(2026, 1, 19), dt.date(2026, 2, 16),
    dt.date(2026, 4, 3), dt.date(2026, 5, 25), dt.date(2026, 6, 19),
    dt.date(2026, 7, 3), dt.date(2026, 9, 7), dt.date(2026, 11, 26),
    dt.date(2026, 12, 25),
}
PMI = re.compile(
    r"ISM (?P<kind>Manufacturing|Non-Manufacturing) PMI.*?"
    r"(?P<actual>\d+(?:\.\d+)?)\s+Vs\s+"
    r"(?P<estimate>\d+(?:\.\d+)?)\s+Est", re.I)


def business_days(year: int, month: int) -> list[dt.date]:
    day = dt.date(year, month, 1)
    out = []
    while day.month == month:
        if day.weekday() < 5 and day not in HOLIDAYS:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def release_dates() -> list[tuple[str, dt.date]]:
    out = []
    for year in (2025, 2026):
        final_month = 12 if year == 2025 else 8
        for month in range(1, final_month + 1):
            days = business_days(year, month)
            out.extend((("manufacturing", days[0]), ("services", days[2])))
    return out


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ET)


def stamp(day: dt.date, when: dt.time) -> str:
    return dt.datetime.combine(day, when, tzinfo=ET).isoformat()


def at(rows: list[dict], when: dt.time) -> float | None:
    for row in rows:
        if parse_time(row["t"]).time() == when:
            return float(row["c"])
    return None


def headline(mcp: MCPClient, day: dt.date, expected_kind: str):
    page = mcp.news(
        start=stamp(day, dt.time(9, 55)), end=stamp(day, dt.time(10, 10)),
        sort="asc", limit=50,
        include_content=False)
    for article in page.get("news", []):
        match = PMI.search(article.get("headline", ""))
        if not match:
            continue
        kind = "manufacturing" if match.group("kind").lower() == "manufacturing" else "services"
        if kind == expected_kind:
            return float(match.group("actual")), float(match.group("estimate")), article["headline"]
    return None


def bars(mcp: MCPClient, day: dt.date) -> dict[str, list[dict]]:
    payload = mcp.stock_bars(
        "SPY,QQQ", timeframe="1Min",
        start=stamp(day, dt.time(9, 55)), end=stamp(day, dt.time(11, 1)),
        feed="iex",
        adjustment="all", limit=1000, sort="asc")
    return payload.get("bars", {})


def summary(label: str, values: list[float]) -> None:
    if not values:
        print(f"{label:<31} n=0")
        return
    print(f"{label:<31} n={len(values):2d} mean={mean(values):+7.3%} "
          f"median={median(values):+7.3%} "
          f"win={sum(value > 0 for value in values)/len(values):6.1%}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--initial-bps", type=float, default=10.0)
    args = parser.parse_args()
    env = dotenv_values(args.env)
    key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit(f"missing Alpaca credentials in {args.env}")

    observations = []
    with MCPClient(key, secret, live=False, timeout=90) as mcp:
        for kind, day in release_dates():
            release, market = headline(mcp, day, kind), bars(mcp, day)
            if not release or "SPY" not in market:
                observations.append({"kind": kind, "day": day, "missing": True})
                continue
            p959, p1003 = at(market["SPY"], dt.time(9, 59)), at(market["SPY"], dt.time(10, 3))
            p1030, p1100 = at(market["SPY"], dt.time(10, 30)), at(market["SPY"], dt.time(11, 0))
            if None in (p959, p1003, p1030, p1100):
                observations.append({"kind": kind, "day": day, "missing": True})
                continue
            actual, estimate, title = release
            initial = p1003 / p959 - 1.0
            sign = 1 if initial > 0 else -1
            observations.append({
                "kind": kind, "day": day, "missing": False,
                "surprise": actual - estimate, "initial": initial,
                "active": abs(initial) >= args.initial_bps / 10_000.0,
                "cont30": sign * (p1030 / p1003 - 1.0),
                "cont60": sign * (p1100 / p1003 - 1.0),
                "title": title,
            })

    active = [row for row in observations if row.get("active")]
    print("Alpaca MCP ISM reaction replay")
    print(f"gate: |SPY 09:59->10:03| >= {args.initial_bps:.0f}bp; evaluate continuation")
    print("date        kind  surprise  initial   cont30   cont60")
    for row in observations:
        if row.get("missing"):
            continue
        print(f"{row['day']}  {row['kind'][0].upper():>4} "
              f"{row['surprise']:+8.1f} {row['initial']:+8.2%} "
              f"{row['cont30']:+8.2%} {row['cont60']:+8.2%}" +
              ("" if row["active"] else "  below gate"))
    print()
    summary("continuation to 10:30", [row["cont30"] for row in active])
    summary("continuation to 11:00", [row["cont60"] for row in active])
    summary("fade to 10:30", [-row["cont30"] for row in active])
    summary("fade to 11:00", [-row["cont60"] for row in active])
    print("LIMITATIONS: IEX stock bars, fixed 4-minute reaction window, no option "
          "NBBO/IV/fill model, no component-level semantic classifier, and a "
          "small scheduled-event sample. Threshold is not optimized here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
