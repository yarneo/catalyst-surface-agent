"""Replay AVGO post-earnings information propagation through Alpaca MCP.

Upcoming AVGO results are official for Wed Sep 2 after the close. This study
uses ten prior official release dates. At 09:45 the next session it observes the
AVGO direction, then tests direct continuation and same-direction lagging peers
(NVDA, AMD, MRVL) through 11:45 and 15:30. It uses adjusted IEX stock bars and
does not pretend to be a historical options-fill backtest.
"""

from __future__ import annotations

import argparse
import datetime as dt
from statistics import mean, median
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from trading_bot.options.mcp import MCPClient


ET = ZoneInfo("America/New_York")
EVENT_DATES = (
    "2024-03-07", "2024-06-12", "2024-09-05", "2024-12-12",
    "2025-03-06", "2025-06-05", "2025-09-04", "2025-12-11",
    "2026-03-04", "2026-06-03",
)
SYMBOLS = ("AVGO", "NVDA", "AMD", "MRVL", "SMH")


def next_weekday(value: dt.date) -> dt.date:
    out = value + dt.timedelta(days=1)
    while out.weekday() >= 5:
        out += dt.timedelta(days=1)
    return out


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ET)


def fetch_event(mcp: MCPClient, event_date: str) -> dict[str, list[dict]]:
    day = dt.date.fromisoformat(event_date)
    end = next_weekday(day) + dt.timedelta(days=1)
    out: dict[str, list[dict]] = {}
    for offset in range(0, len(SYMBOLS), 3):
        batch = SYMBOLS[offset:offset + 3]
        payload = mcp.stock_bars(
            ",".join(batch), timeframe="5Min", start=day.isoformat(),
            end=end.isoformat(), feed="iex", adjustment="all", limit=10_000,
            sort="asc")
        out.update(payload.get("bars", {}))
    return out


def price(rows: list[dict], day: dt.date, at: dt.time) -> float | None:
    for row in rows:
        stamp = parse_time(row["t"])
        if stamp.date() == day and stamp.time() == at:
            return float(row["c"])
    return None


def summarize(label: str, values: list[float]) -> None:
    if not values:
        print(f"{label:<34} n=0")
        return
    print(f"{label:<34} n={len(values):2d} mean={mean(values):+7.3%} "
          f"median={median(values):+7.3%} "
          f"win={sum(value > 0 for value in values)/len(values):6.1%}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--min-avgo-gap", type=float, default=0.03)
    parser.add_argument("--lag-ratio", type=float, default=0.60)
    args = parser.parse_args()
    env = dotenv_values(args.env)
    key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit(f"missing Alpaca credentials in {args.env}")

    observations = []
    with MCPClient(key, secret, live=False, timeout=90) as mcp:
        for event_date in EVENT_DATES:
            bars = fetch_event(mcp, event_date)
            event_day = dt.date.fromisoformat(event_date)
            trade_day = next_weekday(event_day)
            values: dict[str, dict[str, float]] = {}
            for symbol in SYMBOLS:
                rows = bars.get(symbol, [])
                prior = price(rows, event_day, dt.time(15, 55))
                entry = price(rows, trade_day, dt.time(9, 45))
                h2 = price(rows, trade_day, dt.time(11, 45))
                close = price(rows, trade_day, dt.time(15, 30))
                if None not in (prior, entry, h2, close):
                    values[symbol] = {
                        "gap": entry / prior - 1.0,
                        "h2": h2 / entry - 1.0,
                        "close": close / entry - 1.0,
                    }
            if "AVGO" not in values:
                observations.append({"date": event_date, "missing": True})
                continue
            avgo_gap = values["AVGO"]["gap"]
            sign = 1 if avgo_gap > 0 else -1
            active = abs(avgo_gap) >= args.min_avgo_gap
            laggers = []
            if active:
                for symbol in ("NVDA", "AMD", "MRVL"):
                    if symbol not in values:
                        continue
                    peer_gap = values[symbol]["gap"]
                    if sign * peer_gap > 0 and abs(peer_gap) <= abs(avgo_gap) * args.lag_ratio:
                        laggers.append(symbol)
            observations.append({
                "date": event_date, "trade_day": trade_day, "missing": False,
                "sign": sign, "active": active, "avgo_gap": avgo_gap,
                "values": values, "laggers": laggers,
            })

    print("Alpaca MCP AVGO earnings -> semiconductor spillover replay")
    print(f"gate: |AVGO gap at 09:45| >= {args.min_avgo_gap:.1%}; lagger same "
          f"direction and <= {args.lag_ratio:.0%} of AVGO gap")
    print("event       AVGO gap  laggers          AVGO 2h  AVGO close")
    for row in observations:
        if row.get("missing"):
            print(f"{row['date']}   missing bars")
            continue
        avgo = row["values"]["AVGO"]
        laggers = ",".join(row["laggers"]) or "-"
        print(f"{row['date']}  {row['avgo_gap']:+8.2%}  {laggers:<15} "
              f"{row['sign'] * avgo['h2']:+8.2%} "
              f"{row['sign'] * avgo['close']:+10.2%}")

    active = [row for row in observations if row.get("active")]
    direct_2h = [row["sign"] * row["values"]["AVGO"]["h2"] for row in active]
    direct_close = [row["sign"] * row["values"]["AVGO"]["close"] for row in active]
    lag_2h, lag_close = [], []
    all_peer_2h, all_peer_close = [], []
    for row in active:
        for symbol in ("NVDA", "AMD", "MRVL"):
            if symbol in row["values"]:
                all_peer_2h.append(row["sign"] * row["values"][symbol]["h2"])
                all_peer_close.append(row["sign"] * row["values"][symbol]["close"])
        for symbol in row["laggers"]:
            lag_2h.append(row["sign"] * row["values"][symbol]["h2"])
            lag_close.append(row["sign"] * row["values"][symbol]["close"])

    print()
    summarize("AVGO direct continuation, 2h", direct_2h)
    summarize("AVGO direct continuation, close", direct_close)
    summarize("all peers same AVGO direction, 2h", all_peer_2h)
    summarize("all peers same AVGO direction, close", all_peer_close)
    summarize("selected lagging peers, 2h", lag_2h)
    summarize("selected lagging peers, close", lag_close)
    print("LIMITATIONS: ten events; fixed post-event rule; IEX adjusted stock bars; "
          "no beta/sector subtraction, option NBBO, IV crush, or fill model. "
          "Selection is timestamp-safe but not sufficient to activate options.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
