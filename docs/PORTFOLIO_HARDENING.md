# Portfolio hardening register

Updated 2026-11-11. Scope: reusable paper portfolio runtime. Credentials,
account identifiers, private dashboards, host configuration, books, logs and
account evidence remain outside this repository. The event engine and its
historical deployment remain separate.

**Implemented does not mean a strategy is proven profitable.** Operational
changes are tested and deployed first. Forward experiments must collect new
observations before any change in risk budget can be justified.

| # | Deliverable | Implementation and verification | Remaining evidence |
|---|---|---|---|
| 1 | Exact fractional exits | Broker position/available quantity; decimal truncation; regression drill | Observe subsequent real paper exits |
| 2 | Fill-confirmed sequencing | Journaled terminal fills; ETF and router sales precede their purchases; funding sells confirmed | New fills under the release |
| 3 | Portfolio gate for every sleeve | Guard/halt runs before ETF and all daily sleeves; orchestrator interaction test | Scheduled open observations |
| 4 | Prior-close daily loss | Broker prior close replaces morning balance; same-session migration; consistent halt semantics | A naturally occurring trigger |
| 5 | Intraday protection | One-minute risk-only cycle; stale own orders canceled and reconciled; independent watchdog; exchange-aware close | Continuous operational observation |
| 6 | Completed-session data | Shared session context, freshness checks, no bullish fallback, archived signal inputs | Provider outage observation |
| 7 | Accurate outcomes | Exceptions propagate; started/completed/failed cycle records; unknown submissions block expansion | Failure alerts in normal operations |
| 8 | Projected limits | Cash and BIL reserves, gross/sleeve/position/factor caps, pending-order exclusion | Fill/mark drift monitoring |
| 9 | Durable order identity | Pre-submission journal, deterministic client IDs, exclusive execution lock | Broker-response-loss recovery in service |
| 10 | Durable state | Atomic fsync/replace, recoverable risk copy, missing/stale health blocks new allocation | Backup/restore checks continue |
| 11 | Production-parity validation | Exact broker-tape replay plus independent simulated decisions, cash, shares, cost basis and risk-state comparison; network disabled | Five successful new opens including an order; costs and comparison tolerances must pass |
| 12 | Equity and income reconciliation | Headline/curve use broker values; credited income counted once; yield model separated; benchmark dates aligned | Continue reconciliation |
| 13 | Complete ledger | Paginated orders and fill activities; partial canceled fills included; quantities reconciled with broker | Corporate actions/transfers require explicit reconciliation if encountered |
| 14 | Execution evidence | Full-fill denominator, uncovered fills exposed, venue quote capture and feed labels | Historical missing quotes cannot be backfilled as observations |
| 15 | Whole-portfolio experiments | Actual production orchestrator at observed minute quotes; ETF, QLD, router, BIL, cash and funded PEAD; persistent books, intraday guard, 5/25bp impact | Complete forward sample and production comparison agreement |
| 16 | Attribution and benchmarks | QQQ routing beta separated from managed futures; broker marks/credited income; matched daily benchmark dates | Attribution is not a claim of alpha |
| 17 | Truthful dashboard health | Completed cycle/supervisor freshness, exchange calendar, release time, next protection and rebalance | Monitor service freshness |
| 18 | Credential isolation | Code allowlist, credential scan, private file permissions; old broad deployment path retired | Re-run scan each release |
| 19 | Releases and rollback | Source/content manifest, integrity checks, serialized install, prior-code backup; restoration test | Future releases use the same process |
| 20 | Backups | Private state archives with hash manifests and restore verification; private disk snapshot and daily retention schedule | Recurring restore verification |
| 21 | Default tests and failure drills | Default test discovery; rejected/partial/unknown orders, funding failure, stale bars, overnight loss, locks, corruption, half days, release restoration | Maintain regression coverage |
| 22 | Risk-budget comparison | Frozen baseline 70%, cap 60%, cap 50%, stress-aware 70%, risk-cap 25% experiments; upside capture and 5/25bp costs | Forward comparison; no budget changed |
| 23 | Recovery rules | Frozen 1% hysteresis, two-session confirmation, five-session cooldown, three-session recovery experiments | Forward comparison |
| 24 | Tail uncertainty | Asset coverage, tail sample count, bootstrap ES range, simultaneous-shock scenarios and crisis coverage gaps | Unseen crises remain uncertain |
| 25 | Diversification tests | ETF-on/off and funded PEAD implemented: 10% total, 5% per stock, two positions; shared cash/BIL funding, router and guard | New prospective earnings events and completed funded outcomes; no historical surprises or component P&L injected |
| 26 | AI value evidence | Frozen model/prompt/strategy cohort, session deduplication and clustering, conservative costs/confidence gate | Independent changed sessions; target counterfactual remains distinct from broker P&L |
| 27 | Options diagnostics | Rejection stages/classes, unique events, plan freshness, accepted virtual fills and completed outcomes | No invented P&L for candidates without executable quotes |
| 28 | Promotion and retirement | Frozen cohort/input hashes, immutable forward observations, weekly-block evidence, excess-return retirement; legacy promotion path quarantined | At least 60 sessions / 12 weekly blocks plus verified parity; no candidate currently inherits order authority |

