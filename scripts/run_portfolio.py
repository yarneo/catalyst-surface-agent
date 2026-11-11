"""Run the WHOLE portfolio in one scheduled process.

    uv run python scripts/run_portfolio.py --phase premarket
    uv run python scripts/run_portfolio.py --phase open           # dry-run
    uv run python scripts/run_portfolio.py --phase open --live    # submit orders
    uv run python scripts/run_portfolio.py --phase close

Why this exists
---------------
Until 2026-08 the work was split across two hosts, and each ran a different
half of the system against a *different* Alpaca account:

  * the Mac (launchd) ran ``run_live.py`` — the ETF mean-reversion sleeve only;
  * the GCP VM ran regime / crypto / managed-futures / cash-park on its own
    per-sleeve cron entries, against its own account (created 2026-06-27).

The VM's crontab was hand-edited and never written back to
``deploy/setup_vm.sh``, so the repo showed only ``run_live`` + ``retrain`` and
badly understated what was actually running.

The damage was on the Mac's account. Its QLD / KMLM / DBMF / crypto positions
had been opened while the VM still pointed there; when the VM moved to its own
account on 2026-06-27 those positions were abandoned mid-flight. From that day
63% of the Mac's book sat in positions no running process managed — the fills
confirm it exactly: QLD took 23 fills in June and none afterwards.

No rebalance, no regime check, and no stop-loss — because ``RiskManager`` lived
inside ``run_live.py`` and could only see that script's universe. QLD drifted to
32% of equity and -10.35%, past a -8% stop that physically could not observe it.
That single position was 67% of the drawdown.

This script makes that class of failure structurally impossible:

  1. **Ownership audit first.** Every held symbol must map to exactly one
     enabled sleeve. Orphans and contested symbols are loud errors, not silence.
  2. **Portfolio risk guard second.** Stops and weight caps are applied to the
     broker's *actual* position list, so a position can never outlive the
     executor that opened it.
  3. **Then the sleeves**, in dependency order, all in one process. Adding a
     sleeve here is the only way to deploy it, so "built but never scheduled"
     cannot recur.

Sleeve order matters: active sleeves deploy first, the idle router then sizes
itself against what's genuinely deployed, and cash-park sweeps the remainder
last.
"""

from __future__ import annotations

import argparse
import time
import traceback
import sys
import json
from datetime import date
from pathlib import Path
from dataclasses import asdict

from trading_bot.broker import AlpacaBroker
from trading_bot.broker.evidence import capture_broker, capture_inputs, capture_cycle
from trading_bot.cycle import SessionState, plan_cycle
from trading_bot.config import (
    load_crypto_universe,
    load_pead_universe,
    load_universe,
    to_alpaca_position_symbol,
    to_alpaca_symbol,
)
from trading_bot.marketdata import PriceUnavailable, signal_session, captured_signals
from trading_bot.runtime_state import DATA, atomic_json, execution_lock, append_record, ExecutionBusy
from trading_bot.sessions import now_et, iso_now, last_completed_session
from trading_bot.learning import ChampionStore
from trading_bot.allocations import DEFAULT_SLEEVE_ENABLEMENT
from trading_bot.live import ExecutorState, PaperExecutor
from trading_bot.live.cash_park_executor import CashParkExecutor
from trading_bot.live.crypto_executor import CryptoExecutor
from trading_bot.live.managed_futures_executor import ManagedFuturesExecutor
from trading_bot.portfolio_policy import load_policy
from trading_bot.live.pead_executor import PeadExecutor
from trading_bot.live.regime_executor import RegimeExecutor
from trading_bot.ownership import build_sleeves, format_audit
from trading_bot.risk import (
    RiskLimits,
    RiskManager,
    VolatilityTargeted,
    format_guard,
)
from trading_bot.signals import OverlayConfig, SignalOverlay
from trading_bot.strategies import REGISTRY

