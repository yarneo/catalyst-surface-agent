"""Typed boundary between untrusted news/model text and the trading engine.

Featherless may interpret supplied facts, but it cannot invent a tradable
symbol, cite an article it was not given, or smuggle an order through a text
field. Only instances created by ``CatalystAssessment.from_model`` may cross
into deterministic verification.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable


_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_DIRECTIONS = frozenset({"bullish", "bearish", "mixed", "unknown"})


class CatalystValidationError(ValueError):
    """A model output is malformed, ungrounded, or outside its authority."""


def _score(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise CatalystValidationError(f"{name} must be a number in [0, 1]")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise CatalystValidationError(f"{name} must be a number in [0, 1]") from exc
    if not math.isfinite(out) or not 0.0 <= out <= 1.0:
        raise CatalystValidationError(f"{name} must be a number in [0, 1]")
    return out


def _strings(name: str, value: Any, *, maximum: int = 20) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise CatalystValidationError(f"{name} must be an array of at most {maximum}")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise CatalystValidationError(f"{name} must contain non-empty strings")
    return tuple(dict.fromkeys(item.strip() for item in value))


@dataclass(frozen=True)
class CatalystFact:
    fact_id: str
    published_at: str
    headline: str
    summary: str
    symbols: tuple[str, ...]
    source: str = "alpaca_news"

    def __post_init__(self) -> None:
        if not self.fact_id or len(self.fact_id) > 100:
            raise ValueError("fact_id must be non-empty and at most 100 characters")
        if not self.headline or len(self.headline) > 500:
            raise ValueError("headline must be non-empty and at most 500 characters")
        if len(self.summary) > 4000:
            raise ValueError("summary is too long")
        if not self.symbols or any(not _TICKER.fullmatch(s) for s in self.symbols):
            raise ValueError("symbols must be uppercase ticker strings")
        try:
            stamp = dt.datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("published_at must be ISO-8601") from exc
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("published_at must be timezone-aware")

    def as_prompt_data(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "published_at": self.published_at,
            "headline": self.headline,
            "summary": self.summary,
            "symbols": list(self.symbols),
            "source": self.source,
        }


@dataclass(frozen=True)
class CausalLink:
    source_ticker: str
    target_ticker: str
    direction: str
    mechanism: str
    confidence: float
    source_fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class CatalystAssessment:
    catalyst_type: str
    factual_summary: str
    novelty: float
    surprise: float
    direction: str
    expected_half_life_minutes: int
    primary_tickers: tuple[str, ...]
    secondary_tickers: tuple[str, ...]
    causal_links: tuple[CausalLink, ...]
    confidence: float
    invalidation: str
    source_fact_ids: tuple[str, ...]

    @classmethod
    def from_model(cls, raw: Any, *, facts: Iterable[CatalystFact],
                   allowed_tickers: Iterable[str]) -> "CatalystAssessment":
        if not isinstance(raw, dict):
            raise CatalystValidationError("model output must be one JSON object")

        required = {
            "catalyst_type", "factual_summary", "novelty", "surprise",
            "direction", "expected_half_life_minutes", "primary_tickers",
            "secondary_tickers", "causal_links", "confidence", "invalidation",
            "source_fact_ids",
        }
        missing = required - raw.keys()
        unknown = raw.keys() - required
        if missing:
            raise CatalystValidationError(f"missing fields: {sorted(missing)}")
        if unknown:
            raise CatalystValidationError(f"unknown fields: {sorted(unknown)}")

        fact_rows = tuple(facts)
        fact_ids = {f.fact_id for f in fact_rows}
        directly_named = {s for f in fact_rows for s in f.symbols}
        universe = set(allowed_tickers)
        if any(not _TICKER.fullmatch(s) for s in universe):
            raise ValueError("allowed_tickers contains an invalid ticker")

        catalyst_type = raw["catalyst_type"]
        factual_summary = raw["factual_summary"]
        invalidation = raw["invalidation"]
        for name, value, limit in (
            ("catalyst_type", catalyst_type, 80),
            ("factual_summary", factual_summary, 800),
            ("invalidation", invalidation, 500),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise CatalystValidationError(f"{name} must be non-empty and <= {limit}")

        direction = raw["direction"]
        if direction not in _DIRECTIONS:
            raise CatalystValidationError(f"invalid direction: {direction!r}")
        try:
            half_life = int(raw["expected_half_life_minutes"])
        except (TypeError, ValueError) as exc:
            raise CatalystValidationError("expected_half_life_minutes must be an integer") from exc
        if isinstance(raw["expected_half_life_minutes"], bool) or not 5 <= half_life <= 10080:
            raise CatalystValidationError("expected_half_life_minutes must be in [5, 10080]")

        primary = _strings("primary_tickers", raw["primary_tickers"])
        secondary = _strings("secondary_tickers", raw["secondary_tickers"])
        if not primary:
            raise CatalystValidationError("at least one primary ticker is required")
        if not set(primary) <= directly_named:
            raise CatalystValidationError("primary ticker was not named in a supplied fact")
        if not (set(primary) | set(secondary)) <= universe:
            raise CatalystValidationError("model proposed a ticker outside the eligible universe")
        if set(primary) & set(secondary):
            raise CatalystValidationError("a ticker cannot be both primary and secondary")

        cited = _strings("source_fact_ids", raw["source_fact_ids"])
        if not cited or not set(cited) <= fact_ids:
            raise CatalystValidationError("source_fact_ids must cite only supplied facts")

        links_raw = raw["causal_links"]
        if not isinstance(links_raw, list) or len(links_raw) > 12:
            raise CatalystValidationError("causal_links must be an array of at most 12")
        links: list[CausalLink] = []
        link_fields = {"source_ticker", "target_ticker", "direction", "mechanism",
                       "confidence", "source_fact_ids"}
        for item in links_raw:
            if not isinstance(item, dict) or set(item) != link_fields:
                raise CatalystValidationError("each causal link must match the exact schema")
            source, target = item["source_ticker"], item["target_ticker"]
            if source not in universe or target not in universe:
                raise CatalystValidationError("causal link ticker is outside the eligible universe")
            if source == target:
                raise CatalystValidationError("causal link cannot point to itself")
            if item["direction"] not in _DIRECTIONS:
                raise CatalystValidationError("causal link has an invalid direction")
            mechanism = item["mechanism"]
            if not isinstance(mechanism, str) or not mechanism.strip() or len(mechanism) > 400:
                raise CatalystValidationError("causal mechanism must be non-empty and <= 400")
            link_citations = _strings("causal_link.source_fact_ids",
                                      item["source_fact_ids"], maximum=10)
            if not link_citations or not set(link_citations) <= fact_ids:
                raise CatalystValidationError("causal link cites an absent fact")
            links.append(CausalLink(source, target, item["direction"], mechanism.strip(),
                                    _score("causal_link.confidence", item["confidence"]),
                                    link_citations))

        if secondary and not set(secondary) <= {link.target_ticker for link in links}:
            raise CatalystValidationError("every secondary ticker needs a cited causal link")

        return cls(
            catalyst_type.strip(), factual_summary.strip(),
            _score("novelty", raw["novelty"]),
            _score("surprise", raw["surprise"]), direction, half_life,
            primary, secondary, tuple(links),
            _score("confidence", raw["confidence"]), invalidation.strip(), cited,
        )
