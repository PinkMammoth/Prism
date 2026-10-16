# Google Drive off-host backup and recovery — Prism Research

**2026-10-16 operational follow-up:** the first post-reboot unattended check was blocked by
a stale pre-reboot repository lock. Stale-only `restic unlock` and a fresh full unattended
cycle closed the alert at08:13:18 UTC: full data check, Drive restore,146-table verification
and safe retention all succeeded. Keys/repository are unchanged. See
[Research closure and ranking admission](OVH_DU_RANKING_ADMISSION.md#research-alert-exact-cause-and-closure).
Status11 alone must not be labelled corruption or a Drive outage. Before unlocking, establish
lock time/host/PID and absence of live ownership; never use `--remove-all` to bypass legitimate work.

2026-10-15. **No authority transfer.** Railway remains authoritative for Research; OVH
Research remains non-authoritative/read-only; OVH Lab rehearsal is unchanged. This clears
only the independent off-host backup blocker recorded in
[OVH Research namespace validation](OVH_RESEARCH_NAMESPACE_VALIDATION.md).

Evidence: [google-drive-backup/20261015](evidence/google-drive-backup/20261015/).

> Google Drive only counts as a backup once Prism has restored from it. It has: a
> generation was uploaded, every local copy was made unavailable, and the snapshot was
> recovered from Drive into a fresh path and passed the full Research read-parity suite.

## Mechanism

| Item | Value |
|---|---|
| Upload / encryption | restic **0.19.1** (repository format v2: AES-256 + Poly1305 authenticated encryption, compression, dedup) |
| Drive transport | rclone **v1.75.1** `drive` backend, used by restic via `rclone serve restic --stdio` |
| Binary provenance | Upstream release SHA256SUMS **GPG-verified** (restic: Alexander Neumann `CF8F18F2…D3F7A907`; rclone: Nick Craig-Wood `FBF737EC…FF3B54FA`). Installed SHA-256 restic `20d41426…e639`, rclone `f66d8c1d…a4c0`, pinned in `install_research_backup.sh` |
| Install location | `/opt/prism/research-backup/tools/bin/` (root 0755; not apt, not shared with Lab) |
| Google account | Workspace account `matt@secure-talent.co.uk` (pooled 2.2 TB, ~1.07 TB free at setup) |
| OAuth client | Owner's own Google Cloud project, **Internal** user type, **Desktop app** client, scope **`drive.file` only** |
| Repository ID | `148cfeaa9c717db0e0d6d1c304891e44565a1ebc0d343dd0be3181e1709c6742` |

### Why these choices

- **Existing convention.** The Research namespace already had a reviewed restic adapter
  (encrypted repository, exact snapshot, data check, scratch recovery, host/tag-scoped
  retention). This tranche adds an `rclone:` repository scheme rather than a new tool.
- **`drive.file` scope.** Google only lets the app see/modify files it created. Prism
  cannot read, list or delete any other Drive content; retention can only ever touch the
  Prism backup folder. Drive's per-app scoping also means **recovery must use the same
  OAuth client** (or a manual web download — see custody).
- **Own client, Internal.** rclone's docs state its shared client ID "is being retired and
  will stop working during 2026". A first authorization with the shared client was
  discarded *before* anything was written to Drive, so no backup is tied to it. An Internal
  Workspace app needs no publishing/verification and its grants do not expire after 7 days
  (the Testing-mode limit for External apps).
- **Workspace already existed**; it was not introduced for this. No paid service, Google
  Cloud compute or PC relay is involved: upload, verification and restore run on OVH.

## Drive layout

```text
Prism/                          (created by the app; drive.file)
└── backups/
    └── research/
        ├── restic/             encrypted restic repository (config, keys, data, index, snapshots)
        ├── manifests/          plaintext manifest per retained generation (non-secret metadata)
        └── RECOVERY.txt        non-secret pointer to this document
```

"Daily" and "checkpoint" generations are restic snapshots in one repository; weekly and
monthly checkpoints are the snapshots retention keeps. Separate folders would defeat
dedup and add a second retention mechanism, so they are not used.

## Backup artifact

Each generation is the existing verified local workflow output (`backup_sealed`): one
gzip archive of the sealed DB and a manifest containing logical and compressed SHA-256,
sizes, schema/engine version, every relation fingerprint, source runtime/frontier/software
revision, authority claim (as provenance), mode and creation timestamp. The restic
snapshot holds exactly `manifest.json` + `research.duckdb.gz`; no scratch/test DB is uploaded.

| Field | Gate generation |
|---|---|
| Generation / restic snapshot | `generation-20261015T171356752321Z` / `28eea1d3c22438cd…` |
| Second (automation) generation | `generation-20261015T172517321985Z` / `c67a73c4de6f7b6b…` |
| Source DB | `/var/lib/prism-research/import/prism.duckdb` (non-authoritative candidate, read only) |
| Logical size / SHA-256 | 1,646,276,608 / `dbaa5a3c31ebd738cca69bbd714263f4583722331f515ee2d239d4be77d9726f` |
| Compressed size / SHA-256 | 135,614,775 / `023c3240e029eb87686905a1a9dfe3475397ba173ac0d7f9a38b1473c3e7892b` |
| Schema / engine / relations | 31 / DuckDB 1.5.5 / 146 (142 source + 4 empty schema-31 additions) |
| Frontier (last finished cycle) | 2026-10-15 11:31:40.099798 UTC |
| Source runtime / software | `railway-prism-runtime` (claim 2026-10-04 17:46:19Z, provenance only) / `8f9bc347…` |
| Backup software | Research release `13e35bea5996073f117c8e3c5ce4d35eddd205a4` |
| Packed upload | 131,040,016 bytes (gzip output is not further compressible) |

The compressed SHA equals the previous tranche's local generation: gzip runs with
`mtime=0`, so an unchanged DB yields byte-identical archives and near-zero new upload
(second generation added **11,217** packed bytes).

## Upload and remote verification

Run as the real `prism-research-backup.service` (manual start; same unit the timer runs).
Total **4 min 49 s** (17:12:09–17:16:58Z), no restarts.

| Step | Result | Seconds |
|---|---|---:|
| Fresh local generation + local restore proof | compressed/logical SHA, every table | ~107 |
| Encrypted upload (`restic backup`) | snapshot `28eea1d3…` | 36.6 |
| Remote listing (`restic ls`) | `manifest.json` 24,494 B, `research.duckdb.gz` 135,614,775 B = local | — |
| Repository data check (`restic check --read-data`) | every pack downloaded and hash-verified | 36.2 (incl. listing) |
| Proof restore from Drive (`--no-cache`) + materialize | compressed SHA, logical SHA, all 146 table fingerprints | 55.2 |
| Plaintext manifest copy | uploaded, read back, byte SHA-256 equal | — |
| Retention (`forget --prune`) + new-snapshot survival check | retained, manifests reconciled | 25.5 |

The PC was not involved after authorization; the user's machine can be off.

## Restore-from-Drive proof

1. Deleted the backup identity's only local generation and `latest.json`; scratch empty.
   The previous tranche's local generation (`/var/lib/prism-research/backups`, 0700
   `prism-research`) is unreadable by the backup identity.
