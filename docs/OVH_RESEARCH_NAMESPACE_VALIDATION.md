# OVH Research namespace validation

2026-10-15. **No authority transfer.** Railway remains authoritative for Research,
Context, Intelligence and governed science. OVH Lab rehearsal remains unchanged.

**Verdict: BLOCKED — PARITY/BACKUP/CAPACITY ISSUE.** The specific blocker is independent
OVH off-host backup: no approved destination, repository credentials or recovery custody
is configured. Local import, application parity, clock isolation, schema rehearsal and
local backup/restore passed. A frozen prospective-cohort source-hash guard also remains
failing on clean starting main; its manifest was preserved. This is not approval to start Research writers.

Evidence: [20261015](evidence/ovh-research-namespace/20261015/), particularly
[source validation](evidence/ovh-research-namespace/20261015/source-validation.json),
[candidate validation](evidence/ovh-research-namespace/20261015/candidate-validation.json.gz),
[restore validation](evidence/ovh-research-namespace/20261015/restore-validation.json.gz),
[clock comparison](evidence/ovh-research-namespace/20261015/clock-comparison.json), and
[backup proof](evidence/ovh-research-namespace/20261015/backup-proof.json).

## Research namespace

| Item | Installed state |
|---|---|
| Host | `vps-778d603b`, 178.33.95.139 |
| OS identity | `prism-research`, separate primary group, no supplementary Lab group, nologin |
| Future production identity | `vps-prism-research`; **not claimed/configured as active** |
| Offline consumer identity | `vps-prism-research-offline`; receipt identity only |
| Profile | `research_vps` / `prism_research_vps_runtime_v1` |
| Profile digest | `46e81ec9a60ab07acaecd55675e477c65aa976ad978890e0c197cd8744f8be70` |
| Authority / role / runtime ID | `none` / `reader` / empty |
| Processes / scheduled jobs | Both empty; collector, free-source and gateway flags explicitly off |
| Execution / external actions | Both false |
| Release | `/opt/prism/research/releases/c5b81f302f69504b08a4022a9f6515c733dae197-7190e0cb53c2` |
| Research symlink | `/opt/prism/research/current`; Lab's `/opt/prism/current` unchanged |
| DB | `/var/lib/prism-research/import/prism.duckdb`, root:prism-research, 0440 |
| DB parent | Root-owned, group readable/traversable, no Research directory write permission |
| Config | `/etc/prism-research/runtime.env`, root:prism-research 0640 |
| Secrets | `/etc/prism-research/secrets.env`, empty, root:prism-research 0640 |
| Logs / backup / state / scratch | Separate directories beneath `/var/lib/prism-research/` |
| Raw / results / microstructure / Context / free-source paths | Separate, initially inactive directories beneath that same Research root |

`/data` and `/etc/prism` are private Lab parent directories. Research uses separate roots
rather than widening those permissions. An initial config traversal failure was resolved
by relocating only the newly created Research config. No host reboot, package upgrade,
firewall change, Lab unit reload/restart, or Lab permissions change was needed.

Upstream `main` was checked as `020896fd…`. The existing three prepared local main
commits through `c5b81f3` supply the reviewed namespace/recovery conventions. The installed
release includes an explicitly hashed namespace tooling/config overlay, not unrelated
dirty workspace files. [Release identity](evidence/ovh-research-namespace/20261015/release-source-v2.json)
records every overlaid file. One-shot validation drivers and secure config placeholders
are separately retained in evidence/repository. No application revision was deployed to Railway.

## Source snapshot

The newest verified off-host Railway archive available on the receiver was used.
A live allowlisted status check found **2,757,902,336 bytes free**, DB **1,646,276,608 bytes**,
no remaining full backup file to export, Nimble off, and the bridge runner present.
Creating another full backup would temporarily consume roughly 1.65 GB and risk acquisition's
storage guard again. This validation therefore uses the verified recovery point already
exported; it does not claim a fresh final handoff frontier.

