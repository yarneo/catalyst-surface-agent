"""Pure policy for a reusable, fail-closed weekly earnings-volatility engine.

This module has no network client and no order method.  It turns independently
collected calendar facts, market sessions, historical replay statistics, and a
current executable surface into a deterministic weekly plan.  The separation is
intentional: discovery can be creative, but promotion, sizing, and lifecycle
management must be reproducible without an LLM.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping

from trading_bot.options.clock import ET


_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")


class EventTiming(str, Enum):
    BEFORE_OPEN = "before_open"
    AFTER_CLOSE = "after_close"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WeeklyWindow:
    start: dt.datetime
    deadline: dt.datetime

    def __post_init__(self) -> None:
        for name, value in (("start", self.start), ("deadline", self.deadline)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.deadline <= self.start:
            raise ValueError("weekly deadline must follow its start")


@dataclass(frozen=True)
class CalendarFact:
    symbol: str
    event_date: dt.date
    timing: EventTiming
    source: str
    fact_id: str
    summary: str
    event_type: str = "earnings"

    def __post_init__(self) -> None:
        if not _TICKER.fullmatch(self.symbol):
            raise ValueError("calendar fact symbol must be an uppercase ticker")
        if not self.source or not self.fact_id or not self.summary:
            raise ValueError("calendar fact provenance cannot be empty")
        if self.event_type != "earnings":
            raise ValueError("the first weekly engine supports earnings only")


@dataclass(frozen=True)
class EventConsensus:
    symbol: str
    event_date: dt.date | None
    timing: EventTiming
    event_type: str
    sources: tuple[str, ...]
    fact_ids: tuple[str, ...]
    confirmed: bool
    reasons: tuple[str, ...]


def calendar_consensus(
    facts: Iterable[CalendarFact], *, minimum_sources: int = 2,
) -> tuple[EventConsensus, ...]:
    """Require a date quorum plus explicit, uncontradicted session evidence.

    Multiple endpoints from one vendor still count as one source. At least two
    independent sources must agree on the date; at least one of those sources
    must supply a concrete before/after-market session and none may contradict
    it. A conflict is never resolved by confidence scoring or by an LLM.
    """
    if minimum_sources < 2:
        raise ValueError("calendar quorum must require at least two sources")
    by_symbol: dict[str, list[CalendarFact]] = {}
    for fact in facts:
        by_symbol.setdefault(fact.symbol, []).append(fact)

    output: list[EventConsensus] = []
    for symbol, rows in sorted(by_symbol.items()):
        source_rows: dict[str, list[CalendarFact]] = {}
        for row in rows:
            source_rows.setdefault(row.source, []).append(row)
        reasons: list[str] = []
        conflicting_source = any(
            len({(row.event_date, row.timing) for row in values}) > 1
            for values in source_rows.values())
        if conflicting_source:
            reasons.append("one source supplied conflicting schedules")

        votes: dict[tuple[dt.date, EventTiming, str], set[str]] = {}
        for row in rows:
            if row.timing is EventTiming.UNKNOWN:
                continue
            votes.setdefault(
                (row.event_date, row.timing, row.event_type), set()).add(row.source)
        winners = [(key, sources) for key, sources in votes.items()
                   if len(sources) >= minimum_sources]
        if len(winners) != 1:
            reasons.append(
                "no unique independent date quorum with explicit session evidence"
                if not winners else "multiple calendar quorums conflict")
            # Preserve a two-source date consensus even when one provider does
            # not publish the session. This is not trade confirmation: it only
            # permits bounded news enrichment and semantic research for the
            # agreed date. schedule_event still refuses this row.
            date_votes: dict[tuple[dt.date, str], set[str]] = {}
            for row in rows:
                date_votes.setdefault(
                    (row.event_date, row.event_type), set()).add(row.source)
            date_winners = [(key, sources) for key, sources in date_votes.items()
                            if len(sources) >= minimum_sources]
            event_date = None
            event_type = "earnings"
            agreeing_sources: set[str] = set()
            if len(date_winners) == 1:
                (event_date, event_type), agreeing_sources = date_winners[0]
                date_dissent = {
                    row.source for row in rows
                    if row.source not in agreeing_sources
                    or (row.event_date, row.event_type) != (event_date, event_type)
                }
                session_votes: dict[EventTiming, set[str]] = {}
                for row in rows:
                    if (row.source in agreeing_sources
                            and row.event_date == event_date
                            and row.event_type == event_type
                            and row.timing is not EventTiming.UNKNOWN):
                        session_votes.setdefault(row.timing, set()).add(row.source)
                if (not conflicting_source and not date_dissent
                        and len(session_votes) == 1):
                    timing, timing_sources = next(iter(session_votes.items()))
                    reasons.pop()  # replace the provisional no-full-quorum reason
                    reasons.append(
                        f"{len(agreeing_sources)} independent sources agree on date; "
                        f"explicit {timing.value} session from "
                        f"{', '.join(sorted(timing_sources))} is uncontradicted")
                    output.append(EventConsensus(
                        symbol, event_date, timing, event_type,
                        tuple(sorted(agreeing_sources)),
                        tuple(sorted(row.fact_id for row in rows
                                     if row.source in agreeing_sources
                                     and row.event_date == event_date)),
                        True, tuple(reasons)))
                    continue
                if len(session_votes) > 1:
                    reasons.append("calendar sources conflict on event session")
                else:
                    reasons.append(
                        f"{len(agreeing_sources)} independent sources agree on date; "
                        "explicit session evidence is still required")
            output.append(EventConsensus(
                symbol, event_date, EventTiming.UNKNOWN, event_type,
                tuple(sorted(agreeing_sources or source_rows)),
                tuple(sorted(row.fact_id for row in rows
                             if not agreeing_sources or row.source in agreeing_sources)), False,
                tuple(reasons)))
            continue

        (event_date, timing, event_type), agreeing_sources = winners[0]
        dissent = {
            row.source for row in rows
            if row.timing is not EventTiming.UNKNOWN
            and (row.event_date, row.timing, row.event_type)
            != (event_date, timing, event_type)
        }
        if dissent:
            reasons.append(f"conflicting source(s): {', '.join(sorted(dissent))}")
        confirmed = not conflicting_source and not dissent
        if confirmed:
            reasons.append(
                f"{len(agreeing_sources)} independent sources agree on date and session")
        output.append(EventConsensus(
            symbol, event_date, timing, event_type,
            tuple(sorted(agreeing_sources)),
            tuple(sorted(row.fact_id for row in rows
                         if row.source in agreeing_sources)),
            confirmed, tuple(reasons)))
    return tuple(output)


@dataclass(frozen=True)
class EventSchedule:
    event_id: str
    symbol: str
    event_date: dt.date
    timing: EventTiming
    expiry: str
    entry_start: dt.datetime
    entry_end: dt.datetime
    event_at: dt.datetime
    exit_at: dt.datetime
    emergency_flat_by: dt.datetime

    def __post_init__(self) -> None:
        stamps = (self.entry_start, self.entry_end, self.event_at,
                  self.exit_at, self.emergency_flat_by)
        if any(value.tzinfo is None or value.utcoffset() is None for value in stamps):
            raise ValueError("event schedule timestamps must be timezone-aware")
        if not self.entry_start < self.entry_end < self.event_at < self.exit_at \
                < self.emergency_flat_by:
            raise ValueError("event schedule timestamps are not ordered")


def _session_dates(sessions: Iterable[dt.date | str]) -> tuple[dt.date, ...]:
    parsed = {dt.date.fromisoformat(value) if isinstance(value, str) else value
              for value in sessions}
    return tuple(sorted(parsed))


def schedule_event(
    consensus: EventConsensus,
    *,
    sessions: Iterable[dt.date | str],
    expiries: Iterable[dt.date | str],
    window: WeeklyWindow,
) -> EventSchedule:
    """Build the entry/exit clock from the exchange calendar, not weekdays."""
    if not consensus.confirmed or consensus.event_date is None:
        raise ValueError("calendar consensus is not confirmed")
    if consensus.timing is EventTiming.UNKNOWN:
        raise ValueError("event session is unknown")
    days = _session_dates(sessions)
    event_day = consensus.event_date
    if event_day not in days:
        raise ValueError("event date is not an exchange session")
    position = days.index(event_day)
    if consensus.timing is EventTiming.AFTER_CLOSE:
        if position + 1 >= len(days):
            raise ValueError("next exchange session is unavailable")
        entry_day, exit_day = event_day, days[position + 1]
        event_at = dt.datetime.combine(event_day, dt.time(16, 0), ET)
    else:
        if position == 0:
            raise ValueError("previous exchange session is unavailable")
        entry_day, exit_day = days[position - 1], event_day
        # A session label is sufficient for policy.  09:00 is deliberately a
        # boundary marker, not a claim about the issuer's exact release minute.
        event_at = dt.datetime.combine(event_day, dt.time(9, 0), ET)

    entry_start = dt.datetime.combine(entry_day, dt.time(15, 20), ET)
    entry_end = dt.datetime.combine(entry_day, dt.time(15, 40), ET)
    exit_at = dt.datetime.combine(exit_day, dt.time(9, 45), ET)
    emergency = dt.datetime.combine(exit_day, dt.time(15, 30), ET)
    if entry_end < window.start:
        raise ValueError("event entry occurs before the weekly window")
    if exit_at >= window.deadline:
        raise ValueError("event cannot be exited before the weekly deadline")

    expiry_days = _session_dates(expiries)
    eligible_expiries = [value for value in expiry_days if value >= exit_day]
    if not eligible_expiries:
        raise ValueError("no listed expiry survives through the exit session")
    expiry = eligible_expiries[0]
    return EventSchedule(
        event_id=f"{consensus.symbol.lower()}-earnings-{event_day.isoformat()}",
        symbol=consensus.symbol, event_date=event_day,
        timing=consensus.timing, expiry=expiry.isoformat(),
        entry_start=entry_start, entry_end=entry_end, event_at=event_at,
        exit_at=exit_at, emergency_flat_by=emergency)


@dataclass(frozen=True)
class ReplaySummary:
    sample_size: int
    last_mean: float
    last_median: float
    last_win_rate: float
    adverse_mean: float
    adverse_median: float
    adverse_win_rate: float
    premium_median: float
    premium_p75: float

    def __post_init__(self) -> None:
        values = (self.last_mean, self.last_median, self.last_win_rate,
                  self.adverse_mean, self.adverse_median,
                  self.adverse_win_rate, self.premium_median, self.premium_p75)
        if self.sample_size < 0 or any(not math.isfinite(value) for value in values):
            raise ValueError("replay statistics must be finite")
        if not 0 <= self.last_win_rate <= 1 or not 0 <= self.adverse_win_rate <= 1:
            raise ValueError("replay win rates must be in [0, 1]")
        if self.premium_median <= 0 or self.premium_p75 <= 0:
            raise ValueError("historical premiums must be positive")

    @property
    def conservative_edge(self) -> float:
        return min(self.last_mean, self.last_median,
                   self.adverse_mean, self.adverse_median)


@dataclass(frozen=True)
class PromotionPolicy:
    min_events: int = 6
    min_last_mean: float = 0.0
    min_last_median: float = 0.0
    min_last_win_rate: float = 0.55
    min_adverse_mean: float = 0.0
    min_adverse_median: float = 0.0
    min_adverse_win_rate: float = 0.50
    max_premium_to_spot: float = 0.085
    max_premium_vs_historical_median: float = 1.25
    max_total_spread_pct: float = 0.05


@dataclass(frozen=True)
class PromotionDecision:
    symbol: str
    promoted: bool
    conservative_edge: float
    reasons: tuple[str, ...]


def evaluate_promotion(
    *,
    consensus: EventConsensus,
    semantic_confirmed: bool,
    replay: ReplaySummary | None,
    schedule: EventSchedule | None,
    current_premium_to_spot: float | None,
    current_total_spread_pct: float | None,
    require_current_surface: bool = True,
    schedule_error: str | None = None,
    replay_error: str | None = None,
    policy: PromotionPolicy = PromotionPolicy(),
) -> PromotionDecision:
    """Frozen promotion rule; every failed input produces an explicit reason."""
    reasons: list[str] = []
    if not consensus.confirmed:
        reasons.append(
            "calendar lacks an independent date quorum or explicit, uncontradicted session")
        reasons.extend(reason for reason in consensus.reasons
                       if reason not in reasons)
        # Every later stage depends on confirmed date + session. Reporting each
        # skipped stage as an independent outage hid the actual root cause.
        return PromotionDecision(consensus.symbol, False, 0.0, tuple(reasons))
    if not semantic_confirmed:
        reasons.append("Featherless event/status quorum is unavailable")
    if schedule is None:
        reasons.append(schedule_error or
                       "entry and next-session exit do not fit the weekly window")
    if schedule is None:
        return PromotionDecision(consensus.symbol, False, 0.0, tuple(reasons))
    if replay is None:
        reasons.append(replay_error or "historical option replay is unavailable")
        edge = 0.0
    else:
        edge = replay.conservative_edge
        checks = (
            (replay.sample_size >= policy.min_events,
             f"replay sample {replay.sample_size} is below {policy.min_events}"),
            (replay.last_mean > policy.min_last_mean, "last-trade mean is not positive"),
            (replay.last_median > policy.min_last_median, "last-trade median is not positive"),
            (replay.last_win_rate >= policy.min_last_win_rate,
             "last-trade win rate is below threshold"),
            (replay.adverse_mean > policy.min_adverse_mean,
             "adverse-envelope mean is not positive"),
            (replay.adverse_median > policy.min_adverse_median,
             "adverse-envelope median is not positive"),
            (replay.adverse_win_rate >= policy.min_adverse_win_rate,
             "adverse-envelope win rate is below threshold"),
        )
        reasons.extend(message for passed, message in checks if not passed)

    if require_current_surface:
        if current_premium_to_spot is None or not math.isfinite(current_premium_to_spot):
            reasons.append("current executable premium is unavailable")
        elif current_premium_to_spot > policy.max_premium_to_spot:
            reasons.append("current executable premium exceeds the absolute gate")
        elif replay is not None and current_premium_to_spot > (
                replay.premium_median * policy.max_premium_vs_historical_median):
            reasons.append("current executable premium is rich versus replay history")
        if current_total_spread_pct is None or not math.isfinite(current_total_spread_pct):
            reasons.append("current combined spread is unavailable")
        elif current_total_spread_pct > policy.max_total_spread_pct:
            reasons.append("current combined spread exceeds the liquidity gate")
    return PromotionDecision(
        consensus.symbol, not reasons, edge, tuple(reasons or ("all frozen gates pass",)))


@dataclass(frozen=True)
class PortfolioPolicy:
    aggregate_risk_pct: float = 0.40
    per_event_risk_pct: float = 0.20
    singleton_risk_pct: float = 0.40

    def __post_init__(self) -> None:
        if not 0 < self.per_event_risk_pct <= self.singleton_risk_pct \
                <= self.aggregate_risk_pct <= 1:
            raise ValueError("invalid portfolio risk hierarchy")


def allocate_event_risk(
    decisions: Iterable[PromotionDecision], *, equity: float,
    policy: PortfolioPolicy = PortfolioPolicy(),
) -> dict[str, float]:
    """Allocate exact max-loss dollars across promoted overlapping events."""
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("equity must be finite and positive")
    rows = [row for row in decisions if row.promoted]
    if not rows:
        return {}
    if len(rows) == 1:
        return {rows[0].symbol: equity * policy.singleton_risk_pct}

    cap = equity * policy.per_event_risk_pct
    total = equity * policy.aggregate_risk_pct
    scores = {row.symbol: max(row.conservative_edge, 1e-6) for row in rows}
    allocations = {symbol: 0.0 for symbol in scores}
    remaining = set(scores)
    room = total
    # Water-fill by conservative edge while respecting the per-event cap.
    while remaining and room > 1e-9:
        score_sum = sum(scores[symbol] for symbol in remaining)
        additions = {
            symbol: min(cap - allocations[symbol], room * scores[symbol] / score_sum)
            for symbol in remaining
        }
        used = sum(max(0.0, value) for value in additions.values())
        if used <= 1e-9:
            break
        for symbol, value in additions.items():
            allocations[symbol] += max(0.0, value)
        room -= used
        remaining = {symbol for symbol in remaining
                     if allocations[symbol] < cap - 1e-9}
    return {symbol: value for symbol, value in sorted(allocations.items()) if value > 0}


def event_lifecycle(
    *, now: dt.datetime, schedule: EventSchedule, has_position: bool,
    entry_was_attempted: bool, global_deadline: dt.datetime,
) -> str:
    """Return WAIT, ENTER, HOLD, EXIT, or DONE for one planned event."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(ET)
    deadline = global_deadline.astimezone(ET)
    if has_position:
        return "EXIT" if now >= min(schedule.exit_at, deadline) else "HOLD"
    if now >= deadline or entry_was_attempted or now > schedule.entry_end:
        return "DONE"
    if schedule.entry_start <= now <= schedule.entry_end:
        return "ENTER"
    return "WAIT"


def risk_by_event(open_rows: Iterable[object], event_by_entry: Mapping[str, str]) -> dict[str, float]:
    """Compute exact registered max-loss dollars for a multi-event book."""
    output: dict[str, float] = {}
    for row in open_rows:
        event_id = event_by_entry.get(str(getattr(row, "id")))
        if not event_id:
            continue
        risk = float(getattr(row, "max_loss")) * 100.0 * int(getattr(row, "qty"))
        output[event_id] = output.get(event_id, 0.0) + risk
    return output
