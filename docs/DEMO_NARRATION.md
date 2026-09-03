# Three-minute demo narration

This script assumes a screen recording of the live dashboard, then the public
repository. Read naturally; do not rush the loss or the failure analysis.

## 0:00–0:25 · The claim

**Screen:** Dashboard title and final result.

“This is Catalyst Surface Agent, built by Yar and Starboi. It is an autonomous
options agent for scheduled market events. The core idea is simple: language
models interpret whether an event is still intact, but deterministic code alone
controls price, liquidity, risk, orders, and deadlines. This run lost 10.70
percent. We are showing the exact result because auditability is part of the
product, not a slide added after the fact.”

## 0:25–0:58 · One general engine

**Screen:** Overview, four-stage pipeline, then selection funnel.

“This is not an AVGO-only bot. Before the week, the same engine scanned 64 liquid
names, measured 31 usable option surfaces, found nine event-like term structures,
verified six dated events, and promoted one plan. AVGO is what survived this
week's evidence, not a hard-coded permanent ticker. A later week can select a
different event—or correctly seal an empty plan.”

## 0:58–1:30 · Featherless and Alpaca MCP

**Screen:** Featherless panel, then autonomous loop.

“Featherless runs multiple models concurrently and requires typed, grounded
agreement. The committee can stop a trade if earnings leaked, guidance changed,
or the event already resolved. Crucially, it cannot create a trade, change size,
or waive a gate. Alpaca MCP supplies the entire factual lifecycle: account and
clock, news, stocks, option chains, historical option bars, orders, positions,
activities, and final portfolio equity.”

## 1:30–2:03 · What actually traded

**Screen:** Final measured-result cards.

“Fresh entry checks passed on Wednesday. The agent bought 13 near-the-money
September 4 straddles at the 367.50 strike for a combined debit of 29 dollars and
60 cents. That deployed 38,480 dollars, or 38.48 percent of the account. The
fixed exit began Thursday at 9:45 Eastern and filled 18 seconds later for 21
dollars and 37 cents. Gross trade loss was 10,699 dollars. Final paper equity
was 89,299 dollars and 30 cents, and the broker finished flat.”

## 2:03–2:36 · The useful failure

**Screen:** Hindsight warning and post-exit defect panel.

“The loss revealed a strategy flaw that the headline backtest hid. Entry premium
was 8.05 percent of spot—under our 8.5 percent cap, but more expensive than every
accepted event in the historical sample. We extrapolated beyond direct support,
and we sized too aggressively for only eight observations. The live run also
found clock-skew, model-cache, exit-retry, dashboard, and terminal-state bugs.
Every one is preserved and now covered by a regression test.”

## 2:36–3:00 · Why it matters

**Screen:** Repository evidence and final architecture.

“Version 2 trades only when a conservative estimate of liquidation value clears
the actual marketable debit after spread and uncertainty. Size falls as price
and uncertainty rise; moderate moves use capped convexity or no trade. The
lasting result is not a lucky screenshot. It is a reusable, fail-closed agent
that discovers, decides, executes, reconciles, learns, and can prove exactly
what happened.”

## Recording checklist

- Keep the dashboard result visible long enough to read.
- Show the 64 → 31 → 9 → 6 → 1 funnel and Featherless model-by-model trace.
- Show `evidence/measured_result.json` and the passing test command briefly.
- Do not display browser secrets, account numbers, order IDs, local logs, or
  private runtime files.
- End on the repository URL and dashboard URL.

