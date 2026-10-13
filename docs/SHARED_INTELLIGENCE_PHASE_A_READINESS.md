# Shared Intelligence Phase A readiness

Baseline: approved `main`, `401301ff2924a3684a423139113bd8f6867e603d`.
Scope: implementation of Phase A from the
[architecture review](SHARED_INTELLIGENCE_LAYER_REVIEW.md). No deployment or activation.

**Phase A implemented and validated. Ready for review; no deployment or activation.**

## Implemented boundary

The new [`intelligence/`](../src/market_signal/intelligence/) package supplies a bounded,
causal read contract alongside the frozen Phase 23 snapshot implementation. It creates
no schema, runtime authority, account, scheduler, subscription or stored snapshot on a read.

| Requirement | Implementation | Behavior |
|---|---|---|
| Versioned causal read package | [`snapshot.py`](../src/market_signal/intelligence/snapshot.py), [`definitions.py`](../src/market_signal/intelligence/definitions.py) | `intelligence_context_read_v1`, content-addressed package/definition IDs, mandatory UTC cutoff and clock basis; event chains, macro, hourly activity, market breadth and positioning. Selection, taxonomy and implementation hashes travel with the package. |
| Resolvable evidence/provenance | [`repository.py`](../src/market_signal/intelligence/repository.py), `resolve_evidence`, `validate_package` | Embedded source reports, update payloads, URLs/raw hashes where retained, mappings, exact bar versions and source/run provenance. Resolve from retained JSON without a live database. Hash, clock and reference validation reject corrupt/inaccessible evidence. |
| Three explicit access bases | [`clocks.py`](../src/market_signal/intelligence/clocks.py) | `origin-observed`, `origin-committed`, `consumer-available`; per-record content-bound access catalog with origin/consumer identities and ordered clocks. Missing commit/consumer access is withheld rather than inferred from publication or processing. |
| Revision-safe market/breadth | `snapshot.load_bars` | Reconstructs original/revised bar values independently under each access basis; inaccessible revisions fall back to retained prior values. Missing revision dependencies fail or block descendants. Breadth uses closed, accessible bars, an explicit cohort, exact 24h endpoints and freshness. |
| Venue-separated positioning | [`positioning.py`](../src/market_signal/intelligence/positioning.py) | Hyperliquid and Binance remain separate. Reuses frozen OI/crowding/funding analytical primitives; source period end, ingestion/replacement clocks and staleness remain explicit. No sizing, direction or liquidation claim. |
| Daily/weekly persistence | `positioning_persistence_v1` | 24h/168h base-OI change requires complete hourly endpoint/interior coverage; funding sign shares and settled sum require a complete declared settlement grid. Missing samples/cadence return unavailable values with observed/expected counts. |
| Neutral committed-information publication | [`publication.py`](../src/market_signal/intelligence/publication.py) | Immutable `committed_information_v1` value after context commit; injected subscribers own persistence/retry. Context commit clocks can be bound to retained context evidence with `access_catalog_from_publication`; market data cannot inherit them. |
| Separate legacy runtime subscriptions | [`ops/context_consumers.py`](../src/market_signal/ops/context_consumers.py), gateway/free-source drain adapters | Existing workers default to their existing wakeup/enrollment callbacks, outside intelligence code. Supplying empty/injected subscribers allows ingestion/publication without execution schema. Consumer failures leave durable receipts retryable after commit. |
| Minimal intelligence-only fixture | [`test_shared_intelligence.py`](../tests/test_shared_intelligence.py) | Creates only released source/ingress schemas; no Store initialization, paper/Nimble/account/runtime/authority tables. A SELECT-only connection rejects execution queries and mutations. Reads work even with deliberately invalid runtime identity/profile environment settings. |

## Read contract

The API is a library interface, not an activated HTTP endpoint, dashboard integration or
background projection job. Prefer a verified immutable database snapshot and a read-only
file open:

```python
from datetime import datetime, timezone

from market_signal.intelligence.clocks import ClockBasis
from market_signal.intelligence.repository import ReadRepository
from market_signal.intelligence.snapshot import context_package, resolve_evidence

with ReadRepository.open("/path/to/verified-snapshot.duckdb") as repository:
    package = context_package(
        repository,
        "AAVE",
        datetime(2026, 11, 1, 12, tzinfo=timezone.utc),
        basis=ClockBasis.ORIGIN_OBSERVED,
        horizon="weekly",
    )

report_id = package["events"][0]["evidence_ids"][0]  # if an event is available
report = resolve_evidence(package, report_id)
```

