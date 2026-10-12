# Dynamic Hyperliquid universe v1

Implementation baseline: approved `main` / Runtime Optimisation Tranche A, commit
`f30040c`. No Runtime Optimisation Tranche B code or shared cross-run feature cache is used.
This document describes a paper-only successor, not an approved production activation.

## Rollout status and resource decision

Prepared stage: **RANKING ONLY**. Public snapshot discovery and isolated scratch ranking have
been exercised. Production remains on the approved six-asset deployment; no universe worker,
satellite collector or dynamic paper run was activated. No satellite has acquired prospective
warmup from this implementation. Ranking and warming on an authoritative runtime both require
an independently measured resource budget. Warming and execution remain blocked.

The read-only production measurement found 947,965,952 physical bytes free (~904 MiB),
a 702,558,208-byte database and 2,806,599,680 logical backup bytes. The existing backup
headroom formula needs 1,084,994,765 bytes. This is insufficient even before expansion.
No evidence, backup or production data was deleted. Initial rich satellites were reduced
from the proposed twelve to **six**, following measured adapter memory growth. The policy
was frozen only after this reduction. Storage is still inadequate for the reduced cap.

## Architecture and frozen identities

`hyperliquid_dynamic_universe_v1` is a frozen dataclass, persisted with its complete definition
and content identity. See `evidence/dynamic-universe-v1/policy.json`. Any change to thresholds,
normalization, costing, portability or lifecycle rules requires a new version/identity.

| Tier | Implementation | Limits |
|---|---|---|
| 0 Discovery | Public `perpDexs` then `metaAndAssetCtxs` for each dex: mark/mid, rolling 24h notional volume, base OI converted to dollars, funding, impact prices, contract configuration, first seen and inactive state | All discovered perps; no global bars, microstructure or hypothesis evaluation |
| 1 Candidates | Prospectively accumulated ten-minute snapshots; top cheap preselection receives REST book and three closed five-minute candles for trade frequency; full ranking and hypothesis readiness evidence | Thirty candidates; core/pins/active collection obligations can override the light request limit |
| 2 Active | Permanent core plus ranked satellites passing listing, execution, lightweight and economic gates | Six core plus six satellites; twelve total normally |
| 3 Risk pinned | All-run open/pending entries/exits, unsettled closed-trade accounting, required research outcomes and event lifecycle dependencies | Overrides ranking/demotion/subscription caps; never grants new satellite entries |

The core is BTC, ETH, SOL, HYPE, LINK and AAVE. Existing frozen breadth/context definitions,
research identities, scientific verdicts and feature versions remain six-market definitions.
Satellite additions do not change those semantics. A Tier 2 selection is distinct from
hypothesis-specific availability. Data intensity is allocated prospectively.

HIP-3/deployer markets are discovered and explicitly quarantined as `UNSUPPORTED_MARKET` in
v1: canonical names containing `:` need separately validated contract/fee/session semantics.
Unsupported markets are not silently omitted. First-seen time is conservative listing-age
knowledge, not an invented exchange listing timestamp. Removed/inactive assets and their prior
state remain in evidence; today's list is never used to reconstruct yesterday's universe.

## Ranking and normalization

Evaluate every **600 seconds**, separately from two-second quote/exit monitoring. All inputs
are causal market state; equity, capital availability, PnL and hypothesis outcomes are absent.

For finite observations, ascending midrank percentile is `(i+j)/(2*N)`, where `i` is the
zero-based first tied index and `j` its exclusive end. Ties share a percentile. Missing values
receive zero. Volume and OI use `log1p` before ranking. The normalization cohort excludes hard
failures other than new-listing age; new listings can build a provisional score but cannot
receive an executable rank. Core and satellites share normalization; promotion uses satellite
rank. Volatility does not bypass liquidity floors.

```
M = .50*p(RV) + .30*p(range) + .20*p(abs(return))
P = .40*p(log1p(volume)) + .30*p(volume_ratio) + .30*p(trades/minute)
A = .50*p(log1p(OI)) + .30*p(abs(log OI change)) + .20*p(abs(funding))
E = max(0, 1-spread/30) * min(1, lesser_side_10bps_depth/100000)
D = available(rv, range, return, volume_ratio, oi_change, trade_frequency)/6
score = M**.45 * P**.35 * A**.20 * E * D
```

