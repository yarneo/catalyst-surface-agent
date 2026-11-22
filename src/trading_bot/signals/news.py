"""Timestamped Alpaca news with explicit, fixed ETF context relationships.

Context companies are sector proxies, not a claim about current ETF holdings.
They may inform a shadow sentiment proposal; they never supply an ETF earnings
date or a direct company-event claim about the ETF.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import os
import re

import requests
from dotenv import dotenv_values

from ..runtime_state import DATA

NEWS_VERSION = "alpaca-sector-context-v1"
SECTOR_CONTEXT = {
    "XLE": ("XOM", "CVX"),
    "XLB": ("LIN", "APD", "SHW", "FCX"),
    "XLP": ("PG", "KO", "PEP", "WMT"),
    "XLU": ("NEE", "SO", "DUK"),
}


def credentials():
    local = dotenv_values(DATA.parent / ".env")
    return {name: os.environ.get(name) or local.get(name) for name in
            ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")}


def clean_text(value, limit):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()[:limit]


def timestamp(value):
    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("news requires an explicit timezone")
    return value.astimezone(timezone.utc)


def select_articles(rows, symbol, *, now, limit=12):
    """Reject future, old, unrelated and duplicate input before any model sees it."""
    related = set(SECTOR_CONTEXT.get(symbol, ()))
    selected, seen = [], set()
    rejected = {"timestamp": 0, "unrelated": 0, "empty": 0, "duplicate": 0}
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ValueError("invalid article")
            published = timestamp(row.get("created_at"))
            updated = timestamp(row.get("updated_at") or row.get("created_at"))
            if published > now or updated > now or updated < now - timedelta(days=7):
                raise ValueError("article outside observation window")
        except (ValueError, TypeError):
            rejected["timestamp"] += 1
            continue
        tags = row.get("symbols") or []
        symbols = {s for s in tags if isinstance(s, str)} if isinstance(tags, list) else set()
        direct = symbol in symbols
        if not direct and not symbols & related:
            rejected["unrelated"] += 1
            continue
        title = clean_text(row.get("headline"), 300)
        if not title:
            rejected["empty"] += 1
            continue
        identity = str(row.get("id") or row.get("url") or title.casefold())
        if identity in seen or title.casefold() in seen:
            rejected["duplicate"] += 1
            continue
        seen.update((identity, title.casefold()))
        selected.append({"fact_id": "alpaca:" + str(row.get("id") or len(selected)),
            "headline": title, "summary": clean_text(row.get("summary"), 700),
            "published_at": published.isoformat(), "updated_at": updated.isoformat(),
            "symbols": sorted(symbols), "source": str(row.get("source") or "alpaca"),
            "url": str(row.get("url") or ""),
            "scope": "direct" if direct else "sector_context",
            "context_for": symbol if not direct else None})
    selected.sort(key=lambda row: row["updated_at"], reverse=True)
    return selected[:limit], rejected


def fetch_news(symbol, *, now=None, limit=12, get=None, secrets=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    symbol = symbol.upper()
    values = secrets if secrets is not None else credentials()
    result = {"version": NEWS_VERSION, "observed_at": now.isoformat(), "symbol": symbol,
              "provider": "alpaca_news", "context_symbols": list(SECTOR_CONTEXT.get(symbol, ())),
              "articles": [], "status": "UNAVAILABLE", "error": None}
    if not values.get("ALPACA_API_KEY") or not values.get("ALPACA_SECRET_KEY"):
        return {**result, "error": "news_credentials_unavailable"}
    try:
        response = (get or requests.get)("https://data.alpaca.markets/v1beta1/news",
            headers={"APCA-API-KEY-ID": values["ALPACA_API_KEY"],
                     "APCA-API-SECRET-KEY": values["ALPACA_SECRET_KEY"]},
            params={"symbols": ",".join((symbol, *SECTOR_CONTEXT.get(symbol, ()))),
                    "start": (now-timedelta(days=7)).isoformat(), "end": now.isoformat(),
                    "sort": "desc", "limit": 50, "include_content": "false"}, timeout=(3, 8))
        if not response.ok:
            return {**result, "error": "news_http_" + str(response.status_code)}
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("news"), list):
            return {**result, "error": "invalid_news_response"}
        articles, rejected = select_articles(data["news"], symbol, now=now, limit=limit)
        return {**result, "status": "READY" if articles else "NO_RECENT_NEWS",
                "articles": articles, "rejected": rejected,
                "direct_articles": sum(r["scope"] == "direct" for r in articles),
                "context_articles": sum(r["scope"] == "sector_context" for r in articles)}
    except (requests.RequestException, ValueError, TypeError) as exc:
        return {**result, "error": "news_" + type(exc).__name__}
