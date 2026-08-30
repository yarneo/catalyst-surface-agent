#!/usr/bin/env python3
"""Build one autonomous, auditable weekly event-volatility plan.

This command performs discovery and research only.  It has no order flag and
constructs the Alpaca MCP client with ``live=False``.  The one-minute executor
loads the sealed plan, then repeats every live surface gate before an order can
become eligible.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import dotenv_values  # noqa: E402

from trading_bot.options.clock import ET, now_et  # noqa: E402
from trading_bot.options.mcp import MCPClient  # noqa: E402
from trading_bot.tournament.audit import AuditLedger  # noqa: E402
from trading_bot.tournament.event_calendar import (  # noqa: E402
    alpaca_news_facts,
    calendar_catalyst_fact,
    discover_earnings_calendar,
)
from trading_bot.tournament.event_replay import (  # noqa: E402
    historical_earnings_events,
    replay_long_straddles,
)
from trading_bot.tournament.event_semantics import EventSemanticClassifier  # noqa: E402
from trading_bot.tournament.event_universe import UNIVERSE  # noqa: E402
from trading_bot.tournament.scheduled import (  # noqa: E402
    ScheduledEventPolicy,
    surface_from_mcp,
)
from trading_bot.tournament.weekly import (  # noqa: E402
    EventSchedule,
    PortfolioPolicy,
    WeeklyWindow,
    allocate_event_risk,
    calendar_consensus,
    evaluate_promotion,
    schedule_event,
)
from trading_bot.tournament.weekly_plan import (  # noqa: E402
    atomic_write_plan,
    seal_plan,
)


def log(message: str) -> None:
    print(f"{now_et():%Y-%m-%d %H:%M:%S %Z}  {message}", flush=True)


def _parse_stamp(value: str) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("window timestamps must be timezone-aware")
    return stamp.astimezone(ET)


def default_window(now: dt.datetime) -> WeeklyWindow:
    now = now.astimezone(ET)
    monday = now.date() - dt.timedelta(days=now.weekday())
    current_deadline = dt.datetime.combine(
        monday + dt.timedelta(days=4), dt.time(15, 30), ET)
    if now.weekday() >= 5 or now >= current_deadline:
        monday += dt.timedelta(days=7)
    return WeeklyWindow(
        dt.datetime.combine(monday, dt.time(9, 30), ET),
        dt.datetime.combine(monday + dt.timedelta(days=4), dt.time(15, 30), ET))


def _symbols(value: str | None) -> tuple[str, ...]:
    if not value:
        return tuple(UNIVERSE)
    rows = tuple(dict.fromkeys(
        part.strip().upper() for part in value.split(",") if part.strip()))
    unknown = sorted(set(rows) - set(UNIVERSE))
    if unknown:
        raise ValueError(f"symbols outside the declared universe: {unknown}")
    return rows


def _account_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode()).hexdigest()


def _listed_expiries(mcp: MCPClient, symbol: str, event_date: dt.date) -> tuple[str, ...]:
    payload = mcp.option_contracts(
        underlying_symbols=symbol, status="active",
        expiration_date_gte=event_date.isoformat(),
        expiration_date_lte=(event_date + dt.timedelta(days=14)).isoformat(),
        limit=10000)
    rows = payload.get("option_contracts", []) if isinstance(payload, dict) else []
    return tuple(sorted({str(row.get("expiration_date")) for row in rows
                         if isinstance(row, dict) and row.get("expiration_date")}))


def _spot(snapshot: Any, symbol: str) -> float:
    try:
        value = float(snapshot[symbol]["latestTrade"]["p"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{symbol} stock snapshot has no valid trade") from exc
    if value <= 0:
        raise ValueError(f"{symbol} stock trade is non-positive")
    return value


def _surface(mcp: MCPClient, schedule: EventSchedule, observed_at: dt.datetime) \
        -> tuple[float, float, float]:
    symbol = schedule.symbol
    spot = _spot(mcp.stock_snapshot(symbol, feed="iex"), symbol)
    payload = mcp.option_chain(
        symbol, feed="indicative", limit=1000,
        expiration_date=schedule.expiry,
        strike_price_gte=spot * 0.96, strike_price_lte=spot * 1.04)
    policy = ScheduledEventPolicy(
        event_id=schedule.event_id, underlying=symbol, expiry=schedule.expiry,
        entry_start=schedule.entry_start, entry_end=schedule.entry_end,
        event_at=schedule.event_at, exit_at=schedule.exit_at,
        emergency_flat_by=schedule.emergency_flat_by)
    surface = surface_from_mcp(
        payload=payload, spot=spot, observed_at=observed_at, policy=policy)
    premium = surface.executable_debit(policy.order_buffer) / spot
    return premium, surface.total_spread_pct, surface.quote_age_s


def _committee(
    api_key: str, facts_by_symbol: dict[str, list[Any]],
) -> tuple[dict[str, Any], list[str]]:
    accepted: dict[str, Any] = {}
    reasons: list[str] = []
    symbols = sorted(facts_by_symbol)
    classifier = EventSemanticClassifier(api_key)
    for offset in range(0, len(symbols), 3):
        batch = symbols[offset:offset + 3]
        facts = [fact for symbol in batch for fact in facts_by_symbol[symbol]]
        try:
            result = classifier.analyze(
                facts, candidates=[{"ticker": symbol, "order_enabled": False}
                                   for symbol in batch])
        except Exception as exc:  # noqa: BLE001 — candidate fails closed
            reasons.append(f"{','.join(batch)}: {type(exc).__name__}: {exc}")
            continue
        accepted.update(result.by_ticker())
        reasons.extend(result.reasons)
        reasons.extend(
            f"{attempt.model}: {attempt.error}" for attempt in result.attempts
            if attempt.error)
    return accepted, reasons


def build_plan(
    *, mcp: MCPClient, featherless_key: str, window: WeeklyWindow,
    observed_at: dt.datetime, symbols: tuple[str, ...],
    historical_events: int, minimum_market_cap: float,
) -> dict[str, Any]:
    sessions_payload = mcp.calendar(
        (window.start.date() - dt.timedelta(days=10)).isoformat(),
        (window.deadline.date() + dt.timedelta(days=10)).isoformat())
    sessions = tuple(str(row["date"]) for row in sessions_payload
                     if isinstance(row, dict) and row.get("date"))
    discovery = discover_earnings_calendar(
        window.start.date(), window.deadline.date(),
        minimum_market_cap=minimum_market_cap, universe=symbols)
    consensuses = calendar_consensus(discovery.facts)
    facts_by_symbol: dict[str, list[Any]] = {}
    news_counts: dict[str, int] = {}
    for consensus in consensuses:
        if not consensus.confirmed:
            continue
        calendar_rows = [row for row in discovery.facts
                         if row.symbol == consensus.symbol]
        news_payload = mcp.news(
            symbols=consensus.symbol,
            start=(observed_at.date() - dt.timedelta(days=30)).isoformat(),
            end=window.deadline.isoformat(), sort="desc", limit=50,
            include_content=False)
        news = list(alpaca_news_facts(news_payload, symbol=consensus.symbol))
        news_counts[consensus.symbol] = len(news)
        facts_by_symbol[consensus.symbol] = [
            *(calendar_catalyst_fact(row, observed_at=observed_at)
              for row in calendar_rows), *news]

    semantics, semantic_reasons = _committee(featherless_key, facts_by_symbol) \
        if facts_by_symbol else ({}, [])
    candidate_rows: list[dict[str, Any]] = []
    decisions = []
    for consensus in consensuses:
        row: dict[str, Any] = {
            "symbol": consensus.symbol,
            "calendar": asdict(consensus),
            "calendar_facts": [asdict(value) for value in discovery.facts
                               if value.symbol == consensus.symbol],
            "news_fact_count": news_counts.get(consensus.symbol, 0),
        }
        schedule = None
        replay = None
        current_premium = None
        current_spread = None
        try:
            if consensus.event_date is None:
                raise ValueError("calendar did not produce a unique event date")
            expiries = _listed_expiries(mcp, consensus.symbol, consensus.event_date)
            schedule = schedule_event(
                consensus, sessions=sessions, expiries=expiries, window=window)
            row["schedule"] = asdict(schedule)
        except Exception as exc:  # noqa: BLE001 — rejection is recorded
            row["schedule_error"] = f"{type(exc).__name__}: {exc}"

        semantic = semantics.get(consensus.symbol)
        semantic_confirmed = bool(
            semantic and semantic.event_type == "earnings"
            and semantic.status == "upcoming")
        row["semantic"] = asdict(semantic) if semantic else None

        if schedule is not None:
            try:
                events = historical_earnings_events(
                    consensus.symbol, before=consensus.event_date,
                    limit=historical_events)
                replay = replay_long_straddles(mcp, consensus.symbol, events)
                row["replay"] = asdict(replay)
            except Exception as exc:  # noqa: BLE001
                row["replay_error"] = f"{type(exc).__name__}: {exc}"
            try:
                current_premium, current_spread, quote_age = _surface(
                    mcp, schedule, observed_at)
                row["planning_surface"] = {
                    "observed_at": observed_at.isoformat(),
                    "premium_to_spot": current_premium,
                    "total_spread_pct": current_spread,
                    "quote_age_s": quote_age,
                    "live_recheck_required": True,
                }
            except Exception as exc:  # noqa: BLE001
                row["surface_error"] = f"{type(exc).__name__}: {exc}"

        decision = evaluate_promotion(
            consensus=consensus, semantic_confirmed=semantic_confirmed,
            replay=replay.summary if replay else None, schedule=schedule,
            current_premium_to_spot=current_premium,
            current_total_spread_pct=current_spread,
            require_current_surface=False)
        row["promotion"] = asdict(decision)
        row["promotion"]["live_surface_gate_deferred"] = True
        candidate_rows.append(row)
        decisions.append(decision)
        log(f"{consensus.symbol}: " + (
            "PREQUALIFIED; live surface recheck remains"
            if decision.promoted else f"SHADOW — {'; '.join(decision.reasons)}"))

    account = mcp.account()
    equity = float(account["equity"])
    allocations = allocate_event_risk(decisions, equity=equity,
                                      policy=PortfolioPolicy())
    for row in candidate_rows:
        row["max_loss_budget_usd"] = allocations.get(row["symbol"], 0.0)
    return {
        "mode": "AUTONOMOUS_WEEKLY_RESEARCH_PLAN",
        "order_enabled": False,
        "generated_at": observed_at.isoformat(),
        "window": {"start": window.start.isoformat(),
                   "deadline": window.deadline.isoformat()},
        "account_sha256": _account_hash(account.get("account_number")),
        "equity_at_plan": equity,
        "universe_size": len(symbols),
        "calendar": {
            "fact_count": len(discovery.facts),
            "source_errors": list(discovery.errors),
            "consensus": [asdict(value) for value in consensuses],
        },
        "semantic_committee_reasons": semantic_reasons,
        "events": candidate_rows,
        "portfolio": {
            "aggregate_risk_pct": .25, "per_event_risk_pct": .125,
            "singleton_risk_pct": .25, "allocations_usd": allocations,
        },
        "source": {
            "discovery": ["Yahoo Finance calendar", "Nasdaq earnings calendar"],
            "broker_transport": "Alpaca MCP",
            "mcp_tools_used": [
                "get_account_info", "get_calendar", "get_news",
                "get_stock_bars", "get_stock_snapshot", "get_option_contracts",
                "get_option_bars", "get_option_chain"],
            "semantic_models": list(EventSemanticClassifier(
                featherless_key).models),
        },
        "invariants": [
            "The plan itself has no order authority.",
            "Every order candidate needs independent date/session calendar quorum.",
            "Featherless classifies supplied facts but cannot select, size, or order.",
            "Every entry repeats the live option quote, width, freshness, and premium gates.",
            "Every position exits on its own next-session clock or the global deadline.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--featherless-env", default=".env.local")
    parser.add_argument("--start", help="timezone-aware ISO-8601 timestamp")
    parser.add_argument("--deadline", help="timezone-aware ISO-8601 timestamp")
    parser.add_argument("--symbols", help="comma-separated subset of the declared universe")
    parser.add_argument("--historical-events", type=int, default=8)
    parser.add_argument("--minimum-market-cap", type=float, default=2_000_000_000)
    parser.add_argument("--output", default="data/weekly_event_plan.json")
    parser.add_argument("--ledger", default="data/weekly_event_evidence.jsonl")
    args = parser.parse_args()
    if not 3 <= args.historical_events <= 20:
        parser.error("--historical-events must be in [3, 20]")
    observed_at = now_et()
    try:
        window = (WeeklyWindow(_parse_stamp(args.start), _parse_stamp(args.deadline))
                  if args.start and args.deadline else default_window(observed_at))
        if bool(args.start) != bool(args.deadline):
            raise ValueError("--start and --deadline must be supplied together")
        symbols = _symbols(args.symbols)
    except ValueError as exc:
        log(f"plan configuration failed: {exc}")
        return 5

    config = dict(dotenv_values(ROOT / args.featherless_env))
    config.update(dotenv_values(ROOT / args.env))
    key, secret = config.get("ALPACA_API_KEY"), config.get("ALPACA_SECRET_KEY")
    featherless_key = config.get("FEATHERLESS_API_KEY")
    expected_account = str(config.get("ALPACA_ACCOUNT_NUMBER") or "")
    if not key or not secret or not featherless_key or not expected_account:
        log("plan configuration is incomplete")
        return 5
    try:
        with MCPClient(str(key), str(secret), live=False, paper=True, timeout=90) as mcp:
            account = mcp.account()
            if str(account.get("account_number") or "") != expected_account:
                raise RuntimeError("paper-account pin mismatch")
            plan = build_plan(
                mcp=mcp, featherless_key=str(featherless_key), window=window,
                observed_at=observed_at, symbols=symbols,
                historical_events=args.historical_events,
                minimum_market_cap=args.minimum_market_cap)
        sealed = seal_plan(plan)
        atomic_write_plan(ROOT / args.output, sealed)
        audit = AuditLedger(ROOT / args.ledger).append(
            "weekly_plan_built", {
                "plan_sha256": sealed["plan_sha256"],
                "window": sealed["window"],
                "event_count": len(sealed["events"]),
                "promoted": [row["symbol"] for row in sealed["events"]
                             if row["promotion"]["promoted"]],
                "calendar_source_errors": sealed["calendar"]["source_errors"],
            }, recorded_at=observed_at)
    except Exception as exc:  # noqa: BLE001 — one safe, audited failure boundary
        log(f"weekly plan failed: {type(exc).__name__}: {exc}")
        return 2
    log(f"PLAN READY — {len(sealed['events'])} event(s), "
        f"{sum(row['promotion']['promoted'] for row in sealed['events'])} prequalified, "
        f"orders enabled=False, audit=#{audit.sequence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
