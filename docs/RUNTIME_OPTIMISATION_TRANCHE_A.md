# Runtime Optimisation Tranche A

Base: locally approved `origin/main` **791bab8**. Implementation is on
`codex/runtime-optimisation-a` in `.worktrees/runtime-optimisation-a`.
No production deployment, Tranche B, or Dynamic Perp Universe work is included.
The original [audit](RUNTIME_OPTIMISATION_AUDIT.md) remains the design basis.

## Changes and causal boundaries

1. **Grid existence:** one exact keyed read per observation invocation, scoped to the
   existing ledger, run, assets and `(hypothesis_id, signal_at)` pairs. Original
   hypothesis/asset iteration order and activated-at gates remain unchanged. Results
   live only in that call; subsequent calls see late commits. The final admission
   duplicate check remains authoritative. No schema/index or transaction change.
2. **Science:** one lazy completed-result query and JSON decode in a compatible
   invocation. Both `c.as_of <= now` and `r.completed_at <= now`, existing descending
   checkpoint ordering, result/checkpoint identity, member selection and degraded
   defaults are retained. No cache crosses an as-of boundary.
3. **Positioning:** one lazy latest-two capture read per asset, preserving the original
   `captured_at <= now` predicate and ordering. Nimble retains its separate maximum
   capture-time query, once per asset. Only raw captures are shared; each hypothesis
   independently derives its directional predicate and existing availability/warmup.
4. **Features:** one technical Frame per asset/visible BarSeries per call. Predicate
   audit found reads and lazy memoisation, without in-place consumer mutation. The
   adapter opts into read-only base and derived NumPy arrays; other research callers
   retain their existing default. One micro flags mapping per feature frame, wrapped
   in a read-only mapping with non-writeable arrays. Windows, normalization, gaps,
   production cutover and ingested availability are unchanged.
5. **Definitions:** private bounded process caches retain frozen bootstrap/definition
   products and expected canonical policy strings. Public bootstrap/definition APIs
   return deep copies, retaining original list/tuple types. Strategies are deeply
   immutable frozen models; their parameter/horizon fields are tuples, and `.p`
   creates a fresh dict. Database policy/run identity and canonical stored-content
   comparisons still execute on every original load/registration path.
6. **Context:** one lazy `ContextOperation` per existing observations or updates
   call, scoped to the identical Store object, exact timestamp and snapshot version.
   It retains the original common event/update fold, the fixed six-market breadth
   and seven macro series, and common macro derivation. It rejects a different
   Store/cutoff/version or substitution for a research index manifest. Folded raw
   states remain private and are copied for consumers (including nullable legacy
   values); market/macro products are canonical strings decoded to independent
   copies. No shared pandas frame reaches a consumer. Asset links, positioning,
   activity/opportunity and freshness remain per asset. Existing single-argument
   context overrides retain their original dispatch contract.

All operation-local products expire on return. None is retained on Sources across
calls, shared across runs, or rounded to a minute. Each existing decision keeps its
own clock and causal input view. No new input manifest or evidence is written;
existing context payloads/identities contain the same source inputs and versions.
Writer, runtime locks, transaction boundaries, public gateway and collector isolation
are unchanged. No execution, funding, sizing, risk, timing, warmup, collector fidelity,
research horizon, source/quote cadence or paper identity policy changes.

## Cache identity and integrity

| Product | Key | Bound/lifetime | Consumer protection |
|---|---|---|---|
| v2 bootstrap | definition `VERSION`, within loaded code module | LRU 1, process | deep model copies |
| v2/Nimble/event definition | each module's definition `VERSION` | LRU 1 each, process | deep copies |
| expected canonical bytes | `(VERSION, definition producer function identity)` per module | LRU 1 each, process | immutable string |
| technical strategy | full canonical serialized strategy parameters, including feature/spec versions | LRU 64, process | deeply immutable model |
| context/Frame/flags/raw inputs | call-local Store/cutoff/input object | current invocation only | copies/read-only arrays |

Code-module lifetime supplies code identity: restarting with new code starts cold.
The producer function identity additionally makes historical-definition substitutions
miss the expected-bytes cache, preserving universe-extension compatibility. Runtime
hot editing of definition constants in an existing module is not a supported API.
The cache never supplies stored DB content or bypasses integrity comparison. Tampered
DB content is rejected even after caches are warm. No content-addressed ID changes.

## Query and construction counts

For six assets and a completely due phase:

