# Production storage recovery — 12 October 2026

The backup-copy problem is real, but eliminating it does not by itself prove a 30-day storage budget. Dynamic Universe activation remains disabled. All trading, hypothesis, causal, evidence and microstructure history semantics are preserved. No CPU architecture work or Tranche B changes are included.

## Baseline and production revision

The supplied Dynamic Universe report measured 947,965,952 free bytes and a 702,558,208-byte DB. This task's fresh read-only inventory measured **720,523,264 free bytes**, **921,448,448 live DB bytes**, and a 4,838,498,304-byte filesystem. Railway's nominal plan is 5,000 MB; filesystem usable capacity is smaller. The current production deployment and local `main` are `f30040c06f533d3d5e87efb73a9ae139f589ddad`. Runtime A and Dynamic Universe work are uncommitted in the supplied working tree; this task preserves them. No mixed working-tree deployment was performed.

The full physical inventory, including every exact pathname, byte count, allocated blocks, inode, hard-link count, timestamp and backup SHA-256, is in `docs/evidence/storage-recovery/inventory-before.json`. Size summaries and final measurement follow below.

## Why eight backups existed

`src/market_signal/ops/runtime.py` used `BACKUP_KEEP = {daily: 3, weekly: 2, manual: 3}`: eight **names at maximum**, not eight independent recovery layers. The scheduled backup runs at **01:30 UTC daily**. `backup_live` takes the runtime lock, checkpoints, holds a read-only connection excluding DuckDB writers, copies the full DB through `.part`, atomically renames it, then verifies schema, authority and a continuity fingerprint, including research evidence digests and paper ledger metadata. It hashes the entire file with SHA-256. No compression is used. Only the live DB file is copied: backups, raw archives and spool files are **not recursively included**.

Daily backups also generated a weekly hard link if the newest weekly copy was older than 6.5 days. The link initially shares blocks with a daily file; once the daily link rotates away, the weekly link retains the full allocation. Manual/deployment backups are independent full copies. `deploy/railway/deploy.sh` creates a labelled manual pre-deploy backup before every deployment. Separate per-kind pruning let those categories accumulate independently.

This inventory actually found **seven full files**, all with link count one, totalling **2,806,599,680 logical bytes** and **2,806,624,256 allocated bytes**. The daily `paper-dashboard` labelled file is also a full DB copy. Its label documents the purpose, but does not identify the historical caller beyond the backup mechanism. Current dashboard retrieval (`scripts/pull_paper_snapshot.py`) selects an existing backup and exports a compact allowlisted read-only table snapshot over SSH; it creates no additional production DB/archive file. `--full-backup` downloads an existing copy locally. Dashboard refreshes do not call production backup creation. Local downloaded dashboard/inspection copies are off-volume and are not counted as Railway usage.

## Recovery requirements

| Failure | Same-volume recovery copy | Off-host/source recovery |
|---|---|---|
| Process crash | Usually not needed: DuckDB recovery/WAL and process restart handle it; helps if resulting DB damage occurs | Git restores code, not uncommitted live evidence |
| Bad deployment | Verified prior DB state plus exact git revision permits rollback where DB state changed | Off-host prior snapshot provides the same recovery state with download delay |
| Corrupted current DB | Verified independent full file helps if its blocks are intact | Off-host verified copy is required if corruption affects the volume |
| Failed migration | Pre-migration backup plus matching source revision; optional temporary second local copy improves rollback speed | Off-host pre-migration copy preserves the historical schema; restore with compatible code before upgrading |
| Accidental operator error | Prior intact snapshot helps within its recovery point | Exported historical snapshots cover older errors; source journals help reconstruct sourced data, but not every ledger/hypothesis decision |
| Railway volume failure | **None**: all local copies can disappear together | Verified local/external archive is required; git alone does not recover the DB |

Raw provider journals preserve fetched-at envelopes and payload hashes referenced by provider-run provenance. Microstructure raw journals preserve event-level replay information absent from minute aggregates. Spool preserves revisions, finalization records and ingest recovery state. These contribute to replay/revision recovery, but are not substitutes for a complete run/ledger/evidence backup. Recovery point remains the last successful exported backup; no zero-loss disaster-recovery claim is made.

## Railway retention and cadence

Default: authoritative live DB plus **one newest verified full recovery copy globally**, across all daily/manual/weekly folders. No continuing weekly copy set. One strategically named previous verified copy is supported only with an explicit recovery reason, e.g. pending migration health validation. Its benefit is faster local rollback while migration risk is unresolved, not independent disaster recovery. Release that pin after deployment succeeds, production health is validated and its replacement backup/export is verified. Historical deployment copies remain off-host.

