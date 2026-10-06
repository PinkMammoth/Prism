# Perp open interest (OI): collection and storage

**Data collection only.** Stored OI is infrastructure for later research. It is not a
feature, not a Strategy Lab input and not a trading signal. Nothing in scanning,
paper-tracking, the co-pilot or alerts reads it. The only reader is the Phase 19
exploratory study ([PHASE19_OI_PRICE.md](PHASE19_OI_PRICE.md)), which reads retained Lab
snapshots of these tables (`perp_oi_history`, `perp_snapshots`), never the live tables
during evaluation.

Two venues are collected. They are stored separately and never merged:

| | Hyperliquid | Binance USD-M |
|---|---|---|
| Source | `POST /info {"type":"metaAndAssetCtxs"}` (current state only) | `GET /futures/data/openInterestHist` |
| History from the API | **None** | **Latest ~30 days only** |
| Stored in | `perp_snapshots` (`source='hyperliquid'`) | `perp_oi_history` (`source='binance'`) |
| Timestamp | Prism capture time (`snapshot_at`) | Binance `timestamp`, exact |
| Cadence | **Irregular**: whenever Prism runs on the local PC | Fixed provider period, `1h` stored |
| OI, base units | `open_interest` (coins) | `open_interest` = `sumOpenInterest` (coins) |
| OI, USD | `oi_notional` = `open_interest × mark_px`, **derived by Prism** | `oi_notional` = `sumOpenInterestValue`, **as returned by Binance** |
| Missed collection | **Lost for good** | Recovered by the next run if under ~30 days old |

## Querying

`perp_oi_observations` is a view that lists both venues side by side. It never merges them:

| column | meaning |
|---|---|
| `venue` | `hyperliquid` or `binance` (`synthetic` in the demo DB) |
| `coin` | Prism perp coin (`BTC`, `HYPE`, …) |
| `provider_symbol` | `BTC` on Hyperliquid, `BTCUSDT` on Binance |
| `market_type` | `hyperliquid_perp` or `usdm_perpetual` |
| `period` | `snapshot` (Hyperliquid) or the Binance statistics period (`1h`) |
| `observed_at` | exact timestamp (UTC) |
| `open_interest`, `oi_notional` | raw values, as described above |
| `oi_notional_method` | `open_interest*mark_px` or `provider:sumOpenInterestValue` |
| `ingest_run_id` | → `ingestion_runs` (params, timing, archived raw payloads + SHA-256) |

`perp_oi_history` also keeps `ingested_at` (first stored) and `updated_at` (last value
change). `perp_snapshots` keeps the Hyperliquid mark, oracle and funding values captured
with each snapshot. In Python, `market_signal.perps.open_interest.load_oi(store, coin,
venue)` reads exactly one venue.

### Irregular sampling: read this before building OI features

Hyperliquid snapshots come from a PC that is on roughly **07:15–20:00 and 22:00–23:00 UK
time**, not from an always-on collector. Expect several snapshots a day at uneven times, a
gap of about 9 hours every night, and longer gaps on days the PC stays off. Nothing
assumes 6-hourly or 4-per-day sampling, and nothing re-grids timestamps.

Future features (OI change over ~4h/12h/24h, daily last-known OI, venue divergence) must:

- measure change over **actual elapsed time** between two observations, never by row
  count;
- set a maximum staleness for "last known OI" at a decision time, and treat older values
  as missing;
- treat the capture time as the earliest availability time for Hyperliquid. For Binance,
  `observed_at` is the statistics timestamp. The 21:00 hourly point was already being
  served at 21:18 UTC (checked 2026-10-03), but the publication lag is not documented, so
  add a conservative lag;
- remember that `oi_notional` moves with price alone; use base units to isolate
  positioning;
- never fill Hyperliquid gaps from Binance. They are different venues.

## Binance API facts (verified 2026-10-03)

Official docs: [Open Interest Statistics](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics)
(USDⓈ-M futures, base `https://fapi.binance.com`, no key).

- `GET /futures/data/openInterestHist`, parameters `symbol`, `period`, `limit`,
  `startTime`, `endTime`.
- `period`: `5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d`.
- `limit`: default 30, max 500.
- Docs: *"Only the data of the latest 1 month is available."* and *"IP rate limit 1000
  requests/5min"*. Request weight 0.
- Response rows: `symbol`, `sumOpenInterest` (base units), `sumOpenInterestValue` (USD
  value), `CMCCirculatingSupply`, `timestamp` (ms). Prism doesn't store
  `CMCCirculatingSupply`, but it stays in the archived raw payload.