# Which sleeves run. Disabled sleeves still OWN their symbols (so their
# holdings are flagged for wind-down rather than silently orphaned), they just
# don't trade.
#
# crypto/pead are off pending validation: crypto lost money on all six coins
# (-$1,119 over 84 fills) and PEAD has never placed a single order, so neither
# has live evidence behind it. Turn them on one at a time, with a dry-run first.
DEFAULT_SLEEVES = DEFAULT_SLEEVE_ENABLEMENT


def settle_orders(broker, live: bool, timeout_s: float = 180.0,
                  poll_s: float = 1.5, sleep=time.sleep) -> bool:
    """Block until no orders are open, so the next sleeve sees a true balance.

    Alpaca's ``cash`` is the settled balance — submitting a buy does not reduce
    it, only a fill does. Sleeves run in sequence and each sizes itself from
    account state, so without this every sleeve allocates against the same
    unspent cash. On the fresh $100k account that produced $206k of buy orders:
    regime $20k + router $88k + cash-park $98k, each computed as though nothing
    else had spent anything. Paper margin is 4x so nothing would have bounced —
    it would just have opened at 2x leverage.

    Returns True if the book settled, False on timeout (caller should stop
    rather than let the next sleeve size off a stale balance).

    Timeout is 180s, not 45s. On 2026-08-05 a real cycle hit the 45s limit and
    aborted before the router, cash-park and post-cycle leverage check could
    run — the orders did fill, just not that fast. Alpaca paper fills are
    noticeably slower than live ones, and stopping the cycle is a real cost
    (that run left the book levered and unchecked overnight), so wait longer
    before giving up.
    """
    if not live:
        return True
    waited = 0.0
    while waited < timeout_s:
        try:
            open_orders = broker.open_orders()
        except Exception as exc:  # noqa: BLE001
            print(f"    ⚠ could not read open orders ({exc}) — assuming unsettled")
            return False
        if not open_orders:
            if hasattr(broker, "reconcile_orders"):
                try:
                    broker.reconcile_orders()
                except Exception:
                    return False
            return True
        syms = ", ".join(sorted({o.symbol for o in open_orders}))
        print(f"    ⏳ waiting for {len(open_orders)} order(s) to fill: {syms}")
        sleep(poll_s)
        waited += poll_s
    print(f"    ⚠ orders still open after {timeout_s:.0f}s")
    return False


def _run_sleeve(name: str, fn) -> bool:
    """Run one sleeve, isolating its failures.

    A sleeve that can't get data must not take the rest of the portfolio down
    with it — and must not trade. PriceUnavailable is the expected, safe
    outcome of a feed glitch (see trading_bot/marketdata.py); anything else is
    reported loudly but still contained, because a half-run cycle that skips
    the cash sweep is worse than one sleeve sitting out.
    """
    print(f"\n--- {name.upper()} ---")
    try:
        outcome = fn()
        def failed(value):
            if isinstance(value, dict):
                return any((k == "action" and str(v).endswith(("failed", "error")))
                           or failed(v) for k, v in value.items())
            if isinstance(value, list):
                return any(failed(v) for v in value)
            return False
        if failed(outcome):
            raise RuntimeError(f"{name}: incomplete execution")
        return True
    except PriceUnavailable as exc:
        print(f"  ⚠ SKIPPED — market data unavailable: {exc}")
        print("    (holding existing positions; no orders submitted this cycle)")
        raise RuntimeError(f"{name}: market data unavailable") from exc
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ SLEEVE FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        raise RuntimeError(f"{name}: execution failed") from exc


