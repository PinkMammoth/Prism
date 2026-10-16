# OVH autotrader rehearsal closure and kernel recovery

> Subsequent user direction preserves this 26-hour base as valid infrastructure evidence.
> The original recommendation to restart a 24-hour clock is superseded by the
> [targeted 6–12-hour remediation extension](OVH_LAB_TARGETED_REMEDIATION.md).
> This historical window and its two recorded defects remain unchanged.
> The linked extension is now closed and contains the updated verdict and both proven fixes.

Report date: 2026-10-15 UTC. Host: `vps-778d603b.vps.ovh.net` / `178.33.95.139`.
Lab release remains `530ba0d18c3009352eee21bb7f1a1c01004ab73a`.
Evidence: [20261015](evidence/ovh-autotrader-rehearsal/20261015/).
No application release, package, firewall, SSH, Railway, or trading configuration was changed.

## 24h rehearsal summary

The formal window is **2026-10-14 15:44:38 → 2026-10-15 17:45:00 UTC**, **26 h 00 m 22 s**.
The uninterrupted first 24 hours are included. Closure does not mean the data services were
stopped permanently: they continue in the same rehearsal after the maintenance reboot.

The 312 five-minute samples cover 15:47:09 on October 14 through 17:44:26 on October 15.
Maximum interval: **300.732 s**, no interval over 330 s. The initial 151.8 s and final 33.2 s
lack five-minute resource samples; service start times, journals, DB cycles and spool evidence
cover continuity there. Resource extrema below are sampled extrema, not subsecond limits.
The sampler's microstructure heartbeat-age field is null because it does not recognize this
status format; collector continuity is independently established from the spool and live status.

| Scheduled job | OK | Failed | Maximum cycle duration |
|---|---:|---:|---:|
| intraday | 104 | 0 | 12.21 s |
| microstructure | 104 | 0 | 5.62 s |
| context_news | 52 | 0 | 5.57 s |
| positioning | 26 | 0 | 16.23 s |
| paper_v2_funding (data feed) | 26 | 0 | 6.20 s |
| oi | 4 | 0 | 6.88 s |
| context_macro | 0 | 4 | 5.08 s |
| backup | 0 | 1 | 2.87 s |

**321 cycles: 316 OK, five failed.** All scheduled job slots in this window are accounted for.
The independent free-source watchdog completed **1,560 successful minute checks**.
Microstructure, free-source collector, free-source ingest and scheduler stayed active in all
312 samples, with unchanged process start timestamps and **zero automatic restarts**. Nimble,
DU radar and gateway services remained inactive. There was no earlier host reboot in this window.

**Expected scheduled activity.** Four-times-hourly bars and microstructure ingestion, hourly
funding/positioning, six-hourly OI and scheduled Context jobs ran at the frozen v1 cadences.
Five alert-suppression messages correctly fenced external notifications; they do not excuse the
underlying failed jobs. Some sources are intentionally STAGED or DISABLED: only Hyperliquid,
Coinbase and Kraken status sources were actively polled by the free-source collector. These
returned healthy 200/304 statuses after recovery, with pending spool zero. All 71 checked
free-source JSON/JSONL files, including receipts/seals/done records, parse cleanly.

**Freshness and explicit gaps.** At closure, all six Hyperliquid markets had the bars expected
from completed scheduled fetches: **103/103 fifteen-minute bars and 26/26 hourly bars per coin**,
zero missing or duplicate keys. Latest known closes were 17:30 (15m) and 17:00 (1h); the 17:45
close was fetched at 17:46, after closure. Hourly OI and positioning reached the 17:00 grid,
captured at 17:04; settled Hyperliquid funding reached 17:00. Six-hour Binance OI reached
12:00, collected at 14:30. DB microstructure reached 17:34 via the 17:36 ingestion; collector
spool continued beyond that DB frontier as expected between scheduled ingests.

