# Phase 30: event reaction hypotheses v1

> The event trader seeks to exploit short-term market reaction, not determine the ultimate truth of the underlying narrative.

> Source speed matters only insofar as a viable tradeable move remains when Prism can execute.

## Wiring and experimental identity

The Phase 29 public collector reads the existing 18 free feeds, validates and fsyncs receipts,
and writes no orders or paper positions. The authoritative free-source worker commits
Phase 23 observations, logical events, updates and source provenance. The Phase 26A Work
OAuth gateway follows its existing spool/authoritative-ingest path. Both publish the same
information-only `paper_nimble_triggers` outbox and atomic notice **after context commit**.
The supervised Phase 27 worker consumes each trigger independently for each run, under
the existing runtime lock and single DuckDB writer. Its Phase 30 branch classifies individual
causal reports, freezes a reference and admission attempt, and calls Phase 27 admission.
Only a fresh public executable bid/ask **observed after intent** can open the paper position.
Complete minutes subsequently retry confirmed opportunities and monitor position state;
two-second quote ticks monitor prices. Forward reaction collection remains independent.

The original `paper_nimble_v1` definition includes its complete hypothesis universe. Its
policy remains `paperexecution_nimble_ec40a2968bcc33f7c7654a1c8d7dcc200361e88bca99079cb4f45738ba785501`.
All 28 original hypothesis IDs, predicates, scientific verdicts and entry grids are unchanged.
Paper v2 remains the fixed-horizon baseline. No existing run is mutated or retired.

The successor policy is `paper_nimble_event_v1`: 28 original plus six event identities.
Two **independent paper cohorts**, `event_variant=immediate` and `event_variant=confirmed`,
each select the original 28 plus their three event identities (31 per run). Their distinct
universe IDs include policy, variant and selected hypothesis IDs. Each starts at $100 with
unchanged 20% notional, 2x isolated leverage, $10 minimum, three-position/60% gross caps,
lot rounding and six assets: AAVE, BTC, ETH, HYPE, LINK, SOL. These are independent
experimental accounts, not permission to double exposure within an account. Keeping them
separate prevents immediate exposure from turning a confirmed entry into mere support.
Their evidence is never pooled. `market paper nimble create --events --variant both` activates
both atomically with one current clock, after `register --events`. Common technical opportunities and actual dispatch delays
can still affect admission; latency, costs and blocking are disclosed, not controlled away.

## Frozen registered hypotheses

| Family; names end `_v1` | Direction / mapping | Freshness | Immediate window / max hold | Confirmed window / max hold | Materiality minimum |
|---|---|---:|---:|---:|---:|
| `venue_disruption_immediate`, `venue_disruption_confirmed` | Coinbase/Kraken severe core trading outage → BTC short only | 300s | 20m / 60m | 45m / 90m | 40 |
| `protocol_stress_immediate`, `protocol_stress_confirmed` | SOL cluster halt → SOL short; confirmed affected native protocol exploit/pause → its supported asset short | 3600s | 60m / 120m | 120m / 240m | 60 |
| `sec_spot_etf_approval_surprise_immediate`, `sec_spot_etf_approval_surprise_confirmed` | Narrow official contrary-expectation approval → underlying BTC or ETH long only | 5400s | 120m / 240m | 240m / 480m | 60 |

The rationale for the venue pair is a testable interruption of BTC spot liquidity/access,
with execution on another available venue; it does **not** propagate to ETH/SOL or every
exchange. The protocol pair tests a short reaction to interrupted native utility or an
active loss of protocol assets/confidence, confined to that protocol's own execution asset.
The regulatory pair tests a demand/access reaction to a specific unexpected spot product
approval. These are economic priors, not empirically established edges. Horizons and
thresholds were frozen without fitting Phase 29 returns or synthetic PnL.

Protocol entity mappings are `bitcoin→BTC`, `ethereum→ETH`, `solana→SOL`,
`hyperliquid→HYPE`, `chainlink→LINK`, `aave→AAVE`. Exploits require **both** that exact
protocol entity, an explicit matching `protocol` attribute/fact, and its explicitly affected asset. A bridge exploit, an application
on Ethereum, or an unrelated token does not automatically become an ETH trade.
Hyperliquid execution unavailability overrides any HYPE thesis.

Stablecoin depegs/redemption problems, broad regulatory enforcement/court/ETF notices,
withdrawal-only disruption, releases/advisories, general institutional announcements and
ambiguous directions remain research-only. No generic negative-news mapping exists.
There are no trades, new asset mappings or opposite-resolution hypotheses for these.

