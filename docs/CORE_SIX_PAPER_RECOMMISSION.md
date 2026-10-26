# Core-six Nimble paper recommissioning (OVH Lab)

Recommissioning, not a new engine. One static core-six paper account on the OVH Lab,
running the existing Nimble execution engine under an explicit successor policy.
Paper only: no venue client, signing, credentials or live mode exist in the engine.

## Identity

| Item | Value |
|---|---|
| Release | `788db37b140fe349dd2cfec1037f35c0baf7a8cb` (Lab `7cd872d` + recorded retention overlay `109c037` + this tranche) |
| Policy | `paper_nimble_core6_v1`, `paperexecution_core6_88b89e5c2e90d158aa4b38801d4bf9af442d043f73d32567978a94ba241c3df0` |
| Parent policy | `paper_nimble_v1`, `paperexecution_nimble_ec40a2968bcc33f7c7654a1c8d7dcc200361e88bca99079cb4f45738ba785501` |
| Risk policy | `paperrisk_v2_475b7c1e87fc2934eba4bb288f07cc818ed7dadb9fc8b9ab8d24a594a772e8b9` |
| Entry universe | `paperuniverse_core6_35b32ba211a7453c391c61279d0e7de74f609a1f962f5f748fc78c9b0da9f0fb` |
| Cohort | `core_six_prospective_exploratory_validation_v1`, classification `PROSPECTIVE_EXPLORATORY_VALIDATION_COHORT` |
| Run | `paperrun_nimble_a8928c8776bb6a2ff63d5a59b04e5c1e5a77d898df5dbe9815efb72abb5aa9c3` |
| Activation | `2026-10-26T09:13:43.019888+00:00`, $100 starting equity |
| Authority | `vps-prism-autotrader-lab` (`rtev_4ff14114198f4e08851091ac66de68fd`, superseding `vps-prism-autotrader-lab-rehearsal`) |
| Profile | `prism_autotrader_lab_v1` at `full` (frozen definition unchanged) |

Execution, risk, geometry, fee (4.5 bp), slippage, the 2x cost rule, 3 s quote freshness,
60 s entry wait and future-quote semantics are inherited byte-for-byte from the parent.
Sizing is the existing fixed 20 % current-equity notional, 2x isolated, max three positions,
60 % gross. No sizing cohort, adaptive sizing, event cohort or dynamic account exists.
Results apply to this core-six cohort only.

## Allowlist (16 of the released 28)

Evidence read before the freeze: Phase 22 frozen intraday study (2026-10-06 real run NULL),
and the Phase 24B prospective checkpoint `mcheck_f75db5b21762478bbd322870033a96fd`
(`microstructure_absorption_v1`, as of 2026-10-25, Research authority). No hypothesis has
graduated; REJECTED means net t <= -1 or an edge only before costs.

| Included | Status |
|---|---|
| Seven Phase 22 1h long probes (ema_cross_trend, ema_stretch_fade k=3/4, compression_breakout, expansion_continuation, expansion_fade, exhaustion) | INTERESTING (not candidates) |
| Six Phase 23 positioning (crowding_vulnerability, oi_price_stall, oi_directional_pressure; both sides) | NO_VERDICT |
| Phase 24B continuation_persistent long | NO_EVIDENCE (EARLY, 216 episodes) |
| Phase 24B absorption_book long/short | IMMATURE (WARMUP, 2 episodes) |

Excluded, preserved in the Lab: seven Phase 22 short controls (REJECTED), and Phase 24B
continuation_core long/short, continuation_persistent short, absorption_oi_rising long/short
(REJECTED, EARLY). Exact IDs, inputs, missing-input behaviour and execution mappings are in
the frozen definition (`market_signal.paper.nimble.core_spec`).

Hard dependencies: fresh executable bid/ask from an active collector session of the claimed
runtime, a predicted funding rate, full authority and the runtime lock. Hypothesis-declared
inputs (1h bars, hourly OI/positioning, complete 15m microstructure windows) defer or record
FEATURE_WARMUP for the affected candidate only. Context/news/macro are recorded enrichment,
never a veto. An official Hyperliquid major/critical outage blocks admission and entry/exit
fills (VENUE_UNAVAILABLE), reusing the event/dynamic guard; the parent baseline lacked it.

## Fill-starvation investigation

Historical (Railway snapshot `prism-20261012T124347Z-pre-deploy-8f9bc34`): all 15
EXECUTION_EXPIRED cancellations across the three runs were hourly intents at :04-:05 whose
next monitor ran 62-94 s later. Each run's tick took ~9-12 s (spool ingest, discovery and
source reads before monitoring, under the lock), three runs were serialized, and the
positioning/funding/micro jobs held the lock across :04-:06.