Legacy seeded Binance bars remain at October 5, and Binance funding at October 12. They are
historical rows, not evidence of fresh active Binance bar/funding acquisition; no combined
provider maximum is used to conceal them. Hyperliquid is the active native bar/funding feed.
Macro freshness is **not proven**, because the FRED jobs failed. Seeded Work events are historical;
no Work ingress or Research/Context authority was enabled on the Lab.

For the exact full-minute interval **15:45 October 14 → 17:45 October 15**, the spool has
**9,295 COMPLETE + 65 PARTIAL / 9,360 expected coin-minutes**, **zero missing**, zero late
prints and zero process restarts. This excludes the six explicitly partial startup rows at
15:44. Every partial minute is enumerated below; none is treated as complete:

| UTC minute | Affected markets | Cause |
|---|---|---|
| Oct 14 18:37, 21:33 | all six | scheduled venue Expired close/reconnect |
| Oct 15 00:26 | all six | scheduled venue Expired close/reconnect |
| Oct 15 01:13, 02:15, 03:53 | all six | abrupt websocket close/reconnect |
| Oct 15 03:54 | BTC, ETH, HYPE, LINK, SOL | connection boundary |
| Oct 15 06:55, 09:49, 12:39, 15:31 | all six | scheduled venue Expired close/reconnect |

**Harmless transient warnings.** Seven normal Expired closes and three abrupt closes recovered
in 1.3–1.6 s each, **15.0 s total**. The abrupt closes are retained as explicit network events;
there is no missing minute, persistent unhealthy feed or failed microstructure cycle. **19/312
instantaneous read-only sampler opens were busy**, all attributed to the legitimate free-source
DB writer. No scheduled cycle failed/timed out on a DB lock, and no unresolved/stale lock was
found. A busy read-only observer sample is not a lost ingestion cycle.

**Material operational faults.**

1. **Macro acquisition failed four times**, at Oct 14 19:12 and Oct 15 00:20, 12:42 and 13:42:
   both `fred_calendar` and `fred_actuals` reported **`FRED_API_KEY not set`**. This is unresolved.
2. **The Oct 15 01:30 Lab backup job failed** with **`no run metadata`**. The DB was copied and
   `verify_backup` accepted its Lab shape, but generic lifecycle registration still requires a
   paper run. No source manifest or `backup_verified` success event was committed. Manual restore
   success below does **not** make that nightly job successful. This is unresolved; another
   failed copy can also consume the two-copy guard's remaining slot.
3. At **09:33:30**, the kernel killed PID 78342 in the separate UID 994 Research receiver's
   capped `/user.slice/user-994.slice/session-93.scope`. This was a **real, contained cgroup OOM**,
   not global RAM exhaustion or a Lab OOM. Existing independent evidence records its receiver
   cap correction from 1 GiB to 3 GiB and subsequent successful backup validation:
   [receiver-budget-retry](evidence/full-ovh-consolidation/20261015/receiver-budget-retry.json).
   No UID 999 Lab process restarted. It remains disclosed as a resolved host incident.

Kernel errors otherwise: none in the captured rehearsal error log. System error logs also
contain rejected SSH probes, malformed protocol attempts and MaxAuthTries refusals; these are
unsuccessful inbound scans. No failed Prism systemd units or unexplained unit starts/stops
occurred before the planned maintenance. No journald corruption was found in captured output.

