# AI contributor handover

Start here when continuing this repository for Yar and Starboi. Read this file,
`README.md`, and the tests before changing strategy or execution behavior.

## Current objective

Maintain a fully autonomous, auditable paper-trading agent for a fixed measured
window. The strategy is locked: one conditional AVGO near-ATM long straddle
around the configured earnings event. All broad directional-news, macro, and
peer-spillover order paths are disabled on current evidence.

The priority order is:

1. preserve account, deadline, and exact-risk invariants;
2. preserve a truthful evidence trail;
3. maximize measured P&L inside the frozen policy;
4. make the Featherless and Alpaca MCP contributions legible;
5. keep normal operation fully autonomous.

## Locked strategy

- Entry window, event, expiry, exit, and emergency flatten are defined in
  `src/trading_bot/tournament/scheduled.py`.
- Entry is a closest-common-strike long straddle only.
- Executable premium/spot must be ≤8.5%.
- Combined width must be ≤5%; each leg ≤15%.
- Quotes must be fresh, synchronized, two-sided, and sized.
- Featherless must return a valid grounded event-integrity quorum.
- Strong cited evidence of early results, preliminary results, or changed
  guidance vetoes entry. The model has no expansive authority.
- Exact maximum loss is capped at 25% of current equity.
- There is one stable entry attempt. A confirmed non-fill is terminal.
- Exit begins at 09:45 ET the next session regardless of model availability.

Changing any item above is a strategy change, not a refactor. Require new
pre-window evidence, update the decision ledger, and add behavioral tests.

## Architecture and ownership

```text
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

Detailed methods and event rows live in `research/strategy-evidence/`.

## Safe verification workflow

```bash
uv sync
uv run pytest tests/ -q
uv run python -m py_compile scripts/run_event_agent.py app/streamlit_app.py
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

## Remaining integration work

- Bind and verify the dedicated replacement paper account outside Git.
- Run a full replacement-account shadow rehearsal and failure drills.
- Validate one-minute supervision and heartbeat visibility in the target host.
- Capture measured-window account, activity, position, portfolio-history, model,
  and decision-ledger snapshots for the final results narrative.

Do not resurrect a rejected strategy merely to increase trade count. A no-trade
decision is correct when the frozen event, model, surface, account, or clock gate
fails.
