# Runtime profile split and VPS autotrader lab

**Fork Prism operationally, not developmentally.** One codebase and one `main` branch. Two
explicit, versioned runtime profiles decide *where* existing workloads run. No trading logic,
hypothesis, frozen definition or scientific evidence changes. Nothing in this phase migrated,
activated, disabled or redeployed anything in production.

| | Research / Copilot | Autotrader lab |
|---|---|---|
| Profile | `research` → `prism_research_runtime_v1` | `autotrader_lab` → `prism_autotrader_lab_v1` |
| Digest | `8d6431ba…41e6d9` | `f898e2f0…ea102d19` |
| Target | Railway (or local) | VPS (or local) |
| Full-authority runtime id | `railway-prism-runtime` (the existing claim) | `vps-prism-autotrader-lab` |
| Rehearsal runtime id | `prism-research-rehearsal` | `vps-prism-autotrader-lab-rehearsal` |
| Database | its own `/data/prism.duckdb` | its own `/data/prism.duckdb` |

Code: `src/market_signal/ops/profiles.py` (profiles, authority, fail-closed resolution),
`src/market_signal/ops/lab.py` (lab identity, seed), `deploy/vps/` (systemd/bootstrap/release),
`scripts/vps_validate.py`, `scripts/vps_soak.py`, `tests/test_runtime_profiles.py`.

## 1. Profile selection and fail-closed startup

Three settings are validated together (`market ops profile`). Hostnames are never consulted.

| Variable | Values | Unset |
|---|---|---|
| `PRISM_PROFILE` | `research`, `autotrader_lab` | `legacy` (see below) |
| `PRISM_AUTHORITY` | `none`, `rehearsal`, `full` | `none` |
| `PRISM_DEPLOYMENT_TARGET` | `railway`, `vps`, `local` | refused under an explicit profile |

Any contradiction exits **78** (EX_CONFIG) and starts nothing: unknown profile, a target the
profile does not deploy to, `RAILWAY_ENVIRONMENT_ID` present with a non-Railway target,
`PRISM_AUTHORITY=none` with `PRISM_RUNTIME_ROLE=authoritative`, `rehearsal`/`full` with any
runtime id other than the profile's own, or a legacy flag that would *enable* something the
profile excludes. Legacy flags (`PRISM_MICROSTRUCTURE`, `PRISM_CONTEXT_GATEWAY`,
`PRISM_FREE_SOURCES`, `PRISM_NIMBLE_RUNTIME`) can only switch a profile process **off**, so
`PRISM_NIMBLE_RUNTIME=on` under `research` refuses rather than starting execution.

**Legacy.** With no `PRISM_PROFILE` the resolver reproduces the pre-split entrypoint flags
exactly and adds no refusal. Railway production is therefore unchanged until an operator sets
`PRISM_PROFILE=research`. Equivalence is tested two ways: all 3 × 4⁴ role/flag combinations
against a transcription of the old shell conditions, and the old (`36d7563`) and new
entrypoints executed side by side with stubbed binaries for four configurations including the
current production one (identical started processes, crontab and boot catch-up).

**Defence in depth.** The gate is enforced at every layer, not only at startup:

- Railway entrypoint and VPS `run_scheduler.sh` start only `market ops profile --shell` processes.
- systemd `ExecCondition=market ops profile --process X --condition` skips a disabled unit
  (exit 1, no restart loop); invalid configuration fails it (255).
- Every long-running program calls `profiles.main_guard()` first and exits 78
  (`RestartPreventExitStatus=78`).
- `run_job` refuses a job outside the profile *before* creating a cycle row or child process.
- `market paper nimble register|create|evaluate` need `execution_allowed` (full-authority lab or
  legacy). `kill` and `recover` stay available as safety/repair actions.
- `claim-authority` under an explicit profile accepts only that profile's runtime ids, so the
  lab can never claim `railway-prism-runtime`.
- The existing database authority claim still refuses a write from any other runtime id.

`PRISM_FREE_SOURCES=on` also changed behaviour *inside* code (legacy RSS de-duplication and
gateway-worker spool draining). Both now ask `profiles.free_sources_enabled()`, so an explicit
profile cannot silently double-collect or leave the free-source spool undrained.

## 2. Process and job matrix

Classes: **passive** = public data to spool files, never opens DuckDB; **ingest** = writes
this runtime's own DB; **ingress** = accepts external submissions; **execution** = paper
accounts/fills/lifecycle/reports; **retired_drain** = Paper v2 frozen exits (cannot open a
position).