| Resource | Measured result |
|---|---|
| Host CPU | 2.30% mean across 5-minute intervals; p95 3.21%; busiest interval 16.78% of six cores |
| CPU steal | maximum 5-minute interval 0.030% |
| RAM available | minimum **9,210,007,552 B / 8.58 GiB**, 75.1% of total |
| Load maxima, 1/5/15m | **1.389 / 0.974 / 0.729** on six vCPUs |
| Swap | no swap configured; zero used |
| Sampled cgroup memory peaks | collector 221 MiB; free collector 110 MiB; ingest 259 MiB; scheduler including children 549 MiB |
| Process lifetime RSS high-water before maintenance | collector 225.5 MiB; free collector 189.8 MiB; ingest 323.9 MiB; supercronic 17.2 MiB; sampler 254.7 MiB |
| Disk free | 98.152 GB first sample → 94.765 GB last; minimum 91.934 GB |
| DB file | 101,199,872 → 128,200,704 B; **+27,000,832 B**; sampled maximum 131,084,288 B |
| WAL | final zero; sampled maximum 48,902 B |
| Microstructure spool/raw/LP tree | **+67,950,515 B**, approximately 62.8 MB/day |
| Provider raw outside micro tree | **+1,695,193 B** |
| Free-source tree | **+90,741 B** |
| Local Lab backup tree | **+115,879,936 B**, the unregistered nightly copy |
| Lab logs | **+626,850 B** |

Disk consumed **3.387 GB** overall, while sampled Lab category deltas sum to approximately
214 MB. The rest must not be labelled Lab/DU steady growth: independent Research receiver,
namespace, backup and scratch work ran on this host during the window. The telemetry retains
all large steps, notably +1.79 GB at 09:33, removal near 11:48, several 1.65–2.22 GB copies at
12:23–12:38, -3.29 GB at 12:58, and backup restore growth/removal around 17:19–17:24. The
Research evidence documents these copies and scratch cleanup. Positive and negative steps
are preserved in the source series; no daily extrapolation of total filesystem net change is used.

Sources: [summary](evidence/ovh-autotrader-rehearsal/20261015/rehearsal-summary.json),
[all cycles](evidence/ovh-autotrader-rehearsal/20261015/db-before.json),
[collector intervals](evidence/ovh-autotrader-rehearsal/20261015/spool-rehearsal.json),
[per-asset frontiers](evidence/ovh-autotrader-rehearsal/20261015/frontier-proof.json),
[bar continuity](evidence/ovh-autotrader-rehearsal/20261015/bar-continuity.json),
[full Lab journal](evidence/ovh-autotrader-rehearsal/20261015/lab-journal-before.jsonl.gz).

## Lab backup/restore proof

This proof uses **the Lab's `/data/backups` snapshot**, independently of Research, Google Drive
and `/var/lib/prism-research`. No Research backup was substituted.

- Latest Lab artifact: `/data/backups/daily/prism-20261015T013001Z.duckdb`, **115,879,936 B**, schema 31.
- SHA-256: `28d26cdd48ff356eeba840c30f5a1a5eefc538ccaa20c969a316457061442d5a`.
- The source checksum was recorded over SSH; the independent download matched it exactly.
  There is **no pre-existing source manifest**. This proves source/download/restore consistency,
  not a successful original registration or comparison against a nonexistent historical checksum.
- Restored off-host to fresh `/tmp/ovh-lab-restore-00xi9ybp/restored.duckdb` with the exact
  deployed source revision. Writable scratch open succeeds; **all 146 table row counts and
  whole-row fingerprints match**, including critical bars, funding, OI, Context, microstructure,
  policies, runtime and authority metadata. Built-in verify and restore-test also succeed.
- Recorded source frontiers include bars through open 01:00, microstructure minute 01:20,
  Context first-seen 23:17:53 on Oct 14, and the in-progress 01:30:01 backup cycle. Other
  funding/positioning capture frontiers are recorded in the JSON proof. A copied running
  backup-cycle row is snapshot history, not a live worker in the restore.
- Historical rehearsal claim is preserved; scratch role is explicit, with authority `none`,
  execution/external actions false. Removing the scratch role makes writable open refuse.
  No new claim, activation event, account or worker is created in scratch.
- A gzip archive was retained off-host at
  `data/backup-archive/vps/prism-20261015T013001Z.duckdb.gz` (**30,648,696 B**). Decompression
  matches the source SHA. Its separate receipt records hashes and frontiers. No source backup
  was deleted, acknowledged or registered by this audit.