`ReadRepository(connection)` supports an existing internal read connection; its SQL
fragments are code-owned and not a public query API. It permits only allowlisted source
tables and bounded SELECTs. `open` fails if the file is missing instead of creating a
writable database. No read writes to `context_snapshots` or any execution table.

Packages include their exact input records and a digest of definitions/implementation,
plus horizon, market cohort and funding cadence. Retaining canonical JSON freezes both
the values and the evidence resolver. The digest establishes content identity, not the
truthfulness of a provider or authenticity of an untrusted external receipt.

## Clock semantics and provenance

- **Origin observed:** actual stored observation/ingestion clocks. For funding and macro
  rows whose `available_at` is a settlement/vintage/release rule, a completed ingestion
  run supplies a conservative observation bound. The modeled source date is preserved,
  but cannot substitute for Prism receipt. Unproven observation clocks are withheld.
- **Origin committed:** requires an explicit post-commit bound for that exact evidence
  record. `processed_at`, publication time and an in-transaction receipt row do not prove
  commit. The neutral publication uses the existing after-COMMIT availability bound.
- **Consumer available:** additionally requires the selected consumer's durable receive
  and apply/availability clocks, ordered after the origin commit bound. Origin history
  cannot backdate a replicated consumer's knowledge.

An access catalog maps evidence content IDs to immutable `AccessClock` values. The
reader validates identity, clock ordering and source-observation lower bounds. The
publication-to-catalog utility only marks records for its context episode observed by
that receipt, plus their known mappings/source registrations. It excludes later reports
and all bar/positioning/macro records. It is an explicit export/subscriber utility; no
automatic clock persistence, replication or new runtime subscriber was activated.

Warnings do not enumerate inaccessible future records. The unproven-clock policy notice
is static, so a late unclocked backfill cannot alter an older package's diagnostics.

## Persistence definitions

These are descriptive historical summaries **known at the query cutoff**, not a claim
that Prism held every point contemporaneously throughout the window.

| Item | Daily / weekly rule |
|---|---|
| OI | 25 / 169 endpoint-inclusive hourly samples. Each uses a positive observed base-OI value no more than one hour old, with the one-hour boundary excluded. All samples must be covered before change is reported. |
| Funding | Settlements in `(cutoff-window, cutoff]` on the declared UTC cadence; no absent settlement is zero-filled. Require every expected settlement and finite rate before sign shares/sum are reported. |
| Cadence | Hyperliquid hourly is the frozen default. Binance is unknown unless the caller explicitly supplies this asset's 1h/4h/8h settlement cadence. The selected cadence is in the package definition identity. |
| Interpretation | Settled rate sum and positive/negative/zero shares; no compounding, projected carry, leverage, liquidation or directional edge. Existing current funding/crowding summaries retain their own minimum-history semantics. |
| Horizon selection | `fast`, `daily`, `weekly` select 7/30/90-day recent event views; relevant active older chains and known upcoming events remain eligible. This adds no monthly opportunity ranking or strategy. |

## Frozen compatibility and excluded scope

The released `context_snapshot_v1`, context ledger/model/taxonomy/positioning definitions,
Store migrations and both runtime profiles are unchanged. Existing execution consumers
continue reading v1. No frozen snapshot is recalculated under the new reader's meaning,
and no historical snapshot row is rewritten.

An independent comparison against the approved-main snapshot module produced an identical
empty-context payload and the same ID:
`ctxsnap_cd92a50b0a01e23b7a5609f3d2d4c865dc201462e047d1ac7a7cd32d5742ee26`.
Source-byte comparisons also confirmed unchanged snapshot, positioning, ledger, taxonomy,
Store and profile files. The legacy wakeup function, trigger identity and enrollment
arguments remain unchanged behind the new adapter.

No Radar Phase B, thesis lifecycle, LLM, risk/sizing integration, profile change, schema
migration, Dynamic Universe activation, frontend, deployment, Railway or OVH action was
performed. The active risk branch/worktree was not inspected or modified. Pre-existing
workspace documentation edits and evidence artifacts remain outside this change.

## Validation

Focused Phase A suite: **21 passed, zero failures/errors/skips**. Coverage includes:

- SELECT-only minimal schema, unchanged row counts/file checksum, missing-file failure,
  forbidden execution tables and independence from runtime authority/profile settings.