def _apply_guard_actions(
    broker: AlpacaBroker, result, live: bool,
    crypto_symbols: frozenset[str] = frozenset(), crypto_live: bool = False,
    crypto_order_symbols: dict[str, str] | None = None,
) -> int:
    """Execute the guard's de-risking decisions. Sells only — the guard is
    never allowed to increase risk. Returns the number of symbols acted on.

    Crypto is settled on the 24/7 venue, so a kill switch can still liquidate it
    while the equity market is shut — which is exactly when you want it to.
    """
    crypto_order_symbols = crypto_order_symbols or {}
    acted = 0
    for a in result.actions:
        delta = a.target_value - a.current_value
        if delta >= 0:
            continue
        can_trade = crypto_live if a.symbol in crypto_symbols else live
        if a.target_value <= 1.0:
            print(f"    → close {a.symbol} (${a.current_value:,.0f})")
            if can_trade:
                try:
                    if hasattr(broker, "close_position"):
                        broker.close_position(a.symbol, sleeve="risk_guard")
                    else:
                        broker._client.close_position(a.symbol)
                    print("      ✅ close submitted")
                    acted += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"      ❌ close failed: {exc}")
                    raise
            else:
                print("      [dry-run] would close")
                acted += 1
        else:
            print(f"    → trim {a.symbol} by ${abs(delta):,.0f}")
            if can_trade:
                try:
                    price = float(
                        broker._client.get_open_position(a.symbol).current_price
                    )
                    if a.symbol in crypto_symbols:
                        # Alpaca uses different symbol forms per endpoint:
                        # positions are 'BTCUSD', orders are 'BTC/USD'. Sending
                        # the position form to submit_order is rejected.
                        order_sym = crypto_order_symbols.get(a.symbol)
                        if not order_sym:
                            raise ValueError(
                                f"no order symbol mapped for crypto {a.symbol}")
                        qty = round(abs(delta) / price, 6)
                        if qty > 0:
                            broker.submit_crypto_market_order(order_sym, qty, "sell")
                            print(f"      ✅ sell {qty} {order_sym} submitted")
                            acted += 1
                        else:
                            print("      (dust — no action)")
                    else:
                        qty = abs(delta) / price
                        if qty > 0:
                            broker.submit_market_order(
                                a.symbol, qty, "sell", reference_price=price,
                                sleeve="risk_guard",
                            )
                            print(f"      ✅ sell {qty} {a.symbol} submitted")
                            acted += 1
                        else:
                            print("      (delta < 1 share — no action)")
                except Exception as exc:  # noqa: BLE001
                    print(f"      ❌ trim failed: {exc}")
                    raise
            else:
                print("      [dry-run] would trim")
                acted += 1
    return acted