The **physical restore gate succeeds**, so the approved reboot was permitted. The **automated
Lab backup lifecycle gate fails** and remains an activation blocker. The standard pull helper
cannot honestly be called successful while its required source manifest is absent.

Proof: [restore](evidence/ovh-autotrader-rehearsal/20261015/lab-backup-restore-proof.json),
[archive receipt](evidence/ovh-autotrader-rehearsal/20261015/lab-offhost-archive-receipt.json),
[nightly failure](evidence/ovh-autotrader-rehearsal/20261015/backup-nightly-journal.txt).

## Kernel reboot result

**One reboot performed**, at **17:52:41.822 UTC**. Kernel changed from
`6.1.0-51-cloud-amd64` to intended GRUB default **`6.1.0-53-cloud-amd64`**.
Boot ID changed from `e97ff1cb-03db-4686-9f93-52bd604db908` to
`2e218007-141d-4370-9bbe-3a4c6cc998c5`. No package or unrelated host changes accompanied it.

Before reboot, PIDs/runtime, kernel images/default boot entry, firewall, SSH, network, clock,
DB authority and all cycle states were captured. Scheduler and free-source ingest stopped
cleanly at **17:51:58.873**, using their graceful systemd stop behavior. There were no running
DB cycles, no process with the DB open, the runtime flock could be acquired, and WAL was empty.
Collectors continued until normal reboot shutdown. Research backup service was inactive.
No paper runs/accounts/trades or Lab activation existed; only rehearsal write authority existed.

## Recovery validation

| Check | Result |
|---|---|
| Automatic services | collectors, free-source ingest and scheduler started at **17:52:53**, about 11.2 s after request |
| Collector connection | **17:52:56.787**; **14.424 s** from last connection close |
| SSH recovery | observed by **17:53:09.315**, upper bound **27.493 s** after request |
| Clock | chrony Normal, `NTPSynchronized=yes`, UTC; approximately 0.207 ms last offset |
| Host firewall and SSH | rules, effective `sshd -T`, and config hashes identical; `sshd -t` succeeds; root/password/keyboard-interactive login remain off |
| Networking | IPv4/IPv6 addresses, routes and listener endpoint set identical |
| Listeners | SSH plus pre-existing resolver DNS/LLMNR, DHCP and local chrony; no application/DB ingress added; host firewall still drops non-SSH unsolicited inbound |
| DB identity | unchanged schema 31 and original rehearsal claim; preflight `ready_for_jobs=true` |
| Services/workers | one of each expected worker and supercronic; no old PIDs, duplicate services or orphaned workers; Nimble/DU/gateways inactive |
| Locks | collector flock holders match current processes; DB opens and writer operations complete; no stale runtime/DB lock |
| Spool/cursors | all JSON lines valid, every cursor inside its file on a newline, no repaired bytes, no lost or duplicate same-revision records |
| Ingestion | every spool key through the ingested frontier exists in DB, matching revision/status; zero duplicate coin/minute keys |
| Data catch-up | explicitly labelled `reboot-recovery` microstructure and intraday cycles OK; immediate repeat ingest adds no minutes |
| Restart minute | **17:52 PARTIAL for all six**, then COMPLETE from **17:53**; no missing minute keys and no manufactured complete downtime |
| Scheduler | same generated crontab, same role and profile; minute watchdog resumed; no scheduled data-job slot fell inside the writer-drain interval |
| Sampler | original validation sampler is static, so was resumed explicitly after reboot; active, no restart loop |
| Kernel/systemd | final host running, zero failed units, no post-boot kernel errors |

First microstructure health proof was **17:54:06**, 84.2 s after reboot request. Complete
post-reboot 17:53–17:55 minutes for all six were ingested by **17:56:08** (206.2 s after request);
final repeated health cycle finished 17:56:11. These include deliberate observation/finalization
and manual catch-up time, not 206 s of collector downtime. One minute watchdog check at 17:52
was intentionally inside maintenance; it is not an unreported data-job loss.

