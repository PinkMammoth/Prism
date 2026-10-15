# Google Drive backup evidence — 2026-10-15

No authority transfer; no secret values. See [GOOGLE_DRIVE_BACKUP_RECOVERY.md](../../../GOOGLE_DRIVE_BACKUP_RECOVERY.md).

- `lab-before.txt`, `lab-after.txt`: Lab unit PIDs/start/restarts, release symlinks, Lab config/crontab
  hashes, host resources. Only the Research release symlink changed (intended).
- `install.log`: Research release `13e35be` staged; backup identity, dirs, pinned tools, units (disabled).
- `repository-init.log`: restic repository `148cfeaa…` (format v2) created at `Prism/backups/research/restic`.
- `service-run-1.log`, `service-run-1-status.json`: first real service run — fresh generation, upload,
  remote listing/size, full `check --read-data`, no-cache restore proof, manifest copy, retention.
- `drive-restore-proof.log`, `drive-restore-proof.json.gz`: local generation deleted; sandboxed restore
  from Drive with every local copy and the source DB inaccessible; SHA and table verification.
  The first validation attempt in this log failed on a permission (backup identity cannot traverse
  Research `state/`); it was rerun with byte-identical reference copies, not counted as a pass.
- `drive-restore-validation.log`, `drive-restore-validation.json.gz`: network-disabled 30-read parity,
  clock suite and ownership classification on the Drive-restored DB — passed, zero differences.
- `failure-checks.log`: live wrong-key / revoked-token / missing-repository / missing-config failures,
  each surfaced as a fixed credential-free class.
- `service-run-2.log`: second run (dedup 11,217 B added, retention, one local generation).
- `automation-and-permissions.log`: timer enabled, credential file owners/modes (sizes of secret files
  redacted), Research/Lab users cannot read credentials, zero token strings in the journal.
- `tests.log`: focused test run.

Not retained: OAuth tokens, client secret, restic passwords. A first authorization used rclone's
retiring shared client; its token was shredded before any Drive write.
