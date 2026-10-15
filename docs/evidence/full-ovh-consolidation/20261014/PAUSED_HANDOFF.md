# Paused at user request — 2026-10-14

User is shutting down their PC. Stop all task work; resume only when requested.

## Completed production operation

Removed only `/data/backups/daily/prism-20261013T013014Z.duckdb` after exact off-host
checksum and independent decompression/all-table restore proofs for its existing archive
and a fresh production replacement. Metadata sidecar/policies remain on Railway; deletion
intent/completion are fsynced in `/data/backups/retention.jsonl`.

Logical bytes removed: 1,218,195,456. Allocated bytes removed: 1,218,199,552.
Free before: 1,555,816,448. Free immediately after: 2,773,975,040 bytes.
Source SHA: de3d9b4115461f6f0c0f83590ac58d74056c22b7ae341e7b7f699a451ee1f34d.
Exact review/apply evidence: `old-backup-removal.json` and `.applied.json`.
Fresh recovery artifact: `data/backup-archive/railway-research-cutover-20261014/bridge-pre-reclaim-schema30.duckdb.gz`
SHA: 62e5eff00c75b59dfd2751f7c1a40965e4f56ec2fe166eb33aead35f7a8ad2b4.
All full archives are on this PC, outside Git. Older exact archive is retained too.

## Important unfinished stabilization

Manual intraday attempt was skipped with exit 75 because hourly funding held the runtime
lock; this is NOT proof of successful recovery or a capacity failure. Existing scheduler
continues. Re-check the next scheduled intraday cycle or retry after lock release.
No normal full-backup test has been run yet. No periodic off-host export/removal installed.
The next normal nightly full backup can consume the reclaimed headroom and make intraday's
capacity guard refuse again. Do not claim enduring stabilization. Report this caveat to user.
The local machine has no available logged-in/lingering user systemd manager; do not assume a
local backup timer could reliably protect Railway while the PC is off.

## Safety/state

Railway code remains 8f9bc3473142..., schema 30, Nimble worker OFF. No cron edit, schema
migration, profile deployment, autonomous execution, DNS/OAuth change, volume purchase,
OVH mutation or final retirement. Existing Railway/OVH processes continue normally.
Upstream main was verified to equal local main 020896fd3a9d13bd9c4e99e11dac7d3e4e6a2543.

Read-only OVH sample: 6 vCore / 12,256,591,872 RAM bytes, 98,123,595,776 free disk bytes,
~11.27 GB available RAM; existing lab release 530ba0d... and rehearsal units active.
`/etc/prism/secrets.env` has no keys. Railway key presence and public OAuth endpoints were
recorded in secret-name-only evidence; no secret values printed or copied.

## Draft code — NOT deployed, validation unfinished

`scripts/railway_bridge_recovery.py` implements explicit plan/apply with local restore proof,
remote exact hash/identity checks, retained metadata and fsynced deletion evidence. Used for
the authorized removal. Ruff passed, additional safety tests still needed.

`src/market_signal/ops/profiles.py` adds a NEW `research_vps` profile identity, cloned from
research with disjoint VPS research IDs, leaving both prior profile definitions unchanged.
New digest: c2e2ff5e7735e6cab76d308b38e79e1a445e4e5ec3788d97af04891c0105aad0.
**Tests/test_runtime_profiles.py DIGESTS mapping has NOT yet been updated; profile tests
will currently fail the exact-profile-map assertion.** Add new identity and isolation tests.

`deploy/consolidated/` contains PREPARED, UNINSTALLED research env, namespaced systemd
service/target/slice and runner. No user, directory, unit, timer or secret installed on OVH.
**Draft env uses gateway host/port variables not yet supported by launch.py**, which still
binds 0.0.0.0 and reads PORT. Fix this before using the draft. Confirm results path writes:
Paths.results remains code-root/results, so provide a namespace-safe writable target/symlink
or reviewed config override. Runner requires chmod +x at installation.

## Next resume priorities

1. Read this handoff and current production evidence; re-measure Railway volume/backups and
   verify scheduled acquisition/Context/core-six health. No DB migration.
2. Complete normal backup test, export/restore it, and reclaim that exact verified duplicate
   if needed. Solve temporary recurring archival without dependence on this PC being awake.
3. Validate/correct the prepared VPS research namespace/profile isolation; never use existing
   singleton install_release.sh to restart/change the active lab inadvertently.
4. Create docs/FULL_OVH_CONSOLIDATION_PLAN.md with all requested deliverables, phased exact
   authority/clock/collector cutovers and approval gates. It has NOT yet been created.
5. Read Shared Intelligence Phase A clock model: copied origin clocks remain; OVH consumer
   received/available frontier must be newly measured, never backdated. Recommend separate
   Research/Intelligence and Lab writable DBs, immutable read projections, no shared writers.
6. Final ingress/DNS/OAuth/research authority/shutdown cutover still requires explicit approval.

Previous uncommitted docs/artifacts belong to earlier work; preserve them. No subagents used.
