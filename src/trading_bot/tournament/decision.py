"""Pure tournament entry planner: evidence and exact risk in, intentions out."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from trading_bot.options.spreads import Spread


@dataclass(frozen=True)
class CandidateEvidence:
    catalyst_strength: float
    tape_confirmation: float
    surface_lag: float
    model_agreement: float
    spread_capture: float
    stale_mark_penalty: float = 0.0
    correlation_penalty: float = 0.0
    time_decay_penalty: float = 0.0

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [0, 1]")

    @property
    def passes(self) -> bool:
        return (
            self.catalyst_strength >= 0.60
            and self.tape_confirmation >= 0.60
            and self.surface_lag >= 0.45
            and self.model_agreement >= 2 / 3
            and self.spread_capture >= 0.65
            and self.stale_mark_penalty <= 0.25
        )

    @property
    def exceptional(self) -> bool:
        return self.passes and (
            self.catalyst_strength >= 0.85
            and self.tape_confirmation >= 0.85
            and self.surface_lag >= 0.65
            and self.model_agreement >= 0.99
            and self.spread_capture >= 0.80
        )

    @property
    def score(self) -> float:
        positive = (
            0.30 * self.catalyst_strength
            + 0.25 * self.tape_confirmation
            + 0.20 * self.surface_lag
            + 0.15 * self.model_agreement
            + 0.10 * self.spread_capture
        )
        negative = (
            0.40 * self.stale_mark_penalty
            + 0.35 * self.correlation_penalty
            + 0.25 * self.time_decay_penalty
        )
        return positive - negative


@dataclass(frozen=True)
class EntryCandidate:
    candidate_id: str
    event_id: str
    spread: Spread
    evidence: CandidateEvidence
    risk_class: str = "adaptive"

    def __post_init__(self) -> None:
        if self.risk_class not in {"adaptive", "scheduled_event"}:
            raise ValueError("risk_class must be adaptive or scheduled_event")


@dataclass(frozen=True)
class TournamentLimits:
    ordinary_candidate_pct: float = 0.04
    exceptional_candidate_pct: float = 0.08
    aggregate_live_pct: float = 0.40
    event_pct: float = 0.10
    scheduled_event_pct: float = 0.40
    daily_loss_halt_pct: float = 0.12
    account_drawdown_halt_pct: float = 0.25

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if not 0.0 < value < 1.0:
                raise ValueError(f"{name} must be in (0, 1)")
        if self.ordinary_candidate_pct > self.exceptional_candidate_pct:
            raise ValueError("ordinary candidate limit cannot exceed exceptional")
        if self.exceptional_candidate_pct > self.event_pct:
            raise ValueError("candidate limit cannot exceed event limit")
        if self.event_pct > self.aggregate_live_pct:
            raise ValueError("event limit cannot exceed aggregate limit")
        if self.scheduled_event_pct > self.aggregate_live_pct:
            raise ValueError("scheduled-event limit cannot exceed aggregate limit")


@dataclass
class EntryPlan:
    open: list[tuple[EntryCandidate, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    halted: bool = False

    @property
    def added_max_loss_usd(self) -> float:
        return sum(candidate.spread.max_loss * 100.0 * qty
                   for candidate, qty in self.open)


def plan_entries(*, equity: float, measured_start_equity: float,
                 session_start_equity: float, current_max_loss_usd: float,
                 event_exposure_usd: dict[str, float],
                 candidates: list[EntryCandidate],
                 limits: TournamentLimits = TournamentLimits()) -> EntryPlan:
    for name, value in (
        ("equity", equity), ("measured_start_equity", measured_start_equity),
        ("session_start_equity", session_start_equity),
        ("current_max_loss_usd", current_max_loss_usd),
    ):
        if not math.isfinite(value) or value < 0 or (name != "current_max_loss_usd" and value == 0):
            raise ValueError(f"{name} must be finite and positive")

    plan = EntryPlan()
    account_floor = measured_start_equity * (1.0 - limits.account_drawdown_halt_pct)
    daily_floor = session_start_equity * (1.0 - limits.daily_loss_halt_pct)
    if equity <= account_floor:
        plan.halted = True
        plan.notes.append(f"account drawdown halt: equity {equity:.2f} <= {account_floor:.2f}")
        return plan
    if equity <= daily_floor:
        plan.halted = True
        plan.notes.append(f"daily loss halt: equity {equity:.2f} <= {daily_floor:.2f}")
        return plan

    aggregate_room = max(0.0, equity * limits.aggregate_live_pct - current_max_loss_usd)
    if aggregate_room <= 0:
        plan.notes.append("aggregate live-risk ceiling is full")
        return plan

    event_used = dict(event_exposure_usd)
    for candidate in sorted(candidates, key=lambda item: item.evidence.score, reverse=True):
        if not candidate.evidence.passes:
            plan.notes.append(f"{candidate.candidate_id}: evidence gates failed")
            continue
        if candidate.spread.net_credit >= 0:
            plan.notes.append(f"{candidate.candidate_id}: not a debit structure")
            continue

        if candidate.risk_class == "scheduled_event":
            if candidate.spread.structure != "long_straddle":
                plan.notes.append(
                    f"{candidate.candidate_id}: scheduled-event risk requires a long straddle")
                continue
            if not candidate.evidence.exceptional:
                plan.notes.append(
                    f"{candidate.candidate_id}: scheduled-event evidence is not exceptional")
                continue

        candidate_pct = (
            limits.scheduled_event_pct
            if candidate.risk_class == "scheduled_event"
            else limits.exceptional_candidate_pct
            if candidate.evidence.exceptional
            else limits.ordinary_candidate_pct)
        event_pct = (
            limits.scheduled_event_pct
            if candidate.risk_class == "scheduled_event"
            else limits.event_pct)
        event_room = max(0.0, equity * event_pct
                         - event_used.get(candidate.event_id, 0.0))
        room = min(aggregate_room, equity * candidate_pct, event_room)
        qty = candidate.spread.contracts_for_risk(room)
        if qty < 1:
            plan.notes.append(f"{candidate.candidate_id}: insufficient risk room")
            continue

        used = candidate.spread.max_loss * 100.0 * qty
        plan.open.append((candidate, qty))
        aggregate_room -= used
        event_used[candidate.event_id] = event_used.get(candidate.event_id, 0.0) + used
        if aggregate_room < min(c.spread.max_loss * 100.0 for c in candidates):
            break

    if not plan.open:
        plan.notes.append("no candidate passed evidence and risk gates")
    return plan
