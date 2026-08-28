# Strategy reset — rehearsal postmortem and falsifiable hypotheses

**Recorded:** 2026-08-28
**New measured window:** 2026-08-31 09:30 ET to 2026-09-04 09:30 ET

This is the evidence record behind the strategy summarized in `README.md` and
`HANDOVER.md`. It separates
observations from proposed strategy so the plan cannot quietly turn assumptions
into backtest results.

## Observed in the first paper rehearsal

The old account opened five underlyings across seven registered structures:

| Underlying | Contracts | Approx. early unrealized P&L |
|---|---:|---:|
| AAPL | 61 | -$1,159 |
| TSLA | 28 | -$1,036 |
| IWM | 87 | -$694 |
| QQQ | 30 | -$300 |
| SPY | 1 | -$5 |

Exact portfolio maximum risk was $67,728 and entry credit was $41,572. Early
equity was approximately $96.5k–$96.8k. Values are time-specific paper marks,
not final realized P&L.

Parent entry orders:

| Underlying | Qty | Limit | Average fill | Terminal time |
|---|---:|---:|---:|---:|
| AAPL | 61 | -2.02 | -2.06 | 1.0s |
| TSLA | 28 | -3.57 | -3.63 | 13.8s |
| QQQ attempt 1 | 31 | -3.32 | canceled | 61.1s |
| IWM | 84 | -1.06 | -1.08 | 0.04s |
| QQQ attempt 2 | 31 | -3.01 | canceled | 61.2s |
| IWM add | 1 | -1.02 | -1.08 | 0.08s |
| QQQ attempt 3 | 30 | -3.07 | canceled | 60.8s |
| IWM add | 2 | -0.99 | -1.09 | 0.13s |
| QQQ attempt 4 | 30 | -3.04 | -3.08 | 12.3s |
| SPY | 1 | -1.97 | -2.04 | 0.16s |

The three QQQ attempts were confirmed canceled before the next structure was
submitted. No unknown order state or duplicate fill was found.

## Root causes

1. **Objective mismatch.** Short convexity has a capped frequent reward and a
   large infrequent loss. A short leaderboard window rewards a material right
   tail more than small repeatable carry.
2. **Incomplete scorer.** `plan_session` sorted solely by descending
   credit-to-risk and then greedily consumed per-name and total risk budgets.
3. **False diversification.** Per-ticker caps did not measure shared market,
   volatility, technology, or macro factors.
4. **No execution-quality price.** QQQ non-fills did not lower its future rank;
   quote width, depth, fill latency, and mark quality were absent from the score.
5. **Excess initial commitment.** Most of the account's allowed loss was
   committed in one entry wave, leaving little ability to learn or respond.
6. **AI was cosmetic.** Featherless narrated after execution and had no
   measurable decision contribution.
7. **Deadline bug under the new rules.** A 09:30 cutoff gave the legacy helper a
   09:45 flatten. The helper now chooses the prior liquid session.

## Things that did work

- Account pinning and option-level verification.
- Exact maximum-loss arithmetic.
- Marketable multi-leg execution and favorable paper fills on filled orders.
- Cancel confirmation before retrying.
- Client-order-ID recovery, registry reconciliation, and broker-leg checks.
- Missing-quote and unknown-state fail-closed behavior.
- The scheduler completed cleanly before it was deliberately disabled.

Those mechanics should be reused; the condor thesis and greedy allocation
should not.

## Alpaca MCP observations for v2

- The installed server is v3.4.7 and reports 72 tools.
- `get_news` returns timestamped article IDs, URLs, symbols, headlines, summaries,
  optional content, and pagination.
- `get_market_movers` without eligibility filtering is dominated by penny
  stocks, warrants, and non-optionable names. Movers are discovery input, not a
  tradable universe.
- `get_most_active_stocks` has the same issue; optionability, price, dollar
  volume, spread, and quote freshness must be checked independently.
- `get_stock_bars` returns a `next_page_token`, but the installed MCP input
  schema does not accept a page token. The replay scripts therefore request
  small, non-overlapping date chunks and deduplicate timestamps. This is a
  reproducibility constraint, not permission to silently drop later pages.
- Paper trading uses marketability against NBBO but does not model queue
  position, market impact, latency slippage, or displayed-size constraints.
  Paper results must not be described as production fill evidence.

## Featherless observations for v2

A four-model free-form design probe was run through the authenticated API:

- one model produced generic RSI/Bollinger/HFT suggestions;
- two consumed the output budget in reasoning and returned no valid final body;
- one returned a capacity-exhausted error.

This rejects a single free-form "strategy committee" as a production design.
The v2 requirement is typed extraction, source-fact grounding, short bounded
prompts, schema validation, timeouts, model-specific fallback, disagreement as
uncertainty, and no valid quorum = no trade.

