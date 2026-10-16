# OVH ranking admission and Research backup closure — 2026-10-16

Research backup alert **closed**. Ranking resource verdict: **PASS WITH SPECIFIC LIMITATIONS**.
Ranking was **not activated**: a newly reproduced defect in the exact prepared DU worker
violates the required observation cadence. Warming readiness: **NOT READY**.

This is a bounded addition to the existing 26-hour rehearsal and 7h14m remediation evidence;
neither clock is reset. Evidence: [ovh-du-ranking/20261016](evidence/ovh-du-ranking/20261016/).
Work is based on locally approved main `5cb1ff6b025539ace20a5b885c110d67ac595e23`, in the isolated
`codex/ovh-du-ranking` worktree. The remote main `020896f` is an ancestor, 12 commits behind;
it was not substituted for the newer approved local history. Unrelated workspace changes remain intact.

## Research alert: exact cause and closure

Failing stage: `scheduled → offhost → remote integrity check`, after encrypted upload and
remote file listing. Exact tool invocation:

```text
restic --json --no-cache check --read-data
```

Five unattended attempts failed: starts **03:21:32, 04:24:19, 05:27:08, 06:29:56, 07:32:45 UTC**;
failures **03:24:19, 04:27:07, 05:29:56, 06:32:45, 07:35:34 UTC**. The wrapper correctly suppressed
provider stderr; its generic status alone did not establish corruption or a Google outage.

Sanitized live diagnosis at **08:05:38–08:06:00 UTC** established:

- Repository lock `e026b38e4e81e733f4664a34fd00d5247816e7a108470ebb150f915ae8acf325`.
- Created **2026-10-15 17:38:39.799441862 UTC**, shared (`exclusive=false`), host
  `vps-778d603b`, PID **99424**, backup user UID 992/GID 991.
- PID absent; host boot **2026-10-15 17:52:46 UTC**, after lock creation; no restic/rclone
  process was running. Thus this was a stale lock, not legitimate concurrent work.
- Actual check returned **11**, with fixed diagnostic flags `already locked`, `unable to
  create lock`, `repository is already locked`. Authentication/quota/network/space flags
  were absent. Its failure occurred before an integrity scan, so it established no corruption.

The lock timestamp follows the custodian-key ceremony (key created at 17:38:05). The exact
original subcommand/process termination is not retained; attributing it specifically to
`key list` versus the repository-open verification would exceed the evidence. The proven
cause is the abandoned pre-reboot shared lock. Shared uploads could proceed, but the exclusive
check could not. This was **deterministic until stale-lock removal**, not a one-off provider event.

At **08:07:01 UTC**, ran ordinary `restic --json --no-cache unlock`, which removes stale locks
only; never used `--remove-all`. Empty locks confirmed at 08:07:08. No repository recreation,
key rotation, provider change, weakened verification or application-code change was needed.
The six-start/day service limit was exhausted by the earlier failures and manual redispatch;
reset only its failed/start-limit state, retained its journal, and dispatched the unchanged unit.
The timer and retry policy were not edited.

The **unattended normal service** then completed **08:08:30.467309–08:13:18.135059 UTC**,
**4m47.668s**, `Result=success`, inactive/dead after its oneshot completion:

| Stage | Evidence |
|---|---|
| Local generation | Fresh sealed backup, local restore/checksum proof |
| Encrypted upload | Snapshot `fc9507814bea186809af169c88499e19706ba1e1a3bd51c3f2a0c7ce6456553f`; 24.05s |
| Remote verification | Manifest 24,494 B + archive 135,614,775 B, exact listing/size match |
| Integrity | **Full** `check --read-data`, not a subset; check/list stage 41.05s |
| Drive restore | Fresh scratch, `--no-cache`, 57.72s download/materialize/check; archive and logical SHA match; **146/146 relation fingerprints** |
| Critical DB validation | Schema 31/DuckDB 1.5.5, all relation values fingerprinted, including Context, macro, OI/funding, research definitions/results/checkpoints, alerts and journal |
| Manifest copy | Uploaded and read back with byte-SHA equality |
| Retention/prune | Host/tag-scoped daily7/weekly4/monthly3; 42.14s; verified new snapshot survives; three remote snapshots retained |
| Local safety | Previous verified generation removed only after new off-host proof; one 135.65 MB generation remains; scratch empty |

