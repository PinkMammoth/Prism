# OVH DU cadence correction and ranking-only observation — 2026-10-16

**Current gate: v1 restored; DU OFF. Follow-up c6b6e9d is ready but not deployed.**

Follow-up full1809-case coverage is complete:1807 passed,1 skipped,1 frozen sizing provenance
failure retained. The b10d13a-only exception is not extended without a new user decision.
Fresh12:16:57 UTC resource sample projects55.011 days above unchanged reserves.
See [candidate review](evidence/ovh-du-ranking/20261016-cadence/C6B6_REVIEW.md).
The next genuine ranking window has not started.
This report supersedes the activation hold in [the earlier admission report](OVH_DU_RANKING_ADMISSION.md)
only as fresh evidence is completed below. Warming remains OFF. The completed Lab rehearsal is
not reset. Evidence: [cadence tranche](evidence/ovh-du-ranking/20261016-cadence/).

## Host visibility and existing backup closure

SSH was restored by 08:34:18 UTC without a firewall, SSH hardening, reboot or authority change.
The previous connection timeouts remain unattributed; continuous uptime and unchanged kernel,
clock, firewall and collector PIDs do not support a claim of host failure. Fresh checks found
kernel `6.1.0-53-cloud-amd64`, NTP synchronized, normal chrony, expected listeners, zero failed
units and accessible DB. The core collector remains PID625 with zero automatic restarts.

Research backup closure remains valid and independent. The exact cause was an abandoned
pre-reboot shared restic lock; ordinary stale-lock-only unlock repaired it. The unchanged
unattended service completed 08:08:30–08:13:18 UTC with full `check --read-data`, Google Drive
restore, checksum and 146/146 relation fingerprints, safe retention and unchanged repository.
Machine-key usability was freshly exercised; custodian-key usability relies on the previous
independent ceremony plus unchanged key/repository identities, not a new password entry today.
Railway remains authoritative for Research. See the earlier report for precise lock and stage times.

The new release's standard pre-deployment Lab backup is
`/data/backups/manual/prism-20261016T093742Z-pre-deploy-b10d13a31387.duckdb`,
SHA `c97813832e9d0b69f7311bf1e1103227807486a3e334e2f88e5a48afdc38d368`.
It passed on-host verification and an off-host restore/fingerprint comparison. Normal verified
retention replaced the earlier daily copy; no manual deletion or verification weakening occurred.

## Defect, invariant and correction

The rejected `eee205f…` worker launched every600 seconds measured from refresh start, while
both CLI/source due guards required600 seconds since the prior evidence clock at commit entry.
With36-second fetching, starts0/600/1200 produce commit clocks36/NOT_DUE/1236: accepted gaps1200s,
violating frozen900s continuity. “Commit time” in the earlier report meant commit **entry**, not
transaction completion.

The chosen fixed UTC schedule targets epoch multiples of600. First start/restart waits for the
next boundary; each next deadline derives from the previous nominal deadline, and elapsed slots
are explicitly counted/skipped without overlap or fabricated observations. Both admission guards
admit one actual refresh-entry slot, stored as versioned `utc_600s_slots_v1` metadata. This is the
CLI refresh entry before read-only preparation, distinct from network fetch start. The design
document's “source-start slot” refers to that refresh-entry basis.

Evidence clocks remain actual integer UTC sampled at commit entry. Source capture, availability,
rank completion and completed DB transactions remain distinct clocks. Genuine delays still reset
continuity at901s; no policy threshold or readiness rule changed. Completion-based nominal
(deadline+600) and UTC-anchored monotonic designs were considered; fixed UTC boundaries provide
simpler deterministic restart semantics on the synchronized host. A separate kernel flock spans
prepare/fetch/commit; network work does not hold the runtime writer lock. The worker is sequential
and kills/waits on its300s child timeout.

[Detailed design and clock meanings](DYNAMIC_UNIVERSE_CADENCE_FIX.md).

## Regression proof and reviewed release

Immutable release `b10d13a313870f0975842f1ee77e7b9938879359` was created; rejected `eee205f…`
was not relabeled or modified. The release archive SHA is
`3da6caafde04c5ee51684cd0785298d58c73c096f4d00bc808a8eb8ec387c46a`.
Profile v2 digest `e7571ad1a2a35fb42dc8c1ea77759e31887a1c7679445561664c1dee68c12207`;
policy ID `universepolicy_5f88b1dcdf20d7d19047edffab329076d7a63936f8c0916c21ffc83bcf4d56ff`.
Activation script SHA `f12039a006378c459f01ed5dc6fa283d910e21df333464009a08a9a7c1ce7a94`, unchanged.
Full source hashes and exact patch are in the evidence.

