# Dynamic Universe cadence orchestration — UTC slots v1

The rejected immutable release `eee205f24d5d9ef3cd71c32499f13ec2e2fbb8fd` remains historical
evidence. This implementation corrects orchestration without changing the frozen
`hyperliquid_dynamic_universe_v1` policy, inputs, scoring or readiness rules. Deployment
and ranking admission are separately evidenced; this document grants no warming/execution authority.

## Original mechanism

The worker measured600 seconds from child launch. The CLI pre-fetch guard and `source.commit`
both required600 seconds since the last stored evaluation clock. That clock was sampled at
**commit entry**, after fetching, not at fetch start and not after durable transaction completion.
With a36-second fetch, launch0 produces evidence clock36. Launch600 sees only564 seconds of
age and returns `NOT_DUE`; launch1200 produces clock1236. Every second poll is skipped and
accepted observations are1200 seconds apart, above the frozen900-second continuity allowance.

The stored evaluation timestamp was previously described loosely as "commit time" in the
operational report. The precise basis is the existing integer UTC clock passed to
`source.commit` immediately before ranking/state/readiness work and ledger persistence.
It remains on that same basis; no timestamp is moved earlier to conceal a gap.

## Choice and invariant

The supervised worker runs at fixed UTC boundaries (`epoch % 600 == 0`). Initial start/restart
waits for the next boundary. Next deadline derives from the previous nominal deadline plus600,
never from completion time. Expired deadlines are counted and skipped; there is no overlapping
job, catch-up burst, fabricated past observation or nominal timestamp substituted for evidence.

Both due guards now admit **one refresh per actual source-start UTC slot**. The slot is stored
as versioned orchestration metadata, `schedule={version: utc_600s_slots_v1, slot: ...}`. A legacy
evaluation without that metadata falls back to its actual evaluation-clock slot. A later slot
can be admitted even if the previous real transaction finished less than600 seconds earlier.
This resolves the scheduling/commit-age mismatch without changing evidence-clock semantics.

For bounded healthy work, accepted observations follow the nominal600-second schedule, with
actual fetch/processing jitter retained. Frozen continuity checks always use real observed/
evaluation times. A genuine delay >900 seconds still resets continuity/streaks. No scheduler
can guarantee continuity through unbounded network stalls, suspension or missed observations;
those events are recorded and never reclassified as healthy evidence.

Alternatives considered:

- **A, fixed UTC boundaries (chosen):** deterministic restart behavior and inspectable deadlines;
  requires synchronized clock, checked before activation. Separate slot idempotency fixes both guards.
- **B, completion-aware nominal deadlines:** previous *nominal* deadline +600 could also work, but
  persisting/recovering a private phase adds complexity. Previous *commit* +600 accumulates fetch
  duration and does not provide a stable600-second nominal schedule.
- **C, monotonic deadlines mapped to a UTC origin:** equivalent while clocks remain aligned, but
  adds explicit remapping after wall-clock steps/restarts. The existing synchronized UTC host and
  auditable shared slots make A simpler. Actual wall-clock gaps remain visible in either design.

A dedicated kernel `flock` spans each refresh's prepare/fetch/commit, including manual refreshes.
It is independent of the short runtime DB writer lock, so HTTP does not block core data writers.
The lock releases on process exit. `subprocess.run` remains sequential, kills/waits on its300s
timeout, and the worker records timeout/failure without restarting or inventing an evaluation.

## Distinct clocks and telemetry

| Marker | Meaning |
|---|---|
| `scheduled_deadline` | Nominal UTC boundary; scheduling only |
| `evaluation_started_at` | Worker child-launch time |
| `refresh_started_at` | CLI refresh entry before read-only preparation |
| `source_fetch_started_at` | Start of the network fetch phase |
| Market `observed_at`, book `quote_at` | Existing actual source/venue capture clocks; unchanged |
| `source_refresh_completed_at` | All discovery/light fetching completed |
| `evidence_clock` / stored `evaluated_at` | Existing integer UTC clock sampled at commit entry |
| `ranking_computed_at` | Pure ranking and transition computation completed |
| `ranking_db_commit_started_at/completed_at` | Real ranking ledger transaction timing |
| `outcomes_db_commit_started_at/completed_at` | Separate prospective-outcome batch timing |
| `refresh_completed_at` | Publication/DB context closed and CLI result ready |
| `next_scheduled_deadline` | Next nominal target, with skipped-deadline count |

Request telemetry records aggregate request types/statuses, header latency and captured body
bytes, not credentials or response bodies. Worker journal records evaluation identity, candidate/
source-error counts, timing, child CPU and cumulative child high-water RSS. Scores and full
market inputs remain in the existing compressed append-only ledger, not duplicated in logs.

The extra `schedule` field is an explicitly versioned orchestration addition to new ledger
payloads; old payloads are never rewritten. Detailed wall-time metrics are emitted after the
decision is persisted, so completed transaction time is not falsely written before it happens.

## Frozen compatibility and tests

`policy.py`, `engine.py`, `portability.py`, `ledger.py`, `rich.py`, `ops/profiles.py` and the
prepared activation script are byte-identical to approved main. The source functions `decode`,
`book_metrics`, `enrich_history`, `prepare` and `fetch` are AST-identical. Shared ranking gains
no account/equity/risk-pin dependence. Profile digest remains
`e7571ad1a2a35fb42dc8c1ea77759e31887a1c7679445561664c1dee68c12207`.

The focused regression suite exercises the actual worker/CLI/ledger with virtual time:
fast,36s,299s and599s work; variable duration;601s/899s genuine overruns; timeout; restart;
missed/late wakeups; deterministic deadlines; one-per-slot idempotency; no overlapping refresh;
and the exact900/901s frozen continuity boundary. A dedicated two-refresh36s regression
produces two durable evaluations here and fails with one on the rejected release's actual
CLI/source implementation. These tests are mechanical fixtures, never live ranking evidence.

Seven healthy lightweight observations,7-day prospective listing age, technical exact readiness,
two prospective hourly OI observations within140 minutes, funding freshness,288 prior COMPLETE
15m windows plus current, large-print calibration, streak/dwell persistence and reentry reset
remain unchanged. Ranking-only starts only discovery/light-history clocks. It does not enable
satellite backfill, prospective satellite OI captures, rich collection or execution admission.

See [cadence-tranche evidence](evidence/ovh-du-ranking/20261016-cadence/) for final validation,
release identity, fresh admission, activation, genuine observation and rollback evidence.
