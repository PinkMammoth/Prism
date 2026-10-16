# OVH DU cadence correction and ranking-only validation — 2026-10-16

**RANKING VALIDATION PASS WITH SPECIFIC LIMITATIONS**

Exact approved candidate `c6b6e9da7e0d2e04f265e5fec9d1dde9a0a7d67c` is deployed. Lab v2 remains **RANKING ONLY**.
Thirteen new genuine evaluations span **14:20:39–16:20:39 UTC, exactly7200s**; the preserved
morning observation is excluded. No new Lab soak was started. Warming is **NOT READY** and OFF.

## SSH/host visibility

Fresh predeployment checks14:09 UTC passed SSH, DB accessibility, clock/NTP, kernel,
firewall/listeners, failed units, resources, profile, collectors, scheduler and backups.
The earlier SSH timeouts remain unattributed. Continuous uptime, unchanged kernel/firewall
and core PID625 provide no evidence of a host failure; no SSH/firewall hardening was relaxed.
Final16:29 UTC check: kernel `6.1.0-53-cloud-amd64`, NTP synchronized, no failed units,
10.812GB available RAM, no swap, free disk94,467,260,416B.
All reviewed source hashes still match c6b6e9d. Lab authority claim remains
`vps-prism-autotrader-lab-rehearsal`, claimed `2026-10-14 15:43:52.816356+00:00`.

## Cadence defect, invariant and fix

Rejected eee205f launched from refresh start every600s, while both due guards compared against
previous actual commit-entry evidence time. With36s fetches: starts0/600/1200 produce evidence
36/NOT_DUE/1236; accepted observations are1200s apart and violate frozen900s continuity.

The fixed schedule uses deterministic UTC epoch multiples of600. Next deadlines derive from
nominal deadlines; elapsed slots are explicitly reported/skipped without overlap or catch-up.
Both due guards admit one actual CLI refresh-entry slot, versioned `utc_600s_slots_v1`.
Restart waits for the next boundary. A separate flock covers prepare/fetch/commit; the
sequential worker kills/waits on its300s child timeout. Actual delays still reset at901s.

Fixed wall-clock anchoring was selected over completion-aware nominal-deadline or equivalent
monotonic scheduling for deterministic restart alignment on this synchronized host. Fetch work
runs outside the runtime writer lock. The follow-up c6 handoff waits at most60s for that writer;
JSON mode keeps internal progress on stderr, and failed children expose only allowlisted
exception signatures plus stderr length/hash. Source definitions and scientific rules are unchanged.

Refresh entry, actual native observation times, fetch start/completion, commit-entry evidence
clock, ranking completion and completed DB transactions stay distinct. The900-second rule was
not changed or satisfied by relabeling timestamps. [Design](DYNAMIC_UNIVERSE_CADENCE_FIX.md).

The earlier b10 attempt/rollback is preserved in `attempt-1/`. Its original second-child exception
is unproven because stderr was discarded. Actual-CLI tests independently reproduced zero-wait
writer contention and JSON contamination; c6 fixes those paths. In this window a real
**4.001s writer handoff wait** succeeded, producing a604s interval followed by595s.

## Regression proof and exact release

- Candidate: `c6b6e9da7e0d2e04f265e5fec9d1dde9a0a7d67c`; rejected eee205f and prior b10 are unchanged historical revisions.
- Profile: `prism_autotrader_lab_v2`, rehearsal/data-only; digest `e7571ad1a2a35fb42dc8c1ea77759e31887a1c7679445561664c1dee68c12207`.
- Policy: `universepolicy_5f88b1dcdf20d7d19047edffab329076d7a63936f8c0916c21ffc83bcf4d56ff` (`hyperliquid_dynamic_universe_v1`).
- Release archive SHA256: `587daa0d5f8e251ca714a9af82578d73f496ee8d85b0768db8ccd9d7d6e01824`.
- Prepared activation script SHA256: `f12039a006378c459f01ed5dc6fa283d910e21df333464009a08a9a7c1ce7a94`.
- Cadence-focused suite23 passed, including36s reproduction, fast/boundary/overrun/899s work,
  restart/missed cycles, deterministic600s deadlines, no overlap, genuine900/901s resets,
  actual writer handoff, JSON-output contention and safe diagnostics.
