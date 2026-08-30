#!/usr/bin/env python3
"""One autonomous cycle of the reusable multi-event weekly executor.

The sealed weekly plan discovers candidates and prequalifies their evidence.
This runner still repeats calendar semantics and every live surface gate before
entry.  Exits are evaluated before entry guards and remain active through data,
model, registry, and plan failures.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import math
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import dotenv_values  # noqa: E402

from trading_bot.options.book import Book, BookEntry  # noqa: E402
from trading_bot.options.clock import ET, now_et  # noqa: E402
from trading_bot.options.execution import close_spread, open_spread  # noqa: E402
from trading_bot.options.iv import Quote  # noqa: E402
from trading_bot.options.mcp import MCPClient  # noqa: E402
from trading_bot.options.occ import BadOCC, parse  # noqa: E402
from trading_bot.tournament.audit import AuditLedger  # noqa: E402
from trading_bot.tournament.event_calendar import (  # noqa: E402
    alpaca_news_facts,
    calendar_catalyst_fact,
)
from trading_bot.tournament.event_replay import ReplaySummary  # noqa: E402
from trading_bot.tournament.event_semantics import EventSemanticClassifier  # noqa: E402
from trading_bot.tournament.scheduled import (  # noqa: E402
    ScheduledEventPolicy,
    evaluate_entry,
    surface_from_mcp,
)
from trading_bot.tournament.weekly import (  # noqa: E402
    CalendarFact,
    EventConsensus,
    EventSchedule,
    EventTiming,
    WeeklyWindow,
    evaluate_promotion,
    event_lifecycle,
)
from trading_bot.tournament.weekly_plan import read_plan  # noqa: E402


def log(message: str) -> None:
    print(f"{now_et():%Y-%m-%d %H:%M:%S %Z}  {message}", flush=True)


def _stamp(value: str) -> dt.datetime:
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("plan timestamp must be timezone-aware")
    return result.astimezone(ET)


def _window(plan: dict[str, Any]) -> WeeklyWindow:
    return WeeklyWindow(_stamp(plan["window"]["start"]),
                        _stamp(plan["window"]["deadline"]))


def _schedule(raw: dict[str, Any]) -> EventSchedule:
    return EventSchedule(
        event_id=str(raw["event_id"]), symbol=str(raw["symbol"]),
        event_date=dt.date.fromisoformat(str(raw["event_date"])),
        timing=EventTiming(str(raw["timing"])), expiry=str(raw["expiry"]),
        entry_start=_stamp(str(raw["entry_start"])),
        entry_end=_stamp(str(raw["entry_end"])),
        event_at=_stamp(str(raw["event_at"])),
        exit_at=_stamp(str(raw["exit_at"])),
        emergency_flat_by=_stamp(str(raw["emergency_flat_by"])))


def _consensus(raw: dict[str, Any]) -> EventConsensus:
    value = raw["calendar"]
    return EventConsensus(
        symbol=str(value["symbol"]),
        event_date=(dt.date.fromisoformat(str(value["event_date"]))
                    if value.get("event_date") else None),
        timing=EventTiming(str(value["timing"])),
        event_type=str(value["event_type"]),
        sources=tuple(value["sources"]), fact_ids=tuple(value["fact_ids"]),
        confirmed=bool(value["confirmed"]), reasons=tuple(value["reasons"]))


def _replay(raw: dict[str, Any]) -> ReplaySummary | None:
    value = (raw.get("replay") or {}).get("summary")
    return ReplaySummary(**value) if isinstance(value, dict) else None


def _policy(schedule: EventSchedule) -> ScheduledEventPolicy:
    return ScheduledEventPolicy(
        event_id=schedule.event_id, underlying=schedule.symbol,
        expiry=schedule.expiry, entry_start=schedule.entry_start,
        entry_end=schedule.entry_end, event_at=schedule.event_at,
        exit_at=schedule.exit_at, emergency_flat_by=schedule.emergency_flat_by)


def _positions(raw: Any) -> tuple[dict[str, int], list[dict[str, Any]]]:
    options: dict[str, int] = {}
    shares = []
    for row in raw if isinstance(raw, list) else []:
        symbol = str(row.get("symbol") or "")
        try:
            parse(symbol)
        except BadOCC:
            shares.append(row)
            continue
        options[symbol] = int(float(row.get("qty") or 0))
    return options, shares


def _latest_spot(snapshot: Any, symbol: str, *, now: dt.datetime,
                 max_age_s: float = 90.0) -> float:
    try:
        trade = snapshot[symbol]["latestTrade"]
        price = float(trade["p"])
        stamp = _stamp(str(trade["t"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{symbol} snapshot lacks a valid latest trade") from exc
    age = (now.astimezone(ET) - stamp).total_seconds()
    if not 0 <= age <= max_age_s:
        raise ValueError(f"{symbol} trade is stale or future-dated ({age:.1f}s)")
    if price <= 0:
        raise ValueError(f"{symbol} trade is non-positive")
    return price


def _quote_map(payload: Any, symbols: list[str]) -> dict[str, Quote]:
    rows = payload.get("quotes", {}) if isinstance(payload, dict) else {}
    output = {}
    for symbol in symbols:
        row = rows.get(symbol)
        if not isinstance(row, dict):
            continue
        try:
            output[symbol] = Quote(float(row["bp"]), float(row["ap"]))
        except (KeyError, TypeError, ValueError):
            continue
    return output


def _entry_attempted(ledger: AuditLedger, event_id: str, *, live: bool) -> bool:
    types = {"weekly_entry_intent"} if live else {"weekly_shadow_entry"}
    return any(row.event_type in types and row.payload.get("event_id") == event_id
               for row in ledger.read())


def _entry_for(book: Book, event_id: str) -> BookEntry | None:
    return next((row for row in book.open_entries
                 if row.id == event_id or row.id.startswith(f"{event_id}-")), None)


def _coid(event_id: str, action: str) -> str:
    digest = hashlib.sha256(event_id.encode()).hexdigest()[:12]
    symbol = event_id.split("-", 1)[0][:8]
    return f"we-{symbol}-{digest}-{action}"[:48]


def _exit_coid(ledger: AuditLedger, event_id: str) -> tuple[str, bool]:
    """Reuse only an unresolved exit ID; advance after a terminal attempt."""
    rows = [row for row in ledger.read()
            if row.payload.get("event_id") == event_id
            and row.event_type in {"weekly_exit_intent", "weekly_exit_result"}]
    intents = [row for row in rows if row.event_type == "weekly_exit_intent"]
    if intents:
        latest = intents[-1]
        coid = str(latest.payload["client_order_id"])
        results = [row for row in rows
                   if row.event_type == "weekly_exit_result"
                   and row.payload.get("client_order_id") == coid]
        if not results or (results[-1].payload.get("fill") or {}).get("how") == "stuck":
            return coid, False
    return _coid(event_id, f"exit-{len(intents) + 1:03d}"), True


def _calendar_fact(value: dict[str, Any]) -> CalendarFact:
    return CalendarFact(
        symbol=str(value["symbol"]),
        event_date=dt.date.fromisoformat(str(value["event_date"])),
        timing=EventTiming(str(value["timing"])), source=str(value["source"]),
        fact_id=str(value["fact_id"]), summary=str(value["summary"]),
        event_type=str(value.get("event_type") or "earnings"))


def _semantic_recheck(
    mcp: MCPClient, featherless_key: str, event: dict[str, Any],
    now: dt.datetime,
) -> tuple[bool, dict[str, Any]]:
    symbol = str(event["symbol"])
    calendar_facts = [_calendar_fact(row) for row in event.get("calendar_facts", [])]
    facts = [calendar_catalyst_fact(row, observed_at=now) for row in calendar_facts]
    news = alpaca_news_facts(mcp.news(
        symbols=symbol, start=(now.date() - dt.timedelta(days=30)).isoformat(),
        end=now.isoformat(), sort="desc", limit=50, include_content=False),
        symbol=symbol)
    facts.extend(news)
    result = EventSemanticClassifier(featherless_key).analyze(
        facts, candidates=[{"ticker": symbol, "order_enabled": False}])
    semantic = result.by_ticker().get(symbol)
    confirmed = bool(semantic and semantic.event_type == "earnings"
                     and semantic.status == "upcoming")
    return confirmed, {
        "confirmed": confirmed,
        "semantic": asdict(semantic) if semantic else None,
        "reasons": list(result.reasons),
        "news_fact_count": len(news),
        "attempt_errors": [
            {"model": attempt.model, "error": attempt.error}
            for attempt in result.attempts if attempt.error],
    }


def _recover_entries(
    mcp: MCPClient, book: Book, ledger: AuditLedger,
    events: list[dict[str, Any]], now: dt.datetime,
) -> None:
    by_id = {row["schedule"]["event_id"]: row for row in events
             if isinstance(row.get("schedule"), dict)}
    for audit in ledger.read():
        if audit.event_type != "weekly_entry_intent":
            continue
        event_id = audit.payload.get("event_id")
        already_registered = any(
            row.id == event_id or row.id.startswith(f"{event_id}-")
            for row in book.entries)
        if event_id not in by_id or already_registered:
            continue
        try:
            order = mcp.order_by_client_id(str(audit.payload["client_order_id"]))
            filled = int(float(order.get("filled_qty") or 0))
        except Exception:  # noqa: BLE001 — unresolved remains fail-closed
            continue
        if filled < 1:
            continue
        book.add(BookEntry(
            id=event_id, underlying=str(audit.payload["symbol"]),
            structure="long_straddle", legs=list(audit.payload["legs"]),
            qty=filled, entry=float(order.get("filled_avg_price")
                                    or audit.payload["debit"]),
            max_profit=float(audit.payload["max_profit"]),
            max_loss=float(audit.payload["max_loss"]),
            opened_at=now.isoformat()))
        ledger.append("weekly_entry_recovered", {
            "event_id": event_id, "filled_qty": filled,
            "client_order_id": audit.payload["client_order_id"],
        }, recorded_at=now)


def _exit_entry(
    mcp: MCPClient, book: Book, ledger: AuditLedger, entry: BookEntry,
    *, now: dt.datetime, enable_orders: bool, reason: str,
) -> int:
    event_id = entry.id.rsplit("-recovered", 1)[0]
    coid, new_intent = _exit_coid(ledger, event_id)
    payload = {"event_id": event_id, "entry_id": entry.id,
               "qty": entry.qty, "reason": reason, "client_order_id": coid}
    if not enable_orders:
        ledger.append("weekly_shadow_exit", payload, recorded_at=now)
        log(f"SHADOW — would close {entry.underlying}: {reason}")
        return 0
    ledger.append(
        "weekly_exit_intent" if new_intent else "weekly_exit_retry",
        payload, recorded_at=now)

    def quote_fn(symbols):
        return _quote_map(
            mcp.option_latest_quote(",".join(symbols), feed="indicative"),
            symbols)

    fill = close_spread(
        mcp, entry.as_legs(), entry.qty, quote_fn, client_order_id=coid)
    ledger.append("weekly_exit_result", {
        **payload, "fill": fill}, recorded_at=now)
    if fill.how == "stuck" or not fill:
        log(f"{entry.id}: exit {fill.how}; management will retry")
        return 2
    price = fill.avg_price if fill.avg_price is not None else 0.0
    if fill.qty < entry.qty:
        book.close_partial(entry.id, qty=fill.qty, exit_price=price,
                           reason=reason, when=now.isoformat())
    else:
        book.close(entry.id, exit_price=price, reason=reason,
                   when=now.isoformat())
    log(f"CLOSED {entry.underlying} x{fill.qty} at {price}")
    return 0


def _enter(
    mcp: MCPClient, book: Book, ledger: AuditLedger,
    event: dict[str, Any], *, now: dt.datetime, equity: float,
    featherless_key: str, enable_orders: bool,
) -> int:
    schedule = _schedule(event["schedule"])
    policy = _policy(schedule)
    clock = mcp.market_clock()
    if not bool(clock.get("is_open")):
        raise ValueError("Alpaca market clock is closed")
    spot = _latest_spot(
        mcp.stock_snapshot(schedule.symbol, feed="iex"), schedule.symbol, now=now)
    chain = mcp.option_chain(
        schedule.symbol, feed="indicative", limit=1000,
        expiration_date=schedule.expiry,
        strike_price_gte=spot * .97, strike_price_lte=spot * 1.03)
    surface = surface_from_mcp(
        payload=chain, spot=spot, observed_at=now, policy=policy)
    live_gate = evaluate_entry(now=now, surface=surface, policy=policy)
    ledger.append("weekly_surface_decision", {
        "event_id": schedule.event_id, "decision": live_gate,
        "premium_to_spot": surface.executable_debit(policy.order_buffer) / spot,
    }, recorded_at=now)
    if not live_gate.eligible or live_gate.spread is None:
        log(f"{schedule.symbol}: NO TRADE — {'; '.join(live_gate.reasons)}")
        return 0

    semantic_ok, semantic_evidence = _semantic_recheck(
        mcp, featherless_key, event, now)
    ledger.append("weekly_semantic_recheck", {
        "event_id": schedule.event_id, **semantic_evidence}, recorded_at=now)
    promotion = evaluate_promotion(
        consensus=_consensus(event), semantic_confirmed=semantic_ok,
        replay=_replay(event), schedule=schedule,
        current_premium_to_spot=(
            surface.executable_debit(policy.order_buffer) / spot),
        current_total_spread_pct=surface.total_spread_pct)
    ledger.append("weekly_live_promotion", {
        "event_id": schedule.event_id, "decision": promotion}, recorded_at=now)
    if not event["promotion"]["promoted"] or not promotion.promoted:
        log(f"{schedule.symbol}: NO TRADE — {'; '.join(promotion.reasons)}")
        return 0

    spread = live_gate.spread
    registered_risk = sum(
        row.max_loss * 100.0 * row.qty for row in book.open_entries)
    aggregate_room = max(0.0, equity * .25 - registered_risk)
    assigned_room = max(0.0, float(event["max_loss_budget_usd"]))
    qty = math.floor(min(aggregate_room, assigned_room) /
                     (spread.max_loss * 100.0))
    if qty < 1:
        log(f"{schedule.symbol}: NO TRADE — allocated exact-risk room is below one contract")
        return 0
    legs = [{"symbol": leg.symbol, "side": leg.side,
             "ratio_qty": leg.ratio_qty} for leg in spread.legs]
    coid = _coid(schedule.event_id, "entry")
    intent = {
        "event_id": schedule.event_id, "symbol": schedule.symbol,
        "client_order_id": coid, "qty": qty, "debit": spread.max_loss,
        "max_loss": spread.max_loss, "max_profit": spread.max_profit,
        "total_max_loss_usd": qty * spread.max_loss * 100.0, "legs": legs,
    }
    if not enable_orders:
        ledger.append("weekly_shadow_entry", intent, recorded_at=now)
        log(f"SHADOW — would open {schedule.symbol} straddle x{qty}; "
            f"max loss ${intent['total_max_loss_usd']:,.0f}")
        return 0
    ledger.append("weekly_entry_intent", intent, recorded_at=now)
    fill = open_spread(
        mcp, spread, qty,
        {surface.call.symbol: surface.call.quote,
         surface.put.symbol: surface.put.quote}, client_order_id=coid)
    ledger.append("weekly_entry_result", {
        "event_id": schedule.event_id, "fill": fill}, recorded_at=now)
    if fill.how == "stuck":
        log(f"{schedule.symbol}: ENTRY ORDER STATE UNKNOWN — entries halted")
        return 2
    if not fill:
        log(f"{schedule.symbol}: entry did not fill; one-attempt policy is DONE")
        return 0
    filled = min(fill.qty or qty, qty)
    book.add(BookEntry(
        id=schedule.event_id, underlying=schedule.symbol,
        structure=spread.structure, legs=legs, qty=filled,
        entry=fill.avg_price if fill.avg_price is not None else spread.max_loss,
        max_profit=spread.max_profit, max_loss=spread.max_loss,
        opened_at=now.isoformat(timespec="seconds")))
    ledger.append("weekly_entry_booked", {
        "event_id": schedule.event_id, "filled_qty": filled}, recorded_at=now)
    log(f"OPENED {schedule.symbol} straddle x{filled}")
    return 0


def _refresh_plan(
    args: argparse.Namespace, prior_window: WeeklyWindow | None = None,
) -> int:
    command = [
        sys.executable, str(ROOT / "scripts" / "build_weekly_event_plan.py"),
        "--env", args.env, "--featherless-env", args.featherless_env,
        "--output", args.plan, "--ledger", args.planner_ledger,
    ]
    if prior_window is not None:
        start, deadline = prior_window.start, prior_window.deadline
        current = now_et().astimezone(ET)
        while deadline <= current:
            start += dt.timedelta(days=7)
            deadline += dt.timedelta(days=7)
        command.extend(["--start", start.isoformat(),
                        "--deadline", deadline.isoformat()])
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def _emergency_flat_without_plan(
    args: argparse.Namespace, book: Book, now: dt.datetime,
) -> int:
    """Keep managing registered risk even when discovery state is unreadable."""
    config = dotenv_values(ROOT / args.env)
    key, secret = config.get("ALPACA_API_KEY"), config.get("ALPACA_SECRET_KEY")
    expected_account = str(config.get("ALPACA_ACCOUNT_NUMBER") or "")
    if not key or not secret or not expected_account:
        log("cannot manage open risk: Alpaca configuration is incomplete")
        return 5
    if args.enable_orders and config.get("WEEKLY_EVENT_ENABLE_ORDERS") != "YES":
        log("cannot manage open risk: weekly order interlock is disabled")
        return 5
    ledger = AuditLedger(ROOT / args.ledger)
    with MCPClient(str(key), str(secret), live=args.enable_orders,
                   paper=True, timeout=90) as mcp:
        account = mcp.account()
        if str(account.get("account_number") or "") != expected_account:
            log("ACCOUNT PIN FAILED — refusing all actions")
            return 5
        code = 0
        for entry in list(book.open_entries):
            code = max(code, _exit_entry(
                mcp, book, ledger, entry, now=now,
                enable_orders=args.enable_orders,
                reason="weekly plan unavailable; fail-safe flatten"))
        return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--featherless-env", default=".env.local")
    parser.add_argument("--plan", default="data/weekly_event_plan.json")
    parser.add_argument("--book", default="data/weekly_event_book.json")
    parser.add_argument("--ledger", default="data/weekly_event_evidence.jsonl")
    parser.add_argument("--planner-ledger", default="data/weekly_event_evidence.jsonl")
    parser.add_argument("--lock", default="data/weekly_event_agent.lock")
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--flatten", action="store_true")
    parser.add_argument("--no-auto-plan", action="store_true")
    args = parser.parse_args()

    lock_path = ROOT / args.lock
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another weekly cycle owns the lock; exiting cleanly")
            return 0

        now = now_et()
        book = Book(ROOT / args.book)
        plan_path = ROOT / args.plan
        plan = None
        try:
            plan = read_plan(plan_path)
        except Exception as exc:  # noqa: BLE001
            if book.open_entries:
                log(f"plan unavailable with open risk: {type(exc).__name__}: {exc}")
                return _emergency_flat_without_plan(args, book, now)
            elif not args.no_auto_plan and _refresh_plan(args) == 0:
                plan = read_plan(plan_path)
            else:
                log(f"weekly plan unavailable: {type(exc).__name__}: {exc}")
                return 2
        assert plan is not None
        window = _window(plan)
        if now >= window.deadline and not book.open_entries and not args.no_auto_plan:
            if _refresh_plan(args, window) != 0:
                log("automatic week rollover failed")
                return 2
            plan = read_plan(plan_path)
            window = _window(plan)

        config = dict(dotenv_values(ROOT / args.featherless_env))
        config.update(dotenv_values(ROOT / args.env))
        key, secret = config.get("ALPACA_API_KEY"), config.get("ALPACA_SECRET_KEY")
        featherless_key = config.get("FEATHERLESS_API_KEY")
        expected_account = str(config.get("ALPACA_ACCOUNT_NUMBER") or "")
        if not key or not secret or not featherless_key or not expected_account:
            log("weekly executor configuration is incomplete")
            return 5
        if args.enable_orders and config.get("WEEKLY_EVENT_ENABLE_ORDERS") != "YES":
            log("orders disabled: WEEKLY_EVENT_ENABLE_ORDERS must equal YES")
            return 5

        ledger = AuditLedger(ROOT / args.ledger)
        events = [row for row in plan["events"]
                  if isinstance(row.get("schedule"), dict)
                  and bool((row.get("promotion") or {}).get("promoted"))]
        code = 0
        with MCPClient(str(key), str(secret), live=args.enable_orders,
                       paper=True, timeout=90) as mcp:
            account = mcp.account()
            actual_account = str(account.get("account_number") or "")
            actual_hash = hashlib.sha256(actual_account.encode()).hexdigest()
            if actual_account != expected_account or actual_hash != plan["account_sha256"]:
                log("ACCOUNT PIN FAILED — refusing all actions")
                return 5
            raw_positions = mcp.positions()
            _recover_entries(mcp, book, ledger, events, now)
            options, shares = _positions(raw_positions)
            diff = book.reconcile(options)
            can_enter = not diff and not shares
            if diff:
                ledger.append("weekly_reconciliation_mismatch", {"diff": diff},
                              recorded_at=now)
            if shares:
                ledger.append("weekly_assignment_detected", {"positions": shares},
                              recorded_at=now)

            by_id = {_schedule(row["schedule"]).event_id: row for row in events}
            # Exit first.  Reconciliation, model, and data failures never mask it.
            for entry in list(book.open_entries):
                event = by_id.get(entry.id)
                schedule = _schedule(event["schedule"]) if event else None
                due = (args.flatten or now >= window.deadline or schedule is None
                       or now >= schedule.exit_at)
                if not due:
                    continue
                reason = (
                    "manual flatten" if args.flatten else
                    "global weekly deadline" if now >= window.deadline else
                    "unplanned registered position" if schedule is None else
                    "event next-session exit")
                code = max(code, _exit_entry(
                    mcp, book, ledger, entry, now=now,
                    enable_orders=args.enable_orders, reason=reason))

            actions = {}
            for event in events:
                schedule = _schedule(event["schedule"])
                entry = _entry_for(book, schedule.event_id)
                action = event_lifecycle(
                    now=now, schedule=schedule, has_position=entry is not None,
                    entry_was_attempted=_entry_attempted(
                        ledger, schedule.event_id, live=args.enable_orders),
                    global_deadline=window.deadline)
                actions[schedule.event_id] = action
                if action == "ENTER" and can_enter and code == 0:
                    try:
                        code = max(code, _enter(
                            mcp, book, ledger, event, now=now,
                            equity=float(account["equity"]),
                            featherless_key=str(featherless_key),
                            enable_orders=args.enable_orders))
                    except Exception as exc:  # noqa: BLE001 — no-trade boundary
                        ledger.append("weekly_entry_gate_error", {
                            "event_id": schedule.event_id,
                            "error_type": type(exc).__name__, "error": str(exc),
                        }, recorded_at=now)
                        log(f"{schedule.symbol}: NO TRADE — {type(exc).__name__}: {exc}")

            ledger.append("weekly_cycle", {
                "plan_sha256": plan["plan_sha256"],
                "account_sha256": actual_hash, "equity": float(account["equity"]),
                "actions": actions, "can_enter": can_enter,
                "open_entries": len(book.open_entries),
                "window": plan["window"], "orders_enabled": args.enable_orders,
            }, recorded_at=now)
            try:
                ledger.append("weekly_alpaca_outcome", {
                    "account": mcp.account(),
                    "portfolio_history": mcp.portfolio_history(
                        period="1D", timeframe="5Min"),
                    "fill_activities": mcp.account_activities(
                        activity_types=["FILL"]),
                }, recorded_at=now_et())
            except Exception as exc:  # noqa: BLE001 — exit has already run
                ledger.append("weekly_outcome_read_error", {
                    "error_type": type(exc).__name__, "error": str(exc),
                }, recorded_at=now_et())
        return code


if __name__ == "__main__":
    raise SystemExit(main())
