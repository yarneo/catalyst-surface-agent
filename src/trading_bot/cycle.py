"""Decide what one portfolio cycle should do — without doing any of it.

This module exists because of a specific failure. The first version of
``scripts/run_portfolio.py`` put 316 lines of sequencing, broker calls and
printing inside one ``main()``. An audit on 2026-08-04 found six bugs in it,
and every single one was an *interaction* bug rather than a wrong calculation:

  * the wind-down closed a position and the risk guard then closed it again,
    because the guard was handed a position list read before the wind-down;
  * the kill switch stopped latching, so a breaker that tripped at premarket
    disarmed itself by the open;
  * ``peak_equity`` only advanced on a code path that some cycles skipped, so
    drawdown was measured against a stale peak and the kill switch fired late;
  * a missing champion file read as "no symbol is managed" and would have
    liquidated the ETF book.

None of those are visible in a unit test of any single component. They live in
the seams. Meanwhile ``PortfolioRiskGuard``, which was already split into a pure
``evaluate()`` and a separate apply step, had none.

So the cycle gets the same treatment: ``plan_cycle()`` is a pure function from
(account state, positions, config) to a ``CyclePlan`` describing every decision.
The script's only job is to execute that plan. Sequencing is now something you
can assert on rather than something you have to run a market to observe.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ownership import OwnershipAudit, Sleeve, audit_ownership
from .risk import GuardResult, PortfolioRiskGuard, RiskLimits


@dataclass
class SessionState:
    """The persisted bits of ``ExecutorState`` the planner needs.

    Kept separate from ``ExecutorState`` so the planner has no filesystem
    dependency and tests can construct one inline.
    """

    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    session_date: str = ""
    halted_today: bool = False
    killed: bool = False
    baseline_source: str = ""


@dataclass
class CyclePlan:
    audit: OwnershipAudit
    state: SessionState
    abort_reason: str | None = None
    wind_down: dict[str, float] = field(default_factory=dict)
    guard: GuardResult | None = None
    run_sleeves: bool = False
    skip_reason: str | None = None
    # Positions the guard was actually evaluated against — i.e. after removing
    # anything the wind-down is closing this cycle.
    guarded_positions: dict[str, dict] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def guard_will_act(self) -> bool:
        """True if the guard has at least one risk-reducing order to place."""
        if self.guard is None:
            return False
        return any(a.target_value < a.current_value for a in self.guard.actions)


def plan_cycle(
    *,
    phase: str,
    today: str,
    equity: float,
    positions: dict[str, dict],
    state: SessionState,
    sleeves: list[Sleeve],
    limits: RiskLimits,
    champion_symbols: set[str] | None = None,
    champion_store_exists: bool = True,
    wind_down_disabled: bool = False,
    skip_guard: bool = False,
    prior_close_equity: float | None = None,
) -> CyclePlan:
    """Decide everything for one cycle. Places no orders and touches no files.

    ``champion_symbols`` is the set the ETF sleeve is actually trading.
    ``champion_store_exists`` distinguishes "the gate retired everything"
    (legitimate) from "the file is missing" (a data failure that must NOT be
    read as a mandate to liquidate).
    """
    # --- session bookkeeping ------------------------------------------------
    # Done here, unconditionally, because it used to live inside the ETF
    # sleeve's executor and was skipped whenever that sleeve didn't run.
    st = SessionState(**vars(state))
    new_session = st.session_date != today
    if new_session:
        st.session_date = today
        st.day_start_equity = equity
        st.halted_today = False
    if prior_close_equity is not None and (new_session or st.baseline_source != "broker_prior_close"):
        import math
        if not math.isfinite(prior_close_equity) or prior_close_equity <= 0:
            raise ValueError("valid broker prior-close equity required")
        st.day_start_equity = prior_close_equity
        st.baseline_source = "broker_prior_close"
    st.peak_equity = max(st.peak_equity, equity)

    plan = CyclePlan(audit=OwnershipAudit(), state=st)

    # --- ownership audit ----------------------------------------------------
    active: dict[str, set[str]] = {}
    if champion_store_exists:
        active["etf"] = champion_symbols or set()
    else:
        plan.warnings.append(
            "champion store missing — skipping the unmanaged-position check; "
            "nothing will be wound down this cycle"
        )

    plan.audit = audit_ownership(
        {k: v["market_value"] for k, v in positions.items()}, sleeves, active
    )

    if plan.audit.contested:
        plan.abort_reason = (
            "contested symbols would make two sleeves fight over the same "
            f"position: {sorted(plan.audit.contested)}"
        )
        return plan

    # --- what to wind down --------------------------------------------------
    # unmanaged: the DSR gate retired the strategy, so holding its position is
    # incoherent — automatic. disabled_holdings: a human turned the sleeve off,
    # which is a statement of intent, not a verdict on edge — opt-in.
    wind_down = dict(plan.audit.unmanaged)
    if wind_down_disabled:
        wind_down.update(plan.audit.disabled_holdings)
    # Only the open phase places orders.
    plan.wind_down = wind_down if phase == "open" else {}

    # --- risk guard ---------------------------------------------------------
    # Evaluate against the book as it will be AFTER the wind-down. Otherwise the
    # guard re-acts on something already being closed and double-sells it.
    guarded = {k: v for k, v in positions.items() if k not in plan.wind_down}
    plan.guarded_positions = guarded

    if skip_guard:
        plan.run_sleeves = phase == "open"
        plan.skip_reason = None if plan.run_sleeves else f"phase={phase}"
        return plan

    guard = PortfolioRiskGuard(limits, sleeves)
    plan.guard = guard.evaluate(
        equity=equity,
        peak_equity=st.peak_equity,
        day_start_equity=st.day_start_equity or equity,
        positions=guarded,
        already_killed=st.killed,
        already_halted=st.halted_today,
    )

    if plan.guard.kill_switch:
        st.killed = True
        st.halted_today = True
        plan.run_sleeves = False
        plan.skip_reason = "kill switch armed — manual restart required"
        return plan

    if plan.guard.halt_trading:
        st.halted_today = True
        plan.run_sleeves = False
        plan.skip_reason = "daily loss breaker — no new risk today"
        return plan

    if phase != "open":
        plan.run_sleeves = False
        plan.skip_reason = f"phase={phase}: daily sleeves run at the open only"
        return plan

    # Guard orders and sleeve orders can want the same trade; the sleeve re-reads
    # positions before the guard's market order has filled and duplicates it.
    # De-risking wins, sleeves rebalance next cycle on a settled book. This does
    # not loop: each sleeve's allocation is clamped to the same cap the guard
    # enforces, so once the trim lands the guard goes quiet.
    if plan.guard_will_act or plan.wind_down:
        plan.run_sleeves = False
        plan.skip_reason = (
            "de-risking orders placed this cycle — deferring sleeves until they settle"
        )
        return plan

    plan.run_sleeves = True
    return plan
