# Paper v2 operational retirement

Paper v2 is retained as the original fixed-horizon execution baseline. This retirement
does not change any research policy, hypothesis verdict, forward horizon, Phase 27
execution rule, `paper_nimble_v1`, or either Phase 30 event cohort.

## Lifecycle

`market paper v2 retire` appends a `run_state` event stopping admission. Unfilled
intents receive append-only expiration events; an open position enters `DRAINING`
and retains its existing frozen fixed-horizon exit, liquidation guard, costs and
settled-funding accounting. The first management evaluation that confirms the
account is flat appends `RETIRED`. Repeating retirement preserves the original
timestamps. No historical record, identity, definition or policy is rewritten.

Retired runs remain in the same tables and read-only reports. Status includes the
admission-stop and retirement timestamps. Trades, signals, rejected opportunities,
context evidence, hypothetical outcomes, fills, funding, PnL and equity marks remain
queryable. Direct evaluation, account management, shadow outcomes, degradation,
stored briefs and notification delivery stop for retired runs.

## Runtime

The existing scheduler skips v2 evaluation and brief jobs after retirement, before
creating runtime-cycle rows or child processes. `market ops crontab` omits their
cron entries once all v2 runs are archived; this also applies at the next container
start. A running scheduler can reload its filtered crontab using its existing
SIGUSR2 mechanism ([Supercronic documentation](https://github.com/aptible/supercronic#reload-crontab)).

The hourly `paper_v2_funding` job has a legacy name, but its settled-funding feed
is shared with Nimble. It remains at the existing cadence while a Nimble run
exists. Shared intraday bars, microstructure, forward research, checkpoints,
context ingestion, the Nimble worker and the Nimble brief are preserved.

## Independent research

Phase 27's `paper_nimble_research` stores descriptive forward outcomes separately
from execution positions. Its worker continues resolving
`paper_nimble_research_pending` on the minute cadence, using independent research
quotes. Its 15/30/60/120/240/480-minute research curves continue after dynamic exits
and are independent of the retired v2 evaluation runtime. The Phase 30 research
curves and generic prospective research/checkpoint collectors are unchanged.
Previously stored `paper_v2_outcomes` remain available as historical evidence;
their hypothetical paper costs remain distinct from descriptive research returns.

## Production evidence

The admission stop was recorded at **2026-10-10T19:48:53.361582+00:00** for
`paperrun_v2_3811cccc40db4fbe6413ebd018c129d1026748f3cd72e619ec80ba88e47ada95`.
At that instant the run had six closed trades and one SOL short with a frozen
exit due at **2026-10-10T21:08:00+00:00**. Its equity mark was
99.18814911187334 USDC, including the still-open position.

Verified pre-retirement backup:
`/data/backups/manual/prism-20261010T194711Z-pre-paper-v2-retirement.duckdb`,
SHA-256 `19c79b4c8f2bb35e2f2e55885701c0c7f31db7fa174dc2ec05ced0bd12f69de3`.

[Before snapshot](evidence/paper-v2-retirement/before.json) records every v2 row
hash, frozen run/policy metadata, independent research counts and process samples.
[SQL benchmark](evidence/paper-v2-retirement/sql-before.json) measures one
already-evaluated grid on an explicit throwaway copy of that backup; it is
engineering evidence only, with no live prospective writes.

The last 96 successful production evaluations averaged 4.134875 CPU-seconds and
3.602625 wall-seconds, with a mean process peak RSS of 505.49 MiB. Eliminating 96
scheduled evaluations/day saves an estimated **396.95 CPU-seconds/day**
(6.62 CPU-minutes, approximately 0.00459 average vCPU), excluding Python startup,
brief work and runtime-cycle overhead. Evaluation count and account-mark writes
fall from 96/day to zero; two cron entries representing 97 launches/day disappear.
The scratch steady-grid evaluation issued 376 SQL SELECT statements and two
INSERT statements. These are SQL statement counts, not physical storage I/O.
Removing the scheduled evaluations eliminates those recurring reads/writes;
new-grid opportunity/context writes would add further savings.

RSS savings are transient: v2 has short-lived evaluation processes, not a
dedicated persistent worker. Do not interpret its mean peak RSS as a permanent
service memory reduction. Shared worker CPU/RSS continues to depend on market
and event traffic and is not optimized by this change.

The completion evidence and final retirement timestamp are recorded after the
frozen open position has completed.