Hard failures receive score zero; new listings may retain a provisional score. Tie breaking is
score descending, participation descending, canonical asset ascending. RV uses the latest
seven ten-minute snapshots: `sqrt(sum(log_return**2)*3600/elapsed_seconds)*10000`.
Range and absolute return use the same prospective interval. Volume ratio is current rolling
24h volume divided by its prior capture, **not** actual incremental trading volume.
OI change uses base contracts, avoiding mechanically treating price appreciation as new OI.
No volatility/OI acceleration estimator is claimed beyond these explicit components.

## Hard gates and economics

| Gate | Frozen requirement |
|---|---|
| Recent volume | At least $5,000,000 rolling 24h |
| OI | At least $2,000,000 |
| Spread | At most 15 bps; invalid/crossed quotes are degraded |
| Nearby depth | At least $25,000 on the weaker side within 10 bps of executable top |
| Trade frequency | At least 2 trades/minute from three closed five-minute candles |
| Freshness | Metadata and sampled quote age 0–180 seconds |
| Listing age | Seven days since first prospective sighting |
| Contract | Active native supported perp; venue available; leverage capability >=2x |
| Lightweight warmup | Seven healthy observations; no adjacent gap >900 seconds |
| Economy | One-hour RV >=3 times conservative round-trip cost |
| Promotion score | At least .35 |

Absent quotes may permit discovery/warming but never execution. Nonfinite inputs, unhealthy
sources, future timestamps, missing activity, stale data and unsupported configuration fail
closed. Exact venue sizing additionally requires integer size decimals in 0–8.

The frozen `depth_impact_conservative_v1` cost is:

```
slippage_bps = max(3, 20*100/depth_usd, venue_impact_bps, .05*rv_bps)
round_trip_bps = spread_bps + 2*(4.5 + slippage_bps)
```

The $100 reference is conservative for the current account's unchanged per-trade capital.
Satellite fills use fresh post-intent executable bid/ask with taker fee and the greater of
frozen entry classification or fresh quote slippage; never midpoint. Unknown satellite
classification is rejected for admission; a 100bps conservative fallback allows required
exit accounting. Existing core static economics remain unchanged. Satellite quantities use
venue decimals rounded down, retaining 20% notional and existing leverage assumptions.

Rejection evidence includes LOW_VOLUME, LOW_OI, WIDE_SPREAD, THIN_DEPTH,
LOW_TRADE_FREQUENCY, DATA_DEGRADED, DATA_STALE, NEW_LISTING_WARMUP, LIGHTWEIGHT_WARMUP,
MICRO_WARMUP, TECHNICAL_WARMUP, POSITIONING_WARMUP, FUNDING_UNAVAILABLE,
UNECONOMIC_EXECUTION, BELOW_RANK_CUTOFF, TIER_CAPACITY, MARKET_INACTIVE,
VENUE_UNAVAILABLE and UNSUPPORTED_MARKET, plus explicit portability reasons.

## Promotion, hysteresis, demotion and pins

Promotion requires satellite rank <=6 and score >=.35 on three consecutive evaluations,
all execution gates passing, and capacity. Missed evaluations (>900 seconds) reset streaks.
Promotion is effective at the actual evaluation time, never backdated. Old signals preceding
promotion cannot execute. Persist previous/new tier, effective time, components, rank, reasons
and the original promotion time, preserved through risk pinning.

Rank demotion requires rank >10/missing or score <.35 for six consecutive evaluations and
minimum Tier 2 dwell six hours. Hard safety failures bypass dwell. Strong candidates cannot
evict a sound incumbent inside its hysteresis band merely by swapping adjacent ranks.
Risk overrides demotion, marks demotion pending and retains collection. When the dependency
clears, safety/rank/dwell is evaluated again. Tier 3 satellites cannot open new positions.
Disabled/delisted/absent markets cannot admit new entries; required exit/accounting collection
continues. Safety is additionally checked on pending-entry and quote admission paths between
ranking evaluations; this is not a global sub-second discovery feed.

