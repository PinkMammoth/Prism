# Full Prism consolidation onto OVH

Prepared from approved upstream/local `main` **020896fd3a9d13bd9c4e99e11dac7d3e4e6a2543**,
checked October 15, 2026. This replaces Railway research-profile deployment as the objective.
Railway is a temporary source, not the intended architecture. Final authority, ingress,
DNS/OAuth and Railway shutdown changes require separate approval.

Evidence: [October 14](evidence/full-ovh-consolidation/20261014/) and
[October 15](evidence/full-ovh-consolidation/20261015/). Prior detailed workload/storage
inventory: [Railway cutover audit](RAILWAY_RESEARCH_PROFILE_CUTOVER.md).
GB below is decimal; GiB is 2^30 bytes. Measurements, projections and unverified gates
are distinguished. An absence of a failure is not a cutover proof.

## Emergency Railway stabilization

October 14's explicitly authorized removal preserved both the byte-identical old backup
and a newer production recovery point off-host. Removed only:
`/data/backups/daily/prism-20261013T013014Z.duckdb`, **1,218,195,456 logical /
1,218,199,552 allocated bytes**. Its SHA-256 is
`de3d9b4115461f6f0c0f83590ac58d74056c22b7ae341e7b7f699a451ee1f34d`.
The replacement schema-30 snapshot SHA is
`62e5eff00c75b59dfd2751f7c1a40965e4f56ec2fe166eb33aead35f7a8ad2b4`.
Both were independently decompressed, checksum matched and all-table restore tested.
All sidecars/policies remained on Railway; exact identity and source metadata were copied
into the deletion plan. Intent/completion were fsynced. Free space rose from
**1,555,816,448 to 2,773,975,040 bytes** immediately afterward.

The pause crossed the next nightly backup. Actual runtime evidence now confirms:

- **October 15 01:30 backup succeeded**, producing
  `/data/backups/daily/prism-20261015T013019Z.duckdb`, **1,646,276,608 bytes**.
- Intraday had **25 successful cycles** in the latest 24 h, most recently **01:16**;
  after the full backup consumed headroom it again refused writes. The same window
  contains 71 failed cycles. This is recurrence, not permanent stabilization.
- Core microstructure: 96 successful cycles; daily Phase 24B checkpoint, research,
  macro/news/positioning and funding jobs succeeded. Production remains schema **30**,
  identity `railway-prism-runtime`, Nimble off.
- Initial resumed free space was approximately **1.12 GB**, below the **2.217 GB**
  normal backup guard. No scientific deletion or schema/profile deployment is justified.

The temporary solution is **normal nightly backup → verified off-host push → remove only
that verified redundant volume file**. It must not depend on this PC being awake. Exact bridge/recovery procedure:
[`deploy/consolidated/README.md`](../deploy/consolidated/README.md). The
prepared `deploy/consolidated/bridge_archive.py` receiver uses a dedicated forced-command
SSH key/account, checksum verification, bounded decompression, every-table reads and
source/restore row-count agreement. The sender verifies compressed and logical hashes,
source identity stability and the acknowledged restore before fsynced intent/unlink/completion.
Failures preserve the source copy. It never expires scientific evidence or archive history.

Receiver root is **`/var/lib/prism-railway-archive/archive`**, outside protected lab `/data`.
The archive user is separate from `prism`; it has no access to lab secrets or DB authority.
Its SSH key permits only the fixed receiver command: no interactive shell, forwarding,
agent forwarding or PTY. Private key stays on Railway. Receiver uses one DuckDB thread,
256 MB query memory, a 3 GiB account memory ceiling (2 GiB high watermark), at most one CPU, nice 10, 300 s timeout,
1 GiB compressed/16 GiB logical size limits and a **20 GiB filesystem safety floor**.
Archive plus restoration staging require additional free capacity before accepting input.

**Emergency bridge acceptance passed October 15.** Exact evidence is
[`STABILIZATION_RESULT.json`](evidence/full-ovh-consolidation/20261015/STABILIZATION_RESULT.json).
The 01:30 backup was restored on OVH, all **142 tables** read/fingerprinted, and its entire
source metadata retained. Only then was the redundant Railway file removed at **11:33:01 UTC**:
**1,646,276,608 logical / 1,646,280,704 allocated bytes**, free **2,760,253,440 bytes**.
Source SHA: `b6fa3c8657f29b0ed543868cc17793399a6b8500f8e43f6ff44763c120655867`;
compressed SHA: `51e6a8b550246e64f7020fb56d8fac159961b4a4b89863fe25cc7038bccc88ee`.
The complete archive/receipt are under the isolated receiver root, not the operational Lab DB.

A **fresh normal backup test** then succeeded: `prism-20261015T113557Z.duckdb`, same logical/
allocated bytes, completed at **11:37:12 UTC**. Its source SHA is
`1caa6c9677e1f87e4f04fc087a2534b3d8a4eac89d1e93d7fe0fc025213333c6`;
compressed archive **135,829,646 bytes**, SHA
`3e8ba0ffab4d5a93cc3b9cbd0632ca891887207e0a0656d69945fca86b2affa1`.
Restore/all-table proof passed at **11:40:42 UTC**, then its duplicate volume file was removed.
Snapshot's last finished runtime cycle is **11:31:40.099798 UTC**; the backup-cycle itself
was still in progress inside the snapshot. Exact physical bytes, schema, claim and captured
in-progress cycle state define recovery, not an invented all-jobs-complete frontier.