For a major migration, create its pre-deploy backup, then run `market ops backup-pin-latest --reason 'pre-migration rollback until production health is verified'` before deploying. After production health succeeds, create a replacement full backup, export it locally, and release the pin with `market ops backup-retain --apply`. Pinning the current newest snapshot reserves up to two copies once its replacement exists; a pin is not eight independent recovery layers.

Full backup cadence remains daily at 01:30 UTC, with manual/pre-deployment backup support. The new code refuses to create a third local full copy: export/retention must be resolved first. It reports the failure rather than deleting unexported history. External archival must run after each scheduled or deployment backup. A named second backup blocks further full-copy creation until the operator finishes its rollback window and retires it. This bounded safety rule is explicit; it does not silently lower cadence.

`market ops backup-retain` defaults to a dry-run. `--apply` persists the one/two-copy policy and applies it under the runtime lock. `--keep 2 --previous PATH --reason TEXT` requires a named verified backup and recovery reason (the newest can be pinned while waiting for its replacement). Newest verification is repeated before cleanup. All deletions require checksum-matched off-host receipts; historical unmanifested copies are blocked. The routine rejects traversal, symlink paths/manifests/logs and the live DB (including a hard link to it). It only accepts `prism-TIMESTAMP[-label].duckdb` directly under approved daily/manual/weekly backup directories. There is no wildcard deletion and no automatic removal of raw/spool/evidence or `.part` files. Exact checksum/path/size deletion intent and completion are fsynced to a tiny append-only retention log.

The implementation is in `src/market_signal/ops/backups.py`; runtime/CLI integration is implemented locally. It has **not** been deployed over the existing production revision. This task uses the same module over SSH for the bounded reviewed cleanup, without replacing application code or changing environment flags. Until the reviewed integration is deployed, production retains the historical scheduling/pruning implementation; regular off-host export and explicit retention are therefore necessary to prevent recurrence. Do not treat the current cleanup as permanent unattended remediation.

## Local/off-host archive workflow

Run on the user's local machine, optionally targeting mounted external storage:

```sh
uv run python scripts/backup_archive.py /path/to/external/prism-backups
# Preserve all historical states, then review eligible cleanup:
uv run python scripts/backup_archive.py /path/to/external/prism-backups --all --retain
# After reviewing the dry-run, repeat with application enabled:
uv run python scripts/backup_archive.py /path/to/external/prism-backups --all --apply-retention
```

`deploy/railway/pull_backup.sh DESTINATION` wraps latest export. There is no directory hunting. The helper inventories approved paths, selects by timestamp, verifies the existing immutable Railway backup, streams losslessly compressed bytes over read-only SSH without writing an intermediate Railway artifact, decompresses locally, checks logical SHA-256, scans every stored table, commits the archive atomically and emits a manifest. Only then does it acknowledge off-host preservation on Railway. Existing archives are checksum-validated for safe retries. Failed transfer/checksum/restore never emits a valid deletion receipt. A valid receipt is an operator assertion of completed off-host verification, not an independent server proof that external storage will remain available forever.

Each small manifest records source revision (captured from the backup creator REVISION for newly created copies; historical snapshots use the latest recorded runtime event, audited against cycle metadata; deployment label is the intended target revision), historical schema version, filename-derived backup UTC timestamp, original DB size/SHA-256, paper run/policy IDs, all-table read result, origin environment, verification timestamp and download/export timestamp. Compressed artifact size/hash and decompression/restore results are separate. Historical schemas are read without migrating the evidence. Unknown future schemas and missing run/ledger metadata are rejected.

Production exports are stored locally at `data/backup-archive/storage-recovery-20261012/`, excluded from git. Full checksums and restore results are in their manifests. Off-host backup means this local host survives a Railway volume failure; copying this archive to separate external storage protects against local-host failure too. No cloud service or paid storage was purchased.

## Raw, spool, evidence and logs

No raw, spool, journals, ledgers, microstructure rows, research history or evidence exports were removed. Provider journals under `/data/raw` use gzip envelopes and preserve fetch timing, source payload hashes and run-linked file references. No general provider-journal expiry was introduced.

Microstructure raw journals are concatenated gzip members, one file per hour; the existing collector defaults to one day of raw retention. Spool is uncompressed JSONL with eight-day retention, required for large-print calibration. Its existing pruning requires the committed ingestion watermark to be past the whole day, and ingestion records offsets transactionally with authoritative rows/revisions. LP day caches have ten-day retention. Calendar day boundaries make these upper bounds differ from exactly 24/192 hours. Raw capture already pauses below 15% free volume; this rule was not changed. Collector retention and pause behaviour are existing baseline semantics, not new cleanup in this task.

