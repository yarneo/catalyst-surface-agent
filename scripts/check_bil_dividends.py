"""Monitor for BIL dividend payments in the Alpaca paper account.

BIL distributes monthly. This script runs daily, queries Alpaca's
/v2/account/activities REST endpoint directly (the alpaca-py SDK we use
doesn't expose this), filters for DIV activities on BIL, and pings Telegram
on each new dividend it sees (idempotent — tracks seen IDs in a state file).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from trading_bot.notify import send_telegram

load_dotenv()
SEEN_FILE = Path(__file__).resolve().parents[1] / "data" / "bil_dividends_seen.json"


def main() -> None:
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("Missing ALPACA_API_KEY/SECRET_KEY in environment")
        return

    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    url = "https://paper-api.alpaca.markets/v2/account/activities"
    try:
        r = requests.get(url, headers=headers, params={"activity_types": "DIV"}, timeout=30)
        r.raise_for_status()
        activities = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not fetch account activities: {exc}")
        return

    bil_divs = [a for a in activities if str(a.get("symbol", "")).upper() == "BIL"]
    print(f"Found {len(bil_divs)} total BIL dividend records on this account")
    print(f"Total DIV activities of any kind: {len(activities)}")

    seen: list[str] = []
    if SEEN_FILE.exists():
        try: seen = json.loads(SEEN_FILE.read_text())
        except Exception: seen = []
    seen_set = set(seen)

    new = [a for a in bil_divs if str(a.get("id", "")) not in seen_set]
    if not new:
        print("No new BIL dividends since last check.")
        return

    for div in new:
        d_id = str(div.get("id", ""))
        d_date = div.get("date") or div.get("transaction_time") or "?"
        d_amt = float(div.get("net_amount", 0) or 0)
        d_per_share = div.get("per_share_amount")
        print(f"  NEW DIVIDEND: date={d_date}  ${d_amt:.2f}")

        msg_lines = [
            f"💰 *BIL dividend credited* ({d_date})",
            f"Amount: ${d_amt:.2f}",
        ]
        if d_per_share:
            msg_lines.append(f"Per share: ${float(d_per_share):.4f}")
        msg_lines.append("")
        msg_lines.append("✅ Alpaca paper DOES credit Treasury dividends — "
                          "BIL parking is real yield in paper too.")
        try: send_telegram("\n".join(msg_lines))
        except Exception: pass
        seen_set.add(d_id)

    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(list(seen_set), default=str, indent=2))
    print(f"Updated {SEEN_FILE.name} with {len(new)} new dividend ID(s)")


if __name__ == "__main__":
    main()