def _run() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", required=True, choices=["premarket", "open", "close", "risk"])
    p.add_argument("--inspect", action="store_true", help="read-only plan; no state or orders")
    p.add_argument("--live", action="store_true", help="actually submit paper orders")
    p.add_argument("--no-enrich", action="store_true",
                   help="disable the sentiment/earnings overlay (quant only)")
    p.add_argument("--enable", nargs="*", default=[],
                   help="force-enable sleeves by name (e.g. --enable crypto pead)")
    p.add_argument("--disable", nargs="*", default=[],
                   help="force-disable sleeves by name")
    p.add_argument("--skip-guard", action="store_true",
                   help="skip the risk sweep (diagnostics only — never schedule this)")
    p.add_argument("--wind-down-disabled", action="store_true",
                   help="close positions still held by DISABLED sleeves. Opt-in and "
                        "not scheduled: liquidating is a deliberate decision, and the "
                        "audit is meant to prompt it, not perform it silently.")
    # Safeguard knobs (portfolio-wide defaults; per-sleeve overrides live in
    # trading_bot/ownership.py).
    p.add_argument("--stop-loss", type=float, default=0.08)
    p.add_argument("--daily-loss-limit", type=float, default=0.03)
    p.add_argument("--max-drawdown", type=float, default=0.20)
    p.add_argument("--max-position-weight", type=float, default=0.60)
    p.add_argument("--target-vol", type=float, default=0.10)
    p.add_argument("--max-leverage", type=float, default=1.0)
    args = p.parse_args()
    if args.inspect and args.live:
        p.error("--inspect cannot submit orders")

    enabled = dict(DEFAULT_SLEEVES)
    for name in args.enable:
        enabled[name] = True
    for name in args.disable:
        enabled[name] = False

    broker = capture_broker(AlpacaBroker())
    cycle_id = now_et().date().isoformat() + ":" + args.phase
    if args.phase == "risk":
        cycle_id += ":" + now_et().strftime("%H%M")
    broker.bind_cycle(cycle_id)
    sleeves = build_sleeves(enabled)
    by_name = {s.name: s for s in sleeves}

    # Only PaperExecutor and the crypto sleeve checked market hours; the guard,
    # regime, router and cash-park sleeves would happily submit equity market
    # orders into a closed market, where Alpaca queues them to fill at the next
    # open — unattended, at an unknown price, hours after the signal that
    # produced them. Demote equity trading to planning instead.
    #
    # Crypto is exempt: it trades 24/7 on Alpaca, so there is no queued-overnight
    # risk and no reason to block it on the equity calendar.
    live = args.live
    if live:
        try:
            if not broker.get_clock().is_open:
                print("\n⚠ Equity market is CLOSED — demoting equity orders to DRY-RUN "
                      "(no queued overnight orders). Crypto still trades.")
                live = False
        except Exception as exc:
            raise RuntimeError("market clock unavailable; live cycle failed") from exc
    crypto_live = args.live and enabled.get("crypto", False)
    # Alpaca position symbols ('BTCUSD') differ from its order symbols
    # ('BTC/USD'); keep the mapping so de-risking a coin can actually place.
    crypto_order_symbols = {
        to_alpaca_position_symbol(c): to_alpaca_symbol(c)
        for c in load_crypto_universe()
    }
    broker.enabled_sleeves = enabled
    args.live = live
    if args.phase == "risk" and not live:
        return
    if args.live:
        if args.phase == "risk":
            broker.reconcile_orders(protective=True)
        else:
            broker.reconcile_orders()
    mode = "LIVE (paper)" if live else "DRY-RUN"

    acct = broker.get_account()
    equity = float(acct.portfolio_value)
    raw_positions = broker.get_positions()
    positions = {
        p.symbol: {
            "market_value": float(p.market_value),
            "unrealized_plpc": float(p.unrealized_plpc),
        }
        for p in raw_positions
    }

    print(f"\n{'=' * 70}")
    print(f"PORTFOLIO | phase={args.phase} | {mode} | equity ${equity:,.0f}")
    print(f"active sleeves: {', '.join(n for n, v in enabled.items() if v)}")
    print(f"{'=' * 70}")

    # -- 1. DECIDE ---------------------------------------------------------
    # Every decision this cycle makes is computed by trading_bot.cycle.plan_cycle,
    # a pure function with no broker or filesystem access. This split exists
    # because the first version interleaved decisions with order submission
    # inside one 316-line main(), and all six bugs the 2026-08-04 audit found
    # lived in those seams. Everything below is execution of the plan; if you
    # need to change WHAT happens, change the planner and its tests.
    state = ExecutorState.load(require_existing=bool(args.live or raw_positions))
    store = ChampionStore()
    from trading_bot.config import load_config
    capture_inputs(lambda: {"at":now_et().isoformat(),"signal_session":last_completed_session().isoformat(),
        "arguments":vars(args).copy(),"config":load_config(),"initial_state":asdict(state),
        "champions":{s:c.as_dict() for s,c in store.all().items()}})
    plan = plan_cycle(
        phase=args.phase,
        today=now_et().date().isoformat(),
        equity=equity,
        positions=positions,
        state=SessionState(
            peak_equity=state.peak_equity,
            day_start_equity=state.day_start_equity,
            session_date=state.session_date,
            halted_today=state.halted_today,
            killed=state.killed,
            baseline_source=state.baseline_source,
        ),
        sleeves=sleeves,
        limits=RiskLimits(
            max_position_weight=args.max_position_weight,
            stop_loss_pct=args.stop_loss,
            daily_loss_limit_pct=args.daily_loss_limit,
            max_drawdown_pct=args.max_drawdown,
            max_leverage=args.max_leverage,
        ),
        champion_symbols=set(store.all()),
        champion_store_exists=store.path.exists(),
        wind_down_disabled=args.wind_down_disabled,
        skip_guard=args.skip_guard,
        prior_close_equity=float(acct.last_equity),
    )

    # Persist the planner's session bookkeeping immediately — peak_equity must
    # ratchet on every cycle, including ones that place no orders at all.
    state.peak_equity = plan.state.peak_equity
    state.day_start_equity = plan.state.day_start_equity
    state.session_date = plan.state.session_date
    state.halted_today = plan.state.halted_today
    state.killed = plan.state.killed
    state.baseline_source = plan.state.baseline_source
    if args.inspect:
        print(format_audit(plan.audit))
        if plan.guard:
            print(format_guard(plan.guard))
        return
    state.save()

    # -- 2. EXECUTE --------------------------------------------------------
    print("\n--- OWNERSHIP AUDIT ---")
    print(format_audit(plan.audit))
    for w in plan.warnings:
        print(f"  ⚠ {w}")
    if plan.abort_reason:
        raise SystemExit(f"\n❌ ABORT: {plan.abort_reason}\n"
                         "   Fix trading_bot/ownership.py before trading.")
    if plan.audit.disabled_holdings and not args.wind_down_disabled:
        print("    → run with --wind-down-disabled to close the disabled-sleeve holdings")

    if plan.wind_down:
        print("\n  Winding down positions with no active manager:")
        crypto_syms = by_name["crypto"].symbols
        for sym, val in sorted(plan.wind_down.items(), key=lambda x: -abs(x[1])):
            # Crypto closes on the 24/7 venue; equities wait for the open.
            can_trade = crypto_live if sym in crypto_syms else args.live
            print(f"    → close {sym} (${val:,.0f})")
            if can_trade:
                try:
                    broker.close_position(sym, sleeve="wind_down")
                    print("      ✅ close submitted")
                except Exception as exc:  # noqa: BLE001
                    print(f"      ❌ close failed: {exc}")
                    raise
            else:
                print("      [dry-run] would close (equity market closed)")
    elif plan.audit.unmanaged:
        print(f"    ({len(plan.audit.unmanaged)} position(s) to close — "
              "happens at the OPEN phase)")

    if plan.guard is not None:
        print("\n--- RISK GUARD ---")
        print(format_guard(plan.guard))
        if args.phase in {"open", "risk"}:
            _apply_guard_actions(
                broker, plan.guard, args.live,
                crypto_symbols=by_name["crypto"].symbols,
                crypto_live=crypto_live,
                crypto_order_symbols=crypto_order_symbols,
            )
        elif plan.guard.actions:
            print("  (risk actions apply at the OPEN phase — none submitted now)")

        if plan.guard.kill_switch:
            print("\n🛑 KILL SWITCH — protection completed; expansion remains disabled.")
            return

    # -- 3. sleeves --------------------------------------------------------
    if args.phase == "risk":
        if not settle_orders(broker, args.live):
            raise RuntimeError("risk-reducing orders remain unresolved")
        return
    # The gate includes ETF orders. No executor may race an active guard sell.
    if args.phase == "open" and not plan.run_sleeves:
        if not settle_orders(broker, args.live):
            raise RuntimeError("guard/wind-down execution incomplete")
        print(f"Sleeves deferred: {plan.skip_reason}")
        return
    from trading_bot.allocations import read_allocations
    _alloc_file = read_allocations(enabled_sleeves=enabled, require_fresh=args.live)
    portfolio_policy = load_policy()
    capture_inputs(lambda: {"allocations":_alloc_file,"policy":portfolio_policy})
    if portfolio_policy.get("status") == "INVALID_FALLBACK":
        raise RuntimeError("invalid portfolio policy; refusing default risk allocation")
    broker.bil_reserve = float(portfolio_policy["config"]["bil_buffer"])
    broker.max_leverage = args.max_leverage
    if args.phase == "open" and args.live:
        snapshot_path = DATA / "production_inputs" / (state.session_date + ".json")
        if not snapshot_path.exists():
            atomic_json(snapshot_path, {
                "at": iso_now(), "signal_session": last_completed_session().isoformat(),
                "cash": float(acct.cash), "last_equity": float(acct.last_equity),
                "state": asdict(state), "allocations": _alloc_file, "policy": portfolio_policy,
                "champions": {s: c.as_dict() for s,c in store.all().items()},
                "positions": {p.symbol: {"qty": str(p.qty), "avg_entry": str(p.avg_entry_price),
                              "price": float(p.current_price)} for p in raw_positions},
                "arguments": vars(args),
            })
    # The ETF sleeve owns the premarket/open/close phase semantics and the
    # session bookkeeping, so it runs on every phase. The rest are daily
    # rebalancers and only act at the open.
    if enabled.get("etf"):
        print("\n--- ETF SLEEVE ---")
        store = ChampionStore()
        strategies = {}
        for sym in load_universe():
            champ = store.get(sym)
            if not champ:
                print(f"{sym}: no champion (DSR gate not cleared) — skipping")
                continue
            print(f"{sym}: champion {champ.strategy} {champ.params} "
                  f"(judge Sharpe {champ.judge_sharpe:.2f})")
            strategies[sym] = VolatilityTargeted(
                REGISTRY[champ.strategy](**champ.params),
                target_ann_vol=args.target_vol, max_leverage=args.max_leverage,
            )
        if strategies:
            etf_sleeve = by_name["etf"]
            PaperExecutor(
                strategies,
                broker=broker,
                risk_manager=RiskManager(RiskLimits(
                    max_position_weight=etf_sleeve.max_position_weight or 0.60,
                    stop_loss_pct=etf_sleeve.stop_loss_pct or args.stop_loss,
                    daily_loss_limit_pct=args.daily_loss_limit,
                    max_drawdown_pct=args.max_drawdown,
                    max_leverage=args.max_leverage,
                )),
                overlay=None if args.no_enrich else SignalOverlay(OverlayConfig()),
                live=args.live,
            ).run_phase(args.phase)
            if not settle_orders(broker, args.live):
                raise RuntimeError("ETF orders unresolved; later sleeves blocked")
        else:
            print("No champions deployed — ETF sleeve idle.")

    # Whether the daily sleeves run is the planner's decision, not re-derived
    # here — that duplication is how the guard and the sleeves ended up both
    # selling the same $11,386 of QLD.
    if not plan.run_sleeves:
        print(f"\n⏸  Sleeves skipped — {plan.skip_reason}")
        return

    # Allocation is clamped to the sleeve's ownership cap. Without this the
    # regime sleeve and guard must share the same configured target or they can
    # enter a buy/trim loop. The allocation module owns the production target;
    # the ownership registry mirrors it as a hard ceiling.
    #
    # Clamp against max_sleeve_weight, NOT max_position_weight: these are
    # sleeve-wide allocations, and for a multi-instrument sleeve the per-symbol
    # cap is much smaller (crypto: 5% per coin, 20% for the sleeve).
    def _alloc(name: str, configured: float) -> float:
        """Deliberate risk budget, modulated by sleeve health.

        allocations.LIVE_SLEEVE_TARGETS owns HOW MUCH RISK WE WANT;
        sleeve_allocations.json owns IS THIS SLEEVE HEALTHY. Historically those
        two sources diverged: on
        2026-08-09 update_allocations rewrote regime from the 70% chosen for a
        QQQ-level return target back to its 0.3333 baseline — an equal split
        across regime/PEAD/crypto, a three-sleeve world that no longer exists
        (PEAD retired, crypto disabled). The verdict was HEALTHY, so nothing was
        wrong; the number was simply computed for a different portfolio. QLD
        fell 39.6% -> 23.6% and Nasdaq exposure 84% -> 58% without a decision
        being made.

        The cap remains a final consistency guard, and the adaptive policy may
        only REDUCE the configured target.
        """
        cap = by_name[name].max_sleeve_weight
        if cap is None:
            return configured
        verdict = str(verdicts.get(name, "HEALTHY")).upper()
        from trading_bot.allocations import effective_target
        target = effective_target(configured, cap, verdict)
        if verdict in ("TRIPPED", "FAST_TRIPPED", "DISABLED"):
            print(f"  ⚠ {name}: verdict {verdict} → allocation 0%")
            return target
        if verdict == "DECAYING":
            # DECAYING is the warning phase of the documented six-week rule,
            # not a separate sizing rule. Halving after the first weak reading
            # silently turned the chosen 70% return budget into 35%. Keep the
            # target until the pre-specified TRIPPED threshold actually fires.
            print(f"  ⚠ {name}: verdict DECAYING → warning only; keeping "
                  f"the {min(configured, cap):.0%} budget until its tripwire fires")
            return target
        if abs(configured - cap) > 1e-6:
            print(f"  · {name}: using the {cap:.0%} risk budget "
                  f"(allocations file says {configured:.1%}, verdict {verdict})")
        return target

    weights = _alloc_file.get("weights", {})
    verdicts = _alloc_file.get("verdicts", {})

    def _settle_or_stop(after: str) -> bool:
        """Sleeves must not size themselves off a balance that still has the
        previous sleeve's orders in flight — that is how $206k of buys landed
        on a $100k account. If the book won't settle, stop the cycle rather
        than let the next sleeve over-allocate."""
        if settle_orders(broker, args.live):
            return True
        print(f"\n⛔ Orders from the {after} have not settled — stopping the "
              "cycle here so the next sleeve cannot double-spend the same cash.")
        raise RuntimeError(f"{after}: orders remain unresolved")

    if enabled.get("regime"):
        _run_sleeve("regime sleeve", lambda: RegimeExecutor(
            broker=broker,
            allocation=_alloc("regime", float(weights.get("regime", 0.13))),
            health_verdict=str(verdicts.get("regime", "HEALTHY")),
            live=args.live,
        ).run())
        if not _settle_or_stop("regime sleeve"):
            return

    if enabled.get("pead"):
        _run_sleeve("pead sleeve", lambda: PeadExecutor(
            universe=load_pead_universe(), broker=broker, live=args.live
        ).run())
        if not _settle_or_stop("pead sleeve"):
            return

    if enabled.get("crypto"):
        _run_sleeve("crypto sleeve", lambda: CryptoExecutor(
            symbols=load_crypto_universe(),
            broker=broker,
            capital_allocation=_alloc("crypto", float(weights.get("crypto", 0.10))),
            live=args.live,
        ).run())
        if not _settle_or_stop("crypto sleeve"):
            return

    # Router sizes itself from whatever the active sleeves left idle, so it
    # must run after them — and only once their orders have actually filled.
    if enabled.get("managed_futures"):
        portfolio_policy = load_policy()
        if portfolio_policy.get("status") == "INVALID_FALLBACK":
            raise RuntimeError("invalid portfolio policy; refusing default risk allocation")
        router_config = portfolio_policy["config"]
        _run_sleeve("managed futures / idle router", lambda: ManagedFuturesExecutor(
            broker=broker, live=args.live,
            bil_buffer_pct=float(router_config["bil_buffer"]),
            idle_rule=str(router_config["idle_rule"]),
            ma_window=int(router_config["ma_window"]),
        ).run())
        if not _settle_or_stop("idle router"):
            return

    # Sweeps whatever is still in cash. Always last.
    if enabled.get("cash_park"):
        _run_sleeve("cash park", lambda: CashParkExecutor(
            broker=broker, live=args.live
        ).run())

    if not args.live:
        print(
            "\nNOTE: in dry-run the sleeves' buy totals DO NOT sum to what a live\n"
            "cycle would place. Nothing fills here, so each sleeve sees the same\n"
            "un-spent cash and sizes against it — on an all-cash account the\n"
            "printed orders sum to ~2x equity. Live, settle_orders() waits for each\n"
            "sleeve's fills before the next one reads the balance, and the\n"
            "post-cycle leverage check reports actual gross exposure.\n"
            "This asymmetry is inherent to dry-run; it is not the over-allocation bug."
        )

    # Backstop: if the sleeves somehow still over-allocated, say so loudly
    # rather than discovering 2x leverage from tomorrow's equity curve.
    if args.live:
        try:
            if not settle_orders(broker, args.live):
                raise RuntimeError("post-cycle orders unresolved")
            acct2 = broker.get_account()
            eq2 = float(acct2.portfolio_value)
            gross = sum(abs(float(p.market_value)) for p in broker.get_positions())
            lev = gross / eq2 if eq2 else 0.0
            print(f"\nGross exposure ${gross:,.0f} / equity ${eq2:,.0f} = {lev:.2f}x")
            if lev > args.max_leverage + 0.02:
                print(f"🚨 OVER-DEPLOYED: {lev:.2f}x exceeds the {args.max_leverage:.2f}x "
                      "cap — investigate before the next cycle.")
                raise RuntimeError("post-cycle exposure exceeds the configured cap")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠ post-cycle leverage check failed: {exc}")
            raise

    print(f"\n{'=' * 70}\nPORTFOLIO CYCLE COMPLETE ({mode})\n{'=' * 70}")


