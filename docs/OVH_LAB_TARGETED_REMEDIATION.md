# OVH Lab targeted remediation extension

**2026-10-16 next-tranche follow-up:** the separate Research backup alert is now closed with
a successful full unattended Drive recovery cycle. Ranking resource admission passes with
specific limitations (55.4-day conservative runway), but activation is held: the unmodified
prepared DU worker/CLI cadence interaction was reproduced producing1200s observation gaps,
above the frozen900s limit. Lab v1/DUoff remain unchanged. See the
[bounded ranking-admission report](OVH_DU_RANKING_ADMISSION.md). The historical rehearsal verdict below stands.

**Closed and reviewed 2026-10-16.** The 26-hour base rehearsal remains valid infrastructure evidence.
This extension validates two fixes; it is not a second 24-hour rehearsal.
Evidence: [20261015](evidence/ovh-lab-remediation/20261015/).

The implementation is integrated on `main` as backup fix `56cafcf` and preparation-only Lab v2
`09a0a21`. Historical deployed/prepared release hashes below remain unchanged.
[Commit provenance](evidence/ovh-lab-remediation/20261015/COMMIT_PROVENANCE.md).

## FRED root cause

`FRED_API_KEY` was present in the operator's `/home/matth/prism/.env` (matth:matth, 0644).
It was absent from both VPS EnvironmentFiles; `/etc/prism/secrets.env` existed but was empty
(root:prism, 0640), and `/opt/prism/current/.env` did not exist. The scheduler process did not
have the key. `git archive` does not deploy the local untracked `.env`; systemd does not read
that file implicitly. Code uses the same `FRED_API_KEY` name and does not filter it out.

## FRED fix

Provisioned the existing key, without changing its value, through encrypted SSH stdin into
the intended `/etc/prism/secrets.env`, root:prism 0640. No secret was placed in arguments,
release files, runtime config, evidence or this report. Restarted only the scheduler to load
its already-declared EnvironmentFile; collectors were not restarted.

## FRED manual-path proof

At **18:13:38–18:13:55 UTC**, the exact `market ops cycle context_macro` ran as `prism`,
in `/opt/prism/current`, using the actual scheduler PID 2205's complete inherited environment.
It recorded success: FRED calendar received 17 items; ALFRED processed five pending events,
stored four updates and correctly left one GDP vintage pending publication. That pending
vintage is not an outage or configuration failure. DB provider/update metadata corroborates
processing. Scans found no key in child output, scheduler journal, runtime log or FRED raw files.

## FRED scheduled-cycle proof

Two natural cycles passed, without schedule changes:

| UTC slot | Actual execution | Result |
|---|---|---|
| October 15 19:12 | 19:12:01.331449–19:12:12.018584 | Macro and both FRED providers OK |
| October 16 00:20 | 00:20:01.201472–00:20:17.041430 | Macro and both FRED providers OK |

Both calendar fetches received 17 items with normal duplicate suppression. The actuals job
correctly retained one not-yet-published GDP vintage as filtered/pending, with no provider error.
The manual path had already stored four actual updates; subsequent idempotent cycles need not
invent new rows. Final secret scans of scheduler journal, runtime log and FRED raw files are clear.
[Final FRED proof](evidence/ovh-lab-remediation/20261015/fred-final-proof.json).

## Backup-registration root cause

`backup_live → verify_backup` already exempted a data-only Lab from a paper-run requirement.
The next call, `backups.register → inspect_backup`, independently required a paper run and
raised `no run metadata`. It failed before writing the manifest/success event, after copying
the consistent DB. This was a registration validation mismatch, not permissions, authority,
ID collision or a corrupt snapshot.

The existing off-host helper also assumed the SSH user could read private `/data` files and
never acknowledged a verified archive receipt. The repaired helper uses service-user rsync,
revalidates existing archives, tests restores, compares fingerprints and acknowledges receipts
through the existing lifecycle. It does not prune backups itself.

## Backup-lifecycle fix

Live hotfix **`9acb42a94d8888a2e712490b421912739a02bff6`**, based on the exact prior Lab revision
`530ba0d`, contains only backup/lifecycle changes and regression tests. No schema migration,
DU enablement or Research code/authority change was deployed.

Registration recognizes a data-only Lab from persisted schema >=31, `lab_seeded` provenance
and a matching Lab authority identity. A Lab-looking shell or unseeded empty DB is insufficient.
All normal schema/table/checksum checks remain. Manifests now retain all-table fingerprints,
source authority, data frontiers and the last completed source cycle. No fake paper run is created.

The prior orphan was independently reverified/registered, exported using the normal helper,
and governed retention removed its on-host copy only after its off-host receipt was accepted.
Its original archive and complete base-rehearsal evidence are preserved.

