# Storage growth attribution and archival

Railway retains Prism's working recovery set. Historical originals are class B evidence, never disposable merely because ingestion succeeded.

## Production deployment

Latest approved main is operator-confirmed `36d75639e414e88d7488695808cf82dc1c1e23df`.
The isolated retention release `15d9eb495e37033ebf40a7fbcce6ec76a162b0f6` is its direct descendant.
Railway deployment `093555ca-e14c-4f86-a9a9-5957e1ac1a56` was created
**2026-10-12T11:30:33.785Z**, succeeded, and `/app/REVISION` confirmed that exact
revision at 11:33:30 UTC. Preflight: authoritative `railway-prism-runtime`, schema
30, ready for jobs. Collector health: healthy, no reported issues/warnings,
99.17–99.24% complete minutes over the preceding day. Six baseline coins only;
Dynamic Universe stage remains off. Runtime CPU work is unchanged.

Additional archive tooling deployment and post-offload inventory are recorded below
when independently confirmed. GitHub authentication is unavailable; exact release
history is preserved locally in `data/storage-retention-release.bundle` and the
storage archive release bundle. Deployment uses a committed `git archive`, excluding
unrelated dirty workspace code and all local data/secrets.

## Live backup retention

`BACKUP_KEEP = 1` globally across manual/daily/weekly directories. Optional second
backup requires an exact pinned rollback path and explicit deployment/migration
reason. No weekly hard-link/copy set is created. Supercronic has exactly one backup
entry: `30 1 * * * market ops cycle backup --trigger schedule --wait 3600` (UTC).
`microdir_weekly` is a research job, not backup retention.

An obsolete backup is removed only with matching verified off-host export receipt.
Two extant full copies block creation of another. **Operator export/retention is
required between backup cycles**; this policy bounds accumulation, but does not
promise indefinite unattended successful backups without export. Retention after
verification returns the default working set to one. Dashboard snapshot export reads
allowlisted tables from an existing recovery snapshot and writes locally, making no
Railway copy.

Retention release backup at 11:30:00 UTC:
`/data/backups/manual/prism-20261012T113000Z-pre-deploy-15d9eb495e37.duckdb`,
960,770,048 bytes, SHA-256
`ccf2aea1e77aed346de1809e7c9fbf9bbc9bea049e9fb122ef24dfc475aaed40`.
Its off-host gzip was checksum/decompression/all-table restore verified. The previous
906,768,384-byte copy (SHA `08bf19fb13e020f3db2cc314cfc7616453d7b29b7bcca6204351bd9212608e92`)
was removed at 11:39:45.787530 UTC after its own verified export. Exactly one then remained.

## Measurement and physical growth

`scripts/storage_growth.py` runs off-host. It sends a stat-only directory inventory
through SSH; no monitoring service, temporary DB, checkpoint, or archive copy is
created on Railway. It distinguishes logical lengths from allocated filesystem
blocks, deduplicates hard-link allocation, rejects symlinks, records file identity,
and compares additions/removals separately from net size. Sampled peaks are lower
bounds; churn between samples can be missed.

A prior exact inventory at **08:47:29 UTC** plus new inventories at 11:56:14 and
12:20:42 UTC provide a **3h33m** bounded physical window. The 24-hour hourly sampler
was started at 12:20:42 UTC, capped at 25 samples; completion is not claimed yet.
Existing verified DB snapshots cover **23h18m38s**, and retained provider files
supply seven complete captured-day observations. These are explicitly different
measurements; no steady-state forecast is claimed.

The preliminary physical interval ranked additions/day as follows (decimal MB):

