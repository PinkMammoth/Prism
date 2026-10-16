# Ranking-only follow-up review: c6b6e9d

Candidate: `c6b6e9da7e0d2e04f265e5fec9d1dde9a0a7d67c`, parent `b10d13a…`.
Archive SHA: `587daa0d5f8e251ca714a9af82578d73f496ee8d85b0768db8ccd9d7d6e01824`.
The user explicitly approved this exact candidate for ranking only. It was deployed and activated at14:12:19.818775 UTC after fresh host, backup, identity and resource checks. Live validation completed:13 fresh genuine evaluations spanning7200s, no rollback required. See the [final report](../../../OVH_DU_CADENCE_RANKING_VALIDATION.md) for measurements and the separate NOT READY warming gate.

The first b10 attempt stored one real evaluation at09:50:39 UTC (541 markets,23 candidates,
72/72 successful requests). The10:00 refresh exited1 after39.26s. Automatic rollback restored
v1 at10:01:19, preserved history and left core PID625 intact. All114 closed core market-minutes
in that interval were COMPLETE. Original child stderr was discarded, so its precise exception
is not retrospectively proven. A read-only public-fetch diagnostic subsequently succeeded.

Actual CLI regressions reproduce two contention defects in the rejected follow-up path:

- A normal writer owning the runtime lock briefly makes b10's zero-wait commit handoff fail
  immediately with `RuntimeBusy`. The fix waits at most60s and records actual wait clocks.
  Network is outside the lock; the whole child still has its300s timeout. The prior measured
  normal Lab backup held the normal workflow for about10s, well within this wait allowance.
- An external read-only DB observer can produce DB-wait progress before the JSON result,
  corrupting machine parsing. For `--json`, internal progress now goes to stderr; only the
  final JSON result goes to stdout.

Failed children now retain only allowlisted exception-class signatures, stderr size and SHA256,
never arbitrary stderr text or values. The UTC600s schedule, actual evidence clocks,900s gap
reset, seven-observation gate, listing age, score/normalization/cohort/streak/dwell rules,
readiness/backfill/large-print semantics and all authority fences are unchanged.

Focused cadence suite:23 passed. Both new contention regressions fail before their fixes.
The original36s cadence regression remains present and passes. Full1809-case coverage is complete: **1807 passed,1 skipped,1 failed sizing provenance guard**.
This is composite coverage with interrupted completed passes preserved, not a single green run.
The final result is in `validation-summary-attempt2.json`. The two local
websocket-server cases hit restricted-sandbox bind denial, then passed with loopback permission.
Ruff, formatting and diff checks pass. Frozen policy/engine/ledger/portability/rich/profile and
prepared activation tooling are byte-identical to approved main.

The frozen sizing source-provenance guard is retained unchanged. Approved main already fails
its profile-source hash; authorized cadence/source/worker changes additionally alter pinned
hashes. This is not a green full suite. The latest explicit user exception names exact c6b6e9d, supersedes b10d13a, and grants no warming or sizing permission.

The approved exception is only ranking-only deployment/observation of c6b6e9d. Sizing, warming, execution, paper accounts,
real-money access and Research handoff remain OFF. Fresh host/backup/budget checks and the
unchanged prepared activation tool remain required. The completed new measured window contains13 genuine evaluations spanning7200s, excluding the preserved first attempt. The
stopped interval remains a genuine scientific continuity reset.

The revised observer samples host resources frequently, performs heavy DB audits after new
completed events or every120s while ranking is idle, and closes DuckDB before CPU-only replay.
Its configuration/rollback checks pass, including refusal to restore across unexpected pins.
Rollback has now also been exercised successfully on the live first attempt. New-attempt tools
and evidence have a separate directory; no previous decision/history is deleted.
