# Railway research profile cutover — 14 October 2026

Historical audit: the research-profile cutover objective was superseded by the
[full OVH consolidation plan](FULL_OVH_CONSOLIDATION_PLAN.md). The findings below
describe the October 14 frontier; current stabilization status is in that later plan.

**BLOCKED — MORE HEADROOM/WORK REQUIRED.** No deployment, production migration,
job disablement, volume purchase, production retirement or OVH operation was performed.
The audit found an existing recovery/collection problem: the October 14 daily backup
failed its space guard and intraday bars have been refusing writes. Switching profiles
does not fix either problem.

Evidence: [20261014 audit directory](evidence/railway-research-profile-cutover/20261014/).
All times below are UTC; GB means decimal bytes / 1,000,000,000. Full DB artifacts
remain outside Git in `data/backup-archive/railway-research-cutover-20261014/`.

## Current Railway workload inventory

Railway API confirms exactly one service, `prism-runtime`, one running replica,
deployment `3ef1c302-90c4-48bb-a626-e1ea434d22b4`, revision
`8f9bc3473142eb6b8a5ea54a3987db8ef2bcb0cf`, and one `/data` volume.
There are no other services, environments or buckets in this project. This does not
inventory unrelated projects in the Railway account. Production is **legacy/schema 30**,
`PRISM_NIMBLE_RUNTIME=off`, gateway/free sources on, Dynamic Universe absent. Its DB
authority remains `railway-prism-runtime` (claim from October 4).

U = unmeasured, not zero. CPU is CPU seconds / wall seconds. Persistent-process values
come from the 60.005-second OS-only sample, not the DB-scanning audit. Scheduled-job
duration is the mean wall time of successful cycles in the preceding 48 hours; it is
**not CPU time**. This window mostly predates Nimble shutdown. Per-job peak RSS and DB
bytes written are not instrumented. Do not allocate shared process costs twice.