| Process (systemd unit) | Class | research | autotrader_lab |
|---|---|:-:|:-:|
| `microstructure_collector` (prism-microstructure) | passive | ✔ | ✔ |
| `free_source_collector` (prism-free-sources) | passive | ✔ | ✔ |
| `context_gateway` HTTP/MCP (prism-context-gateway) | ingress | ✔ | ✘ |
| `context_ingest` gateway + free-source ingest (prism-context-ingest) | ingest | ✔ | ✘ |
| `free_source_ingest` (prism-free-source-ingest) | ingest | ✘ (context_ingest drains it) | ✔ |
| `nimble_worker` (prism-nimble) | execution | **✘** | ✔ |
| `universe_radar` Dynamic Universe Tier 0 (prism-universe) | ingest | ✘ (absent) | ✘ (**gated**) |
| `scheduler` supercronic (prism-scheduler) | ingest | ✔ | ✔ |

| Job | Class | research | autotrader_lab |
|---|---|:-:|:-:|
| `prospective` (forward, copilot, v1 paper drain, incubation) | research | ✔ | ✘ |
| `daily` (update, scan) | research | ✔ | ✘ |
| `microdir_daily`, `microdir_weekly` (Phase 24B checkpoints) | research | ✔ | ✘ |
| `oi`, `intraday`, `positioning`, `context_macro`, `context_news`, `microstructure` | data | ✔ | ✔ |
| `paper_v2_funding` (hourly settled funding; legacy name) | data | ✔ | ✔ (unconditional) |
| `paper_v2`, `paper_v2_brief` | retired_drain | ✔ | ✘ |
| `paper_nimble_brief` | execution | **✘** | ✔ |
| `backup` | maintenance | ✔ | ✔ |

Authority modes permit classes: `none` = passive only (no scheduler, no DB);
`rehearsal` = passive + ingest, jobs of class research/data/maintenance; `full` = everything in
the profile. Telegram sends, infra alerts and heartbeat pings are suppressed unless the
profile's own runtime is at `full` (a rehearsal ping on the production check would mask a real
outage). `TelegramClient.send` raises, so callers record an honest non-delivery.

The lab's data jobs are exactly the inputs the Nimble/event adapters read (verified from
`paper/v2/data.py`, `paper/nimble/data.py`, `context/snapshot.py`, `context/positioning.py`):
15m/1h bars, settled funding, Hyperliquid hourly OI, Binance OI/ratios, perp snapshots, macro,
events, microstructure. The lab therefore forms every trading input itself and has **no
runtime dependency on Railway**. Work (OAuth gateway) submissions stay on Railway in v1; the
lab's event inputs come from its own free public sources. That is a known, documented
difference from Railway's event cohorts, not a hidden dependency.

Dynamic Perp Universe v1 is on `main` (`cc311d0`, migration 31). Its tiers are declared per
profile: `research` = `absent`; `autotrader_lab` = `gated` (Tier 0 discovery = the
`universe_radar` process, Tier 1/2 warming = the collector's `--dynamic-universe` desired-set
mode, Tier 3 risk pins). No explicit profile starts any tier: `PRISM_UNIVERSE_STAGE=ranking`
or `warming` under `research` or `autotrader_lab` refuses (exit 78), exactly like
`PRISM_NIMBLE_RUNTIME=on` under `research`. Legacy (no `PRISM_PROFILE`) keeps the DU opt-in
semantics bit for bit: the stage flag starts the radar without a role test, and `warming`
switches the collector to the desired-set plan (tested against the `cc311d0` entrypoint).
Lab activation requires VPS validation and a new profile version (`prism_autotrader_lab_v2`).

## 3. Data ownership

| | Railway research | VPS lab |
|---|---|---|
| DuckDB | `/data/prism.duckdb` on the Railway volume | `/data/prism.duckdb` on VPS NVMe |
| Authority claim | `railway-prism-runtime` (unchanged) | `vps-prism-autotrader-lab` (only at cutover) |
| Spools / offsets | `/data/microstructure`, `/data/free_event_sources`, `/data/context_gateway` | the same paths on its own disk |
| Runtime state, locks, logs | `/data/*.runtime.lock`, `/data/logs` | `/data/state`, `/data/logs` |
| Backups | one verified local copy + off-host archive | one verified local copy + provider backup + off-host pull |