- Full1809-case coverage: **1807 passed,1 skipped,1 failed**. Composite accounting is preserved.
  The sole unresolved failure is the frozen sizing source-provenance guard, also failing on
  approved main. The artifact/test remain unchanged and failed. It blocks future sizing/cohort
  activation until separately reconciled/versioned.
- The user explicitly approved exact c6b6e9d for this ranking stage, superseding b10d13a.
  This is a narrow deployment exception, not a green full suite or warming/sizing permission.
- DU/readiness/profile/authority/scheduler coverage passed; Ruff, formatting and diff checks passed.
  The local restic/rclone integration skip has separate live recovery evidence.
- New operational continuing-guard checks17 passed, separately from application-suite counts;
  prior17 observer stop cases and3 rollback configuration scenarios also passed.

Policy, engine, portability, ledger, rich collection, profile and prepared activation semantics
remain frozen; source input/fetch/enrichment functions retain approved definitions. Score/cohort,
promotion/demotion/dwell, seven observations,900s reset and all scientific readiness rules are intact.

## Activation and observation window

The standard no-restart deploy installed exact c6b6e9d, with a verified predeploy backup and
off-host146-fingerprint restore. Fresh budget re-attestation then passed with specific limitations.
Prepared activation completed **14:12:19.818775 UTC** using:

```text
sudo /opt/prism/current/.venv/bin/python /opt/prism/current/deploy/vps/activate_du_data.py ranking --budget /data/state/du-budget.json
```

Activation budget SHA256 `9bb2f480052a94eb26f050772adbaae2041dc2e4c93988beb650a535a0837618`.
Only the intended data-only configuration fields changed. The core collector was not restarted;
scheduler/free-source services were intentionally restarted by the prepared tool. No unexpected
service restart occurred in the window. Authority and protected secrets remained unchanged.

Observer start **2026-10-16T14:12:24.353427+00:00**; end **2026-10-16T16:21:20.171427+00:00**;
wall duration **7735.818s =2h08m55.818s**. The accepted-observation span is exactly2h.
All observations are genuine live fetches; no synthetic, backdated or manually triggered samples.

All times below are UTC. “Evidence” is actual commit entry, used by continuity; “Txn end” is the
completed ranking DB transaction. Full precision, separate refresh/fetch starts, outcomes commits,
API status/missingness and next deadlines are retained in `window-summary-attempt2.json` and samples.

| # | Evaluation start | Fetch done | Evidence | Rank done | Txn end | Next deadline | Accepted gap s | Candidates |
|---|---|---|---|---|---|---|---:|---:|
| 1 | 14:20:00.001 | 14:20:38.795 | 14:20:39.000 | 14:20:40.144 | 14:20:41.937 | 14:30:00.000 | pre-window16200 | 27 |
| 2 | 14:30:00.000 | 14:30:38.981 | 14:30:39.000 | 14:30:40.241 | 14:30:41.814 | 14:40:00.000 | 600 | 27 |
| 3 | 14:40:00.000 | 14:40:38.450 | 14:40:39.000 | 14:40:39.691 | 14:40:41.273 | 14:50:00.000 | 600 | 23 |
| 4 | 14:50:00.000 | 14:50:39.240 | 14:50:40.000 | 14:50:40.646 | 14:50:42.269 | 15:00:00.000 | 601 | 22 |
| 5 | 15:00:00.000 | 15:00:39.437 | 15:00:40.000 | 15:00:40.900 | 15:00:42.440 | 15:10:00.000 | 600 | 23 |
| 6 | 15:10:00.000 | 15:10:39.040 | 15:10:40.000 | 15:10:40.395 | 15:10:41.894 | 15:20:00.000 | 600 | 22 |
| 7 | 15:20:00.000 | 15:20:39.273 | 15:20:44.000 | 15:20:44.602 | 15:20:46.309 | 15:30:00.000 | 604 | 21 |
| 8 | 15:30:00.001 | 15:30:38.875 | 15:30:39.000 | 15:30:40.260 | 15:30:41.895 | 15:40:00.000 | 595 | 19 |
| 9 | 15:40:00.000 | 15:40:38.768 | 15:40:39.000 | 15:40:40.131 | 15:40:41.832 | 15:50:00.000 | 600 | 18 |
| 10 | 15:50:00.000 | 15:50:38.937 | 15:50:39.000 | 15:50:40.285 | 15:50:41.978 | 16:00:00.000 | 600 | 20 |
| 11 | 16:00:00.000 | 16:00:40.018 | 16:00:40.000 | 16:00:41.338 | 16:00:42.982 | 16:10:00.000 | 601 | 19 |
| 12 | 16:10:00.001 | 16:10:39.380 | 16:10:40.000 | 16:10:40.685 | 16:10:42.317 | 16:20:00.000 | 600 | 18 |
| 13 | 16:20:00.000 | 16:20:38.828 | 16:20:39.000 | 16:20:40.117 | 16:20:41.693 | 16:30:00.000 | 599 | 18 |

