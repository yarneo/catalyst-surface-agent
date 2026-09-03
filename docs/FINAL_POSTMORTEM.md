# Measured deployment postmortem

## Executive result

The autonomous deployment completed its intended order lifecycle and lost
money. It entered 13 AVGO Sep 4 $367.50 long straddles at a combined $29.60
debit, deployed $38,480, and exited the next session at 09:45:18 ET for a $21.37
credit. Gross trade P&L was -$10,699. Final paper-account equity was $89,299.30:
**-$10,700.70, or -10.7007%**. The broker was verified flat.

The correct conclusion is neither “the system worked” nor “one loss disproves
the strategy.” Execution and bounded-risk controls largely worked; the strength
of the alpha evidence and the allocation derived from it were inadequate.

## Before the run

The selected historical replay used eight post-split AVGO earnings events. Its
next-morning option-trade-bar proxy showed +44.29% mean, +28.49% median, and
62.5% wins. Six events passed the frozen premium gate and showed +48.42% mean
with four winners. That evidence was reproducible and honestly labeled, but it
had four major limitations:

1. Eight observations cannot estimate a fat-tailed event distribution tightly.
2. Separate leg trade bars are not simultaneous executable NBBO fills.
3. The 8.5%-of-spot cap was not independently validated out of sample.
4. Sizing near the sample's empirical log-growth optimum treated a noisy point
   estimate as more reliable than it was.

## What happened

The realized entry premium was approximately 8.05% of spot. It passed the 8.5%
ceiling, yet it exceeded every premium in the six-event accepted historical
sample, whose maximum was 7.74%. The live rule therefore allowed an observation
outside the region directly supported by its accepted examples. AVGO's
next-morning option value did not cover that debit; the structure lost 27.80%,
which translated to a 10.70% account loss at 38.48% capital deployment.

The fixed exit was still the right ex-ante discipline. Waiting for a hoped-for
rebound after the measurement catalyst would have converted a scheduled-event
thesis into an untested discretionary bet while rapidly decaying options
remained open.

## Operational timeline and defects

- The one-minute supervisor stayed active through entry, hold, and exit.
- A market-data timestamp arrived a few seconds ahead of the host clock. A
  bounded 10-second skew allowance fixed the false stale-data rejection without
  weakening the 90-second freshness rule.
- A cached Featherless result could return without the typed committee object
  during the only live entry attempt. Entry now forces a fresh typed quorum;
  missing quorum remains a veto.
- Completed but unfilled exit attempts originally reused a client order ID,
  which could replay a terminal result. New attempts now receive new IDs, while
  only genuinely unknown/stuck orders retain an ID for safe recovery.
- The dashboard emphasized an old closed-market diagnostic after the position
  was live. Broker truth now leads, caches are short, and the immutable final
  result remains available without credentials.
- After the clean exit, entry recovery recreated a phantom local position from
  the original filled entry order. Close-only order intent prevented new or
  reversed broker exposure, but supervisor cycles kept trying unnecessary
  exits. A closed event row is now terminal for recovery, and a regression test
  proves it cannot be resurrected.

At 16:57 ET the repaired supervisor reported `DONE`, final equity $89,299.30,
and zero open positions.

## What worked

- Risk was finite and known before entry; no naked or leveraged-loss structure
  could escape its premium-paid maximum.
- Featherless remained non-expansive. It could veto risk but could not alter the
  contract, quantity, price ceiling, or deadline.
- Alpaca MCP covered research, live truth, execution, reconciliation, and result
  measurement instead of serving as a thin order endpoint.
- Entry and exit were autonomous and the scheduled exit filled within 18
  seconds of its target.
- Broker-safe close intent prevented a software-state defect from creating a
  new position after exit.
- Losing research ideas and live faults remained visible and reproducible.

## Version 2 decision rule

The next version should not replace 8.5% with another hand-picked threshold.
For each candidate it should estimate a distribution of next-session
liquidation value using walk-forward or leave-one-event-out evidence, then trade
only when a conservative lower confidence bound exceeds the current marketable
debit plus a spread and slippage reserve.

Sizing should be monotone in that value margin and inverse to uncertainty:

- zero allocation when the conservative edge is non-positive;
- smaller allocation when live premium is near or beyond the historical range;
- a hard portfolio cap that does not depend on model confidence;
- capped convex structures when the thesis is a moderate ±4–5% move;
- an explicit no-trade outcome when neither uncapped nor capped convexity clears
  the executable-value test.

The software state machine should also make terminality explicit: once an event
has a reconciled close, historical entry recovery cannot act on it. All recovery
paths must be tested from broker truth, not only local state.

## Final assessment

The measured alpha result was poor, but the project is still worth presenting.
The differentiator is a real end-to-end autonomous system that can explain its
selection funnel, constrain AI authority, execute with exact risk, preserve a
truthful failure trail, and improve from observed defects. The submission must
lead with the loss, avoid claims of validated profitability, and demonstrate
why the next hypothesis is stricter and falsifiable.

