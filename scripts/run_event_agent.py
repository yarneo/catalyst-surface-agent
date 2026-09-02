"""One autonomous Catalyst Surface Agent cycle.

The measured strategy is deliberately narrow: conditionally buy one AVGO Sep 4
ATM straddle during the frozen Sep 2 entry window, then exit at 09:45 ET on Sep
3. Featherless is a grounded, non-expansive event-integrity veto and post-event
semantic observer. Alpaca MCP owns account truth, news, stocks, option surface,
orders, positions, activities, and P&L. Every cycle appends a hash-chained row.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import pathlib
import sys
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
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
from trading_bot.tournament.catalyst import CatalystFact  # noqa: E402
from trading_bot.tournament.decision import (CandidateEvidence, EntryCandidate,  # noqa: E402
                                             plan_entries)
from trading_bot.tournament.featherless import FeatherlessClient  # noqa: E402
from trading_bot.tournament.integrity import evaluate_event_integrity  # noqa: E402
from trading_bot.tournament.scheduled import (ScheduledEventPolicy, evaluate_entry,  # noqa: E402
                                              lifecycle_action, surface_from_mcp)


START = dt.datetime(2026, 8, 31, 9, 30, tzinfo=ET)
DEADLINE = dt.datetime(2026, 9, 4, 9, 30, tzinfo=ET)
POLICY = ScheduledEventPolicy()
ENTRY_COID = "csa-avgo-q3-2026-entry"
EXIT_COID = "csa-avgo-q3-2026-exit"
ELIGIBLE_TICKERS = ("AVGO", "NVDA", "AMD", "MRVL", "SMH")


def log(message: str) -> None:
    print(f"{now_et():%Y-%m-%d %H:%M:%S %Z}  {message}", flush=True)


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


def _latest_spot(snapshot: Any, *, now: dt.datetime,
                 max_age_s: float = 90.0,
                 max_future_skew_s: float = 10.0) -> float:
    try:
        row = snapshot[POLICY.underlying]
        trade = row["latestTrade"]
        price = float(trade["p"])
        stamp = dt.datetime.fromisoformat(
            str(trade["t"]).replace("Z", "+00:00")).astimezone(ET)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("stock snapshot lacks a valid latest AVGO trade") from exc
    age = (now.astimezone(ET) - stamp).total_seconds()
    # IEX timestamps can lead the host wall clock by a few seconds. Permit a
    # tightly bounded feed/host skew without weakening the 90-second freshness.
    if not -max_future_skew_s <= age <= max_age_s:
        raise ValueError(f"AVGO trade is stale or future-dated ({age:.1f}s)")
    if price <= 0:
        raise ValueError("AVGO trade price is non-positive")
    return price


def _quote_map(payload: Any, symbols: list[str]) -> dict[str, Quote]:
    rows = payload.get("quotes", {}) if isinstance(payload, dict) else {}
    out = {}
    for symbol in symbols:
        row = rows.get(symbol)
        if not isinstance(row, dict):
            continue
        try:
            out[symbol] = Quote(float(row["bp"]), float(row["ap"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _news_facts(payload: Any) -> list[CatalystFact]:
    rows = payload.get("news", []) if isinstance(payload, dict) else []
    out = []
    for row in rows[:12]:
        if not isinstance(row, dict) or "AVGO" not in (row.get("symbols") or []):
            continue
        try:
            out.append(CatalystFact(
                fact_id=f"alpaca:{row['id']}",
                published_at=str(row.get("created_at") or row.get("updated_at")),
                headline=str(row["headline"]), summary=str(row.get("summary") or ""),
                symbols=tuple(symbol for symbol in row.get("symbols", [])
                              if symbol in ELIGIBLE_TICKERS) or ("AVGO",),
                source=str(row.get("source") or "alpaca_news"),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _official_fact() -> CatalystFact:
    # `published_at` records when this source fact was frozen into the strategy,
    # not a guessed press-release timestamp.
    return CatalystFact(
        "official:avgo-q3-fy2026", "2026-08-28T00:00:00-04:00",
        "Broadcom to announce Q3 FY2026 financial results",
        "Broadcom investor relations schedules results after market close on "
        "2026-09-02 and the conference call for 17:00 ET.",
        ("AVGO",), "broadcom_investor_relations")


def _fact_hash(facts: list[CatalystFact]) -> str:
    value = "|".join(sorted(
        f"{fact.fact_id}:{fact.published_at}:{fact.headline}" for fact in facts))
    return hashlib.sha256(value.encode()).hexdigest()


def _committee(mcp: MCPClient, featherless_key: str, ledger: AuditLedger,
               now: dt.datetime, *, refresh: bool = False):
    news = mcp.news(symbols="AVGO", start="2026-08-28", limit=50,
                    sort="desc", include_content=False)
    facts = [_official_fact(), *_news_facts(news)]
    batch_hash = _fact_hash(facts)
    prior = [row for row in ledger.read()
             if row.event_type == "featherless_committee"
             and row.payload.get("fact_hash") == batch_hash]
    if prior and not refresh:
        # Entry integrity needs the typed object, not merely a cached prose row.
        # Re-run only during entry; HOLD cycles can safely record the cache hit.
        return facts, None, batch_hash
    result = FeatherlessClient(featherless_key).analyze(
        facts, eligible_tickers=ELIGIBLE_TICKERS,
        require_actionable_direction=False)
    ledger.append("featherless_committee", {
        "fact_hash": batch_hash, "facts": facts, "result": result,
    }, recorded_at=now)
    return facts, result, batch_hash


def _latest_event(ledger: AuditLedger, event_type: str):
    return next((row for row in reversed(ledger.read())
                 if row.event_type == event_type), None)


def _entry_was_attempted(ledger: AuditLedger) -> bool:
    return _latest_event(ledger, "entry_intent") is not None


def _pending_exit_attempt(ledger: AuditLedger, entry_id: str) -> str | None:
    """Return an exit attempt that is still unsafe to supersede.

    A process can die after Alpaca accepts an order but before the result is
    recorded. A ``stuck`` result likewise means an order may still be live.
    Both cases must reuse the same client-order IDs; completed/unfilled/partial
    attempts must get fresh IDs so a later cycle does not replay old fills.
    """
    rows = ledger.read()
    results = {
        (row.payload.get("entry_id"), row.payload.get("client_order_id")):
        row.payload.get("fill", {}).get("how")
        for row in rows if row.event_type == "exit_result"
    }
    for row in reversed(rows):
        if row.event_type != "exit_intent" \
                or row.payload.get("entry_id") != entry_id:
            continue
        base = row.payload.get("client_order_id")
        if not isinstance(base, str) or not base:
            continue
        outcome = results.get((entry_id, base))
        if outcome is None or outcome == "stuck":
            return base
        return None
    return None


def _recover_entry(mcp: MCPClient, book: Book, ledger: AuditLedger,
                   now: dt.datetime) -> None:
    if book.open_entries or not _entry_was_attempted(ledger):
        return
    intent = _latest_event(ledger, "entry_intent")
    try:
        order = mcp.order_by_client_id(ENTRY_COID)
    except Exception:  # noqa: BLE001 — reconciliation below remains fail-closed
        return
    try:
        filled = int(float(order.get("filled_qty") or 0))
    except (AttributeError, TypeError, ValueError):
        return
    if filled < 1:
        return
    payload = intent.payload
    book.add(BookEntry(
        id=f"{POLICY.event_id}-recovered", underlying=POLICY.underlying,
        structure="long_straddle", legs=list(payload["legs"]), qty=filled,
        entry=float(order.get("filled_avg_price") or payload["debit"]),
        max_profit=float(payload["max_profit"]),
        max_loss=float(payload["max_loss"]), opened_at=now.isoformat()))
    ledger.append("entry_recovered", {"client_order_id": ENTRY_COID,
                  "filled_qty": filled, "order_status": order.get("status")},
                  recorded_at=now)


def _verify_account(acct: dict[str, Any], raw_positions: Any,
                    config: dict[str, Any]) -> list[str]:
    errors = []
    actual = str(acct.get("account_number") or "")
    expected = str(config.get("ALPACA_ACCOUNT_NUMBER") or "")
    if not expected:
        errors.append("ALPACA_ACCOUNT_NUMBER is not pinned")
    elif actual != expected:
        errors.append(f"account mismatch: expected {expected}, got {actual or 'missing'}")
    expected_equity = config.get("ALPACA_INITIAL_EQUITY")
    if expected_equity:
        try:
            if Decimal(str(acct["equity"])) != Decimal(str(expected_equity)):
                errors.append(f"equity is {acct.get('equity')}, expected {expected_equity}")
        except (InvalidOperation, KeyError):
            errors.append("invalid equity metadata")
    else:
        errors.append("ALPACA_INITIAL_EQUITY is not pinned")
    expected_level = int(config.get("ALPACA_OPTIONS_LEVEL") or 0)
    approved = int(acct.get("options_approved_level") or 0)
    enabled = int(acct.get("options_trading_level") or 0)
    if expected_level < 2 or min(approved, enabled) < expected_level:
        errors.append(
            f"options level approved={approved}, enabled={enabled}, expected={expected_level}")
    if acct.get("trading_blocked") or acct.get("account_blocked"):
        errors.append("account or trading is blocked")
    if raw_positions:
        errors.append(f"account is not flat ({len(raw_positions)} positions)")
    return errors


def _open(mcp: MCPClient, book: Book, ledger: AuditLedger, now: dt.datetime,
          equity: float, spread, surface, model_agreement: float,
          *, enable_orders: bool) -> int:
    evidence = CandidateEvidence(
        catalyst_strength=0.95, tape_confirmation=0.90,
        surface_lag=max(0.65, min(1.0, 1.0 - spread.max_loss / surface.spot)),
        model_agreement=model_agreement,
        spread_capture=max(0.80, 1.0 - surface.total_spread_pct),
        stale_mark_penalty=min(1.0, max(0.0, surface.quote_age_s / 360.0)))
    candidate = EntryCandidate(
        "avgo-scheduled-straddle", POLICY.event_id, spread, evidence,
        "scheduled_event")
    current_risk = sum(row.max_loss * 100.0 * row.qty for row in book.open_entries)
    plan = plan_entries(
        equity=equity, measured_start_equity=100_000.0,
        session_start_equity=100_000.0, current_max_loss_usd=current_risk,
        event_exposure_usd={POLICY.event_id: current_risk} if current_risk else {},
        candidates=[candidate])
    ledger.append("entry_plan", {"plan": plan, "evidence": evidence}, recorded_at=now)
    if not plan.open:
        log(f"NO TRADE — {'; '.join(plan.notes)}")
        return 0
    _, qty = plan.open[0]
    legs = [{"symbol": leg.symbol, "side": leg.side,
             "ratio_qty": leg.ratio_qty} for leg in spread.legs]
    intent = {
        "client_order_id": ENTRY_COID, "event_id": POLICY.event_id,
        "qty": qty, "debit": spread.max_loss, "max_loss": spread.max_loss,
        "max_profit": spread.max_profit, "legs": legs,
        "total_max_loss_usd": qty * spread.max_loss * 100.0,
    }
    if not enable_orders:
        ledger.append("shadow_entry", intent, recorded_at=now)
        log(f"SHADOW — would open AVGO straddle x{qty}; max loss "
            f"${intent['total_max_loss_usd']:,.0f}")
        return 0
    ledger.append("entry_intent", intent, recorded_at=now)
    quotes = {surface.call.symbol: surface.call.quote,
              surface.put.symbol: surface.put.quote}
    fill = open_spread(mcp, spread, qty, quotes, client_order_id=ENTRY_COID)
    ledger.append("entry_result", {"fill": fill}, recorded_at=now)
    if fill.how == "stuck":
        log("ENTRY ORDER STATE UNKNOWN — new actions halted")
        return 2
    if not fill:
        log("entry did not fill; frozen one-attempt policy is now DONE")
        return 0
    filled = min(fill.qty or qty, qty)
    entry = BookEntry(
        id=f"{POLICY.event_id}-1", underlying=POLICY.underlying,
        structure=spread.structure, legs=legs, qty=filled,
        entry=fill.avg_price if fill.avg_price is not None else spread.max_loss,
        max_profit=spread.max_profit, max_loss=spread.max_loss,
        opened_at=now.isoformat(timespec="seconds"))
    book.add(entry)
    ledger.append("entry_booked", {"entry": entry, "fill": fill}, recorded_at=now)
    log(f"OPENED AVGO straddle x{filled} at {entry.entry}")
    return 0


def _exit(mcp: MCPClient, book: Book, ledger: AuditLedger, now: dt.datetime,
          *, enable_orders: bool, reason: str) -> int:
    if not book.open_entries:
        return 0
    if not enable_orders:
        ledger.append("shadow_exit", {"reason": reason,
                      "entries": book.open_entries}, recorded_at=now)
        log(f"SHADOW — would close {len(book.open_entries)} structure(s): {reason}")
        return 0
    attention = False
    for entry in list(book.open_entries):
        attempt_base = _pending_exit_attempt(ledger, entry.id)
        if attempt_base is None:
            suffix = hashlib.sha256(entry.id.encode()).hexdigest()[:4]
            attempt_base = f"{EXIT_COID}-{suffix}-{now:%H%M%S%f}"
            ledger.append("exit_intent", {
                "entry_id": entry.id, "qty": entry.qty, "reason": reason,
                "client_order_id": attempt_base,
            }, recorded_at=now)
        else:
            ledger.append("exit_retry", {
                "entry_id": entry.id, "qty": entry.qty, "reason": reason,
                "client_order_id": attempt_base,
            }, recorded_at=now)

        def quote_fn(symbols):
            return _quote_map(
                mcp.option_latest_quote(",".join(symbols), feed="indicative"),
                symbols)

        fill = close_spread(mcp, entry.as_legs(), entry.qty, quote_fn,
                            client_order_id=attempt_base)
        ledger.append("exit_result", {"entry_id": entry.id, "fill": fill,
                      "reason": reason, "client_order_id": attempt_base},
                      recorded_at=now)
        if fill.how == "stuck" or not fill:
            attention = True
            log(f"{entry.id}: exit {fill.how}; remains managed and will retry")
            continue
        price = fill.avg_price if fill.avg_price is not None else 0.0
        if fill.qty < entry.qty:
            book.close_partial(entry.id, qty=fill.qty, exit_price=price,
                               reason=reason, when=now.isoformat())
        else:
            book.close(entry.id, exit_price=price, reason=reason,
                       when=now.isoformat())
        log(f"CLOSED {entry.id} x{fill.qty} at {price}")
    return 2 if attention else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.local")
    parser.add_argument("--featherless-env", default=".env.local")
    parser.add_argument("--book", default="data/event_book.json")
    parser.add_argument("--ledger", default="data/event_evidence.jsonl")
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--verify-account", action="store_true")
    parser.add_argument("--flatten", action="store_true")
    args = parser.parse_args()

    config = dict(dotenv_values(ROOT / args.featherless_env))
    config.update(dotenv_values(ROOT / args.env))
    key, secret = config.get("ALPACA_API_KEY"), config.get("ALPACA_SECRET_KEY")
    featherless_key = config.get("FEATHERLESS_API_KEY")
    if not key or not secret:
        log("missing Alpaca credentials")
        return 5
    if args.enable_orders and config.get("TOURNAMENT_ENABLE_ORDERS") != "YES":
        log("orders disabled: TOURNAMENT_ENABLE_ORDERS must equal YES")
        return 5

    now = now_et()
    book = Book(ROOT / args.book)
    ledger = AuditLedger(ROOT / args.ledger)
    with MCPClient(key, secret, live=args.enable_orders, paper=True) as mcp:
        acct = mcp.account()
        raw = mcp.positions()
        if args.verify_account:
            errors = _verify_account(acct, raw, config)
            for error in errors:
                log(f"VERIFY FAILED — {error}")
            if not errors:
                log("VERIFY OK — pinned $100k paper account, flat, unblocked, options enabled")
            return 5 if errors else 0

        actual_account = str(acct.get("account_number") or "")
        expected_account = str(config.get("ALPACA_ACCOUNT_NUMBER") or "")
        if not expected_account or actual_account != expected_account:
            log("ACCOUNT PIN FAILED — refusing all actions")
            return 5

        _recover_entry(mcp, book, ledger, now)
        options, shares = _positions(raw)
        if shares:
            ledger.append("assignment_detected", {"positions": shares}, recorded_at=now)
            log("NON-OPTION POSITION DETECTED — entry halted")
            if not args.flatten:
                return 3
        diff = book.reconcile(options)
        can_enter = not diff and not shares
        if diff:
            ledger.append("reconciliation_mismatch", {"diff": diff}, recorded_at=now)
            log(f"REGISTRY MISMATCH — {len(diff)} disagreement(s); entry halted")

        equity = float(acct["equity"])
        action = "EXIT" if args.flatten else lifecycle_action(
            now=now, has_position=bool(book.open_entries),
            entry_was_attempted=_entry_was_attempted(ledger))
        ledger.append("cycle", {
            "account_number": actual_account, "equity": equity,
            "action": action, "can_enter": can_enter,
            "open_entries": len(book.open_entries), "window_start": START,
            "deadline": DEADLINE,
            "exit_deadline_state": (
                "emergency_flat_by"
                if action == "EXIT" and now >= POLICY.emergency_flat_by else
                "scheduled_exit" if action == "EXIT" else None
            ),
        }, recorded_at=now)
        log(f"action={action} equity=${equity:,.2f} open={len(book.open_entries)}")

        if action == "EXIT":
            if args.flatten:
                exit_reason = "manual flatten"
            elif now >= POLICY.emergency_flat_by:
                exit_reason = "emergency flat-by deadline"
            else:
                exit_reason = "frozen post-earnings 09:45 exit"
            code = _exit(mcp, book, ledger, now, enable_orders=args.enable_orders,
                         reason=exit_reason)
        elif action == "ENTER" and can_enter:
            try:
                clock = mcp.market_clock()
                if not bool(clock.get("is_open")):
                    raise ValueError("Alpaca market clock is closed")
                spot = _latest_spot(
                    mcp.stock_snapshot(POLICY.underlying, feed="iex"), now=now)
                chain = mcp.option_chain(
                    POLICY.underlying, feed="indicative", limit=200,
                    expiration_date=POLICY.expiry,
                    strike_price_gte=spot * 0.97, strike_price_lte=spot * 1.03)
                surface = surface_from_mcp(
                    payload=chain, spot=spot, observed_at=now, policy=POLICY)
                surface_decision = evaluate_entry(now=now, surface=surface, policy=POLICY)
                ledger.append("surface_decision", {
                    "decision": surface_decision,
                    "premium_to_spot": (
                        surface.executable_debit(POLICY.order_buffer) / spot),
                }, recorded_at=now)
                if not surface_decision.eligible or surface_decision.spread is None:
                    log(f"NO TRADE — {'; '.join(surface_decision.reasons)}")
                    code = 0
                else:
                    if not featherless_key:
                        raise ValueError("missing Featherless API key")
                    facts, committee, _ = _committee(
                        mcp, str(featherless_key), ledger, now, refresh=True)
                    # A same-batch cache cannot happen on the one permitted entry
                    # attempt; if it does, fail closed rather than deserialize a
                    # model object from prose.
                    if committee is None:
                        log("NO TRADE — Featherless integrity result is not fresh")
                        code = 0
                    else:
                        integrity = evaluate_event_integrity(committee, facts)
                        ledger.append("event_integrity", {"decision": integrity},
                                      recorded_at=now)
                        if not integrity.clear:
                            log(f"NO TRADE — {integrity.reason}")
                            code = 0
                        else:
                            code = _open(
                                mcp, book, ledger, now, equity,
                                surface_decision.spread, surface,
                                committee.agreement,
                                enable_orders=args.enable_orders)
            except Exception as exc:  # noqa: BLE001 — evidence-gated no-trade
                ledger.append("entry_gate_error", {
                    "error_type": type(exc).__name__, "error": str(exc)},
                    recorded_at=now)
                log(f"NO TRADE — {type(exc).__name__}: {exc}")
                code = 0
        elif action == "HOLD" and now >= POLICY.event_at:
            try:
                if not featherless_key:
                    raise ValueError("missing Featherless API key")
                facts, committee, fact_hash = _committee(
                    mcp, str(featherless_key), ledger, now)
                if committee is not None:
                    ledger.append("post_event_semantics", {
                        "fact_hash": fact_hash, "facts": facts,
                        "accepted": committee.accepted,
                        "agreement": committee.agreement,
                        "reason": committee.reason,
                    }, recorded_at=now)
                    log(f"Featherless post-event: {committee.reason}")
            except Exception as exc:  # noqa: BLE001 — never delays frozen exit
                ledger.append("post_event_semantic_error", {
                    "error_type": type(exc).__name__, "error": str(exc)},
                    recorded_at=now)
                log(f"post-event semantic read failed: {type(exc).__name__}: {exc}")
            code = 0
        else:
            code = 0

        # Sponsor-native outcome evidence on every cycle. Read failures do not
        # erase the primary account snapshot or block a deadline exit.
        try:
            history = mcp.portfolio_history(period="1D", timeframe="5Min")
            activities = mcp.account_activities(activity_types=["FILL"])
            ledger.append("alpaca_outcome", {
                "account": mcp.account(), "portfolio_history": history,
                "fill_activities": activities,
            }, recorded_at=now_et())
        except Exception as exc:  # noqa: BLE001
            ledger.append("outcome_read_error", {
                "error_type": type(exc).__name__, "error": str(exc)},
                recorded_at=now_et())
        return code


if __name__ == "__main__":
    raise SystemExit(main())