2. Restore ran in a transient unit with `InaccessiblePaths` covering the source DB
   directory, both local backup roots, the Railway archive, `/data` and `/etc/prism`, and
   restic `--no-cache` (no local metadata). Download **19.3 s**; decompress + verify **36.5 s**.
3. Downloaded archive SHA `023c3240…` = manifest; materialized DB SHA `dbaa5a3c…` = candidate;
   schema 31, 146 relations, every fingerprint equal; DuckDB opened read-only.
4. Validation (`validate-offline` → `scripts/ovh_research_validate.py`) ran in a separate
   transient unit with **`PrivateNetwork=yes`**, the source DB/Research state still
   inaccessible, references copied byte-identically (SHA recorded in the log).

### Research parity after restore

**PASSED, zero differences** against the imported candidate's validation (196 s wall,
192 CPU s, peak RSS 2,086,666,240 B).

- Schema/ownership: 142 prepared tables all present, no unknown relation, 4 expected
  schema-31 additions; **no relation may resume authority**; the four flagged ambiguous
  ownership tables (`alert_events`, `alert_rules`, `journal_entries`, `positions`) retained.
- All **30 representative reads** identical: 6 Context snapshots (BTC/AAVE at cutoff, −1 d,
  −7 d) + update chains; 6 Shared Intelligence packages (IDs, evidence resolution,
  positioning block hashes); OI/funding coverage; macro PIT/vintages, intraday coverage,
  source health; Phase 24B definitions/checkpoints/outcomes; Research definitions/datasets/
  experiments/science/lifecycle and three forward summaries; Copilot governed watches.
