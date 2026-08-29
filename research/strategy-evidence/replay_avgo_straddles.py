"""Replay actual AVGO earnings straddles with Alpaca MCP option trade bars.

This is stronger than applying today's premium to old stock gaps: it selects the
then-ATM weekly call and put using the event-day stock close, reads the expired
contracts through Alpaca MCP, and values both legs from historical five-minute
option trade bars. It still is not an NBBO fill backtest because the MCP server
does not expose historical option quotes. Two estimates are reported:

* last-trade proxy: final entry-window bar close to the 09:45 exit bar close;
* conservative envelope: each leg's highest entry-window trade to its lowest
  exit-window trade, intentionally assuming adverse timing on both sides.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from trading_bot.options.mcp import MCPClient


ET = ZoneInfo("America/New_York")
# AVGO's July 2024 split makes earlier option deliverables incomparable. These
# eight events are all post-split and use ordinary 100-share contracts.
EVENT_DATES = (
    "2024-09-05", "2024-12-12", "2025-03-06", "2025-06-05",
    "2025-09-04", "2025-12-11", "2026-03-04", "2026-06-03",
)


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


def stock_close(mcp: MCPClient, day: dt.date) -> float | None:
    payload = mcp.stock_bars(
        "AVGO", timeframe="5Min", start=stamp(day, 15, 30),
        end=stamp(day, 16, 0), feed="iex", adjustment="all", limit=100,
        sort="asc")
    rows = payload.get("bars", {}).get("AVGO", [])
    eligible = [row for row in rows if parse_time(row["t"]).time() <= dt.time(15, 55)]
    return float(eligible[-1]["c"]) if eligible else None


def atm_contracts(mcp: MCPClient, day: dt.date,
                  spot: float) -> tuple[float, str, str] | None:
    expiry = friday_on_or_after(day)
    payload = mcp.option_contracts(
        underlying_symbols="AVGO", status="inactive",
        expiration_date=expiry.isoformat(), strike_price_gte=spot * 0.94,
        strike_price_lte=spot * 1.06, limit=1000)
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


def option_windows(mcp: MCPClient, event_day: dt.date, symbols: tuple[str, str]
                   ) -> dict[str, dict[str, float]]:
    trade_day = next_weekday(event_day)
    payload = mcp.option_bars(
        ",".join(symbols), "5Min", start=stamp(event_day, 15, 25),
        end=stamp(trade_day, 10, 0), limit=1000, sort="asc")
    out: dict[str, dict[str, float]] = {}
    for symbol in symbols:
        rows = payload.get("bars", {}).get(symbol, [])
        entry = [row for row in rows
                 if parse_time(row["t"]).date() == event_day
                 and dt.time(15, 30) <= parse_time(row["t"]).time() <= dt.time(15, 55)]
        exit_rows = [row for row in rows
                     if parse_time(row["t"]).date() == trade_day
                     and dt.time(9, 40) <= parse_time(row["t"]).time() <= dt.time(9, 50)]
        if not entry or not exit_rows:
            continue
        # Last rows are sorted ascending. The envelope crosses each leg at the
        # worst observed price in its respective window.
        out[symbol] = {
            "entry_last": float(entry[-1]["c"]),
            "exit_last": float(exit_rows[-1]["c"]),
            "entry_bad": max(float(row["h"]) for row in entry),
            "exit_bad": min(float(row["l"]) for row in exit_rows),
            "entry_trades": sum(int(row.get("n") or 0) for row in entry),
            "exit_trades": sum(int(row.get("n") or 0) for row in exit_rows),
        }
    return out


def summarize(label: str, values: list[float]) -> None:
    if not values:
        print(f"{label:<24} n=0")
        return
    print(f"{label:<24} n={len(values):2d} mean={mean(values):+7.2%} "
          f"median={median(values):+7.2%} "
          f"win={sum(value > 0 for value in values)/len(values):6.1%} "
          f"worst={min(values):+7.2%} best={max(values):+7.2%}")


def observe(mcp: MCPClient, value: str) -> dict:
    """Everything this replay ever reads from the market, for one event.

    Kept separate from the arithmetic so the two can be run apart: recorded
    once against Alpaca, then recomputed by anyone with no credentials.
    """
    day = dt.date.fromisoformat(value)
    spot = stock_close(mcp, day)
    selected = atm_contracts(mcp, day, spot) if spot else None
    if not spot or not selected:
        return {"date": value, "reason": "missing spot/contracts"}
    strike, call, put = selected
    return {"date": value, "spot": spot, "strike": strike,
            "call": call, "put": put,
            "windows": option_windows(mcp, day, (call, put))}


def compute(observation: dict) -> dict:
    """Turn one event's recorded observations into its two return estimates."""
    if "reason" in observation:
        return {"date": observation["date"], "reason": observation["reason"]}
    call, put = observation["call"], observation["put"]
    windows = observation["windows"]
    spot, strike = observation["spot"], observation["strike"]
    if call not in windows or put not in windows:
        missing = "call" if call not in windows else "put"
        return {"date": observation["date"], "spot": spot, "strike": strike,
                "reason": f"missing {missing} trade window"}
    entry_last = windows[call]["entry_last"] + windows[put]["entry_last"]
    exit_last = windows[call]["exit_last"] + windows[put]["exit_last"]
    entry_bad = windows[call]["entry_bad"] + windows[put]["entry_bad"]
    exit_bad = windows[call]["exit_bad"] + windows[put]["exit_bad"]
    return {
        "date": observation["date"], "spot": spot, "strike": strike,
        "entry_last": entry_last, "exit_last": exit_last,
        "last_return": exit_last / entry_last - 1.0,
        "entry_bad": entry_bad, "exit_bad": exit_bad,
        "bad_return": exit_bad / entry_bad - 1.0,
        "entry_trades": min(windows[call]["entry_trades"],
                            windows[put]["entry_trades"]),
        "exit_trades": min(windows[call]["exit_trades"],
                           windows[put]["exit_trades"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--offline", metavar="PATH",
                        help="recompute from a recorded snapshot; needs no "
                             "credentials and makes no network call")
    parser.add_argument("--snapshot", metavar="PATH",
                        help="write the observations this run read from Alpaca")
    args = parser.parse_args()

    if args.offline:
        payload = json.loads(Path(args.offline).read_text())
        observations = payload["observations"]
        source = (f"RECORDED {payload.get('recorded_at', 'unknown')} · "
                  f"replayed offline from {args.offline}")
    else:
        env = dotenv_values(args.env)
        key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise SystemExit(
                f"missing Alpaca credentials in {args.env}. "
                f"To reproduce without credentials: --offline <snapshot.json>")
        with MCPClient(key, secret, live=False, timeout=90) as mcp:
            observations = [observe(mcp, value) for value in EVENT_DATES]
        source = "LIVE read through Alpaca MCP"
        if args.snapshot:
            Path(args.snapshot).write_text(json.dumps({
                "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "note": "Historical five-minute option and stock trade bars "
                        "read through Alpaca MCP. Market observations only: "
                        "no account, position or credential data.",
                "event_dates": list(EVENT_DATES),
                "observations": observations,
            }, indent=1, sort_keys=True) + "\n")
            print(f"snapshot written to {args.snapshot}")

    results = [compute(observation) for observation in observations]

    print("Alpaca MCP AVGO historical ATM straddle trade-bar replay")
    print(f"source: {source}")
    print("event       spot  strike  premium  entry->exit(last)  return   conservative  min trades")
    for row in results:
        if "reason" in row:
            print(f"{row['date']}  {row['reason']}")
            continue
        ratio = row["entry_last"] / row["spot"]
        print(f"{row['date']} {row['spot']:7.2f} {row['strike']:7.1f} "
              f"{ratio:7.2%} "
              f"{row['entry_last']:6.2f}->{row['exit_last']:6.2f} "
              f"{row['last_return']:+8.2%} {row['bad_return']:+13.2%} "
              f"{row['entry_trades']:3.0f}/{row['exit_trades']:3.0f}")

    complete = [row for row in results if "last_return" in row]
    print()
    summarize("last-trade proxy", [row["last_return"] for row in complete])
    summarize("adverse envelope", [row["bad_return"] for row in complete])
    gated = [row for row in complete if row["entry_last"] / row["spot"] <= 0.085]
    summarize("last proxy, premium <=8.5%",
              [row["last_return"] for row in gated])
    summarize("adverse, premium <=8.5%", [row["bad_return"] for row in gated])
    print("LIMITATIONS: post-split events only; historical option trades, not "
          "quotes/NBBO; each leg may trade at a different instant; no fees; "
          "the adverse envelope intentionally overstates execution cost. Missing "
          "a leg's entry or exit window excludes the event and is reported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