| Group | Original | Optimised |
|---|---:|---:|
| technical evaluated-grid SELECTs | 84 | 1 |
| positioning evaluated-grid SELECTs | 36 | 1 |
| micro evaluated-grid SELECTs | 48 | 1 |
| micro science checkpoint SELECT/decode | 48 | 1 |
| positioning predicate last-two captures | 36 | 6 |
| Nimble positioning max capture | 36 | 6 |
| technical Frame constructions | 84 | 6 |
| micro flags constructions | 48 | 6 |
| common breadth/macro SELECTs, per context operation | 78 | 13 |
| common event/update fold reads, when events exist | 12 | 2 |

Laziness preserves paths with no eligible observations; fewer assets/members reduce
original counts correspondingly. Asset-specific positioning context reads remain.
Query shapes remain distinct; the main reduction is repeated executions, not a new
schema or larger ingestion batch. Tests count complete feature vectors/predicate
outputs, canonical observation payloads and construction calls, and attempt mutation
after every consumer. Mutation isolation is also checked for context and bootstrap.

## Replay and validation

Original adapter/spec/context files from `791bab8` are retained as test-only oracle
fixtures in `tests/fixtures/runtime_a/`. They are never imported by production code.
Tranche tests run old and new paths on identical bars/minutes/captures/checkpoints,
clocks and stores. They compare canonical observations and dependencies, every
technical feature/mask, micro member masks and full context snapshot bodies/hashes.
Cases include exact boundaries, missing data/gaps, incomplete warmup, partial grids,
independent runs, late insert, science completion boundary, equal capture clocks
across assets (same-asset ties are prohibited by schema), future capture exclusion,
source updates/denials, late hourly bars, revised minutes at exact ingest boundaries,
restart reconstruction/gaps and incompatible context boundaries. Historical breadth
continues reading the stored bar revision exactly as before; this tranche does not
change that calculation when proving technical known-at revision reconstruction.

`tests/runtime_a_replay_plugin.py` is an opt-in local differential harness for the
existing execution/event fixtures. It injects the same decision/gateway/measurement clocks, fixed RSS telemetry input,
and deterministic UUIDs (including unique truncated ingestion IDs),
substitutes the original adapters/definition producers for the before run, and hashes
all ordered paper/context-snapshot tables after each passed fixture. Normal pytest
never loads it. `scripts/runtime_optimisation_a_worker_replay.py` independently replays
real worker ticks on paired disposable databases, with the same run IDs, state,
quotes and clocks at offsets 0, 2, 60 and 62 seconds. It compares worker results and
all paper/context-snapshot table digests, including payload strings and evidence
sequence fields. Worker telemetry clocks are fixed for equivalence, while separate
real process/performance clocks measure the benchmark. No payload field is removed. Position fixtures exercise real admission, future fills and TP exits.
No live source, collector or production service is contacted.

Differential replay: **182 fixtures match exactly**, including **153 Store fixtures**
and **5,508 table digests**. No field is excluded from comparison. The additional
fixture explicitly holds three BTC positions across independent baseline/immediate/
confirmed runs, including opposite directions, distinct cutoffs/snapshot identities,
closed-but-unfinalised positions and delayed sign-dependent funding reconciliation.
See [replay evidence](evidence/runtime-optimisation-a/replay.json) for the complete
fixture list and combined digest. An initial local harness omitted measurement/gateway
clocks and truncated deterministic ingestion IDs collided; both injection issues were
corrected and the affected fixtures rerun in both modes. A mismatch blocks rollout;
passing assertions on the optimised path alone are not treated as differential proof.

## Offline adapter benchmarks

Five warm paired repetitions after one discarded warmup, using the same archived
snapshot and injected `2026-10-10T07:50:00Z` clock. Snapshot SHA-256:
`eb27453f9a20d4030b04d7d5fe872caad0c0df34d49a6f55998888e3cb1977f2`.
Copy/migration/import/profiling time is excluded; one DuckDB thread is used only by
this local benchmark. This does not change runtime/database configuration.

| Due invocation | CPU median seconds before → after | Wall median seconds before → after | SELECTs | Rows returned |
|---|---|---|---|---|
| 1h technical | 0.745 → 0.487 | 0.741 → 0.484 | 222 → 64 | 55,982 → 54,487 |
| 1h positioning | 0.627 → 0.398 | 0.637 → 0.402 | 204 → 64 | 49,580 → 48,025 |
| 15m micro | 1.099 → 0.819 | 1.099 → 0.818 | 246 → 77 | 71,795 → 70,253 |