In-window gaps: `[600, 600, 601, 600, 600, 604, 595, 600, 600, 601, 600, 599]`; maximum **604s**.
Maximum completed-transaction gap604.415530s. Nominal starts/deadlines stayed600s apart;
no skipped/missed deadline, overlap, timeout, invalid JSON or failed refresh occurred.
The first16200s gap belongs to the stopped morning interval and remains a genuine scientific
continuity reset. That earlier single observation is preserved and contributes zero to this gate.

## Ranking correctness and data quality

Thirteen evaluations each discovered541 markets:340 active,201 inactive,178 active supported
native markets;307 total deployer/HIP-3 markets stay quarantined. Candidate counts:
`[27, 27, 23, 22, 23, 22, 21, 19, 18, 20, 19, 18, 18]`. Thirty markets received bounded books/candle measurements per cycle.
All-market metadata and selected cheap requests succeeded. Expected missing quotes/depth/frequency
outside that thirty-market set remain null. Movement and OI-change inputs remain null before
seven healthy observations, then populate from genuine retained history. Inactive zero OI keeps
OI-change missing. Frozen normalization of missing percentiles is distinct from raw-input zero filling.

Stored-input replay passes for every new evaluation: exact score/rank/components, candidate
states/changes/streaks, reversed-input determinism, pin-independent shared scores, native/deployer
classification, inactive exclusion, core-only subscription plans and absence of satellite rich/entry
state. The7-observation transition occurred prospectively at15:20; listing/readiness clocks were not waived.

Independent midrank/component/geometric arithmetic on the verified recovery copy:

| Asset | Manual score | Stored score | Exact match | Entry eligible |
|---|---:|---:|---|---|
| BTC | 0.287764399913 | 0.287764399913 | True | False |
| ETH | 0.411257008077 | 0.411257008077 | True | False |
| ZEC | 0.78121971899 | 0.78121971899 | True | False |
| AI | 0 | 0 | True | False |
| abcd:USA500 | 0 | 0 | True | False |

No execution-active state exists; all paper account/run/position/evaluation rows remain zero.

API: **936 requests/936 HTTP200**, no429/5xx, transport failures or source errors;
3,787,048 response-body bytes before protocol overhead. Each cycle:30 books,30 candles,
11 metadata batches,1 DEX discovery. Maximum per-cycle request p95 is0.548s. Source completeness
and null handling are preserved in the public ledger and missingness audit.

## Resource impact and ranking budget

Before activation: RAM available11.121GB, load approximately0.15/0.15/0.10, free disk94.5GB.
During the224 resource samples (maximum actual sample spacing60.011s):

