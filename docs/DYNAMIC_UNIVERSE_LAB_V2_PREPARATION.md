# Dynamic Universe Lab v2: prepared data-only activation

**Preparation only. No activation approval is inferred from a clean remediation gate.**
The live Lab remains on `prism_autotrader_lab_v1`, rehearsal authority, with Nimble and DU OFF.
The implementation is committed on `main` as `09a0a21`, following backup fix `56cafcf`.
The separately prepared immutable release below retains its original identity; committing it
on main does not deploy or activate it.
This package is isolated from the Research migration track and contains no Research/Context,
Railway scheduler, DNS, OAuth or Shared Intelligence authority change.

## Profile and exact scope

| Field | Prepared value |
|---|---|
| Selector | `PRISM_PROFILE=autotrader_lab_v2` |
| Frozen identity | `prism_autotrader_lab_v2` |
| Definition SHA-256 | `e7571ad1a2a35fb42dc8c1ea77759e31887a1c7679445561664c1dee68c12207` |
| Authority | `rehearsal` only for writers; `full` is refused |
| Runtime identity | existing `vps-prism-autotrader-lab-rehearsal`; no reclaim |
| Database | existing `/data/prism.duckdb`; no migration |
| Processes | core collector, free-source collector/ingest, scheduler, opted-in DU radar |
| Scheduled jobs | existing data jobs and backup; no new cron/timer |
| Excluded | Nimble worker, paper brief/execution, paper account creation, gateway/Research, sizing |
| Stages | OFF by default; `ranking`, then separately approved `warming` |

Original v1 and Railway Research definitions/digests remain unchanged. The prepared isolated
release derives from the deployed backup hotfix and excludes the unrelated OVH Research profile
addition present on current development `main`.

The diff also wires the VPS collector to the resolved warming profile (the old VPS unit did not
consume the Railway shell flag), and fences direct DU/warming CLI bypasses. Nimble register/create/
evaluate and old Paper v2 register/create refuse under data-only v2 before opening a DB.
Retired Research account draining is unchanged.

Ranking starts the existing `prism-universe.service`: public Tier 0 discovery, up to 30 light
candidates, ten-minute causal ranking/decisions/outcomes/readiness and desired-plan publication.
Warming adds no account or position: it enables candidate backfills and rich subscriptions.
Six core plus at most six shared warming/active satellites gives normally 12 rich markets,
**48 subscriptions** at four per asset. Pins can exceed the ordinary cap only to retain obligations;
this does not grant entries and triggers resource review. HIP-3 remains quarantined.

Initial technical backfill is at most 45 days, followed by hourly bounded two-hour overlaps.
Ranking/selection runs every 600 seconds; continuous WS collection and prospective hourly OI
captures retain the frozen timing and availability semantics. No market-data gate is shortened.

## Exact environment delta

Apply through the prepared guarded script after separate approval, preserving all existing
runtime paths, secrets and authority fields:

```dotenv
PRISM_PROFILE=autotrader_lab_v2
PRISM_UNIVERSE_STAGE=ranking
PRISM_UNIVERSE_BUDGET=/data/state/du-budget.json
PRISM_NIMBLE_RUNTIME=off
```

The later warming delta is exactly `PRISM_UNIVERSE_STAGE=warming`. It needs fresh resource
validation after observing ranking. No `PRISM_AUTHORITY=full`, runtime reclaim, account creation,
experiment activation or sizing registration belongs in either command.

`deploy/vps/activate_du_data.py` requires the completed clean targeted receipt (>=6 hours,
>=2 natural macro successes, >=1 natural backup success), a current matching DU budget and
correct rehearsal identity. It refuses any existing paper execution/account data, insufficient
headroom, or missing original rollback environment. It changes only the four fields above and
restarts affected Lab services. It is not called by the monitor or scheduler.

After the prepared release is separately approved and deployed, the exact on-host command is:

```sh
sudo /opt/prism/current/.venv/bin/python /opt/prism/current/deploy/vps/activate_du_data.py ranking --budget /data/state/du-budget.json
```

Later, after ranking resource evidence and separate warming approval:

```sh
sudo /opt/prism/current/.venv/bin/python /opt/prism/current/deploy/vps/activate_du_data.py warming --budget /data/state/du-budget.json
```

Neither command has been executed. The live hotfix release does not contain this v2 package.
The current bounded observer and v1 scheduler do not automatically deploy or select it.

