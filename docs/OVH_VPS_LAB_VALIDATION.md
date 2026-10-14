# OVH VPS-3 lab validation: 24-hour verdict, rehearsal start, Railway Nimble shutdown

Date: 2026-10-14. Evidence: `docs/evidence/ovh-vps-lab/`. Runbook:
`docs/RUNTIME_PROFILE_SPLIT_AND_VPS_LAB.md`.

> **24-hour minimum completed; preferred 48-hour zero-authority soak intentionally curtailed by
> user decision.**

## Verdict

**PASS — 24-HOUR MINIMUM.** The minimum gate passed on every criterion. The 48-hour soak was
deliberately not completed, by the user's decision on cost grounds. That shortened duration is a
documented limitation, not an observed fault on the VPS.

## Host

| | |
|---|---|
| Plan | OVHcloud VPS-3 2027: 6 vCore (Intel Haswell, no TSX, KVM), 12 GB RAM, 100 GB, 2 Gbps |
| Region | Gravelines (GRA), France; `vps-778d603b.vps.ovh.net`, 178.33.95.139 |
| OS | Debian 12, kernel 6.1.0-51-cloud-amd64 |
| Cost | **£12.72/month inc VAT**, monthly term (≈6% above the £10–12 guide; not material) |
| Zero-authority deploy | `401301f` at 2026-10-13T15:35:05Z; soak sampler from 15:36:26Z |
| Current release | `530ba0d` (main) at 2026-10-14T15:41:25Z |

Hardening on the host:
- `prism` system user with no login shell.
- SSH: key-only; password and keyboard-interactive login off; root login off (`--lock-root`); MaxAuthTries 3.
- Host firewall (nftables): inbound policy drop except tcp/22, which is rate-limited.
- OVH Edge Network Firewall: on since 2026-10-13 17:10Z. Rules: TCP established, tcp/22, ICMP, UDP from source port 53 and 123, then refuse all IPv4. It was proven to block a port the host had opened.
- No database or other public port; journald capped at 500 MB / 14 days.
- Unattended security updates, chrony, pinned uv 0.12.0 / CPython 3.12.3 / supercronic v0.2.49.
- KVM console recovery tested.

## 24-hour zero-authority soak (2026-10-13T15:36:26Z → 2026-10-14T15:36:26Z)

| Measure | Result |
|---|---|
| Samples | 288 at 300.07–300.22 s intervals; 0 gaps; last sample 15:32:09 (23.93 h of samples) |
| Service continuity | microstructure collector, free-source collector and sampler: **0 restarts**, 0 failed units |
| Kernel/system | 0 kernel errors, 0 OOM kills, no reboot, 0 error-level lines from any Prism unit |
| CPU | mean busy 2.5%, p95 0.44%; steal mean 0.005%, max 0.11%. The peak (100%, load 6.1) was the deliberate 30-minute benchmark at 15:40–16:10 on 13 October |
| RAM | ≥ 10.4 GiB available throughout (min 90.8% of 12 GB); swap 0; collector peak 198 MiB, free-source collector 111 MiB |
| Clock | chrony 0.17 ms from NTP, RMS 0.07 ms |
| Prism data growth | microstructure 63 MB/day, free sources 19 KB/day, nothing else (no DB at authority `none`) |
| Disk | 92 GB free (93%) at the end |

**Disk cleanup annotation.** At 2026-10-14T14:43:45Z I deleted my own validation leftovers:
- `/data/validation/prism.0.0` (512 MiB), left by a manual `fio` run;
- `/tmp/pytest-of-debian` (4.2 GB), left by the replay suite.

That causes a **5.04 GB downward step** in the disk-usage series. It is validation cleanup, **not
Prism negative growth**. Prism's own growth comes from the per-category `/data` counters, which
never included those files.

**Unattended upgrade.** At 06:33Z the automatic updates installed a new kernel image plus
openssl/libssl3. Nothing was disrupted: there was no disconnect around that time, and Prism's
uv-managed Python bundles its own OpenSSL. **A reboot is pending** to load the new kernel; it
should be done before full authority.

## Exact-window collector comparison with Railway (2026-10-13T15:36Z → 2026-10-14T15:36Z, 1,440 min × 6 coins)

| | VPS | Railway |
|---|---|---|
| COMPLETE minutes | **8,592 (99.44%)** | 8,556 (99.03%) |
| PARTIAL / missing | 48 / **0** | 84 / **0** |
| Disconnects | 8, all Hyperliquid's scheduled "Expired" close | 9, including 2 abrupt `ConnectionClosedError` |
| Downtime: total / longest | 11.4 s / 1.5 s | 19.3 s / 4.5 s |
| Late prints / collector restarts | 0 / 0 | 0 / 0 |