**Edge limitation:** no OVH control-plane API capability was available to independently reread
Edge Network Firewall settings. This task made no edge changes. SSH and public-feed egress work,
and host networking/security compare exactly, but the earlier documented edge rules were not
independently re-attested here. No provider snapshot restore was performed; that remains a
separate prerequisite for any later paper-authority stage.

[Recovery comparison](evidence/ovh-autotrader-rehearsal/20261015/recovery-summary.json),
[final host](evidence/ovh-autotrader-rehearsal/20261015/host-final.json),
[DB/spool proof](evidence/ovh-autotrader-rehearsal/20261015/ingest-recovery-final.json),
[final data checks](evidence/ovh-autotrader-rehearsal/20261015/recovery-final-data-proof.txt).

## Rehearsal verdict

**REHEARSAL FAIL**

Duration, independent physical Lab restore, fencing and host/application reboot recovery were
proven. **Four failed macro cycles and the failed nightly backup registration remain material,
unresolved defects.** A limitation verdict cannot waive them. The independent Research backup
success does not cure either Lab defect. DU ranking/warming is blocked.

## Dynamic Universe activation plan

This is a **conditional specification only**, not an approved/runnable activation package.
Installed `prism_autotrader_lab_v1` correctly rejects `PRISM_UNIVERSE_STAGE=ranking|warming`.
Do not unset the explicit profile to obtain legacy opt-in behavior.

After remediation and a new passing rehearsal, prepare a separately reviewed
**`prism_autotrader_lab_v2`**, proposed explicit selector **`autotrader_lab_v2`**, without modifying
v1's frozen digest. Preserve `PRISM_AUTHORITY=rehearsal`, deployment target `vps`, role
`authoritative`, runtime ID **`vps-prism-autotrader-lab-rehearsal`**, `/data/prism.duckdb`, and all
current data jobs. The v2 preparation must explicitly permit the DU data worker and collector
desired-set mode while keeping execution/account creation/full authority/sizing inaccessible.
No authority reclaim, account creation or `record_activation` belongs to this step.

Frozen DU policy: **`hyperliquid_dynamic_universe_v1`**, identity
`universepolicy_5f88b1dcdf20d7d19047edffab329076d7a63936f8c0916c21ffc83bcf4d56ff`.
No readiness, scoring, portability, cost or lifecycle constants change.

- Start the existing gated **`prism-universe.service`**: ten-minute discovery/ranking,
  readiness, append-only decision/outcome ledger and atomic desired-subscription plan.
- Existing **`prism-microstructure.service`** switches to desired-set collection only for
  a separately approved warming stage. No new cron job, scheduler or DB service is added.
- Ranking cadence **600 s**; initial rich REST backfill once per satellite, then hourly
  bounded two-hour overlaps and funding refresh. OI captures remain prospective/hourly.
  WS collection is continuous; desired membership is reconsidered every radar evaluation.
- **30 light candidates**, **six satellites sharing the warming/active pool**, six permanent
  core assets: normally **12 rich assets**. Four subscriptions per rich asset, normally **48**.
  Core/pins/active obligations can override bounded light requests; risk pins override rich caps
  but never grant entries. Any such unexpected excess requires resource review.
- No HIP-3 execution or invented Binance corroboration. No exchange order/account credential is
  needed. The worker remains a serialized writer of this Lab DB, never Railway Research.

After implementation, passing gates and separate authorization, the required v2 runtime settings
would be:

```dotenv
PRISM_PROFILE=autotrader_lab_v2
PRISM_AUTHORITY=rehearsal
PRISM_DEPLOYMENT_TARGET=vps
PRISM_RUNTIME_ROLE=authoritative
PRISM_RUNTIME_ID=vps-prism-autotrader-lab-rehearsal
PRISM_NIMBLE_RUNTIME=off
PRISM_UNIVERSE_STAGE=ranking
PRISM_UNIVERSE_BUDGET=/data/state/du-budget.json
```