| Workload | Classification | CPU / memory | Cadence | Storage / DB writes | Dependency; superseded by OVH? |
|---|---|---|---|---|---|
| Railway service, tini, supervisor shells | REQUIRED_RESEARCH | Container 0.1985 cores in short sample; memory.current 3.504→3.486 GB, includes cache; tini ~1.3 MB, shells ~0.4 MB each | Always | Bounded operational files; shared volume below | Owns research authority. No |
| Context gateway HTTP/MCP | REQUIRED_CONTEXT | 0.00183 cores; 160,960,512-byte RSS | Always; HTTP arrivals; minute watchdog | Filesystem receipts/state; 49,152 process write_bytes in sample; HTTP never opens DB | Authorized Work ingress/OAuth. No |
| Work ingestion + free-source ingest + Work descriptive research | REQUIRED_CONTEXT | **One** context_ingest worker: 0.12216 cores; 230,858,752-byte RSS | Ingest attempts every 5 s; quality/reaction work every 60 s | 126,976 process write_bytes/min sample, not DB-byte attribution; 200 Work reaction rows timestamped in prior 24 h | Durable submission receipts, outbox, provenance and descriptive reaction collection. No |
| Free-source collectors | REQUIRED_CONTEXT | 0.00150 cores; 164,052,992-byte RSS | Persistent source due/backoff loop; source-specific intervals | 122,880 process write_bytes/min sample; 3 free-ingest rows timestamped in prior 24 h; shared context/spool state | Shared Intelligence. Similar acquisition on OVH is independent lab input, not a replacement |
| Dedicated free_source_ingest | REQUIRED_CONTEXT | **Absent**, no independent cost | Only used when gateway ingest is absent | Would drain same spool | Research uses context_ingest instead. No duplicate worker |
| Macro jobs `context_macro` | REQUIRED_CONTEXT | CPU/RSS U; mean 36.2 s/cycle | 00:20, 12:42, 13:42, 19:12 | Provider raw; context releases/updates. Job-specific bytes U | Research/context first-seen/revision semantics. No |
| Official news/status `context_news` | REQUIRED_CONTEXT | CPU/RSS U; mean 27.6 s/cycle | Hourly :08, :38 | Shared raw/events; 2 events and 3 updates timestamped in prior 24 h, not all attributable to this job | Includes legacy/free-source deduplication. No |
| Broad data/update/scan `daily` | REQUIRED_RESEARCH | CPU/RSS U; mean 173.6 s/cycle | 09:00 | Provider raw, data/scans; bytes U | Research Lab and Intelligence inputs. No |
| OI `oi` | REQUIRED_RESEARCH | CPU/RSS U; mean 30.5 s/cycle | 02:30, 08:30, 14:30, 20:30 | 144 OI history + 204 observation rows timestamped in prior 24 h across sources | Positioning and research inputs. OVH does not replace Railway history |
| Hourly positioning | REQUIRED_CONTEXT | CPU/RSS U; mean 40.2 s/cycle | Hourly :04 | 576 ratio rows timestamped in prior 24 h; raw capture | Causal positioning context and Phase 24B features. No |
| Settled funding `paper_v2_funding` | REQUIRED_RESEARCH | CPU/RSS U; mean 28.5 s/cycle | Hourly :05 | 154 funding rows with availability in prior 24 h, across acquisition paths | Shared historical funding; unresolved legacy closes. Legacy name does not mean v2 execution. No |
| Intraday bars `intraday` | REQUIRED_RESEARCH | CPU/RSS U; successful mean 28.8 s/cycle; failures mean 25.4 s | :01, :16, :31, :46 | 867,878 retained intraday rows. **109 failed / 83 successful cycles in 48 h; recent step exit 3** | Required data; disk guard is refusing acquisition. Do not disable or bypass it. No |
| Core-six microstructure collector | REQUIRED_SCIENCE | 0.00533 cores; 188,825,600-byte RSS | Continuous WS; minute aggregation | 245,760 process write_bytes/min sample; raw/spool growth below | Governed Railway source/history identity. OVH's independent capture cannot supersede it |
| Microstructure ingest/health | REQUIRED_SCIENCE | CPU/RSS U; mean 32.0 s/cycle | :06, :21, :36, :51 | 8,634 minute rows with ingest timestamps in prior 24 h; 60,318 total, 28 retained revisions at snapshot | Required continuity, offsets, coverage and provenance. No |
| Phase 24B daily checkpoint | REQUIRED_SCIENCE | CPU/RSS U; mean 50.6 s/cycle | 01:10; owed fixed grid caught up | 1 study, 7 checkpoints, 7 results retained | Governed study and authority remain Railway. No |
| Phase 24B weekly checkpoint | REQUIRED_SCIENCE | CPU/RSS U; no cycle in this 48 h window | Monday 01:20 | Same governed tables | Required fixed weekly grid. No |
| Research scheduler `supercronic` | REQUIRED_RESEARCH | ~0 sampled cores; 16,871,424-byte RSS | Always; current 33 cron entries | Cycle/step evidence and bounded logs | One observed scheduler; contains acquisition and science as well as research. No |
| Prospective/forward Research Lab | REQUIRED_RESEARCH | CPU/RSS U; combined prospective mean 111.7 s/cycle | 00:10, 00:45, 03:00, 06:00, 11:00, 17:00 + boot catch-up | 18 forward-input and 24 outcome rows timestamped in prior 24 h | Frozen Research experiments. No |
| Copilot and incubation | REQUIRED_RESEARCH | Shared prospective cycle; separate CPU/RSS U | Same six prospective times | Copilot/manual/incubation records; separate bytes U | Retain manual records and research definitions. No |
| Paper v1 lab paper/brief/shadow steps | LEGACY_DRAIN | No active step cost: v1_work_needed=false | Potentially within prospective/intraday | Historical evidence retained, no new execution steps required | Existing filtering removes paper/brief/shadow steps. No legacy transfer |
| Paper v2 evaluate/brief drains | LEGACY_DRAIN | No running process; work_needed=false | **Already excluded from live crontab** | Historical retired baseline; 7 closed trades, equity 99.2005459 at retirement | RETIRED since October 10; approved profile retains conditional drains, not active execution |
| Nimble worker / new Nimble paper execution | DUPLICATED_WITH_OVH | **Off**, zero worker cost now | Absent | Historical opportunity/trade/ledger tables retained | OVH owns new lab rehearsal. Old Railway accounts must not be copied into it |
| Paused Nimble funding/accounting/research | LEGACY_DRAIN | No active process; no autonomous worker to restart | Explicit one-shot design below | Six unsettled closes; 216 pending items / 552 outstanding horizons | Exact obligations below. OVH does not supersede historical obligations |
| Nimble daily brief | AUTOTRADER_ONLY | CPU/RSS U; mean 26.7 wall seconds/cycle | **00:14** | 12 retained reports; delivery history retained | Execution reporting; no observed Research/Context consumer. Remove from research schedule |
| Backups | REQUIRED_RESEARCH | CPU/RSS U; October 13 success 69.3 s; October 14 failed 11.3 s | 01:30 | One full 1,218,195,456-byte backup; next full copy is space-blocked | Recovery prerequisite; off-host capture is supplemental, not a functioning scheduled replacement |
| Gateway/free-source watchdogs | REQUIRED_CONTEXT | CPU/RSS U, short CLI processes | Every minute each | Health/state/log records; bytes U | Keep worker and gateway checks. No |
| Shared Intelligence read APIs | REQUIRED_CONTEXT | Use existing gateway/read paths; no separate Railway process observed | On demand | Read-side consumers, shared evidence | No separate service to remove. No |
| Dashboard/Streamlit | OPTIONAL | No Railway process, zero separate Railway resource cost | Local/on demand | Local snapshots outside volume | Known local authority rejection below; no scratch workaround |
| Dynamic Universe radar/rich warming, sizing cohort, baseline paper lab authority | AUTOTRADER_ONLY | **Absent**, zero current Railway cost | Not activated | No new schema-31 policy/evaluation rows should be created by research | Keep absent; OVH rehearsal settings untouched |

