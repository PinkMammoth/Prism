# Railway research cutover evidence — 2026-10-14

Read the complete conclusions in `../../../RAILWAY_RESEARCH_PROFILE_CUTOVER.md`.

- `railway-status.json`: active project/service/deployment/volume identity.
- `20261014T173211Z.json`, `final-storage/`: full file/inode/allocation inventories.
- `storage-headroom.json`: exact known guards; null safe minimum means fail closed.
- `historical-growth-comparison.json`: mixed-period physical attribution, not future bounds.
- `runtime-audit*.json`: allowed flags, schedule, lifecycle obligations, cycle durations.
- `quiet-resource-sample.json`: 60-second OS sample without DB scans.
- `profile-diff.json`, `current.crontab`, `proposed-research.crontab`: exact profile/schedule diff.
- `retained-backup-manifest.json`: latest retained volume backup, verified off-host.
- `local-stream-proof-manifest.json`: transfer/decompression/all-table restore proof before live capture.
- `current-backup-manifest.json`, `current-capture.log`: fresh physical capture and exact frontier.
- `schema31-rehearsal.json`: all existing-table value fingerprints, disk samples and exact schema-30 restore proof.
- `legacy-obligations.json`, `nimble-settlement-rehearsal.json`: exact pending obligations and copy-only bounded settlement.
- `rows-in-24h.json`: bounded timestamp-row counts, not DB bytes or per-job write attribution.
- `gateway-health.txt`, `microstructure-health.txt`: read-only current health, not post-cutover acceptance proof.
- `profile-backup-tests.log`, `profile-backup-collected.txt`, `offhost-capture-tests.log`, `lint.log`: checks.
- `verification.json`: scope/safety outcomes and final local script/document hashes.

Production remained schema 30. No files deleted, autonomous trading restarted, OVH operation,
production migration, profile change or retirement. All full archive artifacts remain outside
Git under `data/backup-archive/railway-research-cutover-20261014/`. A current backup is not proof
that later writes or uncaptured external journals are recoverable. The first resource probe
self-classification was corrected in the second audit; use the quiet sample for figures.