## Real backup workflow proof

The installer used the fixed production backup path at **18:19**. Then the exact scheduler
workflow, `market ops cycle backup`, ran under the actual scheduler environment/user at
**18:22:00–18:22:08 UTC**, recording success and a `backup_verified` event.

Artifact: `/data/backups/daily/prism-20261015T182201Z.duckdb`.
SHA-256: `5d8bdb70040a0ee1e0e7d2529c5aa4c53f3c514f88dc65a6de0c8b7283396077`.
It registered normally, wrote the source frontier/manifest, verified checksum, completed normal
retention and passed the standard off-host pull/receipt path. The ordinary **01:30 UTC** backup
ran naturally and succeeded at **2026-10-16 01:30:01–01:30:11 UTC**.

Nightly artifact: `/data/backups/daily/prism-20261016T013003Z.duckdb`.
SHA-256: `82f7c8c089ac8a7ea449d1c13fccde77b0f23f77247a4173c13670188168893f`.
The source manifest, source frontier, successful cycle/event and governed retention are recorded.
At 01:41 UTC the independent on-host closure restored it to fresh scratch and matched all tables.
On return, the standard off-host helper downloaded/checksummed/restored/archived this exact nightly
source and acknowledged its verified receipt. [Off-host receipt](evidence/ovh-lab-remediation/20261015/nightly-backup-offhost-pull.json).

## Restore/fingerprint proof

The real scheduler artifact was restored off-host into a fresh scratch directory.
**All 146 table fingerprints match the source manifest**, schema 31 opens correctly, source
frontiers are recorded, and checksum/decompression verification succeeds. Scratch operates
with `PRISM_AUTHORITY=none`, role `scratch`, execution/external actions false. Historical
claim metadata is preserved for exact fingerprint equality; no new authority/activation event
or paper account is created. Removing the scratch role makes writable open refuse.

The **normal nightly source** independently passed the same proof both on-host overnight and
off-host this morning: **146/146 fingerprints**, including authority/history tables, match.
[Fresh off-host scratch proof](evidence/ovh-lab-remediation/20261015/nightly-backup-offhost-restore-proof.json).
Historical claims do not confer authority on the scratch runtime: its role is explicitly scratch,
authority none, execution/external actions false, and an ordinary writable open is refused.

## Edge firewall re-attestation

Host nftables still permits unsolicited inbound SSH only; effective listeners show SSH and
pre-existing resolver/DHCP/chrony endpoints, with no application/DB listener. SSH is reachable.
No host or edge network change was made.

The user confirmed no authenticated OVH CLI/API credential source exists: the edge was
configured through OVH Manager. Control-plane re-attestation is unavailable from the host and
is an accepted known limitation, with manual Manager verification if needed. No credentials
will be created and no networking scope expanded. Workstation TCP-connect
checks on ports 80/443 cannot attest inbound behavior: the documentation-address control
`192.0.2.123` also accepts connection setup then returns no HTTP response. This vantage is
intercepted or otherwise unsuitable for those connect-only conclusions. Ports 5355/8787 time out.
Do not misreport 80/443 as public VPS listeners or infer OVH rules from that artifact.

## Post-fix rehearsal window

Start: **2026-10-15T18:25:28.271856Z**.
End: **2026-10-16T01:40:01.048838Z**.
Duration: **7h 14m 32.777s** (26,072.777 seconds).

The read-only observer produced **88 samples**, no recorded concerns, two successful natural
macro cycles and one successful normal nightly backup. Its largest sampling interval was
306.66 seconds; samples do not establish sub-second continuity. Actual service start/PID and
journal/spool/cycle evidence supply the intervening continuity and explicit gaps.
The original independent sampler supplied 87 additional resource samples within the window.
[Gate receipt](evidence/ovh-lab-remediation/20261015/final.json),
[closing checks](evidence/ovh-lab-remediation/20261015/closure.json),
[reviewed metrics](evidence/ovh-lab-remediation/20261015/reviewed-window-metrics.json).

The observer and one-shot closure ran on the VPS while the workstation was off. Closure finished
at **01:41:13.643977 UTC**, with checks complete, no authority changes and no activation.
The earlier automation preflight's timestamp parser error was corrected and reverified before
handoff; it was an audit-tool failure, not a Lab scheduled-job failure. Its journal entry is retained.
This is the targeted remediation extension to the original 26 hours, not a second 24-hour run.

## Scheduler/service health