No unexplained persistent application process was found. The OS sample has four persistent
Python workers plus one scheduler. Init/supervisor entries and a zero-RSS leftover shell
also appear; the initial probe did not record process state, so do not count that shell as
an active workload. The first DB-audit resource sample mislabeled its own Python `-c`
probe as a collector because the source included that string. The second audit corrected
classification; use `quiet-resource-sample.json` for workload figures.

## Current storage breakdown

Measured 17:32:11; final filesystem sample 17:44:48. All regular paths/inodes/link counts
and provider subcategories are in the inventory. Allocation is inode-deduplicated.

| Category | Logical bytes | Allocated bytes | Policy / authority / recovery / deletion assessment |
|---|---:|---:|---|
| Live DuckDB | 1,646,276,608 | 1,646,280,704 | Authoritative schema 30; never delete/compact to force cutover. Fresh verified off-host snapshot below |
| One daily backup, October 13 01:30:14 | 1,218,195,456 | 1,218,199,552 | Global keep=1; verified off-host this phase. Preserve on Railway; not redundant |
| Provider raw | 161,010,328 | 171,745,280 | Run envelopes/history; scientifically evidentiary. Prior 6,255 exports are verified, not a blanket receipt for these 3,747 remaining/new files. Only per-file verified-archive eligibility could authorize offload |
| Microstructure raw | 89,624,402 | 89,862,144 | Existing bounded collector retention; exact archives available only to previous capture frontiers. Current material is not all verified elsewhere; no expiry change/manual deletion |
| Microstructure spool | 104,691,260 | 105,717,760 | Offsets/consumed state do not alone prove replay dispensability. Preserve unread tails, revision recovery and governed journals |
| Large-print calibration | 114,823 | 126,976 | Scientific thresholds/calibration, seven files; prior copies do not cover all new files. Preserve |
| Journals/receipts/state | 17,783,862 | 18,227,200 | 161 files: provider deletion audit, context gateway/free-source receipts/state, collector state, legacy markers and lock. Includes 16,421,762-byte prior deletion audit. Preserve causal and unresolved accounting evidence |
| Logs | 8,421,200 | 8,433,664 | Existing runtime rotation; three files. Could archive stable rotated logs after checksum verification, but <8.5 MB cannot solve capacity; not removed |
| Evidence exports/baselines | 12,397 | 16,384 | Required frozen evidence. Preserve |
| Backup metadata | 252,792 | 286,720 | Recovery manifests, retention/export receipts/policy. Preserve |
| Temporary files | 63 | 4,096 | Sole classified file is the operational runtime lock; not deployment garbage. Preserve while runtime active |
| Volume archives / obsolete deployment or test artifacts | 0 material files found | 0 | Off-host artifacts live on the operator machine. `/app` build and `/tmp` generated crontab are outside persistent volume |
| Filesystem metadata/unattributed allocation | — | 2,457,600 | statvfs used minus regular-file blocks, including directories/FS accounting and non-atomic sample timing. Not a delete candidate |