- Clock proofs identical to the original import: BTC 9,027 origin-observed, 0 committed
  before the seal bound, 9,027 after, 0 consumer-available before receipt, 9,015 after
  (AAVE 9,025 / 9,013). Authority metadata: Railway claim retained as provenance only.

The 1,781,919,973-byte scratch restore was deleted after its SHA was rechecked.

## Retention

| Where | Policy |
|---|---|
| Drive | `restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 3 --prune`, scoped to host `vps-prism-research` + tag `prism-research-foundation-v1`, grouped by host+tags. Hard upper bound 14 snapshots; restic 0.19 also keeps the oldest snapshot while history is shorter than the policy. After pruning, the job **fails if the newly verified snapshot is missing**. Manifests are deleted only when their generation has no retained snapshot. |
| Drive trash | `use_trash = false` for this remote, so pruned packs do not linger 30 days in trash. Only app-created files are deletable (`drive.file`). |
| Local (`/var/lib/prism-research-backup/generations`) | Steady state **one** generation (~136 MB). During a run: the previous off-host-verified generation is kept until the new one is verified on Drive; only then is it removed. A failed run therefore leaves at most two. |

## Storage estimate (current ~131 MB packed per changed generation)

| Scenario | Drive | Local |
|---|---:|---:|
| Frozen non-authoritative DB (now) | ~131 MB total (dedup) | ~136 MB |
| DB changes daily, steady state (~12 unique of max 14 snapshots) | ~1.6 GB | ~136 MB (272 MB during a run) |
| Worst case 14 unique snapshots | ~1.8 GB | — |
| Transient during a run | + scratch restore ~1.78 GB on VPS (deleted after proof) | |

These scale with DB size. gzip archives dedup poorly when the DB changes; if Drive use
matters later, back up the raw sealed DB and let restic chunk/compress it (deferred: not
needed at ~0.2% of free pooled storage).

## Encryption and privacy

Backups are **client-side encrypted** (restic). The Research DB contains personal and
operational data (a previous Railway backup pull was refused for PII reasons; Context/Work
observations, journal and alert data are present), so provider encryption alone was not
considered sufficient. Drive only sees opaque pack files. The plaintext `manifests/`
contain hashes, row counts, IDs and timestamps — no record bodies or credentials. The
authoritative manifest is the one inside the encrypted, authenticated snapshot; the
plaintext copy is a convenience and is not trusted for verification.

## Credentials on OVH (no values)

| File | Owner / mode | Content |
|---|---|---|
| `/etc/prism-research-backup/` | `prism-research-backup` 0700 | directory |
| `rclone.conf` | `prism-research-backup` 0600 | remote `prism-gdrive`: owner client ID/secret, OAuth token (rclone rewrites it on refresh) |
| `restic-password` | `prism-research-backup` 0400 | machine repository key, generated on the VPS, never printed |
| `backup.env` | 0600 | locations only (`RESTIC_REPOSITORY`, `RESTIC_PASSWORD_FILE`, `RCLONE_CONFIG`) |
| `backup.json`, `source.json` | 0600 | pipeline config; sealed-source descriptor |

