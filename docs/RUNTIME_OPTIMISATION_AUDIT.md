# Prism runtime and infrastructure optimisation audit

**Audit date: 2026-10-11. Planning only. No runtime/configuration changes, production commands, deployments, run changes or source changes were performed.** Local measurements used read-only archived data or disposable synthetic databases under `/tmp`. The only deliverables are this document and [profile.json](evidence/runtime-optimisation/profile.json).

## Executive summary

Optimise **before expanding the execution universe**, after the requested 24–48 hours of additional evidence. Start with repeated computation and query work inside existing decision boundaries; introduce the shared-state architecture in a second tranche. Design collection tiers alongside Dynamic Perp Universe. Preserve every experiment, causal cutoff, decision grid, execution quote check and single-writer guarantee.

The top five opportunities are:

1. **Reuse context inputs and immutable features.** Each Nimble run creates its own `Sources`; each asset's context rebuilds common event state, six-market breadth and seven macro series. Local six-asset adapter calls issued **222 technical / 204 positioning / 246 microstructure SELECTs**. A separate unprofiled six-asset context reconstruction cost **0.586 CPU-s**. Sharing compatible work across three runs can eliminate up to **two thirds of that work**, not two thirds of the whole service.
2. **Batch existence/science/positioning lookups and reuse indicator frames.** Microstructure signals retain a 15-minute grid, but minute triggers can cause **144 per-opportunity existence queries per minute** across six assets and three runs, mostly finding an already evaluated grid. Technical bars are loaded once per asset per adapter call, but `Frame.from_series` is rebuilt per technical hypothesis. Positioning repeats the same last-two-captures query six times per asset. These are specific, low-complexity targets.
3. **Remove repeated minute-cycle housekeeping across run children.** The three children are objects in **one Python process**, but independently reopen DuckDB, discover the same latest bars/OI, attempt the same spool ingest, load funding, validate policy definitions and retrieve quotes. Share immutable products and discovery results without consuming another run's receipts or moving its decision timestamp. Consolidating the entire writer cycle is a later, higher-risk step.
4. **Avoid heavy CLI startup for filesystem health checks.** Two enabled watchdogs launch **2,880 CLI processes/day**. Fresh local CLI import alone cost **0.982 CPU-s / 156 MiB peak RSS**. A lightweight entry path preserving checks, cadence, exits and alert behaviour is a better first target than changing watchdog frequency. Memory savings chiefly concern overlapping processes; this is not 156 MiB permanently saved per launch.
5. **Batch ledger/projection work without dropping evidence.** `event()` repeatedly selects and decodes account state and updates its projection for every event. Transaction-local state can reduce round trips while preserving sequence, payload and final projection. Minute account marks are real redundant-looking writes, but suppressing them changes evidence and account sequence. **Dirty-only marks are not in the strict zero-observable-change tranche.** Their payload volume alone is small.

Do not infer that free event coverage or full microstructure ingestion caused the observed CPU increase. Existing collector measurements are cheap; the expensive path includes repeated adapters, database lifecycle, research and contention. There is no evidence supporting Redis, Kafka, another service, a paid database, a language rewrite or replacement of DuckDB.

## Evidence scope and chronology

Evidence labels throughout: **M** = measured, **C** = code-confirmed, **E** = estimate/assumption, **U** = unavailable. Different hosts, times and workloads are not controlled comparisons.

| Evidence | Scope and limitation |
|---|---|
| [Phase 30 production runtime](../.worktrees/phase30/docs/evidence/phase30/production-runtime.json) | M: deployed revision `57e7a01fabcce940da00a370410dfd33a12ac4c7`; one-minute process CPU/RSS samples and sampled heartbeats, not service-wide averages |
| [Phase 30 execution report](../.worktrees/phase30/docs/PHASE30_EXECUTION_REPORT.md), [ledger counts](../.worktrees/phase30/docs/evidence/phase30/production-final-ledger.json) | M: three worker accounts, original 28 predicates per account, event cohorts add three each; three account marks/492 bytes and no event evidence in sampled minute |
| [Later Paper v2 handoff](../.worktrees/retire-paper-v2/docs/PAPER_V2_RETIREMENT_HANDOFF.md) | Records deployment of `a6b21bf…`, admission stop at **2026-10-10 19:48:53 UTC**, last verified **DRAINING**, one SOL position, historical opportunities frozen. **Current RETIRED status has not been verified in this audit.** |
| [Retirement pre-change measurements](../.worktrees/retire-paper-v2/docs/evidence/paper-v2-retirement/before.json), [SQL scratch measurement](../.worktrees/retire-paper-v2/docs/evidence/paper-v2-retirement/sql-before.json), [quiet worker sample](../.worktrees/retire-paper-v2/docs/evidence/paper-v2-retirement/worker-before.json) | M: useful pre-retirement resource evidence, not authority to retire/reactivate anything here |
| [Phase 27 source benchmark](../.worktrees/phase30/docs/evidence/phase27/railway-source-benchmark.json) | M: Railway scratch adapters, six-asset microstructure adapter 3.379 CPU-s; not whole-worker steady state |
| [Phase 29 benchmark](../.worktrees/phase30/docs/evidence/phase29/railway-isolated-benchmark.json) | M: 18 live endpoints in an isolated Railway scratch benchmark, production untouched |
| [Phase 24A collection study](../.worktrees/phase30/docs/MICROSTRUCTURE_COLLECTION.md) | M: older live smoke and storage replay; extrapolate cautiously |
| [New local profile](evidence/runtime-optimisation/profile.json) | M: read-only `data/remote/paper-dashboard.duckdb`, SHA-256 recorded; 865,340 intraday bars, 22,242 minute records, three revisions, 83 events, 184,329 funding rows. Archive ends around 2026-10-10 07:49 UTC and predates Nimble schema. Uses the common v2 adapters, not a production worker replay. |

The primary checkout is older (`42d5d76`). Runtime inspection uses `.worktrees/phase30` at `fc49770`; `git diff` found **no differences in src/deploy/config/scripts** from the measured production revision. Later retirement scheduler changes and dashboard code are identified separately. Worktree links are local evidence locations; the hashes and selected measurements in `profile.json` preserve provenance if those worktrees are later removed.

The user's four-run architecture is analysed as the Phase 30 baseline. The later retirement handoff means a new 24–48h sample must first establish the actual manifest, revision and v2 lifecycle. Do not compare a retired-v2 period with a four-run period and call the difference an optimisation result.

## Current resource profile

### CPU, memory and process topology

| Measurement | Result | Interpretation |
|---|---:|---|
| Phase 30 ordinary combined Nimble worker | 15.35 CPU-s / 60.004 s = **0.2558 vCPU** | One process containing three run objects; excludes separate collectors, gateway and CLI jobs |
| Worker endpoint RSS | 285.27 → **285.55 MiB** | +0.28 MiB over one minute is not a leak test |
| Earlier operator-overlap endpoint | **698.71 MiB** | A transient observed endpoint, not a proven maximum or steady baseline |
| Pre-deployment single-run worker | 3.99 CPU-s / 60.010 s; 231.66 MiB endpoint | One open position reported; different-time comparison, no controlled marginal attribution |
| Later quiet shared-worker sample | **0.05 CPU-s / 30.002 s = 0.00167 vCPU**, RSS 259.77 MiB | Demonstrates cheap between-cycle polling; not a full minute/grid or a verified flat-account period |
| Container `memory.current` | **1,198,063,616 bytes ≈ 1.116 GiB** | Point sample including other processes/cache; 285 MiB is not total Railway memory |
| Container limits | 8,000,000,000 bytes; `cpu.max=800000 100000` | Limits, not consumption |
| Paper v2 recorded 96 evaluations | mean **4.135 CPU-s / 3.603 wall-s**; mean reported process peak 505.49 MiB | At 96 evaluations/day, ≈397 CPU-s/day before imports; peak metric is not time-average RAM |

[Entrypoint](../.worktrees/phase30/deploy/railway/entrypoint.sh) gives **five persistent Python processes** with gateway/free-source flags enabled: microstructure collector, gateway HTTP, gateway ingest/research worker, free-source collector, Nimble worker. There are also tini, supercronic, supervisor shells, temporary boot catch-up and periodic CLI parent/step processes. If the gateway is disabled, the dedicated free-source ingest worker substitutes for its ingest role. There are **not three Nimble Python processes**.

A later recorded process sample showed gateway HTTP ~160.2 MiB, gateway worker ~231.0 MiB, free collector ~180.6 MiB, Nimble ~260.6 MiB and supercronic ~16.6 MiB. Its process filter omitted the `market microstructure collect` process and included a transient operator process; do not sum it as a complete clean service measurement. Full PID/PSS and cgroup time series remain U.

### Runtime path and responsibility map

Paths below are relative to the audited revision's `src/market_signal/` unless stated otherwise.