- Symbols are the USD-M names (`BTCUSDT`). This is not the COIN-M (`dapi`) endpoint.

Checked live, beyond the docs:

- **Window**: on 2026-10-03 21:18 UTC the oldest `1h` row was 2026-09-03 22:00, i.e.
  29.97 days back. Any `startTime` 30 days or more in the past is **rejected** with HTTP
  400 `{"code":-1130,"msg":"parameter 'startTime' is invalid."}`. It is not clamped.
- **Ordering**: when the requested range holds more than `limit` rows, Binance returns the
  **latest** `limit` rows of the range, not the earliest. Paging forward by `startTime`
  would silently skip data. Prism therefore pages **backward by `endTime`** and never
  sends `startTime`.
- An unknown symbol returns HTTP 200 with `[]`, not an error. Prism reports it as
  `unavailable`.
- Timestamps fall on the period boundary (`1h` → `HH:00:00Z`).

**Chosen period: `1h`.** That is 720 rows per coin for the full window, 2 requests at
`limit=500`, and about 100 bytes per row in DuckDB. It is fine-grained enough for later
4h/12h/24h windows and costs nothing for six coins. `5m` would be 8,640 rows and 18
requests per coin per full backfill without helping daily-scale research. The period is
configurable (`open_interest.binance.period`). Each period is its own series, keyed by
`period`.

## Rolling backfill (Binance)

Every run (`market update`, `market update --only perps` or `market oi collect`) does
this for each configured coin, with no date arguments:

1. Read the stored `1h` rows from the last 30 days.
2. Pick how far back to reach: the start of the earliest gap between stored rows, or 3
   periods before the newest row (to re-read provisional points). Never earlier than the
   API window. With nothing stored, take the whole window.
3. Page backward from now until that point is reached, a short page arrives, or Binance
   returns nothing.
4. Upsert keyed on `(source, coin, period, observed_at)`, in one transaction. New rows are
   inserted. Changed values are updated and counted as `revised`. Identical rows are left
   alone. Duplicates are impossible.
5. Record an `ingestion_runs` row per coin (`provider='binance'`, `dataset='perp_oi_1h'`)
   and archive the raw pages.

If a request fails, nothing is written for that coin. Its run is marked `failed` and the
other coins carry on. A steady-state run needs one request per coin.

**Coin mapping** (`open_interest.binance.symbols`). All six Prism perp coins have a
TRADING USD-M perpetual: BTCUSDT, ETHUSDT, SOLUSDT, HYPEUSDT (listed 2025-05-30),
LINKUSDT, AAVEUSDT. A coin without a mapping is reported as `unsupported`, an empty answer
as `unavailable`, and a failure as `failed`. None of these stop the other coins. Binance
coverage is its own venue-specific set. It can differ from the Hyperliquid list, and
HYPEUSDT history starts only in May 2025.

The existing Binance candle/funding venue (`venues.binance.symbols`, used for research)
is unchanged. It still excludes HYPE, which only the OI mapping includes.

## Hyperliquid snapshots

Unchanged: every `market update` / `--only perps` run records one row per configured coin
in `perp_snapshots` at capture time. `market oi collect` records the same snapshot without
the candle/funding updates. The only addition is a 5-minute minimum gap
(`open_interest.hyperliquid.min_snapshot_gap_minutes`). Two runs fired back to back (for
example `daily.sh` and the OI task) record once; the second reports `skipped`.

## Downtime and the three-week trip

What happens if the PC is off for ~21 days and nothing runs:

| Data | After returning |
|---|---|
| Binance OI, 1h | **Fully recovered** by the first ordinary run (any of the three commands). No special command. The gap is about 21 days and the window about 29.9 days, so there are **~9 days to spare**. |
| Hyperliquid OI | **Permanently missing** for the whole trip. No API can restore it. |
| Hyperliquid / Binance daily candles and funding | Recovered: those endpoints serve long history. |
| Perp paper-tracking checks | Not recorded for the trip, by design: they are live-only. |

**Margin warning.** The Binance window is 30 days and does not stretch. Each day past
~29.9 days without a run loses that day's oldest hours for good. A 3-week trip is fine.
Four weeks plus a few days before the PC is next switched on is not. Run `market oi
collect` (or let the logon trigger do it) on the first day back. `market doctor` turns a
coin `AT RISK` once its newest row is 20 days old, and `LOST` at 30 days. Binance can
change its retention without notice; the parser would still work, but the window
assumption would not.