## Readiness stays frozen

Frozen policy `hyperliquid_dynamic_universe_v1`:
`universepolicy_5f88b1dcdf20d7d19047edffab329076d7a63936f8c0916c21ffc83bcf4d56ff`.
Its definition, weights, costs, hypothesis portability and scientific verdicts are unchanged.

- Seven healthy lightweight observations, no adjacent gap >900 seconds.
- Seven days since first prospective sighting; no invented listing date.
- Exact technical primitives/current closed hour after bounded native backfill; backfill is
  known only when ingested and failures retry initialization.
- At least two prospective hourly OI captures within 140 minutes; no fabricated retrospective OI.
- Observed settled funding available by the decision clock and no older than 80 minutes.
- **288 prior COMPLETE fifteen-minute micro windows, excluding current, plus current COMPLETE**;
  exact quality rules and current rich collection segment required. Gaps extend elapsed time.
- Large prints: prior seven UTC days, at least three qualifying days with >=720 non-gap minutes
  each, >=1,000 prints total. Required null features remain unavailable.
- Promotion: rank <=6, score >=0.35, three consecutive evaluations, all gates/capacity passing.
  Demotion: rank >10/missing or score <0.35 for six consecutive evaluations and >=6-hour dwell;
  hard safety failures bypass dwell, pins defer removal, and gaps >900 seconds reset streaks.
- Reentry preserves history but records subscription gaps and resets rich-segment warmup;
  old segment history cannot satisfy current readiness.

## Resource budget and stops

Use the base rehearsal plus measured DU evidence in
[DU v1](DYNAMIC_HYPERLIQUID_UNIVERSE_V1.md) and fresh host measurements at the end of the
[targeted remediation](OVH_LAB_TARGETED_REMEDIATION.md). The targeted gate has closed successfully. The current proposal remains unvalidated pending
whole-host storage attribution and the separately observed Research backup retry; see the final
remediation report. Do not forge `validated=true` or treat past Research recovery as current backup health.

Prior measured DU ledger peak: ~362 MiB and 2.49 CPU-s per ten-minute cycle; isolated 12-market
adapter sensitivity: ~641 MiB / 1.3–1.5 CPU-s. Six-market live collector: ~174 MiB RSS,
11,744 wire B/s. Twelve-market traffic is a sensitivity, not a measured live ceiling.
Reserve 1 GiB for the radar/readiness worker, approximately 0.5 GiB rich collector memory,
normal core jobs, backup transients, and **3 GB for later baseline paper execution**. Paper stays OFF.
Measured base peak including independent Research recovery left 8.58 GiB available; the measured
incremental DU model plus that paper reserve fits the 40%-free target. Unit caps are not expected
simultaneous usage: stop expansion if actual shared headroom disagrees, and avoid initial rich
backfill overlapping heavy scratch restores. No Research schedule is changed.

Conservative DU-stage storage: approximately 0.30 GB/day including base/DU DB, one growing
recovery copy, raw/spool, logs and overhead; reserve 7 GB for transients. Do not infer Lab growth
from the earlier Research scratch-copy filesystem steps. Budget another ~0.10 GB/day for a later
baseline paper workload and its backup amplification; that is an estimate to remeasure before
paper authority, not authorization. Recompute >=30-day physical runway above the 70%-free floor.

Keep the existing warning/stop policy:

| Resource | Warning | Stop |
|---|---|---|
| Available RAM | <50% for 5 min or DU unit >75% cap | <40% for 5 min; OOM/cgroup kill or swap pressure immediately |
| Disk | free <75% or conservative runway <60 days | free <70%, runway <30 days, backup reserve unavailable, or checksum/backup failure |
| CPU/load | busy >50% for 5 min; 5m load >3; steal >2% sustained | busy >80% for 10 min; 5m load >6 for 5 min; steal >5% for 5 min; DU >=30 CPU-s/min |
| DB/retained growth | DB >67.5 MB/day or total >0.30 GB/day | DB >90 MB/day, total >0.45 GB/day, runway <30 days; corruption/lock timeout immediately |
| WS/subscriptions | >3 abrupt closes/hour, missing ack >60 s, feed gaps >10 s | >6 abrupt closes/hour, core unhealthy >60 s, satellite unacknowledged >120 s, reconnect loop or unjustified cap breach |

