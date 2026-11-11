"""News sentiment via Claude Code headless (`claude -p`).

Routes through the user's Max subscription — NOT the Anthropic API — so it costs
nothing per call (see the project's no-API-billing rule). Because Max has usage
rate limits, every path here degrades gracefully: if `claude` is missing, times
out, errors, or returns junk, we return a neutral score and mark the source as
"fallback" so the bot keeps trading on the quant signal alone.

Headlines come from yfinance (free). Results are cached per symbol per day, so
the premarket/open/close runs share a single LLM call per symbol.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Load .env at import time so CLAUDE_CODE_OAUTH_TOKEN is in os.environ before
# subprocess.run inherits it. Without this, the `claude -p` call ran without
# auth and silently returned "Not logged in" — every sentiment call fell back
# to neutral 0.0, hidden by the per-day cache. Discovered 2026-06-03.
load_dotenv()

CACHE_PATH = Path(__file__).resolve().parents[3] / "data" / "sentiment_cache.json"
DEFAULT_MODEL = "claude-haiku-4-5"  # fast + light on Max rate limits


def fetch_headlines(symbol: str, limit: int = 12) -> list[str]:
    """Recent news headlines for a symbol (yfinance). Best-effort, never raises."""
    try:
        import yfinance as yf

        items = yf.Ticker(symbol).news or []
    except Exception:
        return []
    headlines: list[str] = []
    for it in items[:limit]:
        # yfinance has shifted the news schema across versions; handle both.
        title = it.get("title") or it.get("content", {}).get("title")
        if title:
            headlines.append(title.strip())
    return headlines


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _find_claude_cli() -> str | None:
    """Locate the claude CLI.

    `shutil.which` checks $PATH only; cron/SSH sessions on the VM often don't
    include ~/.local/bin (where the Max install puts the binary), so we also
    probe known locations. Returns absolute path or None."""
    import os
    import shutil
    found = shutil.which("claude")
    if found:
        return found
    candidates = [
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _score_with_claude(
    symbol: str, headlines: list[str], model: str, timeout: int
) -> dict | None:
    bullets = "\n".join(f"- {h}" for h in headlines)
    # Structured sentiment per the anthropic/financial-services equity-research
    # skill patterns: classify each headline by event type, score INCREMENTAL
    # move vs what was already priced in (not absolute tone), and require an
    # explicit disconfirming-evidence check before settling on confidence.
    # Backward compatible: 'score' and 'confidence' remain the primary fields.
    prompt = (
        f"You are a sell-side analyst writing a one-line morning-note take on {symbol}. "
        f"Below are recent headlines (newest first). Score the INCREMENTAL near-term "
        f"sentiment — how the headlines should move the stock RELATIVE TO what was "
        f"already priced in before they hit.\n\n"
        f"Procedure (do all steps internally; output ONLY the JSON):\n"
        f"1. Classify each headline into one of: EARNINGS, GUIDANCE, MA, MGMT, "
        f"PRODUCT_REG, ANALYST, MACRO, NOISE. Discard NOISE (minor reblogs, "
        f"non-events, >7-day-old reiterations).\n"
        f"2. For each non-noise headline, note what it literally says (fact) vs how "
        f"the market reads it given consensus expectations. A beat that misses the "
        f"whisper is bearish.\n"
        f"3. Identify the single strongest DISCONFIRMING headline in the set. If it "
        f"materially offsets the dominant signal, compress score toward 0.\n"
        f"4. Score direction and magnitude separately, then combine:\n"
        f"   - direction: -1, 0, or +1\n"
        f"   - magnitude: 0.0 (already priced in / stale) to 1.0 (clear surprise)\n"
        f"   - score = direction * magnitude, clipped to [-1, 1]\n"
        f"5. Confidence reflects agreement across non-noise headlines and source "
        f"quality (primary filings / tier-1 wires > blogs / rumors). Anchors: "
        f"Low=0.2, Medium=0.5, High=0.8.\n\n"
        f"Output ONLY this JSON, no prose, no markdown fences:\n"
        f'{{"score": <float -1..1>, "confidence": <float 0..1>, '
        f'"direction": <-1|0|1>, "magnitude": <float 0..1>, '
        f'"dominant_event_type": "<one of the categories above>", '
        f'"disconfirming": "<strongest contrary headline or \'none\'>", '
        f'"rationale": "<one sentence: fact -> market interpretation>"}}\n\n'
        f"Headlines:\n{bullets}"
    )
    claude_bin = _find_claude_cli()
    if not claude_bin:
        return None
    try:
        proc = subprocess.run(
            [claude_bin, "-p", prompt, "--model", model],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _extract_json(proc.stdout)


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


def get_daily_sentiment(
    symbol: str,
    *,
    use_cache: bool = True,
    model: str = DEFAULT_MODEL,
    timeout: int = 60,
) -> dict:
    """Return ``{"score", "confidence", "rationale", "source", "n_headlines"}``.

    ``source`` is "llm", "cache", or "fallback". ``score`` is clamped to [-1, 1].
    Always returns a usable dict; never raises.
    """
    today = date.today().isoformat()
    cache = _load_cache()
    if use_cache and cache.get(symbol, {}).get(today):
        return {**cache[symbol][today], "source": "cache"}

    headlines = fetch_headlines(symbol)
    raw = _score_with_claude(symbol, headlines, model, timeout) if headlines else None

    if raw is None:
        result = {
            "score": 0.0,
            "confidence": 0.0,
            "rationale": "no LLM/news available — neutral fallback",
            "source": "fallback",
            "n_headlines": len(headlines),
        }
    else:
        score = max(-1.0, min(1.0, float(raw.get("score", 0.0))))
        result = {
            "score": score,
            "confidence": max(0.0, min(1.0, float(raw.get("confidence", 0.5)))),
            "rationale": str(raw.get("rationale", ""))[:300],
            # New structured fields from the upgraded prompt — captured for
            # logging and future use (e.g. cap dampening when event_type ==
            # ANALYST or magnitude is below noise threshold). Optional; older
            # LLM responses without these still work.
            "direction": int(raw.get("direction")) if raw.get("direction") is not None else None,
            "magnitude": (max(0.0, min(1.0, float(raw["magnitude"])))
                          if raw.get("magnitude") is not None else None),
            "dominant_event_type": str(raw.get("dominant_event_type", ""))[:30] or None,
            "disconfirming": str(raw.get("disconfirming", ""))[:200] or None,
            "source": "llm",
            "n_headlines": len(headlines),
        }

    if result["source"] != "fallback":
        cache.setdefault(symbol, {})[today] = result
        _save_cache(cache)
    return result