| Signal | Measured |
|---|---:|
| Whole-host CPU busy | mean2.998%,p957.244%,max11.595% across six vCPUs |
| Steal | max0.0548% |
| Load1m /5m maximum | 1.103 /0.402 |
| Available RAM | minimum10,387,533,824B,84.75% of total; no swap/OOM |
| Ranking whole-CLI CPU | mean8.385s,max9.240s per600s; total109.008s |
| Radar cgroup CPU | 109.686s; approximately0.236% of whole six-core host time |
| Whole child RSS high-water | 449.414MiB, against1GiB worker cap |
| Worker cgroup charged-memory peak | 417,767,424B; a different accounting basis from RSS |
| Ranking transaction | mean0.196s,max0.230s |
| Outcomes transaction | max0.830s |
| Lab DB file | +5,767,168B |
| New compressed evaluation payloads | 931,638B |
| Prospective outcome payloads | 634rows,180,191B |
| Whole-host used-space growth | 23,511,040B, including bounded audit evidence |

The prior372MiB whole-CLI estimate was replaced with actual449MiB; it remains below the1GiB cap.
2CPU-s/min resource estimate covers the measured radar average; hard stop remains30CPU-s/min.
Averages are not instantaneous peak claims. Existing collectors, scheduler and the temporary
full-history audit contribute independent CPU/RAM. The audit used234.139 self CPU-s and323.55MiB
lifetime RSS; its accumulated proof files are one-off footprint, not production daily growth.

The continuing safety guard uses current safety assertions rather than replaying all history.
Its read-only probe took1.302s/1.504CPU-s, temporary RSS peak337.3MiB; live cgroup at16:29 was
103.686MB current,241.840MB peak. Its own cost is tracked separately; it makes no network calls
and stores bounded latest-status files. Ordinary ranking remains the only new scientific workload.

## Whole-host disk reconciliation

Snapshot16:29 UTC. Allocated bytes are inode-aware; nested parent totals must not be added again.
The detailed raw tree is in `final-host-safety-attempt2.json`.

| Component | Allocated bytes | Decimal MB |
|---|---:|---:|
| Lab DB | 155,729,920 | 155.730 |
| Frozen Research imported DB | 1,646,280,704 | 1646.281 |
| Lab backup tree | 156,176,384 | 156.176 |
| Research compressed generations, two independent proof/lifecycle copies | 271,314,944 | 271.315 |
| Microstructure raw | 93,741,056 | 93.741 |
| Microstructure spool | 46,911,488 | 46.911 |
| Microstructure large-print spool metadata | 61,440 | 0.061 |
| Provider raw | 10,420,224 | 10.420 |
| Free-source spool/receipts | 385,024 | 0.385 |
| Lab logs | 1,204,224 | 1.204 |
| Host journal | 79,859,712 | 79.860 |
| Other host logs | 1,757,184 | 1.757 |
| Lab releases, inode-aware incremental charge | 287,821,824 | 287.822 |
| Research tools, backup tools and Python installation | 514,908,160 | 514.908 |
| Lab/Research uv caches | 1,064,198,144 | 1064.198 |
| Lab validation evidence and Research proof/state | 53,043,200 | 53.043 |
| Temporary directories, no retained materialized restore DB | 430,080 | 0.430 |
| OS, remaining control files and filesystem accounting residual | 2,279,403,520 | 2279.404 |
| **Current used footprint** | **6,663,647,232** | **6663.647** |
| **Current available free** | **94,467,260,416** | **94467.260** |

Filesystem total105,467,994,112B; inaccessible/reserved/accounting difference4,337,086,464B is
excluded from available free. Local Research has two compressed proof/lifecycle copies; frozen
import, tools/caches, releases and duplicate proof artifacts are already charged to this footprint.
No persistent materialized restore DB remains. Off-host backups/evidence occupy the development
machine, not OVH disk. No evidence was deleted to obtain admission.

## Measured growth, temporary peaks and runway

Across the post-deploy resource interval7857.383s:
Lab DB file growth projects approximately63.416MB/day;
this is a short allocation interval, not a settled daily rate. The base model retains the51MB/day
prior short-interval upper sensitivity, compared with19–40MB/day in longer rehearsal/remediation
measurements. Current core micro rows are already inside that base DB figure.