| Responsibility / path | Trigger and path | Resource character and waste candidate |
|---|---|---|
| OHLCV / market ingestion: `intraday/ingest.py`, `intraday/bars.py`, `data/http.py`, provider adapters | `intraday` at :01/:16/:31/:46; closed 15m/1h/4h bars, overlap/revision handling; daily/forward jobs separately | Periodic network + DB; clients reuse connections within process; CLI startup repeats; retain revisions and first-observed clocks |
| Microstructure collection: `microstructure/ws.py` → `engine.py` → `aggregate.py` → `spool.py` | Persistent WS, four subscriptions/asset; 250ms maintenance tick; finalised minute after 5s grace | Network/continuous CPU, bounded rolling state, fsync/gzip I/O; no DB; quotes published every 2s |
| Microstructure ingestion: `microstructure/ingest.py` | Scheduled every 15m and worker on changed spool; records + revisions + outbox | DB/I/O; offsets make repeated ingestion idempotent but not free; three child checks and fallback cron overlap |
| OI/funding: `context/positioning.py`, perp ingestion | Positioning :04 hourly; settled funding :05; OI jobs four/day; stream contexts supply current predicted funding | Network/DB; repeated snapshot/funding history reads dominate consumer duplication, not an API request per paper run |
| Context ingestion: `context/gateway/{server,spool,ingest,worker}.py`, `context/ledger.py` | HTTP durable receipt; worker checks every 5s; ingest under runtime lock, enqueue logical event/update | Long-running network/filesystem then single DB writer; no backlog means no ingest DB open |
| Free events: `context/free_sources/{collector,parsers,spool,ingest}.py` | 18 source-specific cadences, 1s due-check; shared gateway worker if enabled | Mostly network; persistent HTTP client; conditional requests/body hashes suppress downstream repeats |
| Older context providers: `context/service.py` and `providers/*` | News twice/hour; macro four/day; positioning hourly | Periodic network and DB; potential endpoint overlap with Phase 29 must be measured without merging distinct provenance |
| Dispatch: `ops/evaluation_triggers.py`, `paper/nimble/worker.py` | Durable outbox + filesystem hint; minute fallback discovers latest bar/OI | Event-driven + minute DB work; per-run receipts preserve independent consumption; repeated discovery/conflict inserts |
| Technical: `paper/v2/data.py:Sources.observations` | Only phase 22 on affected technical assets; 14 predicates/asset on hourly grid | 45-day 1h bars, revision-aware join, indicators; Frame rebuilt per predicate |
| Positioning: same adapter, Nimble override in `paper/nimble/data.py` | Only phase 23 on affected assets; six predicates/hour | Context + last-two captures repeated per predicate, extra max-capture query per returned observation |
| Microstructure hypothesis: same adapter → `research/microdir/features.py` | Phase 24 on affected assets; eight predicates on **15m**, not 1m, grid | Up to eight days of minute rows, missing-grid construction and seven-day prior normalisations; reusable inside one call, repeated across runs |
| Event hypotheses: `paper/nimble/events.py:evaluate` | Context event IDs plus enrolled pending candidates; three event predicates per cohort | Classifier/report/timing/ref-state repeated by cohort; pending confirmation/expiry still needs time/minute work without new event |
| Open risk/quote monitoring: `paper/nimble/worker.py:_action`, `engine.py:monitor` | DB-free cached position checks every loop, nominal 2s; DB only when action/minute/change/wake | Cheap quote arithmetic; DB state, funding, event lifecycle and marking add cost on working ticks |
| Account marking: `engine.py:_mark`, `event`, `_project` | Minute due or action; timestamp-keyed events per account/position | Small payload but multiple SELECT/INSERT/upsert operations and JSON conversion; flat accounts also marked |
| Evidence and research: `engine.py:research`, `events.py:evidence`, `context/work_research.py` | Run receipts/observations/lifecycle append; minute research catch-up | Causal records essential; repeated horizon data loads and pending queries; gateway research budget 2s starts **after** selection query |
| Reporting/dashboard | Daily scheduled briefs; offline dashboard snapshot | Brief writes/delivery bookkeeping compete under lock; Streamlit is **not started** by Railway entrypoint; later paper dashboard opens read-only snapshots |

## Primary hotspots and Python profile

Read-only adapter profiling used one DuckDB thread, six assets, a nonexistent audit run ID (therefore no pre-existing-grid skip), fixed as-of 07:50 UTC, and two repetitions. No observations were admitted or written. Timings include cProfile overhead. Bars were stale for the technical health gate; microstructure history was too short to warm up. These numbers measure reconstruction, not healthy signal throughput.

| Adapter | Predicates / observations | CPU-s, repeat 2 | SELECT count | SQL execute + result fetch wall-s |
|---|---:|---:|---:|---:|
| Technical | 14 / 84 | 1.143 | 222 | 0.460 |
| Positioning | 6 / 36 | 1.009 | 204 | 0.420 |
| Microstructure | 8 / 48 | 1.754 | 246 | 0.679 |

Separate measurements without cProfile: six context snapshots **0.586 CPU-s/call**; an already-evaluated full 168-observation grid returned zero observations for **0.0573 CPU-s/call**; loading/converting two days of settled funding, 288 records, **0.00289 CPU-s/call**. Query batching matters, but funding alone cannot explain 15.35 CPU-s/minute.

Largest observed cumulative suboperations in the local micro adapter profile: result-fetch wrapper **0.478s/246 calls**, `microdir.features.windows` **0.226s/6**, DataFrame `__getitem__` **0.195s/3,267**, pandas index access **0.178s/2,921**, `ledger.link_type_for` **0.154s/438**. These overlap and **must not be added**. Pandas conversion/filtering and repeated context work are evidenced hot paths; regex/parser micro-optimisation is not supported by this profile. The profile is not a flamegraph of the production worker, so it does not apportion that worker's 15.35 seconds.

Frozen policy work is also measurable: serialising `nimble.spec.definition()` produced 32,037 characters and cost **5.08ms/call** unprofiled. Twenty profiled calls performed 60 baseline `bootstrap()` calls, 1,440 canonical serialisations, 1,420 content-ID calls and 16,740 model validations. `policy_id()` already has `@cache`; **the repeated full definition construction/comparison in `engine.load()` does not**. Preserve validation against stored content; cache the expected canonical bytes and immutable bootstrap objects keyed by code/definition version, rather than removing integrity checks. This is a small concrete win, not the primary presumed cause of CPU usage.

## Multi-run duplication and hypothesis fan-out

### What repeats exactly

| Operation | Current sharing boundary | Repetition / opportunity |
|---|---|---|
| `Sources` and `_micro` | One child working tick only | Three independent objects; eight-day micro frames discarded/rebuilt; v2 uses another process/cutoff |
| Context per asset | Once within one `observations()` invocation | Rebuilt on another phase invocation, `updates()`, event admission, corroboration, another run |
| Common market context | No cross-asset sharing | **36 bar + 42 macro queries** for six asset snapshots in one adapter call; compatible three-run call repeats these 234 queries |
| Context event/update fold | No common snapshot index supplied by runtime | Same recent event set loaded/folded six times per adapter call; asset link selection differs |
| Technical bars vs indicators | Bars/BarSeries once per asset | `Frame.from_series(tech)` inside 14-hypothesis loop; Frame's internal memoisation is lost each time |
| Positioning | Context once per asset per adapter call | Six identical latest-two queries/asset; Nimble adds up to six identical max-capture queries/asset |
| Science checkpoint | Per micro hypothesis | **48 identical latest-checkpoint SELECTs** per six-asset micro invocation; parse once and choose member in memory |
| Micro features vs flags | `windows()` once per asset per invocation | `flags(f)` repeated for each of eight predicates; three runs repeat load/window derivation |
| Geometry | Per admitted opportunity | Same asset/cutoff minute window reloaded; ATR/fallback depends on hypothesis and must be keyed separately |
| Funding / state updates | Distinct open assets within one run | Same asset and rate history rebuilt across run children; unresolved settlement may require a longer boundary |
| Quote decode / session lookup | Latest file already shared by producer | Each child reads/decodes it, then `quotes_for_store` repeats authority/session SELECTs; entry quotes deliberately reread after slow work |
| Run/policy/account loads | Per engine helper | `load`, `hypotheses`, `monitor`, event evaluation and admission repeat definition/account SELECT/JSON work |
| Account mark | Always per account | Cannot merge independent equity/cash; identical underlying asset price inputs can be shared |
| Event classification/reference | Per event cohort | Same report/source timing/base market reference often identical; activation, variant, conflict/pending/entry evaluation differ |

**Cost bounds:** identical pure work shared by three runs has a **66.7% component ceiling**; including active v2 gives 75% only where its cutoff and inputs actually match. Under an explicitly assumed shareable fraction of 25–60% of the measured Nimble minute, the ceiling is **2.56–6.14 CPU-s/minute**, or **16.7–40% of worker CPU**. This is a planning sensitivity, not an achieved saving, and overlaps batching/context proposals.

### Actual targeting

| Incoming information | Current fan-out | Safe narrowing |
|---|---|---|
| Completed 1m record | Ingest enqueues micro trigger with affected asset; all three run children independently consume; phase 24 only, intersect six `COINS` | Batch per-run grid existence keys, then compute only absent keys. Already evaluated 15m grids need no frame rebuild. Keep each original trigger receipt. |
| Context event/update | Event IDs go to both event cohorts; original Nimble runs corroboration; source-to-phase mapping has no technical phase for context | Share immutable report decode/classification inputs; retain relevant event update transitions and all cohort evidence. Do not run all technical predicates. |
| Closed 1h bar | Minute fallback discovers latest per coin; phase 22 / affected assets / hourly ledger guard | Shared discovery result and bulk absence check, keeping exact first-observed/activation conditions |
| Positioning capture | Latest-per-coin discovery; phase 23 / affected assets / hourly guard | Share last-two rows and context primitives, not cohort admissions |
| Latest quote file | Cached open/pending/exit-pending positions checked; no normal signal-grid evaluation just because bid/ask moved | Decode once per unchanged file generation; validate each use at its actual decision time; group positions by asset |
| Minute clock / no new data | Each child can open DB, discover, load funding, monitor/mark, reconcile, research/degrade, inspect pending events | Skip only a proven empty responsibility using authoritative dirty/deadline state. Deadlines and time-based freshness are information too. |

The system does **not** evaluate every hypothesis on every quote. A minute micro trigger usually invokes eight predicates for each affected asset, but the ledger prevents rebuilding features for an already evaluated grid. At six assets/three runs this still implies up to **207,360 point probes/day** for the micro gate if a trigger arrives every minute, versus **13,824** new micro predicate evaluations/day. Hourly technical/positioning contribute **8,640** new evaluations/day; total **22,464**. These are complete-delivery model counts, not measured daily rows.

Triggers are grouped by source in each child, already coalescing affected asset sets. Pending selection has `LIMIT 1000` and independent receipts. A changed-since optimisation must use a committed monotonic discovery cursor plus recovery scan; **available_at alone is not an insertion cursor** because late inserts can carry older timestamps. Do not skip unpublished or unconsumed trigger IDs on crash, equal timestamps or backlogs.