| Category/source | Added MB/day | Net MB/day | Interpretation |
|---|---:|---:|---|
| Recovery backups | 6488.3 | 364.7 | One deployment replacement; not recurring retained accumulation |
| Authoritative DB | 421.3 | 421.3 | File high-water allocation, includes checkpoint/reusable effects |
| Provider FRED | 53.66 | 53.66 | Daily batch lands inside a short window; not steady rate |
| Microstructure raw | 43.87 | 43.87 | Growing working set before calendar-day expiry |
| Provider Hyperliquid | 19.34 | 19.34 | Burst/full response archives |
| Microstructure spool | 15.02 | 15.02 | Working set not yet at its eight-day bound |
| Provider SEC EDGAR | 11.84 | 11.84 | Daily batch inside short window |
| Provider Binance futures | 3.10 | 3.10 | Small files: allocated blocks greatly exceed gzip lengths |
| Journals/receipts/state | 3.07 | 0.47 | State replacement and receipts |
| Logs | 1.05 | 1.05 | Already rotated: 5 MiB × six files maximum |
| Remaining providers | 1.83 | 1.83 | See machine-readable per-family data |
| Evidence exports/calibration/temp | 0 | 0 | Present and measured, no observed increment |
| WAL | 0.055 | 0 | A transient 8 KiB WAL appeared then disappeared |

Every category's start/end allocated and logical bytes, bytes added/removed, net,
sampled peak, and file-count change are in `docs/evidence/storage-growth-attribution/physical-window.json`.
Filesystem availability is measured independently, including filesystem metadata.
A backup made/removed between samples may not appear in sampled peaks.

## DuckDB retained versus reusable growth

Read-only diagnostics decompress three verified off-host backups, verify original
SHA, query database size/free blocks, table rows and storage block IDs, then verify
that read-only inspection did not change restored bytes. Production was not vacuumed
or compacted. Only the ordinary pre-deployment backup checkpoint occurred.

| Snapshot UTC | File bytes | Used blocks | Free blocks | Block bytes |
|---|---:|---:|---:|---:|
| Oct 11 12:11:22 | 624,177,152 | 1,862 | 580 | 262,144 |
| Oct 12 08:37:38 | 906,768,384 | 2,452 | 1,100 | 262,144 |
| Oct 12 11:30:00 | 960,770,048 | 2,495 | 1,178 | 262,144 |

Occupied-block increase implies **181,622,439 bytes/day** over 20.44 hours and
**94,171,088 bytes/day** over the recent 2.87 hours. File-size increases were larger;
free blocks also increased. Latest free blocks represent approximately **308,805,632
bytes internally reusable**, not filesystem free bytes. At 11:56 the live file was
911,224,832 bytes, smaller than its 960,770,048-byte backup; at 12:20 it was
963,915,776 bytes. Checkpoint/allocation churn is material. WAL was tiny/transient;
no large temporary DB amplification was observed in the sampled inventory.

`paper_nimble_opportunities` gained **17,856 rows/191 data blocks** in the first
interval, then **2,520 rows/27 blocks** in the second: about 21,000 rows/day in both.
This is the largest attributable growing table data allocation. The first 191 blocks
are about 50.1 MB, not all 154.7 MB of occupied growth: indexes and metadata remain
unassigned and blocks may be shared. Trigger receipts grow about 26,600/day and
microstructure minute rows about 8,630/day. Some tables (macro history and most bar
history) hardly change. This is real retained row/data growth plus reusable/checkpoint
space, not solely a file-size artifact. No paper evidence or payload definitions changed.

## Provider archives: exact source attribution

Gzip bytes captured on the last complete UTC day (Oct 11), file count, and retained
compression ratio (decompressed envelope/body bytes ÷ gzip bytes):

| Family | Bytes/day | Files/day | Compression ratio |
|---|---:|---:|---:|
| FRED | 7,887,976 | 58 | 11.79 |
| Hyperliquid | 3,273,614 | 254 | 2.73 |
| SEC EDGAR | 1,743,922 | 6 | 15.68 |
| Binance futures | 287,286 | 616 | 4.27 |
| Hyperliquid intraday | 194,268 | 96 | 5.64 |
| DeFiLlama | 74,784 | 5 | 2.89 |
| EIA | 35,237 | 2 | 38.44 |
| Coinbase | 9,155 | 10 | 2.45 |
| Tiingo | 7,345 | 12 | 4.93 |
| Binance futures intraday | 0 | 0 | 3.08 |
| RSS CFTC/Coinbase/Fed/Kraken/SEC | 0 | 0 | 3.17–9.95 |