`prism-research-backup` is a system nologin user: own group + read-only `prism-research`
(to read the sealed DB); **no Lab group**. Verified: neither `prism-research` nor `prism`
(Lab) can read the credential files. The unit uses `EnvironmentFile` with locations only,
so `systemctl show` reveals no secret; tool stderr is never logged — failures surface as a
fixed class. The token was moved PC→VPS over SSH stdin from a 0600 file, then shredded on
the PC; the shared-client token was shredded unused.

**Rotation.** OAuth: re-run `rclone authorize` with the same client on a browser machine,
pipe output + client JSON into `install_research_backup_token.sh` (refuses to overwrite;
move the old file aside), confirm with `research_backup.py offhost-snapshots`. Revoke old
grants in the Google account if needed. Client secret: rotate in the Cloud console
(Clients), then reinstall. Machine restic key: `restic key add --new-password-file` then
`restic key remove <old id>`; the custodian key is unaffected.

## Recovery custody — if the OVH VPS disappears

Held **outside** the VPS (password manager of the owner):

1. **Restic custodian recovery password** — a second repository key added by the owner
   with `research_backup_recovery_key.sh` (key `6f78b5ae…`, host `custodian-recovery`,
   2026-10-15 17:38Z) and **proven to open the repository without the VPS key**. The VPS
   machine key is `b6a269bc…`. Losing the VPS key does not lose the backups
   ([recovery-keys.log](evidence/google-drive-backup/20261015/recovery-keys.log)).
2. **OAuth client JSON** for the Desktop client (`client_secret_848206253839-….json`; the client ID is not secret, the secret is), because
   `drive.file` access is per client. Fallback without it: download
   `Prism/backups/research/restic/` from the Drive web UI and use it as a local repository.
3. The Google Workspace account `matt@secure-talent.co.uk` and its own sign-in/2FA.

Non-secret: restic ≥ 0.19.1, rclone ≥ 1.75.1 (verify release signatures as above), and
this repository (`scripts/research_backup.py`, `market_signal.ops.research_namespace`).

Recipe on a new machine:

```bash
# 1. Tools (verify SHA256SUMS signatures), then authorize the SAME client in a browser:
rclone config create prism-gdrive drive scope=drive.file use_trash=false \
  client_id=<from client JSON> client_secret=<from client JSON>   # interactive browser auth
# 2. Open with the custodian password (prompted; never put it in argv):
export RESTIC_REPOSITORY=rclone:prism-gdrive:Prism/backups/research/restic
restic --no-cache snapshots --host vps-prism-research --tag prism-research-foundation-v1
# 3. Recover and verify into a fresh path (checks archive + logical SHA + every table):
python scripts/research_backup.py offhost-restore --snapshot <id|latest> --scratch /fresh/path --keep
# 4. Optionally compare against manifests/<generation>.json, then run the offline
#    read-parity validator against the restored DB before trusting it.
```

`offhost-restore` needs `RESTIC_PASSWORD_FILE` or an interactive prompt wrapper; for an
interactive recovery run `restic restore <id> --target /fresh/path` and then
`research_backup.py restore --archive … --manifest … --destination …`.

**Re-establishing authority** is a separate approved handoff: a restored DB carries the
old runtime claim as provenance only and opens read-only. Use the documented owner
claim/cutover procedure (stop the previous owner, verify frontier, claim, then start
writers); never point a writer at a restored copy while another runtime may still hold
authority.

## Automation