## DB/query hotspots: ranked

Ranking combines measured aggregate call cost with production frequency/growth risk. It is not a ranking of individual SQL execution time alone.

| Rank | Query group / location | Evidence | Proposed action |
|---:|---|---|---|
| 1 | Context event fold, common market/macro + positioning/activity; `context/snapshot.py:context_snapshot`, `market_context` | Six context-event reads **86ms** including conversion; 42 macro reads **60ms**, 36 market reads **59ms** in micro profile; common event links processed 438 times | Shared causal raw inputs; same-time common market object; group link types once. Keep full payload semantics and cutoffs. |
| 2 | Minute history `microstructure/query.py:_rows` + reindex/derive + `windows` | Six minute SELECT/fetches **204ms**; six window calculations **226ms**, ~2.5-day archive | Share bounded versioned frames/windows; later incremental update with revision-aware equivalence tests |
| 3 | Opportunity existence checks, repeated `load`/account/positions | 48/84 point probes per adapter; quiet-grid 168 checks ~57ms; production v2 already-evaluated scratch grid still **376 SELECTs**, 1.779 CPU-s | Bulk select evaluated keys; transaction-local run/account/position state, retain admission guard |
| 4 | Science checkpoint join, positioning captures | 48 identical science joins **36.8ms**; 36 last-two-capture queries **19.5ms**; Nimble adds capture maxima | One checkpoint query + one decode; one capture result per asset; batched variants if beneficial |
| 5 | `load_bars` historical revision join, 45-day technical / context activity histories | Six technical-history execute/fetch calls ~44ms; broad activity another ~44ms in technical profile | Keep revision undo; shared frames and selected columns. Check predicate pushdown on revision subquery before any index. |
| 6 | `triggers.pending/discover` | Per-run LEFT JOIN receipts; discovery scans since activation and re-enqueues latest source keys | Batch shared source discovery, keep per-run pending/receipts; measure growth and plans on next snapshot |
| 7 | Work reaction pending + horizon reads | LEFT JOIN/GROUP BY/hash sort/limit; seven horizon checks per event/asset; selection query outside 2s budget | Batch existence/horizon inputs; durable next-due summary later, retaining retry/fairness/missingness rules |
| 8 | Event `source_timings`, `unavailable`, pending/enrolment/decision lookups | JSON membership search on trigger payload, source report folds, per-cohort repeats | Cache immutable event report revisions; optional indexed mapping only if plans establish material cost |

Local `EXPLAIN ANALYZE`: representative filtered intraday scan **1.5ms**, 728 returned rows; minute scan + order **5.3ms**, 3,707 returned rows; nonexistent opportunity predicate **0.3ms**, **EMPTY_RESULT**. The first two used sequential scans. These are neither proof that all rows were physically read nor justification for adding indexes. The nonexistent-run plan does **not** test an established-run hit. Full plans are in `profile.json`; they are representative shape probes, not exact plans for every top-ranked query. Actual callback timings above cover real adapter queries.

Existing indexes include `nimble_open(run_id,state)`, `nimble_signal(run_id,asset,signal_at)`, `context_free_event(event_id,received_at)` and `paper_event_run(run_id,recorded_at)`; relevant tables also have primary/unique constraints. Microstructure deliberately has no ART primary key: prior measurement was ~120 bytes/row, ~50% extra storage. At 30 assets, 1.296 million new minute rows/month would make such overhead ~156 MB/month before backups. Preserve single-writer uniqueness/conflict checks. Test a proposed index on a copy with representative hits, misses, growing receipts and write cost; do not recommend blanket indexing or repartitioning.

No SQL-level production profile is available for the shared worker. Report physical bytes from `/proc/<pid>/io` and cgroup/volume deltas separately from SQL statement/returned-row counts. `SELECT *` on minute/context tables and JSON-to-DataFrame conversion are material; narrowing columns is safe only after proving every consumer's required fields, missingness and canonical evidence are unchanged.

## Writer contention and scheduling

`ops/runtime.py:runtime_lock` serialises authoritative jobs using flock. Scheduled jobs hold it **across child command startup and network work**, not just INSERTs. Nimble and gateway ingest use `wait=0`, return BUSY and retry; cron jobs wait with a 2s polling sleep and job-specific limits. DuckDB read-only access from another process can still conflict with an open writer. Keep the single writer and existing lock release/recovery rules.

**Measured contention:** Phase 30 ordinary sample has 12 heartbeat observations but only **nine distinct heartbeat timestamps**; **five of nine** distinct samples are BUSY, four idle-OK. Original raw count was 8/12 BUSY and overcounts repeated stale heartbeat reads. Operator-overlap sample: four BUSY among ten distinct heartbeats. These are observations, **not a time-weighted contention rate, lock acquisition count or wait-duration distribution**. The sample interval includes scheduled v2 timing, but holder attribution is U. The sampling file can miss a whole successful cycle while a later child is still running.

Competing work: intraday/funding/positioning/RSS network jobs, v2 evaluations, gateway ingestion, minute reaction research, daily/weekly research checkpoints, backup/fingerprint and operator audits. The macro/positioning/network calls inside the coarse runtime lock can delay a 2s quote action without consuming Nimble CPU. Lower lock occupancy is a latency benefit, but **changing when data commits is also a timing change** requiring separate validation.

Safe first work keeps transaction and visibility boundaries while doing less work inside them. Later, compute pure derived results from an immutable extracted view outside the write transaction, then reacquire/validate input generations before applying. Never open a second competing reader on the live file or act on a cached state that changed during release. Longer shared multi-run transactions may reduce opens but worsen starvation and shift child cutoffs: not a free consolidation.

The Phase 30 schedule contains ~**403.14 top-level cycles/day** and up to **619.14 child steps/day** before lifecycle gates, plus 2,880 standalone health invocations/day when both watchdogs are on. Prospective gates can omit retired v1 steps; v2 retirement changes counts again. Jobs recur even when child work is idempotently empty. At the local 0.982 CPU-s import cost, watchdog imports alone imply **~2,828 CPU-s/day = 0.0327 vCPU** (E, host-dependent). All scheduler/child starts add potential overhead but should be measured, not multiplied blindly through skipped paths.

Do not merge schedulers merely for elegance. Retain HTTP isolation from DuckDB and acquisition isolation from the writer. A cheap module entrypoint for health checks or an existing process publishing equivalent health data is a candidate; an always-open writer connection would obstruct the current CLI topology. Preserve source polls, watchdog detection and alerts. Nonessential reporting/quality summaries may later use a slower cadence; outcome collection and health detection need separate treatment.

## Idle versus active cost; open-position monitoring

| Scenario | Evidence | What can be concluded |
|---|---|---|
| Unactivated / no work | Phase 27: 1,000 polls 0.00919 CPU-s; Phase 30 filesystem benchmark 0.0263 CPU-s/1,000 polls | Filesystem-only polling can be extremely cheap; excludes an active minute cycle |
| Active accounts, no synthetic events/positions | Phase 30 event benchmark: 60 logical minutes × two cohorts **2.922 CPU-s**, 122 ledger rows including setup, no event evidence | Quiet event branch ~0.0487 CPU-s/two-cohort minute. Correct daily extrapolation **70.13 CPU-s/day = 0.000812 vCPU**, not the artifact's `2.922/86400` figure for a one-hour sample. Excludes adapters/network/lock/open costs. |
| Between-cycle live quiet period | Later 30s sample **0.05 CPU-s**, flat RSS | Actual idle polling is cheap; does not prove no positions/signals/events or full-cycle idle cost |
| Ordinary live combined minute | **15.35 CPU-s**, three marks/no event evidence | All three accounts were flat at the nearby 19:23:56 ledger audit. The sampled minute had no position-event writes. This is the best available near-flat production interval, but market ingestion, earlier research obligations and contention still ran; no new event evidence is not a full no-information idle test. Cannot label all 15.35s waste. |
| One open position, older production | Single-run 60s sample **3.99 CPU-s**; two successful ticks 2.07 and 1.87 CPU-s; latter micro dispatch returned **zero observations** | Real example of cost with no new signal observations; source state, DB lifecycle and monitoring still ran |
| Synthetic 0 / 1 / 3 positions; 30 monitors, `mark=False` | **0.238 / 0.213 / 0.216 CPU-s**, zero ledger rows | Fixed monitor/policy overhead dominates tiny arithmetic; differences within noise, not evidence that positions save CPU |
| Same, `mark=True` each call | **0.312 / 0.436 / 0.614 CPU-s**; **30 / 60 / 120** ledger rows | Added marking scales with positions; three-position wall time 3.00s reflects local storage stalls. This intentionally overmarks versus minute cadence. |
| 100 event observations, burst | Existing Phase 30 scratch benchmark **18.50 CPU-s**, 10 event-evidence rows, 406 execution rows | Repeated same-event/update/dedup burst, not 100 independent actionable events |
| 100 separate synthetic events | **56.10 CPU-s**, 600 event-evidence rows, 1,802 execution rows | Active event work remains modest at low daily event frequency; excludes normal adapters |
| 500 synthetic signal decisions | Phase 27 **5.94 CPU-s**, 500 opportunity rows | Admission/ledger benchmark only; not the cost to derive 500 market signals |

New synthetic position measurements use one run, BTC/ETH/SOL, fresh injected quotes, zero predicted funding, fixed geometry, no network and no source loading. They confirm unit mechanics, not actual production rates. Current three-position production attribution, truly flat active-worker minute profile and distinct-event burst load remain U.

Within one run, `updates(assets)` already deduplicates non-pending position assets. `_action` uses cached positions and one quote list, so there is **not a DB quote query per position every 2s**. `monitor` handles pending fill, exit intent/fill, TP, invalidation, state updates, funding, thesis expiry, max hold and account guards. Funding is filtered against each position's entry/settlement; its resulting liability cannot be shared across positions. Share quote validation inputs, causal asset state and rates, then apply position-specific side/thesis/entry/expiry/capital rules. `_mark` and `monitor` reread position/account projections and rescan quote lists; a transaction-local asset map is straightforward.

