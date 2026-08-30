# Catalyst Surface Agent

An autonomous scheduled-event convexity engine for paper-trading research. It
combines typed language-model interpretation with deterministic market-data
verification, exact options risk, idempotent execution, continuous broker
reconciliation, and a tamper-evident result ledger.

[Open the live dashboard](https://catalyst-surface-agent.streamlit.app/)

Catalyst Surface Agent is one reusable agent with two time-separated phases.
Weekly intelligence discovers scheduled events, verifies independent calendar
and semantic evidence, replays each candidate's historical options, applies a
frozen promotion rule, and seals the resulting plan. Autonomous execution then
consumes that plan, repeats every mutable live-market gate, allocates exact
maximum-loss risk, trades, reconciles, and exits on the market's clock.

For the current measured week, the process selected one conditional AVGO
earnings straddle. AVGO is the content of this week's sealed plan—not a second
bot or permanent ticker rule. A subsequent plan may contain other qualified
events or no trade at all. Broader news-continuation, macro, and peer-spillover
ideas remain disabled or shadow-only because their timestamped replays did not
justify orders.

## What makes it different

- **Magnitude instead of guessed direction.** The primary structure is a long
  call plus long put, so the maximum loss is exactly the premium paid.
- **Auditable weekly discovery.** A fixed 64-name liquid universe is checked
  against independent event calendars and historical option outcomes before a
  plan receives a cryptographic seal.
- **Featherless as a bounded semantic gate.** A multi-model committee checks
  source-grounded event integrity before entry and parses the release after it
  arrives. It may veto risk; it cannot create an order, select quantity, relax a
  limit, invent a ticker, or delay the exit.
- **Alpaca MCP across the lifecycle.** The agent uses MCP for account state,
  clock, news, stock data, option chains, historical option evidence, orders,
  positions, activities, and portfolio history.
- **Evidence before narrative.** Every tested idea—including rejected ones—is
  retained with its method, result, limitation, and keep/reject decision.
- **Autonomous but fail-closed.** Stale data, malformed model output, missing
  quorum, wrong account, unknown order state, or reconciliation mismatch cannot
  silently become a trade.

## Audited preflight

Check it yourself, with no credentials and no network:

```bash
python scripts/verify_evidence.py
```

It recomputes the whole hash chain from the file on disk, prints the terminal
hash and every event type, and asserts the two claims the chain exists to
support: that no unredacted credential field survives, and that no row changed
a policy gate. It exits non-zero if any of that fails.

The published, credential-free hash chain at
[`evidence/preflight_evidence.jsonl`](evidence/preflight_evidence.jsonl) records:

- 8/8 end-to-end and named failure-drill groups passing;
- a real read-only Alpaca MCP lifecycle against the isolated paper account;
- 29 paired AVGO Sep 4 strikes, fitted smile curvature, timestamps, spread, and
  premium/spot, explicitly marked stale and diagnostic-only;
- a Featherless 0/3 fail-closed committee followed by a 2/3 valid recovery with
  its novelty/surprise/confidence vector and per-model latency;
- `policy_gate_changed=false` throughout.

The dashboard makes the 64 → 31 → 9 → 6 → 1 selection funnel, failed models,
rejected strategies, MCP lifecycle, and autonomous evidence chain visible
instead of presenting only the final trade.

## One trade is what survived, not what we looked at

The engine is a scanner, not a single bet. Against a fixed liquid universe it
looks for option term structure implying a scheduled jump, confirms the event
against grounded sources, and replays each candidate's own option history.
Exactly one name cleared every stage:

| Stage | Count | Survivors |
|---|---:|---|
| Universe, fixed before the scan | 64 | — |
| Measurable parity-corrected surface | 31 | — |
| Term structure implies a shared jump | 9 | AVGO CRM CRWD DELL HPE LULU MRVL PANW SNOW |
| Grounded committee confirms a dated earnings event | 6 | AVGO DELL HPE LULU PANW SNOW |
| Survives its own option replay and the execution gates | **1** | **AVGO** |

The rejections are the argument. SNOW stays shadow-only: positive on average,
but its adverse median is negative and its closed-market spread exceeded the
frozen gate. LULU is excluded operationally — its Thursday after-close release
resolves at the exact Friday 09:30 scoring boundary, leaving no reliable liquid
exit. PANW had the most attractive short-fly proxy in the set and the worst
four-leg adverse envelope, at -25.5% on a 15.3% combined spread.

Rebuild the funnel from the committed shadow artifacts, with no credentials:

```bash
python scripts/build_event_premium_funnel.py
```

The cross-sectional rich/cheap model that produced these candidates was itself
falsified before activation and is recorded as such: its residual mostly
distinguishes event names from non-event names rather than mispriced events,
and it labelled AVGO a sell against a directly opposite option history. The
scanner survives as audited discovery; the ranking has no order authority.
`scan_event_premium_book.py` and `classify_event_premium_candidates.py` contain
no order flag and construct no order.

## Current sealed plan

1. Observe the Broadcom earnings event selected before measurement.
2. During the configured pre-close window, select the closest common-strike
   call and put expiring at the end of the measurement week.
3. Require executable premium no greater than 8.5% of spot, combined bid/ask
   width no greater than 5%, each-leg width no greater than 15%, fresh
   synchronized quotes, displayed size, and a valid semantic integrity quorum.
4. Make at most one idempotent entry attempt and cap exact maximum loss at 25%
   of current equity.
5. Hold through the release and begin the exit at 09:45 ET the next session.
   A later emergency flatten is independent of model availability.

The timestamps and event identity currently live in
[`scheduled.py`](src/trading_bot/tournament/scheduled.py). They are configuration
facts, not model decisions.

## Evidence snapshot

The strongest replay uses expired option contracts and historical five-minute
option trade bars obtained through Alpaca MCP. Across eight post-split AVGO
events, the next-morning last-trade proxy returned +44.29% mean and +28.49%
median on premium, with a 62.5% win rate. Under the frozen 8.5%-of-spot premium
gate, six events remained: +48.42% mean and 66.7% wins.

Reproduce every number in this section without credentials:

```bash
python research/strategy-evidence/replay_avgo_straddles.py \
  --offline research/strategy-evidence/avgo_straddle_snapshot.json
```

The snapshot holds only what the replay ever read from the market — the
event-day close, the selected strike and contract symbols, and the entry and
exit trade-bar windows for each leg. No account, position or credential data.
The arithmetic runs on your machine, and the offline output is byte-identical
to the live run it was recorded from; drop `--offline` and supply paper
credentials to re-read it from Alpaca yourself.

A deliberately adverse envelope—buying each leg at its highest entry-window
trade and selling at its lowest exit-window trade—averaged +18.06% across all
eight and +21.30% across the gated six. This is historical option-trade-bar
evidence, **not** an NBBO fill backtest. The sample is small, leg trades need not
be simultaneous, and paper execution does not model market impact or queue
position. Full methods and event-level outcomes are in
[`research/strategy-evidence/`](research/strategy-evidence/).

## Architecture

```text
weekly intelligence
        |
        +----> two independent calendars + Alpaca MCP history
        +----> Featherless typed committee
        +----> deterministic replay + frozen promotion policy
        v
SHA-256-sealed weekly plan
        |
        v
one-minute autonomous execution
        |
        +----> Alpaca MCP account, clock, news and option truth
        +----> Featherless non-expansive integrity veto
        v
deterministic surface + exact-risk gates
        |
        v
idempotent multi-leg order + reconciliation
        |
        v
hash-chained JSONL evidence + read-only dashboard
```

Key modules:

- `scripts/run_event_agent.py` — one autonomous cycle; shadow mode by default.
- `scripts/build_weekly_event_plan.py` — discover, verify, replay, promote, and
  seal the next plan.
- `scripts/run_weekly_event_agent.py` — execute every event in a sealed plan.
- `scripts/run_weekly_event_rehearsal.py` — deterministic failure drills plus a
  real read-only weekly cycle.
- `scripts/capture_surface_diagnostic.py` — read-only multi-strike IV capture.
- `scripts/capture_semantic_preflight.py` — live typed surprise-vector probe.
- `scripts/run_event_rehearsal.py` — real shadow cycle plus named failure drills.
- `src/trading_bot/tournament/scheduled.py` — frozen surface and lifecycle gates.
- `src/trading_bot/tournament/featherless.py` — concurrent typed model router.
- `src/trading_bot/tournament/integrity.py` — semantic veto boundary.
- `src/trading_bot/tournament/decision.py` — exact contract-level risk planner.
- `src/trading_bot/tournament/audit.py` — hash-chained, secret-redacting ledger.
- `src/trading_bot/tournament/weekly.py` — promotion, allocation, and per-event
  lifecycle policy.
- `src/trading_bot/options/` — chain, payoff, order, registry, and MCP mechanics.
- `app/streamlit_app.py` — read-only strategy, evidence, and P&L dashboard.

## Setup

Python 3.12 and [`uv`](https://docs.astral.sh/uv/) are recommended.

```bash
uv sync
cp .env.example .env.local
uv run pytest tests/ -q
```

Before publishing anything — the repository, an evidence export, or a
recording — check that no credential is going out with it:

```bash
python scripts/check_no_secrets.py            # working tree + full git history
python scripts/check_no_secrets.py --selftest # prove the rules still match
```

It reports `path:line rule` and never the matched text, so the output of a
failing run is itself safe to paste. The rules are unit-tested, because a
scanner that has quietly stopped matching is worse than none.

Fill `.env.local` with dedicated paper-account and Featherless credentials.
Secret files, mutable books, runtime ledgers, logs, caches, and Streamlit secrets
are ignored by Git. The tracked preflight chain is an explicitly reviewed,
credential-free evidence export, not a mutable account ledger.

Run one fully connected **shadow** cycle:

```bash
uv run python scripts/run_event_agent.py \
  --env .env.local \
  --featherless-env .env.local \
  --book data/event_book.json \
  --ledger data/event_evidence.jsonl
```

Shadow mode can read real market/account data but has no broker write path.
Paper orders require both the `--enable-orders` flag and the explicit
`TOURNAMENT_ENABLE_ORDERS=YES` environment switch. Do not enable either against
a live-money account.

Build and rehearse a subsequent weekly plan without order authority:

```bash
uv run python scripts/build_weekly_event_plan.py --env .env.local
uv run python scripts/run_weekly_event_rehearsal.py --real
```

The full two-phase operating contract is in
[`deploy/WEEKLY_EVENT_ENGINE.md`](deploy/WEEKLY_EVENT_ENGINE.md).

Run the dashboard:

```bash
uv run streamlit run app/streamlit_app.py
```

## Safety model

- Paper trading only; no live-money support is intended.
- Credentials and account identifiers never belong in source, prompts, logs,
  examples, screenshots, fixtures, or issues.
- Every structure must have a finite positive maximum loss before planning.
- Orders use stable client IDs; a lost response is recovered before any retry.
- A registry mismatch blocks new risk but never blocks required exits.
- Model or data failure may remove an entry, never remove the exit deadline.
- Research metrics, paper fills, current-surface calculations, and measured P&L
  remain separately labeled.

This repository is research software, not investment advice. Paper performance
does not establish live execution quality or a durable trading edge.
