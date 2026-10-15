# OVH Research validation evidence — 2026-10-15

No authority transfer. Production Railway revision `8f9bc3473142…` remains the source.
The frozen snapshot stays on OVH; evidence contains metadata, counts, hashes and access
clocks, rather than copied market/Work record bodies or credentials.

- `release-source*.json`: immutable source/overlay identity; unrelated dirty workspace files excluded.
- `railway-source-status.json`: allowlisted live flags and storage; no production DB mutation.
- `ovh-before.txt`, `ovh-after.txt`: Lab service identity/continuity and host resources.
- `installed-profile.json`, `isolation.json`, `service-audit.txt`: no authority/process/schedule,
  disjoint OS groups and DB access, prepared units uninstalled, no application listener.
- `source-snapshot-receipt.json`: exact preexisting verified Railway archive receipt,
  checked against the freshly materialized source fingerprint.
- `source-validation.json`: checksum and all 142 source relations matched the sealed receipt;
  30 representative application reads over identical source cutoffs.
- `schema-compatibility.json`, `migrate-proof.json`: additive schema 30→31 rehearsal, original
  DDL and scientific fingerprints retained, source/rollback bytes verified, measured resources.
- `candidate-validation.json.gz`: 30 identical application reads, five fully classified schema
  differences; ownership classification for 146 relations and per-record access catalogs.
- `clock-comparison.json`: exact origin record/ID preservation and explanation of rolling-window
  count differences at the later OVH availability cutoff; no unexplained mismatch allowed.
- `backup-proof.json`: one compressed local generation, manifest, logical/compressed checksums,
  two fresh scratch restore checks and sampled disk/RSS/CPU/time measurements.
- `restore-validation.json.gz`: same 30 reads and clock suite on the fresh restore, with zero
  differences from the imported candidate.
- `frozen-reader-code-identity.json`, `frozen-reader-code-diff.patch`: frozen Context,
  positioning, Research forward and Copilot readers unchanged from deployed Railway code.
  Shared Intelligence Phase A is new approved-main library code, not an already-deployed
  Railway producer; comparisons use that same reader on both sealed snapshot sides.
- `operation-driver.py`, `clock-audit.py`: exact one-shot administrative payloads; private
  Research paths only. Scripts execute under a network-disabled, one-CPU/3-GiB transient
  systemd service. No persistent Research service is installed.
- `focused-tests.log`, `backup-foundation-tests.log`, `full-suite-parallel.log`, `ruff.log`,
  `format.log`, `diff-check.log`: final verification evidence.

Earlier incomplete attempts are retained, not reported as passes. The initial installer
could not traverse Lab's private `/etc/prism` parent; only the new Research configuration
was relocated to `/etc/prism-research`. Source validation first hit metadata permissions
and then was interrupted to bound repeated evidence resolution; the successful v2 run
validates every evidence record and exercises representative resolution calls. Serial
pytest attempts were replaced with the full four-worker run with local socket permission.
The independent off-host workflow is prepared but unconfigured and unproved: the existing
OVH Railway archive is on the same VPS and cannot satisfy OVH disaster recovery.

The first supplementary clock audit made an incorrect assumption that every dropped record
was a bar/provenance pair. Its assertion failed without changing data. The retained per-record
diagnostic showed six bars, four ratios, one OI observation and one funding settlement; the
corrected audit verifies the exact documented window bounds for every dropped record.
The final `clock-comparison.json` passes; the earlier assertion is not reported as a pass.

Final regression outcome: 1,756 passed / 2 failed. Context performance passes its isolated
13.40-second retry; the frozen prospective-cohort profile-file hash guard reproduces on clean
starting main c5b81f3. See `baseline-frozen-provenance-test.log` and
`baseline-provenance-regression.json`. No frozen manifest/threshold was changed.

Before commit, trailing whitespace and extra blank EOF lines in four textual captures were
normalized for Git checks. Exact original bytes remain in `raw-captures/*.gz`;
`capture-normalization.json` records both hashes. Scientific evidence and test results are unchanged.