Logical SHA remains `dbaa5a3c31ebd738cca69bbd714263f4583722331f515ee2d239d4be77d9726f`;
archive SHA `023c3240e029eb87686905a1a9dfe3475397ba173ac0d7f9a38b1473c3e7892b`.
Frozen DB dedup added only **11,220 packed bytes**. The source frontier remains October15
11:31:40.099798 UTC. The backup does not claim to capture newer authoritative Railway writes.
The full 30-read offline parity suite was not rerun: today's all-table value fingerprints and
the entire logical-file SHA equal the already parity-validated frozen database.

Repository ID/version are unchanged: `148cfeaa…c6742`, format2. Machine key `b6a269bc…` is
freshly exercised by this full cycle. Custodian key `6f78b5ae…` remains byte-identical by its
content-addressed key ID to the key independently opened by the owner yesterday without the
machine key. Recovery usability is supported by that existing password proof plus unchanged
repository/key identity; the custodian password was **not** re-entered or obtained today.
No key material was printed. Credentials remain backup-user-only: config/env0600, password0400.
Final repository lock listing is empty. Known-secret scans of this morning's backup/scheduler
journal and runtime log are clear. Only backup service/timer are loaded in the Research namespace.

[Final closure receipt](evidence/ovh-du-ranking/20261016/research-closure.json).
Lab configuration was not altered during diagnosis or recovery. Research remains non-authoritative
on OVH; Railway remains the Research owner.

## Whole-host storage reconciliation

Admission sample **08:15:23.320782 UTC**. Decimal units below; MB=1,000,000 B, GB=1,000,000,000 B.
Physical counts come from one ordered, inode-aware `du` traversal across the application roots:
shared uv/release hardlinks are charged once, not repeatedly as separate dependency copies.
The independent whole-filesystem traversal corroborates the used-space total.

| Persistent component | Allocated size |
|---|---:|
| Lab DB | 146.29 MB logical, 146.29 MB physical; no WAL at sample |
| Frozen Research imported DB | 1,646.28 MB |
| Lab backups | 135.28 MB, one daily recovery generation plus metadata |
| Research backup identity generation | 135.66 MB |
| Earlier independent Research foundation proof backup | 135.66 MB, intentionally preserved |
| Microstructure raw | 74.79 MB |
| Microstructure spool | 41.56 MB |
| Large-print state | 0.061 MB |
| Other provider raw | 8.67 MB |
| Free-source receipts/state | 0.37 MB |
| Lab logs | 1.00 MB |
| System logs including journal | 73.19 MB, of which journal71.47 MB |
| Lab validation artifacts | 5.86 MB |
| Research state/validation artifacts, both identities | 35.92 MB |
| Research + Lab uv caches | 1,063.76 MB, including dependencies hardlinked into releases |
| Lab releases, exclusive allocation after shared caches | 146.01 MB across three releases |
| Research releases, exclusive allocation | 130.20 MB |
| Pinned restic/rclone tools | 116.32 MB |
| Managed Python | 268.06 MB |
| Temporary restore DBs | **Zero retained**; scratch directories empty |
| Other OS/home/tools/filesystem overhead | Remainder of the measured whole-host total |
| **Whole-host filesystem used** | **6.44448 GB** |
| Filesystem withheld/reserved blocks | **4.33709 GB**, not spendable application capacity |
| **Available free disk** | **94.68643 GB of 105.46799 GB** |

Nested entries must not be added to their parent totals. `/data` totals413.97 MB; frozen Research
namespace2.33183 GB; backup identity153.54 MB. The two independent Research archive copies and
duplicated validation references are visible, preserved and charged. No cleanup was used to pass.
Temporary `/tmp` and `/var/tmp` footprints are below0.2 MB combined at the stable sample.

The old **01:32–01:37** filesystem step remains unattributed: free space fell126.90 MB while
physical `/data` declined18.70 MB, leaving approximately145.60 MB elsewhere. Existing evidence
does not identify a retained file responsible. It is already charged in today's free bytes;
it is neither erased nor declared a recurring daily Lab cost. During this morning's retry,
a second135.65 MB Research generation was actually observed; it disappeared through verified
normal retention after success. Do not conflate these two separate events.