## Warmup and hypothesis portability

Exact per-hypothesis IDs, versions, verdicts and classifications are exported to
`evidence/dynamic-universe-v1/portability.json` (34 entries including six Phase 30 rules).
No strategy is retrained, rescreened or scientifically reclassified.

| Existing family | Satellite classification / availability |
|---|---|
| Fourteen technical hypotheses (seven long/short pairs) | PORTABLE_WITH_ASSET_DATA_GATE: same native hourly feature version and exact `technical_ready` primitives, previous/current indicators and fresh latest closed hour |
| Eight micro hypotheses: continuation_core, absorption_book, absorption_oi_rising, continuation_persistent, both sides | PORTABLE_WITH_ASSET_DATA_GATE: exact Phase 24A/24B quality and predicate gates |
| crowding_vulnerability, both sides | CORE_ONLY: Binance cross-venue corroboration/context unvalidated |
| oi_price_stall and oi_directional_pressure, both sides | NOT_YET_VALIDATED: prospective OI does not replace validated positioning adapter asset registration |
| Six Phase 30 event rules | CORE_ONLY: explicit frozen event-direction asset mappings |

Selected rich satellites fetch at most 45 days of public native hourly bars once, then bounded
two-hour overlaps. Backfill failure retries initialization; it is not mistaken for completed
warmup. Bars are only known at actual ingest; their existence does not fabricate past eligibility.
The exact technical readiness functions determine usable history, not a blanket bar-count claim.

Positioning warmup records at least two prospective hourly OI captures within 140 minutes;
OI is never invented retrospectively. Satellite settled funding must be observed, available by
the decision clock and no older than 80 minutes. Funding backfill retains observed availability.
No equivalent Binance corroboration is fabricated.

Microstructure requires **288 prior complete fifteen-minute windows**, excluding the current
window, plus the current complete window and exact feature-quality rules. Satellite history
must belong to the current rich collection segment. Large-print calibration remains the exact
prior-seven-UTC-day rule: at least three qualifying days, each >=720 non-gap minutes, and
>=1,000 prints total. Null large-print features remain unavailable where predicates need them.
Radar readiness does not bypass the exact adapter's current feature checks.

A bounded rich warming pool shares the **six** satellite slots with active satellites; it is
not six active plus six additional warm markets. Pins may exceed the pool for evidence/risk.
New listings with otherwise healthy economics may warm while their seven-day age gate runs.
Re-entry resets segment warmup, preserves old rows and records explicit subscription gaps.

## Collector lifecycle and memory retention

The DB writer publishes an atomic desired-set plan containing frozen policy identity, runtime
identity, evaluation timestamp and pins. The DB-free collector validates it, explicitly adds
or removes four subscriptions per asset, clears departed quotes, filters departed packets,
retains the core and reconnects with the complete desired set. Duplicate desired coins are
rejected. Running stale/corrupt plans retain the previous set; restart can retain a valid stale
plan so risk does not disappear while ranking is unavailable. Existing plans are honored on
restart even if the opt-in flag is removed. Re-entry waits for retirement revisions to drain.

Engine desired-set events are journal-replayable. Removed assets drain the final collected
minute and late-revision window; no invented continued minute collection. Re-entry records
`subscription_paused` GAP evidence. Add/remove spool events are ingested into the append-only
`microstructure_subscription_events` ledger with effective and ingest clocks and collector authority.
Live health covers current subscriptions, and stored health excludes deliberately demoted
satellites. A failed/unacknowledged satellite stays visible as unhealthy without repeatedly
reconnecting healthy core feeds; core watchdog behavior is unchanged. Fresh quote and exact
feature gates prevent execution on that unavailable satellite. Paused-history GAPs do not become false collector alarms during the first hour of
re-entry; feature eligibility still requires full exact completeness.

Old in-process large-print days/cache/emission keys are bounded to D-8 through D (a temporary
extra boundary day can exist before pruning). The oldest retained day protects prior-day late
revision calibration. Deterministic equivalence tests compare current/prior-day thresholds over
multiple rollovers. Persisted histogram/evidence rows are retained unchanged.