From the one-hour simultaneous network test on 2026-10-13, 16:26–17:26Z:
- **Hyperliquid websocket:** both hosts had 0 disconnects at 21.32 msg/s and 0 l2Book gaps over 10 s. The l2Book gap p99 was 0.93 s on the VPS and 0.87 s on Railway.
- **HTTP:** every probe (HL info, Binance fapi, Coinbase, Kraken, FRED, SEC, DefiLlama, Auth0 JWKS) returned 0 errors on both hosts.
- **Firewall:** the edge firewall was enabled mid-test, at 17:10Z, with no disconnect.

**The VPS shows no degradation and is slightly better on continuity.**

## Earlier stage results

| Stage | Result |
|---|---|
| CPU, 30 min sustained | steal 0.025%; last/first 0.991 (no throttling); 5.98× scaling across 6 vCPUs; all-core variation 3.3% |
| Disk (corrected validator, `530ba0d`) | fsync p99 0.66 ms; **fio direct-IO 26.2k / 26.2k IOPS, write p99 0.91 ms** |
| DuckDB (221 MB synthetic, no production data) | copy+fsync 0.28 s; checkpoint 0.49 s; restore verified |
| Replay | `replay.json` **byte-identical** to the local reference. Tranche A digest `77cad631…` (29/29 fixtures, 1,008 table digests); semantic suite 450 tests, 0 failures, 7 skipped (same as reference); benchmark digests identical. Covers the Dynamic Universe ranking tests |

## `fio` validator fix

- **Commit:** `0823905`, merged as PR #27.
- **What changed:** `_cmd` now takes a timeout. `fio` gets 120 s, and a failed run is now recorded as an explicit error instead of being silently dropped.
- **Rerun on OVH at `530ba0d`:** passes, with `fio_randrw_4k` = 26,197 read / 26,168 write IOPS and write p99 905 µs. No test files were left behind.

## Rehearsal (live since 2026-10-14T15:44Z)

| | |
|---|---|
| Profile / authority | `autotrader_lab` (`prism_autotrader_lab_v1`, digest `f898e2f0…`), **rehearsal**, role authoritative |
| Runtime identity | `vps-prism-autotrader-lab-rehearsal`; claim event `rtev_e3856a9a…` (no previous claim) |
| Seed | `labseed_22024329c0e8491f`, policy `lab_seed_v1` (digest `e4d467b9…`, the approved policy); frontier 2026-10-12T12:43Z; 91.5 MB; SHA-256 `75bf353b…` |
| Seed source | local verified pre-deploy backup `prism-20261012T124347Z-pre-deploy-8f9bc34` (SHA-256 `49546a18…`), migrated to schema 31 on a copy |
| Running | microstructure + free-source collectors, free-source ingest, scheduler (data jobs + 01:30 backup) |
| Not running | Nimble, Dynamic Universe radar, context gateway and its ingest (all skipped by profile) |
| Fencing | `execution_allowed=false`, `external_actions_allowed=false`; no `paper_nimble_brief` in the crontab |
| Initial health | 10/10 cycles `ok`; 0 restarts or failures; bars, funding and positioning caught up to 16:00–16:15Z; lab microstructure starts at its first authoritative minute (15:44Z); DB 107 MB; 10.7 GB RAM free |

Seed contents:
- **Included (18 tables):** market data, frozen policies, public context events, and the Phase 24B labels.
- **Excluded:** all paper execution history, runtime/authority state, microstructure, Dynamic Universe, copilot, incubation and gateway/Work receipts.
- **Paper rows:** none apart from `paper_nimble_triggers`, a data-plane wake-up outbox that only the fenced execution worker consumes.

**Why the seed came from an existing local backup.** A fresh download of the production backup
was **blocked by the safety classifier** (personal-data handling) and was not retried. The seed
is therefore built from the newest verified backup already on the operator machine, so its
frontier is two days old. The data jobs backfill from public APIs.

## Railway reduction

**Change.** At 2026-10-14T16:01Z I set `PRISM_NIMBLE_RUNTIME=off` on `prism-runtime`. This is a
legacy switch-off flag. Deployment `79722dc1` rebuilt **the same `8f9bc34` snapshot**: no newer
code and **no migration** (schema still 30; authority claim unchanged).

**Revert.** Delete the variable.