Deterministic reproduction (`tests/test_core_six_paper.py`): 20 s of pre-monitor work on a
50 s-old intent expires it under the old ordering; the fill-first pass fills it at 50 s. Lock
held for 40 s then released fills at 42 s on a fresh quote; lock held past 60 s expires with
no fill.

OVH replay (scratch DB copies, real worker code and CPU, live quotes mirrored, the measured
live lock occupancy of 22:00-22:10 replayed): with one account, the old code did **not**
starve (fills 8-23 s). The fix is therefore a timing improvement, not a rescue:

| Replayed intent | Old code | Fixed code |
|---|---|---|
| :04:40 (positioning/funding/micro holds) | 13.8 s | 21.8 s (poll alignment) |
| :05:00 (funding hold) | 8.5 s | 7.1 s |
| :01:40 (near-idle) | 23.2 s | 8.2 s |
| continuous 70 s hold | - | EXECUTION_EXPIRED, no fill |

Fix: the worker executes already-decided entry/exit intents first, on a fresh clock and
quote, before ingest/discovery. It decides nothing new (exits/invalidation/marks stay in the
full pass). No policy semantics changed.

## Authority, backup and retention

* Scratch transition test: claim without `--supersede` refused; supersede recorded; repeat is
  a no-op; full writer allowed and rehearsal writer refused after claim; foreign id refused;
  rehearsal cannot run the worker or create runs; rollback restores the opposite.
* The collector stamps its runtime id into `latest_books.json`; quotes are rejected unless it
  matches the claim, so the collector is restarted after the claim (one partial minute).
* Backups: inspection fingerprints every `paper_nimble_*` table; verify passes under the Lab
  profile; restore-test has zero differences. The Lab nightly had been refused by the
  two-copy guard since 2026-10-20; both copies were exported off-host, restore-tested and
  acknowledged, and governed retention released one before the deploy.
* Retention: the rehearsal manifest fails closed under the new authority (suppression off).
  A fresh manifest (`execution_off: false`) has the identical 28 holds apart from the
  authority field, including Microdir and Context timing protections. The superseded seal is
  kept at `/data/retention-phase1/protection.rehearsal-20261026.json`.

## Rollback

Stop `prism-nimble`; `market paper nimble kill --reason ...` drains any position on future
quotes; restore `/etc/prism/prism.env.rehearsal-20261026`; claim the rehearsal id with
`--supersede`; restart the collector, free-source ingest and scheduler. Evidence is kept.

## Daily review

`market paper nimble funnel [--start <UTC>] [--end <UTC>]` (read-only): evaluations,
data-deferred grid instants, signals, every rejection reason, intents and cancellations
(including EXECUTION_EXPIRED), fills, closes, provisional/final accounting, fees, funding,
PnL, latency, capital utilisation, worker heartbeat and per-asset input frontiers. The
existing daily brief records a summary at 00:14 even with no Telegram channel.

## Live commissioning (2026-10-26)

Inputs before activation: all six assets with valid executable quotes in 20/20 samples on the
open collector session; 1h bars, hourly OI and settled funding within one grid; complete
minutes within the 15-minute ingest cadence.

Through 10:08 UTC (first 55 minutes): worker active and heartbeating; 132 recorded causal
evaluations (Phase 22: 42, Phase 23: 36, Phase 24B: 54); one fired signal, rejected
`UNECONOMIC_TARGET`; 131 `NO_SIGNAL`; the 09:15 microstructure grid (18 candidates) deferred
because the collector restart left 09:12 partial. No intent, fill or position yet: genuine
signals have not cleared the unchanged economics. Worker restart resumed cleanly. Post-cutover
scheduled cycles ran under the new authority. Worker ticks ~9 s wall / ~10 s CPU per minute;
worker RSS ~175 MiB; host ~10 GB available.

Fill, exit, funding reconciliation, venue guard, draining and recovery paths are proven by the
focused lifecycle tests and the scratch OVH replay; they have not yet occurred live.

## Open operational items (not blockers for this cohort)

* Lab off-host export is manual. The next backup (tonight 01:30) succeeds; the following one
  is refused by the two-copy guard unless `deploy/vps/pull_backup.sh` and
  `market ops backup-retain --apply` run in between.
* Research's own Drive backup source is refused by the same guard on its separate database
  (observed 2026-10-26 07:28 onward); untouched by this tranche.
* Telegram is unconfigured; the daily brief records summary-only reports.
