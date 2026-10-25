# Conservative retention Phase 1 operations

See [deployment report](../../docs/PHASE1_RETENTION_ROLLOUT_20261025.md) for exact policy cutovers and evidence. The installed interface is `python -m market_signal.ops.retention_phase1`; it does not require a collector/application restart or a new database writer.

## Current deployment

* Lab state: `/data/retention-phase1`; candidate reporting only.
* Research state: `/var/lib/prism-research/retention-phase1`; only `hyperliquid_settled_funding` enrolled.
* Lab timer: `prism-retention-phase1-prism.timer`, daily 02:10 UTC.
* Research timer: `prism-retention-phase1-prism-research.timer`, 04:12/10:12/16:12/22:12 UTC, maximum 100 eligible originals per pass.
* Existing independent backup permissions, existing application units, resource guards and collectors are unchanged. The restricted Research oneshot uses root to read the separate backup owner's private custody; the database connection is read-only and filesystem writes are restricted to retention state/raw plus private temporary scratch.

## Candidate-only inspection

```sh
sudo -u prism /opt/prism/current/.venv/bin/python -m market_signal.ops.retention_phase1 report --db /data/prism.duckdb --raw /data/raw --summary
sudo /opt/prism/research/current/.venv/bin/python -m market_signal.ops.retention_phase1 report --db /var/lib/prism-research/import/prism.duckdb --raw /var/lib/prism-research/raw --summary
```

Omit `--summary` for exact file-level reports. Reports contain identities/reasons/bytes, not raw responses, URLs, parameters, or credentials. They do not write production data or policy state. Do not retain unlimited copies of full reports; the rollout keeps one compressed exact-file report and compact summaries.

`qualify` checks representative recent settled-funding originals against canonical data without enrolling or deleting anything. `triggers` reports inactive classification and historical candidate counts without deleting rows. `baseline` records filesystem/database counters, queues and provider sizes. These commands use the existing authority flock and native read-only DB access.

## Enforcement and monitoring

```sh
sudo systemctl start prism-retention-phase1@prism-research.service
sudo systemctl show prism-retention-phase1@prism-research.service -p Result -p ExecMainStatus
sudo journalctl -u prism-retention-phase1@prism-research.service -n 10 --no-pager
```

`last-run.json` retains the last successful apply summary. Unit exit status/journal exposes failures. Successful oneshots normally become inactive; their timers remain active. A successful zero-expiry pass is expected while originals are young, legacy, latest-source protected or held. It is not evidence of mature savings.

Policies protect the current UTC date and previous 30 completed dates. Legacy capture timestamps, not filesystem mtime, establish custody. Each actual removal requires a current reviewed dependency/code frontier, exact normalized coverage, verified recovery and no applicable hold. An intent with capture clock, source/run/body hashes and disposition is fsynced before unlink; interrupted completion is reconciled idempotently. Compact daily expiry journals avoid one tiny file per expired original.

## Re-review and failure handling

New study registrations, schema/consumer-code changes, malformed evidence and missing recovery proof retain data. A reviewed manifest change must preserve original activation timestamps, all existing scientific/custody holds and immutable review records. There is no automatic study-release API in Phase 1. Do not rerun `prepare` to reset a cutover: it refuses an existing manifest.

Do not enroll other providers by editing a name into the manifest. Their sufficiency validators are not implemented. Lab funding normalization is qualified but fresh prospective recovery coverage is withheld. Normalized values alone, upstream availability or age cannot establish eligibility.

If maintenance fails, investigate the explicit blocker. Do not delete its state, bypass the old-tool guard, lower admission allowances, force compaction or prune study evidence. Snapshot inspection can also retain data when its scratch/RAM reserve checks fail.

## Safe pause / rollback

Disable the new Research timer to stop future expiry passes; stop its oneshot too if one is running. An interrupted pass is retry-safe because evidence intent precedes unlink. Leave protection manifests and custody intact. Application collectors do not need to stop.

Original overlaid source files and exact deployment receipts are retained under `/etc/prism/retention-phase1-20261025/{lab,research}`. Restoring source is a reviewed source-only rollback; do not restore an old database to undo an operational policy. Missing/stale configuration preserves wakeups and refuses new expiry, and the legacy deletion path remains blocked while Phase 1 state exists.

The first useful mature-growth observation follows the first post-cutover cohort's complete expiry boundary and a verified bounded pass. Track avoided rows, expired original bytes, protected exceptions, DB reusable blocks and OS file growth separately. The existing 400 MB/day admission allowance remains in force.
