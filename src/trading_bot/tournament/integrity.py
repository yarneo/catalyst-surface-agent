"""Featherless-backed, non-expansive veto for scheduled-event integrity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .catalyst import CatalystFact
from .featherless import CommitteeResult


# These phrases mean the uncertainty the straddle was intended to buy may have
# already been released. Matching text alone cannot authorize a trade; it can
# only combine with a grounded model quorum to remove one.
_RESOLUTION_PHRASES = (
    "reported quarterly", "reports quarterly", "announced quarterly results",
    "announces quarterly results", "preannounced", "pre-announced",
    "preliminary results", "raises guidance", "raised guidance",
    "cuts guidance", "cut guidance", "lowers guidance", "lowered guidance",
    "withdraws guidance", "withdrew guidance", "earnings released",
)


@dataclass(frozen=True)
class EventIntegrityDecision:
    clear: bool
    reason: str
    cited_fact_ids: tuple[str, ...]


def evaluate_event_integrity(
        result: CommitteeResult, facts: Iterable[CatalystFact], *,
        underlying: str = "AVGO") -> EventIntegrityDecision:
    """Require a valid semantic quorum and veto evidence of early resolution.

    Featherless is deliberately asymmetric here. It can identify that the
    scheduled uncertainty is no longer intact and block the straddle. It cannot
    manufacture evidence, alter premium/risk gates, or make an otherwise
    ineligible surface tradable.
    """
    fact_rows = tuple(facts)
    if not result.valid or result.accepted is None:
        return EventIntegrityDecision(
            False, f"Featherless integrity quorum unavailable: {result.reason}", ())
    accepted = result.accepted
    if underlying not in accepted.primary_tickers:
        return EventIntegrityDecision(
            False, f"integrity assessment does not ground {underlying} as primary",
            accepted.source_fact_ids)

    cited = set(accepted.source_fact_ids)
    resolution_facts = []
    for fact in fact_rows:
        if fact.fact_id not in cited:
            continue
        text = f"{fact.headline} {fact.summary}".lower()
        if any(phrase in text for phrase in _RESOLUTION_PHRASES):
            resolution_facts.append(fact.fact_id)

    strong = (accepted.novelty >= 0.70 and accepted.surprise >= 0.70
              and accepted.confidence >= 0.70)
    if resolution_facts and strong:
        return EventIntegrityDecision(
            False, "scheduled earnings uncertainty may already be resolved",
            tuple(resolution_facts))
    return EventIntegrityDecision(
        True, "valid grounded quorum found no strong early-resolution fact",
        accepted.source_fact_ids)