There is also a duplicate mark attempt: worker passes `mark=minute_due or action`, then calls `_mark` again for `action and not minute_due` with the same timestamp. Event-key idempotency suppresses repeated ledger inserts, but recomputation/SELECTs remain. This is a **conditional** simplification: the second pass occurs after admissions/research and may inspect changed state or run the catastrophe guard; prove equivalence before removing it.

“No new information = almost no work” must include due expiry/settlement/research clocks. A flat, settled account with no pending opportunity/outcome, no new input and no deadline could avoid DB work using durable watermarks. Current minute marking, discovery, integrity checks and degradation semantics make that a second-stage design, not permission to skip all minute ticks.

## Quote handling

The requested spool-backed latest-quote cache **already exists**: `microstructure/live.py:LatestBooks` atomically replaces `latest_books.json` every ~2s. It contains per-asset books/current contexts and collector identity; it is not a DB tick ledger. Raw normalised micro events have their existing short-retention compressed journal, and final minutes enter DuckDB. Avoid adding another quote database/service.

`live_cache` checks role/runtime and publication time; quote validity checks positive finite non-crossed prices, `exchange_at <= received_at <= available_at <= now`, and both exchange/receipt ages against the frozen **3s** quote limit. Live context age is capped at 10s. `quotes_for_store` additionally checks active collector process/session rows and authority claim. Disconnect clears books. Pending fills require a quote **after the intent**, and entry quotes are reloaded after slow feature calculations. These are correctness constraints, not redundant checks to remove wholesale.

Proposed parsed-file cache: key `(runtime identity, collector session, file generation/inode, mtime_ns, size/content token)`, bounded to latest generation; cache bytes/decoded immutable records only. Re-evaluate age at **every use** and reread at existing entry/exit quote boundaries if generation changed. Do not use timestamp rounding or TTL-valid booleans. Stat metadata alone is conditional until atomic-generation collision/replacement tests pass. Restart starts empty and revalidates authority/session against DB before any fill. Never reconstruct executable bid/ask from minute bars; never persist a remembered “fresh” verdict. No trading check is delayed to save file reads.

At six assets/three runs, up to 90 child file reads/minute occur on 2s polling, plus working-tick/entry reloads. Decode-once can cut common reads/decodes toward 30/minute **only when the generation and requested visibility actually match**; some extra reads are deliberately required. File size and measured decode cost are U, so expected total CPU saving is small until profiled.

## Account marking, evidence and batching

Three account marks/minute extrapolate to **4,320/day, 129,600/30 days**, about **0.71 MB/day / 21.25 MB/month of payload** at the measured 492 bytes/minute. This excludes row keys, timestamps, WAL, projection writes and backups. It is not 21 MB of guaranteed file growth. `_mark` writes even a flat unchanged account; each event increments `ac.seq` and `_project` writes account state again. With open positions, per-position mark events contain mark/funding/MFE/MAE/quote receipt and precede the account mark.

Potential dirty-only savings: up to all flat-account mark payloads during flat periods, or `flat_fraction × 4,320` rows/day. One flat run is 1,440 marks/day. However removal changes sequence, time-series observations, updated-at and append-only record identity; position marks can change quote receipt/funding reserve even at unchanged price. Account catastrophe guards and later sizing read projected state. **Require explicit evidence-contract/research approval; do not list this as zero observable difference.** Cadence reduction has the same caveat. Shared price/rate inputs and fewer intermediate account SELECTs preserve those records and are safer.

| Evidence class | Treatment |
|---|---|
| Source receipts/provenance, first-known times, minute revisions/gaps, triggers/receipts, run/policy/hypothesis IDs | Essential causal/restart evidence: preserve exactly |
| Opportunity observations/rejects, cohort classifications, enrolments, confirmations, source latency/move missed, theses, fills, funding settlements, research horizons | Essential experimental evidence, including non-trades: do not drop apparently repetitive rows across cohorts |
| Ledger account/position marks | Preserve under strict tranche; later compression/alternative representation must retain replay equivalence and explicitly authorised evidence semantics |
| Heartbeats, per-poll diagnostic summaries, repeated status rendering | Noncritical telemetry may be aggregated/bounded, but health/alert cadence and failure visibility must remain unchanged |
| Exact duplicate IDs/retries | Existing conflict guards already suppress many; avoid repeated work before append without weakening conflict detection |

Phase 30's 100-separate-event scratch load produced **6.46 MB combined payload**, ~64.6 KB/event across both cohorts; this includes execution/lifecycle records, not just classification. The repeated-event burst produced only 169 KB. Preserve these distinctions when estimating growth. Three account marks did **not** demonstrate event-evidence bloat in production.

Exact batching candidates:

| Candidate | Expected operation reduction | Boundary/validation |
|---|---|---|
| Opportunity grid checks | 48 or 84 SELECTs → one keyed select per phase/run; potentially one for all compatible runs | Same `(run,hypothesis,asset,signal_at)` set; admission duplicate guard remains |
| Latest science | 48 identical queries → one; JSON decode once | Same as-of/completed-at and checkpoint selection |
| Position captures | 36 last-two + up to 36 max queries → six results or one bulk result | Same tie ordering, known-at and stale/warm gates |
| Common context | 78 common queries across six snapshots → 13 shared primitive queries, then possibly two bulk selects | Same six-market definition and seven series; batching must preserve each series ordering |
| Account/event projection | Multiple SELECT/upsert pairs per event → transaction-local fold + final projection writes | Preserve every ledger row, sequence, read-your-writes and intermediate state seen by guards; atomic rollback/recovery |
| Research horizons | Per-horizon repeated range/exists queries → one bounded data window and existing-key set per compatible event/asset | Keep each horizon's availability, endpoint delay, missingness and record ID |
| Minute ingest | Per-record uniqueness/run checks → staged bulk lookup/insert inside existing ingest transaction | Preserve revision ordering, conflicts, cutover, per-record available time, trigger emission and offset atomicity |
| Evidence append | Batch already-produced rows at same original transaction boundary | Do not wait to fill a batch; do not delay source receipt/fsync/notice or alter hash/sequence ordering |

Batch size by bytes and work budget, not unbounded day-long arrays. DuckDB already compresses column data and the raw journal is gzip; adding per-row JSON compression complicates queries/identity and is low priority. Offline lossless archival/backup compression is a separate reversible storage experiment with restore verification; no evidence deletion or retention reduction is proposed.

## Safe shared-state architecture and cache design

Recommended boundary:

```text
committed causal source rows + exact visibility cutoff + revision manifest
    → immutable asset/market primitive snapshot
    → versioned pure features / predicate inputs
    → per-run observation envelope, receipts, admission, economics and execution
```

The same asset/timestamp/feature version is **necessary but insufficient** as a cache key. Include venue, dataset/authority identity, source revisions/content hashes, interval/window boundaries, availability mode (`ingested` versus `finalized`), production cutover, feature/parameter digest, missing-data/quality policy and exact `known_at` or an equivalent visibility manifest. Include the frozen market-context universe; expanding execution assets must **not silently change the existing six-market breadth definition**.

For two run children at different wall-clock cutoffs, share underlying immutable history where valid, then independently filter/build time-sensitive envelopes. Do not replace their cutoffs with the beginning of a common cycle. `Sources.now`, context `as_of`, snapshot hashes, freshness/expiry, event report visibility and current quote generation may differ even within a minute. Current evidence IDs often include time; rounded keys would change them.

Content-addressed **input manifests and feature results** would help audit cache hits, deduplicate immutable content and support deterministic replay. Prefer bounded in-process caches first. A new persisted snapshot table that hashes/writes every second may cost more than it saves. Reuse existing minute content hashes/revision identities, context snapshot identity and frozen definitions; preserve exact canonical JSON bytes. Pandas frames and nested dicts are mutable even when their containing dataclass/model is frozen: use read-only arrays/immutable values or private copies at mutation boundaries.

Keep run-specific: policy/universe/activation and lifecycle, opportunity/trigger receipts, cooldown/episode gates, direction-dependent geometry overrides, cohort immediate/confirmed state, scientific admission status, account cash/equity/exposure, funding allocation, sizing/risk, intent/fill times and executable quotes at those times, thesis/invalidation/TP/expiry and evidence identities. Share math, not final admission or account marks.

Every cache below is optional future work, not implemented here. “SAFE” means equivalent for the same logical inputs and timestamps; all remain subject to replay gates.