Micro raw/spool net growth was82.513MB/day, versus62.71MB/day in the
base rehearsal. Use90MB/day now, without credit for future rotation. Provider/free-source physical
interval rate stays below6MB/day allowance. Lab logs project approximately
0.631MB/day; journal allocation did not increase in this window.
Research imported DB size/mtime remain unchanged, growth0. One-off bounded audit proof growth
is9.896MB and is excluded from persistent daily growth.

Modelled recurring components:

| Component | MB/day |
|---|---:|
| baseline_lab_db_upper | 51.000 |
| ranking_evaluation_ledger | 13.433 |
| ranking_outcomes_30_candidates_5_horizons | 12.442 |
| one_growing_lab_backup | 76.875 |
| existing_micro_raw_spool_no_rotation_credit | 90.000 |
| provider_free_sources | 6.000 |
| logs_journal_host_allowance | 37.000 |
| frozen_research_db | 0.000 |
| research_backup_metadata | 0.100 |
| **Sum** | **286.849** |
| **Rounded admission growth** | **290.000** |

Ranking ledger uses the measured84,405B maximum compressed payload plus key/index allowance,
144evaluations/day and10% margin. Outcomes use all30 possible candidates and all five horizons,
not just the horizons that matured in2h: measured maximum JSON296B plus key/index/margin,
rounded to576B/row. One growing full Lab backup amplifies DB growth once; the old eight-copy
model is not used. The four-hour outcome horizon remains immature, rather than fabricated.

```text
Current free after recovery proof            94,467,260,416 B
Unchanged70%-free safety floor                 73,827,595,879 B
Separate backup/restore working reserve       7,000,000,000 B
Retained-growth capacity                      13,639,664,537 B
Conservative persistent growth                  290,000,000 B/day
Current runway                                  47.033 days
Extra150MB/day recurrence stress                 30.999 days
```

The7GB reserve separately funds Research archive+materialized restore1.782GB, overlapping new
compressed generation135.6MB, Lab creation≥1.2× current DB, restore/cache overhead and margin.
Temporary nightly copies are not daily growth. Releases, DB bootstrap/free-block allocation and
proof copies are not daily growth. The historical145.6MB host step is still unattributed; it is
charged to current free and tested as a separate recurrence sensitivity, not asserted to recur.

DB allocation cross-check: predeploy recovery copy595 blocks/500 used/95 free; post-window
597 blocks/533 used/64 free,256KiB blocks, zero WAL. Reusable space/row-group allocation explains
why a short file-size delta is not an exact payload/day forecast. Frozen Research remains0growth.

**Ranking resource admission: PASS WITH SPECIFIC LIMITATIONS.** Runway warns below60days;
30-day stop,70%-free floor and7GB reserve remain intact. This is290MB/day derived from measured
components, not the old0.30GB/day warming-inclusive assumption. No warming cost is charged here.

Installed measured ranking receipt SHA256 `701eab3711310d0cf9a6a724e04ce8a62fd02df1bfd5768e61a7d2ff15fb3c9b`; rich-assets6, bound to c6b6e9d.
Its measurement is16:21:54 UTC and expires **2026-10-17 16:21:54 UTC**. A continuing guard stops
ranking and restores v1 if the receipt expires without governed refresh. Budget renewal retains
this approved ranking scope; warming still needs a separate12-market receipt and approval.

## Collector/scheduler impact

**26 recorded scheduler cycles** in-window, all successful; no delayed/failed writer,
lock failure, spool error, drops or free-source backlog. Core PID625 and all postactivation
service PIDs/restart counts stayed unchanged. All six DB/bar ingestion frontiers continued.

Closed-minute audit14:13–16:21 UTC:762COMPLETE,6PARTIAL,0missing of768expected records;
0late prints and0collector restarts. One upstream close at16:10:06.221 after10,000.4s,
reason1000/Expired, recovered in1.6s. Each core market retains one PARTIAL minute with disconnect/
connect flags. This matches prior venue-expiry behavior and is not attributed to resource load.
No partial data is relabeled COMPLETE or used to waive exact15-minute readiness/calibration gates.