That requirement is now implemented in the typed router. A real three-model
probe returned three valid, directionally agreeing assessments in 14.51 seconds.
Two outputs contained invalid self-links; the router removed only those links
under a narrow, audited repair rule and recorded the repairs. An earlier probe
showed that a nominal 60-second HTTP timeout could consume 97.6 seconds, so each
model now runs in a killable worker under a true wall-clock deadline. The probe
ticker itself was intentionally not proof of a tradable candidate; production
eligibility remains a deterministic pre-model gate.

## Research decision ledger — 2026-08-28

Every row below is retained whether the idea helped or failed. Metrics are
outputs of the named replay scripts using Alpaca MCP news/bars unless explicitly
described as a feasibility calculation.

| Hypothesis | Predeclared implementation | Result | Decision |
|---|---|---|---|
| Broad direct-news continuation | Directional keyword screen, then 30/60/120/240-minute return relative to SPY. | Only 3 usable events across 2 symbols; 60-minute aligned excess mean +0.461%, but 120-minute mean +0.018% and win rate 33.3%. | Reject for activation: sample and labels are too sparse/noisy. |
| NFP-to-crypto bridge | On an NFP miss of at least 50k, require BTC confirmation from 08:29–08:32 and hold to 09:25; charge 30 bp. | 3 qualifying releases; mean -0.028%, compounded -0.086%, win rate 33.3%. | Reject the naive bridge. |
| AVGO earnings continuation | If next-open AVGO gap is at least 3%, follow its sign. | 8 qualifying events; aligned mean -0.594% to +2 hours and +0.252% to close. | Reject intraday continuation; close result is too small and inconsistent for options cost. |
| AVGO peer spillover | Follow same-direction laggards among NVDA, AMD, MRVL, and SMH after an AVGO gap. | Selected-lagger mean -0.267% to +2 hours and -0.637% to close. | Reject naive spillover. |
| ISM reaction continuation | Follow the 09:59–10:03 SPY move when magnitude is at least 10 bp. | 14 events; continuation mean -0.088% to 10:30 and +0.055% to 11:00. | Reject as a primary edge. |
| ISM short-horizon fade | Fade the same qualified SPY move to 10:30. | Mean +0.088%, median +0.111%, win rate 64.3%. | Observe only: likely consumed by option spreads and too small for tournament P&L. |
| AVGO scheduled-event convexity | Buy the Sep 4 ATM straddle before Sep 2 earnings only if live premium/liquidity and exact-risk gates pass; exit next day at 09:45. | Eight post-split historical ATM straddles from actual Alpaca MCP option trade bars: last-trade proxy mean +44.29%, median +28.49%, win rate 62.5%; deliberately adverse envelope mean +18.06%. | Activate conditionally under a frozen premium ≤8.5% of spot plus liquidity/freshness gates. Historical trades are stronger evidence than the preliminary current-premium calculation, but still not NBBO fills. |

### Direct-news continuation replay

`replay_direct_continuation.py` read 2,316 news rows from Jul 15 through Aug 27.
A conservative headline/summary keyword screen found only three qualifying
events across two symbols. Direction-aligned excess return over SPY was:

| Horizon | n | Mean | Median | Win rate |
|---|---:|---:|---:|---:|
| 30 minutes | 3 | +0.262% | +0.217% | 66.7% |
| 60 minutes | 3 | +0.461% | +0.333% | 66.7% |
| 120 minutes | 3 | +0.018% | -0.001% | 33.3% |
| 240 minutes | 1 | +0.601% | +0.601% | 100.0% |

One association was an Amazon-related FDA recall and another headline was
retrospective earnings language. The exercise demonstrates why typed factual
interpretation is useful; it does not demonstrate an edge.

### NFP-to-crypto bridge replay

`replay_nfp_crypto_bridge.py` paired official release dates, Alpaca macro-news
headlines, and BTC one-minute bars. The rule was fixed before inspection: long
BTC only when reported payrolls missed consensus by at least 50k and BTC rose
from 08:29 to 08:32 ET; exit at 09:25 and charge 30 bp. Qualifying outcomes were
-0.279% on 2025-09-05, +0.424% on 2026-07-02, and -0.230% on 2026-08-07.
The bridge was creative but did not survive its first costed falsification.

### AVGO continuation and peer-spillover replay

`replay_avgo_spillover.py` evaluated ten official AVGO post-earnings sessions
from 2024–2026 using AVGO, NVDA, AMD, MRVL, and SMH IEX bars. AVGO next-open
gaps were -2.88%, +15.19%, -9.62%, +20.48%, +6.13%, -2.20%, +12.33%, -9.26%,
+3.86%, and -15.52%. Eight met the 3% gate.