Regular-file allocation totals **3,258,900,480** bytes. Filesystem used is
**3,261,358,080**, usable capacity **4,838,498,304**, available **1,560,363,008**.
The used+available difference from total includes filesystem reserved space. Final
available: **1,559,826,432**, live file size unchanged. DuckDB diagnostics report 2,455
internally reusable 262,144-byte blocks; those are **not filesystem free space**.
Metadata and DB stats are sequential observations, not an atomic filesystem checkpoint.

Railway API reports `currentSizeMB=3755.474944`, differing from OS used bytes by
494,116,864 at the initial sample. Its reporting frontier/semantics were not established;
do not invent a file category to reconcile it or use it instead of statvfs for admission.
Nominal 5,000,000,000 bytes are not usable filesystem capacity.

Reconciliation with prior archive work: October 12 provider cleanup removed 6,255 files,
98,233,925 logical / 117,596,160 allocated bytes after verified export, but created
~16.4 MB of audit records. Two old backups rotated as replacements were created; this
was not net 1.87 GB reclamation. October 12's 968,110,080-byte pre-deploy backup is now
off-host history; the October 13 daily file replaced it. Provider working data increased
from 134,090,268 to 161,010,328 logical bytes, live DB from 1,018,703,872 to
1,646,276,608, and free space fell from 2,528,350,208 to 1,560,363,008. No new claim is
made that current raw/spools are all recoverable from those earlier archives.

## Minimum safe migration headroom

`minimum_safe_free_space = NOT YET BOUNDED — FAIL CLOSED`

An exact worst-case X cannot honestly be supplied from a sampled migration peak and an
incomplete post-Nimble growth window. This is an unresolved admission gate, not a guessed
number. Known requirements already fail, so production is stopped before migration.

For the **existing approved deployment/backup path**, B=1,646,276,608 and
V=4,838,498,304 bytes. `backup_live` checks free space before copying/checkpointing:

`ceil(1.2 × B + 0.05 × V) = 2,217,456,845 bytes`

That guard includes one new full backup (B), a 20% copy/checkpoint contingency
(329,255,322 rounded bytes), and a 5% filesystem reserve (241,924,916 rounded bytes).
The `.part` is renamed into the final backup: **not two full new copies**. Existing
retained backup is already counted in used space. Compression/export happens off-host,
with zero Railway export temporary bytes. Initial free space falls short of this guard
by **657,093,837 bytes**; final shortfall **657,630,413**.

Safe rollback preserves the failed live DB/WAL and writes a checksum-verified restored
DB to `.part`, then renames it. Reserve a further **B=1,646,276,608** bytes for that
recovery copy. The measured schema rehearsal used zero external migration temp bytes,
no net DB growth, a sampled 4,096-byte WAL peak and 8,192-byte total additional allocation.
That result is a **lower-bound observation**, not an enforced worst-case cap.

Thus the same-volume path has a known conservative admission lower bound of
**3,863,741,645 bytes before ordinary growth and any larger migration/checkpoint peak**:
backup guard + recovery copy + observed migration increment. Off-host fresh capture
removes the new backup/copy requirement but still leaves a recovery+reserve lower bound
of **1,888,209,716 bytes before ordinary growth or larger migration peak**. It does not
make the existing daily backup job or deployment wrapper work.

The October 12 15:30→October 14 17:32 physical comparison spans 50.03 h and measures
~301.06 MB/day DB net growth, 16.23 MB/day provider growth, 10.72 MB/day raw growth and
15.58 MB/day spool growth. These include Nimble execution, archive/rotation effects and
allocator reuse. They are **neither a post-shutdown baseline nor upper bounds**. A
planning projection for live DB + one future backup and those journals is ~644.65 MB/day,
before other files; it must not be substituted for worst-case G.

