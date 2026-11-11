"""The agent, one cycle per invocation.

Deliberately thin: read state, ask `plan_session` what to do, carry it out. Six
audits found ~50 defects and the majority lived in this layer, so the rules it
enforces itself are only the ones that must happen BEFORE a decision can be
trusted:

    reconcile   -> the registry must match the account
    assignment  -> a non-option position means an option was exercised
    marking     -> a position we cannot price is a position we cannot manage

Each of those was a real defect. Reconcile used to `return 2` on any mismatch,
which also disabled the deadline flatten — so an expired-worthless condor, the
most common outcome of this strategy, left the agent unable to close anything
for the rest of the contest. Assignment was invisible because positions were
filtered to symbols longer than six characters, hiding ~$37k of naked short
stock. And a position whose quote was missing was silently dropped from the
managed book while its risk vanished from the budget, so the agent opened a
full-size book on top of one it could not see, every cycle.

    --dry-run   plan and print, place nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
import warnings
from decimal import Decimal, InvalidOperation
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import dotenv_values  # noqa: E402

from trading_bot.options.book import Book  # noqa: E402
from trading_bot.options.chain import ChainClient  # noqa: E402
from trading_bot.options.clock import (ET, flat_by, is_market_open,  # noqa: E402
                                       last_expiry_settling_before, now_et)
from trading_bot.options.condor import build_condor  # noqa: E402
from trading_bot.options.executor import execute_plan  # noqa: E402
from trading_bot.options.iv import IVUnavailable  # noqa: E402
from trading_bot.options.journal import Entry, append, narrate  # noqa: E402
from trading_bot.options.mcp import MCPClient  # noqa: E402
from trading_bot.options.occ import parse  # noqa: E402
from trading_bot.options.rnd import fit_smile  # noqa: E402
from trading_bot.options.session import Holding, plan_session  # noqa: E402
from trading_bot.options.spreads import marketable_limit  # noqa: E402

START = dt.datetime(2026, 8, 31, 9, 30, tzinfo=ET)
DEADLINE = dt.datetime(2026, 9, 4, 9, 30, tzinfo=ET)

# Indices for breadth, single names for the idiosyncratic risk that actually
# diversifies them. Names with earnings inside the window are excluded at scan
# time by the term-structure check, not by a hardcoded list.
UNIVERSE = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "GOOGL", "AMZN", "META",
            "TSLA", "AMD", "NFLX"]

EVENT_RATIO = 1.15      # front/back ATM vol above this means a scheduled event


def log(msg: str) -> None:
    print(f"{now_et():%Y-%m-%d %H:%M:%S %Z}  {msg}", flush=True)


def _opened_facts(opened) -> list[dict]:
    """Describe registry rows that filled, never candidates that were planned.

    An execution report is shorter than ``plan.open`` whenever an order fails.
    Zipping the two lists therefore shifts the remaining fills onto the wrong
    symbols.  Build narration from the durable rows written by the executor.
    """
    facts = []
    for entry, _fill in opened:
        row = {
            "symbol": entry.underlying,
            "qty": entry.qty,
            "credit": -entry.entry * 100 * entry.qty,
            "risk": entry.max_loss * 100 * entry.qty,
        }
        # Strike detail is useful to an LLM narrator but non-essential. Keep
        # this post-trade path incapable of failing the scheduler if an unusual
        # adjusted contract symbol ever reaches the registry.
        try:
            parsed = [(leg, parse(leg["symbol"])) for leg in entry.legs]
            short_put = next(c.strike for leg, c in parsed
                             if leg["side"] == "sell" and c.right == "P")
            short_call = next(c.strike for leg, c in parsed
                              if leg["side"] == "sell" and c.right == "C")
            row.update({
                "strikes": f"{short_put:g}/{short_call:g}",
                "expiry": max(c.expiry for _leg, c in parsed),
            })
        except Exception:  # noqa: BLE001 — narration must never break a cycle
            row["legs"] = [leg.get("symbol", "unknown") for leg in entry.legs]
        facts.append(row)
    return facts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env.hackathon")
    ap.add_argument("--book", default="data/options_book.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-account", action="store_true",
                    help="verify pinned account/equity/option level and exit")
    # Sized for a graded week, not for a track record. Backtested over 736
    # contest-shaped weeks: 25-delta shorts cannot clear +50% at ANY risk budget
    # — the best week in fourteen years paid 49.3% of risk, and a condor cannot
    # pay more than its credit. Moving to 30-delta breaks that ceiling (median
    # +38%, roughly even odds of +30%) while leaving a bad week at -70% rather
    # than a near-wipe, which matters because P&L is one of four judged criteria
    # and the live demo has to still be running on submission day.
    ap.add_argument("--max-risk", type=float, default=0.70)
    ap.add_argument("--per-name", type=float, default=0.18)
    ap.add_argument("--short-delta", type=float, default=0.30)
    # Validation only: lets a dev-account cycle exercise the open path before
    # the judged window begins. The judged run never passes it.
    ap.add_argument("--start-override", default=None,
                    help="ISO datetime (ET) to use as the window start")
    ap.add_argument("--flatten", action="store_true",
                    help="close every position and open nothing (kill switch)")
    ap.add_argument("--ignore-market-hours", action="store_true",
                    help="plan against stale quotes; dry-run inspection only")
    a = ap.parse_args()

    v = dotenv_values(ROOT / a.env)
    key, sec = v["ALPACA_API_KEY"], v["ALPACA_SECRET_KEY"]
    chain = ChainClient(key=key, secret=sec)
    book = Book(ROOT / a.book)
    now = now_et()
    start = START
    if a.start_override:
        start = dt.datetime.fromisoformat(a.start_override).replace(tzinfo=ET)
        log(f"START OVERRIDDEN to {start:%Y-%m-%d %H:%M %Z} — validation only")
    last_safe = last_expiry_settling_before(DEADLINE)
    open_now = is_market_open(now)
    log(f"cycle start; last settling expiry {last_safe}; "
        f"flat-by {flat_by(DEADLINE):%Y-%m-%d %H:%M %Z}; "
        f"market {'OPEN' if open_now else 'closed'}")

    # Exit before touching the broker or the chain. Nothing can be traded into a
    # closed book, and a full scan costs ~90 seconds of rate-limited requests.
    # This is also what lets the scheduler stay dumb: it fires on a fixed
    # interval and contains no timezone logic of its own, which matters in a repo
    # that has had four separate date/time incidents.
    if not open_now and not a.ignore_market_hours and not a.verify_account:
        log("  market closed — nothing to do")
        return 0

    # Account verification is always read-only, even if --dry-run was omitted.
    with MCPClient(key, sec, live=not a.dry_run and not a.verify_account) as mcp:
        acct = mcp.account()
        actual_account = str(acct.get("account_number") or "")
        expected_account = str(v.get("ALPACA_ACCOUNT_NUMBER") or "")
        if expected_account and actual_account != expected_account:
            log(f"ACCOUNT MISMATCH — expected {expected_account}, got "
                f"{actual_account or 'missing'}; refusing to continue")
            return 5
        equity = float(acct["equity"])
        raw = mcp.positions()
        log(f"account {actual_account or 'unknown'}  equity ${equity:,.2f}")

        if a.verify_account:
            errors = []
            expected_equity = v.get("ALPACA_INITIAL_EQUITY")
            if expected_equity:
                try:
                    if Decimal(str(acct["equity"])) != Decimal(expected_equity):
                        errors.append(
                            f"equity is ${equity:,.2f}, expected "
                            f"${Decimal(expected_equity):,.2f}")
                except (InvalidOperation, KeyError) as exc:
                    errors.append(f"invalid equity metadata: {exc}")
            expected_level = int(v.get("ALPACA_OPTIONS_LEVEL") or 0)
            approved = int(acct.get("options_approved_level") or 0)
            enabled = int(acct.get("options_trading_level") or 0)
            if expected_level and min(approved, enabled) < expected_level:
                errors.append(
                    f"options level approved={approved}, enabled={enabled}; "
                    f"expected {expected_level}")
            if acct.get("trading_blocked") or acct.get("account_blocked"):
                errors.append("account or trading is blocked")
            if raw:
                errors.append(f"account is not flat ({len(raw)} positions)")
            if errors:
                for error in errors:
                    log(f"VERIFY FAILED — {error}")
                return 5
            log(f"VERIFY OK — exact initial equity, options level "
                f"{expected_level}, flat and unblocked")
            return 0

        options = {p["symbol"]: int(float(p["qty"]))
                   for p in raw if len(p["symbol"]) > 6}
        shares = [p for p in raw if len(p["symbol"]) <= 6]

        # --- assignment: an option was exercised against us
        if shares:
            log("ASSIGNMENT DETECTED — non-option positions are open:")
            for p in shares:
                log(f"    {p['symbol']} qty={p['qty']} "
                    f"mv=${float(p['market_value']):,.0f}")
            log("  a short leg was exercised. The structure's stated max loss no "
                "longer bounds this account. Refusing to trade; resolve by hand.")
            return 3

        # --- retire anything that already expired, before reconciling against
        # the account. Otherwise an expired-worthless condor leaves four legs in
        # the registry against an empty account and blocks opening forever.
        expired = book.expire_passed(now.date())
        for eid in expired:
            log(f"  retired expired row {eid}")

        # --- reconcile. A mismatch blocks OPENING, never closing.
        diff = book.reconcile(options)
        can_open = not diff
        if diff:
            log("REGISTRY DISAGREES WITH ACCOUNT — will not open new positions:")
            for sym, (exp, act) in diff.items():
                log(f"    {sym}: registry {exp:+d}, account {act:+d}")
            log("  closing and the deadline flatten remain enabled.")

        # --- mark the book. A position we cannot price is a hard stop.
        holdings: list[Holding] = []
        for e in book.open_entries:
            try:
                qs = chain.quotes([l["symbol"] for l in e.legs])
                mark = marketable_limit(e.as_legs(), qs, intent="close", buffer=0.0)
            except Exception as exc:  # noqa: BLE001
                # An unmarkable position must still be MANAGED. Halting here is
                # what killed the deadline flatten in the previous two versions:
                # the strategy holds contracts whose quotes vanish, and a hard
                # return meant every later cycle died before planning anything.
                #
                # Mark it at its worst case instead. That is conservative in the
                # right direction — it trips the loss limit, so the position is
                # closed rather than quietly carried — and the cycle continues.
                mark = e.max_loss + e.entry
                log(f"  cannot price {e.id} ({type(exc).__name__}: "
                    f"{str(exc)[:60]}); marking at worst case to force an exit")
            holdings.append(Holding(e.id, e.underlying, e.as_legs(), e.qty,
                                    e.entry, mark, e.max_loss))

        # --- scan
        candidates, skipped = [], []
        if can_open:
            for sym in UNIVERSE:
                try:
                    c = _scan(chain, sym, last_safe, a.short_delta)
                    if c is not None:
                        candidates.append(c)
                        log(f"  {c.describe()}")
                    else:
                        skipped.append({"symbol": sym,
                                        "reason": "event in window or no expiry"})
                except (IVUnavailable, Exception) as exc:  # noqa: BLE001
                    log(f"  {sym}: skipped ({type(exc).__name__}: "
                        f"{str(exc)[:80]})")
                    skipped.append({"symbol": sym, "reason": str(exc)[:60]})

        # A full universe scan takes two to three minutes. Never decide against
        # the timestamp captured before it: a 10:59 cycle can finish after the
        # 11:00 contest start, and a pre-deadline scan can cross flat-by while
        # it is working. Refresh immediately before the pure decision.
        decision_now = now_et()
        if decision_now.date() != now.date() or \
                int((decision_now - now).total_seconds()) >= 60:
            log(f"  scan took {(decision_now - now).total_seconds():.0f}s; "
                f"planning at {decision_now:%H:%M:%S %Z}")
        plan = plan_session(
            now=decision_now, start=start, deadline=DEADLINE, equity=equity,
            holdings=holdings, candidates=candidates,
            max_risk_pct=a.max_risk, per_name_pct=a.per_name,
            require_open_market=not a.ignore_market_hours,
            flatten=a.flatten)
        for n in plan.notes:
            log(f"  {n}")
        log(f"plan: {plan.summary()}")

        if a.dry_run:
            for h, why in plan.close:
                log(f"  WOULD CLOSE {h.entry_id} ({h.underlying} x{h.qty}) — {why}")
            for c, qty in plan.open:
                log(f"  WOULD OPEN {c.describe()} x{qty} "
                    f"= ${qty * c.max_loss * 100:,.0f} risk, "
                    f"${qty * c.credit * 100:,.0f} credit")
            return 0

        rep = execute_plan(mcp, lambda s: chain.quotes(s), book, plan,
                           log=log, now=decision_now)
        log(f"execution: {rep.summary()}")
        for what, why in rep.failures:
            log(f"  FAILED {what}: {why}")
        for oid in rep.orphans:
            log(f"  ORPHANED {oid}")

        # --- journal: narrate what already happened. Read-only by design —
        # this runs after execute_plan and nothing reads its output back.
        facts = {
            "when": decision_now.isoformat(timespec="minutes"),
            "equity": equity,
            "skipped": skipped,
            "opened": _opened_facts(rep.opened),
            "closed": [{"symbol": h.underlying, "qty": h.qty, "reason": why}
                       for h, why in plan.close],
            "summary": plan.summary(),
        }
        entry = narrate(facts)
        # The journal lives NEXT TO the registry, not at a hardcoded path.
        # Hardcoding ROOT/data meant every test run of main() — which points
        # --book at a temp dir — appended its fixture trades to the PRODUCTION
        # journal, and the dashboard dutifully displayed "Closed SPY x4" events
        # that never happened to any judge who scrolled by.
        append(pathlib.Path(ROOT / a.book).parent / "journal.jsonl", entry)
        log(f"journal[{'llm' if entry.generated else 'fallback'}]: "
            f"{entry.text[:100]}")

        final = mcp.account()
        log(f"equity ${float(final['equity']):,.2f}  "
            f"realised ${book.realised_pnl():+,.2f}  "
            f"open structures {len(book.open_entries)}")
        return 1 if rep.requires_attention() else 0


def _scan(chain: ChainClient, sym: str, last_safe: dt.date, short_delta: float):
    """Build one condor for `sym`, or None if it should be skipped."""
    spot = chain.spot(sym)
    dte_max = (last_safe - now_et().date()).days
    if dte_max < 1:
        return None
    exps = [e for e, _ in chain.expiries(sym, spot, dte_min=1, dte_max=dte_max)]
    if not exps:
        log(f"  {sym}: no expiry settling on or before {last_safe}")
        return None
    expiry = exps[-1]

    sig = chain.signal(sym, expiry=expiry)
    _, strikes, occ = chain.structure_inputs(sig, moneyness=0.25)
    quotes = chain.quotes(list(occ.values()))
    calls = {k: quotes[occ[(k, "C")]] for k in strikes
             if (k, "C") in occ and occ[(k, "C")] in quotes}
    puts = {k: quotes[occ[(k, "P")]] for k in strikes
            if (k, "P") in occ and occ[(k, "P")] in quotes}
    smile = fit_smile(spot, sig.dte / 365, calls, puts)

    # Scheduled event inside the window? Selling a condor into an earnings gap
    # is selling exactly the tail the structure cannot absorb. The DETECTION —
    # front vol far above a later expiry's — is sound and was measured at 1.02
    # for index ETFs against 1.40-1.46 for the three names reporting in the
    # window. Only the variance DECOMPOSITION built on top of it was invalid, so
    # this uses the ratio as a filter and makes no attempt to price the jump.
    back = [e for e, d in chain.expiries(sym, spot, dte_min=21, dte_max=60)]
    if back:
        try:
            bsig = chain.signal(sym, expiry=back[0])
            _, bk, bocc = chain.structure_inputs(bsig, moneyness=0.12)
            bq = chain.quotes(list(bocc.values()))
            bc = {k: bq[bocc[(k, "C")]] for k in bk
                  if (k, "C") in bocc and bocc[(k, "C")] in bq}
            bp = {k: bq[bocc[(k, "P")]] for k in bk
                  if (k, "P") in bocc and bocc[(k, "P")] in bq}
            back_iv = fit_smile(spot, bsig.dte / 365, bc, bp).atm_iv
            ratio = smile.atm_iv / back_iv if back_iv > 0 else 1.0
            if ratio >= EVENT_RATIO:
                log(f"  {sym}: event in window ({ratio:.2f}x front/back) — "
                    f"not selling a condor into a scheduled gap")
                return None
        except Exception as exc:  # noqa: BLE001
            log(f"  {sym}: no event reference ({type(exc).__name__}); "
                f"skipping rather than selling blind")
            return None

    return build_condor(sym, smile.forward, smile.atm_iv, sig.dte / 365,
                        expiry, sig.dte, strikes, occ, quotes,
                        short_delta=short_delta)


if __name__ == "__main__":
    sys.exit(main())