## Eligibility and confidence

Classification reads structured attributes and provenance; it never reads a headline to
choose direction. Each report retains its **own** facts and source type from the authoritative
source registry. Attributes merged from different reports cannot manufacture an official
eligibility claim. Publication must exist, not be future-dated, and the report must be live.
DENIED and historical imports cannot trade. Apply Phase 23 `cap_confidence` first:
AI/Work maximum REPORTED, secondary sources at most CONFIRMED, approved direct official
feeds at most OFFICIAL. Require at least REPORTED and the per-family Phase 23 materiality
score above. Source labels, claimed OFFICIAL confidence and external discovery clocks do
not bypass these rules. Materiality is relevance, not direction or expected profitability.

Venue admission requires `exchange_outage`, the exact allowlisted status provider and
hostname, exchange-notice source class, Statuspage impact `major`/`critical`, status
`investigating`/`identified`, and a core trading component in `major_outage`.
Verified component IDs are Coinbase `jgjv19xyfvpv` (Advanced Trade), `tjhgmj0ysvln`
(Trading), `zzhmzjz0wqj6` (API); Kraken `cfpbbhgnlnbf` (spot REST), `qkyy5vkk5zpx`
(Kraken API). If IDs are omitted, only frozen exact corresponding component names qualify.
Unknown IDs fail closed. Custody, Prime and derivatives-only Kraken API IDs are excluded.
Canonical severity is minor/major/critical from these structured fields, not alarming prose.
Provider status/severity/component states remain in the context and entry evidence.
[Public component audit](evidence/phase30/official-components.json) records the source checks.

Protocol admission is either Solana's official `chain_halt`, impact major/critical and
cluster `qxs6x3yc9cxg` in major outage (not RPC/explorer), or `exploit_confirmed` /
`emergency_pause` with structured `status=confirmed`, `target_kind=protocol`, estimated
loss ≥$10m and exact affected native `protocol`/entity/asset. Security source classes are protocol
announcement, security advisory or capped AI monitor. The existing Work GenericAttrs
contract can carry these scalar facts. An advisory, suspected exploit or unspecified loss
fails eligibility. Work facts never become OFFICIAL merely by citing an official URL.

Regulatory admission requires provider `sec_rss_v1`, regulator class, sec.gov hostname,
structured `decision=approved`, `product=spot_etf`, `underlying_asset=BTC|ETH`, a docket ID,
and a **prior official structured contrary expectation** (`expected_decision=rejected`)
for the same docket, recorded within the preceding day and available before the event's
first_seen. Each expectation is checked from its own official report, not folded attributes.
The current ordinary RSS classification does not invent those fields or expectations.
This pair is consequently dormant when that evidence is unavailable: an approval article
or Work assertion alone remains AMBIGUOUS_DIRECTION. Synthetic eligibility does not imply
that today's feeds can populate every required field. No broad regulatory direction is used.

Freshness uses source publication for a first report. A structured official state/severity
transition may use its provider information/revision time; arbitrary article rewording or
corroboration never rejuvenates age. Publication age is retained separately. A qualifying
transition observed after activation can enroll an older logical episode, but the logical
event/asset can be admitted only once per cohort, ever. Escalation/resolution changes state;
it does not reopen a consumed episode. Later corroborators add provenance and a diagnostic
remaining-opportunity check, with zero automatic sizing change.

## Immediate, confirmed and reference clocks

Immediate submits intent as soon as all deterministic eligibility, data, venue, exposure,
funding and cost gates pass. It requires **no** agreeing return, taker flow, book/OI state,
or minimum initial price move. Price validity and a viable target are execution gates.

Confirmed additionally requires the first qualifying **fully post-context-availability**
COMPLETE minute with signed mid return >0 **and** signed taker notional fraction ≥0.10.
`fraction=(buy_notional-sell_notional)/(buy_notional+sell_notional)`; long sign +1, short −1.
Zero/missing flow, incomplete data or gaps cannot confirm. The previous mid must belong
to a continuous preceding minute. Confirmation must be ingested and ≤90s old at decision.
Both signals are causal; no OI or six-filter stack is added. Retry only until information
freshness or the cohort reaction window expires. Missing confirmation is retained once
per observed minute as `CONFIRMATION_MISSING`; terminal expiry is `STALE_EVENT`.
Store confirmation observed/available times and extra availability delay in seconds.

