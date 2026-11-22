"""Shared real-time quote routing. No delayed data or trading methods.

Access is probed with this account's credentials. A short negative cache avoids
rate-heavy retries but discovers an activated SIP entitlement within 15 minutes.
"""
from datetime import datetime, timezone
import hashlib
import json
import os

import requests

from ..runtime_state import DATA, atomic_json
from ..market_tape import instant
from dotenv import dotenv_values


def credentials():
    local = dotenv_values(DATA.parent / ".env")
    return {name: os.environ.get(name) or local.get(name) for name in
            ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")}


def fetch_quotes(symbols, *, feed="iex", secrets=None):
    if feed not in {"iex", "sip"}:
        raise ValueError("execution requires a real-time feed; delayed quotes cannot replace fresh quotes")
    values = credentials() if secrets is None else secrets
    response = requests.get("https://data.alpaca.markets/v2/stocks/quotes/latest",
        params={"symbols": ",".join(symbols), "feed": feed},
        headers={"APCA-API-KEY-ID": values["ALPACA_API_KEY"],
                 "APCA-API-SECRET-KEY": values["ALPACA_SECRET_KEY"]}, timeout=(3, 6))
    response.raise_for_status()
    rows = response.json()["quotes"]
    if not isinstance(rows, dict):
        raise ValueError("invalid quote response")
    return {s: {"bid": q.get("bp"), "ask": q.get("ap"), "at": q.get("t"), "feed": feed}
            for s, q in rows.items() if isinstance(q, dict) and s in symbols}


def select_feed(symbols, *, at, fetch=None, data=None, identity=None, force=False):
    requested = os.environ.get("RESEARCH_QUOTE_FEED", "auto").lower()
    if requested not in {"auto", "iex", "sip"}:
        raise ValueError("RESEARCH_QUOTE_FEED must be auto, iex or sip")
    data = DATA if data is None else data
    # Identity is a one-way credential fingerprint, never the account ID/key.
    path = data / "research/quote_feed.json"
    session = str(instant(at).date())
    try:
        cached = json.loads(path.read_text())
        age = (instant(at)-instant(cached["at"])).total_seconds()
    except (OSError, ValueError, KeyError, TypeError):
        cached, age = {}, float("inf")
    if (not force and cached.get("session") == session and cached.get("requested") == requested
            and cached.get("credential_fingerprint") == identity and 0 <= age < 900):
        return cached["feed"], None, cached
    fetch = fetch or fetch_quotes
    feed = requested if requested != "auto" else "sip"
    initial, capabilities, error = None, {}, None
    try:
        initial = fetch(symbols, feed=feed)
        capabilities[feed] = "AVAILABLE"
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        message = getattr(getattr(exc, "response", None), "text", "") or ""
        capabilities[feed] = ("SUBSCRIPTION_REQUIRED" if status == 403 and
            ("subscription" in message.lower() or not message) else "ACCESS_DENIED" if status in {401, 403} else "UNAVAILABLE")
        error = type(exc).__name__
        if requested != "auto":
            raise
        feed = "iex"
    result = {"at": at, "session": session, "requested": requested, "feed": feed,
        "credential_fingerprint": identity, "capabilities": capabilities, "error_type": error,
        "order_authority": False, "reprobe_after_seconds": 900,
        "limitation": "IEX covers one exchange; quiet symbols may fail minute freshness" if feed == "iex" else None}
    atomic_json(path, result)
    return feed, initial, result


def latest_quotes(symbols, *, secrets=None, at=None, force=False):
    values = credentials() if secrets is None else secrets
    # Namespace the shared entitlement cache when deployment credentials change.
    identity = hashlib.sha256(str(values.get("ALPACA_API_KEY", "")).encode()).hexdigest()[:16]
    fetch = lambda names, feed: fetch_quotes(names, feed=feed, secrets=values)
    feed, initial, access = select_feed(symbols,
        at=at or datetime.now(timezone.utc).isoformat(), fetch=fetch, identity=identity, force=force)
    return (initial if initial is not None else fetch(symbols, feed=feed)), access