FRED/macro schedule00:20,12:42,13:42,19:12 UTC: no new macro job was due inside this window.
The13:42 FRED calendar/actuals run succeeded and remains fresh; protected configuration is unchanged.
Free-source ingestion/status and ordinary scheduled data jobs remained healthy. No claim of a
live macro overlap is made.

## Backup interaction and Research isolation

No nightly backup naturally overlapped this daytime window; it was not extended overnight.
The ordinary predeploy Lab backup/restore succeeded before activation. A controlled post-window
Lab backup was created16:25:55, then pulled/restored off-host: all146 relation fingerprints,
checksums, schema31 and critical DB validation passed. The recovery copy contains the preserved
old observation plus all13 new observations and634outcomes. Normal governed retention removed
only the previously verified predeploy copy. No verification was disabled or repository recreated.

Post-window source: `/data/backups/manual/prism-20261016T162555Z-du-ranking-validation-c6b6e9d.duckdb`.
Uncompressed SHA256 `0a561fe6547e0737f3fbab9e53d0b174483e22165df4a3c6e6771eda2b0be90e`.
This controlled proof is additional durability evidence, not a claimed nightly overlap.

Research alert stays closed: abandoned pre-reboot shared restic lock, ordinary stale-lock unlock,
then unchanged unattended cycle08:08:30–08:13:18 UTC. Full `check --read-data`, Drive restore,
checksums/146fingerprints and safe retention passed. Machine key was exercised; custodian key
usability relies on the prior independent ceremony plus unchanged key/repository identities,
not a new password entry today. Its frozen import backup is not a live Railway replication claim.
Railway remains authoritative. No Research scheduler/Context/DNS/Auth0/Work authority was changed.

## Rollback readiness and continuing protection

Current clean window did not need rollback. The failed first attempt's automatic v1 recovery
and evidence are preserved; configuration tests prove DB/history and core-six preservation.
The continuing `prism-du-ranking-guard.service` is active/enabled, with a separate OnFailure
fallback,512MiB cap,25%CPU cap, protected paths and no Research secret/DB access. It rejects
warming, paper/sizing state, drift, failures, gaps/staleness, expired budgets and resource breaches.
Restart/reboot PID discontinuity fails closed to v1. It reuses the original window/baseline;
starting this safety process does not reset scientific clocks or start another soak.

Manual rollback path, only if needed:

```text
sudo systemctl stop prism-du-ranking-guard.service
sudo /opt/prism/current/.venv/bin/python /data/validation/du-ranking-cadence/attempt-2/tools/ranking_watch.py --root /data/validation/du-ranking-cadence/attempt-2/post-window-guard --rollback-only
```

It stops ranking first, checks core-only/no pins, waits for the writer, restores the saved v1
environment atomically, restarts only scheduler/free-source services, and preserves DB/candidate
history, core collection, rehearsal authority, Research and Railway. The saved original v1 env
SHA remains93116e83a9169e6ee8cb0cbcb5d7532766465c3569412ffce39540e0f5071439.
The ranking-only guard must be replaced under a separately approved controlled warming transition;
it correctly rejects attempts to change this live stage to warming.

## Updated warming budget — prepared only

**NOT READY.** Proposed max6satellites +6core =12rich markets/48subscriptions is modelled,
not activated or claimed as measured live12-market capacity. No assumption that all six slots
warm immediately. The proposal JSON remains `validated=false` and is never installed.

Disk: ranking290MB/day + rounded110MB/day richer workload =400MB/day;
nominal runway **34.099days**, extra150MB/day recurrence stress
**24.799days**. Extra micro DB2.212MB/day and its one backup,
up to90MB/day additional raw/spool without rotation credit, OI/funding/hypothesis evidence and
logs are explicit.100MB bounded initial backfill fits inside the existing7GB transient reserve.