## Run identity, controls, admission and attribution

`paper_nimble_dynamic_v1` has a separate content-addressed execution policy/universe, frozen
portability and cost rules. Baseline `paper_nimble_v1` and Phase 30 immediate/confirmed cohorts
retain existing identities and six-market mappings. Dynamic plus event creation is rejected.
No new dynamic run exists in production. Account limits remain max three positions, 20% per
trade notional, 60% gross exposure and existing 2x leverage assumptions; max two satellite
positions adds a simple concentration safeguard. No broad correlation/theme model is claimed.

Dynamic simultaneous priority is RV/cost descending, tradeability score descending, then
canonical asset, hypothesis and signal identity. All factors are causal and frozen; DB iteration
order does not allocate capital. CORE/SATELLITE and complete entry universe context are stored
with opportunity/pending/trade evidence: rank/tier/score/components, market volume/OI/RV/funding,
spread/depth/costs, warmup and time since original promotion.

The existing three cohorts continue without expanding into three satellite universes. A future
active dynamic control adds one additional evaluation run with operation-local Tranche A
features; no cross-run Tranche B cache. The measured 12-market adapter costs roughly 1.3–1.5 CPU
seconds and ~640 MiB peak per isolated evaluation, versus ~.8 CPU seconds and ~348 MiB for six.
A six-market control has scientific value but its incremental computation/storage is not free.
Retain it only for a bounded initial seven-day comparison following activation, then separately
review continued cost and experimental value. This does not authorize stopping current runs.

## Market radar and operations

Read-only dashboard navigation adds Hyperliquid market radar: tier counts, ranks, core/satellite,
price, volume ratio, OI/change, RV/range, funding, spread/depth, score components, rich collection,
warmup, pins, rejection reasons and per-hypothesis history readiness. Paper tables identify
CORE/SATELLITE. No write controls exist. Old database snapshots degrade gracefully with a
not-activated message. A paper-only export without universe tables cannot show the radar;
the dashboard needs a snapshot containing the universe ledger once deployed.

Removed-but-pinned assets remain explicitly named in the radar and inspectable in the asset
command with absent market metadata and MARKET_INACTIVE, rather than disappearing from view.

Read-only CLI:

```
market universe status --json
market universe rank --json
market universe asset ASSET --json
```

`market universe refresh` is the explicit writer. `--warming` and authoritative ranking require
`PRISM_UNIVERSE_BUDGET` with matching policy, measurement <=24h old, >=30-day physical runway,
all DB/raw/spool/backup growth accounted, adequate rich asset capacity and worker CPU <30s/min.
Railway `PRISM_UNIVERSE_STAGE=ranking|warming` opts into the supervised ten-minute data worker;
default remains off. CLI paper register/create `--dynamic-universe` adds a separate successor;
creation requires fresh warming evidence and a genuinely usable satellite hypothesis.
Routine rank changes generate no Telegram messages.

## Evidence, runtime and storage

See `evidence/dynamic-universe-v1/benchmark.json`, `snapshot-sanity.json`,
`production-resources.json`, `ledger-cost.json` and `storage.json`. Public discovery saw **539** markets:
336 active, including 178 active supported native perps; 305 HIP-3 contracts were quarantined
and 203 contracts inactive (exclusions overlap). All first-seen assets need seven-day age and
prospective light warmup. There is no claim of current executable satellites or historical profit.

A public all-dex + bounded light snapshot required 72 requests, 288,851 response bytes,
36.1 seconds wall and .106 CPU seconds. Pure all-market ranking takes approximately 13–15ms.
Cold eight-day synthetic micro readiness benchmarks separately exercise actual expensive
predicates; cloned snapshot workloads are load fixtures, not valid satellite research histories.
Benchmark JSON records CPU, wall, peak RSS, reads/rows, zero adapter writes, collector events,
minute writes, compressed spool/journal bytes and synthetic network volume for 6/12/18/20/30.
Thirty is measured as light-only; full collection at thirty is a storage sensitivity only.