Pre-event reference is the latest COMPLETE production minute bid/ask mid **known before
Prism first_seen**, with quote time ≤availability <first_seen and quote age ≤120s.
Require 15 continuous complete known minutes for volatility geometry; no fallback to
future bars or candle-open fills. Availability price is a known current quote if available
at the context clock, otherwise the latest causal sampled pre-availability reference;
its quote time/basis are explicit. Missing reference or availability remains missing,
never interpolated. These minute samples are research references, never executable fills.

## Objectives, exits, funding and costs

Let `P0` be the frozen pre-event mid, `s` +1/-1, `W` the reaction window minutes, and
`V15=sqrt(15)*mean(causal_minute_true_range)/P0` over the last 15 known minutes.
The objective is `P0*(1+s*0.5*V15*sqrt(W/15))`. It is fixed from causal pre-event geometry.
Waiting never recenters that objective on a later price. A completed or uneconomic move
fails entry as UNECONOMIC_TARGET. No TP percentage grid search or tiny-sample tuning.

Price invalidation is `P0-s*entry_observed_full_spread`, frozen at entry; an executable
reclaim across this level invalidates. State invalidation uses Phase 27's two consecutive
fully post-entry minutes with opposing signed flow fraction ≤−0.35. Resolution/monitoring,
denial, or a fresh qualifying opposite event for the same asset within 900s invalidates the
existing event thesis; its event ID must match. New conflicting entries are suppressed.
Resolution never creates a long. Missing state is unknown, not false confirmation.

Reuse Phase 27 TAKE_PROFIT, INVALIDATED, THESIS_EXPIRED (after W with progress below
25% of fixed target distance), COST_DECAY, data-failure handling and isolated gap/emergency
guards. Max hold is measured from actual fill and is only a backstop. Forward research at
1/5/15/30/60/120/240m plus the original reaction-primary window continues after exit and
for fired rejected signals with a causal reference. Context forward outcomes stay independent.

Retain 4.5bp taker fees per side and per-side slip BTC/ETH 2bp, SOL 4bp, HYPE/LINK 6bp,
AAVE 8bp plus observed full spread. Require target distance ≥2× total round-trip cost
including adverse funding reserve (ceil(max_hold/60) hourly settlements). Recheck economics,
freshness, resolution/conflict and venue availability at the future fill. Quotes must be
≤3s old by exchange and receipt clocks and strictly after intent by exchange/availability.
Entries and exits cross bid/ask plus adverse slippage; no historical or maker fills.

Store current signed funding, next hourly settlement, estimated first cost/credit and
reserve economics at admission and fill. Existing observed settlement accounting and later
reconciliation remain. COST_DECAY stays active: after half the reaction age, below 80% TP
progress, within 120s of funding, adverse marginal funding >25% of remaining target distance
terminates the thesis. Beneficial funding does not trigger it. Funding does not excuse a
marginal headline trade and missing funding cannot silently admit an event.

## Venue safety, conflicts and confluence

An active major/critical official Hyperliquid exchange incident rejects successor entries
as VENUE_UNAVAILABLE and cancels pending intents. Successor positions receive no executable
quotes while that venue is unavailable: **exit fills are also blocked**, until official
monitoring/resolution. Risk exits may queue, but cannot pretend to execute; no timeout
silently restores availability. Market-price reaction research continues independently.
The original baseline policy remains frozen; this extra venue safeguard applies to the
successor accounts, including their technical positions.

Existing Phase 27 conflict/reversal controls remain: monitor/invalidate first, release
capital only on confirmed exit, block opposite OPEN/EXIT_PENDING/PENDING exposure, allow a
subsequent causal reversal after close. Same-side support records a co-occurrence with
zero size increase. The event entry snapshot records aligned fired existing hypotheses in
the last 15m, their IDs/families and `event_only`, `event+technical`, `event+microstructure`,
`event+positioning` or `event+multiple`. This is descriptive, not optimized sizing.

## Append-only evidence and reporting

Migration 30 adds `paper_event_evidence` (append-only classifications, enrollments, waiting,
decisions, duplicates and later-source viability) and a recoverable bounded confirmation
projection `paper_event_pending`. Existing nimble opportunity/thesis/intent/open/close and
forward-curve ledgers retain execution outcomes. Neither updates nor later corroboration
rewrite frozen entry knowledge. Waiting writes deduplicate by minute; consumed episodes
cannot reopen after restart. Rejected/unsupported/outside-universe reports remain recorded.