| Candidate / safety | Exact key | Lifetime and invalidation | Restart and causal safety | Expected saving |
|---|---|---|---|---|
| Frozen strategy/bootstrap/expected policy bytes — **SAFE immutable cache** | code revision + full definition/feature/parameter digest | Process lifetime; new code/version creates new key; never expose mutable dicts | Recompute/validate at startup; still compare DB stored bytes/identity | ~5ms per repeated full definition serialisation measured; avoids repeated catalogue/model construction |
| Technical Frame and cached primitives — **SAFE immutable cache** | venue/asset/timeframe + ordered visible bar-content/revision manifest + exact 45-day window + feature version | Within adapter first; bounded cross-run LRU by input digest later; new bar/revision/window anchor invalidates | Rebuild from causal bars; gap segments, EMA seed and ATR rounding identical | 14 Frame builds → one per asset per compatible input; Frame's own memoised EMA/ATR/rolling arrays finally reusable |
| Micro frame/windows/flags — **SAFE immutable cache** | asset + minute content/revision manifest + window boundaries + cutover + availability mode + feature version | Bounded one/two current windows per asset; invalidate affected historical normalisation on revision/gap change, prune after eviction | Empty on restart; use DB-visible ingest times, not file-finalised minutes | Up to 18 → six compatible window builds for three runs; eight flag-array builds → one per asset |
| Context raw event fold, links and macro/breadth — **SAFE within timestamp** | exact known-at + committed event/update/link/bar/macro manifests + snapshot/taxonomy versions + breadth universe | One decision boundary; raw immutable histories can persist bounded, time-sensitive views reconstructed | Rebuild from DB; future schedule/expiry and freshness move with time even without new writes | Remove five of six common market rebuilds and duplicated event folding; local context cost ~0.586 CPU-s/six |
| Full context snapshot — **SAFE within timestamp**, cross-time reuse **CONDITIONAL** | asset + exact as-of + all above + positioning/activity/freshness configuration | Only identical as-of/input view; no minute TTL | Reconstruct on restart; same canonical `snapshot_id`; changes in as-of alone miss cache | Repeated context calls at same cutoff → one; actual inter-run hit rate to measure |
| Latest science checkpoint — **SAFE within timestamp** | study + as-of/completed-at cutoff + committed checkpoint/result ID/digest | One source evaluation; invalidate on result/checkpoint commit or cutoff transition | Read-only reconstruction; extract each hypothesis's member without mutation | 48 SELECTs/decodes → one per six-asset call |
| Positioning/funding histories — **SAFE immutable cache**; current derived freshness **SAFE within timestamp** | venue/asset + time range + captured/ingested/available cutoff + row manifest/version | Cycle-local first; invalidate on settled funding/late accounting input/new capture; extend for old unresolved positions | Rebuild from authoritative rows; each position applies its own entry/end/sign/reserve | Six predicate queries → one capture result; up to three duplicate history reads → one compatible read |
| Geometry/reference inputs — **SAFE within timestamp** | asset + source phase/geometry version + observation ATR/feature identity + cutoff + minute/bar revisions; event reference includes first-seen and event revision | Same decision boundary; bounded frozen event reference thereafter | Preserve fallback choice, health and pre-event reference; never substitute another hypothesis's ATR | Avoid repeated 15m/2-day loads for same input, magnitude depends on actual admissions |
| Parsed latest quotes — **CONDITIONAL** | authority/runtime/session + atomic file generation token | Latest generation only; age rechecked each use, invalidate on disconnect/session/authority change | Start empty; DB session verification at existing fill boundary; retain later-than-intent requirement | Up to two thirds common child decode work; small until measured |
| Run/account/positions/evaluated keys — **SAFE within transaction**, cross-transaction **CONDITIONAL** | run + transaction generation + ledger seq + immutable definition ID | One transaction; update local projection after every logical event, discard on rollback/commit | DB/ledger authoritative, crash replay identical; admin stop/unsettled accounting never cached across unknown commits | Multiple helper SELECT/JSON/upsert round trips removed |
| Event report/classification inputs — **SAFE immutable cache** for input decode; output **SAFE within timestamp** | logical event + report/update revision + classifier/expectation version + known-at + source provenance | Until new update/expectation or deadline; pending cohort state remains outside cache | Rebuild reports; retain distinct classification evidence per cohort and source timing | At most half common two-cohort classification/reference work |
| Freshness verdict, admission decision, account balance, last quote by asset with TTL — **UNSAFE for trading path** | Inadequate keys omit clock/state/provenance | Do not introduce | Can permit stale fills, skip stop/expiry or conflate independent accounts | No admissible saving |

Incremental rolling features are a **P2 follow-up**, after memoisation. EMA/ATR warmup and gap resets, variance ddof, prior-only percentile membership/ties, window eviction and floating-point summation order must match existing implementations. A different summation order can change a threshold decision and a content hash. Keep original feature identities only if outputs are identical; otherwise it is a separately approved feature/research change. This applies equally to SMA, local extrema, realised volatility, volume stats, OI deltas and funding normalisations.

## Free event collectors and microstructure collection

### Free sources

Phase 29's Railway scratch benchmark exercised all **18** sources; **18/18 warm responses were HTTP 304**, with zero warm body bytes. Configured request ceiling totals **10,656/day** before jitter/backoff. Collector process CPU for that cold/warm experiment was **0.480s**, ingest **0.195s**, idle tick **29µs**, peak RSS **173.82 MiB**. These are short samples, not continuous production CPU or 24h bandwidth. One-second idle due-check extrapolates to ~2.5 CPU-s/day, excluding actual polls.

Code already uses one `httpx.Client`, gzip, four bounded connections/keepalives, conditional ETag/Last-Modified, daily unconditional refresh, body-hash comparison, per-item ID dedup, bounded 2 MiB response, backoff and durable state before validators advance. A 304 skips parsing; a same-hash 200 skips parsing/downstream work too. XML/JSON parsers operate only when content changes, with no expensive model/parser service initialisation evidenced. Known IDs are capped at 1,000/source; hourly counters retain ~24h. A ThreadPoolExecutor is constructed on each tick, but threads start only for submitted work; idle measurement is already tiny. Reusing that object alone is **NOT WORTH IT** without a contrary profile.

Distinct Phase 23 polls and Phase 29 collectors may hit related official endpoints. Inspect exact URL/headers/body/provenance totals before estimating duplicate network load; **do not merge sources, dedup logical events differently or change cadence/coverage in this optimisation**. Their source behaviour and distinct knowledge clocks are experimental inputs. Keep all 18 sources.

### Phase 24A

Measured older six-asset collector: **21–25 messages/s**, ~13.3 KB/s inbound, ~40 normalised events/s, parse ~21–24µs/message, applied-event p50/p99 4.3/21.7µs, **3.84 CPU-s / 857s = 0.00448 vCPU**, max RSS ~167 MB for CLI-launched process. Synthetic throughput ~228k fills/s demonstrates headroom, not a load SLA.

Aggregation uses all collected primitives; book statistics use a fixed 1s time grid, with native feed receipt, depth, trade coverage and gap/revision rules. Spool fsync per record measured p50 5.5ms/max 218ms on WSL; 300-minute catch-up finalisation ~65ms. Older ingest measurement: 156 records/89ms, duplicate reingest ~2ms; production-cadence replay up to ~1.4s/15-minute batch, one loaded-host outlier 17s. Phase 27 Railway scratch ingest: 36 records, **0.207 CPU-s / 0.141 wall-s**. Separate aggregation versus parsing/spool CPU in current production is U; status already exposes useful counters.

This is separate from the considerably more expensive **consumer feature derivation**, whose six-asset adapter cost 3.379 CPU-s in an older Railway scratch run and 1.754s in this local partial-history profile. Do not reduce feed fidelity to compensate for repeated consumer calculations. Same-transaction ingest batching can help after query duplication; changing fsync timing, finalisation grace or ingest visibility is a behaviour/durability change.

Memory guards: three-minute book retention, 20-minute trade-ID history, bounded minute revisions, 4,000-item latency/proc deques, 60s message counters pruned at status, minute journal flush, and retention of histogram files on disk. Disk-failure pending records are bounded at **100,000**, still a potentially large transient. **Unbounded lifetime state is present:** `LargePrints.days`, its `(coin,day)` `_cache` and `Engine.lp_emitted` have no in-process date eviction in the inspected code; pruning old histogram files does not prune these dictionaries. Only seven prior days are read for a threshold, but a continuously running collector retains older days until restart. Worker `children` also retains created run objects. This is a code-confirmed growth risk, not a measured leak rate. Future eviction must preserve current/prior-day requirements, revision inputs and persisted threshold identities; measure bytes/day before prioritising. Burst trade lists are rate-sensitive even when time-bounded. Spool restart `read_lines` loads the complete unread file portion and materialises records; bounded chunking is a future catch-up-memory candidate if measured peaks justify it.

## Reporting/dashboard isolation and memory

Railway's entrypoint starts no dashboard. The later dashboard's `dashboard/paper_data.py` uses read-only connections to an explicitly selected file; caches are 30s with bounded entry counts and local-file revision keys. It should continue consuming **offline verified snapshots**. Read-only is not synonymous with lock-free against the production file. Daily scheduled briefs still execute on the runtime and can generate/report ledger data; keep required delivery semantics, move optional exploration to a downloaded snapshot. Reuse existing verified backups for dashboard refresh; do not trigger full production fingerprint/copy work on every browser refresh.

The ~285 MiB Nimble RSS can be explained structurally, but cannot be numerically decomposed from endpoints alone:

- Fresh local worker import: **151.49 MiB**; broad CLI import: **156.45 MiB**; gateway-worker import **185.82 MiB**; free-source collector import **148.14 MiB**. These are separate fresh-process local peaks, not production allocations.
- Remaining worker memory plausibly includes DuckDB buffers/threads, loaded contexts/bar histories, pandas/NumPy arrays and allocator retention. A measured BTC frame held **3,693 rows × 89 columns = 5.24 MB deep memory**. Eight full days at comparable per-row size would be ~16.3 MB/asset, ~98 MB for six or ~491 MB for 30, **before temporary copies** (E). `_micro` retains all touched assets within one Sources instance.
- New local profiling kept a read-only DB connection across runs and ended around 441 MiB; this is not comparable to a worker closing connections between ticks. Earlier Railway source benchmark fell **530.2 → 224.3 MiB after close**, evidence that DB/working buffers matter.
- Child objects are sequential, so peak frames need not be three simultaneously retained copies. A naïve persistent cache could **increase**, not reduce, steady RSS; use a byte budget/LRU and track allocations/hits. Prefer small computed feature products over keeping every full historical frame forever.
- Collector memory is a different process. Importing its lightweight `microstructure.ws` module alone used **34.89 MiB**, while CLI startup used 156 MiB locally. A direct equivalent entry path may save O(100 MiB), but it must preserve CLI configuration, authority and startup checks and needs production confirmation.

No harmful leak rate is proved; the unbounded large-print history above is a concrete retention risk. The return from a 698.7 MiB observation to ~285 MiB and later ~260 MiB supports transient/released memory. It does not rule out slow growth in event history, run objects, outcomes or caches. Measure cgroup working memory, per-PID RSS/PSS and idle-after-cycle slope for 24–48h, including daily research and backup peaks; do not treat `ru_maxrss` as current memory. Report resident baseline, cycle peak and retained post-cycle memory separately.