Cadence suite20 passed; DU/readiness/profile/authority/scheduler focused suite208 passed.
Cases include fast/36s/boundary/overrun/899s work, deterministic600s deadlines, restart, missed
cycles, no overlap, timeout and exact900/901s continuity. The actual CLI36s regression passes
here and fails against the unmodified rejected implementation (one evaluation instead of two).
Frozen engine/policy/portability/ledger/rich/profile/activation code is byte-identical to approved
main; source input/fetch/enrichment functions are AST-identical. Scores, cohort, streak/dwell,
seven-observation gate and all warming/readiness semantics are preserved.

Complete full-suite coverage: **1,804 passed,1 skipped,1 failed**. The failure is the frozen sizing
source-provenance guard: approved main already fails its profile-source hash, and this authorized
cadence fix additionally changes the pinned source/worker hashes. The frozen manifest/test remain
unchanged and failed. **The user explicitly approved a ranking-only exception for b10d13a at
09:34 UTC. This is not a green full suite and grants no warming/sizing/execution permission.**
The skip is the local real-restic/rclone roundtrip; live backup recovery has separate evidence.
Ruff, formatting and `git diff --check` pass for release code.

## Fresh ranking resource admission and activation

After deployment and before activation, 09:38:30 UTC sample: free disk94,599,426,048 B,
RAM available11,132,350,464 B of12,256,567,296 B; swap0. Preserve70% free floor73,827,595,879 B,
7,000,000,000 B backup/restore reserve and30-day minimum runway. Retained-growth capacity is
13,771,830,169 B. The conservative ranking-only250,000,000 B/day model yields **55.087 days**;
extra150 MB/day historical-step stress yields34.430 days. Temporary restore copies and release
installation are not persistent daily growth, and future rich warming is excluded.

Resource verdict: **PASS WITH SPECIFIC LIMITATIONS**, below60-day warning, unchanged absolute
floors. Existing measured component growth and thresholds remain in the earlier report; final
measured ranking costs and updated whole-host breakdown will replace provisional inputs below.

Installed budget SHA `69d3bc695944c4a88bf5f023e6d251bbf1a7c4120252f1ea0d5199085f91ce44`,
rich-assets6, bound to b10d13a. The prepared activation tool ran successfully at
**09:40:09.007059 UTC** with `ranking --budget /data/state/du-budget.json`.
It changed only the intended four environment fields, retained rehearsal authority and original
claim timestamp, saved the exact v1 environment and kept Nimble off. Core collection was not
restarted; scheduler/free-source services were intentionally restarted by the prepared tool.

The bounded observer began09:40:13 UTC. Its diagnostic-only restart before the first evaluation
at09:41 UTC added handling for an unpublished initial subscription plan, with earlier evidence
preserved; ranking and Lab processes were not restarted. Final observation timing and measurements
are pending. Configuration-level rollback is ready and guarded; it stops ranking, restores saved
v1 atomically only when core-only/no pins, waits for the writer lock, restarts only scheduler/free
services and preserves DB/history, core collector and Research/Railway. It has not been executed.

## Genuine observation and measured cost

The first attempt ran09:41:43–10:01:19 UTC (1176.005s), with one genuine evaluation at09:50:39.
The10:00 child exited1 after39.258s. Automatic rollback followed the10:01:19 stop decision: environment restored10:01:21.269,
restart commands complete10:01:22.841 UTC, preserving the evaluation
and restoring the exact original v1 environment hash; core PID625 was unchanged. No12-evaluation
or2-hour live cadence pass is claimed. Original child stderr was discarded, so its precise
exception is unproven. Both a normal-writer `RuntimeBusy` failure and JSON corruption under a
read-only DB observer were subsequently reproduced using the actual CLI on temporary DBs.
A follow-up immutable candidate fixes those handoff/output paths and adds safe failure signatures.
Full validation coverage is complete with only the disclosed sizing guard unresolved;
the b10d13a-only user exception is not silently extended.

The next accepted window still requires at least13 new genuine evaluations and7200s first-to-last span.
The observer records every launch/fetch/rank/transaction/deadline, stored-input replay, host/cgroup
resources and collector/scheduler/backup health. It rolls back on the authorized stop conditions.
No synthetic or backdated live observations are used.

## Warming preparation and next action

Warming remains **OFF**, readiness **NOT READY while observation is incomplete**. The final budget
will use measured ranking costs plus the existing6 core assets and up to6 satellites, about12 rich
markets/48 subscriptions, bounded45-day hourly backfill, prospective OI/funding, micro growth,
large-print calibration and backup overlap. Six slots do not imply six immediately ready assets.
All frozen scientific gates remain unchanged.

After a clean window, the exact separate approval is data-only warming for up to six satellites
under a fresh12-rich-market budget, with all frozen readiness gates and paper/sizing/real-money/
Research handoff OFF. Reusing this release for warming also requires resolving the full-validation
exception or explicitly extending its scope; the approval currently covers ranking only.