`warming` replaces `ranking` only after ranking's runtime/resource evidence is accepted.
The budget must match the frozen policy, be measured within **24 h**, account for DB/raw/spool/
backups, fund **at least 30 days** of physical runway at 12 rich assets, and demonstrate DU worker
CPU **below 30 CPU-s/min**. Refresh it from measurements as it ages; never mark this provisional
estimate validated. The release/profile code and digest do not exist yet, so **there is no valid
activation command on the current release**. These settings alone currently fail closed.

Stop on a backup failure, unresolved ingestion/DB fault, authority mismatch, corrupt plan/cursor,
profile drift, unexpected execution/account row or any resource stop threshold below. Preserve
pins/evidence during stops. Enabling ranking/warming grants **no paper execution, sizing cohort
or real-money access**.

## Resource budget

Final host measurement at **17:57:32 UTC**: **11,415,609,344 B RAM available**, total
12,256,567,296 B; **94,910,136,320 B disk free**, total 105,467,994,112 B; load **0.20/0.17/0.08**;
zero swap. Rehearsal peak RAM use still left 8.58 GiB available while independent Research
recovery work ran. There is substantial apparent headroom; this is not permission to activate.

Prior measured DU evidence, not a new live 12-market measurement:

| Workload | Prior measurement / conservative planning allowance |
|---|---|
| Base Lab | observed service/RSS/CPU peaks above; DB net approximately 25 MB/day, includes initial catch-up |
| Discovery/light requests | 72 requests, 288,851 response bytes, 36.1 s wall / 0.106 CPU-s per snapshot; ~41.6 MB/day response payload at ten-minute cadence, excluding protocol overhead |
| Ranking/readiness ledger | pure rank 13–15 ms; persisted 30-candidate cycle 0.804 CPU-s / 196 MiB; mature synthetic ledger 2.49 CPU-s / 362 MiB; allow 1 GiB worker cgroup |
| Rich warming / adapter cost | measured synthetic 12-market evaluation 1.30–1.50 CPU-s / ~641 MiB vs six-market ~0.8 CPU-s / ~349 MiB; execution worker remains OFF; reserve 1 GiB transient per data worker |
| Rich collector | live six-market baseline ~174 MiB RSS, 0.53% of one CPU, 22.37 messages/s, 11,744 B/s; reserve 0.5 GiB for 12; satellite traffic extrapolation is unmeasured |
| Network sensitivity | doubling baseline gives ~23.5 KB/s / 2.03 GB/day WS total, roughly +1.01 GB/day; active satellite tapes can exceed the linear estimate |
| DU storage sensitivity | 20.1 MB/day permanent DB at 12 rich markets, 78.7 MB/day raw archive, 30.3 MB/day spool, ~430 MB retained raw/spool reserve |
| Backups | one retained Lab recovery generation plus one transient copy; reserve ≥1.2× live Lab DB for each creation; retain the independent Research backup service's measured ~2 GB working-memory requirement and at least 6 GB shared-disk transient reserve |

Evidence: [DU v1 measurements](DYNAMIC_HYPERLIQUID_UNIVERSE_V1.md), especially
[benchmark](evidence/dynamic-universe-v1/benchmark.json),
[ledger cost](evidence/dynamic-universe-v1/ledger-history-cost.json) and
[Research restore resources](OVH_RESEARCH_NAMESPACE_VALIDATION.md).