def main() -> None:
    # The same lock serializes normal cycles, protection and deployment.
    phase = sys.argv[sys.argv.index("--phase") + 1] if "--phase" in sys.argv else "unknown"
    inspect_only = "--inspect" in sys.argv
    status_path = DATA / "portfolio_cycles.json"
    with execution_lock(wait=180 if phase != "risk" else 0), signal_session(last_completed_session()), \
            capture_cycle(phase == "open" and "--live" in sys.argv and not inspect_only,
                          Path(__file__).resolve().parents[1]):
        if inspect_only:
            _run()
            return
        state = json.loads(status_path.read_text()) if status_path.exists() else {"phases": {}}
        started = iso_now()
        previous = state.setdefault("phases", {}).get(phase, {})
        row = {"phase": phase, "status": "RUNNING", "started_at": started,
               "last_success_at": previous.get("last_success_at")}
        state["phases"][phase] = row
        atomic_json(status_path, state)
        try:
            _run()
        except BaseException as exc:
            row.update(status="FAILED", completed_at=iso_now(), error_type=type(exc).__name__)
            atomic_json(status_path, state)
            append_record(DATA / "portfolio_cycles.jsonl", row)
            raise
        else:
            signals = captured_signals()
            if phase == "open" and "--live" in sys.argv and signals:
                path = DATA / "production_signals" / (now_et().date().isoformat() + ".json")
                if not path.exists():
                    atomic_json(path, {"at": iso_now(), "bars": signals})
            row.update(status="COMPLETED", completed_at=iso_now(), last_success_at=iso_now())
            atomic_json(status_path, state)
            append_record(DATA / "portfolio_cycles.jsonl", row)


if __name__ == "__main__":
    try:
        main()
    except ExecutionBusy:
        if "risk" not in sys.argv:
            raise
        print("Protection tick skipped: another portfolio cycle owns the lock.")
