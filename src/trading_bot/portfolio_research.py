"""Frozen whole-portfolio experiments. No broker imports or order authority.

Daily simulations use completed signals and following-session opens. Historical
champion selection and intraday execution are NOT reconstructed; the parity
gate exposes these limitations instead of treating a proxy as the live bot.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, asdict
from datetime import date
import numpy as np
import pandas as pd

from .live.managed_futures_executor import idle_router_targets
from .ownership import FACTOR_CAPS, factor_exposures, loading_of
from .shadow_regime import propose_shadow_targets

MODEL_VERSION = "portfolio-lab-v4-production-rules"


@dataclass(frozen=True)
class Experiment:
    name: str
    cap: str = "production_70"
    band: float = 0.0
    confirm: int = 1
    cooldown: int = 0
    recovery: int = 1
    etf: bool = True
    pead_weight: float = 0.0


EXPERIMENTS = (
    Experiment("baseline_70"), Experiment("cap_60", cap="cap_60"),
    Experiment("cap_50", cap="cap_50"), Experiment("stress_70", cap="stress_70"),
    Experiment("risk_cap_25", cap="risk_cap_25"),
    Experiment("hysteresis_1pct", band=0.01),
    Experiment("confirmation_2_sessions", confirm=2),
    Experiment("cooldown_5_sessions", cooldown=5),
    Experiment("recovery_3_sessions", recovery=3),
    Experiment("without_etf", etf=False),
    Experiment("pead_10pct", pead_weight=0.10),
)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:20]


def regime_states(close, sma, spec):
    states, active, up, down, cooldown = [], False, 0, 0, 0
    for px, avg in zip(close, sma):
        if not np.isfinite(avg):
            states.append(False)
            continue
        up = up + 1 if px > avg * (1 + spec.band) else 0
        down = down + 1 if px <= avg * (1 - spec.band) else 0
        if active and down >= spec.confirm:
            active, cooldown = False, spec.cooldown
        elif not active:
            if cooldown > 0:
                cooldown -= 1
            elif up >= max(spec.confirm, spec.recovery):
                active = True
        states.append(active)
    return np.asarray(states)


def target_weights(*, regime_on, qqq_vol, verdict, etf_targets, spec, policy):
    """Shared production sizing rules with explicit residual allocation."""
    vol_scale = min(1.0, 0.18 / max(qqq_vol, 1e-9))
    variants = propose_shadow_targets(live_allocation=0.70, vol_scale=vol_scale,
        realized_vol=qqq_vol * 2, regime_on=regime_on, health_verdict=verdict)
    target = {s: min(0.60, max(0.0, float(w))) for s, w in etf_targets.items()} if spec.etf else {}
    # ETF signals divide one common equity budget. Reserve cash and BIL first.
    available = 0.98 - policy["bil_buffer"]
    total_etf = sum(target.values())
    if total_etf > available:
        target = {s: w * available / total_etf for s, w in target.items()}
    target["QLD"] = min(variants[spec.cap], max(0, available - sum(target.values())))
    if spec.pead_weight:
        target["PEAD_MODEL"] = min(spec.pead_weight, max(0, available - sum(target.values())))
    idle = min(0.60, max(0, available - sum(target.values())))
    router, _, _ = idle_router_targets(idle, bull=regime_on,
        regime_nasdaq=100000 * target["QLD"], idle_rule=policy["idle_rule"], ma_window=policy["ma_window"])
    target.update({s: min(0.35, w) for s, w in router.items()})
    for factor, exposure in factor_exposures(target, 1.0).items():
        if exposure > FACTOR_CAPS[factor]:
            scale = FACTOR_CAPS[factor] / exposure
            target = {s: w * scale if loading_of(s, factor) > 0 else w for s, w in target.items()}
    target["BIL"] = max(0, 0.98 - sum(target.values()))
    return target


def simulate(opens, closes, etf_signals, health, spec, policy, *, cost_bps=5.0, start_after=None):
    """Daily open execution, cash, drift, loss halt and permanent drawdown kill.

    No intraday stop fills are invented. Rebalancing is a daily-bar proxy and
    remains promotion-ineligible until reconciled against a production replay.
    """
    if spec.pead_weight and "PEAD_MODEL" not in closes:
        return None
    indices = opens.index.intersection(closes.index)
    opens, closes = opens.loc[indices], closes.loc[indices]
    if not opens.columns.equals(closes.columns):
        raise ValueError("price columns must match")
    qqq = closes["QQQ"]
    sma = qqq.rolling(policy["ma_window"]).mean()
    vol = qqq.pct_change().rolling(20).std() * np.sqrt(252)
    bull = regime_states(qqq, sma, spec)
    shares = np.zeros(len(closes.columns))
    symbols = list(closes.columns)
    cash, previous, peak, killed = 100000.0, 100000.0, 100000.0, False
    records = []
    for i in range(policy["ma_window"], len(indices)):
        day, signal_day = indices[i], indices[i-1]
        if start_after is not None and str(day.date()) <= start_after:
            continue
        opening = opens.iloc[i].to_numpy(dtype=float)
        closing = closes.iloc[i].to_numpy(dtype=float)
        if not np.isfinite(opening).all() or not np.isfinite(closing).all() or min(opening) <= 0 or min(closing) <= 0:
            raise ValueError("missing portfolio price: no silent asset exclusion")
        value = shares * opening
        equity = cash + value.sum()
        peak = max(peak, equity)
        killed |= equity / peak - 1 <= -0.20
        halted = equity / previous - 1 <= -0.03
        etf = etf_signals.loc[signal_day].to_dict() if signal_day in etf_signals.index else {}
        verdict = str(health.loc[signal_day]) if signal_day in health.index else "UNKNOWN"
        targets = target_weights(regime_on=bool(bull[i-1]), qqq_vol=float(vol.iloc[i-1]),
            verdict=verdict, etf_targets=etf, spec=spec, policy=policy)
        target = np.array([targets.get(s, 0) * equity for s in symbols])
        if killed:
            target[:] = 0
        elif halted:
            target = np.minimum(target, value)
        # Production drift tolerances: regime relative target; router equity.
        for j, symbol in enumerate(symbols):
            threshold = 0.05 * target[j] if symbol == "QLD" else 0.05 * equity if symbol in {"QQQ", "DBMF", "KMLM"} else 0
            if target[j] > 0 and abs(target[j] - value[j]) < threshold:
                target[j] = value[j]
        cost, turnover = 0.0, 0.0
        deltas = target - value
        for j in np.argsort(deltas):  # all sales fill before purchases
            delta = deltas[j]
            if abs(delta) < 1:
                continue
            if delta > 0:
                delta = min(delta, max(0, cash - equity * 0.02) / (1 + cost_bps / 10000))
            qty = delta / opening[j]
            charge = abs(delta) * cost_bps / 10000
            shares[j] += qty
            cash -= delta + charge
            cost += charge
            turnover += abs(delta)
        close_equity = cash + shares @ closing
        peak = max(peak, close_equity)
        records.append({"date": str(day.date()), "equity": float(close_equity),
                        "return": float(close_equity / previous - 1), "cost": float(cost),
                        "turnover": float(turnover), "cash": float(cash),
                        "halted": bool(halted), "killed": bool(killed),
                        "signal_date": str(signal_day.date())})
        previous = close_equity
    return records


def performance(rows, benchmark=None):
    if not rows:
        return {"observations": 0}
    r = np.asarray([x["return"] for x in rows])
    eq = np.r_[1.0, np.cumprod(1 + r)]
    sd = float(np.std(r, ddof=1)) if len(r) > 1 else 0
    out = {"observations": len(r), "return": float(eq[-1]-1),
           "cagr": float(eq[-1] ** (252/len(r))-1),
           "max_drawdown": float(np.min(eq / np.maximum.accumulate(eq) - 1)),
           "sharpe": float(np.mean(r)/sd*np.sqrt(252)) if sd else 0,
           "cost": float(sum(x["cost"] for x in rows)),
           "turnover": float(sum(x["turnover"] for x in rows))}
    if benchmark is not None:
        b = np.asarray(benchmark)[-len(r):]
        up = b > 0
        out["upside_capture"] = float(r[up].mean()/b[up].mean()) if up.any() else None
    return out


def forward_gate(candidate, baseline, *, parity, minimum_sessions=60):
    joined = {r["date"]: r for r in baseline}
    pairs = [(r, joined[r["date"]]) for r in candidate if r["date"] in joined]
    delta = np.array([a["return"] - b["return"] for a, b in pairs])
    # Non-overlapping weekly clusters reduce false confidence from daily
    # autocorrelation. A 3-sigma threshold addresses the frozen candidate family.
    blocks = np.array([delta[i:i+5].sum() for i in range(0, len(delta)-4, 5)])
    lower = float(blocks.mean() - 3 * blocks.std(ddof=1) / np.sqrt(len(blocks))) if len(blocks) > 1 else None
    reasons = []
    if not parity.get("passed"):
        reasons += parity.get("blockers", ["production parity unproven"])
    if len(pairs) < minimum_sessions or len(blocks) < 12:
        reasons.append("at least 60 forward sessions and 12 independent weekly blocks required")
    complete=sum(a.get("data_complete",False) and b.get("data_complete",False) for a,b in pairs)
    if any("data_complete" in a for a,b in pairs) and complete < minimum_sessions:
        reasons.append("at least 60 sessions with complete observed market coverage required; incomplete days remain in P&L")
    if lower is None or lower <= 0:
        reasons.append("cost-adjusted weekly excess lower bound is not positive")
    if performance(candidate).get("max_drawdown", 0) < performance(baseline).get("max_drawdown", 0) - .02:
        reasons.append("drawdown exceeds baseline tolerance")
    retired = len(delta) >= 20 and float(delta[-20:].sum()) < -.03
    if retired:
        reasons.append("retired: trailing 20-session excess below -3 percentage points")
    return {"eligible": not reasons, "status": "RETIRED" if retired else "ELIGIBLE_FOR_REPLAY_REVIEW" if not reasons else "COLLECTING",
            "sessions": len(pairs), "complete_sessions":complete,"weekly_blocks": len(blocks), "weekly_excess_lower_bound": lower,
            "reasons": reasons, "order_authority": False}


def stress_scenarios(weights):
    # Hypothetical simultaneous moves, not attributed historical portfolio returns.
    shocks = {
        "equity_gap": {"QLD": -.20, "QQQ": -.10, "DBMF": -.03, "KMLM": -.03, "BIL": 0},
        "correlation_break": {"QLD": -.16, "QQQ": -.08, "DBMF": -.10, "KMLM": -.10, "BIL": -.002},
        "fast_rebound_while_deleveraged": {"QLD": .20, "QQQ": .10, "DBMF": -.03, "KMLM": -.03, "BIL": 0},
    }
    return [{"name": name, "hypothetical_return": sum(w * scenario.get(s, -.08) for s,w in weights.items()),
             "assumption": "simultaneous asset shocks; no stop-fill guarantee"} for name,scenario in shocks.items()]