[Stable host sample](evidence/ovh-du-ranking/20261016/host-admission.json),
[during-retry sample](evidence/ovh-du-ranking/20261016/host-before.json).

## Growth measurements and runway

| Component | Measurement | Ranking model/day |
|---|---|---:|
| Existing Lab DB | 24.92 MB/day over26.006h base;19.11 over7.243h remediation;40.10 over6.590h since remediation end | **41 MB** using the largest recent rate |
| Ranking evaluations | Prior measured full decision60,188 B ×144/day | 8.667 MB |
| Ranking outcomes | Prior mature prospective ledger upper sensitivity | 6.912 MB |
| One growing Lab backup | One retained full recovery copy, not eight | **57 MB**, rounded up from the modeled live DB increment |
| Core-six micro raw/spool | Base net62.71 MB/day; targeted extension net declined under rotation | **64 MB**, no pruning credit |
| Provider raw/free sources | Base raw1.564 MB/day; current physical files include block overhead | 6 MB |
| Runtime logs/journal/host overhead | Base runtime log0.578 MB/day; journal allocation occurs in steps | 37 MB allowance, not a claimed measurement |
| Frozen Research DB | Same1.646 GB and identical SHA across yesterday/today | **0** |
| Frozen Research backup | Identical archive; current packed increment11,220 B/run | 0.100 MB metadata allowance |
| Additional margin | Rounded model total | 29.321 MB |
| **Conservative persistent growth** | Components220.679 MB/day, rounded up | **250 MB/day** |

The 40.10 MB/day recent DB rate is a short allocation-based estimate, not a settled24h forecast.
It is used conservatively instead of extrapolating the lower rehearsal rate. Existing core micro
rows are already inside that DB figure. Their extra raw/spool bytes are charged separately.
Raw/spool retention (raw1 UTC day, spool8 days subject to cursor) is bounded, but this admission
does not depend on future rotation savings. Releases/caches are static installation costs, already
charged, not ordinary daily growth. Future deployments and a live Research mirror require remeasurement.

```text
Free disk                                 94,686,425,088 B
70% minimum free floor                    73,827,595,879 B
Additional temporary/backup reserve         7,000,000,000 B
Capacity available for retained growth     13,858,829,209 B
Persistent growth model                      250,000,000 B/day
Runway                                           55.435 days
```

The7 GB reserve includes, rather than double-counts, Research's downloaded archive + materialized
DB **1.782 GB**, an overlapping new135.6 MB local archive, Lab backup creation ≥1.2× current DB
(175.55 MB now), restore/cache/DB overhead and margin. Local generation creation and remote restore
run sequentially, but the reserve also tolerates the independent Lab backup overlapping them.
Temporary nightly copies are **not added to persistent growth/day**.

If the unattributed historical step recurred at an extra150 MB **every day**, the model would be
400 MB/day and runway **34.647 days**. That stress case warns but does not breach the30-day stop;
it is not evidence that the event actually recurs. A repetition requires attribution and budget review.
No free-space/backup floor was reduced. The previous0.30 GB/day warming-inclusive figure was not
reused as ranking cost. The current warning is explicit rather than grounds to reject a cheap workload.

[Reproducible resource model](evidence/ovh-du-ranking/20261016/ranking-resource-model.json).
`validated=false` is deliberate: no executable activation receipt is issued or installed while the
prepared implementation fails correctness. The exact file hash is recorded in `SHA256SUMS.json`.

## Ranking-only envelope and operational thresholds

Expected worker peak is the prior measured362 MiB; retain its1 GiB unit cap. Prior mature work
cost2.49 CPU-seconds/600s (**0.249 CPU-s/minute**,0.415% of one core); initial discovery0.106 CPU-s.
Prior all-market fetch72 requests,288,851 response bytes,36.1s wall, at most2 requests/sec:
approximately10,368 requests/day and41.59 MB/day response bodies before protocol overhead.
DEX count can change; measure actual count before accepting the next window. No rich traffic or
subscriptions are added in ranking. Source HTTP happens outside the writer lock; decision/outcome
writes are bounded transactions (prior mature cycle92 logical inserts,163 SELECTs).

