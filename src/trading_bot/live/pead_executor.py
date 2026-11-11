"""Live (paper) executor for the Post-Earnings Announcement Drift sleeve.

A second, uncorrelated sleeve alongside the crypto bot. Runs daily near the US
open. Each run:
  * EXIT any position held >= ``hold_days`` trading days (liquidate via Alpaca).
  * ENTER fresh concordant long signals: a stock whose latest earnings beat
    consensus AND reacted up, where TODAY is the entry session (open after the
    reaction). Size = ``position_frac`` of equity; cap concurrent positions.
Holds are multi-week (60 trading days) so there's no PDT day-trade issue.

State (which positions, when to exit) persists to data/pead_state.json.
Dry-run by default; ``--live`` submits to the (paper) account.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..backtest.pead import earnings_events
from ..broker import AlpacaBroker
from ..data import load_ohlcv
from ..notify import send_telegram
from .executor import STATE_PATH

PEAD_STATE_PATH = STATE_PATH.parent / "pead_state.json"


class PeadExecutor:
    def __init__(
        self, universe: list[str], broker: AlpacaBroker | None = None,
        hold_days: int = 60, position_frac: float = 0.10, max_positions: int = 8,
        min_surprise: float = 0.0, live: bool = False,
    ):
        self.universe = universe
        self.broker = broker or AlpacaBroker()
        self.hold_days = hold_days
        self.position_frac = position_frac
        self.max_positions = max_positions
        self.min_surprise = min_surprise
        self.live = live

    def _state(self) -> dict:
        if PEAD_STATE_PATH.exists():
            return json.loads(PEAD_STATE_PATH.read_text())
        return {"positions": []}

    def _save(self, st: dict) -> None:
        PEAD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PEAD_STATE_PATH.write_text(json.dumps(st, indent=2))

    def _recent(self, sym: str) -> pd.DataFrame:
        end = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        start = (datetime.now(timezone.utc).date() - timedelta(days=160)).isoformat()
        return load_ohlcv(sym, start, end, use_cache=False)

    def run(self) -> dict:
        st = self._state()
        acct = self.broker.get_account()
        equity = float(acct.portfolio_value)
        today = datetime.now(timezone.utc).date()
        mode = "LIVE (paper)" if self.live else "DRY-RUN"
        print(f"\n=== PEAD SLEEVE | {today} | {mode} | equity ${equity:,.0f} | "
              f"open {len(st['positions'])} ===")

        held = {p["symbol"] for p in st["positions"]}
        exits: list[dict] = []
        entries: list[dict] = []

        # --- EXITS: positions held >= hold_days trading days ---
        keep = []
        for p in st["positions"]:
            try:
                px = self._recent(p["symbol"])
                held_days = int((px.index >= pd.Timestamp(p["entry_date"])).sum())
            except Exception:
                held_days = 0
            if held_days >= self.hold_days:
                print(f"  EXIT {p['symbol']} (held {held_days} trading days)")
                exits.append({"symbol": p["symbol"], "held_days": held_days})
                if self.live:
                    try:
                        self.broker._client.close_position(p["symbol"])
                    except Exception as exc:  # noqa: BLE001
                        print(f"    close failed: {exc}")
                else:
                    print(f"    [dry-run] would liquidate {p['symbol']}")
            else:
                keep.append(p)
        st["positions"] = keep

        # --- ENTRIES: fresh concordant signals entering today ---
        # PASS 1: identify all qualifying signals (no orders submitted yet).
        # This lets us free cash from BIL BEFORE buying, so PEAD can't fail with
        # insufficient buying power when the cash park has parked our reserves.
        slots = self.max_positions - len(st["positions"])
        candidates: list[dict] = []
        # Scan diagnostics — printed at end of pass so the daily log makes it
        # obvious why N=0 entries happened (vs. silent skips that took an hour
        # to diagnose on 2026-05-31).
        scan = {"total": 0, "skipped_held": 0, "fetch_failed": 0, "no_data": 0,
                "no_recent_earnings": 0, "non_concordant": 0, "candidates": 0}
        non_concordant_log: list[str] = []
        for sym in self.universe:
            scan["total"] += 1
            if len(candidates) >= slots:
                break
            if sym in held:
                scan["skipped_held"] += 1
                continue
            try:
                px = self._recent(sym)
                ev = earnings_events(sym, use_cache=False)
            except Exception as exc:  # noqa: BLE001
                scan["fetch_failed"] += 1
                continue
            if px.empty or ev.empty:
                scan["no_data"] += 1
                continue
            recent = ev[ev["date"] >= pd.Timestamp(today) - pd.Timedelta(days=8)]
            if recent.empty:
                scan["no_recent_earnings"] += 1
                continue
            fire = False
            fire_e = None
            last_reaction = None
            last_surprise = None
            for _, e in recent.iterrows():
                pos = px.index.searchsorted(e["date"])
                react = pos + 1 if e["amc"] else pos
                if react < 1 or react >= len(px):
                    continue
                react_date = px.index[react].date()
                between = [x.date() for x in px.index[(px.index.date > react_date) & (px.index.date <= today)]]
                if not (react_date < today and all(d == today for d in between)):
                    continue
                reaction = px["close"].iloc[react] / px["close"].iloc[react - 1] - 1
                last_reaction = reaction
                last_surprise = float(e["surprise"])
                if e["surprise"] > self.min_surprise and reaction > 0:
                    fire = True
                    fire_e = e
                    fire_reaction = reaction
                    break
            if not fire:
                if last_reaction is not None:
                    scan["non_concordant"] += 1
                    non_concordant_log.append(
                        f"{sym}: surprise={last_surprise:+.1f}% reaction={last_reaction*100:+.2f}% — not concordant"
                    )
                else:
                    scan["no_recent_earnings"] += 1
                continue
            scan["candidates"] += 1
            price = float(px["close"].iloc[-1])
            qty = round(self.position_frac * equity / price, 4)
            candidates.append({
                "symbol": sym, "qty": qty, "price": price,
                "notional": qty * price,
                "surprise": float(fire_e["surprise"]),
                "reaction": float(fire_reaction),
            })

        # Daily scan summary — always print so the log is self-explanatory
        print(f"  Scan: {scan['total']} symbols → "
              f"{scan['skipped_held']} already held · "
              f"{scan['fetch_failed']} fetch fail · "
              f"{scan['no_data']} no earnings data · "
              f"{scan['no_recent_earnings']} no recent earnings · "
              f"{scan['non_concordant']} non-concordant · "
              f"{scan['candidates']} candidates")
        for line in non_concordant_log:
            print(f"    skipped {line}")

        # PASS 1.5: if we found buys to make, free cash from BIL first
        if candidates and self.live:
            total_needed = sum(c["notional"] for c in candidates)
            acct = self.broker.get_account()
            cash_now = float(acct.cash)
            shortfall = total_needed - cash_now
            if shortfall > 0:
                bil_pos = next((p for p in self.broker.get_positions() if p.symbol == "BIL"), None)
                if bil_pos:
                    print(f"  Need ${total_needed:,.0f} for entries; cash ${cash_now:,.0f}. "
                          f"Liquidating BIL (${float(bil_pos.market_value):,.0f}) to free cash…")
                    try:
                        self.broker._client.close_position("BIL")
                        print("    ✅ BIL close-position submitted; waiting 5s for fill")
                        import time as _t; _t.sleep(5)
                    except Exception as exc:  # noqa: BLE001
                        print(f"    ⚠ BIL liquidation failed: {exc}")

        # PASS 2: now execute the buys
        for c in candidates:
            sym = c["symbol"]; qty = c["qty"]; price = c["price"]
            print(f"  ENTER {sym}: beat +{c['surprise']*100:.0f}%, reaction "
                  f"+{c['reaction']*100:.1f}% → buy {qty} sh (~${c['notional']:,.0f}) @ ${price:,.2f}")
            entries.append(c)
            if self.live:
                try:
                    order = self.broker.submit_market_order(
                        sym, qty, "buy", reference_price=price, sleeve="pead"
                    )
                    st["positions"].append({"symbol": sym, "entry_date": today.isoformat(), "qty": qty})
                    print(f"    ✅ submitted (id {order.id})")
                except Exception as exc:  # noqa: BLE001
                    print(f"    buy failed: {exc}")
            else:
                st["positions"].append({"symbol": sym, "entry_date": today.isoformat(), "qty": qty})
                print("    [dry-run] would buy")

        self._save(st)
        print(f"Open PEAD positions: {[p['symbol'] for p in st['positions']]}")

        # Telegram summary (silent no-op if creds not set; quiet if nothing happened)
        if entries or exits:
            try:
                send_telegram(_format_pead_msg(today.isoformat(), equity, entries, exits, st["positions"]))
            except Exception:
                pass
        return st


def _format_pead_msg(today: str, equity: float, entries: list, exits: list, open_pos: list) -> str:
    lines = [f"📰 *PEAD* · {today}", f"Equity ${equity:,.0f}", ""]
    for e in entries:
        lines.append(
            f"🟢 *ENTER {e['symbol']}* — surprise {e['surprise']*100:+.0f}%, "
            f"reaction {e['reaction']*100:+.1f}%\n"
            f"   Bought {e['qty']:.0f} sh @ ${e['price']:.2f} (~${e['notional']:,.0f})"
        )
    for x in exits:
        lines.append(f"🔴 *EXIT {x['symbol']}* — held {x['held_days']} trading days")
    if open_pos:
        names = ", ".join(p["symbol"] for p in open_pos)
        lines.append("")
        lines.append(f"Open: {names}")
    return "\n".join(lines)