There is no shared or network-mounted database, no cross-host write and no shared spool. Three
independent mechanisms prevent dual authority: distinct runtime ids (enforced by the resolver
and `claim-authority`), the database claim guard, and microstructure ingest accepting only
spool records stamped with the claiming runtime id. A zero-authority (`scratch`) or rehearsal
collector's spool can never become lab production data.

## 4. Run identity and seed

`market ops lab-identity` prints the frozen experiment **`paper_nimble_dynamic_lab_v1`**
(digest `ec455164…`). Its base policies are computed from released code and tested against the
Phase 30 constants: Nimble `paperexecution_nimble_ec40a296…`, event
`paperexecution_event_12208f08…`, immediate/confirmed universes `…1caa53ff…`/`…4a76847c…`.
Railway runs `paperrun_nimble_a8aef8be…` (baseline), `…a5fafb2c…` (immediate) and
`…981fac86…` (confirmed) are **predecessors**: their evidence stays on Railway, and the lab
never continues them. New accounts start at lab activation. `lab.record_activation()` appends
experiment, run ids, seed id/frontier/source checksum, profile record, runtime id, host and
revision as a `lab_experiment_activated` runtime event. It exists for the later approved
activation and was not called against any real database.

**Seed policy `lab_seed_v1`** (`market ops lab-seed SOURCE TARGET --frontier ISO`, run with
`PRISM_RUNTIME_ROLE=scratch`). It reads a verified snapshot only (never the live DB; source
attached read-only, checksummed before and after), creates a **new, unclaimed** database,
copies nothing later than the frontier, records a manifest and a `lab_seeded` event, and
removes a partial target on failure. Every table has an explicit decision, and a test fails
when a new table lacks one.

| Seeded (complete history ≤ frontier) | Never seeded |
|---|---|
| assets, context_sources, `paper_nimble_policies` (frozen definitions) | all `paper_*` execution history (new frontier) |
| perp_intraday_bars, perp_funding, perp_oi_history, perp_snapshots, perp_bars | `runtime_*` (host state, authority claims) |
| context_hl_oi_hourly, context_ls_ratios, macro_observations | all `microstructure_*` (lab warmup starts at its own first authoritative minute: no backdating) |
| | all `universe_*` (Dynamic Universe ranking is prospective; the frozen policy self-registers) |
| context_events + their updates/asset links | copilot, incubation, lab research (research authority stays on Railway) |
| Phase 24B study/checkpoints/results + lab_software (descriptive science labels only, frozen at the frontier) | gateway/Work/free-source receipts (host ingress) |

The first design used 45-day windows. The real-data rehearsal showed that was wrong: funding
percentiles look back 120 days and scheduled events 60. Included inputs therefore carry their
complete history, and minimality is by category. **Rehearsal** (local scratch, a copy of
production backup `prism-20261009T013002Z`, migrated on the copy): 233.6 MB source → 90.2 MB
seed in 6.7 s, 1.03 GiB peak RSS. **Sufficiency:** Phase 23 context snapshots and
point-in-time 15m/1h bars for 6 coins × 3 instants were byte-identical between source and seed
(18/18). Evidence: `docs/evidence/runtime-profile-split/seed-rehearsal.json`.

Phase 24B science labels in Nimble observations are descriptive and never used in decisions.
On the lab they stay at the seeded checkpoint because the study's checkpoint authority remains
on Railway. That is deliberate: running the study on the lab would create a second divergent
record of a frozen study.

## 5. VPS deployment

```text
/opt/prism/releases/<40-hex commit>/   immutable release (git archive + REVISION + .venv, .installed marker)
/opt/prism/current -> releases/<rev>   atomic symlink switch (ln -sfn + mv -T)
/opt/prism/python/                     uv-managed CPython 3.12.3 (not /home: ProtectHome=yes)
/etc/prism/prism.env                   profile/authority/paths (root:prism 0640)
/etc/prism/secrets.env                 secrets, never in Git (root:prism 0640)
/data/{prism.duckdb, microstructure/, free_event_sources/, context_gateway/, raw/, backups/, state/, logs/, validation/}
```

`/data` paths match Railway, and no OVH-specific path appears in application code.
`deploy/vps/`:

| File | Purpose |
|---|---|
| `bootstrap.sh` | One-time and idempotent, as root on Debian 12/13 or Ubuntu 22.04/24.04: packages, `prism` system user (nologin), `/data` layout, pinned uv 0.12.0 (release checksum), CPython 3.12.3, supercronic v0.2.49 (same SHA-256 pin as the Dockerfile), key-only SSH (validated by `sshd -t` before reload), nftables inbound deny, journald caps, unattended security updates, chrony, UTC. Installs units without starting them. `--lock-root` disables root SSH once a sudo login is verified. |
| `deploy.sh` | From the dev machine: refuses uncommitted tracked changes, uploads `git archive <commit>`, runs that release's own installer. |
| `install_release.sh` | Builds the release in place, runs `market ops profile` with the new code (fail closed), takes a pre-deploy backup when a DB is authoritative/rehearsal, switches the symlink, reinstalls units, restarts each component individually. **The microstructure collector is not restarted** unless `--restart-collector`, which preserves websocket continuity across deploys. Keeps five releases and never deletes one a process still maps. |
| `rollback.sh` | Re-points to the previous or a named installed release. Code only: crossing a forward-only migration means restoring the pre-deploy backup. |
| `run_scheduler.sh` | The scheduler unit's main process: profile check, preflight, profile crontab, optional boot catch-up, `exec supercronic`. |
| `systemd/*.service`, `prism.target` | One unit per process (table above); `User=prism`, `ProtectSystem=strict`, `ReadWritePaths=/data`, `NoNewPrivileges`, per-unit `MemoryMax`, `Restart=always`/10 s, `RestartPreventExitStatus=78`. The scheduler uses `KillMode=mixed` and `TimeoutStopSec=900` so supercronic lets a running job finish. |
| `etc/` | `prism.env.example` (starts `autotrader_lab` / `vps` / **`none`** / `scratch`), sshd, nftables, journald, auto-upgrades, Caddy template (unused in v1) |
| `pull_backup.sh` | Off-host: newest verified backup over SSH/rsync, checksum versus server manifest, local verify + restore-test, gzip. |
| `validate/` | `replay.sh`, `prism-soak.service` |

**One scheduling authority.** supercronic under `prism-scheduler.service` is the only
scheduler: there are no systemd timers (tested), and the soak sampler is a supervised loop,
not a timer. One authoritative DB writer remains: every writer still takes the existing
runtime flock and DuckDB file lock. Docker, Kubernetes, Redis and Kafka are not used.

**Future sizing and LLM cohorts.** Accounts are independent run objects, each with its own
frozen policy and risk id; the worker already supervises several. Fixed-allocation,
fixed-fractional, volatility-normalised or risk/reward-aware sizing would be new risk-policy
ids running as parallel runs, and a thesis-generation cohort would be another run whose
thesis source differs. Neither changes the deterministic risk, execution, quote-safety or
accounting code. Nothing here hardcodes a sizing model into the profile or the account
layout, and no LLM or sizing code was added.

## 6. Validation procedure (before any authority)

1. `bootstrap.sh`, `deploy.sh`, then `systemctl enable --now prism.target` at
   **`PRISM_AUTHORITY=none`**. Only the two passive collectors start: no scheduler, no DB.
2. **Host** (`scripts/vps_validate.py`; run the same on Railway via `railway ssh` and compare
   with `compare A B`):
   - `host`: virtualisation, cgroup limits, RAM, swap, time sync.
   - `cpu --seconds 1800`: single and all-core rate, coefficient of variation, last-vs-first
     throttling, worst second versus median, steal fraction.
   - `disk`: sequential write with fsync, random 4 KiB write+fsync p50/p99, optional `fio`
     direct IO.
   - `duckdb --backup <real backup copy>`: realistic backup copy+fsync, `verify_backup`, write
     + `CHECKPOINT`, full count scan, `restore_test`, gzip archive time.
   - `network --seconds 3600`: Hyperliquid WS (six coins, trades + l2Book) connects,
     disconnects, messages/s, l2Book gap p99 and gaps beyond the collector's STALE_BOOK_S, ping RTT; it subscribes exactly as the collector does; HTTP p50/p95 and errors
     for Hyperliquid info, Binance fapi, Coinbase/Kraken status, FRED, SEC, DefiLlama, Auth0
     JWKS and optionally the Work gateway; ICMP to Hyperliquid.
3. **Replay** (`deploy/vps/validate/replay.sh OUT` at the same commit on both hosts):
   - Tranche A differential evidence digests, compared with
     `scripts/runtime_optimisation_a_compare_replay.py`.
   - Semantic suites: Nimble, event cohorts, Paper v2, microstructure, intraday, 24B features,
     runtime, profiles.
   - Timing-stripped digests of the Nimble and event-reaction synthetic loads.
   - Pass = identical digests and zero failures.
   - Dynamic Universe ranking/ledger determinism is covered by `tests/test_dynamic_universe.py`
     in the semantic suite.