| Policy | Horizon | n | Mean | Median | Win rate |
|---|---|---:|---:|---:|---:|
| Follow AVGO gap | +2 hours | 8 | -0.594% | -1.284% | 37.5% |
| Follow AVGO gap | Close | 8 | +0.252% | +0.132% | 62.5% |
| Follow all peers | +2 hours | — | -0.491% | — | — |
| Follow all peers | Close | — | -0.637% | — | — |
| Follow selected laggards | +2 hours | — | -0.267% | — | — |
| Follow selected laggards | Close | — | -0.637% | — | — |

The results reject the original claim that semantic peers predictably catch up
after the open. They weakly suggest a short-horizon gap fade, but that was not a
predeclared policy and has not been shown to cover option costs.

### ISM reaction replay

`replay_ism_reaction.py` constructed 34 scheduled releases from the official
calendar rule and parsed Alpaca headlines against SPY one-minute bars. Fourteen
events passed the predeclared absolute 10 bp confirmation gate from 09:59 to
10:03 ET. Continuation returned -0.088% mean / -0.111% median to 10:30 and
+0.055% mean / -0.004% median to 11:00. The corresponding 10:30 fade returned
+0.088% mean / +0.111% median with a 64.3% win rate; the 11:00 fade returned
-0.055% with a 50% win rate. The only positive pattern is too small to activate
without costed option evidence.

### AVGO scheduled-event convexity — preliminary feasibility

Broadcom has announced Q3 FY2026 results after the close on Wed Sep 2. At the
time of this snapshot, AVGO traded near $366.52 and indicative Sep 4 near-ATM
straddles cost approximately $29.57–$30.57, or 8.1–8.3% of spot, with implied
volatility near 71–74%. The 365 straddle was approximately $28.92 bid / $29.90
ask.

As a deliberately conservative feasibility check, the current $29.90 ask was
applied to each of the ten historical absolute post-earnings gaps and valued at
next-open intrinsic only. It ignores positive remaining time value, but it also
is **not** a historical option quote/fill reconstruction and does not model exit
spread or paper marking. Returns on premium were -69.8%, +91.3%, +12.8%,
+156.1%, -19.8%, -78.1%, +56.2%, +8.4%, -47.6%, and +85.2%: mean +19.48%,
median +10.63%, win rate 60%, worst -78.1%, best +156.1%.

At a 20% account maximum-loss allocation, those illustrative account effects
were mean +3.9%, median +2.1%, worst -15.6%, and best +31.2%. This preliminary
calculation motivated the stronger historical-option replay below; it is no
longer the primary evidence used to size or activate the strategy.

### AVGO historical straddle trade-bar replay

`replay_avgo_straddles.py` uses Alpaca MCP's expired option-contract catalogue
and historical five-minute option trade bars. For each post-split event it:

1. reads the adjusted AVGO 15:55 event-day stock price;
2. selects the closest common-strike 100-share call and put expiring that week;
3. reads both legs' 15:30–15:55 entry window and 09:40–09:50 next-day exit
   window;
4. reports a last-trade proxy and a deliberately adverse envelope that buys
   each leg at its highest entry-window trade and sells it at its lowest
   exit-window trade.

| Event | Spot | Strike | Entry premium/spot | Last-trade return | Adverse envelope |
|---|---:|---:|---:|---:|---:|
| 2024-09-05 | $150.25 | $150.0 | 6.20% | +30.15% | -6.36% |
| 2024-12-12 | $178.20 | $177.5 | 7.60% | +216.69% | +154.52% |
| 2025-03-06 | $177.14 | $177.5 | 9.31% | -9.82% | -35.69% |
| 2025-06-05 | $257.57 | $257.5 | 6.63% | -66.86% | -75.57% |
| 2025-09-04 | $303.87 | $305.0 | 5.74% | +111.92% | +100.06% |
| 2025-12-11 | $404.72 | $405.0 | 6.65% | +26.83% | +5.73% |
| 2026-03-04 | $315.94 | $315.0 | 7.74% | -28.21% | -50.60% |
| 2026-06-03 | $477.86 | $477.5 | 8.63% | +73.62% | +52.40% |

Across all eight, the last-trade proxy is +44.29% mean / +28.49% median,
62.5% wins, -66.86% worst, and +216.69% best. The adverse envelope is +18.06%
mean / -0.31% median, 50% wins, -75.57% worst, and +154.52% best.