| | Before (15:49Z; 51 h process averages) | After (16:25–16:33Z) |
|---|---|---|
| Nimble worker | 0.45 cores average, ~2/3 of Railway CPU; RSS 328 MB → 2,030 MB | **not running** |
| Container memory | 4.35–5.09 GB; **peak hit the 8 GB cap** | ~1.6 GB at rest; 4.0 GB peak since restart (boot catch-up included) |
| Container CPU | 0.69 cores (51 h average) | **≈0.38 cores (estimate: one 3-minute window including the :31 job)** |
| Throttling | 7,566 periods (≈10,100 s) | 73 periods since restart |
| Volume | 3.20 / 4.84 GB (67%) | 3.26 / 4.84 GB (68%) |

**Retained on Railway, and why:**

| Workload | Why it stays |
|---|---|
| Microstructure collector + 15-minute ingest | Phase 24B governed science continuity |
| Context gateway + worker | Work ingestion over OAuth; Shared Intelligence |
| Free-source collector | Context intelligence |
| `prospective`, `daily`, `microdir_*` | Research, Copilot, incubation, 24B checkpoints |
| `oi`, `intraday`, `positioning`, `context_*`, `paper_v2_funding` | Research data |
| `backup` | Backups |

`paper_nimble_brief` (00:14) also remains: the legacy crontab can't drop it without a code
deploy. It only reads.

**The three Nimble runs were not killed.** They are ACTIVE, flat, with no open position, and their
evidence is untouched. That matches runbook §7.5: "Nimble stops on Railway, and its runs and
evidence remain untouched." Two kinds of obligation are frozen, not lost:
- 6 closed trades whose funding accounting is not yet final (from 10–13 October);
- 72 pending research curves per run.

The 15-minute microstructure job keeps ingesting the quotes those curves need. Note also that, in
the deployed code, a *killed* run keeps recording opportunities and labels their rejections
`CATASTROPHE_GUARD`; that is why `kill` was not used. Settling the frozen items, and a clean Nimble
retirement mechanism like Paper v2's, are proposed follow-ups.

**Billing.** No billing saving is claimed until Railway reports it. As an estimate only: Railway CPU
should fall by roughly half, and memory by roughly 2–3 GB at rest.

## Safety

- No real orders, exchange credentials or live authority anywhere.
- No production account migration and no paper run created.
- No sizing cohort registered or activated (`dynamic_lab_risk_sizing_v1` untouched).
- No Dynamic Universe ranking, warming or execution (the radar is gated and the collector is in core-six mode).
- No historical evidence deleted. The Railway Nimble runs, Paper v2 and all Railway data are intact.
- No DNS, OAuth or gateway change; no production DB migration; no production DB copy on the VPS (seed only).
- **Incident (resolved):** during the Railway audit, the value of `PRISM_CONTEXT_GATEWAY_TOKENS` was printed into the session transcript by a filtering mistake. It never reached any evidence file or commit.
  - **Rotated at 2026-10-14T17:14Z:** a new 64-character `secrets` token under the same identity (`prism_activation_cli_v1`), set via stdin and loaded by Railway deployment `3ef1c302` (same `8f9bc34` build; Nimble still off). No values were printed.
  - **Consumers:** Railway `prism-runtime` was the only stored holder. The CLI smoke command takes the token at invocation; ChatGPT Work uses OAuth, and the in-container health monitor uses no token.
  - **Proof:** on `POST /context/v1/events` with a deliberately malformed body (so no event is created), the old token was authenticated before rotation (400 `schema_rejected`). After rotation the new token is authenticated (400) and the old and random tokens are rejected (401 `auth_rejected`).

## Next gates

| Step | Evidence required first |
|---|---|
| **Dynamic Universe ranking/warming** | ≥ 24 h of rehearsal with every cycle `ok`, 0 unexplained restarts, the 01:30 backup verified and restore-tested, and DB growth measured. Then a new profile version `prism_autotrader_lab_v2` (runbook §2) with its own approval; warming then needs its natural readiness/warmup |
| **Baseline paper authority (`full`)** | The rehearsal soak above; the pending kernel reboot done and the collector recovering cleanly; an off-host restore test (`pull_backup.sh`) and one OVH snapshot restore; then explicit approval to claim `vps-prism-autotrader-lab`, register/create the lab runs and call `record_activation` (runbook §7) |
| **Further Railway reductions** | Deploy `main` to Railway with `PRISM_PROFILE=research` (removes `paper_nimble_brief`). Needs approval for migration 30→31 on production and a pre-deploy backup with volume headroom (1.5 GB free against a 1.6 GB DB, so make room first). Separately: a Nimble retirement mechanism to settle the frozen research/accounting and mark the runs RETIRED |
| **Prospective sizing cohort** | Every prerequisite in `docs/PROSPECTIVE_SIZING_COHORT_V2.md` §prerequisites, starting from lab baseline-only activation plus ≥ 24 h, 30 eligible source opportunities, and the manifest/authority checks. Not authorised by this work |