To derive a numeric admissible X, measure a representative post-shutdown window spanning
daily research/Phase 24B/backup obligations, declare a near-term horizon (at least 24 h
for planning), bound write/temp growth or fail before exceeding the reserved budget, and
repeat the migration/checkpoint experiment with the intended production DuckDB version,
limits and current frontier. Recompute `X = backup/copy budget + migration/checkpoint
budget + rollback reserve + bounded runtime growth + safety floor`, avoiding overlapping
allocations. Require comfortable free-space margin above X, not merely equality.

## Safe reclamation performed

**Removed: 0 files, 0 logical bytes, 0 allocated bytes, 0 GB.** No destructive retention
operation was run. No scientific artifact was deleted to manufacture headroom.

Verified off-host copies created: retained backup **98,650,950 compressed bytes** and fresh
production snapshot **134,445,581 compressed bytes**, totalling **233,096,531 bytes**.
Their source DBs are 1,218,195,456 and 1,646,276,608 bytes. A separate 98,650,950-byte
local transfer-proof artifact is retained as test evidence; it is not additional Railway
backup protection. Export registration can refresh small production backup sidecar metadata;
there was no backup pruning, new full volume copy or scientific DB write.

Reclamation preference review: no deployment/test trash; one required backup, no duplicate;
current provider set only 171.75 MB allocated; raw/spools have governed recovery obligations;
logs too small to help. Even deleting **every** other regular file beyond live DB and its
backup (394,420,224 allocated bytes, including protected evidence) would leave less than the
normal 2,217,456,845-byte backup guard. This is an upper-bound impossibility check, **not
authorization to delete those files**.

## Remaining storage risk and backup relocation

The 01:30 backup failed on October 14 at a 1,363 MiB live size. The latest retained volume
backup is therefore ~40 h old at audit time. Intraday's capacity guard reserves projected
growth across live+retained copies plus next-backup headroom; recent bars exit 3 without
writing. Other evidence continues growing. Formal profile change will not restore backup
cadence or intraday coverage.

Existing `backup_archive.py` safely streams **existing immutable** backups without a
Railway intermediate file, then checks compressed/logical SHA and all-table restore.
It cannot itself create a fresh live recovery point. `PRISM_BACKUP_DIR` changes destination,
but `backup_live` still guards space on the DB filesystem; pointing it at `/tmp` does not
solve this safely, and ephemeral storage alone is not recoverable rollback protection.

Prepared and exercised `scripts/railway_snapshot_offhost.py` as a supplemental capture:

1. Local proof against the verified retained schema-30 backup reproduced its SHA and passed
   decompression/all-table restore before the production invocation.
2. Existing runtime flock + read-only DuckDB connection fence all writers. Refuse nonempty
   WAL, symlinks, wrong authority, changing inode/mtime/size or checksum; bounded 180 s alarm.
3. Stream compressed chunks to a temporary **off-host** directory. No production checkpoint
   or full backup file. Record revision, source SHA, schema, authority, completed-cycle and
   Nimble ledger frontiers, and the read fence interval.
4. Publish only after a complete transfer, source SHA matching, independent decompression,
   all-table read/restore and fsync/atomic artifact+receipt publication. Failed/incomplete,
   bad-base64, checksum-mismatched and duplicate-manifest streams publish nothing (four tests).
   A fifth test rejects running the archive destination command as a production runtime.

Fresh capture: **17:37:51.558770→17:38:16.829195**, WAL zero, authority
`railway-prism-runtime`, latest completed cycle **17:36:41.317697**. Logical SHA:
`7c0377ac2566c7962d12a6714235f07871a286250c4153fb3b12f688854d4961`.
Compressed SHA:
`2152715859263fa45b2ffa27aee4df153f2584afb33f8eebf292753475bff109`.
It is a recovery point for that frontier, **not future writes or external raw directories**.
The 25.27 s read fence temporarily queued DB writes; passive collectors kept running.

The default deploy wrapper still requires its normal fresh local backup. It has **not**
been modified to skip that guard. This one-shot off-host capture is not a new recurring
backup service, automatic retention receipt or reason to delete the last local backup.
The archive is on the established operator machine, not an independently replicated cloud
archive; preserve it and its manifests/code revisions independently for host-loss safety.

