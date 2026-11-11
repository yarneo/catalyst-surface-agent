"""Free crypto sentiment: Reddit (public JSON, no auth) + crypto-news RSS,
scored by Claude Code headless (`claude -p`, Max plan, $0).

No API keys, no paid services, no Twitter scraping. Every step is best-effort and
degrades to a neutral score if a source or the LLM is unavailable, so the bot
never breaks on a sentiment failure. Used as a *dampen-only* overlay — it can
trim exposure on bad sentiment, never add a bet (sentiment is unvalidated).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .sentiment import _extract_json  # generic ```json``` / {...} extractor

# Load .env so CLAUDE_CODE_OAUTH_TOKEN (Max-subscription headless auth) is in the
# environment and inherited by the `claude -p` subprocess, regardless of caller.
load_dotenv()

CACHE_PATH = Path(__file__).resolve().parents[3] / "data" / "crypto_sentiment_cache.json"
DEFAULT_MODEL = "claude-haiku-4-5"
USER_AGENT = "crypto-bot-sentiment/1.0 (research; contact: local)"

# data-symbol -> search terms (most distinctive first)
COIN_TERMS = {
    "BTC-USD": ["Bitcoin", "BTC"],
    "ETH-USD": ["Ethereum", "ETH"],
    "XRP-USD": ["XRP", "Ripple"],
    "ADA-USD": ["Cardano", "ADA"],
    "LTC-USD": ["Litecoin", "LTC"],
    "DOGE-USD": ["Dogecoin", "DOGE"],
}
NEWS_RSS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
]


def _http_get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _reddit_via_praw(term: str, limit: int) -> list[str]:
    """Authenticated (read-only) Reddit search via PRAW — works from datacenter
    IPs, unlike the public JSON. Returns [] if creds are absent or it fails."""
    cid, csec = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and csec):
        return []
    try:
        import praw

        reddit = praw.Reddit(client_id=cid, client_secret=csec, user_agent=USER_AGENT)
        reddit.read_only = True
        posts = reddit.subreddit("CryptoCurrency").search(term, sort="new", limit=limit)
        return [p.title.strip() for p in posts if getattr(p, "title", None)]
    except Exception:
        return []


def fetch_reddit(coin: str, limit: int = 15) -> list[str]:
    """Recent r/CryptoCurrency post titles mentioning the coin.

    Prefers the authenticated PRAW path (reachable from the cloud VM); falls back
    to the public JSON endpoint (works from residential IPs) if PRAW isn't set up.
    """
    term = COIN_TERMS.get(coin, [coin.split("-")[0]])[0]
    titles = _reddit_via_praw(term, limit)
    if titles:
        return titles
    url = (
        "https://www.reddit.com/r/CryptoCurrency/search.json?"
        f"q={urllib.parse.quote(term)}&restrict_sr=1&sort=new&limit={limit}"
    )
    try:
        data = json.loads(_http_get(url))
        return [c["data"]["title"].strip() for c in data["data"]["children"] if c["data"].get("title")]
    except Exception:
        return []


def fetch_news(coin: str, per_feed: int = 40) -> list[str]:
    """Crypto-news RSS headlines mentioning the coin (no auth)."""
    terms = [t.lower() for t in COIN_TERMS.get(coin, [coin.split("-")[0]])]
    out: list[str] = []
    for feed in NEWS_RSS:
        try:
            xml = _http_get(feed)
            for raw in re.findall(r"<title>(.*?)</title>", xml, re.DOTALL)[:per_feed]:
                title = re.sub(r"<[^>]+>", "", raw).strip()
                if any(t in title.lower() for t in terms):
                    out.append(title)
        except Exception:
            continue
    return out


def fetch_google_news(coin: str, limit: int = 15) -> list[str]:
    """Recent headlines from Google News RSS search — free, no auth/key, reachable
    from datacenter IPs, and gives per-coin coverage for every coin."""
    terms = COIN_TERMS.get(coin, [coin.split("-")[0]])
    q = urllib.parse.quote(f'"{terms[0]}" crypto when:3d')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        xml = _http_get(url)
        # Google News wraps titles in CDATA; handle both forms.
        titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", xml, re.DOTALL)
        if not titles:
            titles = [re.sub(r"<[^>]+>", "", t).strip()
                      for t in re.findall(r"<title>(.*?)</title>", xml, re.DOTALL)]
        # First <title> is the feed's own name — skip it.
        return [t.strip() for t in titles[1:limit + 1] if t.strip()]
    except Exception:
        return []


def _score(coin: str, headlines: list[str], model: str, timeout: int = 60) -> dict | None:
    name = COIN_TERMS.get(coin, [coin])[0]
    bullets = "\n".join(f"- {h}" for h in headlines[:30])
    prompt = (
        f"You are a crypto market analyst. Based ONLY on these recent Reddit posts and "
        f"news headlines about {name}, rate the aggregate near-term sentiment from -1.0 "
        f"(very bearish) to +1.0 (very bullish); 0 = neutral/mixed. Respond with ONLY a "
        f'JSON object: {{"score": <float -1..1>, "confidence": <float 0..1>, "rationale": "<one sentence>"}}.'
        f"\n\nItems:\n{bullets}"
    )
    # Retry on transient failures (rate-limit/throttle) with backoff; give up
    # gracefully (None -> neutral fallback) if claude is missing or keeps failing.
    for attempt in range(3):
        try:
            proc = subprocess.run(
                ["claude", "-p", prompt, "--model", model],
                capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            return None  # claude not installed — don't retry
        except subprocess.TimeoutExpired:
            proc = None
        if proc is not None and proc.returncode == 0:
            parsed = _extract_json(proc.stdout)
            if parsed is not None:
                return parsed
        if attempt < 2:
            time.sleep(5)  # back off before retrying a likely throttle
    return None


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2))


def get_crypto_sentiment(coin: str, *, use_cache: bool = True, model: str = DEFAULT_MODEL) -> dict:
    """Return {"score","confidence","rationale","source","n_headlines"} for a coin.
    ``source`` is 'llm', 'cache', or 'fallback'. Never raises."""
    today = date.today().isoformat()
    cache = _load_cache()
    if use_cache and cache.get(coin, {}).get(today):
        return {**cache[coin][today], "source": "cache"}

    # Multiple free sources; dedup preserving order. Google News works from the
    # VM for every coin, so it carries coverage even when Reddit is unavailable.
    headlines = list(dict.fromkeys(
        fetch_google_news(coin) + fetch_news(coin) + fetch_reddit(coin)
    ))
    raw = _score(coin, headlines, model) if headlines else None
    if raw is None:
        return {"score": 0.0, "confidence": 0.0, "rationale": "no data/LLM — neutral fallback",
                "source": "fallback", "n_headlines": len(headlines)}
    result = {
        "score": max(-1.0, min(1.0, float(raw.get("score", 0.0)))),
        "confidence": max(0.0, min(1.0, float(raw.get("confidence", 0.5)))),
        "rationale": str(raw.get("rationale", ""))[:300],
        "source": "llm",
        "n_headlines": len(headlines),
    }
    cache.setdefault(coin, {})[today] = result
    _save_cache(cache)
    return result


def sentiment_multiplier(score: float, confidence: float, floor: float = 0.0) -> float:
    """Dampen-only: negative sentiment (weighted by confidence) scales exposure
    down toward ``floor``; non-negative sentiment leaves it unchanged."""
    if score >= 0:
        return 1.0
    return 1.0 - (1.0 - floor) * (-score) * confidence