**91/91 scheduled Lab cycles succeeded**: intraday 29, microstructure ingest 29,
context news 15, positioning 7, funding data 7, macro 2, OI 1 and backup 1.
`paper_v2_funding` is a data capture job; no paper execution/account existed.
PIDs stayed collector625/free612/ingest609/scheduler2205, all with zero automatic restarts.
The scheduler's intentional FRED secret reload happened before the clean window. The collector
continues its original release image; only scheduled jobs consumed the immutable backup hotfix.
No duplicate scheduler/collector, orphan worker, authority event or DU evaluation was found.

Six original-sampler DB reads encountered an active writer (19:38:57, 21:04:08, 21:29:11,
22:04:16, 22:39:21 and 01:09:43 UTC). These read-only samples intentionally do not wait out
contention. **No job failed from DB locking**, no stale lock was found, and the separate bounded
DB observer opened successfully at every sample. Do not describe the window as having zero contention.

At closure all six markets had 15m bars through01:30, hourly bars/funding through01:00 and
micro DB rows through01:34 (normal 15-minute batch ingestion, not collector staleness).
Collector output covered every full minute18:26–01:39: **2,604 market-minutes, none missing**.
**2,592 COMPLETE and12 PARTIAL**: all six markets at **20:46** and **23:39 UTC** were partial
because the provider normally expired its WS connection with close1000. Reconnection took
**1.5s** and **1.4s** respectively. No run restart, late print or spool drop/error. These partials
remain excluded by readiness quality rules; 99.54% completeness is not substituted for the gaps.
Every cursor stayed within a file on a newline. Through the DB frontier there were no missing
spool keys, duplicated records, DB duplicates, malformed lines or revision/status mismatches.

Free-source collector/ingest heartbeats stayed current, with no pending backlog at sample times.
One new receipt was accepted/ingested during the extension. Kraken was transiently FAILING at
19:08–19:09, Coinbase at20:11–20:12 (HTTP=None); later polls recovered without intervention.
Current checks show all three providers successful, zero consecutive failures and an empty backlog.
The descriptive reaction worker's existing bounded budget is not execution authority.

Expected activity: scheduled data jobs, nightly backup/verified retention and raw rotation.
Harmless/recovered warnings: the two marked WS expiry minutes, the two free-source polling
transients, and SSH pre-auth disconnect/protocol noise. Kernel error journal is empty; no failed
systemd unit at closure. The corrected audit preflight failure is explicitly retained in the journal.
There is no unresolved material Lab ingestion/DB/scheduler defect in this target window.

Resources (5-minute samples; no claim about unmeasured instantaneous peaks): minimum available
RAM **10.575GB of12.257GB**, maximum CPU busy interval **4.19%** across six vCPUs,
maximum steal **0.0205%**, load maxima **0.387/0.304/0.192**, zero swap/OOM.
Maximum sampled unit memory: collector197.3MB, free collector135.5MB, free ingest307.4MB,
scheduler including children459.1MB. Current process high-water RSS is recorded separately.
DB129,511,424→135,278,592B (**+5.77MB**, sampled max138.42MB).
Root filesystem free94.844→94.718GB, minimum94.715GB. Do not attribute this whole-host delta
solely to Lab growth: physical `/data` usage declined18.70MB under raw rotation.
Physical spool grew4.53MB, provider raw1.54MB, micro raw declined38.51MB, backup tree grew5.53MB.
The ~140.6MB filesystem step near01:32–01:37 is preserved for whole-host resource attribution;
it is not extrapolated as a proven recurring Lab cost. No deleted open files >1MB were found this morning.

**Current separate alert after the target window:** the independent `prism-research-backup`
service is in retry after `RuntimeError: restic check failed (status11)` (three failures observed).
This task has not diagnosed that status, unlocked/pruned a repository, restarted that service or
changed Research authority. Its prior successful gate is historical evidence, not proof that today's
scheduled backup is healthy. Track this as a separate operational fault and include its shared
resource impact before validating DU admission. Lab cycles also remained successful after01:40.
The later collector audit (01:40–06:23 UTC) also has no missing minutes or process restart.
It records an expiry reconnect at02:41 (1.8s after close) and a stale-feed reconnect at04:41
(10s silence detector, then1.4s after close); all six markets' affected minutes remain PARTIAL.
These recovered feed interruptions are retained in the evidence and do not satisfy COMPLETE
readiness windows. [Later collector detail](evidence/ovh-lab-remediation/20261015/post-window-collector-quality.json).
Current host clock is synchronized, kernel6.1.0-53-cloud-amd64 is unchanged, host security hashes
match the baseline, nftables/SSH/listeners are unchanged and Lab core PIDs remain continuous.

## Updated rehearsal verdict

**REHEARSAL PASS WITH SPECIFIC LIMITATIONS**

