# Catalyst Surface Agent weekly pipeline

Status: both phases are implemented and rehearsed in read-only mode. For the
measured week, the intelligence phase produced a sealed AVGO plan and the
audited execution phase runs that plan without strategy drift.

## What it does

Each week the same agent can autonomously:

1. Scan the declared 64-name liquid universe for earnings in the active window.
2. Require Yahoo Finance and Nasdaq to agree on ticker, date, and before-open or
   after-close session. One provider cannot manufacture a quorum; any conflict
   fails closed.
3. Use the Alpaca MCP exchange calendar and listed contracts to select the
   actual entry session, next-session exit, and first viable expiry. Weekends
   and market holidays are not inferred from weekdays.
4. Add Alpaca MCP news and ask a three-model Featherless committee to classify
   the supplied facts. The committee can veto an event, but cannot choose a
   structure, size it, or authorize an order.
5. Pull prior earnings dates automatically, find the then-ATM expired call and
   put through Alpaca MCP, and replay the 15:30–15:55 entry against the
   09:40–09:50 next-session exit. Both last-trade and deliberately adverse
   entry-high/exit-low envelopes are retained.
6. Apply one frozen promotion policy: at least six complete events, positive
   mean and median for both execution proxies, minimum win rates, a schedulable
   pre-deadline exit, and semantic/calendar quorum. At entry it additionally
   requires premium/spot at most 8.5%, combined width at most 5%, fresh
   synchronized quotes, displayed size, and a near-ATM common strike.
7. Allocate exact maximum-loss dollars across overlapping promoted events. A
   sole qualifying event may use 25% of equity; multiple events share the 25%
   aggregate budget with a 12.5% per-event cap, weighted by the weakest replay
   statistic.
8. Manage every event on its own clock with stable client order IDs, durable
   book reconciliation, timeout recovery, next-session exit, and global
   deadline flatten. A stale plan is automatically rolled by whole weeks while
   preserving its cutoff convention.

The weekend planning surface is diagnostic, because a closed-market width is
not an executable entry quote. It never waives the surface rule: the executor
reruns the complete live surface and promotion policy inside the entry window.

## Commands

Build a measured-window read-only plan:

```bash
uv run python scripts/build_weekly_event_plan.py \
  --start 2026-08-31T09:30:00-04:00 \
  --deadline 2026-09-04T09:30:00-04:00
```

Run one shadow cycle:

```bash
uv run python scripts/run_weekly_event_agent.py --no-auto-plan
```

Run all deterministic drills plus a real read-only Alpaca/Featherless cycle:

```bash
uv run python scripts/run_weekly_event_rehearsal.py --real
```

The execution phase requires both `--enable-orders` and the separate
gitignored environment value `WEEKLY_EVENT_ENABLE_ORDERS=YES`. The live and
shadow launchd templates share one supervisor label so they cannot run in
parallel. The measured AVGO plan continues through its already rehearsed
runtime; the plan-driven supervisor is the same lifecycle used for subsequent
weekly plans.

## Evidence boundary

- Calendar sources identify a scheduled session; they do not prove an options
  edge.
- Historical Alpaca option trades are not historical quotes, NBBO, or fills.
- Featherless classifies supplied evidence and is non-expansive by construction.
- A prequalified plan has `order_enabled=false`, is SHA-256 sealed, and carries
  no order authority. Editing it invalidates the digest.
- A strategy passing these rules can still lose money. The rules bound loss and
  reduce discretionary overfitting; they do not guarantee P&L.

## Files

```text
src/trading_bot/tournament/event_calendar.py  source-normalized discovery
src/trading_bot/tournament/event_replay.py    generic MCP option replay
src/trading_bot/tournament/weekly.py          schedule/promotion/allocation policy
src/trading_bot/tournament/weekly_plan.py     sealed plan and rollover boundary
scripts/build_weekly_event_plan.py            autonomous read-only planner
scripts/run_weekly_event_agent.py             one-minute multi-event executor
scripts/run_weekly_event_rehearsal.py         drills and real shadow rehearsal
```