| Identity | Value |
|---|---|
| Source runtime / authority | `railway-prism-runtime`, production; claim since 2026-10-04 17:46:19.092023 UTC |
| Source software | `8f9bc3473142eb6b8a5ea54a3987db8ef2bcb0cf` |
| Railway deployment | `3ef1c302-90c4-48bb-a626-e1ea434d22b4` |
| Backup | `prism-20261015T113557Z.duckdb` |
| Schema / engine | 30 / DuckDB 1.5.5 |
| Relations | 142: 141 physical tables plus one view |
| Logical size | 1,646,276,608 bytes |
| Logical SHA-256 | `1caa6c9677e1f87e4f04fc087a2534b3d8a4eac89d1e93d7fe0fc025213333c6` |
| Compressed size | 135,829,646 bytes |
| Compressed SHA-256 | `3e8ba0ffab4d5a93cc3b9cbd0632ca891887207e0a0656d69945fca86b2affa1` |
| Last finished runtime cycle | 2026-10-15 11:31:40.099798 UTC |
| Source verification upper bound | 2026-10-15 11:36:50.031621 UTC |
| OVH archive verification | 2026-10-15 11:40:42 UTC |
| Method | Existing normal locked/checkpointed backup → gzip → forced-command isolated receiver → hash/size and every-table restore proof |

The physical snapshot includes an in-progress backup cycle. Last finished cycle is a useful
read cutoff, not a claim that every captured job finished. Original manifest/run/policy IDs,
source revision, authority claim and all table fingerprints are preserved. The original
archive remains at `/var/lib/prism-railway-archive/archive/`. It protects Railway from
Railway loss; it does **not** protect OVH from loss of this VPS.

## Import result and schema compatibility

Original compressed/logical checksums, length and every source relation matched the
receiver receipt. Source materialization took **35.39 s**. The schema-30 source remained
sealed while a separate copy rehearsed the approved-main schema-31 migration.

The migration/checkpoint itself took **19.80 s**; the complete migration, fingerprint and
rollback exercise took **116.62 s**, **112.86 CPU s**, peak RSS **2,242,363,392 bytes**.
All existing DDL remained identical. Four empty relations were added:
`universe_policies`, `universe_evaluations`, `universe_outcomes`,
`microstructure_subscription_events`. The only existing table fingerprint change is the
new `schema_version` row. Those five differences are explicitly classified
`EXPECTED_PATH/ENVIRONMENT_DIFFERENCE`, with the additive schema change stated as the reason.
All other relation counts/value fingerprints remained equal.

A fresh rollback copy matched the original schema-30 SHA and all fingerprints. Rollback
requires no production action: discard the candidate and re-materialize the retained sealed
source under the stopped, non-authoritative namespace. No rollback touches Railway or Lab.

The resulting schema-31 candidate is **1,646,276,608 bytes**, SHA-256
`dbaa5a3c31ebd738cca69bbd714263f4583722331f515ee2d239d4be77d9726f`.
It retains Railway's claim as historical provenance; a sidecar labels it non-authoritative.
New empty universe tables confer no Lab or universe authority.

## Ownership-manifest validation

All **142 prepared source relations** are present, with no unexpected source relation.
The prepared manifest's frontier was **01:21:39.668259 UTC**. Its 37 changed relation
counts/fingerprints are recorded as `SOURCE_FRONTIER_DIFFERENCE` against the later sealed
source; old counts were not treated as same-frontier parity expectations. The original
retention dispositions remain attached to each refreshed row. Candidate classification
covers all **146 relations**, including the four empty schema additions.

Every relation has an owner class and `import_role=read-only imported history` with
`may_resume_authority=false`. The classes distinguish Research, Context, shared factual
history, legacy execution evidence, Lab-only universe state, and scheduled/runtime state.
Runtime claims/cycles, microstructure cutover/offsets, pending work/trigger state and
notification/delivery state cannot resume authority merely by existing in the copy.