Available RAM is **11.133 GB /12.257 GB**, zero swap, load **0.104/0.283/0.253** at admission.
Use the prior simultaneous-workload low9.210 GB as the conservative reserve cross-check: subtract
the full1 GiB radar allowance and the still-unused future-paper3 GB reserve, leaving5.137 GB,
above the4.903 GB40%-free floor. Existing Research backup/offline-validation peaks are covered by
the earlier simultaneous-workload observation and explicit backup unit3 GiB cap. Do not add those
same observed peaks a second time. New simultaneous peaks invalidate the model if actual headroom fails.

| Signal | PASS | WARN | STOP |
|---|---|---|---|
| Available RAM | ≥50%, reserve check above satisfied | <50% for5min; radar>768MiB | <40% for5min, swap pressure/OOM/cgroup kill immediately |
| Disk/runway | ≥75% free, ≥60days,7GB working reserve funded | <75% free or30–60days | <70% free, <30days, next backup/restore reserve unavailable, any checksum/integrity failure |
| CPU/load | Busy<50%,5m load≤3 across6vCPUs | Busy>50% for5min,5m load>3,steal>2% sustained | Busy>80% for10min,5m load>6 for5min,steal>5% for5min,radar≥30CPU-s/min |
| Public API | Native discovery valid, cheap coverage and required fields classified; no rate limits | Any429/5xx or isolated light/DEX failure; p95 request>2s or batch>60s | Native source unavailable/misaligned; two consecutive degraded refreshes; repeated429s; batch approaches300s timeout |
| Ranking cadence | ≥12 real evaluations; expected600s plus measured processing overhead, always≤900s continuity | One delayed/missed evaluation: report exact gap | Repeated skips/gap>900s caused by worker scheduling; never waive light continuity |
| DB | Prior write/read work usually<3s, no job failure | Commit/write lock hold>5s or p95≥2× baseline; DB>67.5MB/day settled; retained>300MB/day | Any corruption/job lock timeout; write>30s or induced scheduler failure; DB>90MB/day settled; retained>450MB/day; runway<30days |
| Core collectors | Six markets, no drops, healthy acknowledged feed/cursors | >3 abrupt closes/hour,ack>60s,feed gap>10s | Core unhealthy>60s,>6 abrupt closes/hour,reconnect loop,spool errors/drop,unexpected rich expansion |
| Scheduler/backups | Normal successful cycles and timely frontiers | Cycle latency≥2× baseline or late cycle | Resource-induced failed/missed job, backup failure or authority drift |

These are ranking thresholds. Missing optional public inputs remain null in stored markets;
the frozen percentile policy deliberately assigns missing normalized inputs zero. That permitted
normalization is not license to zero-fill raw missing prices/quotes/OI/readiness data.

Resource verdict: **PASS WITH SPECIFIC LIMITATIONS**. Nominal55.4-day runway warns; a historical
host step remains unattributed; all ranking CPU/API/write estimates are prior measured evidence,
not new live workload measurements. Resource capacity does not cure a functional cadence defect.
Later final SSH reads also failed as described below; this resource decision describes the saved
08:15 sample, not an assertion of continuing host reachability.

## Prepared identity and activation hold

The immutable prepared release is still exactly
`eee205f24d5d9ef3cd71c32499f13ec2e2fbb8fd`, clean checkout; profile digest
`e7571ad1a2a35fb42dc8c1ea77759e31887a1c7679445561664c1dee68c12207`.
The prepared activation script SHA is
`f12039a006378c459f01ed5dc6fa283d910e21df333464009a08a9a7c1ce7a94`, identical on approved main.
Its ranking profile resolves rehearsal authority, existing runtime ID, execution/external actions
false, `collector_dynamic=false`, Nimble off. The original clean remediation receipt still passes.

The exact prepared command remains:

```text
sudo /opt/prism/current/.venv/bin/python /opt/prism/current/deploy/vps/activate_du_data.py ranking --budget /data/state/du-budget.json
```

It was **not executed**. Neither prepared-release deployment nor service/profile restart occurred.
The live release remains `9acb42a94d8888a2e712490b421912739a02bff6`, profilev1, DUoff.
The activation script and DU budget are absent from the current live image/state as expected
before deployment; no replacement command was improvised.