These are retained-file captured-day sums, not retrospectively measured filesystem
writes. Burst/bootstrap days differ: the protected Binance intraday bundle is
38,585,816 bytes, with no observed recent additions. RSS files remain from earlier
polling days; source polling is not changed. Seven-day medians/maxima and exact daily
series are in `provider-growth.json`.

Provider archives otherwise accumulate without expiry. FRED is the largest current
family (110,133,441 gzip bytes), followed by Hyperliquid (42,381,255) and the single
Binance intraday bundle (38,585,816), then SEC EDGAR (24,410,398).
Ingestion receipts reference `ingestion_runs.raw_paths/raw_sha256`; context sources
also reference **`context_provider_runs.payload.raw`**. Runtime/research normal
consumers query DuckDB; context restart state comes from DB payload.state, not raw
responses. Historical raw paths are provenance, and full responses can include
filtered/unnormalized records absent from DuckDB. Those records are preserved off-host.
No claim that DuckDB duplicates every raw byte is made. Normal runtime does not read
settled historical originals; deep replay/audit/research may, and must resolve their
verified off-host archive.

## Microstructure recovery chain

Active collector journals, normalized minute/revision spool, committed DB offsets,
watermark, collector state and large-print calibration form a joint recovery chain.
Successful ingest alone does not prove raw or spool disposal safe.

Existing collector policy is unchanged: raw day directories older than current UTC
date minus one day expire; spool directories older than minus eight days expire only
when the ingest watermark is past that day; calibration files expire beyond ten days.
A whole-day policy retains up to two raw calendar days and nine spool calendar days.
The large-print threshold for day D uses completed days D−7 through D−1; missing
calibration can be rebuilt from spool. This explains the existing eight-day spool
working set. An ingest outage extends spool retention rather than removing unread data.

V1 exports closed raw/spool hours and completed-day calibration **copy-only**. It
never deletes microstructure files, including consumed spool. A real off-host closed
hour (79,078 events, 6,586,755 decompressed bytes) passes strict gzip/read and existing
Engine.replay, emitting 366 records with an explicit fresh start seed. This exercises
read/replay capability, not exact historical reconstruction without preceding state
and calibration. A consumed spool hour (350 records/602,187 bytes) passes complete
Spool.read_lines; calibration histogram passes the existing threshold reader. Full
microstructure regression/restart/revision tests pass. Exact rates and consumed versus
pending sizes are added to the final measured inventory below.

## A/B/C classification and explicit eligibility

| Class | Files | Capability lost if removed from Railway |
|---|---|---|
| A: must remain | Live DB/WAL, default verified backup, optional pinned rollback, active/pending spool/raw, offsets/watermark/state/calibration, latest source run, rejected/partial/failed/unmatched raw, open replay/research dependencies | Local crash recovery, current ingest/revision/calibration, or an unproven evidence dependency |
| B: off-host history | Provider originals with every contained body proven settled in the verified recovery ledger; historical recovery copies with verified export | Direct local historical read; audit/deep replay/disaster recovery remains available from the verified off-host copy |
| A in v1 (copy-only) | Closed microstructure raw, consumed spool, completed calibration | Exact recovery/replay dependence has not been proven absent; no deletion permission granted |
| C: disposable | **None identified** | No scientific originals classified disposable |

Provider B eligibility requires all of the following, not age alone:

1. Approved provider/family/day/run path, independent regular file, no symlinks/traversal/hard links, stable size/inode/device/mtime.
2. Strict gzip, valid envelope/bundle and JSON or existing RSS reader; every body SHA matches its envelope.
3. Exact complete multiset of body hashes matches all file references in successful terminal `ok` runs in a SHA-verified immutable recovery DB. No rejected rows, failed/partial/running ingest, missing reference, or incomplete bundle.
4. Capture and ingest completion timestamps are at/before the committed backup boundary, preserving causal timezone/subsecond precision. Latest successful per provider/dataset/entity is protected.
5. Complete open-file scan; no pending consumer or explicit replay/research reference. Operator records the source-reviewed dependency clearance.
6. Verified off-host artifact with exact source SHA, byte length, schema/format version, full decompression/parse/read result and machine-readable receipt.
7. Fresh eligibility, identity and SHA recheck under the runtime writer lock immediately before exact deletion. Every intent and completion is fsynced in `/data/backups/raw-archive-deletions.jsonl`.

Invalid RSS responses remain protected as parse failures. Unmatched older sources
remain A even when apparently redundant; the tooling never guesses a path alias or
settlement boundary. V1 uses the last verified backup as its recovery cutoff,
protects newest per-source observations, and makes no arbitrary 24h/48h/7d provider
expiry promise. With daily backup plus successful operator offload, settled provider
history can leave after the next verified recovery boundary, while protected latest
observations and exceptions persist. A missed backup/export extends retention safely.

## Off-host operator workflow

Run on the archive machine, never the authoritative Railway container. Default
relative destination: `data/backup-archive/{provider-raw,microstructure-raw,manifests}`;
DB archives can use `data/backup-archive/db` (existing verified archive directory is
also supported). Committed code contains no user-specific absolute local path.

```sh
PROJECT=3c479387-0ddf-40a5-b065-52e49cf3e2cd
uv run python scripts/backup_archive.py data/backup-archive/db --all --retain
# Review, then explicitly apply if verified:
uv run python scripts/backup_archive.py data/backup-archive/db --all --apply-retention

uv run python scripts/raw_archive.py list data/backup-archive/manifests/provider.json \
  --project "$PROJECT" --type provider-raw \
  --dependency-review 'Record the reviewed runtime/replay/research dependency clearance'
uv run python scripts/raw_archive.py export data/backup-archive/manifests/provider.json \
  --project "$PROJECT"
uv run python scripts/raw_archive.py apply data/backup-archive/manifests/provider.json \
  --project "$PROJECT"                 # dry-run, no deletion
uv run python scripts/raw_archive.py apply data/backup-archive/manifests/provider.json \
  --project "$PROJECT" --apply         # explicit operator deletion

uv run python scripts/raw_archive.py list data/backup-archive/manifests/micro.json \
  --project "$PROJECT" --type microstructure-raw
uv run python scripts/raw_archive.py export data/backup-archive/manifests/micro.json \
  --project "$PROJECT"                 # copies only; apply refuses microstructure
uv run python scripts/raw_archive.py read data/backup-archive/microstructure-raw/DAY/HH.jsonl.gz \
  --project "$PROJECT" --type microstructure-raw

uv run python scripts/storage_growth.py data/storage-growth-window --project "$PROJECT" \
  --samples 25 --interval 3600
uv run python scripts/storage_growth.py data/storage-growth-window --project "$PROJECT" --summarise
```

Archive transfer streams existing files over SSH in bounded batches, creates no
Railway archive/temp full copy, and manually validates numeric regular tar entries
without extractall. Source identity/SHA is checked around transfer. Already-gzipped
originals remain byte-identical. Local artifacts and receipts commit atomically after
checksum/decompression/read verification. Receipt fields include source path/type/provider,
first/last timestamps, source/compressed/logical sizes and hashes, format version,
export time, verification status, recovery/ingest boundary and deletion eligibility.

All local artifacts reverify before any apply. Receipts stream over SSH stdin;
max 10,000 files/64 MiB request. The complete selected batch is validated before its
first deletion, then each source is rechecked at unlink. A mismatch stops the workflow;
any already completed deletions are exactly logged. No wildcard recursive cleanup,
automatic offload cron, or automatic provider deletion is installed. Keep the local
artifacts/manifests backed up: one off-host destination is not a distributed archive.

