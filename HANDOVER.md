# AI contributor handover

Start here when continuing this repository for Yar and Starboi. Read this file,
`README.md`, and the tests before changing strategy or execution behavior.

## Repository boundary

This public repository, `yarneo/catalyst-surface-agent`, on `main`, is the
canonical collaborator-facing project. Commit and push all shareable source,
tests, research, dashboard work, and documentation here.

The separate `trading-bot` checkout is used only by the host running the paper
deployment and contains private environment and supervisor configuration. Its
`simplify-for-kickoff` branch is a legacy development branch, not a project or
contribution target. Never copy credentials, account identifiers, mutable
books, runtime ledgers, logs, or host-specific launch configuration from that
checkout into this repository.

## Current deployment status

The measured lifecycle is complete. The agent bought 13 AVGO Sep 4 $367.50
straddles at a $29.60 combined debit on Sep 2 and autonomously exited at
09:45:18 ET on Sep 3 for a $21.37 credit. Final paper equity was $89,299.30:
-$10,700.70, or -10.7007%. The broker is flat. After the terminal-state fix, a
clean supervisor cycle reported `DONE` with zero open positions. Reviewed,
credential-free figures are in `evidence/measured_result.json`; mutable account
books, ledgers, order IDs, and logs remain private.

## Current objective

Present the measured result honestly, preserve the reusable autonomous engine,
and develop the stricter version-2 hypothesis in `docs/FINAL_POSTMORTEM.md`.
Weekly intelligence still discovers, verifies, replays, promotes, and seals a
plan; autonomous execution consumes that plan without strategy drift. AVGO was
the measured week's selected result, not a separate bot or permanent ticker
rule. Broad directional-news, macro, and peer-spillover order paths remain
disabled on current evidence.

The priority order is:

1. preserve account, deadline, and exact-risk invariants;
2. preserve a truthful evidence trail;
3. improve expected P&L only through new falsifiable evidence;
4. make the Featherless and Alpaca MCP contributions legible;
5. keep normal operation fully autonomous.

## Completed measured plan

- Entry window, event, expiry, exit, and emergency flatten are defined in
  `src/trading_bot/tournament/scheduled.py`.
- Entry is a closest-common-strike long straddle only.
- Executable premium/spot must be ≤8.5%.
- Combined width must be ≤5%; each leg ≤15%.
- Quotes must be fresh, synchronized, two-sided, and sized.
- Featherless must return a valid grounded event-integrity quorum.
- Strong cited evidence of early results, preliminary results, or changed
  guidance vetoes entry. The model has no expansive authority.
- Exact maximum loss is capped at 40% of current equity. This was selected in a
  versioned Aug 30 sizing review; all non-sizing gates remain unchanged.
- There is one stable entry attempt. A confirmed non-fill is terminal.
- Exit begins at 09:45 ET the next session regardless of model availability.

These rules are frozen historical facts for the measured run. Do not edit them
to make the result look better. A future policy is a new version: require new
evidence, update the decision record, and add behavioral tests.

## Architecture and ownership

```text
scripts/build_weekly_event_plan.py
  ├─ tournament/event_calendar.py  independent event discovery
  ├─ tournament/event_semantics.py bounded Featherless classification
  ├─ tournament/event_replay.py    automatic historical option replay
  ├─ tournament/weekly.py          promotion and exact-risk allocation
  └─ tournament/weekly_plan.py     sealed plan contract

scripts/run_weekly_event_agent.py
  └─ consumes the sealed plan and owns each event lifecycle

scripts/run_event_agent.py
  ├─ options/mcp.py          Alpaca MCP transport and tool wrappers
  ├─ options/book.py         durable structure registry/reconciliation
  ├─ options/execution.py    idempotent open and escalating close
  ├─ tournament/scheduled.py frozen event/surface/lifecycle policy
  ├─ tournament/featherless.py typed concurrent model quorum
  ├─ tournament/integrity.py non-expansive semantic veto
  ├─ tournament/decision.py  exact-risk allocation
  └─ tournament/audit.py     hash-chained result evidence
```

`app/streamlit_app.py` is read-only. It may display decisions and broker truth;
it must never become an order surface.

## Non-negotiable invariants

1. Never commit or print secrets, account numbers, mutable books, ledgers, or
   logs. `.env*`, `data/`, `logs/`, and `.streamlit/secrets.toml` stay ignored.
2. The MCP client is read-only unless constructed with `live=True`; the runner
   additionally requires `--enable-orders` and `TOURNAMENT_ENABLE_ORDERS=YES`.
3. Every option structure has a finite, positive exact maximum loss.
4. Write the complete intent and stable client order ID before submission.
5. A submission error does not prove rejection. Recover by client order ID.
6. Never escalate over an order that is not provably terminal.
7. Book the quantity and price that filled, not what was requested.
8. Reconciliation mismatch blocks opening but cannot block closing.
9. Host-local time is forbidden in strategy code; use timezone-aware ET helpers.
10. Model output is untrusted data. It cannot choose size, construct orders,
    add tickers, waive gates, or delay deadline exits.
