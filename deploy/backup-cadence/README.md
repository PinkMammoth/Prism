# OVH backup cadence

This directory contains backup-only glue. It imports the installed authority's
backup verifier, executes `market ops backup-retain --apply`, and preserves the
current retention policy including an explicit rollback pin. It changes no
application release, runtime environment, authority claim, paper policy or trader.

Lab retains its supercronic 01:30 UTC backup. The independent export timer runs
hourly at :50, including 01:50, and catches missed runs after reboot. An existing
backup is compressed, uploaded to the existing encrypted Drive/restic repository,
read back without a cache, decompressed and fingerprinted across every table.
Both authorities use the existing encrypted payload repository
`rclone:prism-gdrive:Prism/backups/research/restic`; no second repository or credentials
are created. Lab uses restic host `vps-prism-lab-backup`, tag `prism-lab-backup-v1`, and its own
plaintext manifest directory `Prism/backups/lab/manifests`. All Lab-tagged
off-host snapshots are kept, preserving Lab's existing archive-history behavior. Research's existing
host/tag, manifest directory, schedule and remote retention are unchanged. The
existing protected credentials are used without modification.

Research retains its existing 03:15 UTC timer with up to ten minutes jitter,
backup identity, `scheduled` command, owner checkpoint/spool/context metadata,
and canonical compressed generation recovery path. A pre-source reconciliation
hook verifies, registers and exports any interrupted full source backups before
attempting another. The source process alone limits DuckDB to one thread and
256 MB working memory, avoiding the default host-sized allocation that exhausted
its 3 GB cgroup. The existing root acknowledgement hook now delegates to governed
retention rather than unlinking the raw recovery point. The newest full registered
source and its sealed hard link remain locally recoverable.

Each authority has its own nonblocking lifecycle lock. A bounded shared repository
lock serializes exports and the canonical Research Drive command, preventing
restic prune/check collisions; neither job depends on the other job succeeding. Owner inspection,
acknowledgement and retention also use the existing runtime lock. Credentials
are used only by the existing backup identity; root coordinates the same identities
as the existing Research acknowledgement hook. Source copies are never removed
by this glue. Full-copy removal is exclusively the public governed command.
Compressed export staging uses the existing generation retention helper only
after every generation has a matching off-host proof and recorded owner ACK;
one newest acknowledged compressed export remains. Healthy hourly retries take
no runtime lock and perform no full-table retention scan. Failed or ambiguous staging
is preserved. Neither authority starts or depends on the other authority's job.

Install as root using `sh install.sh SOURCE_DIRECTORY`. This installs only the
helper, backup units and backup drop-ins, reloads systemd and enables Lab's export
timer. Research's existing timer remains enabled. A focused manual verification
can start either backup service without restarting a trader or scheduler:

```sh
systemctl start prism-lab-backup-export.service
systemctl start prism-research-backup.service
sh /opt/prism/backup-cadence/status.sh
```

The status report gives local inventory, latest export and verification,
admissibility under the two-copy and disk-reserve guards, retained policy,
last retention action, last run and blocked reasons. Logs remain in the existing
systemd journal and one small last-run file per authority. Provider errors are
classified by the existing helper; credential-bearing stderr is never printed.
An explicit rollback pin or a failed/uncertain export can intentionally block a
future backup. Do not release a pin or delete a recovery copy to make the timer green.
