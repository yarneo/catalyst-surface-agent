"""Non-trading challengers for the leveraged Nasdaq regime sleeve.

This module is deliberately pure except for the explicit JSONL recorder. It
cannot submit orders. The live executor supplies the observations it already
used, and this module marks several virtual portfolios from one decision price
to the next, including estimated turnover costs.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


SHADOW_LEDGER = Path(__file__).resolve().parents[2] / "data" / "shadow_regime.jsonl"
SHADOW_STATE = Path(__file__).resolve().parents[2] / "data" / "shadow_regime_state.json"
SHADOW_INITIAL_EQUITY = 100_000.0
SHADOW_COST_BPS = 5.0


@dataclass(frozen=True)
class ShadowRegimeDecision:
    live_target_weight: float
    shadow_target_weight: float
    capital_cap_weight: float
    volatility_cap_weight: float
    health_multiplier: float
    limiting_reason: str


def propose_shadow_target(
    *,
    live_allocation: float,
    vol_scale: float,
    realized_vol: float | None,
    regime_on: bool,
    health_verdict: str,
    max_capital_weight: float = 0.25,
    max_volatility_contribution: float = 0.06,
    decaying_multiplier: float = 0.50,
) -> ShadowRegimeDecision:
    """Return a no-trade QLD challenger target.

    ``max_volatility_contribution`` limits the position's standalone annualized
    volatility contribution (weight × QLD volatility). The live dashboard also
    reports covariance-based component risk; this conservative proxy is usable
    inside the executor without making its decision depend on another data feed.
    """
    live_target = max(0.0, live_allocation * vol_scale) if regime_on else 0.0
    if not regime_on:
        return ShadowRegimeDecision(live_target, 0.0, 0.0, 0.0, 0.0, "regime_off")

    capital_cap = min(live_target, max_capital_weight)
    if realized_vol is not None and realized_vol > 0:
        vol_cap = min(capital_cap, max_volatility_contribution / realized_vol)
    else:
        vol_cap = capital_cap

    verdict = health_verdict.upper()
    if verdict in {"DISABLED", "ERROR", "TRIPPED", "FAST_TRIPPED"}:
        health_mult = 0.0
    elif verdict == "DECAYING":
        health_mult = decaying_multiplier
    else:
        health_mult = 1.0
    shadow = max(0.0, vol_cap * health_mult)

    if health_mult == 0:
        reason = f"health_{verdict.lower()}"
    elif health_mult < 1:
        reason = "decaying_throttle"
    elif vol_cap < capital_cap:
        reason = "volatility_contribution_cap"
    elif capital_cap < live_target:
        reason = "capital_cap"
    else:
        reason = "same_as_live"
    return ShadowRegimeDecision(
        live_target_weight=live_target,
        shadow_target_weight=shadow,
        capital_cap_weight=capital_cap,
        volatility_cap_weight=vol_cap,
        health_multiplier=health_mult,
        limiting_reason=reason,
    )


def _health_multiplier(verdict: str, decaying_multiplier: float = 0.50) -> float:
    verdict = verdict.upper()
    if verdict in {"DISABLED", "ERROR", "TRIPPED", "FAST_TRIPPED"}:
        return 0.0
    # DECAYING is warning-only in production until the six-week tripwire. The
    # legacy risk_cap_25 challenger applies its own extra decay throttle inside
    # propose_shadow_target; the production/60/50/stress benchmarks must mirror
    # the actual binary health policy.
    return 1.0


def propose_shadow_targets(
    *,
    live_allocation: float,
    vol_scale: float,
    realized_vol: float | None,
    regime_on: bool,
    health_verdict: str,
) -> dict[str, float]:
    """Targets for production and the shadow-only QLD allocation challengers.

    The production target is included as an apples-to-apples virtual benchmark;
    it does not read actual fills. ``stress_70`` keeps the 70% ceiling in calm
    markets but cuts harder when leveraged QLD volatility is already elevated.
    """
    live = max(0.0, live_allocation * vol_scale) if regime_on else 0.0
    health = _health_multiplier(health_verdict)
    if not regime_on:
        return {name: 0.0 for name in (
            "production_70", "cap_60", "cap_50", "stress_70", "risk_cap_25"
        )}

    stress_cap = live
    if realized_vol is not None:
        if realized_vol >= 0.50:
            stress_cap = min(stress_cap, 0.35)
        elif realized_vol >= 0.35:
            stress_cap = min(stress_cap, 0.50)

    conservative = propose_shadow_target(
        live_allocation=live_allocation,
        vol_scale=vol_scale,
        realized_vol=realized_vol,
        regime_on=regime_on,
        health_verdict=health_verdict,
    )
    return {
        "production_70": live * health,
        "cap_60": min(live, 0.60) * health,
        "cap_50": min(live, 0.50) * health,
        "stress_70": stress_cap * health,
        "risk_cap_25": conservative.shadow_target_weight,
    }


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def record_shadow_portfolios(
    targets: dict[str, float],
    *,
    price: float,
    observed_equity: float,
    realized_vol: float | None,
    regime_on: bool,
    health_verdict: str,
    session_date: str | None = None,
    path: Path = SHADOW_LEDGER,
    state_path: Path = SHADOW_STATE,
    initial_equity: float = SHADOW_INITIAL_EQUITY,
    cost_bps: float = SHADOW_COST_BPS,
) -> dict:
    """Advance all virtual portfolios once per session and append one row.

    Each interval earns the *previous* target weight times QLD's price return.
    A turnover charge is deducted whenever the target changes. Repeated live
    invocations on the same session are idempotent, so restarts do not create
    fake extra observations or costs.
    """
    if price <= 0:
        raise ValueError("shadow mark price must be positive")
    session = session_date or datetime.now(timezone.utc).date().isoformat()
    state = _load_state(state_path)
    if state.get("session_date") == session:
        return state.get("latest_row", {})

    previous_price = float(state.get("price") or price)
    price_return = price / previous_price - 1.0
    previous = state.get("variants") or {}
    variants: dict[str, dict] = {}
    for name, raw_target in targets.items():
        target = min(1.0, max(0.0, float(raw_target)))
        prior = previous.get(name) or {}
        prior_equity = float(prior.get("equity", initial_equity))
        prior_weight = float(prior.get("target_weight", 0.0))
        gross_return = prior_weight * price_return
        turnover = abs(target - prior_weight)
        cost_fraction = turnover * cost_bps / 10_000.0
        new_equity = prior_equity * (1.0 + gross_return - cost_fraction)
        peak = max(float(prior.get("peak_equity", initial_equity)), new_equity)
        variants[name] = {
            "target_weight": round(target, 8),
            "equity": round(new_equity, 2),
            "daily_pnl": round(new_equity - prior_equity, 2),
            "total_pnl": round(new_equity - initial_equity, 2),
            "return_fraction": round(new_equity / initial_equity - 1.0, 8),
            "drawdown_fraction": round(new_equity / peak - 1.0, 8),
            "turnover_fraction": round(
                float(prior.get("turnover_fraction", 0.0)) + turnover, 8
            ),
            "estimated_cost": round(
                float(prior.get("estimated_cost", 0.0)) + prior_equity * cost_fraction, 2
            ),
            "peak_equity": round(peak, 2),
        }

    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_date": session,
        "symbol": "QLD",
        "price": round(float(price), 6),
        "price_return": round(price_return, 8),
        "observed_account_equity": round(float(observed_equity), 2),
        "realized_vol": round(float(realized_vol), 6) if realized_vol is not None else None,
        "regime_on": bool(regime_on),
        "health_verdict": health_verdict.upper(),
        "cost_bps": float(cost_bps),
        "variants": variants,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")
    new_state = {
        "schema_version": 1,
        "session_date": session,
        "price": float(price),
        "variants": variants,
        "latest_row": row,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    from .runtime_state import atomic_json
    atomic_json(state_path, new_state)
    return row


def record_shadow_decision(
    decision: ShadowRegimeDecision,
    *,
    equity: float,
    realized_vol: float | None,
    regime_on: bool,
    health_verdict: str,
    path: Path = SHADOW_LEDGER,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": "QLD",
        "equity": round(float(equity), 2),
        "realized_vol": round(float(realized_vol), 6) if realized_vol is not None else None,
        "regime_on": bool(regime_on),
        "health_verdict": health_verdict.upper(),
        **asdict(decision),
        "live_target_usd": round(equity * decision.live_target_weight, 2),
        "shadow_target_usd": round(equity * decision.shadow_target_weight, 2),
    }
    with path.open("a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")