A deliberately additive planning model uses **25 MB/day base DB + 20.1 MB/day DU DB**, even
though some micro rows overlap. Live DB plus one growing backup then contributes ~90 MB/day.
Add ~64 MB/day measured base spool/raw, another ~109 MB/day DU spool/raw sensitivity, and
~37 MB/day logs/overhead: **round up to 0.30 GB/day** retained growth without relying on raw
pruning. Reserve **7 GB** for Research/Lab backup and DU bootstrap transients. Above a 70%-free
filesystem floor this leaves approximately **14.1 GB / 47 days** at current measurements.
Absolute-free-byte runway is much longer, but is not the admission number.

This is **provisional**: 12-market live traffic/RSS is unmeasured; initial bar backfill is a
burst; Research mirror/source growth must be remeasured; current Lab backup retention is broken.
Do not rely on hypothetical pruning/rotation savings. Recompute total-host growth, backup
transient overlap and measured DU CPU/RSS before validating an admission budget. Serialize
heavy scratch restores with initial warming or stop when shared thresholds are approached.

| Resource | Warning | Stop new expansion / stop data worker safely |
|---|---|---|
| RAM | available <50% (~6.13 GB) for 5 min, or any DU unit >75% of its cgroup cap | available <40% (~4.90 GB) for 5 min; any OOM or cgroup kill immediately; any unexpected swap pressure |
| Disk | free <75% of total or conservative runway <60 days | free <70%, runway <30 days, or inability to fund next backup transient; backup/checksum failure immediately |
| CPU/load | total busy >50% for 5 min, 5m load >3, or steal >2% sustained | busy >80% for 10 min, 5m load >6 for 5 min, steal >5% for 5 min, or DU worker ≥30 CPU-s/min |
| DB/total growth | settled 24h DB growth >67.5 MB/day or total retained >0.30 GB/day | DB >90 MB/day for a settled 24h period, total >0.45 GB/day, or recomputed runway <30 days; any corruption/lock timeout immediately |
| WS/subscriptions | >3 abrupt disconnects/hour, any missed subscription ack for 60 s, or live feed gaps >10 s | core stale/unhealthy >60 s, unresolved satellite subscription >120 s, >6 abrupt closes/hour, reconnect loop, or unplanned cap breach without a justified pin |

Startup/bootstrap bursts must be recorded separately and reserve-funded; they do not authorize
ignoring a stop threshold. Satellite warming stops/pauses when these fire, while mandatory core
and pin collection continues wherever healthy. Never kill required evidence to make RAM look low.

## Readiness gates

Frozen DU v1 rules remain unchanged:

- **Light observation:** seven healthy ten-minute observations, no adjacent gap >900 s.
  Separate seven-day first-prospective-sighting age gate. Warming may run during that age gate;
  it never backdates listing knowledge or eligibility.
- **Technical/backfill:** at most 45 days native hourly bars initially, then two-hour overlap;
  exact `technical_ready` primitives, previous/current indicators and fresh latest closed hour.
  Initialization retries after failure. Backfill knowledge begins at actual ingestion.
- **Positioning:** at least two prospective hourly OI captures within 140 min. No retrospective
  OI fabrication. Binance-corroborated and unvalidated positioning families retain their frozen
  CORE_ONLY / NOT_YET_VALIDATED restrictions.
- **Funding:** genuinely observed settled funding, available by the decision clock, age ≤80 min.
  Backfill retains actual observed availability.
- **Microstructure:** **288 prior complete 15-minute windows**, excluding the current window,
  **plus a complete current window** and exact quality/predicate checks. This is at least
  72 hours of prior complete windows; gaps and alignment can extend elapsed time. Satellite
  history must belong to its current rich collection segment.
- **Large-print calibration:** exact prior-seven-UTC-day rule; ≥3 qualifying days, each with
  ≥720 non-gap minutes, and ≥1,000 prints total. Required null features remain unavailable.
- **Satellite reentry:** preserve old rows, record subscription-paused GAPs and reset segment
  warmup. Old rich segments cannot satisfy current-segment readiness. Reentry's first-hour
  alarm suppression does not grant feature eligibility.