An ingested minute does not prove its event journal redundant. Revisions, source timestamps and replay equivalence must be established independently. No additional aggressive raw/spool expiry or content deduplication is authorised by this audit. Identical payload bodies with different capture envelopes are distinct provenance observations. No equivalence/replay proof supports new deletion. Context/free-source `done`, `receipts`, seals and journals remain; they are source evidence, not disposable temporary logs.

Runtime logs already rotate at 5 MiB with five archived logs; observed files remain untouched. The inventory records lock files, source temporary files and evidence reports separately. No broad temporary-file cleanup was performed.

## Compression and restore validation

All seven original backups were exported and decompression/read tested, including schemas 17, 25, 26 and 30. Every stored table was scanned using a hash aggregate, forcing value reads rather than a metadata-only count. Paper runs and ledgers, context/event tables where present, research/evidence tables and any dynamic-universe tables present are included. Historical backups that predate a feature correctly lack that feature; they are not represented as current-schema recovery copies.

The original seven files total 2,806,599,680 bytes; compressed local artifacts total **355,581,051 bytes** (87.3% smaller). Compression happens only after source verification and is reversible; decompressed hashes match every original. The active DB is never compressed in place. Initial old-schema restore records may contain additional context-provider run IDs; subsequent manifests restrict IDs to paper runs to keep metadata small.

## Cleanup, before/after, runway and activation

See the exact machine-readable `cleanup-initial.json`, `retention-dry-run.json`, final inventory and runway evidence in `docs/evidence/storage-recovery/`. The initial reviewed cleanup removed six exported files, reclaiming **2,182,422,528 logical bytes**. The seventh (newest) was protected. A fresh current-schema recovery copy is then created, verified/exported locally, and replaces the previous recovery copy under the same policy.

The runway calculation must include baseline live DB growth, dynamic growth, one retained full copy, current raw/spool usage, the supplied 429,967,898.8-byte dynamic reserve, and transient space for the next full backup. The existing backup guard requires `1.2 × live DB size + 5% filesystem capacity` free before creating another full copy. As the live DB grows this reserve also grows. A raw/spool reserve is not equivalent to claiming all future provider journals have bounded size. Expected and conservative scenarios are sensitivity models, not proof of stationary future growth.

**Gate: BLOCKED.** No ranking/warming or dynamic satellite execution is enabled. The 30-day target cannot be asserted from duplicate-copy savings while the baseline DB is growing rapidly. New paid storage is not purchased or configured. A requirement after sane retention is reported by the final measured model; other physical-duplication work must independently demonstrate equivalence before being proposed as a substitute.

## Validation

The focused backup/runtime/dashboard compatibility suite passed 45 tests. Additional coverage includes persisted optional rollback policy and refusal to create a third full copy. Full repository regression results and final exact storage/runway numbers are recorded when verification completes. No production trading/research code, policies, causal rules, feature history or execution configuration was modified by this task.

## Exact storage map and results

| Category | Before logical bytes | After logical bytes | After allocated bytes |
|---|---:|---:|---:|
| authoritative live DB | 921,448,448 | 901,525,504 | 901,529,600 |
| backup manifests / retention log | 0 | 199,597 | 221,184 |
| evidence exports / baselines | 1,089,876 | 1,089,876 | 1,110,016 |
| full recovery backups | 2,806,599,680 | 906,768,384 | 906,772,480 |
| logs | 6,915,935 | 6,929,009 | 6,942,720 |
| microstructure raw | 52,042,584 | 52,536,100 | 52,707,328 |
| microstructure spool | 67,888,981 | 68,181,365 | 68,870,144 |
| provider raw archives | 219,604,692 | 219,627,192 | 241,573,888 |
| source journals / receipts / state | 171,479 | 171,494 | 405,504 |
| state / metadata / locks | 86,484 | 87,220 | 118,784 |
| temporary files | 63 | 63 | 4,096 |
| Dashboard/read-only copies on Railway, separate from classified backups | 0 | 0 | 0 |

Physical available space: **720,523,264 → 2,639,220,736 bytes**, a measured **1,918,697,472-byte increase**. Net backup reduction is **1,899,831,296 logical bytes**. All seven old files were removed only after preservation. A 906,768,384-byte fresh copy was created; its file is `/data/backups/manual/prism-20261012T083738Z-storage-recovery-20261012.duckdb`, SHA-256 `08bf19fb13e020f3db2cc314cfc7616453d7b29b7bcca6204351bd9212608e92`. The small concurrent DB/raw/spool/log changes reflect normal runtime/checkpoint activity; the cleanup removed only the enumerated backups. The live file was not truncated, compressed or pruned.