Four ambiguous future ownership boundaries are explicit: **`alert_events`, `alert_rules`,
`journal_entries`, `positions`**. They retain their data and require a separate decision
before any writer/delivery/portfolio ownership. The name `lab_*` denotes existing Research
Lab science, not the OVH Autotrader Lab OS namespace. No tables were destructively omitted.
External raw files/spools are not covered by this DB-only transfer.

## Context parity

All six representative frozen Context snapshots match exactly: BTC and AAVE at the
source cutoff and one/seven days earlier. Event counts, update-chain queries, source and
asset-link values/IDs are unchanged: **102 events, 67 updates, 833 links, 15 sources**.
Known-at reads use the same cutoff and original observation/mapping/provenance fields.
The frozen Context reader code is byte-identical between deployed Railway revision and
approved main, as are positioning, forward-report and Copilot reader files.

## Shared Intelligence parity

Six canonical origin-observed packages match in full value hashes and package IDs;
all embedded evidence/content/provenance references validate and representative evidence
resolution succeeds. The canonical comparison explicitly uses the same source/consumer
identity on both sides. Actual OVH consumer reads use their separate identity/catalog.

Phase A is absent from deployed Railway code. These are the same approved-main Phase A
reader evaluated on the sealed Railway source and the OVH candidate, not a claim that
Railway already emits these packages. No online producer/transport was activated.

## Positioning parity

OI, funding, crowding inputs, daily/weekly persistence and coverage/missingness summaries
match, including venue separation and unknown states. Full source fingerprints include
**5,976 OI-history rows, 936 snapshots, 1,110 hourly HL OI rows, 21,474 ratio rows and
185,163 funding rows**. The OI union view has **6,912 rows**. Grouped coverage/missingness
queries and the positioning block hashes in all six packages match.

This proves preservation of existing coverage, not repair of Railway acquisition gaps.
No new observations, backfills, synthetic zeros or retrospectively eligible samples were added.

## Research, macro/data, Phase 24B and Copilot parity

All **30 representative reads** match source→candidate→restore. In addition to Context
and Shared Intelligence, they include three actual forward summaries and governed Copilot
watch reads, registration/dataset/experiment/scientific-result/lifecycle reports, PIT/vintage
and intraday coverage/source-health queries, and Phase 24B historical definitions/outcomes.

- Research: **40 strategies, 2 datasets, 47 experiments, 3 forward trackings,
  132 forward evaluations, 204 forward outcomes**; stored scientific verdicts and incubation
  lifecycle evidence retained with original IDs and software definitions.
- Macro/data: **1,093,572 macro observations, 868,922 intraday bars, 9,026 ingestion runs**;
  PIT filters, vintage identities, funding/OI and source-health evidence match. Production
  intraday revision history is empty; synthetic causal-revision tests cover that path.
- Phase 24B: **1 study, 8 checkpoints, 8 results**; original definitions, dataset/frontier
  identities, sequence/as-of and outcomes match. No checkpoint was advanced.
- Copilot: **2 watches, 1 decision**, existing status/policy/evidence inputs preserved;
  no notification was generated or delivered.
- Microstructure: **66,708 minutes, 31 revisions**, thresholds and offset/cutover evidence
  match. Original raw journals, calibration assets and collector/ingest handoff remain a
  future migration gate; this tranche moves no collector authority.
- Legacy Nimble: **3 runs, 198 position records, 216 pending research records** and all
  original evidence retained as immutable, nonexecuting history. No run/account was activated
  or retired by this task.

## Clock semantics

Actual candidate receipt: **2026-10-15 12:31:22.492499 UTC**. Availability after atomic
publication/fsync: **12:31:22.493899 UTC**. Historical origin fields remain unchanged.
Availability was not set to Railway ingestion or historical publication time.

