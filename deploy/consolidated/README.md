# Prepared full-OVH foundation and live temporary bridge

Research services remain **uninstalled and disabled**. A separate disabled namespace was
installed October 15; see `docs/OVH_RESEARCH_NAMESPACE_VALIDATION.md` for its validation
and remaining independent off-host backup gate. Never deploy these through the singleton
Lab installer or change `/opt/prism/current`. Source review/release, dedicated Research
user/paths, independent off-host backup and explicit cutover gates come first. The full
plan is `docs/FULL_OVH_CONSOLIDATION_PLAN.md`.

## Temporary Railway recovery bridge

Installed October 15 to preserve Railway as temporary authority. Its private key exists
only at `/data/railway-bridge/id_ed25519` (0600); config and pinned host key are in that
0700 directory. The operator key is never deployed. Sender and scheduler runner are
installed there, using the **deployed Railway** Python/code, not the unreviewed Research
profile. Container has `openssh-client`; check/reinstall after any redeploy. Custom cron
changes are also ephemeral and must be reapplied/verified after a replacement container.

OVH forced-command account: `prism-railway-archive`, UID 994, separate from `prism`.
Root-owned authorized key uses `restrict` and only this command:

```text
/usr/bin/nice -n 10 /usr/bin/timeout 300 /opt/prism/current/.venv/bin/python /usr/local/libexec/prism-railway-archive.py receive --root /var/lib/prism-railway-archive/archive
```

Stderr appends to `receiver-errors.log` under that archive root. It is not an interactive
or general deployment credential. Receiver has CPUQuota=100%, MemoryHigh=2G, MemoryMax=3G;
DuckDB is one thread/256MB but wide-value scans require additional process memory.
Initial 1G cgroup OOM is recorded. Source/receiver DuckDB version was **1.5.5**. Receiver
currently uses the existing Lab interpreter read-only; revalidate its dependency/version
if that release changes. Archive publication and receiver root do not touch Lab data.

Source cron has one scheduler (PID 3 at application):

```cron
30 1 * * * /app/.venv/bin/python /data/railway-bridge/railway_bridge_job.py nightly
55 * * * * /app/.venv/bin/python /data/railway-bridge/railway_bridge_job.py retry
```

Nightly runs the unchanged normal backup cycle, then exports. Retry does not run another
backup. Source runtime flock queues up to 240 s, wrapper export deadline 360 s, receiver
300 s. Failure preserves the source unless a verified, fsynced deletion already completed.
A full copy can temporarily make acquisition's capacity guard refuse; never bypass it.
If export fails, retry and inspect the exact backup/receipt before any cleanup. At the
current size there is about 542 MB over the normal backup guard, not migration headroom.
Runner reports nonzero if free falls below guard plus 256 MiB. Errors appear in scheduler
logs and `/data/logs/railway-bridge.log` (three files, 1 MiB each); no new messaging route
or alert credential is assumed. Operator must monitor this temporary bridge daily.

Every accepted archive has a `.gz.manifest.json`: full original source sidecar, exact
DB size/SHA, schema, source revision/run/policy IDs, every-table count/value fingerprint,
last completed runtime cycle, authority tail, compressed SHA and actual verification time.
This is a physical DB frontier, not a guarantee every captured cycle was complete.
Intent, receipt and deletion completion remain in `/data/backups`, including original
manifest sidecars. Archives are never rotated/deleted by this bridge. Keep at least two
verified generations; monitor disk and the receiver's 20 GiB reserve.

## Recovery procedure (administrative execution requires a reviewed restore plan)

1. Retrieve the selected gzip and exact `.manifest.json` from OVH using the operator's
   administrative SSH access (the archive key cannot read files). Preserve these original
   artifacts. Select by DB SHA/frontier, not merely the newest filename. The Oct 15 manual
   proof's snapshot is `prism-20261015T113557Z.duckdb.1caa6c9677e1.gz`; its receipt is copied
   into `docs/evidence/full-ovh-consolidation/20261015/fresh-backup-receipt.json`.
2. On an isolated local disk with space for compressed+restored+verification reserve,
   verify compressed SHA against `artifact_sha256`, gzip-decompress to a **new** file and
   match logical length/SHA against `source.source_size` and `sha256`. Never overwrite a
   live file or accept an archive merely because gzip can decode it.
3. Use pinned DuckDB **1.5.5**, read-only connection, one thread and bounded memory/spill.
   Compare schema, authority claim, all 142 table counts and every-value fingerprints with
   `restore_test.all_tables`; preserve differences as failures. The receiver already performed
   these checks on each published archive. `backups.inspect_backup` supports historical schemas;
   do not use a command that opens `Store` writable and silently migrates the recovery file.
4. Recover matching code/dependency lock and secret names from the source metadata/release,
   plus separately retained raw/spool/calibration/context receipts and exact ingest offsets.
   A DB backup alone does **not** back up every external journal or secret. No archive key
   contains provider/OAuth secrets; recover those through the approved secret mechanism.
5. Keep schedulers/ingress/workers stopped. Inventory any in-progress runtime cycles captured
   during backup and record recovery disposition; do not invent a successful finish. Match
   source identity/schema and restore DB/raw/spool together under an approved single-writer
   plan. Do not start a schema-31 image on this schema-30 production file as a shortcut.
6. Re-run read comparisons and causal/provenance/collector frontier checks, then explicitly
   authorize one owner. Nimble stays OFF. Moving scientific authority or restoring into a
   new runtime requires the full plan's sealed-source handoff; the source claim in this
   archive is not permission to activate an OVH scientific writer.

Before target writes, rollback leaves Railway untouched. After target writes, preserve and
reverse-transfer all new commits/receipts before source reactivation; DNS reversal alone
is insufficient. No production restore or final cutover has been executed here.

## Reverting only the temporary schedule

`/data/railway-bridge/pre-bridge.crontab` and the complete local before/after evidence are
retained. Restoring the old file is **not** safe unattended: the original nightly full
copy refills the volume and reblocks acquisition. Restore it only together with a proven
replacement backup/export path or a deliberate stopped maintenance window. Validate with
`supercronic -test`, atomically replace the file and SIGUSR2 the sole scheduler. Do not
restart the whole Railway service or collectors just to reload cron. Revoke the dedicated
archive key only after the final Railway archive and replacement backup are verified.
