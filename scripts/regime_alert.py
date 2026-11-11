"""200-DMA proximity alert — pings Telegram when QQQ nears or crosses the line
that actually flips the bot defensive, so you react to the REGIME signal instead
of to intraday/overnight noise.

The 200-DMA is the trigger the live bot uses: below it, the regime sleeve rotates
QLD→BIL and the idle router rotates QQQ→DBMF+KMLM. This watcher classifies:
  OK       : QQQ > WATCH_PCT above its 200-DMA          (quiet)
  WATCH    : within WATCH_PCT of the 200-DMA (still above)
  RISK-OFF : QQQ at/below its 200-DMA — bot will flip defensive at next regime run

To avoid daily spam it only Telegrams on a STATE CHANGE (escalation or recovery),
persisting the last tier to data/regime_alert_state.json. Uses settled daily
closes (matches when the regime executor actually acts), so run it post-close.
  uv run python scripts/regime_alert.py           # send on state change
  uv run python scripts/regime_alert.py --force    # always send (test)
"""
from __future__ import annotations
import argparse, json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
try:
    from dotenv import load_dotenv; load_dotenv()
except Exception:
    pass
import yfinance as yf
from trading_bot.notify import send_telegram

STATE = pathlib.Path(__file__).resolve().parents[1] / "data" / "regime_alert_state.json"
MA = 200
WATCH_PCT = 0.05   # warn once QQQ is within 5% of its 200-DMA


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    c = yf.Ticker("QQQ").history(period=f"{MA + 120}d", auto_adjust=True)["Close"].dropna()
    if len(c) < MA:
        print("not enough QQQ history"); return
    px = float(c.iloc[-1]); ma = float(c.rolling(MA).mean().iloc[-1])
    gap = px / ma - 1.0

    if gap <= 0:
        tier, icon = "RISK-OFF", "🔴"
    elif gap <= WATCH_PCT:
        tier, icon = "WATCH", "🟠"
    else:
        tier, icon = "OK", "🟢"

    print(f"QQQ {px:.2f} vs 200-DMA {ma:.2f} → {gap*100:+.1f}% [{tier}]")

    last = None
    if STATE.exists():
        try:
            last = json.loads(STATE.read_text()).get("tier")
        except Exception:
            last = None

    changed = tier != last
    if args.force or (changed and last is not None) or (changed and tier != "OK"):
        if tier == "RISK-OFF":
            msg = (f"{icon} *REGIME: RISK-OFF* — QQQ {px:.0f} has crossed BELOW its 200-DMA "
                   f"({ma:.0f}). The bot will flip defensive at the next regime run "
                   f"(QLD→BIL, idle QQQ→DBMF+KMLM). This is the designed crash defense.")
        elif tier == "WATCH":
            msg = (f"{icon} *REGIME: WATCH* — QQQ {px:.0f} is within {WATCH_PCT*100:.0f}% of its "
                   f"200-DMA ({ma:.0f}), {gap*100:+.1f}% above. Approaching the risk-off trigger; "
                   f"no action yet — the bot flips only on a close below the line.")
        else:  # recovered to OK
            msg = (f"{icon} *REGIME: back to OK* — QQQ {px:.0f} is {gap*100:+.1f}% above its "
                   f"200-DMA ({ma:.0f}). Clear of the risk-off trigger.")
        try:
            send_telegram(msg)
            print("  → Telegram sent")
        except Exception as e:
            print(f"  → Telegram failed: {e}")
    else:
        print("  → no state change; quiet")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"tier": tier, "qqq": round(px, 2), "ma200": round(ma, 2),
                                 "gap_pct": round(gap * 100, 2)}))


if __name__ == "__main__":
    main()