Documented recovery: verify compressed artifact SHA, decompress to an operator scratch
path, verify logical SHA and all-table readability against the manifest. For production,
stop all DB writers/scheduler and pause collectors with an explicit governed restart plan;
preserve damaged DB/WAL and spool tails, upload verified schema-30 bytes to a **new** `.part`
path, checksum/fsync it, then replace atomically only under explicit recovery approval.
Restore matching revision `8f9bc3473142…`, retain `railway-prism-runtime` authority and
`PRISM_NIMBLE_RUNTIME=off`; remove an incompatible explicit profile if rolling code back.
Validate authority, table/ledger frontiers, offsets, Work receipts and collector health
before resuming. Do not reverse schema 31 in place or invoke default Nimble-on legacy flags.

Safest next options are a separately approved volume expansion with bounded runway, or
integrating the proven off-host capture/restore protocol into deployment and scheduled
backups with revised capacity guards **and** sufficient rollback/growth space. Neither
was applied. Offloading today's small raw working set cannot solve the normal backup path.

## Legacy Nimble retirement plan

The three historical IDs are fully recorded in `legacy-obligations.json`. Short names:

| Run | Closed / cancelled | Status | Unsettled closes | Pending items / outstanding horizons | Last report |
|---|---|---|---:|---:|---|
| Baseline `a8aef8be…` | 15 / 58 | ACTIVE, flat | 3 | 72 / 184 | October 13 |
| Immediate `a5fafb2c…` | 17 / 45 | ACTIVE, flat | 1 | 72 / 184 | October 13 |
| Confirmed `981fac86…` | 22 / 41 | ACTIVE, flat | 2 | 72 / 184 | October 13 |

Zero PENDING/OPEN/EXIT_PENDING positions. Historical cash/equity are 99.0607417869,
98.5138506353 and 98.6545600367; preserve them until known funding adjustments are appended.
Six provisional closes depend on four distinct missing settled asset/hour observations:
HYPE October 10 16:00, HYPE October 11 17:00 (three runs), AAVE October 12 23:00, ETH
October 13 00:00. The DB has **zero matching Hyperliquid settled rows** at those boundaries.
Do not substitute predicted rates, zeros or newly invented historical availability.

Pending items reference October 14 08:06–16:01 across BTC/ETH/SOL/LINK/AAVE/HYPE, with
15/30/60/120/240/480-minute horizons. Latest owed horizon is **October 15 00:01:00.919223**.
At this snapshot, those later observations do not exist yet; waiting for the governed
Railway collector is necessary. Shared core-six collection remains required independently.

Prepared explicit **local-only** `scripts/nimble_retirement_rehearsal.py` on the verified
fresh backup. Calls only existing `Sources.funding`, `Sources.research_quotes`,
`engine.reconcile` and `engine.research` on a disposable copy. No monitor/admit/evaluate,
worker, activation, notification or terminal transition. It completed eight items per
run (72→64), left all six funding closes unresolved, changed no opportunity counts or
ledger sequences, and was idempotent on repeat. Source checksum remained unchanged.
Historical research has 3,730/9,489 rows marked missing_path and a 171.35 s max delay;
those are existing+rehearsed aggregate quality flags, not permission to fill gaps or a claim
that all new completions had missing paths. Preserve the semantics and quality flags.

**A:** Existing reconciliation/research primitives can settle obligations on Railway through
an explicit bounded one-shot; autonomous Nimble need never restart. Retrieve missing
historical settled funding with source envelope/provenance and actual ingestion availability,
then rehearse the adjustments on a newer backup. Continue research after the final due time.

**B:** Preferred final mechanism is a later explicit `plan` → `settle` → `finalize` command,
not a scheduler dependency. Bind plan to DB SHA/authority/run IDs/ledger frontier. Settlement
rejects nonflat runs and disallows new opportunities/fills; it appends funding adjustments
and descriptive outcomes atomically/idempotently. Finalize refuses any remaining settlement,
research or undischarged report obligation, creates a final local report for October 14,
records closure/reason/frontier and digests for all run-linked evidence, and appends a
terminal RETIRED lifecycle event. Require explicit approval for production finalization.

Immutability needs guards in every ledger/projection/research/report/worker entry point,
including cohort children and recover/replay semantics. Restarting a worker must neither
recreate old triggers/opportunities nor mutate retired runs. A zero-cost lifecycle lookup
must suppress legacy brief/settlement scheduling once archived. This is broader than a
safe small `status='RETIRED'` patch, so **no incomplete production finalizer was introduced**.
Only the explicit copy-only rehearsal is executable now.