| Access basis | Actual proof at the common historical cutoff |
|---|---|
| Origin-observed | BTC 9,027 / AAVE 9,025 evidence records; IDs and record bodies preserved |
| Origin-committed | Zero at 11:31:40 under the conservative sealed-source commit upper bound; 9,027 / 9,025 at 11:36:50.031621 |
| Consumer-available | **Zero** at 12:31:22.492498, immediately before receipt; **9,015 / 9,013** after apply; wrong-consumer reads empty |

The later consumer cutoff advances selection windows. Every one of the 12 removed records
per asset is accounted for: one asset bar leaves the 40-day window, five cohort bars leave
the 30-hour window, four ratios and one OI sample leave the 30-day window, and one funding
settlement leaves the 120-day window. Exact before/after timestamp bounds are asserted;
there are no new records in the comparison. These are
`EXPECTED_RUNTIME_CLOCK_DIFFERENCE`, not unexplained parity losses.

Catalog entries are content-bound by evidence IDs, catalog SHA and batch SHA, and retain
the origin identity plus actual local receipt/apply clocks. Original source receipts stay
unchanged. The older source does not retain Phase A post-commit receipts, so exact historical
commit instants are **unknown**: the source backup's verified seal provides a conservative
upper bound, not an invented original commit time. Catalogs cover the exercised evidence;
unclocked inputs remain withheld. This does not claim a complete historical receipt catalog
or deployed replication. Focused tests also cover future rows, revised facts, missing commit
clocks, contiguous revision/update dependencies, and wrong-consumer withholding.

## Read-only operation and authority isolation

Every scientific validation opens DuckDB read-only, without Store construction/migrations,
scheduler/ingestion startup, checkpoint advancement, notification or external action. The
restored workload ran through a guard that loads the installed disabled Research environment.
One-shot systemd services had private networking, no access to `/data`, one CPU quota,
3 GiB memory maximum, low CPU/I/O priority and a separate UID; all are transient.

Actual OS checks: Research cannot read/write Lab's DB or replace its parent contents;
Lab cannot read/write Research's DB or replace it. Research can only read its candidate.
A writable Store open and an INSERT over the read-only connection both failed; candidate
SHA remained unchanged. A scheduler condition and direct runner attempt both refused with
exit 1 before any scheduler/state creation. Profile output shows no processes/jobs and
external actions/execution false. Neither namespace inherits Railway's production identity.

## Backup architecture and restore proof

Current local flow: **sealed read-only candidate → one compressed generation and manifest →
verified scratch restore → atomic latest pointer → remove previous local generation**.
Full temporary restore files are staging only. WAL-bearing sources, checksum changes,
corruption, symlinks/traversal, existing restore destinations, interrupted staging, missing
source identity and concurrent backup attempts fail closed. The prior published generation
survives a failed backup. This tool explicitly does not checkpoint or snapshot a live writer:
a future authoritative runtime must first use the existing owner lock/checkpoint backup
protocol to produce its sealed input.

The local manifest contains schema/engine identity, all relation fingerprints, original
frontier/runtime/software metadata, target code/overlay identity, authority mode and actual
OVH receipt/apply bounds. Compressed SHA:
`023c3240e029eb87686905a1a9dfe3475397ba173ac0d7f9a38b1473c3e7892b`;
compressed/uncompressed sizes **135,614,775 / 1,646,276,608 bytes**.

Backup creation **82.79 s**; internal restore **45.93 s**; separate fresh scratch restore
**40.16 s**. Total backup/two-restore exercise **169.90 s**, **164.88 CPU s**, peak RSS
**1,996,132,352 bytes**. The fresh restore has identical schema, 146 relations, every table
fingerprint, authority metadata and DB checksum. All 30 read results and the full clock
suite then matched the candidate with **zero differences**. No production writer lock was
held; this cannot establish a future live-writer lock duration.