Writes are zero on both adapters. Query shape counts are 12/12/15 on both paths.
Paired-process peak RSS is 417/423/457 MiB respectively; high-water marks belong to
the shared process and cannot establish independent before/after RSS savings.
Returned rows are measured, not physical engine rows scanned. Five samples do not
support a stable p95 claim. This archive contains stale technical/short micro warmup
paths; the populated synthetic replay verifies healthy feature parity/constructions.
Do not extrapolate these timings to production signal throughput.

[Batch evidence](evidence/runtime-optimisation-a/batch.json),
[Frame-stage evidence](evidence/runtime-optimisation-a/frame.json),
[definition evidence](evidence/runtime-optimisation-a/definitions.json), and
[final adapter evidence](evidence/runtime-optimisation-a/final.json) retain measured
results at the separate implementation stages. Expected canonical bytes avoid
repeated full serialization; bootstrap retains the small cost of defensive copying.

## Offline worker benchmarks

Five warm repetitions after one discarded warmup in separate before/after processes,
using the identical extracted input tape and seed account/run database. Each sample
replays four ticks at 0/2/60/62 seconds; these are tape totals, not single-tick times.
Store opening is timed; query counters start after its constructor. Copy/extraction,
import and seed setup are excluded. Real process/performance clocks measure CPU/wall;
worker telemetry clocks are separately injected for complete output parity. Linux
`VmRSS` and `VmHWM` measure each child exec image, avoiding inherited pre-exec peaks.

| Scenario | CPU s before → after | Wall s before → after | SELECTs | Returned rows | Write statements | RSS MiB | Peak MiB |
|---|---|---|---|---|---|---|---|
| quiet_minute | 1.703 → 1.101 | 1.792 → 1.396 | 406 → 213 | 56,664 → 55,169 | 40 → 40 | 199.7 → 197.7 | 266.2 → 260.8 |
| due_15m_micro | 4.458 → 2.783 | 4.088 → 3.105 | 1018 → 521 | 178,088 → 173,466 | 77 → 77 | 213.8 → 212.8 | 313.1 → 313.1 |
| due_1h | 2.729 → 1.730 | 2.821 → 2.061 | 718 → 390 | 106,282 → 103,202 | 78 → 78 | 199.8 → 198.2 | 272.6 → 269.9 |
| event_wake | 2.774 → 1.930 | 2.656 → 2.431 | 725 → 397 | 106,287 → 103,207 | 77 → 77 | 200.3 → 198.7 | 271.6 → 270.5 |
| one_position | 3.420 → 2.328 | 3.243 → 2.449 | 811 → 483 | 123,253 → 120,173 | 90 → 90 | 200.9 → 200.8 | 279.4 → 276.8 |
| three_positions | 4.245 → 2.730 | 3.849 → 3.612 | 947 → 559 | 156,605 → 152,341 | 114 → 114 | 202.7 → 200.5 | 282.1 → 279.3 |
| duplicate_retry | 3.328 → 2.392 | 3.234 → 2.610 | 706 → 344 | 128,472 → 125,435 | 41 → 41 | 213.3 → 211.5 | 311.0 → 309.2 |

All seven scenarios have **exact worker-result and full ledger equivalence** across
all six repetitions. Writes are unchanged. Median CPU falls 28–38% on this tape;
median wall time improves in each scenario. Current RSS is approximately 198–214 MiB
and peak approximately 261–313 MiB, without material growth. Caches are explicitly
bounded; this is not a long production memory soak. Five samples are insufficient for
a stable p95 or production critical-path tail-latency claim. Raw warm samples, shapes
and full metrics are in [worker evidence](evidence/runtime-optimisation-a/worker.json).

The quiet case starts without a position/new event; due grids, context wake, one/three
open positions and duplicate retry are separate seeds. Positions are mechanically
created through real admission/future fills; executable quotes are synthetic. Context
wake measures baseline context dispatch, not the complete paired event-cohort workload;
paired cohort classification/confirmation behavior is covered by differential fixtures.
This archive/tape is not a production profitability or healthy-universe throughput study.

## Final checks

- Full pytest: **1,369 passed**, including Phase 30, 29, 28A, 27, 26A, 24B, 24A, 23
  and paper/dashboard/reporting compatibility. Four existing Starlette warnings.
- Final Tranche A tests: **29 passed**, including three edge fixtures added after full
  suite collection. The unchanged production code passed the complete suite; the
  later test additions were run separately with all Tranche A tests.
- Differential execution/event replay: **182 fixtures / 5,508 table digests exact**.
- Seven worker tapes: exact outputs/ledger, five warm samples each.
- Repository-wide `ruff check .`, `ruff format --check .`, and `git diff --check` pass.
- No frontend changes or production deployment.