4. **Passive soak ≥ 24 h, prefer 48 h**, still `none`, with `prism-soak.service` sampling every
   5 min (`scripts/vps_soak.py`). It records unit restarts, per-unit CPU/RSS, host CPU, steal,
   RAM, swap, disk, per-category /data growth, spool minute status (COMPLETE fraction versus
   expected minutes) and heartbeat ages. Then `vps_soak.py report`.
5. **Rehearsal soak ≥ 24 h**: claim a seeded copy with `vps-prism-autotrader-lab-rehearsal`,
   set `PRISM_AUTHORITY=rehearsal`, and restart. This adds ingestion, scheduler success (cycle
   status/duration), DuckDB growth, checkpoints, the daily backup and lock contention (busy
   read-only opens). Execution, notifications, heartbeat and ingress remain fenced.
6. **Restore test**: `pull_backup.sh` to the archive machine, plus a provider snapshot restore
   onto a scratch instance once.

**Pass criterion:** no material regression versus Railway in data quality (WS completeness,
quote age, source freshness) or execution safety (fencing, replay identity), with adequate
headroom (RAM ≥ 40% free at peak, no swap pressure, steal < 5% sustained, disk ≥ 70% free,
fsync p99 < 50 ms, zero unexplained restarts). The VPS does not need to be faster. **Not
validated by a short test:** the soaks above are the minimum.

## 7. Migration rehearsal, cutover and rollback (later, separately approved)

1. Take a verified Railway backup and archive it off-host, then build `lab-seed` with an
   explicit frontier.
2. Install the seed on the VPS and `claim-authority --runtime-id vps-prism-autotrader-lab`.
3. Set `PRISM_AUTHORITY=full`. Lab microstructure production starts at its first
   authoritative minute.
4. `paper nimble register/create` the approved lab runs, then `record_activation`.
5. Separately, with Railway explicitly authorised: set `PRISM_PROFILE=research`,
   `PRISM_AUTHORITY=full`, `PRISM_DEPLOYMENT_TARGET=railway` and redeploy. Nimble stops on
   Railway, and its runs and evidence remain untouched.
6. **Rollback.** Lab: set `PRISM_AUTHORITY=none` and stop `prism-nimble`; the lab DB and
   evidence are kept. Railway: unset `PRISM_PROFILE` and redeploy (legacy is bit-for-bit the
   old process set). No step deletes evidence.

## 8. Core-six microstructure on Railway: **keep collection**

Measured cost is low:

- Collector: about 174 MiB RSS, 0.53% of one CPU, 22.4 msg/s, 11.7 KB/s.
- Database: `microstructure_minutes` 41,280 rows in 38 data blocks (about 10 MB) over about
  4.8 days, roughly 2–2.5 MB/day, at 8,630 rows/day (≈ 6 × 1440).
- Raw journal: 36.4 MB/day gzip with 1-day local retention.
- Spool: about 16 MB/day.

Research value is direct. The frozen Phase 24B prospective study (`sstudy_16c5f72d…`,
registered on Railway, WARMUP) is evaluated only on these minutes, and its validation window
would be destroyed by stopping collection. The expensive part was downstream evaluation, which
is Nimble, and Nimble is excluded from `research`. Recommendation: keep the collector, the
15-minute ingest and the 24B checkpoints on Railway, and remove execution evaluation.

## 9. Phase 30 event cohorts: recommendation

Evidence:

- Each cohort is a full account re-evaluating the original 28 hypotheses plus three event
  hypotheses, so 28 of 31 identities are shared with the baseline.
- `paper_nimble_opportunities`, about 21,000 rows/day across the three runs, is the largest
  growing table (432 of 822 table data blocks; the Nimble/event family holds 477), and two
  of the three runs are the event cohorts.
- First audit (Oct 10): one observed event, zero eligible, zero event trades. The regulatory
  pair is dormant by design.
- Quiet event classification is cheap (1,000 unactivated polls in 0.027 s); the cost is the
  duplicated technical evaluation.

As controls for the dynamic trader they add nothing the baseline does not already provide.
Their immediate-versus-confirmed comparison is confounded by the shared 28 hypotheses, and at
the observed event rate it has almost no power.

**Recommendation:** do **not** create event cohorts at lab activation. Keep their Railway
history and frozen definitions unchanged, and let them run on Railway until the production
cutover approval (nothing is disabled now). Later, integrate event hypotheses into a broader
thesis framework as event-gated signals of the single lab account, or as one event-only
account that does not duplicate the 28. The lab's experiment identity already names both event
definitions as base policies, so either path is a new run, never a redefinition.

