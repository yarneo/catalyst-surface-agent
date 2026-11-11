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
| 11 | Production-parity validation | Real orchestrator replay with simulated broker and network disabled; private input/bar archives; promotion gate | Full historical parity remains unproven; archived champion/health decisions cannot be invented |
| 12 | Equity and income reconciliation | Headline/curve use broker values; credited income counted once; yield model separated; benchmark dates aligned | Continue reconciliation |
| 13 | Complete ledger | Paginated orders and fill activities; partial canceled fills included; quantities reconciled with broker | Corporate actions/transfers require explicit reconciliation if encountered |
| 14 | Execution evidence | Full-fill denominator, uncovered fills exposed, venue quote capture and feed labels | Historical missing quotes cannot be backfilled as observations |
| 15 | Whole-portfolio experiments | ETF, QLD, router, BIL and cash; following-open daily-bar proxy; costs, reserves and limits | Forward sample and production replay agreement |
| 16 | Attribution and benchmarks | QQQ routing beta separated from managed futures; broker marks/credited income; matched daily benchmark dates | Attribution is not a claim of alpha |
| 17 | Truthful dashboard health | Completed cycle/supervisor freshness, exchange calendar, release time, next protection and rebalance | Monitor service freshness |
| 18 | Credential isolation | Code allowlist, credential scan, private file permissions; old broad deployment path retired | Re-run scan each release |
| 19 | Releases and rollback | Source/content manifest, integrity checks, serialized install, prior-code backup; restoration test | Future releases use the same process |
| 20 | Backups | Private state archives with hash manifests and restore verification; private disk snapshot and daily retention schedule | Recurring restore verification |
| 21 | Default tests and failure drills | Default test discovery; rejected/partial/unknown orders, funding failure, stale bars, overnight loss, locks, corruption, half days, release restoration | Maintain regression coverage |
| 22 | Risk-budget comparison | Frozen baseline 70%, cap 60%, cap 50%, stress-aware 70%, risk-cap 25% experiments; upside capture and 5/25bp costs | Forward comparison; no budget changed |
| 23 | Recovery rules | Frozen 1% hysteresis, two-session confirmation, five-session cooldown, three-session recovery experiments | Forward comparison |
| 24 | Tail uncertainty | Asset coverage, tail sample count, bootstrap ES range, simultaneous-shock scenarios and crisis coverage gaps | Unseen crises remain uncertain |
| 25 | Diversification tests | ETF-on/off comparison implemented; 10% PEAD slot and data-readiness checks; crypto stays disabled | **PEAD comparison needs timestamped earnings/entry evidence; not treated as a zero-return completed test** |
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

The new portfolio lab is a conditional daily-bar model, not a claim that every
production trade has been reproduced. Its baseline includes actual deployed
sleeves and caps, but historical ETF selection, historical health decisions,
intraday stops and venue liquidity need separate evidence. A second harness
executes the actual production orchestrator with archived inputs and an
in-memory broker; this isolates decision-code discrepancies from fill-model
assumptions. Exact signal frames and inputs begin accumulating at the next
successful open under this release.

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
| Exact production replay | Private broker request/response tape, input hashes, offline replay and risk-state comparison; tampering/changed-decision drills | Five distinct scheduled open sessions under the same engine, including at least one order request. This is an operational acceptance threshold, not statistical strategy evidence. |
| Portfolio forward lab | Pre-open decision sealing; persistent virtual shares/cash; immutable closes; explicit split/dividend economics; gap detection | 60 new sessions and 12 weekly blocks, followed by cost/confidence and parity review. A Nov 12 start reaches session 60 on Feb 9, 2027 if no sessions are missed or model changes require a new cohort. |
| Earnings drift (PEAD) | Timestamped forecast/result collector; completed reaction filter; subsequent fresh IEX quotes; separate virtual book and corporate-action checks | A forecast must be archived before a new reported result. Eligible entries then need a 60-session holding period. Downloaded old surprises cannot create prospective trades. |
| Featherless availability | Detect capacity errors even inside HTTP 200; bounded requests; fixed backup pair only when primary votes are missing | Verify provider responses each planning cycle. Valid primary disagreement cannot trigger model shopping. Economic rejection still blocks a candidate even if the provider recovers. |
| AI contribution | Frozen cohort, duplicate removal and session-level value/cost accounting | 30 independent sessions in which AI changes the target, with marked outcomes. Thirty ordinary calendar/trading days do not necessarily provide this. |
| Options | Separate infrastructure/economic rejection, quote and lifecycle evidence | 30 independent completed eligible events with executable entry/exit evidence, plus calibration and positive cost-adjusted expectancy. Rejections without quotes have no invented P&L. |
| Safety and recovery | Synthetic rejected/partial/unknown-fill, stale-data, loss, locking, corrupt-state and restore drills | Observe normal scheduled operation. A natural outage or crash is not required to complete engineering tests, and should never be induced in the trading service. |

Historical venue quotes, champion changes and health decisions that were never
archived cannot be recovered by simply waiting. Their historical research
remains conditional. The exact broker-tape replay verifies production code
against what it actually requested and received; it does **not** independently
validate broker marks or the daily-bar simulator's fill assumptions.

The PEAD collector currently runs an isolated component book. Integrating its
actual prospective path into the whole-portfolio allocation comparison, and
validating candidate-specific production execution, remain separate engineering
steps once usable inputs exist. The PEAD portfolio row stays `DATA_REQUIRED`;
a flat or unobserved history must not be presented as a successful test.

Forward books fail visibly on a missed initial/intermediate session. Do not
silently backdate an entry or restart only a losing candidate. Any recovery or
new cohort must be recorded and applied consistently to the comparison family.

Collection schedule: PEAD estimates before the open and after the close,
virtual execution within 09:35–09:45 ET, forward plans before the open, and
close settlement/replay verification after the close. The exchange calendar
governs eligibility; repeated runs cannot create additional evidence.

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
- `scripts/verify_portfolio_replays.py`: compare captured production cycles offline.
- `scripts/run_pead_shadow.py --phase collect|observe|close`: prospective earnings evidence and virtual fills only.
- `scripts/portfolio_backup.py`: private backup plus temporary restoration/hash check.
- `scripts/build_portfolio_release.py`: code-only archive and manifest.
- `scripts/install_portfolio_release.py`: validate/install under the execution lock; explicit rollback supported.

Release requirements: passing failure drills, credential scan, code manifest,
private-state preservation, read-only broker reconciliation, and post-release
service/scheduler verification. Host configuration and account-specific
verification evidence stay on the private runtime.
