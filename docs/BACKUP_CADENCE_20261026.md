# Lab and Research backup cadence — 26 October 2026

## Lab backup cadence

The existing `prism-scheduler.service` / supercronic entry remains unchanged:
`30 1 * * * market ops cycle backup --trigger schedule --wait 3600` (UTC).
It creates a daily full backup in `/data/backups/daily`; approved manual/weekly
paths share the same global inventory and ceiling. `backup_live` checkpoints
under the owner runtime lock and excludes writers while copying the full DB.
Routine backups do not require stopping a trader.

The earlier failures were the intended refusal to create a third recovery file.
The guard counts approved full files regardless of their acknowledgement state;
manual off-host export was not followed automatically by governed local retention.
It never treated a failed timer as permission to discard an unexported copy.

`prism-lab-backup-export.timer` now runs at :50 each hour, including 01:50 after
nightly creation, with persistent catch-up. It exports pending registered copies,
restore-tests them from Drive, records the existing acknowledgement, and invokes
`market ops backup-retain --apply` with the persisted keep/pin/reason arguments.
It can also verify/register a complete interrupted source copy before export.
The original manual `deploy/vps/pull_backup.sh` remains available.

Payloads use the existing encrypted repository
`rclone:prism-gdrive:Prism/backups/research/restic`, isolated under host
`vps-prism-lab-backup` and tag `prism-lab-backup-v1`; plaintext manifests have their
own `Prism/backups/lab/manifests` directory. All Lab-tagged off-host snapshots are
kept, preserving Lab's historical archive behavior. No repository, credentials,
OAuth scope or provider configuration was replaced.

## Research backup cadence

The existing `prism-research-backup.timer` remains enabled at 03:15 UTC plus up to
10 minutes jitter, with its original persistence/retry settings. The main service
still runs as `prism-research-backup`, using the original `research_backup.py
scheduled` command, source descriptor, compressed generations, encrypted Drive
repository, restore procedure and Research host/tag. Its off-host retention remains
7 daily / 4 weekly / 3 monthly snapshots, grouped by its existing host and tag.

The source jobs were OOM-killed at 03:21:03 and 04:26:01 after creating full files
but before publishing their registered manifests. Those two files then caused
third-copy refusals from 05:26:41 onward. See
[the focused original failure log](evidence/backup-cadence/20261026/research-original-failures.txt).

The new pre-source hook fully verified/registered both interrupted files, exported
and restored them through the existing helper, and acknowledged them. Governed
retention then made fresh creation admissible. A fresh run of the original owner
checkpoint/spool/context publication and canonical Drive pipeline succeeded.
Core calibration, direct Context clocks and Context recovery metadata are present
in that fresh off-host manifest.

Only backup worker processes cap DuckDB at one thread and 256 MB working memory.
The source service retains its original 3 GB budget. The acknowledgement hook now
has the same verification budget and a bounded 30-minute timeout because it runs
full governed verification/retention instead of the former checksum/unlink step.
The newest registered full source remains, with its sealed pathname a hard link
to the same recovery file. No raw source unlink is implemented by the new glue.

## Changes made

Implementation and reproducible installation are in
[deploy/backup-cadence](../deploy/backup-cadence/README.md).
Only the backup helper, backup systemd units/drop-ins and tests/docs were added.
No application release, scheduler entry, runtime environment, authority claim,
Nimble execution code, hypothesis allowlist or paper policy was changed.

Each authority has its own lifecycle lock and uses its existing owner runtime
lock. A separate bounded repository mutex serializes restic operations, including
the original Research scheduled command. Neither authority requires the other
job to succeed. Healthy repeated reconciliation skips retention scans and owner
locks; live repeat checks completed successfully for both authorities.

Status on OVH:

```sh
sudo sh /opt/prism/backup-cadence/status.sh
```

This reports both authorities independently: latest local backup, latest successful
export and verification, local full-copy count, next-backup admissibility, last
retention action, persisted retention policy, last run and blocked reasons.

## Off-host verification

Both fresh backups passed full repository data checks and fresh restores from
Drive with no restic cache. Compressed SHA-256, decompressed SHA-256, sizes and
all-table fingerprints matched. Current physical source hashes were then checked
against their manifests and matching acknowledgement/restore hashes.