- Origin/commit/consumer delays, unknown access clocks, consumer identity separation,
  clock ordering, post-commit publication catalog and exclusion of later facts.
- Bar revision replay and breadth under observation/commit bases, event confidence/status
  updates and mapping/source provenance, immutable evidence resolution and tamper checks.
- Daily/weekly complete and gapped positioning, per-venue isolation, unknown Binance
  cadence, truthful funding/macro backfill access and replacement-aware OI reads.
- Neutral source ingestion with no execution schema, cross-connection proof of durable
  commit before delivery, failed delivery/retry and no duplicate logical event.
- Row/asset/package byte limits and deterministic same-cutoff output.

Full-suite result: **1,568 passed, zero failures/errors/skips**, two existing Starlette
TestClient deprecation warnings, in **2,074.46s (34m34s)**. The
[full results](evidence/shared-intelligence-phase-a/full-suite.xml),
[run log](evidence/shared-intelligence-phase-a/full-suite.log), and
[validation manifest](evidence/shared-intelligence-phase-a/validation.json) are retained.
Final command:

```text
.venv/bin/python -m pytest -n 2 --dist=loadscope -o addopts='' -q --junitxml=/tmp/prism-shared-intelligence-phase-a-full.xml
```

The sandboxed Starlette/AnyIO TestClient stalled before application code. The same gateway
test passed outside the sandbox; the full suite passed there using temporary fixtures.
The initial serial run was stopped during final causal diagnostic hardening and is not
counted as a passing full suite. Ruff passes across `src`, `tests`, and `dashboard`;
all **409 Python files** pass formatting checks; `git diff --check` passes.

## Resource bounds and measured cost

Each read is bounded to 24 cohort assets, 50,000 total source rows and a 16 MiB canonical
package body. Oversized queries fail explicitly. Reads use fixed lookbacks (40 days for
hourly activity, 30 days OI/ratios, 120 days funding) and bounded event/output selection.
No independent daemon, ingestion feed, account evaluation or rich microstructure collection
is added. Packages embed evidence; they are larger than a compact UI summary and should be
retained/shared by identity rather than copied into repeated display rows.

The [final local synthetic benchmark](evidence/shared-intelligence-phase-a/benchmark.json)
measured **0.18–0.19s wall / 0.19–0.21 CPU-seconds**, 30 SELECTs per read, **218 MiB** peak
process RSS, and a **560,706-byte** canonical package (**64,815 bytes gzip**) containing
577 evidence records. Three repeated reads were identical; daily/weekly coverage gates passed.
The [focused test results](evidence/shared-intelligence-phase-a/targeted.xml) are retained.
The benchmark uses six assets with 80 closed hourly bars each, 169 AAVE hourly OI/funding
points and one event. It excludes production history, network, writer contention and cloud
capacity/billing. RSS includes imports/fixture setup. It is a smoke cost measurement, not
approval to add workloads or a production SLA.

## Material limitations

1. Commit and receiving-consumer clocks are only as complete as the explicit receipt
   catalog. Historical data without those clocks cannot become consumer-available merely
   because it exists in an export. Unknown fields remain unavailable.
2. The legacy funding and Binance OI tables can replace values without keeping all prior
   versions. The new reader never exposes a replacement at its original ingestion time,
   but cannot reconstruct deleted old values from those tables. A previously retained
   package remains exactly resolvable; without one, unavailable history stays unavailable.
3. The original v1 breadth implementation is intentionally untouched. Its earlier audit
   finding is addressed in the new read contract, not by silently changing frozen bot
   snapshots. Any execution adoption requires a separately versioned, approved change.
4. Persistence is a descriptive definition with coverage gates, not statistical validation
   of a long-horizon trade signal. Short source history, missing events/market coverage and
   variable settlement cadence remain visible constraints.
5. No production-scale historical benchmark, cross-host replication, API transport,
   persistent access catalog or automatic snapshot archive has been activated. Existing
   runtime locks/ownership remain local; no shared writable database is proposed or opened.

## Readiness verdict

**PHASE A READY FOR REVIEW.** The full suite and focused causal/read-only checks pass.
Frozen source definitions and the independently checked legacy snapshot ID are preserved.
The implementation is scoped to the Phase A library/read and publication boundaries;
activation remains a separate decision outside this task. Stronger access bases require
explicit receipts, and unretained replaced history remains unavailable as described above.