New material defect, exercised against the **unmodified prepared worker and real CLI due gate**:

| Worker start (s) | Real CLI branch | Commit clock, using36s fetch |
|---:|---|---:|
| 0 | Fetch admitted | 36 |
| 600 | `NOT_DUE`, previous commit only564s old | — |
| 1200 | Fetch admitted | 1236 |
| 1800 | `NOT_DUE` | — |
| 2400 | Fetch admitted | 2436 |

`worker.main` anchors sleep to subprocess start; `refresh` checks due against the previous
**commit** time. Nonzero fetch/write duration therefore causes the intervening poll to skip,
with approximately1200s between accepted observations. The frozen900s light maximum gap resets
continuity and streaks, preventing the intended seven healthy observations. This is an existing
prepared-code defect discovered before deployment, not a live Lab regression.

The audit uses a virtual clock and an HTTP-construction sentinel: no network, DB open, account,
or writer action. The actual branch and scheduling code run unchanged;36s is simulated using
the prior measured36.1s fetch as a realistic input. This is **not** a measured host ranking window.
[Executable reproduction and source hashes](evidence/ovh-du-ranking/20261016/prepared-cadence-audit.json).
Existing148 profile/activation/DU regression cases pass; they did not cover this worker/commit-clock
interaction. No application change was silently added to the prepared release.

## Observation, correctness, collectors and rollback

Ranking window start/end/duration: **not started / not started /0**. Evaluations **0** before and
after. Candidate counts, current DU source completeness/latency, live determinism and incremental
ranking RSS/CPU/DB growth are **not measured**. The prescribed2–4h window and12+ evaluations remain
outstanding because the explicit preactivation correctness gate failed.

The existing frozen engine/profile checks passed, including classification/readiness/fencing.
Live promotion/demotion history replay cannot be claimed without live evaluations. The read-only
DB audit tool is prepared to compare every stored score/component/state/change against frozen
evaluation and reversed-input ranking, and to verify core-only plans and no execution eligibility.

Core collector PID625, free-source612/609, scheduler2205 remained continuous, zero automatic
restarts. During **08:05–08:15 UTC**, all **60/60 core market-minutes were COMPLETE**, no missing
minute, disconnect, late print or spool restart. The08:06 micro ingest,08:08 news and08:16 intraday
cycles succeeded. All observed cycles since06:00 are successful; six micro frontiers agree.
FRED's00:20 cycle remains successful; no new macro slot or nightly Lab backup naturally fell in
this short operational interval. The earlier verified01:30 Lab backup remains valid. Free-source
backlog is zero. Do not describe these checks as a2h ranking-impact proof.

Host sample08:09:12→08:15:23 (during/after Research retry): mean interval CPU busy**7.81%** of
six cores, steal**0.017%**; RAM available11.131→11.133 GB, free disk94.508→94.686 GB,
DB141.05→146.29 MB. These are existing workload/backup deltas, **not ranking overhead**. Research
used162.94 CPU-seconds in its287.67s run; no instantaneous CPU or memory ceiling is inferred from
the sparse samples. Prior offline read-parity validation used2.087 GB peak RSS and192 CPU-seconds,
already included as independent overlap evidence. No execution process or extra scheduler ran.

The prepared ranking rollback path is validated by source/identity/config-path inspection:
activation first saves `/etc/prism/prism.env.pre-du-v1` before changing the four intended fields.
That saved file is absent now because activation never happened. After a corrected deployment:

```sh
sudo systemctl stop prism-universe.service
# First require no in-flight data job, no pins/satellites, and an exact six-core plan.
sudo install -o root -g prism -m 0640 /etc/prism/prism.env.pre-du-v1 /etc/prism/prism.env.rollback
sudo mv /etc/prism/prism.env.rollback /etc/prism/prism.env
sudo systemctl restart prism-scheduler.service prism-free-source-ingest.service prism-free-sources.service
```

Verify v1 profile digest, unchanged rehearsal claim, core-six collection and next ingest success.
No collector restart is required for ranking rollback; no DB/history/plan deletion or Railway action.
Any unexpected pin/satellite requires the separately documented obligation-preserving warming rollback.
This path was inspected, **not exercised**, because ranking was never activated.