| Authority | Source SHA-256 | Verified snapshot | Tables |
|---|---|---|---:|
| Lab | `9209da9c8f4a6f129d0c6970b8c741a5a847f4a9df87f47c4855f343a0d92d79` | `f6ee3d049d8a9927f1517dee421bd73278fdc8a4f043705a7f427812a8be07fd` | 146 |
| Research | `eedde57f2aa75039ab0efd93ac090d47448722da6cb90fb58d45ec481bf3eb91` | `8a92470198f8acffbeb4f264da121bcfa4552c60614201cc13a3ac97713e8091` | 146 |

Lab's fresh backup was created under `vps-prism-autotrader-lab`. It covers all
13 `paper_nimble_*` tables, including runs, policies, accounts, positions, events,
triggers and trigger receipts, plus every related paper execution/accounting
and runtime table. Empty tables are fingerprinted too. See
[paper coverage](evidence/backup-cadence/20261026/lab-paper-coverage.json) and
[complete verification](evidence/backup-cadence/20261026/final-verification.json).
Recovery is at these snapshot times; execution continues after each snapshot.

## Governed retention behavior

The two-copy creation ceiling, one-newest-full-copy minimum, checksum checks,
verified off-host receipt requirements, approved paths, symlink/live-DB protection
and fsynced deletion audit remain unchanged. Explicit rollback pins are carried
forward; a policy/pin changing while the CLI waits for its runtime lock fails
closed before its new value could be overwritten. Pins are never released by
this automation.

The three full-file removals during verification have matching checksum and
restore acknowledgements in their preserved sidecars and exact governed audit
entries. Existing `backup_live` retention and the public retention command use
the same governed engine. Compressed export staging uses the existing generation
retention helper only after matching verification and owner acknowledgement;
the newest compressed export remains. Research's canonical generation retention
and its remote expiry policy remain unchanged.

## Failure safety

A live concurrent-export test encountered a restic repository lock refusal during
Lab's remote retention step. The local fresh recovery copy remained, no new
acknowledgement was issued, and local retention did not run for that failure.
The repository mutex fixed this collision. Earlier sandbox permission failures
also left recovery files intact. Provider stderr remains classified/suppressed;
no credential contents were printed or changed by the installation.

Tests cover failed inspection/export/acknowledgement, failed restore proof,
identity mismatch, unacknowledged staging, overlapping invocations, successful
step ordering, safe no-op retries, preserved/changing rollback pins and separate
Lab/Research remote retention behavior. **32 focused tests passed**; lint,
compilation, shell syntax and final systemd verification passed. See
[test results](evidence/backup-cadence/20261026/focused-tests.xml) and
[the preserved failure status](evidence/backup-cadence/20261026/lab-safe-lock-failure.json).

## Current local backup inventory

| Authority / path | Inventory |
|---|---|
| Lab `/data/backups` | One acknowledged full file, **384,053,248 bytes**, `manual/prism-20261026T115246Z-cadence-verification.duckdb` |
| Lab export staging | One verified compressed export, **72,656,150 bytes** |
| Research registered source | One acknowledged full file, **1,588,080,640 bytes**, `manual/prism-20261026T121528Z-google-drive-source.duckdb`; the sealed alias is the same inode, not an additional allocated copy |
| Research canonical generations | One verified compressed generation, **137,395,027 bytes** |
| Research reconciliation staging | One verified compressed recovery export, **135,873,158 bytes** |
| Historical Research foundation archive | Existing **135,614,775-byte** compressed generation retained untouched |

The full exact paths, sizes, link counts and checksums are in
[final verification](evidence/backup-cadence/20261026/final-verification.json).

## Next scheduled backup admissibility

**Both authorities: YES.** Each has one governed local full copy, sufficient disk
reserve, and no blocked reason. Lab's next nightly creation is 27 October at
01:30 UTC; its export/retry timer next fires at 12:50 UTC on 26 October. Research's
next timer deadline at verification is 27 October **03:21:53 UTC**. Both timers
are enabled. See [final status](evidence/backup-cadence/20261026/final-status.jsonl).

## Paper-trader impact

None beyond existing backup protection/locking. No trader or scheduler was
restarted or manually paused. Nimble remained active with PID **1032858** and
its original 09:33:43 UTC activation time. Both runtime environment hashes and
both application release targets match the initial inspection. Paper policies,
runtime authority, cohort selection and trading behavior were untouched.

## Remaining manual steps

None for routine nightly operation. An explicit operator rollback pin or a failed
integrity/export check remains an intentional safety blocker; automation does not
release it or delete a copy to make the schedule green.

## Final verdict

`BACKUP CADENCE SELF-SUSTAINING`