## What is deployed versus what is being evaluated

The current risk budget is preserved. The one-minute supervisor only applies
portfolio protection; it does not rebalance strategies every minute.
Daily strategy allocation remains a scheduled open action. Reporting closes
follow the exchange calendar, including half days.

ETF retraining continues to generate proposals while the deployed champions are
frozen for comparison. The old equal-third research model cannot promote
portfolio policies. The write boundary independently rejects automatic
promotion unless parity and frozen forward evidence are supplied.

Historical research remains a conditional daily-bar proxy. The v4 prospective
lab instead executes the production orchestrator against isolated in-memory
brokers, using frozen configuration, champion and allocation inputs, completed
signal bars and subsequently observed minute quotes. Opening allocation runs at
09:35 ET, funded PEAD entries at 09:40, and the production guard runs at each
observed minute through the exchange close. Fills use observed bid/ask plus
modeled adverse impact of 5bp or 25bp. Live execution probabilities and queue
position remain unmodeled; the observed-cost comparison can reject these cost
assumptions. All experiments remain without order authority.

Forward experiments start from equal virtual capital after their freeze time.
An intraday freeze excludes that session's already elapsed open. Model and
runner source fingerprints are part of the immutable cohort identity.
They do not import the historical model's ending positions or past profits.
A change in model source, strategy selection or portfolio configuration creates
a new cohort. Retired candidates cannot gain authority because they once had
a strong backtest.

## Verification and operation

### Remaining work: engineering versus elapsed evidence

The prospective evidence release adds these collectors without changing the
deployed risk budget or granting research candidates order authority:

| Item | Can be finished now | What still needs observations |
|---|---|---|
| Production/simulator comparison | Exact broker tape plus independent ledger and simulated decision cycle using observed fill prices; initial/final broker snapshots; quantity, basis, cash, risk and cost checks | Five distinct successful scheduled opens under the same engine, including at least one order request. Synthetic drills never enter these counts. |
| Portfolio forward lab | Eleven production-rule variants at observed minute quotes; pre-open sealed inputs; persistent books; immutable closes; explicit corporate actions and gap detection | 60 complete new sessions and 12 weekly blocks; positive cost-adjusted excess at both 5/25bp assumptions and verified comparison. A Nov 12 start reaches session 60 on Feb 9, 2027 if no days are incomplete or model changes require a new cohort. |
| Earnings drift (PEAD) | Prospective earnings collector integrated into a funded common-book sleeve; entry cost, cash/BIL funding, router interaction, 60-session exits and earlier portfolio protection | A forecast archived before a new result, a completed positive reaction, then a fresh next-session quote. At least one completed funded outcome plus all portfolio gates; cash-only results cannot qualify PEAD. |
| Featherless availability | Detect capacity errors even inside HTTP 200; bounded requests; fixed backup pair only when primary votes are missing | Verify provider responses each planning cycle. Valid primary disagreement cannot trigger model shopping. Economic rejection still blocks a candidate even if the provider recovers. |
| AI contribution | Frozen cohort, duplicate removal and session-level value/cost accounting | 30 independent sessions in which AI changes the target, with marked outcomes. Thirty ordinary calendar/trading days do not necessarily provide this. |
| Options | Separate infrastructure/economic rejection, quote and lifecycle evidence | 30 independent completed eligible events with executable entry/exit evidence, plus calibration and positive cost-adjusted expectancy. Rejections without quotes have no invented P&L. |
| Safety and recovery | Synthetic rejected/partial/unknown-fill, stale-data, loss, locking, corrupt-state and restore drills | Observe normal scheduled operation. A natural outage or crash is not required to complete engineering tests, and should never be induced in the trading service. |