A budget is policy-matched, <=24 hours old, accounts for DB/raw/spool/backups and rich capacity,
and demonstrates worker CPU <30 CPU-s/min. Ranking alone does not validate rich warming traffic.
Renew the budget from actual measurements before it ages out.

## Rollback without destroying warmup

Stop `prism-universe.service` first to stop new expansion. Preserve the plan and mandatory
collector while any open/pending/accounting/research/event pin remains. After obligations clear,
publish a causally timestamped atomic core-only plan, record removals and drain late revisions.
Then restore `/etc/prism/prism.env.pre-du-v1` and restart only affected Lab services.
Verify v1 digest, rehearsal identity, six core subscriptions, cursors and ingestion health.

Stage OFF alone is insufficient: the collector intentionally honors an existing valid plan on
restart. Never delete a plan to unsubscribe risk, erase warmup rows/outcomes/raw/spool or restore
an old DB to undo a profile change. Railway Research and old Railway Nimble are untouched.

## Reviewed preparation state, October16

The original26-hour evidence plus7h14m32.777s targeted extension is closed. Both natural FRED
runs and the normal nightly Lab backup passed; fresh on-host and off-host restores matched146/146
fingerprints. The edge Manager re-attestation limitation remains explicit.

Current host06:23 UTC: RAM available11.197/12.257GB, disk free94.562/105.468GB, load0.06/0.10/0.13.
After the70%-free floor and7GB transient reserve, model growth capacity is13.734GB (~45.8days
at0.30GB/day), already a runway warning. Whole-host disk steps outside net Lab growth and a
separate `prism-research-backup` restic-check retry need resource reconciliation before admission.
The saved `du-budget-proposal.json` is deliberately `validated=false`; no budget is installed.
This does not restart the Lab rehearsal. Ranking-only needs a fresh six-rich-asset budget;
warming needs a later12-rich budget supported by ranking/backup overlap measurements.

The immutable tested package was recovered from its verified bundle into the durable checkout
`/home/matth/prism/.worktrees/ovh-lab-v2-prepared` at `eee205f24d5d9ef3cd71c32499f13ec2e2fbb8fd`.
After resource admission and separate approval, its exact deployment command is:

```sh
cd /home/matth/prism/.worktrees/ovh-lab-v2-prepared
PRISM_VPS_HOST=debian@178.33.95.139 deploy/vps/deploy.sh eee205f24d5d9ef3cd71c32499f13ec2e2fbb8fd --no-restart
```

No deployment/profile/stage change was executed this morning. The original148 prepared-release
checks and8 legacy Paper-v2 fence checks remain valid; this morning only evidence/docs changed.

## Concrete rollback sequence

For the first **ranking-only** stage, the rich collector has not been expanded. Verify the saved
v1 environment exists, no data job is in flight, the desired plan contains only the six core
assets and no pins, and the collector still shows those six. If any satellite/pin is present,
use the warming procedure below instead. Then the exact service/config sequence on the VPS is:

```sh
sudo systemctl stop prism-universe.service
sudo install -o root -g prism -m 0640 /etc/prism/prism.env.pre-du-v1 /etc/prism/prism.env.rollback
sudo mv /etc/prism/prism.env.rollback /etc/prism/prism.env
sudo systemctl restart prism-scheduler.service prism-free-source-ingest.service prism-free-sources.service
```

Check `/data/state/profile.json` returns to `prism_autotrader_lab_v1` with its frozen digest,
rehearsal authority and the existing runtime ID; check Nimble/DU inactive, collector six core,
and the next ingest cycle successful. No DB restore/delete is involved and all ranking evidence
and the budget receipt remain available. This sequence is documented, **not executed**.

For **warming**, stop the radar first. Read current `universe.ledger.risk_pins` from the Lab DB
and the last subscription plan's pins; preserve collection while either has obligations. With
obligations resolved, use the existing `universe.subscriptions.publish` API to write an atomic,
causally dated core-only plan, retaining the previous plan and a rollback audit record. Wait for
all six core acknowledgements and satellite removals; drain late minute revisions through the
normal ingest job and verify cursor/DB agreement. Only then restore the saved environment using
the commands above and restart `prism-microstructure.service` so it resolves v1. Verify six-core
collection and readiness segment/gap bookkeeping. If removal cannot be proved, leave required
collection running and report that condition. Preserve all DB/history/raw/spool files.
