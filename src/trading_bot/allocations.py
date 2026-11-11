"""Shared portfolio allocation state for the adaptive sleeve policy.

A single file (data/sleeve_allocations.json) is the source of truth for what
% of total equity each sleeve should own. Each executor reads it at runtime
(falls back to defaults if missing). The weekly update script (update_allocations.py)
writes it based on the adaptive HARD rule:
  if a sleeve's 60-day rolling Sharpe has been negative for 4 consecutive
  weekly readings, set its weight to 0 and redirect to QQQ.

Current production risk budget (chosen 2026-08-09):
  regime: 70% (QLD-regime: QQQ>200dma → QLD, else cash via cash_park)
  pead:    0% (retired)
  crypto:  0% (disabled pending validation)
  qqq:     0% (the managed-futures router sizes its own residual allocation)
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ALLOCATIONS_FILE = Path(__file__).resolve().parents[2] / "data" / "sleeve_allocations.json"
REVALIDATION_FILE = Path(__file__).resolve().parents[2] / "data" / "revalidation.jsonl"

THIRD = 1.0 / 3
# One explicit source for the production allocation. Historical research still
# imports THIRD for equal-weight comparisons; live planning must not infer its
# target from that obsolete three-sleeve baseline.
LIVE_SLEEVE_TARGETS = {"qqq": 0.0, "regime": 0.70, "pead": 0.0, "crypto": 0.0}
DEFAULT_WEIGHTS = dict(LIVE_SLEEVE_TARGETS)

# Runtime enablement is part of allocation governance, not merely an
# orchestration detail. Keeping it here gives the executor and both dashboards
# the same answer to "may this sleeve own capital?".
DEFAULT_SLEEVE_ENABLEMENT = {
    "etf": True,
    "regime": True,
    "managed_futures": True,
    "cash_park": True,
    "crypto": False,
    "pead": False,
}

_VERDICT_RANK = {
    "HEALTHY": 0,
    "WATCH": 1,
    "DECAYING": 2,
    "TRIPPED": 3,
    "FAST_TRIPPED": 4,
    "ERROR": 5,
    "DISABLED": 6,
}
_ZERO_WEIGHT = {"TRIPPED", "FAST_TRIPPED", "ERROR", "DISABLED"}


def effective_target(configured: float, cap: float | None, verdict: str) -> float:
    """Production target after the binary health gate and hard ceiling.

    DECAYING is deliberately warning-only. The documented slow policy requires
    six consecutive weekly readings before it changes capital; applying an
    extra half-allocation here would create an untested fourth rule.
    """
    ceiling = float(configured) if cap is None else min(float(configured), float(cap))
    return 0.0 if verdict.upper() in _ZERO_WEIGHT else max(0.0, ceiling)


def _latest_revalidation(path: Path = REVALIDATION_FILE) -> dict:
    """Return the most recent validation record.

    A present-but-corrupt health ledger is a hard error. Ignoring it could
    resurrect a sleeve whose tripwire was the last valid decision.
    """
    if not path.exists():
        return {}
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


def _revalidation_verdict(section: dict | None) -> str | None:
    if not section:
        return None
    if section.get("error"):
        return "ERROR"
    tripwire = str(section.get("tripwire", "")).upper()
    if tripwire.startswith("TRIPPED"):
        return "TRIPPED"
    verdict = str(section.get("verdict", "")).upper()
    return verdict if verdict in _VERDICT_RANK else None


def resolve_allocations(
    data: dict,
    *,
    revalidation: dict | None = None,
    enabled_sleeves: dict[str, bool] | None = None,
) -> dict:
    """Reconcile policy, validation, and deployment into one enforced view.

    The most conservative verdict wins. A blocked sleeve is assigned zero
    weight in the returned structure, regardless of a stale policy weight.
    Raw policy weights/verdicts remain available for auditability.
    """
    out = deepcopy(data)
    raw_weights = {k: float(v) for k, v in out.get("weights", {}).items()}
    raw_verdicts = {
        k: str(v).upper() for k, v in out.get("verdicts", {}).items()
    }
    validation = revalidation or {}
    validation_verdicts = {
        sleeve: verdict
        for sleeve in ("crypto", "pead")
        if (verdict := _revalidation_verdict(validation.get(sleeve))) is not None
    }

    sleeves = set(raw_weights) | set(raw_verdicts) | set(validation_verdicts)
    if enabled_sleeves is not None:
        sleeves |= set(enabled_sleeves)

    effective: dict[str, str] = {}
    conflicts: list[dict] = []
    overrides: list[dict] = []
    enforced_weights = dict(raw_weights)
    for sleeve in sorted(sleeves):
        sources: dict[str, str] = {}
        policy = raw_verdicts.get(sleeve)
        if policy in _VERDICT_RANK:
            sources["allocation_policy"] = policy
        validated = validation_verdicts.get(sleeve)
        if validated:
            sources["weekly_revalidation"] = validated
        # QQQ is not an independent executor; it is the managed-futures idle
        # router's risk-on destination, so its enablement follows that sleeve.
        is_enabled = (
            enabled_sleeves.get("managed_futures", False)
            if enabled_sleeves is not None and sleeve == "qqq"
            else enabled_sleeves.get(sleeve, False) if enabled_sleeves is not None else True
        )
        if enabled_sleeves is not None and not is_enabled:
            sources["deployment"] = "DISABLED"

        verdict = max(sources.values(), key=_VERDICT_RANK.get) if sources else "HEALTHY"
        effective[sleeve] = verdict
        if verdict in _ZERO_WEIGHT:
            enforced_weights[sleeve] = 0.0
        if len(set(sources.values())) > 1:
            detail = {"sleeve": sleeve, "sources": sources, "enforced": verdict}
            overrides.append(detail)
            # A real governance conflict is specifically an allocation policy
            # trying to fund a sleeve that validation has blocked. A healthy
            # monitoring verdict beside an intentionally DISABLED deployment is
            # not an error; those fields answer different questions.
            policy_funds_sleeve = raw_weights.get(sleeve, 0.0) > 0
            validation_blocks = validated in _ZERO_WEIGHT
            if policy_funds_sleeve and validation_blocks:
                conflicts.append(detail)

    out["policy_weights"] = raw_weights
    out["policy_verdicts"] = raw_verdicts
    out["revalidation_verdicts"] = validation_verdicts
    out["weights"] = enforced_weights
    out["verdicts"] = effective
    out["governance"] = {
        "status": "DEGRADED" if conflicts else "OK",
        "conflicts": conflicts,
        "overrides": overrides,
        "rule": "most_conservative_source_wins",
    }
    return out


def read_allocations(
    *,
    enabled_sleeves: dict[str, bool] | None = None,
    revalidation_file: Path = REVALIDATION_FILE,
    require_fresh: bool = False,
    now: datetime | None = None,
) -> dict:
    """Read current weights.

    A MISSING file (fresh checkout) falls back to defaults. A CORRUPT file
    RAISES: silently returning defaults would resurrect a deliberately-tripped
    sleeve at its production target (e.g. redeploy a deliberately disabled
    sleeve after the Sunday write crashed) —
    failure default must never be MORE risk. The cron wrapper Telegrams the
    raised error, which is the correct outcome."""
    if not ALLOCATIONS_FILE.exists():
        data = {"weights": dict(DEFAULT_WEIGHTS), "policy": "default", "ts": None}
    else:
        data = json.loads(ALLOCATIONS_FILE.read_text())  # raise on corruption
    if "weights" not in data:
        raise ValueError(f"allocations file {ALLOCATIONS_FILE} has no 'weights' key — refusing to guess")
    if require_fresh:
        stamp = data.get("ts")
        if not stamp:
            raise ValueError("dated allocation evidence required for live purchases")
        observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("allocation timestamp must be timezone-aware")
        age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
        if age < -300 or age > 10 * 86400:
            raise ValueError("allocation evidence is stale or dated in the future")
    return resolve_allocations(
        data,
        revalidation=_latest_revalidation(revalidation_file),
        enabled_sleeves=enabled_sleeves,
    )


def get_weight(sleeve: str) -> float:
    """Get a single sleeve's current weight. A sleeve absent from a VALID
    weights dict is 0.0 (retired/unknown), NOT 1/3 — defaulting a missing key
    to a third deployed capital into sleeves that were deliberately zeroed."""
    return float(read_allocations().get("weights", {}).get(sleeve, 0.0))


def write_allocations(weights: dict, policy: str = "adaptive_hard",
                       streaks: dict | None = None, verdicts: dict | None = None,
                       lookback_days: int = 60, streak_threshold_weeks: int = 4) -> None:
    ALLOCATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": policy,
        "lookback_days": lookback_days,
        "streak_threshold_weeks": streak_threshold_weeks,
        "weights": {k: round(float(v), 4) for k, v in weights.items()},
        "streaks": streaks or {},
        "verdicts": verdicts or {},
    }, indent=2)
    # Atomic write (tmp + rename): a crash mid-write must never leave a
    # truncated file that the executors would then refuse to read.
    tmp = ALLOCATIONS_FILE.with_suffix(".json.tmp")
    tmp.write_text(payload)
    tmp.replace(ALLOCATIONS_FILE)
