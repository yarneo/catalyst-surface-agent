"""Telegram notifications. Reads creds from env; no-ops silently if not set so
the bot runs the same with or without Telegram. Never raises into the caller."""
from __future__ import annotations

import os

import requests


def send_telegram(text: str, parse_mode: str = "Markdown") -> bool:
    """Send a message to the configured Telegram chat. Returns True on success.
    Always safe to call — silent no-op if not configured, never raises.

    If Telegram rejects the message with the requested parse_mode (legacy
    Markdown chokes on unpaired '_'/'*' in interpolated names like
    'ts_momentum' and the message silently VANISHES), retry once as plain
    text — a slightly ugly digest is infinitely better than no digest.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _post(mode: str | None) -> bool:
        data = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
        if mode:
            data["parse_mode"] = mode
        r = requests.post(url, data=data, timeout=10)
        return r.status_code == 200 and r.json().get("ok") is True

    try:
        if _post(parse_mode):
            return True
        # parse failure (or any non-ok) — retry as plain text so it still arrives
        return _post(None)
    except Exception:
        return False