| Original file (all now archived locally) | DB bytes | SHA-256 |
|---|---:|---|
| `/data/backups/daily/prism-20261009T131120Z-paper-dashboard.duckdb` | 250,884,096 | `ae958ab773f419e5328414d117e33e505fd50e46da55436b1abcdca3b906ab60` |
| `/data/backups/daily/prism-20261010T013003Z.duckdb` | 306,982,912 | `4be8a0fb483a17d5c0c10aa5c4d6253cdce3429962d1ebb54fd751d54e8a907b` |
| `/data/backups/daily/prism-20261011T013019Z.duckdb` | 500,969,472 | `589dcf12d416cf30765755b364712e22e77ad789f93d1c7d28da73ad598b2217` |
| `/data/backups/manual/prism-20261010T194711Z-pre-paper-v2-retirement.duckdb` | 493,105,152 | `19c79b4c8f2bb35e2f2e55885701c0c7f31db7fa174dc2ec05ced0bd12f69de3` |
| `/data/backups/manual/prism-20261010T195836Z-pre-deploy-a6b21bfc2023.duckdb` | 493,105,152 | `f6a4dd6a4656a14d42c3da99e3e54ba4841c401d03d069985758e38731ae0fd3` |
| `/data/backups/manual/prism-20261011T121122Z-pre-deploy-f30040c06f53.duckdb` | 624,177,152 | `f5117485f3a2f9de86f3d2c907fb1b7b60ebc9cce4066b2b432dd5a9068609cc` |
| `/data/backups/weekly/prism-20261004T174911Z.duckdb` | 137,375,744 | `23323257f953c13e3abafca0518b3d59b65917e5bd70a04858d0577db89d7a27` |

Every original file maps to a local archive of the same basename plus `.gz` in the documented archive directory. Including the fresh backup, **eight** off-host archives were individually decompressed, schema-table validated and every stored table read, with both compressed and logical checksums validated. The fresh backup is schema **30**, source `f30040c06f533d3d5e87efb73a9ae139f589ddad`, with **142** readable tables and five paper run IDs across Paper v1, frozen Paper v2 and Nimble cohorts. Its compressed artifact is **81,357,799 bytes**; artifact SHA-256 `0e8b96f9c5e5e274e12ee782e387a1371bb3855be8e4a1f6ec0ebf75d4b6f7c9`. Total off-host compressed artifacts are **436,938,850 bytes**. Current production backups predate the new `universe_*` tables; those are absent, not invented. Local tests explicitly validate their schema when present and reject a missing required universe table.

Raw/spool audit: **7,697** files read with gzip decompression where appropriate, **zero duplicate logical files**, **zero read errors** and **zero files changing during the audit**. Committed watermark: `20261012/07.jsonl`. This does not prove replay equivalence to DB aggregates or authorise additional deletion.

Current backup headroom: **1,323,755,520 bytes** (`1.2 × DB + 5% filesystem`). Additional dynamic raw/spool reserve: **308,390,427 bytes** above the existing 121,577,472 allocated bytes. Additional log reserve: **24,514,560 bytes**. After these reserves the comfortable growth budget is **982,560,229 bytes**.

| Model | Growth GB/30d (decimal) | Comfortable days / months | Exhaustion days | Usable capacity for comfortable 30d |
|---|---:|---:|---:|---:|
| expected measured-growth sensitivity | 13.377 | 1.40 / 0.047 | 5.17 | 25.997 GB |
| conservative recent-growth sensitivity | 22.748 | 0.83 / 0.028 | 3.04 | 41.085 GB |
| conservative plus stalled spool retention | 26.018 | 0.76 / 0.025 | 2.66 | 44.527 GB |
| optimistic: zero baseline DB growth | 1.738 | 11.98 / 0.399 | 39.80 | 6.394 GB |
| dynamic-only: zero baseline and provider archive growth | 1.204 | 15.31 / 0.510 | 57.48 | 5.831 GB |

The expected sensitivity uses the supplied recent daily DB delta of **193,986,560 bytes/day** and **17,818,965 allocated provider-journal bytes/day** averaged over three complete days. Conservative sensitivity uses **331,845,744 DB bytes/day** between the two newest verified backup snapshots (73,576 seconds apart), and **54,460,416 provider-journal bytes/day**, the largest complete-day capture allocation in the recent week. These rates span deployment/retirement/checkpoint effects and cannot establish a stationary forecast. The stalled-retention row adds the supplied 108,992,749-byte/day raw/spool sensitivity.

