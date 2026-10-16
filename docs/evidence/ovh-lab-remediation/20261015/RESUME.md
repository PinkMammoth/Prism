# Closed target gate — October16 morning

Formal verdict: REHEARSAL PASS WITH SPECIFIC LIMITATIONS. Full report is
`docs/OVH_LAB_TARGETED_REMEDIATION.md`; DU plan `docs/DYNAMIC_UNIVERSE_LAB_V2_PREPARATION.md`.
Window18:25:28.271856 Oct15→01:40:01.048838 Oct16 UTC (7h14m32.777s),91/91 scheduled cycles OK,
2natural macros,1natural backup; source82f7c8... restored146/146 both on-host and off-host.
Normal off-host helper completed and source receipt acknowledged, no pruning by this task.
Current core PIDs unchanged625/612/609/2205; v1/rehearsal, no paper/DU/sizing/authority events.
6 read-only sampler overlaps with writers;0 job lock failures. Explicit provider partials/gaps in report.
Independent Research backup currently retries restic check(status11), observed read-only, NOT fixed.
No speculation about exit code meaning. Do not touch Research authority or expand scope silently.
DU proposal deliberately validated=false: current hostfree94.562GB/RAM11.197GB,13.734GB above70%
floor+7GBreserve,~45.8days at.30GB/d warning; whole-host steps and Research impact need resource
admission before activation. No new24h host clock. V2 package recovered to persistent
`.worktrees/ovh-lab-v2-prepared` at eee205f... from bundle; prior/tmp checkout disappeared on PC shutdown.
No activation approval yet. Final next commands and conditional admission in report. Safety unchanged.

---
Earlier handoff history (superseded pending statements retained for provenance):

# Running remediation extension — operator handoff

User explicitly rejects a new 24h rehearsal. Preserve the 26h base, use 6–12h clean extension.

## PC-independent handoff (20:30 UTC October 15)

User must power off PC soon. Confirmed they can close the session now. Local polling stopped;
VPS observer PID3462 continues. Added verified `prism-lab-remediation-close.timer`, active/waiting,
next 2026-10-16 01:41:00 UTC. Root-owned scripts under `/etc/prism/lab-remediation/`.
One-shot closure after observer end, TimeoutStartSec1800/MemoryMax1G/CPUQuota100%/Nice10/
ProtectSystem strict, only validation directory writable. No deployment/activation capability.
It writes `/data/validation/lab-remediation/closure.json` after gate checks, natural FRED/provider
checks, normal nightly manifest/checksum and fresh scratch restore/all146 fingerprints/authority
guards, host/net/journals, spool quality and cursor/DB proof. Keeps evidence, deletes scratch.
Preflight `closure-check-now.json` passed20:29:42.872462 with manual18:22 snapshot146/146 and
all host/helper commands successful. Initial audit ISO journal timestamp parser issue fixed;
not a production job defect, core/observer untouched. No updated verdict yet.
Installation evidence: unattended-closure-install.txt. Retrieve final.json + closure.json first on
return; inspect error_type/checks_completed/targeted_gate, actual concerns/gaps, not just exit status.
No overnight local process is required. Off-host nightly pull/receipt waits until workstation
returns; on-host unacknowledged source is preserved by governed retention. Do not confuse
on-host fresh restore proof with new independent off-host recovery evidence.
First natural macro proof now saved: fred-first-natural-proof.json includes19:12 provider OK.
tools/fred_proof.py now includes provider counters/errors boolean; tools/restore_proof.py accepts
optional receipt filepath argument (default manual proof). Use installed venv interpreter remotely,
local .venv/bin/python; system Python cannot import application modules.

## Earlier remediation details