One export initially timed out queuing behind governed microstructure; it retained the source.
Sender queue wait is now **240 s**, wrapper export deadline **360 s**. Initial receiver OOM at
1 GiB was contained to its own account; the bounded 3 GiB retry passed. These failures are kept
in evidence. No Lab restart/configuration change occurred. Verified duplicate receiver staging
was also removed: **1,781,831,057 logical / 1,781,837,824 allocated bytes on OVH only**.
Error logs and all published recovery archives remain.

The active Railway schedule now calls the normal backup **then immediate export** at 01:30;
retry runs hourly at :55. Removed only the recurring 00:14 Nimble brief, preserving historical
reports. Crontab remained **33 entries**, one supercronic PID **3**, gracefully reloaded using
[SIGUSR2](https://github.com/aptible/supercronic#signals); collectors were not restarted.
The exact recurring retry entry point passed. The first automatic nightly wrapper run is
still due **October 16 01:30 UTC**; the fresh manual backup/export proof is not a claim that
this future scheduled run has occurred. Monitor that first run. Logs rotate at 1 MiB × three files; low headroom
returns nonzero in scheduler logs. This is temporary bridge infrastructure, not a new profile.

Scheduled intraday also succeeded at **11:46:37 UTC**, and scheduled governed microstructure
at **11:51:40 UTC** after the final export. A real post-export intraday cycle succeeded at
**11:42:03 UTC**; free-source Context committed
at **11:40:52 UTC**. Microstructure/Research/positioning/Phase 24B success and unchanged schema-30
Railway authority were verified. Three old Nimble runs remain flat: **54 CLOSED, 144 CANCELLED**;
**216 research items** remain, with no new execution ledger events after October 14 16:03 UTC.
The fresh backup/export work temporarily refilled the volume; one manual intraday test failed
before export and the post-export retry passed. Do not claim uninterrupted acquisition or
backdate late provider observations. Attribute missed acquisition/coverage windows before
scientific cutover; functional recovery does not prove historical gap repair. A nightly backup can still briefly trip acquisition's
capacity guard until export finishes; queue/export failure must remain visible and be retried.

Measured volume: **4,838,498,304 bytes total**, DB **1,646,276,608**. Normal guard requires
`ceil(1.2 × DB + 0.05 × volume) = 2,217,456,845 bytes` free. The recurring runner observed
**2,759,626,752 free**, **542,169,907 bytes above guard**. It fails visibly below guard plus
256 MiB. This is a reasonable short bridge margin, **not migration headroom or a long-term
capacity solution**. Do not deploy schema/profile changes on Railway.

Archive CPU/I/O is declared overlap with rehearsal observation and must be excluded from a
clean commissioning sample. No OVH scientific writer, ingress, Lab cohort or new authority was
started by this stabilization. The receiver alone is installed; target Research units remain
prepared and disabled/uninstalled.

No disposable Railway material of useful size was found. Temp/test/log cleanup is not a substitute
for the verified backup bridge. Keep at least two off-host recovery generations during
the transition; retain the exact removed backup's metadata and source revision.

Bridge limitation: custom source schedules and the SSH client must be checked/reinstalled
after a Railway redeploy/container replacement. Do not perform an unrelated deploy while
this bridge owns recovery. A missing receiver receipt or bridge error is a visible failure;
never bypass capacity guards to keep data writes running. Recheck free space before/after
each export. Alert if post-export free space is below the normal guard plus 256 MiB.

## Current Railway dependency map

One Prism service/replica (`prism-runtime`), one `/data` volume, deployment
`3ef1c302-90c4-48bb-a626-e1ea434d22b4`, revision `8f9bc3473142…`. No unrelated Railway
project/service is within shutdown scope. Four persistent Python workers plus supercronic:
HTTP gateway; gateway/free-source ingest + Work reaction collection; free-source collector;
governed core-six collector. HTTP ingress does not itself open DuckDB. Ingest and scheduled
writers serialize through the runtime flock; the collector writes journals/spool independently.

| Responsibility | Current owner / dependency | Destination / disposition |
|---|---|---|
| Work/OAuth ingress, durable receipts | Railway HTTP → spool → context ingest | OVH Research gateway/ingest; coordinated endpoint handoff |
| Free-source context, official news, macro, OI/funding/positioning | Railway acquisition + context history | OVH Research owns scientific history; Lab remains independently fed until an approved asynchronous projection is proven |
| Research Lab, experiment registry, datasets/provenance, forward/incubation | Railway research scheduler/DB | OVH Research writer, original definition IDs |
| Copilot/manual records | Railway prospective cadence/DB | OVH Research; dashboard reads immutable projections |
| Core-six raw/spool/minutes/revisions/calibration | Railway governed collector + quarter-hour ingest | OVH **Research** collector; not the existing Lab collector's history |
| Phase 24B | Railway original study/checkpoint authority | OVH Research, same study and owed grid, explicit handoff |
| Retired Paper v1/v2 evidence | Railway, work_needed=false | Immutable historical evidence; conditional drains never activate new accounts |
| Three old Nimble runs | Railway, flat but obligations incomplete | Finalize on Railway or an explicitly isolated retirement copy; immutable archive only afterward |
| New autonomous execution / Dynamic Universe / sizing | Existing OVH Lab commissioning gates | Stay Lab-owned; this plan grants no activation |
| Backup bridge | Railway copy → isolated OVH archive receiver | Temporary only; permanent OVH backups must go to an independent host/provider |
| Dashboard | Local write attempt rejected correctly | Read API/immutable projection; no write authority |

Freeze Railway scope now: no new account, execution cohort, DU warming, new science experiment
or long-term hosting optimization. Keep acquisition, Context, shared Intelligence, research,
Copilot, science and explicit legacy obligations until their ownership transfers. The recurring
Nimble brief has been removed; retain history and prepare one final retirement report.

## OVH target architecture and database topology

Recommend **B: two writable operational DuckDBs**, separate OS users, profiles, paths,
flocks, scheduler ownership and resource groups. Immutable archives/read projections are
additional files, not new scientific authorities.

```text
OVH VPS-3
  Caddy :443 (owned hostname; loopback upstream only)
    Research gateway :8787 → durable Work spool → Research ingest
  prism-research user / research_vps profile
    /data/research/prism.duckdb + raw/spool/calibration + research outputs
    Research Lab, Shared Intelligence, Copilot, macro/OI/funding/positioning
    governed core-six collector, Phase 24B, one Research scheduler
  existing prism Lab user / autotrader_lab profile (unchanged during preparation)
    current Lab DB /data/prism.duckdb, independent inputs and scheduler
    later DU/baseline/sizing only under their existing approval gates
  immutable projection publisher → read API/dashboard and optional async Lab subscriber
  isolated archive receiver (temporary Railway bridge)
  nightly per-DB local recovery + independent off-host compressed/encrypted archive
  immutable Railway-final legacy archive (never attached writable by Lab)
```

| Arrangement | Assessment |
|---|---|
| A: one writable DuckDB with separate profiles | Fewer files, but research/ingest/science and execution share a process-level writer lock and fault domain. It creates the synchronous dependency this move must avoid. Reject |
| B: Research/Intelligence and Lab writable DBs | Recommended. Local serialized writer per DB; independent authority, restart, resource and migration gates; small duplicate input cost is acceptable |
| C: separate service DBs/PostgreSQL/distributed DB | Not needed to leave Railway. A read snapshot/catalog is useful; another writable engine adds migrations, operational ownership and recovery complexity without measured need |

No shared concurrent writable connection, network-mounted DuckDB, cross-runtime file attach
to a live DB or dashboard write role. Each DB has its own runtime flock. Duplication of cheap
public input is intentional until a content-addressed, asynchronous read projection proves
semantics and availability. Execution never waits synchronously on Research locks.

Prepared **new** `research_vps` / `prism_research_vps_runtime_v1`, full identity
`vps-prism-research`, rehearsal `vps-prism-research-rehearsal`. Original Railway research and
Lab profile definitions/digests remain unchanged. The new profile forbids Nimble, DU and the
daily Nimble brief; funding is unconditional even when legacy Nimble accounts are absent.
Its digest is **46e81ec9a60ab07acaecd55675e477c65aa976ad978890e0c197cd8744f8be70**.
Prepared namespaced units/env/runner are under `deploy/consolidated/`; **not installed/enabled**.

Use `/etc/prism/research/{runtime,secrets}.env` and `/opt/prism/research/current`, not existing
Lab files/symlink. `PRISM_RESULTS_DIR` permits mutable Research output under `/data/research`.
Loopback bind is explicit; the legacy bind default is preserved. Stage DB copies never claim
production authority just to pass startup. Do not use the existing singleton `install_release.sh`
to deploy Research: it switches `/opt/prism/current` and restarts Lab units.

## Context gateway migration and clocks

Current public endpoint:
**`https://prism-runtime-production.up.railway.app/mcp`**. The resource audience is that
exact URL: `Verifier.environment()` derives it from `PRISM_CONTEXT_GATEWAY_URL + /mcp`.
Issuer: `https://dev-spoapkyacre0gg6s.us.auth0.com/`; public JWKS is its
`/.well-known/jwks.json`. Tokens require RS256, issuer, exact audience, scope
`context:submit`, and approved subject/client principals. Service bearer tokens are a separate
integration. Neither accepts arbitrary identities merely because a new host is reachable.

Recommend a stable owned **`context.<owned-domain>`**. Domain ownership is not established
in this session; no domain purchase, DNS record or Auth0 setting has been changed.
Sequence, with explicit external-configuration approval:

1. Choose/verify the owned hostname, export current Auth0 application/resource and Work/plugin
   settings without secrets, record callback/resource URLs and allowed principals. Inventory all
   five monitor chats/schedules and direct CLI/service consumers; do not assume updating one
   connection updates every stored monitor/action definition.
2. Prefer establish the stable hostname **while Railway remains authoritative**: Railway
   custom-domain/TLS setup, matching Auth0 resource audience, Work/plugin reauthorization and
   exact token validation. This adds one controlled preparatory ingress change, not a DB migration.
   If avoiding another Railway restart is faster/safer, combine this with the final gateway move
   and explicitly schedule a short ingress maintenance window instead.
3. Provision OVH Caddy certificate/reverse proxy, app bound **127.0.0.1:8787**, trust only
   loopback proxy. Open TCP 443 in **both** host nftables and OVH edge firewall; TCP 80 only
   if required for chosen ACME validation/redirect. Keep SSH key-only; no public DB or app port.
4. Validate metadata/resource URLs, TLS chain, issuer/JWKS, correct token success, bad audience,
   issuer, scope, principal and expired token rejection. Do not silently accept both audiences
   without an explicitly versioned transition. Reuse approved identity mappings; securely transfer
   or rotate service tokens and reconnect authorized consumers.
5. At ingress cutover drain/seal old receipts, switch DNS/endpoint and Work connections, then
   prove one **authorized** submission→durable receipt→commit→read result. Preserve duplicate
   and retry IDs. Do not submit invented market facts as an acceptance test.

There is no proven existing reliable forwarding relay from Railway HTTP to an OVH writer.
The fastest safe default is therefore **one coordinated DB-writer/ingest/endpoint window**,
not Research authority on OVH while the old ingest secretly continues writing Railway.
Pause senders or prove durable client retry first. If neither is possible, a bounded,
idempotent durable relay with receipt/ACK/clock proof is an additional gate—not an assumed
capability. Failed requests during maintenance must be accounted for/replayed; do not call
the ingress cutover lossless without that proof.

Clock contract is Shared Intelligence Phase A: preserve report IDs, event/update chains,
first_seen, origin observation, true origin commit bounds, raw hashes, mapping/source versions
and prior receipt clocks **byte-for-byte** where retained. New runtime receipt/apply clocks
are measured on OVH. `AccessClock(origin_id, origin_observed_at, origin_committed_at,
consumer_id, received_at_here, available_at_here)` orders commit≤receipt≤availability.
Never set OVH availability to historical Railway ingestion merely because bytes were copied.
Unknown historical commit/access clocks remain unavailable, not inferred from publication.

Produce immutable per-record content-bound access catalogs/manifests for transferred evidence;
catalog content ID, origin runtime, batch SHA, actual target receipt and after-commit/apply bound.
Compare origin-observed reads at identical cutoffs before/after; compare consumer-available
reads using the new OVH catalog and show historical cutoffs before receipt withholding data.
Retain canonical packages so replacements/revisions remain resolvable. Phase A is a library
and receipt utility, **not already-deployed replication or a persistent transport**.

## Research / Intelligence and authority cutover

Copy definitions/config, datasets and provenance, registry, forward/checkpoint/outcome evidence,
incubation/lifecycle, Copilot/manual records, context/history, macro/positioning, frozen snapshots
and raw references. The initial [142-table ownership plan](evidence/full-ovh-consolidation/20261015/table-ownership-plan.json)
binds the verified snapshot fingerprints. Refresh it at the final frontier; every table
must be classified retained-operational, immutable-legacy or explicitly derived/rebuildable.
Unknown tables block omission. Preserve canonical definitions and content IDs, not regenerated
equivalents. Keep source git revisions, DuckDB version, schema and raw manifests with recovery.

Pre-copy only immutable closed raw/spool hours and verified DB snapshots; compare SHA/length,
all-table counts/value fingerprints, definition IDs, study/checkpoint state and bounded reads.
Do not activate an OVH scientific scheduler on the copied Railway authority claim. Test schema
30→31 on a **current local/offline copy** before target use; no Railway schema migration.
Prior current-data schema-31 rehearsal/restore passed, but refresh evidence at final frontier.

**Do not transplant old Nimble accounts.** Preserve a complete Railway-final DB as an immutable
archive, including old accounts for evidence, outside both operational runtimes. Construct the
new operational Research DB with an explicit audited table/row export: old `paper_nimble_*`
account/run/position/trigger/lifecycle projections are not active operational rows. Their
definitions, ledger, outcomes and reports are retained in immutable evidence exports/read
datasets with exact fingerprints and references. No Lab seed/import contains these accounts.
The new Research profile collects funding without relying on the existence of old runs.
If clean export cannot preserve every required reference, stop; do not silently drop history.

Final writer handoff is a two-phase fence, with a signed/checksummed reviewable manifest:

1. Stop admission of new external ingress or prove buffering; stop old scheduler/new queued
   writers; finish owed work or record exact debt; stop/final-drain collector as below.
2. Drain and seal **all** Railway DB writers. Capture file/zero-WAL SHA, authority tail,
   runtime-cycle frontier, raw/spool hashes+offsets, pending gateway receipts, definition IDs,
   study/checkpoint tails and original role/environment. Take final backup and verify restore.
3. Apply final deltas into isolated target; test all retained fingerprints/read outputs, schema,
   access catalogs, ownership manifest and no legacy account activation. Railway stays sealed.
4. Explicit administrative transfer records old claim, source seal, new claim and target SHA.
   Normal target workers cannot overwrite the old claim to make startup pass.
5. Define **T_RESEARCH = actual UTC after-commit/fsync availability bound of the target authority
   handoff**, recorded in `cutover.json`. Old last commit < seal; new first scientific commit
   ≥ T_RESEARCH. Until measured, T_RESEARCH is **UNSET**, and activation must refuse. No fabricated
   date/time is assigned by this planning document. Exactly one scientific writer resumes.

The HTTP/ingest transition belongs in this same fence unless a proven relay exists. It is
unsafe to follow the conceptual phase list literally and leave two writable copies active.

## Core microstructure / Phase 24B continuity

The existing OVH **Lab** core-six history is independent; it does not retroactively replace
Railway governed observations. Keep original Railway rows, revisions, calibration, collector
sessions, raw journals, offsets, watermark and first production cutover record.

Specific cutover protocol:

1. Inventory frozen core-six membership, feature/aggregation definition hash, grace/late-event
   rules, subscription semantics, depth/fidelity, calendar/timebase and calibration IDs. Match
   the OVH Research collector's code/config; use only isolated excluded shadow comparison before
   approval. Do not mix DU desired-set/rich semantics into the governed collector.
2. Pick the next approved UTC minute boundary **T_COLLECT** after both sides are ready. Record
   the exact last Railway websocket receive, last finalized COMPLETE/PARTIAL minute per asset,
   session ID, late/revision tail and source clock health. Stop old collection and drain every
   complete old spool record **on Railway before changing the DB claim**.
3. This is mandatory: existing ingest rejects records whose collector runtime differs from
   the DB claim. Offsets are **absolute paths** (`str(f.resolve())`). Do not copy old spool
   files into the new live root and let them be rejected/consumed, reset offsets, or rewrite
   source path/identity to disguise them as new observations. Archive old sealed spool separately;
   preserve original offset rows. Transfer/drain unread tails before seal, or block cutover.
4. Start the Research collector under `vps-prism-research` into a **new empty live spool root**,
   independent of existing Lab spool. Record first receive, first finalized minute, session,
   feature identity and actual process/collector runtime. New records ingest only after the
   target claim is active; passive buffering can cover the writer-transfer interval.
5. The restart may lose an open minute. Mark every affected asset/minute PARTIAL/MISSING and
   record `[last valid old observation, first valid new observation)` explicitly. Retain any
   excluded parallel overlap as shadow evidence. No overlapping primary-key conflict is resolved
   by overwrite; any conflict or unknown gap blocks approval of continuity.
6. Preserve `microstructure_cutover` as the historical first-production fact. Append a
   `collector_handoff` runtime evidence manifest with old/new IDs, exact boundary, source/raw
   hashes, gaps and collection identity; never relabel old sessions to the new runtime.
7. Compare per-minute coverage and fields across an agreed overlap/reference interval, revisions
   as-of original availability cutoffs and start/end semantics. This proves comparable collection;
   it does not assert byte-identical independent websocket captures.
8. Preserve Phase 24B study/definition ID and seven-plus checkpoint/result tails; catch up only
   owed original daily/weekly grids after the final source checkpoint. Recompute shadow outputs
   from retained inputs and compare before publishing any new authoritative checkpoint.

No fabricated uninterrupted series. Any unavoidable gap and its scientific effect on the
existing validation window require explicit governance acceptance; otherwise defer cutover.

## Scheduled-job migration

All current UTC entries below migrate to one OVH Research scheduler under the **new** identity
only after the source seal. Original crontab had 33 entries / 13 job types before bridge
and brief changes. Current crontab has 33 entries, with the brief removed and archive retry added. Separate Lab scheduler continues only its existing profile-owned functions.

| Job / current cadence | Decision |
|---|---|
| prospective: 00:10, 00:45, 03:00, 06:00, 11:00, 17:00 + boot catch-up | Migrate with new identity; forward/Copilot/incubation unchanged. Run boot catch-up explicitly once after seal, not an uncontrolled parallel child |
| daily update/scan 09:00 | Migrate with new identity; same data/definition clocks |
| OI 02:30, 08:30, 14:30, 20:30 | Migrate scientific ownership; Lab independent acquisition is allowed, not duplicate scientific scheduling |
| funding hourly :05 | Migrate; unconditional in Research even after old accounts are removed from operational DB |
| positioning hourly :04 | Migrate with exact last source grid and first target grid; preserve venue/availability distinctions |
| intraday :01/:16/:31/:46 | Migrate, catch up refused source acquisition with truthful actual availability; never backdate missing collection |
| macro 00:20, 12:42, 13:42, 19:12 | Migrate unchanged semantics/new identity; keys and source envelopes required |
| news :08/:38 | Migrate with same dedup/first-seen semantics |
| microstructure :06/:21/:36/:51 | Migrate only after collector/offset handoff proof |
| Phase 24B daily 01:10 / Monday 01:20 | Migrate governed authority; exact grid debt, no duplicate checkpoints |
| backup 01:30 (now immediate archive wrapper; retry :55) | Replace with isolated per-DB OVH nightly backup + external archive jobs; stagger Lab/Research |
| gateway/source health every minute | Migrate; check both ingress and durable worker, not just HTTP liveness |
| Nimble brief 00:14 (removed October 15) | Retired recurring Railway/Research job; preserve old reports and create final explicit report |
| v1 paper/brief/shadow steps | Already no-op/filtered after retirement; keep historical evidence, do not reactivate |
| v2 evaluate/brief drains | Already absent from actual crontab; no new schedule once dependency audit confirms terminal |
| old Nimble accounting/research | Explicit bounded retirement command; never autonomous worker/scheduler support |
| temporary Railway archive bridge | Temporary source-only owner; remove after final archive and Railway shutdown |

Ownership manifest binds function→runtime→DB→epoch and source seal/target activation; startup
must refuse missing/contradictory identity. One scheduler process per DB. Do not start the
Research target scheduler merely because systemd has a unit file. After transfer inspect
process IDs, actual generated schedules, cycle authority and duplicate output keys on both hosts.

## Permanent backup architecture

Per writable DB: nightly local WAL-free checkpoint/copy under its own lock, checksum and
all-table verification; retain **one current local recovery point**, with a second only as
an explicit bounded rollback pin. Export immutable compressed/encrypted archives to a genuinely
independent off-host destination, verify stored hash plus downloaded restore before expiry.
The temporary OVH receiver is off-host **for Railway**, but is **not off-host for OVH**.

Permanent target gate: choose/configure an independent object archive or another always-on
host. The PC archive remains valuable but is not a reliable unattended nightly destination
when powered off. No paid storage account, bucket or key has been purchased/activated here.
Prefer S3-compatible immutable/versioned storage with client-side encryption and an independently
stored recovery key; least-privilege upload key and separate deletion/restore authority.

Suggested bounded retention, subject to evidence/governance: seven daily and four weekly DB
recovery generations off-host; retain named final/migration/scientific freeze archives by explicit
evidence policy. Recovery rotation is not permission to delete unique raw evidence. Archive
closed raw/spool hours by exact compressed and decompressed hash, with DB frontier and committed
offsets; no deletion on mere successful ingestion. Copy only stable tails or bounded captures.

Daily verify artifact/hash/manifest, weekly download-and-full-restore each DB, and rehearse
code+secrets+raw/offset recovery monthly or after schema/backup changes. Restore test uses
disposable paths, never live files. Preserve a code/dependency lock and secret-recovery procedure.
Stagger backup/archive work from critical execution/Phase 24B measurement windows; cap threads,
memory and I/O and record overlap. Alert on missed export/restore, low disk and stale recovery.

Planning RPO: ≤24 h for DB loss with nightly backups, shorter only after an actually working
intraday export/append-journal mechanism is proved. Raw/Work receipt RPO equals last verified
stable export, not assumed zero. Target RTO ≤2 h for these current-size DBs **is an objective**;
network transfer, secret recovery and full read/collector/ingress tests must measure it before
claiming an achieved RTO. Keep Railway-final archive outside Railway after deletion of service.

## OVH capacity estimate

Known VPS-3: **6 vCore, 12 GB nominal RAM, 100 GB nominal disk, 2 Gbps**; current measured
MemTotal **12,256,591,872**, available RAM **11,209,756,672**, available filesystem bytes
**97,954,357,248** at October 15 inspection. Existing Lab rehearsal is lightweight and has
not activated Nimble/DU/baseline/sizing full authority. The earlier 24 h zero-authority soak
passed, including comparable core-six capture, low steal and no OOM. It is not a full-stack test.

| Resource | Evidence / full-stack planning case | Limit / decision gate |
|---|---|---|
| CPU | Railway short post-Nimble sample ~0.20 cores; earlier research+execution ~0.67. Old Nimble alone ~0.45. Plan 0.5–2 cores average initially, bursts up to all six; DU/cohort marginal cost **unmeasured** | Warn >4.2 cores sustained 10 min, sustained steal >2%, or execution/science latency regression. Bound Research work; measure before adding tiers |
| RAM | Railway points ~3.5 GB incl cache; old Nimble RSS up to ~2.0 GB; OVH currently ~1 GB used. Plan 4–8 GB combined working load with ≥2 GB available; future DU/dashboard cost estimated, not proven | Research slice draft High=4500M/Max=5500M; cap Lab/other jobs so aggregate cannot consume all RAM. Warn available <2 GB 5 min; any OOM is a failed gate |
| Disk | Current live Research source 1.646 GB; archive ratio previously ~8%; plus Lab DB, one local copy per DB, raw/spools, projection and restore temp. Budget 10–20 GB initial managed data/recovery working set, not 100 GB of full snapshots | Warn free <30 GiB or projected <20 GiB in 30 days; fail writes/exports before reserve is consumed |
| DB growth | Mixed pre/post Nimble physical comparison ~301 MB/day is not future Research growth. File size stayed 1.646 GB across overnight check; internal free blocks mask row growth | Initial planning sensitivity 0.1–0.5 GB/day Research+Lab; stress 1 GB/day. Measure row/occupied-block growth and actual file size for each DB, including DU/cohorts |
| Raw/spool | Earlier OVH core-six ~63 MB/day; Railway retained/provider/spool net windows ~16/11/16 MB/day. Retention/churn differs from generation | Allow ~0.1–0.3 GB/day with separate core captures initially; DU richness scales with subscribed assets and traffic, not guaranteed linear |
| Backup/projection | One local full copy per DB plus next-copy/restore reserve; off-host compressed history | Include simultaneous live+backup+new .part+damaged-DB recovery, not just steady files; cap read projection retention |
| Network | Core feed ~11.7 KB/s on earlier benchmark; two independent feeds ~2 GB/day payload. HTTP, DU and backup egress additional | Port capacity is ample, but provider limits/latency and egress policy need measurement. Warn retries/disconnects/backlog; no paid network forecast from nominal 2 Gbps |

At 0.2 GB/day total managed growth, 60 GB spendable lasts ~300 days; at 1 GB/day ~60 days,
before copy amplification and reserve changes. These are sensitivities, not measured runway.
Disk growth/retention is the first likely **long-horizon** constraint; concurrent peak RAM or
DU tick latency may become limiting first **operationally**. Identify the observed threshold
before recommending an upgrade. Do not buy a larger tier preemptively.

The archive verifier's observed 1 GiB OOM and successful 3 GiB cap are additional peak-memory
proof. That cap is a ceiling, not reserved capacity: an 8 GB application working set + 3 GiB
verification + OS + 2 GB reserve does **not** fit this VPS. Before full activation, measure
combined peaks and run only one heavy backup/import/restore at a time; perform independent
archive restore verification off-host where possible. Bound deferrable Research batch work
without stopping governed collection. If required concurrent peak cannot preserve reserve
and latency, RAM becomes the measured upgrade trigger; idle capacity alone does not prove fit.

Reserve explicit headroom for DU rich warming, baseline and paired sizing by a staged capped
load test: retain existing commissioning plan, record active assets/cohorts, p95/p99 tick and
lock wait, collector gaps, checkpoint lag and available RAM. Start none of these as part of
this migration. Run heavy export/replay/build on the operator machine or outside the clean
commissioning sample; annotate unavoidable backup overlap. Migration need not wait for a
prospective sizing experiment to finish if DB ownership remains separate and load is bounded.

## Legacy Nimble retirement

Known source frontier: three flat ACTIVE runs, six provisional closes, 216 pending items /
552 outstanding horizons. Four distinct missing settled asset/hour inputs: HYPE October 10
16:00, HYPE October 11 17:00 (three runs), AAVE October 12 23:00, ETH October 13 00:00.
The last research horizon was due **October 15 00:01:00.919223 UTC**, now elapsed. Do not
assume all observations/rates exist merely because the horizon elapsed.

Smallest safe mechanism is **explicit `plan` → `settle` → `finalize`**, not a resurrected worker:

- Plan binds source DB/authority/schema/ledger tails, flatness, exact missing rates, pending
  horizon/reference clocks and final-report/delivery obligations. Frozen policy IDs stay fixed.
- Settle only existing positions/research: retrieve missing historical settled rates with source
  envelopes/hash and **actual new ingestion availability**, call existing reconcile/research,
  preserve missing-path/delay quality flags. No discover/admit/monitor/activation, new opportunity,
  fill or account. Append actual calculation/commit batch clocks separately from underlying quote
  availability; do not backdate a result's production merely because its source quote was old.
- Rehearse on a fresh backup, repeat to show idempotence and unchanged opportunity/fill counts.
  Existing copy-only rehearsal proved 72→64 items per run at October 14 frontier and six closes
  still unresolved. Refresh against post-maturity data before claiming completion.
- Finalize refuses nonflat state, missing settlements, pending outcomes or unhandled reporting.
  Freeze a final shutdown-day report and its delivery disposition, append a RETIRED run_state and
  retirement manifest/hash for all run-linked tables. Guard every event/research/report/recovery
  writer against mutation afterward; replay recovery must reproduce the terminal state.
- Disable all recurring dependencies. Archive original full DB and immutable ledgers/outcomes/
  reports; no old operational account rows enter either OVH runtime. An unresolved evidence-only
  archive can remain indefinitely with explicit incomplete status, but cannot be called complete.

No production finalization is authorized by this preparation. The supplied
`scripts/nimble_retirement_rehearsal.py` is copy-only; a partial status flip without writer guards
is not a safe finalizer. Missing historical settlement provenance is an explicit finalization gate.

## Dashboard/read architecture

Keep the current authority rejection: dashboard cannot write to the Railway-owned or new
Research-owned DB and must not use scratch mode to bypass it. Publisher under Research ownership
creates versioned allowlisted immutable read snapshots/canonical Intelligence packages after
commit, with SHA, source frontier, consumer receipt/apply clocks and staleness. Read API opens
those files SELECT-only and bounds queries; dashboard talks to API or a verified local snapshot.
No live-DB network mount, writable attachment or execution-role credentials. Keep old projection
until new one validates; atomic pointer promotion. This work is not a prerequisite for core move.

## Secrets audit and deployment boundary

No values were printed or copied into this document/evidence. October 14 names/presence audit:

| Name(s) | Railway | OVH deployment requirement |
|---|---|---|
| TIINGO_API_KEY, FRED_API_KEY, EIA_API_KEY | Present | Research secrets file; transfer securely, verify provider access without key logging |
| STOOQ_API_KEY | Absent | Optional; do not invent a dependency |
| TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID | Present | Research reporting/alert owner; Lab notifications remain independently gated |
| PRISM_CONTEXT_GATEWAY_TOKENS | Present, rotated October 14 | Research gateway only; secure transfer/rotation and consumer check |
| PRISM_CONTEXT_OAUTH_ISSUER/JWKS/PRINCIPALS | Present | Exact Auth0 resource/principal configuration; URLs public, identity map protected |
| PRISM_CONTEXT_TEST_TOKEN | Absent | No test credential assumed |
| PRISM_FREE_GITHUB_TOKEN, PRISM_HEARTBEAT_URL | Absent | Optional; no secret dependency silently created |
| dedicated archive SSH key | New on Railway, private 0600 | Public key only in OVH forced-command authorized_keys; revoke when bridge ends |
| operator SSH/deployment key | Existing on PC | Never copied to OVH or Railway; avoid account-wide Railway credentials |
| independent archive encryption/storage credentials | Not selected/deployed | Separate approval/configuration gate; recovery key stored independently |

OVH `/etc/prism/secrets.env` was empty at audit; do not assume Research provider/OAuth credentials
are already deployed. Existing files are root:prism 0640. New Research secrets should be
root:prism-research 0640 (or reviewed systemd credentials), distinct from Lab/archive users.
Document deployment status only, verify no secret-bearing URLs in logs, and rotate any value
exposed during future work. No exposure was identified in this resumed work.

## Ordered migration and rollback

| Phase | Executable work / proof | Authority and approval |
|---|---|---|
| A — verified | Backup bridge installed/tested; verified duplicate reclaimed; intraday/Context/science writes and normal guard proved | Existing Railway identity; no schema migration |
| B | Install reviewed **disabled** Research namespace, separate user/paths/release, private gateway config, bounded resources, local+independent off-host backup/restore | Lab unchanged; no target scientific scheduler/ingress enabled |
| C | Import immutable snapshots, clean Research operational export + immutable legacy archive, schema rehearsal, read/definition/clock comparisons | Offline/read-only copies; Railway remains sole scientist |
| D-prep | Collector comparability/shadow evidence, closed raw/spool pre-copy, exact boundary/offset plan | Shadow explicitly excluded; no silent scientific authority |
| E/F/D-final | One approved coordinated source seal, final drain/export/verify, collector boundary, Research claim, ingest/endpoint/auth transition | Actual T_COLLECT and T_RESEARCH recorded; no dual writer |
| G | Enable verified read publisher/API/Copilot consumers as appropriate; Copilot scientific job already transferred with Research | Read-only projections; no dashboard write authority |
| H | Verify representative schedule, backups/restore/ingress/science; retain rollback seal/archive; remove only Prism Railway service/config/volume | Explicit final shutdown approval |

Do not wait for paired sizing to finish. Preserve a clean commissioning measurement window and
throttle/defer migration compute rather than confusing resource evidence or stopping paper plans.

Rollback before writer activation: discard/rebuild only staging, leave Railway authority/clients
unchanged. Rollback after target writes **must not simply restart the stale Railway DB**:
quiesce target, preserve its DB/WAL/spools/receipts, validate/export every accepted new event and
scientific record, then reverse-transfer authority using a new seal/frontier and measured clocks.
If reverse data/schema compatibility is unproved, keep OVH sealed and repair there; do not create
divergent histories. DNS rollback alone restores routing, not DB state or a second writer.

Keep old code/config/auth exports and final Railway backup + raw manifests until rollback
window is explicitly discharged. Restore tests use exact bytes, matching code and source
authority metadata; never reverse schema migrations in place or re-enable default Nimble-on flags.

## Cutover proof and final Railway shutdown gate

For each function retain before/after evidence: exact source/destination frontiers and SHA,
all retained table counts/value fingerprints, same definition IDs and historical causal reads,
same context normalization/dedup chains, real authorized ingress receipts, source/consumer clocks,
collector coverage/revisions, Phase 24B owed grid/checkpoint state, scheduled outputs and read models.
New authority metadata/consumer clocks are expected differences; list them explicitly, never
whitelist unexplained scientific changes. Wrong identity, gaps, conflicts or failed backup stop the gate.

Do not shut the last Prism Railway service until all are true:

- OVH Research and Lab responsibilities have explicit disjoint DB ownership and health proof;
  no stale/duplicate Research scheduler or writer remains.
- External Context ingress, authorization and durable commit/read path work, including retries,
  rejection cases and outstanding old receipts.
- Research/Copilot schedules, required acquisition, core-six coverage and Phase 24B continuity
  are demonstrated across a representative daily window and owed weekly checkpoint as applicable.
- Local backups, independent off-host export, checksum/restore and operator recovery procedure work.
- Railway-final DB/code/raw/spool/calibration/metadata archives are verified **outside Railway**;
  legacy retirement needs no Railway execution. Retain incomplete obligations explicitly if approved.
- Forward/rollback state and the source seal are recorded and approved. No new writer starts on
  the source after the seal, including restart catch-up or queued cron processes.

Then inventory project/service IDs again, stop/remove only Prism's service/domain/volume,
remove Prism-specific variables/integrations/tokens, revoke bridge key, stop bridge jobs and
archive the receipts. Do not delete unrelated projects/services or exported evidence. Confirm
Railway no longer reports active Prism compute/volume, then inspect actual billing usage.

## Economics, immediate next tranche and verdict

Known fixed VPS cost: **£12.72/month including VAT**, existing paid plan. No upgrade purchased.
Final Prism infrastructure target: **£12.72 + deliberately selected independent archive cost**.
A provisional £1–5/month archive allowance gives **£13.72–17.72/month planning range**;
this is an unquoted budget, not a provider price or measured bill. Storage/transfer/retention,
VAT/currency and recovery needs determine the actual archive cost. Existing Work/LLM/provider
subscriptions are outside this infrastructure estimate and are not asserted free.
Railway residual usage/billing is transitional; zero cost is not claimed before actual shutdown
and reported usage. Do not compromise independent backup safety to hit an exact total.

**Single next migration tranche after the current rehearsal gate (Phase A is complete):** deploy the
reviewed **disabled OVH Research namespace plus independent backup/restore foundation**, then
import one verified immutable Research snapshot for read-only/schema/clock comparisons. Do not
switch the lab release, start a second scientific scheduler or change external ingress in that tranche.

**Verdict: READY AFTER SPECIFIC GATES.** The existing host has substantial measured idle capacity;
no architecture/capacity evidence yet forces an upgrade. Current-data
clean export/schema/clock proof, collector/Phase 24B handoff, independent OVH backup, owned-hostname/
Auth0/Work transition and explicit final authority/shutdown approval remain gates. This plan does
not describe the full stack as deployed or the migration as complete.

## Validation and operator handoff

Final focused suite: **174 tests passed**, including profile/authority isolation, restored backup
rejection cases and sender deletion gates, bridge scheduler retries, Shared Intelligence clocks,
core microstructure collection/ingest and existing Lab context/microstructure trigger integration.
Ruff and `git diff --check` passed. Two earlier local fake-websocket tests were refused by the
sandbox's loopback restriction; they passed with local socket permission. An earlier broader
Nimble worker run was interrupted; no full-suite result is claimed. The final focused run is
`final-focused-tests.log`. Automated regression tests used isolated databases; the separate
authorized live acceptance checks are recorded above.

Recovery tooling is committed locally as `af6a6d9`; isolated Research runtime preparation
is committed locally as `3783fe7`. These commits have not been pushed or deployed as a
new application release. The prepared Research runtime is not an approved deployed release.
Only the explicitly described emergency archive helpers/schedule were installed remotely.
No branch reset, unrelated user-file cleanup, schema deployment, DB authority transfer,
DNS/Auth0 change, external Work reconfiguration or Railway shutdown was performed.

On the next session, first recheck free space, the latest off-host archive/restore receipt,
normal acquisition/science cycles, bridge schedule/SSH client and the current Lab measurement
window. Keep both published October 15 archives plus prior PC recovery copies. The empty/failed
local stream files are not recovery artifacts. The archive-only OVH account has no authority
over a Prism operational DB. See the README's exact recovery and bridge rollback procedures.
