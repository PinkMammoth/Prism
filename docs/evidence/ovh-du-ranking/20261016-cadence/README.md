# Cadence correction tranche — current review gate

The first b10d13a ranking attempt was stopped after one stored observation and a failed second child. Automatic rollback restored v1; core collection and history were preserved. Stop decision10:01:19 UTC, environment restored10:01:21.269, restart commands complete10:01:22.841, profile file v1 at10:01:24.025.

Follow-up immutable candidate **c6b6e9da7e0d2e04f265e5fec9d1dde9a0a7d67c** is committed but **not deployed or activated**. Full1809-case coverage:1807 passed,1 skipped,1 retained sizing source-provenance failure. The user-approved exception named b10d13a; a candidate-specific c6b6 exception is pending. No permission is inferred from elapsed time.

Review [C6B6_REVIEW.md](C6B6_REVIEW.md), [validation summary](validation-summary-attempt2.json), [candidate identity](release-candidate-attempt2.json), [source patch](writer-handoff-release.patch), [frozen compatibility](frozen-compatibility-attempt2.json), [resource receipt, not installed](ranking-budget-before-deploy-attempt2.json), and [actual first rollback](attempt-1/rollback.json).

The precise original child exception is unproven because b10 discarded stderr. The actual CLI reproduces zero-wait writer contention and JSON contamination from read-only DB wait messages; c6 fixes both and adds safe failure signatures. Cadence23 tests pass. The original eee36s guard regression remains covered. No policy/readiness threshold changed.

`full-suite-coverage-attempt2.json` accounts for every case:949 restricted passes,23 cadence passes,3 permitted focused passes, then832 permitted remaining passes,1 skip,1 sizing failure. Two local websocket bind failures and a Context TestClient stall were proven sandbox-specific and resolved with permission. No failure was hidden by editing tests.

A resumed live window still needs13 NEW genuine observations spanning7200s; the old observation is preserved and excluded. The stopped gap remains a real scientific reset. New diagnostic tooling is staged under `/data/validation/du-ranking-cadence/attempt-2`; it is not started. Its final version must be recopied and checked before arming.

Resource admission remains PASS WITH SPECIFIC LIMITATIONS: fresh12:16:57 UTC receipt,55.011 modeled days above70% floor and7GB reserve. This is software-independent and grants no activation. Warming preparation is NOT READY while the genuine window is incomplete.

No paper, sizing, real money, warming or Research authority change. Railway remains authoritative for Research.
