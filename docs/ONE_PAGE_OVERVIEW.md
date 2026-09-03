# Catalyst Surface Agent

**Yar + Starboi · autonomous scheduled-event options research**

Catalyst Surface Agent is a paper-trading system that decides whether a known
market event offers enough option value to justify a bounded-risk trade. It does
not ask a language model to guess the stock direction. It combines semantic AI
with deterministic market checks, exact-risk sizing, autonomous execution, and
an evidence trail that records both successes and failures.

## What it built

One reusable weekly engine scans a fixed liquid universe, verifies scheduled
events through independent sources, replays each candidate's historical options,
and seals a plan before measurement begins. The one-minute execution loop then
rechecks fresh option quotes, liquidity, premium, account state, and event
integrity. If every frozen condition passes, it submits an idempotent multi-leg
order, reconciles the broker position, and exits on a fixed deadline.

Featherless runs as a typed, source-grounded committee. Its authority is
deliberately asymmetric: it may veto an entry when the event has leaked,
changed, or already resolved; it cannot invent a ticker, enlarge size, weaken a
price limit, place an order, or delay the exit. Alpaca MCP provides the market
and account truth across the lifecycle: calendars/news, stock and option data,
historical option bars, account state, orders, positions, activities, and
portfolio outcomes.

## The measured deployment

The discovery funnel narrowed 64 names to 31 measurable surfaces, nine event-like
term structures, six independently dated candidates, and one plan that survived
the frozen replay and operating rules: a near-ATM AVGO long straddle around its
scheduled earnings release.

The agent autonomously bought 13 Sep 4 $367.50 straddles at a combined $29.60
debit, deploying $38,480 (38.48% of starting equity). It exited at 09:45:18 ET
the next morning at a $21.37 credit. The trade lost $10,699 before minor fees and
adjustments; final paper equity was $89,299.30, a **-10.70% account return**. The
broker was verified flat. This is a losing result, and the project presents it
without relabeling or cherry-picking.

## What the result taught us

The 8.05%-of-spot entry was inside the frozen 8.5% gate, but it was more
expensive than every event in the six-event accepted historical sample. The
rule therefore extrapolated beyond its strongest evidence. The eight-event
replay was also small and used asynchronous option trade bars, not historical
executable NBBO quotes. A positive aggregate average concealed a wide outcome
distribution and could not justify a 40% allocation with confidence.

Version 2 replaces the standalone premium cap with an executable value margin:
trade only when a conservative estimate of next-session liquidation value
exceeds the current marketable debit after spread and model uncertainty. Sizing
will shrink with premium percentile and confidence-bound width. Moderate
expected moves will be evaluated with capped convex structures; otherwise the
correct action is no trade.

The run also exposed operational faults—a bounded market-clock skew, a cached
model-quorum edge case, retry-ID semantics, a stale dashboard, and a post-exit
phantom registry row. Each became a regression test. The scheduled exit itself
filled on time, close-only semantics prevented accidental reverse exposure, and
the supervisor now reaches a terminal `DONE` state with zero open positions.

**Links:** [source and reproducible evidence](https://github.com/yarneo/catalyst-surface-agent) ·
[live result dashboard](https://catalyst-surface-agent.streamlit.app/) ·
[full postmortem](FINAL_POSTMORTEM.md)