**C:** Indefinite archival is safe only if explicitly labelled incomplete with an immutable
unresolved-obligation manifest; it is not historically complete retirement. It can require
zero recurring Nimble execution, with obligations preserved for later settlement.

**D:** Immutable evidence may be archived off-host with schema/source/ledger hashes. Do not
transfer accounts, activate old run IDs, or rewrite their authority on OVH.

Reports: four reports/run, latest day October 13. Four delivery attempts and four sent
records, last at October 14 00:14. No outstanding attempted-without-sent delivery was
identified by these counts; a finalizer must bind exact report IDs, not rely on aggregate
counts. The October 14 shutdown-day terminal report remains an explicit obligation, not
an excuse for indefinite daily reporting. Manual/copilot and unrelated research stay intact.

## Nimble daily brief recommendation

Remove `paper_nimble_brief` at **00:14** from the research schedule. Current approved
`prism_research_runtime_v1` already excludes it. Historical reports/deliveries remain;
prepare one final explicit report with the retirement plan. No Research/Context path in
the inspected code consumes recurring Nimble reports. No job was disabled during this audit.

## Schema 31 rehearsal

Production source verified schema **30**; local `MIGRATIONS` target **31**. Exact current
backup frontier above. `scripts/rehearse_research_schema.py` migrated a disposable local
copy using `Store.migrate` and checkpointed it, preserving the authority metadata. `scratch`
was used only for disposable local rehearsal files, never dashboard or authoritative DB.

**PASS:** migration/open/checkpoint/close **49.823 s** on the local machine; initial/final
file **1,646,276,608 bytes**. 40,990 disk samples at a requested 1 ms interval; sampled
peak increment **8,192 bytes**, sampled WAL peak **4,096**, external temp **0**. Sampling
and local-host timing do not establish an enforced production maximum. Existing schema-30
tables: **142**; every old table except schema_version retained identical count/value hash.
Authority matched. Four new tables empty: universe_policies, universe_evaluations,
microstructure_subscription_events, universe_outcomes. New tables do not activate workers.

Separate rollback restoration reproduced original schema-30 SHA, all table fingerprints
and authority. Both source compressed artifact and decompressed source remained unchanged.
Profile/backup suite **85 passed**, capture safety suite **5 passed**, Ruff clean for added
scripts/tests. DB queries checked schema, authority, all-table reads/value fingerprints,
governed checkpoints, manual/copilot/context tables, legacy obligations and new-table emptiness.
This is local data/restore proof, not a successful production rollback or a cutover approval.

## Research-profile diff

Approved identity **`prism_research_runtime_v1`**, definition SHA-256
**`8d6431ba5d6088b90df3e94a2edf3b9415f03a01c8d6f79416921eb5d141e6d9`**.
Proposed variables: `PRISM_PROFILE=research`, `PRISM_AUTHORITY=full`,
`PRISM_DEPLOYMENT_TARGET=railway`, existing role/id authoritative/railway-prism-runtime,
retain `PRISM_NIMBLE_RUNTIME=off`. Build from one reviewed committed revision, never the
mixed working tree. Variables/deployment have not been changed.

**Current actual → proposed actual:** persistent processes unchanged: microstructure_collector,
context_gateway, context_ingest, free_source_collector, scheduler. No new process starts.
Only cron deletion: `14 0 * * * market ops cycle paper_nimble_brief --trigger schedule --wait 3600`.
Current **33 entries → 32**, active job types **13 → 12**. Conditional Paper v2 drains remain
in the definition but are already absent from the actual crontab because v2 is retired.
Prospective v1 paper/brief and intraday shadow steps remain filtered out because v1 is retired.

Retained jobs: prospective (forward/copilot/incubation and conditional v1 drains), daily,
microdir_daily, microdir_weekly, oi, intraday, paper_v2_funding, positioning, context_macro,
context_news, microstructure, backup; gateway/source minute health checks. Shared Intelligence,
Work ingestion and Phase 24B use their original Railway authority and definitions.
Profile fences additionally refuse Nimble create/register/evaluate, Nimble worker, new
autotrader account authority, DU radar/rich warming and other execution workloads.
Dynamic Universe tiers remain absent. OVH profile/rehearsal is a separate experiment.