The reported dynamic-only permanent growth drops from **5,416,507,150 bytes/30d** with live DB plus eight copies to **1,203,668,256 bytes/30d** with live DB plus one copy. That saving does not eliminate baseline growth or the need to construct the next full copy. Even the optimistic zero-baseline case misses the 30-day comfortable-headroom target. **BLOCKED** remains the sole activation state.

No paid capacity was added. The current volume is insufficient for the requested gate under these models. If the recent rates persisted, the 30-day requirement would be approximately **26 GB expected / 41 GB conservative usable filesystem capacity**, and approximately **44.5 GB** with stalled spool retention. These are capacity sensitivities, not a recommendation to purchase an unproven larger plan. Even assuming zero baseline DB growth requires approximately **6.4 GB usable capacity** with observed provider journals. Establish a stable post-retirement baseline and separately audit any additional provable physical duplication before deciding on a plan.

## Journal durability and completeness

The verified fresh DB snapshot records **5,958 successful** and **7 failed** provider ingestion runs. Of **7,554** provider raw files on the final volume inventory, **5,817** match stored run references after normalizing the historical raw-directory prefix; **1,737** do not match those snapshot references. This is not a deletion criterion: later capture, historical ingestion paths and failed/source evidence must be reconciled individually. The DB contains **5,951** referenced raw paths absent from this volume inventory; this existing historical/import coverage gap is recorded explicitly rather than claiming source journals alone provide complete recovery. Exact paths and run/offset metadata are in `raw-spool-provenance.json`. None were removed by this task.

Of **111** retained microstructure spool files, **110** have byte sizes fully covered by committed offsets in the earlier fresh DB backup; one current file has later bytes conservatively treated as uncommitted here. The snapshot contains **39,804** authoritative microstructure minute rows and **3** preserved revision rows. Offset commit is necessary for safe spool expiry, but does not prove that deletion preserves calibration, replay and revision recovery. All retained spool/raw files remain.

Off-host DB archives cover the entire DB at their recovery points, including embedded research snapshots and ledgers. They do **not** include external raw/spool directories. Full event-journal recovery after volume loss additionally needs a separate off-host journal archive: preserve immutable completed hour files and run-linked provider envelopes, verify original/decompressed hashes locally, and record the associated DB checksum and committed offsets. Copy active append files only once stable or from an explicitly bounded capture, and preserve their unread tail until it is durably ingested. Such a journal archive must acknowledge both source provenance and replay validation before any newly proposed expiry. This task did not deploy journal archival or shorten the existing collector retention.

All eight source revisions recorded in the DB archive manifests are available as local git commits (`source-revisions.json`). Git recovers code; the verified DB archives recover evidence/state. Raw replay completeness is bounded by the available journals, not inferred from either git or backup count.

## Final regression evidence

**1,449 unique regression cases have passing results.** The first restricted run completed 1,248 cases; two websocket cases failed at local socket binding and threaded HTTP test clients stalled. Completed results were preserved, and 209 remaining/targeted cases were run outside the restrictive sandbox: 208 passed and one exposed the old intraday `.values()` retention assumption. That storage-preflight consumer was updated to the one/two-copy policy and projected full-backup headroom, then its entire 30-test module passed, including two new capacity-policy tests. The focused backup/runtime/dashboard suite passed 50 cases; 13 lifecycle hardening cases and two creator-provenance cases also passed. No unresolved failures remain. Exact union of passing case IDs was checked against the complete collected set; see `tests.json` and compressed regression logs. Ruff, syntax compilation and shell syntax checks passed for the changed implementation.

The snapshot layout audit reports **1,100 internal free blocks** at **262,144 bytes/block** (`database-layout.json`). These are reusable DB storage, not OS volume free space; physical file-size deltas therefore cannot establish a stationary growth forecast. No active DB rewrite or compaction was performed. The measured runway rows remain explicit sensitivities, not proof that a 26–41 GB purchase is genuinely necessary. A stable post-retirement baseline and any further lossless physical-storage change need separate validation before a paid capacity decision.

The final intraday capacity guard reads the same persisted one/two-copy policy, accounts for growth across live/retained copies, and reserves the larger of the warning floor and projected next-full-backup headroom. Its complete 30-test module passed. Including the two additional capacity tests, **1,449 unique collected regression cases** have passing results. The final operator helper was exercised against the retained Railway snapshot using its already verified local archive and a deletion dry-run.