Evidence: **26-hour base rehearsal +7h14m32.777s targeted post-fix validation**.
Both material Lab defects are fixed and proven through real scheduler paths, with no recurrence.
The independently proven reboot/host/collector evidence remains valid; no second reboot or
new24-hour rehearsal was needed. Lab backup/restore, authority fences and scheduler gates pass.

Limitations: OVH edge control-plane configuration still needs manual Manager verification if
required; provider reconnect partials and recovered polling gaps are explicit above; a separate
current Research backup retry is not concealed or declared fixed by this Lab verdict.
DU resource admission remains a separate gate. No pass here authorizes execution or migration.

## `prism_autotrader_lab_v2` diff

Prepared but **not deployed/activated**:
**`eee205f24d5d9ef3cd71c32499f13ec2e2fbb8fd`**, parent the live backup hotfix.
Profile digest **`e7571ad1a2a35fb42dc8c1ea77759e31887a1c7679445561664c1dee68c12207`**.
[Exact patch](evidence/ovh-lab-remediation/20261015/lab-v2-prepared.patch) and
[recoverable release bundle](evidence/ovh-lab-remediation/20261015/lab-releases.bundle).

The selector is `autotrader_lab_v2`; writers preserve rehearsal authority and the existing Lab
runtime ID. It adds only opted-in discovery/ranking/warming, excludes execution/account creation
and rejects full authority. See [full preparation](DYNAMIC_UNIVERSE_LAB_V2_PREPARATION.md)
for the exact env delta, guarded commands, resource envelope, stop thresholds and rollback.
Frozen DU readiness windows and policy identities remain unchanged.

## Dynamic Universe activation readiness

**Prepared for review; not active or automatically authorized.** The exact profile/config
package is ready. A validated live admission budget has **not** been issued or installed.
[Reviewable resource proposal](evidence/ovh-lab-remediation/20261015/du-budget-proposal.json)
intentionally says `validated=false`; the guarded activation command will refuse it.

Current measurement06:23 UTC: **11.197GB RAM available**, **94.562GB disk free**, load0.06/0.10/0.13,
zero swap. Using the existing70%-free floor and7GB transient reserve leaves **13.734GB**
for admitted retained growth. The prior0.30GB/day DU-stage model gives **45.8days** (already below
its60-day warning threshold, above30-day stop). Current whole-host steps/Research backup behavior
must be reconciled before calling that a validated total-host forecast. Keep the same thresholds;
do not solve the warning by reducing reserves. Ranking-only can use a six-rich-assets budget;
12-rich warming needs actual ranking/backup overlap evidence and a new budget.
The read-only file-size/timestamp audit found no retained >1MB file modified during the main
01:32–01:37 step in the sampled cache/Research/log/temp roots. It does not establish the cause
or justify discarding that host-wide delta. No cleanup or repository unlock was attempted.

Reserve future baseline-paper RAM3GB, retain backup headroom, and preserve all frozen readiness
rules. See the full preparation document for measured prior DU CPU/RSS/network sensitivities,
warning/stop policy and rollback. No additional host-soak clock is required to review these.

## Exact next action

The targeted gate is closed. Next complete whole-host resource attribution/admission, and
triage the separately reported Research backup alert within its own scope. Then separately approve
**ranking-only** under Lab v2. Warming follows ranking telemetry and its own validated budget;
all scientific warmups remain unchanged. There is no remaining24-hour infrastructure wait.

After resource admission and separate activation approval, the exact prepared deployment is:

```sh
cd /home/matth/prism/.worktrees/ovh-lab-v2-prepared
PRISM_VPS_HOST=debian@178.33.95.139 deploy/vps/deploy.sh eee205f24d5d9ef3cd71c32499f13ec2e2fbb8fd --no-restart
```

Install the reviewed, freshly validated budget at `/data/state/du-budget.json` as prism-readable
0640; do not install the unvalidated proposal as an activation receipt. Then the guarded on-host
command is:

```sh
sudo /opt/prism/current/.venv/bin/python /opt/prism/current/deploy/vps/activate_du_data.py ranking --budget /data/state/du-budget.json
```

Neither command was executed. Warming is a later data-only stage, after ranking resource
acceptance and approval; its exact command is in the preparation document.

## Safety confirmation

- No paper trading started; no paper account/run or full-authority claim created.
- No sizing cohort registered or activated.
- No real-money access enabled; no exchange credential/order action.
- No Railway Research/Context, scheduler ownership, gateway, DNS/OAuth or Shared Intelligence
  authority change. Old Railway Nimble remains untouched.
- Live profile remains v1 rehearsal; DU and Nimble stay OFF.