## Headroom, runway and cost

Runway must reserve the next full copy, checkpoint/copy overhead, raw/spool reserve
and filesystem comfort. Existing backup creation guard requires 1.2× current DB free
headroom while preserving 5% filesystem reserve. Future DB growth also enlarges the
retained backup and the next-backup reserve: counting only live DB growth is unsafe.
Internal free blocks may defer allocation but are not credited as filesystem capacity.

The 30-day target is **not proven** by the current volume. Even the lower observed
occupied growth is 94.2 MB/day of authoritative data; maintaining a full recovery copy
and safe next-copy headroom compounds capacity requirements. Raw archival alone
cannot make evidence growth disappear. Previous 13.4/22.7 GB/month numbers remain
historical sensitivities; new bounded observations are not proven steady-state rates.
Final scenario inputs/results are stored in `runway.json` below. No storage upgrade,
plan change, purchase, CPU optimization or semantic reduction is authorized/performed.
Railway documents $0.15/GB-month volume usage and $0.05/GB egress; subscription credits
and actual consumed bytes affect billing, not simply provisioned capacity. Sources:
https://docs.railway.com/pricing/plans and https://docs.railway.com/volumes/reference
(checked 2026-10-12). Larger capacity recommendation and cost are conditional on these
sensitivities and existing account entitlement; no paid action follows from this report.

## Dynamic Universe gate and safety

**BLOCKED.** Thirty-day conservative comfortable runway is not demonstrated.
No Tier 0 toggle, satellite warming or dynamic paper execution is enabled. Existing
six-coin execution/research/source polling/quality/OI/funding/evidence identity/causal
timestamps/warmup and hypothesis/event/feature definitions remain unchanged. Approved
storage guards and backup architecture are the only core runtime changes; archive
module/scripts are operator-only additions.

## Validation and rollback

Retention: all **1,393** collected full-regression cases have passing results across
three non-overlapping completion runs. Archive/recovery/provider/runtime/dashboard/
discovery regression run: 349 passed plus one environment-dependent fixture failure
(process permissions). Fixture process scan was isolated; all 26 focused archive/
attribution tests then passed, including the failed case and added protections.
This is not a claim that the initial 350-case run was green. Repository-wide Ruff
and formatting (439 Python files), shell syntax and diff checks pass.

Tests cover backup global rotation, scheduler policy, local dashboard snapshots,
physical attribution, ledger/context references, UTC/subsecond cutoffs, manifests,
exact export/hash/decompression/read/replay, failed verification, source mutation,
symlinks/traversal, active/current spool protection, dry-run, exact intent/completion
logs, DB-only context restart with historical raw removed, and discovery compatibility.

For code rollback, deploy the exact retained retention release `15d9eb495e37033ebf40a7fbcce6ec76a162b0f6`
from its Git bundle using the project-pinned deployment script; this preserves corrected
retention. Avoid rolling back to the older rolling-backup implementation. Core schema
is unchanged, so no DB rollback is required for the operator tooling.

For historical raw restoration, verify artifact SHA against its `.manifest.json`,
decompress/read-test with `scripts/raw_archive.py read`, and either point historical
replay at the off-host file or copy the exact byte-identical artifact back to its exact
approved `/data/raw/family/day/run...` source path. Hold the runtime lock for a
production restore; reject existing mismatched destinations/symlinks, verify copied
SHA and fsync before releasing the lock. Never replace a current source with a different
file. Local deep replay requires the recorded calibration/start/revision state.

For disaster DB recovery, stop **all** DB writers/collectors/scheduler, verify the
selected full archive artifact and decompressed SHA, restore the matching schema-30
DB per `docs/OPERATIONS.md` recovery procedure, retain authority metadata, and validate
all-table restore fingerprints, preflight, offsets/watermark, collector restart and
paper receipts before resuming. Preserve the damaged DB/WAL separately for diagnosis.
Raw archived files and their receipts remain available to extend recovery/replay.