Historical venue quotes, champion changes and health decisions that were never
archived cannot be recovered by simply waiting. Their historical research
remains conditional. The exact broker-tape replay verifies production code
against what it actually requested and received; it does **not** independently
validate broker marks or the daily-bar simulator's fill assumptions.

Simulator comparison and funded PEAD integration are implemented and tested;
neither waits for future coding once inputs arrive. Exact broker replay is
request-for-request. The independent fill ledger permits at most $0.05 cash
discrepancy, 1e-7 shares and $0.01 average entry discrepancy. The simulated
decision book uses a frozen 5bp-of-equity tolerance for mark-timing effects;
risk flags must agree. Observed adverse impact beyond the venue bid/ask must
fit the 25bp stress assumption. A failed comparison stays visible and prevents
eligibility. These are operational tolerances, not a profitability guarantee.

The PEAD portfolio row is `FORWARD_ONLY` for historical research; its prospective
implementation is active. Forecast/result/reaction timestamps are validated at
execution. Its 10% reserve reduces capital available to the other sleeves;
entries release that reserve through the usual broker funding checks. Two
cost books can hold different stocks and both are included in quote capture.
Corporate actions update quantity, cost basis and income. Early risk exits
become completed portfolio outcomes. The component collector remains diagnostic.

Forward books fail visibly on a missed initial/intermediate session. Do not
silently backdate an entry or restart only a losing candidate. Any recovery or
new cohort must be recorded and applied consistently to the comparison family.

Collection schedule: PEAD estimates before the open and after the close,
forward plans before the open, one-minute read-only quote capture from 09:35 ET
through the actual exchange close, and settlement/comparison after the close.
The component PEAD observer retains its 09:35–09:45 window. Research execution
is reconstructed from the sealed observed tape; it cannot place broker orders.
The exchange calendar handles holidays and half days. A missing opening
observation cannot be filled retrospectively; individual orders require fresh
quotes. Missing required minutes remain in the reported P&L but do not count
as complete evidence. Repeated collection cannot rewrite observations.

Every fill requires a fresh executable quote; nonheld symbols with stale quotes
do not disable unrelated protection. An existing BIL cash-equivalent holding
may use its last quoted value for valuation for at most one day, matching its
exemption from price stops. Such minutes are counted explicitly. This exception
never permits a stale-quote BIL purchase, funding sale or exit. Other held assets
need fresh minute marks. Partial progress before a failed simulated cycle is
preserved, and failures make the session incomplete.

The minute collector uses a bounded read-only HTTP request and does not import
the trading SDK, pandas or the simulation engine. Heavy settlement runs after
close, separately from protection. Host wrappers prevent overlapping imports;
their machine-specific settings remain private.

Collector failures return a failing process status for the existing scheduled
job alerting path, as well as a visible dashboard state. Options calendar
confirmation uses bounded, markup-stripped Alpaca article bodies when summaries
omit the release session. Explicit dates are checked at full day/year boundaries;
provider errors remain recorded and the independent-source quorum is preserved.
The general shadow planner profile is now included in code-only releases.

Run `uv run pytest` for the default suite. Portfolio installations use
`uv sync --extra portfolio`.

- `scripts/run_portfolio.py --phase open --inspect --no-enrich`: read-only ownership/risk plan.
- `scripts/run_portfolio.py --phase risk --live --no-enrich`: paper protection only.
- `scripts/portfolio_health.py`: independent completed-work watchdog.
- `scripts/portfolio_close.py`: report once after the actual exchange close.
- `scripts/portfolio_lab.py`: frozen research; no order authority.
- `scripts/portfolio_lab.py --prepare`: seal next decisions before that session opens.
- `scripts/capture_research_market.py`: read-only minute quotes for the frozen research universe.
- `scripts/verify_portfolio_replays.py`: compare captured production cycles offline.
- `scripts/run_pead_shadow.py --phase collect|observe|close`: prospective earnings evidence and virtual fills only.
- `scripts/portfolio_backup.py`: private backup plus temporary restoration/hash check.
- `scripts/build_portfolio_release.py`: code-only archive and manifest.
- `scripts/install_portfolio_release.py`: validate/install under the execution lock; explicit rollback supported.

Release requirements: passing failure drills, credential scan, code manifest,
private-state preservation, read-only broker reconciliation, and post-release
service/scheduler verification. Host configuration and account-specific
verification evidence stay on the private runtime.