- Live Lab hotfix: 9acb42a94d8888a2e712490b421912739a02bff6, parent 530ba0d. Backup registration/helper fixed. V1 profile f898e2f0..., rehearsal identity unchanged.
- FRED key existed locally in .env, absent from empty intended VPS secrets.env. Existing value provisioned by encrypted stdin to root:prism 0640 secrets.env. Only scheduler restarted to load it; PID 2205. Collectors/ingest not restarted.
- Manual exact macro passed 18:13:38–18:13:55 UTC, four actual updates. Secret scans false.
- Exact scheduler backup cycle passed 18:22:00–18:22:08. Source daily/prism-20261015T182201Z.duckdb, SHA 5d8bdb70040a0ee1e0e7d2529c5aa4c53f3c514f88dc65a6de0c8b7283396077. Standard fixed pull and fresh scratch restore passed all 146 fingerprints; export receipt acknowledged. Old orphan and predeploy snapshots preserved off-host then normally pruned after receipts.
- Window START 2026-10-15T18:25:28.271856Z; END planned 2026-10-16T01:40:00Z (~7h14m32). DO NOT CONCLUDE EARLY. Natural macro slots 19:12 and 00:20; ordinary backup 01:30. Need multiple natural macro successes, backup success, >=6h clean.
- Read-only transient unit prism-lab-remediation-monitor, PID 3462, script /data/validation/lab-remediation/targeted_monitor.py. It only observes; no second job scheduler. Its samples.jsonl/latest.json/window.json/final.json are in that directory. final.json appears only at 01:40.
- Check progress: ssh -o BatchMode=yes debian@178.33.95.139 'sudo /usr/bin/python3 -' < tools/read_progress.py. Outputs only safe summary. Base sampler still runs.
- User clarified no OVH CLI/API credential exists. Edge configured in OVH Manager. Do NOT create credentials or change networking. Record unavailable control-plane re-attestation as known accepted limitation, manual Manager verification if needed; continue. Host nft/listeners intact. Workstation 80/443 connect tests also accept RFC5737 dummy address then time out: unsuitable to infer edge state.
- Prepared V2 NOT LIVE: eee205f24d5d9ef3cd71c32499f13ec2e2fbb8fd in /tmp/prism-lab-v2-prepared. Bundle lab-releases.bundle contains hotfix+v2, parent 530ba0d required (exists repo). V2 digest e7571ad1a2a35fb42dc8c1ea77759e31887a1c7679445561664c1dee68c12207. 148 exact prepared profile/DU/activation-guard tests and 8 Paper v2 CLI tests passed. Source working tree changes also exist, preserve unrelated preexisting dirt.
- V2 data-only, new selector autotrader_lab_v2, excludes Nimble/execution, rejects full, keeps same rehearsal runtime. It wires VPS collector warming and fences direct CLI stage bypass/account creation. No scientific gates changed. Activation script guarded by completed extension+budget+identity. No activation authorization exists: keep DU OFF, all paper OFF, sizing OFF.
- Documents: docs/OVH_LAB_TARGETED_REMEDIATION.md (pending), docs/DYNAMIC_UNIVERSE_LAB_V2_PREPARATION.md (reviewable plan). Old base report has supersession note, not erased historical failures.

At/after end:
1. Copy /data/validation/lab-remediation/{window.json,final.json,samples.jsonl,latest.json} into evidence; inspect all concerns, exact times/counts. No fabricated elapsed validation.
2. Host final audit using prior tools/host_audit.py; DB final audit and bound cycles to start/end; collect journal/kernel errors since start, compress. Verify same expected PIDs/authority, fresh ingestion, no paper or DU rows.
3. Run tools/fred_proof.py via sudo /opt/prism/current/.venv/bin/python -; verify two natural scheduled macros and provider status, no secret in logs/raw. It scans without printing key.
4. Run standard PRISM_VPS_HOST=... deploy/vps/pull_backup.sh to a receipt nightly-backup-offhost-pull.json. Fresh scratch/all-146 proof for the 01:30 normal artifact, preserve manual proof. tools/restore_proof.py currently defaults to real-backup-offhost-pull.json; adapt receipt parameter for nightly.
5. Reuse earlier spool_compare and ingest_recovery_proof tools for explicit partial/missing minutes and cursor/DB correspondence; don't hide gaps.
6. Refresh resource planning budget with current RAM/disk/load, base+prior DU evidence, backup transients, 3GB reserve for future baseline paper. Preserve warning/stop thresholds in preparation doc. Keep no execution authorization.
7. Update pending report to exactly one verdict. With clean extension and accepted edge limitation, REHEARSAL PASS WITH SPECIFIC LIMITATIONS is appropriate; explain 26h base + actual targeted duration, not second 24h. If any real material defect, don't waive it.
8. Final evidence hashes and git diff --check. No broad retesting absent changes/failures. Final response requested 16 headings in latest user request, exact next command/config ready but NOT executed. No Research/Railway/DNS/OAuth changes.