## 10. Gateway and domain

**Current:** Work → `https://prism-runtime-production.up.railway.app/mcp`, Railway TLS ingress,
port 8787, Auth0 RS256 with fixed issuer/JWKS, exact audience, `context:submit`, and
subject/client allowlists. The research profile keeps it.

**Lab v1:** no gateway and no public port (SSH only).

**Recommended before any move:** a user-controlled hostname (for example `context.<domain>`)
CNAMEd to Railway, with a deliberately registered Auth0 API audience for that hostname and Work
reconnect/authorisation tested. A later move then changes DNS, not identity. The Caddy template
in `deploy/vps/etc/` is for that later, approved step. OAuth was not touched.

## 11. Desktop read model

Do not build two desktop backends. The future desktop reads research/copilot views from the
research runtime and autotrader views from immutable lab snapshots: the existing
paper-snapshot pull pattern, over SSH for the VPS. Later, a small read-only aggregator API can
merge both by profile identity, which every runtime cycle, deploy record and seed now carries.
Not built here.

## 12. Backups

- **Railway research:** unchanged. One verified local recovery backup (`BACKUP_KEEP=1`) and the
  verified off-host archive (`pull_backup.sh` / `scripts/backup_archive.py`).
- **VPS lab:** the same daily `backup` job (one application-consistent verified copy;
  `verify_backup` no longer requires a v1 paper run under `autotrader_lab`, tested), plus the
  provider's standard daily backup or snapshot, plus `deploy/vps/pull_backup.sh` off-host with
  a local restore test.

No multi-copy rotation.

## 13. OVH target specification

| Item | Requirement |
|---|---|
| Plan | OVHcloud VPS-3 class: 6 vCore, 12 GB RAM, 100 GB NVMe; UK or France standard region (lower latency to the UK operator; venue latency to be measured, not assumed) |
| OS | Debian 12 (preferred) or Ubuntu 24.04 LTS, x86_64 |
| Price | Published floor £9.01 ex-VAT, so budget about £10–12/month inc. VAT. Verify the exact month-to-month checkout before purchase. |
| Included | Standard previous-24 h backup; no extra IP, block disk or premium backup |
| Expected lab load | Nimble about 1–2 GB peak, collectors about 0.4 GB, ingest about 0.25 GB, jobs ≤ 2 GB: comfortably within 12 GB. CPU well under 1 core average at six markets. Disk: 90 MB seed plus tens to low hundreds of MB/day, so 100 GB lasts well over a year with the off-host archive. |
| Accept only if | §6 pass criterion holds after both soaks |

## 14. Expected Railway savings once `research` is applied (estimate, not a bill)

From measured production samples:

- **Removed process:** the Nimble worker. Its intermediate RSS was 970 MiB with a 1.94 GB
  high-water mark. CPU was 15.35 CPU-s/min (0.26 CPU) before Tranche A, which cut 28–38%
  offline, so about 0.16–0.26 CPU of the 0.69 CPU-equivalent sample, roughly 25–35%.
- **Removed job:** `paper_nimble_brief`.
- **Storage:** Nimble/event tables are about 58% of table data blocks. Opportunities alone grew
  about 50 MB per ~20 h interval, so live-DB growth falls by roughly 50–100 MB/day, and the
  backup copy and off-host transfer shrink accordingly.
- **Money:** with Railway rates ($20/CPU-month, $10/GB-month RAM, $0.15/GB-month disk), about
  $3–5 CPU + $10–19 RAM + $0.5–1 disk (growing) ≈ **$14–25/month** less usage. The bill
  effect depends on usage above the plan minimum. A 7-day post-change billing window is
  needed to confirm it.
- **Lock contention:** the research DB loses its most frequent writer (the minute-cadence
  Nimble ticks).

Exact change to apply later (not applied): Railway service variables
`PRISM_PROFILE=research`, `PRISM_AUTHORITY=full`, `PRISM_DEPLOYMENT_TARGET=railway`, then
`deploy/railway/deploy.sh <commit>`. Remove `PRISM_NIMBLE_RUNTIME` if it is set to `on`, because
it would refuse.

## 15. Production state

Nothing was deployed, migrated, claimed, activated or disabled. No DNS, OAuth or Railway
variable was changed, and no VPS was provisioned. The only production-derived data used was a
local copy of an already archived October 9 backup, in a scratch directory. Railway continues
running the legacy combined runtime.