## Paper-run cost and baseline retirement analysis

No baseline is stopped by this audit. Shared acquisition/funding/research/HTTP memory is not saved merely by removing one paper account.

| Run removed, hypothetical | Direct resource saving | Evidence lost / qualification |
|---|---|---|
| Paper v2 fixed horizon | M: 96 evaluations × mean 4.135 CPU-s ≈ **397s/day = 0.00459 vCPU ≈ $0.092/month CPU**, plus imports/briefs and short-lived RAM. Two CLI imports per eval at local 0.982s would add ~189s/day, **$0.044/month** (E). Fewer ~3.6s mean evaluation lock holds, potentially more useful than dollars. | Loses continuing fixed-horizon comparator/new opportunity/outcome evidence. Later handoff already records admission stop/draining; any remaining avoidable cost must be measured post-retirement. **Keep hourly funding** consumed by Nimble and outstanding accounting/research. |
| Original `paper_nimble_v1` | U exact marginal. Symmetric attribution ceiling from busy sample: **15.35/3 ≈ 5.12 CPU-s/min = 0.0853 vCPU ≈ $1.71/month CPU**; actual could be far lower and workload-dependent. Removes one copy of 28 predicates, per-run receipts/marks/research; shared Python base remains. | Loses original execution-policy control and comparison with event-enabled accounts. Earlier 3.99s single-run sample is not an isolated removal experiment. |
| Immediate event cohort | Same **$1.71/month CPU symmetric allocation**, not measured savings; extra event/pending/position work varies | Loses event-immediate arm, its original 28 controls and paired latency/move-missed evidence |
| Confirmed event cohort | Same symmetric allocation; confirmation costs depend on pending count | Loses confirmation arm and immediate-versus-confirmed comparison; do not assume confirmation always costs more without measurement |

Keeping all three Nimble accounts preserves experimental integrity at a shared-worker CPU-equivalent **$5.12/month only if the sampled minute persisted continuously**. That figure excludes their shared process memory and the rest of the service. Shared pure computation is preferable to retiring controls for such uncertain savings. Per-run removal does not automatically release historical tables or backup storage, and draining positions/settlements can retain monitoring work.

If v2 is already retired at the next observation, record that and use three-run measurements for implementation comparisons. Do not reactivate it for profiling. Run hypothetical removal/before-after replay on scratch copies only.

## Dynamic-universe scaling model: 6 / 12 / 20 / 30 assets

These are **engineering scenarios**, not a request to change frozen universes. Current code hardcodes six `COINS` and per-asset execution settings; adding assets is separately governed universe/research work. Assume three concurrent Nimble accounts retain 14 hourly technical, six hourly positioning and eight 15m microstructure predicates; same traffic mix/history depth per asset, no change to open-position limits. Event bursts and daily research are additional.

Let `C6 = 15.35 / 60.004 = 0.2558 vCPU`, asset-sensitive fraction `f ∈ [0.25,0.75]` (unmeasured). Then `C(N) = C6 × [(1−f)+f×N/6]`. Include all-linear extrapolation as a stress sensitivity, not an upper bound: event-history growth, lock queues and feature warmup can be worse. Memory illustration `150 + 135.55×N/6 MiB` separates a rough import floor from scalable working memory; it is **not a measured forecast**.

| Measure | 6 | 12 | 20 | 30 |
|---|---:|---:|---:|---:|
| Nimble worker vCPU, sensitivity range | 0.256 | 0.320–0.448 | 0.405–0.703 | 0.512–1.023 |
| Worker all-linear vCPU stress | 0.256 | 0.512 | 0.853 | 1.279 |
| Illustrative worker RSS MiB | 286 | 421 | 602 | 828 |
| Full-micro WS messages/s, older mix | 21–25 | 42–50 | 70–83 | 105–125 |
| Full-micro collector vCPU, older mix | 0.0045 | 0.0090 | 0.0149 | 0.0224 |
| New micro minute rows/day | 8,640 | 17,280 | 28,800 | 43,200 |
| New baseline predicate evaluations/day, three runs | 22,464 | 44,928 | 74,880 | 112,320 |
| Micro ledger existence probes/day at minute wakes | 207,360 | 414,720 | 691,200 | 1,036,800 |
| Independent micro feature builds/day, three runs, full grids | 1,728 | 3,456 | 5,760 | 8,640 |
| Same-input shared micro builds/day, ideal | 576 | 1,152 | 1,920 | 2,880 |
| Technical Frame constructions/day before reuse, full hourly grids | 6,048 | 12,096 | 20,160 | 30,240 |
| Same-input shared Frame constructions/day, ideal | 144 | 288 | 480 | 720 |
| Live-book file publications/day | 43,200 | 43,200 | 43,200 | 43,200 |
| Account marks/day if still flat & minute-marked | 4,320 | 4,320 | 4,320 | 4,320 |
| Micro DB growth/month, old 15m ingest replay, GiB | 0.14 | 0.28 | 0.47 | 0.70 |
| Micro spool MB/day / raw gzip MB/day, older mix | 15.6 / 55 | 31.2 / 110 | 52 / 183 | 78 / 275 |

The technical-frame row assumes all hourly inputs exist and each predicate constructs its Frame; actual observations skipped/empty reduce it. Sharing cannot reduce per-run evidence count. Ideal feature counts require identical visible input manifests, which different child cutoffs may not provide. Paper v2, if still active, adds **7,488 predicate evaluations/day** at six assets and 576 micro builds/day, but not another WS subscription set. Full minute rows grow with full-collection assets, not number of paper runs.

Quote risk arithmetic scales with **open/pending positions P**, not automatically with all execution assets: approximately `30×P` cached checks/minute at the nominal polling rate; all-asset quote decoding/file size scales with N. If P remains three total, checks remain ~90/minute. If a hypothetical workload has one position per asset per run, checks become 540/1,080/1,800/2,700 per minute; that is a workload sensitivity, **not an authorised sizing/risk expansion**. Every working tick can delay the next one: current loop sleeps two seconds **after** sequential child work, so “2s” is not an end-to-end latency guarantee.

At a common due grid, new opportunity/evidence payload is `evaluations × measured bytes/evaluation`; rates depend heavily on context payload and signals. Measure this coefficient in the next snapshot rather than borrowing event-burst payload sizes for technical evidence. Trigger source rows are roughly N/minute when every asset finalises, with up to 3N receipts/minute; revisions/restarts/context add more. Inserts attempted by discovery also consume work even when `ON CONFLICT` writes no new row.

Storage is a more immediate scaling risk than collector CPU. Older measurement has ~0.14 GiB/month of micro DB growth at six assets. At 30 this is ~0.70 GiB/month **before repeated full backups**: live plus six copies could eventually represent ~4.9 GiB/month of incremental micro history across copies, though backup ages/rotation make the trajectory uneven. The recorded volume is 5 GB; actual free space now is U. Do not promise months of runway from October 7's disk sample. Keep existing evidence and backups; quantify compression/offline verified archival options and a byte budget before increasing coverage. No paid-volume recommendation is made.

### Microstructure scaling strategy and tiers

| Tier | Scope | Data responsibility / promotion contract |
|---|---|---|
| 0 | All listed Hyperliquid markets | Cheap batched metadata/listing state; no per-market process or full books. Refresh cadence belongs to future universe policy. |
| 1 | Candidate markets | Batched price/volume/OI/funding plus lightweight quotes/trades where required; track causal first-observed and data quality. Lightweight data cannot manufacture depth or historical micro features. |
| 2 | Active execution and deliberately warmed candidates | Full required features and Phase 24A where their hypotheses need it. Keep current six/core fidelity. Admit satellites only when their frozen data prerequisites are satisfied. |
| 3 | Open/pending/exit-pending or unsettled-risk assets, regardless of ranking | Pin required full state and executable quotes; highest required existing monitoring cadence. Do not unsubscribe on ranking demotion while an open position, pending fill or accounting/research dependency needs the data. |

At an execution/candidate target of 20, retaining six full-micro assets instead of collecting all 20 saves **70%** of asset-proportional full-micro stream/storage/derivation work; at 30 saves **80%**, at 50 **88%**. Keeping ten full assets instead gives **50% / 66.7% / 80%** respectively. Savings are before light-tier overhead and do **not** apply to fixed process/import memory or total worker CPU. Keeping only six full assets also means the other assets cannot immediately run hypotheses requiring full Phase 24A data.

For example, 30→10 full assets would reduce older-mix message rate ~105–125→35–42/s, minute rows 43,200→14,400/day, raw gzip ~275→92 MB/day, estimated micro DB growth ~0.70→0.23 GiB/month. Thirty→six would preserve today's collector baseline. These numbers assume comparable asset traffic; volatile event-driven satellites can exceed the mean.

Dynamic subscribe/unsubscribe is a **future data-availability policy**, not zero-semantic optimisation. Normalised micro features require **288 prior complete 15m windows (three days)** within a seven-day history; large-print thresholds require three qualified prior days, at least 720 minutes/day and 1,000 prints. Cold admission at the moment of an event cannot recreate this history. Warm a bounded candidate set prospectively, record promotion/demotion and gap boundaries, use dwell/hysteresis consistent with the future policy, and preserve research outcome horizons before unsubscribe. Tier 3 is an override on other tiers, not merely a bigger rank. Do not weaken current completeness/warmup or substitute light features under existing feature IDs.

## Railway cost implications