| Item | State |
|---|---|
| `prism-research-backup.timer` | **Enabled**. Nightly 03:15 UTC + up to 10 min random delay, `Persistent=true`. Clear of the Lab backup (01:30 UTC). First timed run 2026-10-16 ~03:19 UTC. |
| `prism-research-backup.service` | Oneshot, `prism-research-backup` user, nice 19, idle I/O, `CPUQuota=100%`, `MemoryMax=3G`, `ProtectSystem=strict`, `/data` and `/etc/prism` inaccessible, source DB read-only. |
| Retry | `Restart=on-failure`, `RestartSec=1h`, at most 6 starts/day (`StartLimitBurst`). |
| Status | `/var/lib/prism-research-backup/state/last-run.json` (`ok` or `failed` + stage + fixed error class); unit `Result`/journal. |
| Scope | The non-authoritative imported DB (proves the pipeline and catches auth expiry early). Production scheduling begins only at Research authority transfer, when the authoritative runtime's owner-lock/checkpoint backup must supply the sealed input and `source.json`. |

The nightly run uses roughly one CPU for ~5 minutes; exclude 03:15–03:35 UTC from clean
Lab capacity samples.

## Failure behaviour (verified)

| Failure | Behaviour | Proof |
|---|---|---|
| Backup creation / checksum / WAL / changed source | `ValueError`, status `failed` stage `backup`; previous generations untouched | existing + new unit tests |
| Upload failure / quota (`Error 403`, rate limit) | `quota failure`; last verified local copy kept | unit test |
| Remote listing/size mismatch | `remote listing/size mismatch`; no receipt | unit test |
| Missing manifest in snapshot / manifest mismatch | fails before receipt | unit tests |
| Restore validation failure | fails; no receipt; scratch removed | unit tests |
| Retention would drop the new snapshot | `retention removed the newly verified snapshot` | unit test |
| Expired/revoked OAuth | `authentication failure` (rclone and restic) | **live** on OVH |
| Wrong repository key | `repository key failure` | **live** on OVH |
| Missing repository | `missing failure` | **live** on OVH |
| Missing protected rclone config | refused before any call | **live** on OVH |

Local deletion happens only after Drive verification; failed runs keep the last verified
generation and retry hourly.

## Google Drive practical limits

- **Upload size:** packs are ~16 MiB, well below Drive limits; rclone uses chunked uploads
  above 8 MiB (default `chunk_size` 8 MiB, buffered in memory). Total ~131 MB/day.
- **Rate limits:** Drive throttles to roughly 2 files/s; rclone's pacer retries `403
  rateLimitExceeded`. The per-user 750 GiB/day upload limit is irrelevant at this size.
- **Quota errors:** surfaced as `quota failure`; hourly retry.
- **Token refresh:** proven by forcing expiry; refreshed with the owner client. Internal
  apps are not subject to the 7-day Testing expiry. Refresh tokens can still be revoked
  (user/admin action, or long disuse) → `authentication failure` → re-authorize.
- **Resumability:** restic is restart-safe: an interrupted backup leaves unreferenced
  packs that the next `prune` removes; stale locks expire. The pipeline never deletes
  local data until a run completes.

## Tooling

- `deploy/consolidated/install_research_backup.sh TOOLS_DIR SOURCE_JSON` — identity, dirs,
  pinned tools, machine key, units (timer not enabled by the script).
- `deploy/consolidated/install_research_backup_token.sh` — stdin-only token install.
- `deploy/consolidated/research_backup_recovery_key.sh` — interactive custodian key add + proof.
- `deploy/consolidated/stage_research_release.sh ARCHIVE REV` — new Research release; Lab untouched.
- `scripts/research_backup.py {local,offhost,offhost-restore,offhost-snapshots,scheduled,restore}`.

## Safety

No Research or Railway authority change, no Research writer/scheduler, Context gateway,
DNS/OAuth audience, Phase 24B, Dynamic Universe or paper change. Lab services (PIDs,
start times, restarts), Lab release symlink, `/etc/prism/prism.env` and Lab crontab hashes
are identical before/after; only the Research release symlink moved
(`c5b81f30…-7190e0cb53c2` → `13e35bea…`; rollback = repoint it). The source DB SHA is
unchanged. No secret value was printed, logged or committed.