11. Every research failure and runtime no-trade remains part of final reporting.

## Evidence already established

- The initial large condor book was retired after correlated early losses,
  excessive initial commitment, weak scoring, and a payoff shape mismatched to
  the short objective.
- Broad direct-news continuation was too sparse and noisy.
- The tested NFP-to-BTC bridge lost after its cost assumption.
- AVGO post-open continuation and semiconductor laggard spillover were negative
  at the primary two-hour horizon.
- ISM continuation was weak; its short fade was too small for demonstrated
  option costs.
- Eight post-split AVGO historical ATM straddles from Alpaca MCP option trade
  bars produced +44.29% mean / +28.49% median premium return. The six events
  passing the frozen premium gate produced +48.42% mean and 66.7% wins.
- These are trade-bar proxies, not historical NBBO fills. Preserve that label.
- Featherless probes revealed malformed schemas, reasoning-only responses,
  capacity errors, and nominal HTTP timeouts exceeding wall-clock budgets. The
  production router uses killable workers, exact schemas, grounding, narrow
  audited repairs, and no-quorum/no-entry behavior.
- The activation rehearsal passed 8/8 named groups. A real closed-market MCP
  surface captured 29 paired strikes and positive fitted smile curvature; it is
  stale, diagnostic-only, and changed no gate.
- The latest visible Featherless trace preserves both a 0/3 fail-closed result
  and a later 2/3 valid recovery. The third model hit the true 35-second bound.
- The 09:45 exit and explicit 15:30 emergency-flat state both pass, including
  when reconciliation disagrees. The managed exit remains retriable.
- A real read-only weekly rehearsal discovered AVGO from two independent
  calendars, classified it through the Featherless committee, automatically
  reproduced eight historical option events, sealed a prequalified plan, and
  completed the account/MCP cycle without order authority.
- The weekly planner correctly keeps closed-market width diagnostic-only and
  reruns the unchanged premium, width, freshness, size, and timing gates inside
  the actual entry window. It may seal an empty plan when evidence is weak.
- The measured entry deployed $38,480 (38.48% of starting equity). The
  straddle's -27.80% return produced a -10.70% account return.
- The live 8.05%-of-spot debit was inside the 8.5% gate but above every premium
  in the accepted six-event historical sample. This unsupported extrapolation
  and the small sample are central strategy lessons, not footnotes.
- The scheduled exit filled within 18 seconds of 09:45 ET and broker truth was
  flat. A later local recovery bug recreated a phantom registry row but
  close-only order semantics prevented reverse exposure. Recovery now treats a
  closed event row as terminal and has a regression test.

Detailed methods and event rows live in `research/strategy-evidence/`.

## Safe verification workflow

```bash
uv sync
uv run pytest tests/ -q
uv run python -m py_compile scripts/run_event_agent.py \
  scripts/build_weekly_event_plan.py scripts/run_weekly_event_agent.py \
  app/streamlit_app.py
uv run python scripts/check_no_secrets.py
```

To exercise real integrations without order capability, use the shadow command
from `README.md`. Use a temporary book and ledger for rehearsals so a shadow run
cannot contaminate a measured result record.

Before any order-enabled deployment, verify through the runner that the account
is the explicitly pinned, flat, unblocked $100k paper account with the required
options level. The repository intentionally contains no real account metadata.

## Contribution protocol

1. Inspect `git status`; preserve unrelated collaborator changes.
2. State the invariant or evidence question the change addresses.
3. Prefer pure functions and injected transports for decision behavior.
4. Add a regression test for every failure mode or policy boundary.
5. Run focused tests, then the full exported suite.
6. Update `research/strategy-evidence/POSTMORTEM_AND_HYPOTHESES.md` when evidence
   changes a keep/reject decision.
7. Update README and dashboard when implementation changes what users see.
8. Never reinterpret weak or missing evidence as permission to trade.

## Remaining work

- Keep the dedicated paper account and host-specific supervisor configuration
  outside Git. The measured deployment is already flat and complete.
- Treat `evidence/measured_result.json` as the immutable public result summary;
  never replace it with mutable books, ledgers, or logs.
- Use `docs/ONE_PAGE_OVERVIEW.md` and `docs/DEMO_NARRATION.md` for the public
  explanation. Keep the loss and limitations prominent.
- Research version 2 with walk-forward or leave-one-event-out validation,
  executable value margins, uncertainty-aware sizing, and explicit no-trade
  behavior. Do not merely lower 8.5% to another arbitrary number.

Do not resurrect a rejected strategy merely to increase trade count. A no-trade
decision is correct when the frozen event, model, surface, account, or clock gate
fails.