Current published **container** rates checked for this audit are CPU **$20/vCPU-month**, RAM **$10/GB-month**, volume **$0.15/GB-month**, network egress **$0.05/GB**. Hobby includes $5 of usage against its subscription; do not add $5 again to a usage estimate above that inclusion. These are not the separate VM/sandbox rates. [Railway pricing](https://docs.railway.com/pricing/plans).

Use `monthly CPU equivalent = 20 × total CPU-seconds / observed wall-seconds`; `memory equivalent = 10 × average billed GB`. Dollars here avoid an unverified GBP exchange rate and exclude tax. RSS/cgroup memory are only proxies for billable usage; Railway's actual service usage/credits must establish the bill.

- Busy-minute worker CPU equivalent **$5.12/month**; 285.55 MiB = ~0.299 GB, **$2.99/month memory equivalent**. Combined **$8.11/month for this worker alone if sustained**. It is not a current service bill.
- Point-sampled cgroup 1.198 GB would represent **$11.98/month memory equivalent**, plus CPU of all processes. It must not be extrapolated as the average without 24–48h measurements.
- Removing 1 CPU-second of work per minute saves about **$0.333/month**; saving 100 CPU-s/day saves **$0.023/month**. Saving 100 MiB continuously is about **$1.05/month**. Engineering effort should be proportional.
- A 25–60% shareable fraction with two thirds eliminated yields **$0.85–$2.05/month worker CPU equivalent** at the sampled burden. Watchdog import elimination has a local-model ceiling **$0.65/month CPU**, plus transient memory/overlap benefits. These savings cannot be summed with every overlapping batching estimate.
- Phase 24A older collector CPU equivalent is only **$0.090/month** at six assets, **$0.45/month** at 30 under linear traffic. Its ~167 MB process memory can cost more than its CPU; backups/volume headroom matter more than stream CPU at these rates.
- Incoming WS/feed bytes are not outgoing Railway egress. Snapshot downloads are: a ~343 MiB backup once/day is ~10.8 GB/month, ~$0.54 egress (E). Avoid frequent full downloads just to refresh a dashboard; use scheduled verified backups and display staleness.

| Assets | Worker CPU monthly sensitivity | Illustrative worker memory monthly | Combined worker equivalent |
|---:|---:|---:|---:|
| 6 | $5.12 | $2.99 | $8.11 |
| 12 | $6.40–8.95 | $4.42 | $10.81–13.37 |
| 20 | $8.10–14.07 | $6.31 | $14.41–20.38 |
| 30 | $10.23–20.47 | $8.68 | $18.91–29.15 |

Actual service totals need average gateway/collector/cron/research memory, all CPU and storage/egress. All-linear 30-asset worker sensitivity is ~$34.26/month including illustrative RAM. None is a forecast precise enough to justify buying infrastructure. Use measured bytes/cycle and lock occupancy to set the expansion budget before spending development time on marginal micro-optimisation.

## Optimisation priority matrix

Priorities describe **future implementation after evidence**, not changes now. CPU ranges are working hypotheses or component ceilings; “U” means not measured. Memory may increase with caching. Small = roughly ≤1 engineering day including focused validation; medium = 1–3 days; large = several days. Do not treat these as fixed quotes.

| Optimisation | Expected CPU saving | Expected memory saving | DB/I/O saving | Effort | Regression risk | Effect on semantics | Priority / monthly scale |
|---|---|---|---|---|---|---|---|
| Baseline telemetry and reproducible snapshot replay | None directly | None | Enables attribution | Small | Low, observational | None; avoid intrusive production reads | **P0**, prerequisite |
| Bulk evaluated-key, capture and checkpoint reads | ~50–98% of named query groups; U total CPU | Small; bounded key sets may add memory | 48/84→1 grid queries, 48→1 science, 36→6 capture | Small–medium | Low with equal query results | Zero at same decision inputs | **P0**, cents to sub-$1 CPU, overlaps sharing |
| Reuse one technical Frame and micro flags per asset/call | Up to 13/14 Frame constructions, 7/8 flag builds; U total CPU | Fewer transient arrays; small retained Frame | No immediate writes saved | Small | Low–medium floating/mutation risk | Zero with exact feature outputs | **P0**, likely small dollars, strong scaling benefit |
| Cache canonical frozen definitions/bootstrap | ~5ms per repeated definition build measured | Small object retention | Avoid repeated JSON/model work, keep DB validation | Small | Low with immutable keys | Zero identities must match byte-for-byte | **P0**, likely cents/month |
| Same-cutoff common context/market inputs | Five of six common market reconstructions; six contexts ~0.586 CPU-s locally | Transient reduction; bounded cache can raise baseline | 78→13 common queries before batching | Medium | Medium causal/mutation risk | Zero only at identical view/cutoff | **P1**, portion of $0.85–2.05 combined-sharing ceiling |
| Cross-run immutable feature/input sharing | Up to 66.7% of compatible pure work; assumed total 16.7–40% worker | U; cache budget required | Duplicate scans/conversions reduced | Medium–large | Medium–high causal cutoffs | Same logical inputs only; retain run envelopes/clocks | **P1**, ~$0.85–2.05 worker CPU sensitivity |
| Shared discovery and cycle housekeeping | U; at most two of three identical discovery passes | Small | Conflict inserts, opens, scans reduced | Medium | Medium restart/fairness | Conditional; no trigger/visibility changes | **P1**, must measure component first |
| Transaction-local account/position projections; same-boundary evidence batches | Mark unit cost ~10–20ms/engine call synthetic; reducible part U | Small | Fewer SELECT/upserts, same logical events | Medium | Medium–high sequence/read-your-writes risk | Zero only with identical ledger/projection/replay | **P1**, savings unpriced until writer counters |
| Lightweight CLI health entrypoints | Up to ~2,828 CPU-s/day imports locally | Lower transient peaks, not permanent 156MiB per job | Same health outputs/check frequency | Small–medium | Low–medium invocation parity | Zero check/alert behaviour | **P1**, ~$0.65/month CPU ceiling |
| Lightweight collector entrypoint/import boundary | Startup CPU minor | Potential O(100 MiB) steady collector RSS, unverified | None | Medium | Medium config/authority parity | Zero data/receipt behaviour required | **P2**, O($1/month) memory if confirmed |
| Latest-quote decode/asset maps | Up to two thirds duplicate decode; U total | Tiny | Fewer file reads/session queries where same view | Small–medium | High if freshness weakened | Conditional; no cached validity or deferred reload | **P2**, probably cents |
| Incremental micro/technical windows | Potential large at warmed 20–30 assets; U | Less frame rebuilding, bounded history still needed | Fewer repeated history reads | Large | High revisions/numerics | Zero only with exact values; else research change | **P2**, profile after simpler sharing |
| Bound unused large-print history and catch-up reads / profile DB threads and memory limits | U | Lower transient memory possible | Fewer giant materialisations/spill risk | Medium | Medium; threads change timing | Conditional; settings require replay/load evidence | **P2**, do not tune blindly |
| Reporting on existing offline snapshots | Avoid operator-query interference | No runtime dashboard process | Avoid extra live queries/fingerprints | Small | Low | None for trading; preserve required brief delivery | **P1**, latency isolation, dollars U |
| Dirty-only marks / slower research/health cadence | Payload ceiling 129,600 marks/month; CPU U | Small | Up to 21.25MB/month payload plus row overhead | Medium | High evidence/time semantics | **Observable change; approval required** | **P2 separate proposal**, not first tranche |
| Tiered future universe/full-micro subscriptions | 50–88% full-micro proportional work in examples | Reduces per-asset state, not imports | Same proportional stream/minute/archive reduction | Universe project | High availability/warmup impact | **Future universe policy** | **P1 design during universe**, not retrofit current six |
| Parser/executor-object micro-tuning, hash algorithm replacement, blanket indexes, removing source coverage | No demonstrated material gain | U or negative | Index write/storage overhead possible | Disproportionate | Unnecessary risk | Hash/source changes prohibited | **NOT WORTH IT** |
| Redis/Kafka/paid DB/additional services/rewrite | No evidence of need | More overhead | More infrastructure | Large | High | Outside scope | **NOT WORTH IT** |

## Zero-semantic-change tranche and behaviour-changing proposals

The first implementation batch should contain only these separately revertible changes, preserving function signatures and clock/read/transaction boundaries:

1. Batch absent-grid checks **within the current phase/run invocation**, preserving deterministic iteration and the final admission duplicate check. Query all science members once at the current as-of. Load last-two positioning rows and max capture once per asset rather than per hypothesis.
2. Construct one `Frame` per asset/visible BarSeries and one `flags` result per micro feature frame. Confirm no predicate mutates shared arrays. Reuse validated immutable strategy/bootstrap data and expected canonical policy bytes; still verify stored frozen policy/run identities.
3. Build common market/macro primitives once **within an existing exact-cutoff context operation**, and reuse identical `(asset,as_of,input manifest)` context calls. Preserve as-of in every envelope and hash. Start with same-invocation reuse before cross-run caching.

If evidence supports it, a **separate operational patch** may provide lightweight health command entrypoints with identical schedules, output/exit status, stale thresholds and alert behaviour. This should not be mixed into a trading-cache patch.

**Excluded from this strict batch:** dirty-only account marks, reduced mark/research/source/health cadence, grouping different child decision timestamps, delayed/bigger spool fsync batches, changed ingest commit availability, removing redundant-looking event evidence, changing full-micro coverage, skipping data warmup, collector unsubscribe or retiring a baseline. Account projection batching and broad shared-cycle connection changes come later because they affect more correctness boundaries.

A crucial limit: faster code can reach an existing `utcnow()`/quote refresh earlier in live operation. “Zero semantic change” here means **identical output given the identical causal input tape and injected decision clocks**, with the same live clock/refresh call sites. It is not a guarantee that faster production code chooses the same future quote as slower code. A patch that deliberately changes wake cadence, cutoff selection, fill eligibility or visibility is a policy/timing proposal, even if expected P&L is unchanged. Any observed replay trading-output difference fails the optimisation gate; do not reclassify it as harmless speedup.

Potential reduced-cadence proposals, only separately approved: optional dashboard summaries, human-only quality aggregation and flat-account diagnostics. Do not reduce 2s critical quote checks, required event triggers, source polling, expiry/TP/invalidation detection or pending confirmations. Slowing a 2s check to 10s would add up to ~8s polling delay (and ~4s mean under uniformly arriving updates), before lock/computation delays, and could change fills. This audit does not recommend it. Reaction collection delay can also change available input revisions/record times: treat it as research semantics.

## Metrics to collect over the next 24–48 hours

**Do not deploy instrumentation or change runtime behaviour to begin this evidence window.** Use existing logs/status JSON, OS counters, Railway usage and normally generated verified backups. If a required statistic is absent, label it U and obtain it on an offline replay; add minimal aggregate counters only in a later explicitly authorised observability patch. No paid monitoring platform, per-query production logging flood or permanent new service.

Minimal checklist, with collection bounded and aggregated off-production:

| Metric | Existing source / method | Cadence and result |
|---|---|---|
| Revision/manifest/lifecycle/config | `REVISION`, Nimble activation manifest, existing health/status and next ordinary snapshot | Once at start/end; identify three active children, v2 active/draining/retired, source flags; freeze comparison cohort |
| Service CPU/memory/volume/egress | Existing Railway metrics and cgroup counters (`cpu.stat`, `memory.current`, `memory.peak` if available, `memory.events`), disk usage | 60s resource buckets for 48h; mean/p50/p95/max, total CPU, growth, limits; include backup/research times |
| CPU/RSS per responsibility, transient processes | `/proc/<pid>/stat`, `status`/`statm`, `/proc/<pid>/io`; existing collector `cpu_s` | One lightweight persistent observer if available, ~10s; no new Python process per poll. Capture process birth/death/starttime and classify command. PSS less often, e.g. 5min. Short cron processes missed by sampling remain U. |
| Worker cycle and dispatch | `paper_nimble_worker.json`: per-run CPU/wall/evaluated/assets/observations/open_positions/state | Observe about every 2s, dedup heartbeat timestamp; retain one record per distinct heartbeat off-host, aggregate by minute/run. Explicitly count missed/overwritten cycles—this file is not an append log. |
| Lock contention | Runtime job logs/cycles plus existing lock-holder JSON and worker BUSY timestamps | Correlate BUSY with holder at ~2s during short boundary samples; label estimated blocked spans, not exact waits. Group by job and exclude operator profiling from clean baseline. |
| Collector workload/fidelity | `microstructure/status.json`: msgs/s, bytes/s, CPU, flush p50/max, pending/dropped, status counts, reconnects | 60s buckets; separate parse/aggregate/spool CPU unavailable today, obtain on offline journal replay |
| Free sources / gateway | Source state hourly `requests/wire_bytes/cpu/detected/accepted/duplicates/rejected`, 304 totals if exposed; gateway/free worker backlog and quality | 5min source aggregates; arrival bursts correlate with ingest/worker CPU; do not actively refetch feeds for measurement |
| Logical reads/writes and accounts | Start/end **normal verified backup** copies; count ledger kinds/bytes, receipts, opportunity evaluations, reactions, marks per run, open/closed/account sequences | Offline at T0/T24/T48; measure counts/deltas and calculate hypothetical unchanged marks without modifying them. No live table-wide fingerprint for each sample. |
| SQL/query plans and cache-hit potential | Replay one ordinary minute, due 15m/hour grid, flat and active windows from the same snapshot/tape | Count query shapes, returned rows, time, input-digest collisions, repeats across runs/hypotheses; cProfile locally. Live per-function/SQL time remains U unless already logged. |

Sampling heartbeat every 2s is read-only but can itself have overhead if implemented with repeated remote commands. Prefer an existing persistent session/file stream and aggregate locally, or accept coarser coverage. Never install an always-on profiler or acquire the writer just for each resource sample. OS file reads do not require DuckDB. Measurements collected by an operator observer should be labelled so their CPU/SSH traffic are not mistaken for trading load.

**Window design:** cover two daily checkpoint/backup cycles if possible. Segment intervals by: no open/pending risk and no new trigger; no signal/event but new market data; one open position; three positions if naturally occurring; hourly/15m grid; distinct-event burst; repeated-update burst; signal burst; gateway backlog; lock BUSY; operator activity. Never generate production signals/events/trades to populate missing classes. If a class does not occur in 48h, report “not observed” and use a labelled scratch replay.

At 24h produce a one-page distribution by responsibility/run; at 48h repeat and record CPU-seconds/day, mean/peak service RAM, DB+spool growth, completed decisions/grid, seconds BUSY, latency distribution where available and percentage of identical compatible inputs. Compare day/time strata, not just two endpoints. Preserve sampled input quote generations/decision clocks for future replay where existing journals allow it; latest-quote overwrite alone cannot reconstruct every past decision.

Future lightweight counters, if authorised later: successful/idle/BUSY ticks by cause; cycle and lock-acquired/held durations; query **shape** aggregate durations/count/rows; source/feature construction count; run/phase/asset evaluations/skips; compatible cache hits/misses and bytes retained; mark attempted/written/would-be-unchanged; ingest offsets/rows/conflicts; quote age/session rejects; event wake-to-decision and input-available-to-fill; research due/backlog/budget; fsync bytes/time. Emit bounded minute aggregates to existing logs, not a row per quote. No heavy platform is needed.

## Implementation staging and validation

### Tranche A — local reuse and query batching

After the evidence window: implement the three strict changes above as independent patches, beginning with keyed lookups and per-call Frame/flags. Add benchmarks/replay fixtures that exercise meaningful causal edge cases, not tests that merely mirror a cache implementation. Preserve all IDs and evidence, no schema requirement, feature flag or simple revert returns the old pure path. A separately reviewed health-entrypoint patch can follow. Expected engineering spend: a few small patches; stop if measured benefits are below noise.

### Tranche B — shared immutable inputs and transaction work

Introduce a bounded, explicit market-input snapshot API and per-run envelopes. Start with identical-cutoff sharing; then dependency-manifest reuse across cutoffs only with filtering equivalence. Add bounded shared checkpoint/funding/context primitives, idempotent discovery reuse and transaction-local projections in separate changes. Preserve single writer and per-run receipt commits. Measure added cache RSS; do not trade small CPU savings for unbounded memory. Pure derivation outside transactions or consolidated DB open/close requires a distinct concurrency/visibility review. Persistent content-addressed snapshots are optional, not prerequisite infrastructure.

### Tranche C — universe scaling preparation

Define tier admission/warmup/demotion/position-pinning and byte/CPU budgets; this is preparatory design only until Dynamic Perp Universe is approved. Run 6/12/20/30-asset synthetic/snapshot workloads with representative bursts and larger event/receipt tables. Dynamic subscriptions, new asset config and data-availability policy belong to that project. Preserve current six assets and all frozen feature versions. Only then consider incremental features and indexes based on measured bottlenecks.

### Required acceptance gates for every optimisation

| Gate | Required proof |
|---|---|
| Identical logical experiment | Same policy/run/universe/hypothesis IDs, activation/lifecycle, input manifests, causal cutoffs, known-at and revision choice; no changes to risk/sizing/funding/event/source rules |
| Deterministic before/after replay | Isolated copies, identical input event/quote/clock tape, single-thread reproducibility pass plus production-thread setting pass; unchanged decisions/reject reasons/pending transitions/fills/fees/funding/TP/invalidation/expiry and every required evidence identity/payload/order |
| Quote and causal edge cases | Exactly-at/stale/future timestamps, cross-minute/hour boundary, late/revised bars/minutes, missing gaps, incomplete warmup, source update/retraction, collector restart/disconnect/session mismatch, quote after intent and as-of updates between children |
| Account/research edge cases | Multiple positions same asset across runs, opposite directions, cooldown/episode dedup, independent capital, administrative stop, delayed settlement/closed-but-unfinalised positions, event immediate/confirmed/rejected/conflict, all research horizons/missingness |
| Crash/restart/locks | Crash before/after append/receipt/offset/projection commit, interrupted batch, duplicate delivery, >1,000-trigger backlog, equal-time/late trigger insertion, lock BUSY/retry, cache cold restart/eviction; final ledger recovery identical |
| Mutation/concurrency | Shared arrays/dicts unchanged after every consumer; no cross-run state leakage; expected policy canonical bytes still checked against DB; no live external reader or second writer |
| Timing/performance | Same machine/data/settings, alternate before/after at least five warm and cold repetitions; report median/p95 CPU and wall, query/read/write counts, I/O bytes, baseline/peak/retained RSS and cache bytes; cProfile timings separate from uninstrumented timing |
| System-level consequence | Natural shadow/read-only replay, then authorised canary with no double evidence writes; no increased BUSY/backlog, quote ages or missed deadlines. No live policy/latency change may be disguised as optimisation. |
| Reversal | Remove cache/flag/revert patch without rewriting ledger/history; optional additive derived artifacts disposable; verified restore/recovery works |

Suggested performance targets, **not guaranteed results**: named query groups fall by ≥50%; compatible three-run pure recomputation falls toward one third; end-to-end worker CPU decreases beyond run-to-run noise; no p95 critical-path regression; bounded memory plateaus after warmup and cache eviction; same logical evidence write count unless a separately approved evidence contract explicitly permits change. A byte-identical replay failure blocks rollout even if CPU improves.

## Recommendation

Collect the next **24–48 hours** first, establish the actual v2 lifecycle and service-wide resource distribution, then perform **Tranche A before widening the execution universe**. Build Tranche B before or alongside the initial Dynamic Perp Universe implementation; design tiered collection and warmup as part of that universe's policy. Do not wait for 20–30 full-micro assets to expose predictable duplication and backup growth.

The first objective is to avoid repeated reconstruction of the same causal inputs while retaining independent experiments. Preserve the existing cheap filesystem quote path, all required monitoring and all 18 sources. A quiet interval should do only the work justified by new committed inputs, an open/pending risk position, a due accounting/research deadline or required health checks. The current architecture already provides that boundary between busy ticks; the plan makes the busy ticks cheaper without weakening what Prism can observe or prove.