Reproduce from this worktree with the repository virtualenv:

```bash
PYTHONPATH=src /home/matth/prism/.venv/bin/pytest -n 4
PYTHONPATH=src /home/matth/prism/.venv/bin/pytest tests/test_runtime_optimisation_a*.py
ruff check .
ruff format --check .
git diff --check
PRISM_RUNTIME_ROLE=scratch PYTHONPATH=src:. python scripts/runtime_optimisation_a_benchmark.py SNAPSHOT OUTPUT
PRISM_RUNTIME_ROLE=scratch PYTHONPATH=src:. python scripts/runtime_optimisation_a_worker_replay.py SNAPSHOT OUTPUT
PYTHONPATH=src:. python scripts/runtime_optimisation_a_definition_benchmark.py
PRISM_RUNTIME_A_REPLAY_MODE=before PRISM_RUNTIME_A_REPLAY_OUT=/tmp/before.json PYTHONPATH=src:. pytest -n 4 -p tests.runtime_a_replay_plugin tests/test_exploratory_paper_v2.py tests/test_nimble_paper.py tests/test_nimble_runtime.py tests/test_event_reaction.py tests/test_runtime_optimisation_a_execution_edges.py
PRISM_RUNTIME_A_REPLAY_MODE=after PRISM_RUNTIME_A_REPLAY_OUT=/tmp/after.json PYTHONPATH=src:. pytest -n 4 -p tests.runtime_a_replay_plugin tests/test_exploratory_paper_v2.py tests/test_nimble_paper.py tests/test_nimble_runtime.py tests/test_event_reaction.py tests/test_runtime_optimisation_a_execution_edges.py
PYTHONPATH=src:. python scripts/runtime_optimisation_a_compare_replay.py /tmp/before.json /tmp/after.json OUTPUT
```

Use the virtualenv's `python`/`pytest`/`ruff` binaries or activate it. Gateway fixtures
need local socket access. Do not overlap benchmarks with tests or other heavy work.
Only benchmark a closed snapshot, never the live writer database.

## Watchdog and production

Watchdog patch **deferred**. Gateway health performs watchdog state/alert deduplication,
live publication and runtime validation; micro health depends on configured store and
spool/schema validation. Extracting imports without a separate command/config/auth/
alert equivalence fixture is not yet proven. Both schedules, stale thresholds,
check frequency, exit status and alert behavior remain untouched.

**Not deployed.** Production CPU/RSS, cycle duration, BUSY, evidence/account writes,
quote age and trigger latency have not been measured after these changes. Production
savings are not claimed. Deployment must follow acceptance/review, one patch at a time,
then comparable multi-cycle before/after windows with workload differences labelled.

## Rollback

Implementation commits, in order:

- `e44e76c`: batch queries.
- `61a982f`: Frame/flags reuse.
- `e426f4a`: immutable definition/bootstrap products.
- `397c769`: same-cutoff context primitives.

Revert in reverse order, restart the existing worker to clear process caches, and use
ordinary replay/health checks. No schema migration, evidence deletion, data rewriting,
run replacement, collector reset or source warmup reset is required. The prior code
at `791bab8` is the original reference. A whole-tranche rollback can use
`git revert 397c769 e426f4a 61a982f e44e76c` on the deployed branch, followed
by the normal deployment/restart procedure.

## Dynamic universe readiness and remaining Tranche B

These are construction/query projections, not end-to-end CPU forecasts. Breadth stays
at the original six markets even if the execution universe expands.

| Assets | Grid reads across three phase calls | Frame constructions | Micro flags constructions | Common context reads per operation with events |
|---|---|---|---|---|
| 12 | 336 → 3 | 168 → 12 | 96 → 12 | 180 → 15 |
| 20 | 560 → 3 | 280 → 20 | 160 → 20 | 300 → 15 |
| 30 | 840 → 3 | 420 → 30 | 240 → 30 | 450 → 15 |

Asset-specific scans/window derivation and per-run decisions/evidence remain linear.
There is no new universe behavior or cross-run market snapshot architecture. Before
expansion, representative healthy 12/20/30-asset tapes must measure worker budgets,
RSS, quote age and decision latency. Tranche B still includes explicitly manifested
shared immutable inputs with independent per-run causal envelopes, repeated connection/
discovery/history work, and separately reviewed transaction-local account projections.
Cross-cutoff filtering and revision invalidation need their own differential proof.
This tranche supplies lower repeated pure cost and a replay/measurement harness;
it does not authorize or begin that architecture or Dynamic Perp Universe.