Only an always-on collector would avoid the Hyperliquid gap: a machine left on, or a small
hosted job. That is not part of this change.

## Diagnostics

`market oi status` (add `--gaps` to list each gap, `--coin BTC` to filter) and `market
doctor` show a coverage table per venue and coin: rows, first/last observation, age, gap
count and the longest gap in the last 30 days, and a status:

| venue | status | when |
|---|---|---|
| hyperliquid | `ok` | newest snapshot ≤ 20h old (an overnight PC-off gap of ~9h is normal and not flagged) |
| hyperliquid | `STALE` | newest snapshot > 20h old. **Each missed hour is lost.** |
| hyperliquid | gap count | gaps > 20h in the last 30 days, noted as permanent |
| binance | `ok` / `stale` | newest row ≤ 12h / > 12h old (stale is still recoverable) |
| binance | `AT RISK` | newest row ≥ 20 days old; the note says how many days are left |
| binance | `LOST` | newest row ≥ 30 days old; the gap is now permanent |
| binance | gap count | missing hours inside the window; the next run re-fetches them |
| both | `NO DATA` / `unsupported` | nothing stored yet / no Binance mapping |

A coin that stops returning data shows up as `unavailable` in the run output, and as
`stale` → `AT RISK` in the coverage table while the other coins stay `ok`. Failed runs
also appear under "Recent failed ingestion runs" in `market doctor`. Thresholds are in
`config/perps.yaml` under `open_interest`.

A Binance gap that Binance itself never fills (a missing point on their side) is
re-requested on each run until it leaves the 30-day window. That costs at most two
requests per coin per run.

## Scheduling (recommended; not installed)

Prism does not install or change any scheduler. Recommended setup for a PC that is on
about **07:15–20:00 and 22:00–23:00** (UK time; WSL runs Europe/London):

| Trigger | Why |
|---|---|
| At log on, delayed 2 min | first snapshot of the day (~07:17), and catch-up after any downtime |
| Daily 11:00 | late morning |
| Daily 15:00 | mid-afternoon |
| Daily 19:00 | early evening, before the ~20:00 shutdown |
| Daily 22:20 | after the PC comes back on at ~22:00 |

This gives about five Hyperliquid snapshots a day, at most ~4h apart in the daytime and
~9h overnight. The existing 10:00 `daily.sh` run (`market update`) adds another. Tick
"Run task as soon as possible after a scheduled start is missed" so a missed slot runs at
the next boot. The minimum gap and the idempotent Binance upsert make the extra run
harmless.

Wrapper script `oi_collect.sh` (local, next to `daily.sh`, not committed because it has
machine paths):

```bash
#!/usr/bin/env bash
REPO=/home/matth/prism
UV=/home/matth/.local/bin/uv
LOG=data/oi.log
cd "$REPO" || exit 1
exec 9>data/.oi.lock
if ! flock -n 9; then echo "=== $(date -Iseconds) skipped: already running" >> "$LOG"; exit 0; fi
{ echo "=== $(date -Iseconds) start"; "$UV" run market oi collect; rc=$?; echo "=== done oi=$rc"; } >> "$LOG" 2>&1
if [ "$(wc -l < "$LOG")" -gt 5000 ]; then tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; fi
exit "$rc"
```

- `flock -n` skips the run if a previous OI run is still going.
- Overlap with `daily.sh` or the dashboard is handled by Prism's database lock: the later
  process waits up to `PRISM_LOCK_TIMEOUT` (600s) instead of failing.
- The exit code is 0 when everything succeeded or the run was skipped as a duplicate, and
  1 when any OI step failed (stored data is untouched for that step).

Task Scheduler action:

- Program: `C:\Windows\System32\wsl.exe`
- Arguments: `-d Ubuntu -u matth -- /home/matth/prism/oi_collect.sh`

Or create the task with PowerShell, run as the Windows user:

```powershell
$a = New-ScheduledTaskAction -Execute "C:\Windows\System32\wsl.exe" -Argument "-d Ubuntu -u matth -- /home/matth/prism/oi_collect.sh"
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; $logon.Delay = "PT2M"
$t = @($logon) + ("11:00","15:00","19:00","22:20" | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ })
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Prism OI collect" -Action $a -Trigger $t -Settings $s -Description "Prism: Hyperliquid OI snapshot + Binance OI backfill (data only)"
```

Check the result with `tail data/oi.log` and `uv run market oi status`.