Exact current/proposed schedule files and full allowlist diff are in the evidence directory.

## Production cutover readiness

**BLOCKED — MORE HEADROOM/WORK REQUIRED**

Passed: current source/schema identity, verified off-host recovery, local schema-31 rehearsal,
exact-copy restore, profile definition/flags, v1/v2 no-work audit, current gateway/microstructure
health commands (both exit 0), no duplicate scheduler or worker observed.

Failed/unresolved: deployment backup guard, numeric worst-case headroom/growth bound,
functioning scheduled backups, intraday continuity, missing legacy funding, future research
horizons/terminal lifecycle and final report. The current capture must be refreshed if writes
advance before any eventual production gate. No gate is waived because migration is small.

Before a later production migration: resolve capacity and retained dependencies; refresh and
verify backup; prove the intended recovery/upload procedure and reserved disk budget; validate
current schema/revision/profile; rerun current-data rehearsal; present exact deployment and
rollback plan for approval. Production migration remains a separate explicit gate.

Post-cutover verification is **not performed because there was no cutover**. Required later:
gateway/authorized Work acceptance and durable receipt; free-source drain; scheduled forward,
copilot and research successes; Phase 24B checkpoint frontiers and core-six coverage/offsets;
backup creation/export/restore success; Shared Intelligence reads; no new Nimble/paper
opportunities/fills; correct authority identity; one scheduler/no stale replicas. A real
authorized Work submission belongs to that later gate; none was fabricated during this audit.
Monitor at least a full daily schedule including backup and science, with a longer weekly
check when required; preserve gaps/partial-minute semantics across any necessary restart.

## Expected Railway reduction

| Metric | Historical / before | Current measured | Additional cutover expectation |
|---|---|---|---|
| CPU | Prior Nimble worker ~0.45 cores; pre-shutdown service ~0.67 in earlier evidence | **0.1985 cores over 60 s**, includes transient jobs/probe; persistent ingest dominates sample | Remove one ~26.7 wall-second brief/day; CPU savings U, no new large worker saving to claim |
| Memory | Prior Nimble RSS 328 MB→2,030 MB; prior container ~3.95 GB point sample | Container 3.504→3.486 GB over 60 s; four worker RSS total ~744.7 MB; cache/job memory separate | Essentially same persistent processes; brief transient removed; sustained delta U |
| DB growth/day | Mixed October 12→14 physical window | ~301.06 MB/day mixed-period net file growth; **post-shutdown steady rate unmeasured** | No new autonomous opportunity/mark growth expected; existing research still writes |
| Raw/spool growth/day | Governed acquisition retained | Provider ~16.23 MB/day; micro raw ~10.72; spool ~15.58, mixed physical interval | Collection largely unchanged; no justified coverage-reducing saving |
| Application process count | Five persistent Python workers before Nimble off, plus scheduler | **Four Python workers + one scheduler** | Unchanged; zero additional persistent process removals |
| Scheduled entries/job types | Current legacy after Nimble off | **33 / 13** | **32 / 12**; only daily brief disappears |
| Volume | Initial 3,261,358,080 used / 1,560,363,008 free | Final 3,261,894,656 used / 1,559,826,432 free | **0 bytes reclaimed**; profile itself does not shrink existing evidence |

No resource reduction was newly applied in this phase, so there is **no measured production
before/after profile delta**. The earlier worker shutdown is measured prior work. Short
CPU/RSS/cgroup points, mixed-period growth and wall duration are not Railway billing.
No billed saving is claimed. The user's £12.72 OVH lab remains responsible for its hungry
autotrader; Railway keeps durable research responsibilities and still needs recovery capacity.

## Safety confirmation

No persistent evidence deleted; recovery copies verified and retained. No autonomous trading
restarted, production retirement/finalization executed, old accounts migrated, OVH SSH/config
change, DU/baseline/sizing activation, authority weakening or production schema migration.
Production remained schema 30; local migration copies are isolated, disposable files.

Known dashboard issue: local dashboard attempts a write to the Railway-owned authoritative
DB and is correctly rejected. No authority bypass or dashboard scratch setting. Future
Dashboard / Read Architecture should use verified snapshots or another governed read-consumer
interface; dashboard repair is not a prerequisite for this cost phase.