The frozen 8.5%-of-spot premium gate excludes the 9.31% and 8.63% events. On
the remaining six, the last-trade proxy is +48.42% mean, +28.49% median, 66.7%
wins, -66.86% worst, and +216.69% best. The adverse envelope is +21.30% mean,
-0.31% median, 50% wins, -75.57% worst, and +154.52% best.

At the frozen 25% account maximum-loss ceiling, the gated last-trade sample
maps illustratively to +12.1% mean account P&L, +7.1% median, -16.7% worst, and
+54.2% best. The gated adverse envelope maps to +5.3% mean, approximately flat
median, -18.9% worst, and +38.6% best. Exact absolute loss remains capped at
25% if both legs expire worthless.

Limitations remain material: only eight post-split events; historical option
trades rather than quotes/NBBO; call and put trades need not share an instant;
no fees; and a current indicative paper surface may mark differently. The
adverse envelope intentionally overstates timing cost but does not substitute
for a true historical marketable-quote replay.

## Remaining hypotheses before activation

### H1 — narrowly typed direct catalyst continuation

For a liquid optionable ticker, a high-novelty directional article followed by
abnormal volume and persistent same-direction trading has a post-confirmation
return large enough to cover a marketable debit-spread round trip.

The broad keyword version is rejected. A narrower typed version remains shadow
only until news+tape outperforms tape-only on held-out timestamped events.

### H2 — causal spillover

When a primary ticker reprices on a catalyst, a fact-supported liquid secondary
ticker sometimes reprices with a delay, creating a better executable option
payoff than chasing the primary.

The naive AVGO peer version is rejected. Do not activate another version unless
secondary relationships beat sector/beta-matched random peers after costs on
held-out events.

### H3 — surface lag is useful

Among otherwise similar confirmed catalysts, option structures with lower
measured repricing relative to the stock move have better subsequent payoff
than already-expanded surfaces.

Reject if an IV/skew/surface-lag feature does not improve held-out candidate
ranking after conservative spreads.

### H4 — model disagreement contains information

Candidates on which the Featherless roles agree and cite the same supplied facts
have better calibration than candidates with high directional or causal-link
disagreement.

Reject if disagreement has no relationship with calibration; do not retain a
committee for presentation value alone.

### H5 — convex bounded structures fit the tournament

Given comparable expected value after costs, debit verticals/butterflies produce
a more useful four-day right tail than large short-condor allocation while
keeping maximum loss exact.

Reject or narrow if achievable spread cost consumes the modeled advantage.

### H6 — scheduled-event implied distribution is mispriced

A predeclared long AVGO straddle may produce a more useful bounded right tail
than trying to predict the earnings direction. Featherless should parse the
post-release surprise vector and invalidations; deterministic code should own
the live surface gate, quantity, order, and exit.

Reject if the live marketable premium exceeds the frozen threshold, liquidity
fails, open-position cutoff marking is unacceptable, or better historical
option evidence removes the apparent advantage.

## Activation evidence table

| Gate | Required evidence | Current status |
|---|---|---|
| Historical timestamp integrity | Bars use only observations after the recorded article timestamp. | Implemented in four replay scripts; broader samples still needed. |
| Direct continuation ablation | News+tape versus tape-only on held-out events. | Broad keyword version rejected; typed version shadow-only. |
| Spillover placebo | Causal peer versus beta/sector matched random peers. | Naive AVGO laggard version rejected. |
| Option feasibility | Conservative executable spread and mark assumptions. | Eight-event MCP historical option-trade replay passed conditionally; current live marketable surface still gates entry. Historical NBBO fills remain unavailable. |
| Featherless reliability | Valid-output, latency, disagreement, and failure rates by model. | Typed router passed 3/3 real probe in 14.51s; prolonged shadow statistics pending. |
| Account isolation | New account exactly $100k, flat, paper, correct options level. | Waiting for replacement account |
| Deadline drill | Evidence-matched 09:45 exit and emergency prior-session flatten. | Pure lifecycle boundaries and mismatch-does-not-block-exit runner behavior pass; replacement-account drill pending. |
| Autonomous restart | No duplicate orders after process kill/restart. | Stable entry client ID, pre-order intent, recovery path, and tests implemented; broker-backed v2 drill pending. |

No pending row is evidence of an edge. It is a build/research obligation.

## Reproducibility and final reporting

The replay programs in this directory are the executable record:

```text
replay_direct_continuation.py
replay_nfp_crypto_bridge.py
replay_avgo_spillover.py
replay_avgo_straddles.py
replay_ism_reaction.py
```

The final result narrative must report all attempted strategies, including
rejected ones, before presenting the final policy. It must distinguish observed
paper fills, stock/crypto event studies, historical option-trade-bar proxies,
current-surface feasibility calculations, and actual measured-window P&L. No
number may move between those evidence classes without being relabeled.