Frozen entry includes logical event/category, its own source report/provenance, first_seen,
publication/information/availability clocks, reference quote, variant/hypothesis, side/asset,
context/positioning/microstructure snapshots, confluence, objective/invalidation, window,
max hold, funding and policy ID. Source-specific clocks include source_publication_at,
source_revision_at, collector_observed_at, prism_first_seen_at, context_available_at,
first_evaluator_notice_at, evaluator_noticed_at, intent_at, quote_at, quote_received_at,
quote_available_at and fill_at. Work adds work_discovery_at, work_sent_at and
gateway_received_at where supplied. Null means unobserved, not zero latency.

`PRE_ENTRY_REACTION_CAPTURED` records reference_price, availability_price, fill_mid,
fill_price, signed_move_before_fill, signed_mid_move_before_fill and
signed_move_before_availability. Positive signed movement is expected-direction movement
already missed before execution. Subsequent position MFE/MAE and independent forward
curves remain separate; no causal claim is made from the event label alone.

`market paper nimble events --json` compares independent cohorts; `--run-id` selects one.
The dashboard's Event reactions page shows hypothesis/class/source/age/variant, latency,
missed move, exits, funding, costs and PnL, with preserved baseline IDs. Per-hypothesis
reports expose event/eligible/entry/rejection counts, win rate, gross/net, expectancy,
MFE/MAE, hold duration, fees/funding, invalidation/expiry reasons and confirmation waits.
No-reaction expiry is an explicit **false-positive proxy**, not a fact-checking verdict.
Paired actual entries expose extra delay/missed move and whether the immediate trade
closed before confirmation. Rejected confirmed opportunities and their forward curves
show missed opportunities without constructing fictitious profitable fills.

Source comparison lists first provider, corroborators, publication/discovery/availability
latencies and evaluator-time freshness/cost/confirmation/venue diagnostics against the
frozen remaining target. Missing executable data means unknown viability, not a claimed
counterfactual fill. Hourly Work may still qualify within protocol/regulatory freshness
if a viable target remains; it is not rejected merely for losing the discovery race.
Venue freshness intentionally excludes a 30–60m late outage report. One existing daily
Telegram summary includes cohort totals; no per-event/per-trade notifications are added.

Canonical rejection taxonomy: STALE_EVENT, LOW_MATERIALITY, UNSUPPORTED_EVENT_CLASS,
AMBIGUOUS_DIRECTION, ASSET_OUTSIDE_UNIVERSE, VENUE_UNAVAILABLE, UNECONOMIC_TARGET,
CONFIRMATION_MISSING, DATA_DEGRADED, DUPLICATE_EPISODE, CONFLICTING_EVENT, EXPOSURE_LIMIT.
Original execution details (funding/quote failure, conflict, min notional, exposure cap,
invalid geometry, cooldown etc.) remain alongside their event diagnostic mapping.

## Calibration, historical sanity and operations

[Phase 30 tests](../tests/test_event_reaction.py) calibrate immediate/no/delayed reaction,
false alarm/resolution, sharp protocol selloff, positive/failed rally, confirmation delay
and missed objective, causal reference/minutes/future fills, episode transitions, source
caps, flow/price/state invalidation, funding cost/credit/decay, conflicts, bursts, forward
research after exit and Hyperliquid entry/exit unavailability. Synthetic PnL never tunes
thresholds. No test event is injected into production.

Historical checks use a frozen small set for classification/provenance mechanics only.
The [Solana February 2024 outage report](https://solana.com/news/02-06-24-solana-mainnet-beta-outage-report)
confirms a mainnet block-finalization failure: a structured live severe cluster report
maps to SOL, while a postmortem/resolved/historical import cannot trade. The
[SEC January 2024 approval statement](https://www.sec.gov/newsroom/speeches-statements/gensler-statement-spot-bitcoin-011023)
is an official spot-BTC approval, but alone supplies no Prism-known contrary expectation:
it remains research-only under this narrow rule. The fixed 2024 status incident is checked
through its official archived API where available. Unknown historical Prism discovery,
component snapshots and executable quotes are not invented. These checks do not measure
historical profitability or prove source speed.

[Engineering benchmark](evidence/phase30/benchmark.json) measures quiet polls/active minutes,
10/100 events per day and a duplicate burst, two event accounts, CPU/wall/RSS, evidence and
execution rows/payload/database bytes. It uses synthetic geometry/quotes, excludes network,
writer waiting and original technical adapters, and is not a steady billing/latency SLA.
Prospective production evidence is authoritative; no activity quota or automatic graduation.
Existing infrastructure, public services, scheduler and £0/month data/API cost remain.

Production activation IDs/timestamps, complete validation and measured resource results
are recorded in [Phase 30 execution report](PHASE30_EXECUTION_REPORT.md).