Initial backfill≤45days/1080closed hourly bars per satellite,≤6480total; six satellites add at
most12initial REST requests, then at most288/day. Cheap ranking coverage remains bounded at30;
prospective hourly OI bookkeeping can include the selected non-core lightweight set, within
its metadata allowance. Funding history48h; OI remains prospective. Rich CPU uses the prior
6→12adapter increment, not a second full ranking worker. Wire/subscription scaling and large-print
calibration are explicit sensitivities in the proposal.

RAM conservative peak overlap: observed whole-CLI peak plus prior6→12adapter RSS increment,
radar parent, incremental collector, measured guardian charge, prior Research/backup-overlap low
and3GB future-paper reserve leaves **4.835GB**,
below the **4.903GB** reserve cross-check by
**67.847MB**. Steady guardian charge would fit more narrowly;
the peak/shared-page/time-phasing model needs validation. This is a future12-market sensitivity;
current ranking actual headroom is healthy. No reserve is weakened to force a warming pass.

Before requesting activation: validate lower-peak monitoring and/or prove backup/rich-work phasing,
then issue a fresh max12 resource receipt and warming-aware rollback/guard. This is a bounded
resource gate, not another24h Lab soak. The code exception also remains ranking-only.

All scientific gates remain: seven healthy observations≤900s apart; seven-day listing age;
exact technical/backfill readiness; two prospective hourly OI captures within140minutes;
funding≤80minutes;288prior COMPLETE15m windows plus current; prior7UTCday large-print
calibration with≥3qualifying days each having720non-gap minutes and1000prints in total; promotion3/demotion6,
6h dwell and reentry segment reset. Ranking starts permitted clocks; it waives none.

## Safety and exact next action

Paper execution OFF; paper account creation OFF; sizing cohort OFF; real-money access/actions OFF;
warming and satellite rich subscriptions OFF. All paper rows are zero, no cohort state exists,
secrets/config hashes stayed fixed, and host evidence secret scan found zero matches.
Railway Research authority is unchanged. No Railway execution restart or Research handoff occurred.

Keep approved ranking protected and refresh its measured resource receipt before17Oct16:21:54 UTC
if it remains running. Prepare/prove the max12 RAM/backup-phasing gate without enabling warming.
Only after that passes, the exact next approval is **data-only warming for up to six satellites,
max12rich markets/48subscriptions, under a fresh12-market budget and reviewed warming guard,
with paper/accounts/sizing/real-money/Research handoff OFF and every frozen readiness gate intact**.
That approval must name the reviewed warming revision and explicitly address its software gate;
the c6b6e9d ranking-only exception does not extend automatically. Sizing provenance remains a
separate activation blocker and is not modified here.

## Evidence index

- [Completion facts](evidence/ovh-du-ranking/20261016-cadence/completion-verdict-attempt2.json)
- [Every actual evaluation and measured resource statistic](evidence/ovh-du-ranking/20261016-cadence/window-summary-attempt2.json)
- [Final host/source/guard/safety proof](evidence/ovh-du-ranking/20261016-cadence/final-host-safety-attempt2.json)
- [Whole-host growth and runway model](evidence/ovh-du-ranking/20261016-cadence/resource-reconciliation-attempt2.json)
- [Installed measured ranking budget](evidence/ovh-du-ranking/20261016-cadence/ranking-budget-measured-attempt2.json)
- [Prepared warming budget, not admitted](evidence/ovh-du-ranking/20261016-cadence/warming-budget-preparation-attempt2.json)
- [Core-minute audit](evidence/ovh-du-ranking/20261016-cadence/core-quality-window-attempt2.json)
- [Recovery evidence and manual score proof](evidence/ovh-du-ranking/20261016-cadence/recovery-evidence-proof-attempt2.json)
- [Application validation accounting](evidence/ovh-du-ranking/20261016-cadence/validation-summary-attempt2.json)
- [Exact candidate approval](evidence/ovh-du-ranking/20261016-cadence/approval-c6b6e9d.json)