Independent off-host workflow is implemented using encrypted restic backup, exact snapshot
ID, full repository data check, recovery to a new scratch path, compressed/logical checksum
and every-table restore verification, then retention scoped to a dedicated repository and
Research host/tag: **7 daily, 4 weekly, 3 monthly**. No local copy is deleted by off-host
export. Credentials are inherited from protected config; provider diagnostics are not
printed. Repository initialization is never automatic. The command/retention/failure paths
are tested with a fake provider; they are **not an off-host disaster-recovery acceptance test**.
See the upstream [backup](https://restic.readthedocs.io/en/stable/040_backup.html),
[restore](https://restic.readthedocs.io/en/stable/050_restore.html) and
[retention](https://restic.readthedocs.io/en/stable/060_forget.html) contracts used by the adapter.

**Off-host status: UNCONFIGURED / NOT PROVED.** No repository/credentials were selected,
restic was not installed globally, no off-host upload/prune occurred, and no provider-specific
recovery test passed. `/etc/prism-research/backup.{env,json}` contains inactive placeholders;
`approved_independent_destination=false`. This PC, Railway and the existing same-VPS archive
are explicitly insufficient destinations. The exact remaining approval point is selection
of an independent provider/account/dedicated repository, credential provisioning and
independent recovery-password/credential custody. Then install a pinned namespace-local
restic binary, initialize only the approved repository, run upload/check/recovery/retention,
and rerun the actual restored scientific/clock suite before lifting this blocker.

Manual tooling: `scripts/research_backup.py local --db PATH --root BACKUP_ROOT --source
SOURCE_JSON`; `restore --archive ARCHIVE --manifest MANIFEST --destination NEW_SCRATCH_PATH`;
`offhost --config APPROVED_CONFIG --generation GENERATION --scratch NEW_SCRATCH_DIR`.
Recover a selected off-host snapshot with restic to a fresh directory, then use the restore
command and offline validation runner on its archive/manifest. No backup scheduler is enabled.

## Resource usage and combined OVH capacity outlook

GB here is decimal. Host: **6 vCPU, 12,256,591,872 RAM bytes, 105,467,994,112 filesystem bytes**.

| Exercise | Wall / CPU seconds | Peak RSS bytes |
|---|---:|---:|
| Sealed source fingerprints + 30 reads | 144.92 / 144.82 | 2,052,800,512 |
| Candidate reads + causal clocks | 204.77 / 202.60 | 2,085,400,576 |
| Restored reads + causal clocks | 210.15 / 210.08 | 2,091,184,128 |
| Migration/fingerprints/rollback | 116.62 / 112.86 | 2,242,363,392 |
| Backup and two scratch restores | 169.90 / 164.88 | 1,996,132,352 |

Representative observed calls: frozen Context snapshots **0.30–0.50 s**, Intelligence
packages **4.30–4.92 s**, forward reports **3.52–4.64 s**, Copilot watches **1.76–2.25 s**;
macro/Phase 24B SQL queries mostly milliseconds. These are small samples under a one-CPU
cap, with page cache and verification overhead; they are not production latency percentiles.

Migration sampled Research logical peak **4,938,829,824 bytes**; backup/restore peak
**5,074,469,224 logical / 5,074,489,344 allocated bytes**, including source/candidate/scratch
copies. Temporary staging peaked **1,781,891,383 bytes**. Sampling interval 0.2 s; very short
peaks can be missed. No spill file was observed. Before cleanup the lowest sampled host
free disk was **91,960,176,640 bytes**. DB plus one local compressed generation uses about
**1.78 GB**, excluding code/cache/logs and the separate preexisting Railway archive.

During restore-read validation the host had **9,209,573,376 bytes available RAM**, no swap,
and the short vmstat sample was **83% CPU idle**, with no observed steal/I/O wait. Current
Lab DB was **128,462,848 bytes**. Pre-validation Lab service cgroups summed approximately
**0.67 GB**; all continuity checks use the actual services, not invented future workloads.

Research validation CPU/I/O is declared overlap with Lab rehearsal observation
(approximately 12:20–12:56 UTC); exclude it from any clean Lab capacity/commissioning sample.
Lab behavior/configuration and service continuity were preserved.

No obvious CPU/RAM/disk shortage was detected for the measured offline tranche alongside
current Lab rehearsal. Future Research ingestion/scheduler bursts, Dynamic Universe and
baseline paper have **not** been benchmarked together. Research's prepared aggregate slice
has a 5,500 MiB ceiling; backup verification must fit within its eventual joint budget or
run separately. The temporary bridge itself can use up to 3 GiB and must be included while
it exists. Plan nonoverlapping Research/Lab backup verification, a disk reserve and explicit
measured DU/paper peak budgets before activation. Do not add independent maximum limits
and infer guaranteed headroom from one read workload. No host upgrade is justified by this
sample alone, and no future production capacity guarantee is made.

Both disposable 1,646,276,608-byte scratch DBs were removed only after their exact hashes,
source archive, candidate, local backup and all restore/read/clock proofs were checked.
Cleanup reclaimed **3,292,561,408 allocated bytes**; one local compressed generation remains.
No source archive or scientific evidence was removed.

## Services prepared but disabled; secrets

Prepared under `/etc/prism-research/prepared-services/`: Research target/slice and templated
units for scheduler/data jobs, Context gateway, Context ingest, free-source collector and
core microstructure. They are **outside systemd's installed unit search paths**, unenabled
and inactive. There is no Research cron file, listener or persistent scheduler. The dashboard
continues as an offline read/projection consumer; an HTTP read API is not deployed or
claimed validated. No public port, DNS record or OAuth resource was changed.

`research-secrets.env.example` / installed `secrets.names` inventories Context tokens,
OAuth issuer/JWKS/principals/audience URL, conditional FRED/EIA/Tiingo/Stooq/SEC/GitHub input
credentials, Telegram and heartbeat names. Backup credentials/password file are separate.
No Lab secrets were copied, no credential values printed, no delivery/heartbeat enabled.

## Migration readiness matrix

“Local only” means restore-tested local DB recovery; **independent off-host proof blocks
all future OVH authority**, regardless of row-level parity success.

| Subsystem | Snapshot parity | Runtime prepared | Backup safe | Authority design | External dependency / blocker |
|---|---|---|---|---|---|
| Context | Yes, frozen reads/chains/mappings | Disabled gateway/ingest templates | Local only | Coordinated receipt/ingest/writer handoff | Work/Auth0/hostname, durable ingress/drain proof |
| Shared Intelligence | Yes, same-reader packages/evidence | Offline Phase A library | Local only | Origin/consumer clocks explicit | Exact old commit clocks unknown; complete transport/catalog not deployed |
| Macro/data | Yes, PIT/vintages/health/bars | Disabled Research scheduler | Local only | Owner lock + final sealed frontier | Provider credentials, acquisition-gap accounting |
| Positioning | Yes, history/coverage/persistence | Disabled data jobs | Local only | One Research scientific owner | Original missingness retained; future live collection handoff |
| Research Lab | Yes, definitions/datasets/verdicts/forward | Disabled scheduler | Local only | Preserve IDs/lifecycle; explicit claim | Manual alert/portfolio/journal ownership, external artifact closure |
| Copilot | Yes, watches/evidence | Disabled scheduler, no delivery | Local only | Preserve governance; delivery separately gated | Telegram only if later authorized |
| Phase 24B | Yes, checkpoints/results/datasets | Historical queries only | Local only | Drain/seal single owed grid at final handoff | No checkpoint authority moved |
| Microstructure | Yes, DB evidence | Disabled separate collector template | Local only | Offset/raw/calibration + single source handoff | Live journals/spools/calibration not transferred |
| Dashboard/read | DB reads yes; HTTP untested | Offline reads; API uninstalled | Local only | Immutable projections/read role | Read API/auth/hostname design |
| Backups | Local checksum/table/read/clock restore yes | Local CLI + guarded off-host adapter | **No independent off-host proof** | Dedicated encrypted repository, bounded retention | **Provider/repository/credentials/recovery custody** |
| Legacy Nimble evidence | Yes, complete immutable history | Execution/brief disabled | Local only | Preserve and separately settle obligations | Pending accounting/research retirement obligations |

## Immediate next authority-transfer tranche

**Do not transfer authority next while backup is unproved.** Complete the independent
repository acceptance and resolve the flagged ownership/ancillary-state handoff first.

The safest subsequent authority-transfer proposal is **Research scheduler/data jobs within
one coordinated Research DB writer handoff**, including the remaining writers' stop/drain
and a final sealed frontier. Context ingest writes this same scientific DB, and no durable
Railway→OVH receipt relay has been demonstrated. An isolated scheduler authority move
while old ingest continues needs such a proven relay/projection first. Otherwise propose
one controlled writer/ingest maintenance window, with Context gateway/Work endpoint changes
last after sealing/draining receipts. This dependency follows the existing topology; it is
not permission to move any collector, Phase 24B checkpoint, Context or Work authority now.
Separate final approval must specify exact owner, ingress/retry accounting, rollback and
how post-transfer commits/receipts would be preserved on reversal.

## Safety confirmation and tests

Railway remains authoritative. OVH Research has no production write authority. No duplicate
scheduler/collector, Context/DNS/OAuth cutover, Lab restart/profile/DB change, Dynamic Universe
warming or paper activation was performed. Only the disposable migration copy was opened
writable to rehearse schema changes. Transient validation jobs have no production authority.
Unrelated user workspace changes were preserved.

The 113 unique focused cases passed across the initial 112-case run and the final
8-case backup run (7 overlap): 69 runtime-profile, 21 Shared Intelligence causal-clock,
15 prior namespace-authority and 8 new backup/namespace cases.
Ruff and formatting pass across application/scripts/tests/dashboard and the evidence drivers;
`git diff --check` passes. The full 1,758-case run completed in **2,284.87 s**:
**1,756 passed, 2 failed**. It is not a green full-suite result.

1. `test_context.py::test_snapshot_performance_is_cheap`: parallel-run ingestion took
   113.30 s for 400 events, above the 80 s budget; the snapshot itself took 0.11 s.
   The isolated serial retry **passed in 13.40 s**. No timing threshold was changed.
2. `test_prospective_sizing_cohort_spec.py::test_source_definitions_and_artifact_are_unchanged`:
   frozen `ops/profiles.py` hash expects `d6ba8b7d…`, actual `c999a7a1…`. This reproduces
   on a clean **starting main c5b81f3** checkout. The earlier `3783fe7` Research-profile
   preparation changed this file; this tranche did not. Existing Lab profile digests pass
   their focused tests. The frozen cohort manifest/guard was not changed or weakened.
   Separate reviewed provenance/semantic reconciliation is required before that future
   sizing cohort's release/activation. This is an existing release gate, not an observed
   source→candidate data-parity mismatch.

Final host audit: zero installed/loaded Research units, no Research crontab or application
listener. All five existing Lab service PIDs, start timestamps and restart counts match
before/after; Lab release symlink and both existing config hashes match. Railway still has
one scheduler (PID 3), 33 cron entries, original runtime/revision, Nimble off, and its gateway,
ingest, free-source and microstructure processes. Only allowlisted metadata was read.

Independent off-host recovery remains the current tranche's blocking foundation gate.
The full-suite provenance failure remains separately visible for future release planning.
No parity BUG/UNKNOWN or measured capacity fault was concealed to obtain this verdict.