- **Promotion/demotion persistence:** satellite rank ≤6 and score ≥0.35 on three consecutive
  evaluations, all hard/economic/readiness gates and capacity; >900 s evaluation gaps reset
  streaks. Rank >10/missing or score <0.35 for six consecutive evaluations and minimum six-hour
  Tier 2 dwell permits rank demotion. Hard safety failures bypass dwell; pins defer removal and
  grant no entries. Persist causal transitions and original promotion clocks.
- **Other frozen gates:** native supported active perp, 24h volume ≥$5m, OI ≥$2m, spread ≤15 bps,
  weaker-side 10-bps depth ≥$25k, ≥2 trades/min, source age ≤180 s, one-hour RV ≥3× conservative
  round-trip cost. Radar readiness never overrides the exact adapter's current eligibility.

No observation, history, calibration, persistence or warmup window was shortened. There is no
promise of trades at the end of a fixed timer, and no paper stage is authorized here.

## Rollback plan

**Current state is already the rollback destination:** v1 rehearsal, core-six collection, DU
inactive, execution/sizing off. The only persistent operational change from this task is loading
the already-installed kernel; no application rollback is required.

For a future authorized v2 ranking/warming rollout, the reviewed activation package must provide
this procedure before it starts:

1. Stop `prism-universe.service` to stop ranking/warming refresh and new membership expansion.
   Keep Nimble OFF and authority at rehearsal. Stop means stop new data expansion, not delete DBs.
2. Inspect all-run pending/open/accounting/research/event pins. Retain the current desired plan
   and collector while any satellite obligation remains; do not remove a plan to force core-only.
3. After obligations resolve, publish an **atomic, causally timestamped core-only plan**, retaining
   policy/runtime identity and a recorded rollback reason. Keep dynamic collector processing on
   until desired removals and final-minute/late revisions drain; record subscription removals.
4. Restore the saved v1 environment/profile, stage OFF and Nimble OFF. The existing collector
   intentionally honors an existing valid plan on restart even after its opt-in flag is removed,
   so stage OFF alone is insufficient: verify the persisted plan is core-only first.
5. Restart only the affected Lab components, and verify the original v1 digest, rehearsal claim,
   six core subscriptions, successful ingestion and healthy cursors. Preserve universe ledger,
   warmup rows, outcomes, raw/spool files and backup evidence. Do not restore an older DB to
   undo a profile change.

This is a documented procedure, not an implemented one-command helper. No rollback command was
executed against an unactivated DU. It never touches Railway services/variables, Research roots,
Drive backups or old Railway Nimble. Any unresolved pin or authority mismatch stops rollback
at the evidence-preserving stage.

## Safety confirmation

- **No paper trading started.** No Nimble worker, paper account/run creation, full-authority claim
  or Lab experiment activation. `paper_nimble_triggers` is a data outbox, not a trading account.
- **No sizing cohort activated or registered.** Existing policy definitions remain intact.
- **No Railway Research/Context authority changed.** No Railway deploy, write or variable change.
  Old Railway Nimble was not restarted. Railway remains authoritative as instructed.
- **No real-money access enabled.** No exchange credential installation or real-order action.
- DU remains OFF. The independent Research backup timer remains enabled and its service was
  inactive during reboot. No Research runtime was activated on OVH.

## Next action

**Remediate the Lab backup lifecycle's paper-run assumption and provision the missing FRED
configuration through the normal secret path without printing its value.** Keep all authority
fences in place. Verify a successful registered/checksummed Lab backup, its normal off-host pull
and independent restore, and successful macro cycles, then begin a new ≥24-hour rehearsal
window. Preserve this failed window as evidence; do not retroactively rewrite its verdict.

Only after that window passes should v2 data-only profile implementation, a fresh measured DU
budget and independent edge control-plane attestation be reviewed for separate authorization.
There is no executable DU activation command for the installed v1 profile, and none was run.