After the last successful SSH read at **08:19:16 UTC**, three final read-only SSH attempts
returned255 at TCP connection setup (`ConnectTimeout=10`, connection timed out), all complete by
08:27:07 UTC. No authentication or remote command ran on those attempts. Cause is unknown from
this vantage; this does not establish host downtime, a firewall change or collector failure.
Latest saved service sample is08:15:23, DB evidence08:18:09, release/config/path check08:19:16.
Fresh host visibility and health re-attestation are required before any future activation.
[Final visibility limitation](evidence/ovh-du-ranking/20261016/final-visibility.json).

## Warming preparation only

The following is an **unvalidated proposal**, not warming admission. It lacks the required clean
live ranking window. Six is a maximum satellite capacity, not a claim that six become ready together.

| Item | Provisional envelope |
|---|---|
| Rich markets/subscriptions | Existing6 + up to6 satellites =12; approximately48 subscriptions |
| Worker/collector memory | Existing1GiB worker cap; prior12-market adapter641MiB; rich collector~0.5GiB sensitivity; preserve backup/future-paper reserves |
| CPU | Prior12-market adapter1.3–1.5CPU-s; mature ledger2.49CPU-s per cycle, distinct sensitivities; measure combined real cost |
| REST | Ranking baseline plus2 calls per initialized satellite/hour, max288/day additional; initial six also at most12 backfill calls; retries bounded |
| Backfill | ≤45 days of closed hourly bars per newly admitted satellite, up to~1,080 rows/asset; then hourly2h overlaps; funding48h; no retrospective OI |
| DB micro growth | Prior12-rich estimate4.424MB/day total; increment2.212MB/day over existing six, plus funding/OI/backfill metadata |
| Raw/spool | Model at most128MB/day before rotation, twice the conservative observed core budget; prior retained raw/spool sensitivity~430MB |
| Wire traffic | Prior six11,744B/s; linear12-rich23,488B/s, approximately2.03GB/day total; unmeasured live extrapolation |
| Initial disk working space | Separate bounded bootstrap allowance100MB inside7GB reserve; validate actual serialization/allocation before admission |
| Persistent total | Ranking components + incremental rich DB/backup/raw/spool, rounded **300MB/day**, ~46.20days above unchanged reserves |
| Extra150MB/day historical-step stress | ~30.80days, close to stop; warming therefore needs fresh measurements and attribution review |
| Backup overlap | Preserve7GB disk and independent Research3GiB unit cap; avoid initial rich backfill coinciding with heavy offline restores |

Large-print calibration remains prior7 UTC days, at least3 qualifying days of≥720 non-gap
minutes and≥1,000 prints total. Also unchanged: seven healthy lightweight observations with
≤900s gaps;7-day prospective listing age; exact technical/backfill readiness; two prospective
hourly OI captures within140min; funding≤80min old;288 prior complete15m windows plus current;
promotion3 evaluations/demotion6 and≥6h dwell; reentry resets the rich segment. Missing data
never gets invented to advance those gates. No clocks were started in this tranche.

Warming readiness verdict: **NOT READY**.

## Safety and exact next action

No paper accounts/runs/positions exist; all execution tables remain empty. No sizing cohort,
real-money credentials/actions, Railway restart, Research scheduler authority, Context migration,
DNS/Auth0/Work change, or full-authority claim. Lab release/profile/config and core processes
remain unchanged. The only remote mutation was stale Research repository-lock cleanup and
redispatch of the existing backup service after its start-limit reset.

**Next action:** re-establish read-only SSH visibility and re-attest host/collector health; repair
and review the DU worker/commit-clock scheduling interaction, preserving
the frozen600s cadence and900s continuity rule; add a regression exercising nonzero fetch duration
and `NOT_DUE`; issue a newly identified prepared release. Recheck a fresh ranking budget, then use
the guarded ranking activation and observe2–4h with12+ genuine evaluations. No24h soak reset.
The current immutable prepared image must not be activated as-is.

Only after that clean measured ranking window, the next separate approval is: **enable data-only
`warming` for up to six satellites under a freshly validated12-rich-market budget, with bounded
backfill and all frozen readiness gates; paper, sizing, real orders and Research handoff remain OFF**.
That approval has neither been requested as a final action nor executed here.