Real scratch decision persistence with thirty readiness audits measured .804 CPU seconds,
.742 seconds wall, ~196 MiB process peak, 162 SELECTs and two logical INSERTs. Each full
decision compressed to 60,188 bytes (including portability/warmup reasons), rather than the
~34KB pure-ranking payload. Storage uses this larger measured payload.

The 32-evaluation synthetic ledger replay exercised 2,034 prospective outcomes and a
five-hour retained decision window. Its final cycle measured 2.49 CPU seconds, 2.26 seconds
wall, ~362 MiB peak, 163 SELECTs and 92 logical INSERTs. Outcomes are committed as a batch;
partial outcome-write failure rolls back that evidence without altering the durable ranking.
This includes full payload decode/readiness work, not just the 15ms pure rank.

Live six-market collector telemetry measured ~174 MiB RSS, .53% of one CPU, 22.37 messages/sec
and 11,744 wire bytes/sec. Linear traffic extrapolation is a sensitivity, not measured production
traffic at twelve/eighteen. Shared deployment cgroup memory was ~2.19GB including page cache.
No Railway price/billing forecast is claimed; no external data subscription was added (£0/month).

At twelve rich assets, the conservative estimate is ~20.1MB/day permanent DB growth (~602MB/30d),
including full ten-minute compressed ledger/outcome upper bounds and ~4.4MB/day micro rows.
Raw archive extrapolation is ~78.7MB/day; synthetic spool ~30.3MB/day. Retained raw+spool reserve
is ~430MB. Up to eight full backups plus the live DB imply ~5.4GB/30d growth before links/rotation
benefits. Remaining-volume runway under that upper model is ~5.25 days, excludes existing base
research/trade growth and is **not a production forecast**. Current backup transient-space
failure independently blocks rollout. Even ranking-only ledger/outcome storage must be budgeted.
Do not activate by treating compressed/raw/spool/backups as free or by fabricating validation.

## Prospective research and rollback

Append-only gzip decision snapshots every ten minutes preserve all markets, listing/inactive
state, normalization inputs, rejected/eligible rank, component values, warmup, pins, transitions
and content identities. This compact full-snapshot choice is reconstructable without a delta
chain; measured growth is explicit. Same-clock conflicting decisions fail rather than overwrite.

For up to thirty ranked candidates, collect future 15m/30m/1h/2h/4h mark return, sampled range
and RV, recording actual sample time/lag and gaps. Ten-minute cadence means a 15m horizon may
first be observed at twenty minutes; no interpolation or tick-perfect extrema are claimed.
Core/satellite trade attribution supports later net-cost comparison. These outcomes never
change v1 weights or admission. Production prospective evidence starts only after rollout.

Rollback first disables new dynamic admissions/run activation and stops ranking refresh.
Keep the existing plan and collector while open/pending/accounting/research/event pins remain.
Do not delete the plan or unsubscribe risk to achieve a cosmetic rollback. Once obligations
are resolved, publish a causal core-only plan, drain revisions and stop the opt-in worker.
Preserve policy, outcomes, historical micro rows and old six-market identities.

Recommended next phase: **targeted runtime/storage optimisation**, justified by measured volume
headroom and backup amplification. This phase has not been started.

## Validation

An initial serial full pytest run passed 1,417 tests. The final complete file sweep passed
1,430 tests across four isolated groups (398/272/397/363). The final targeted rerun passed
130 tests, including four subsequently added safety cases; combined evidence verifies
1,434 distinct collected cases, with no failures, errors or skips. Existing Starlette
deprecation warnings remain. Ruff, formatting (405 files), shell syntax and git diff checks
passed. Exact module counts and current source hashes are recorded in
`evidence/dynamic-universe-v1/tests.json`.
The full suite includes Phase 30, Phase 29, Phase 28A, Phase 27, Phase 26A, Phase 24B/24A,
Phase 23 and Tranche A regressions. Synthetic tests cover deterministic rank/ties, liquidity
rejection, sleepy markets/activity breakout, promotion/demotion/dwell, safety collapse, listing
warmup, pin override, delisting/removal, causal history gaps, restart, dynamic add/remove/replay,
conservative cost/sizing, portability, paper identities and large-print retention equivalence.
