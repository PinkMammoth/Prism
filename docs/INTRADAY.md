# Intraday market data (Phase 15)

> **Phase 15 introduces intraday market data but does not introduce intraday strategy signals.**
>
> Phase 16 adds descriptive structural primitives on these bars (swings, clusters, failed
> breakouts, structure shifts, retests, trade paths). They are not signals either; see
> [STRUCTURE.md](STRUCTURE.md).
>
> **The existing daily paper account continues using its frozen Phase 12 execution semantics;
> intraday execution data is observational/shadow-only in this phase.**

This is the market-data and execution-timing substrate for later 4h/1h/15m research and
OI × price work. Nothing in this phase reads intraday bars to make a decision: no strategy,
no co-pilot rule and no paper fill uses them. Code: `src/market_signal/intraday/`, CLI
`market bars …`, migration 18.

## Venues and endpoints

| venue | role | endpoint | history the provider serves |
|---|---|---|---|
| **Hyperliquid** perps | **live**, canonical for the paper/co-pilot venue; scheduled every 15 min | `POST https://api.hyperliquid.xyz/info` `{"type":"candleSnapshot","req":{"coin","interval","startTime","endTime"}}` | newest **5,000 candles per interval** only: 15m ≈ 52 days, 1h ≈ 208 days, 4h ≈ 833 days (HYPE 4h starts at its listing, 2024-12-05) |
| **Binance USD-M** perps | **history only** (manual backfill, never scheduled) | `GET https://fapi.binance.com/fapi/v1/klines` (1,500 per page) | since listing (BTC 2019-09) |

Provider semantics, verified on 2026-10-05 and enforced at parse time (a mismatch is a
`SchemaError` and nothing is written):

- Hyperliquid candle `t` is the bar **open** (ms UTC) and `T = t + interval − 1 ms`. `endTime` is
  inclusive of a candle's `t`. The newest candle returned is **still forming**. A request older
  than the retention window returns `[]`. `s`/`i` must echo the requested coin and interval.
- Binance kline `[0]` is the open and `[6] = open + interval − 1 ms`; `[8]` is the trade count.
- Both are **provider-native** bars. Nothing is aggregated in v1 (`derivation = 'native'`).

Venues are never mixed. Every row carries its venue (`source`), and a Binance bar is never a
fallback for a Hyperliquid one (USDT vs USDC markets, different books).

## Timeframes and UTC boundaries

Supported: **15m, 1h and 4h** (`Timeframe.M15/H1/H4`). There is no 1m/5m. Daily bars keep
their existing canonical source (`perp_bars`, `timeframe='1d'`) and are not duplicated here.

Every bar is the UTC half-open interval **`[open_time, close_time)`**,
`close_time = open_time + interval`, on the grid aligned to the Unix epoch:

| timeframe | bars open at (UTC) |
|---|---|
| 15m | :00, :15, :30, :45 of every hour |
| 1h | the top of every hour |
| 4h | 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 |

Off-grid provider bars are rejected. Naive timestamps are errors: there is no local-time logic.
These rules are pinned by `tests/test_intraday.py::test_utc_boundaries_are_pinned`.

## Closed bars and availability

- **Closed-bar guarantee.** A bar is stored only if `close_time + settle (5 s)` is at or before
  the instant its request was **sent**. The forming candle is never requested (the window ends at
  the newest closed boundary), is dropped if served, is refused again by the writer, and is
  forbidden by a table CHECK (`first_observed_at >= close_time`). Partial bars are not stored at
  all, so research code cannot see one.
- **Availability.** `first_observed_at` is when Prism first held the closed bar: the response
  time, never the theoretical close. A bar the provider publishes late becomes available late.
- `observed_live` is true when that first observation came within one interval of the close
  (live collection). It is false for backfilled history, whose real publication time is unknown.

## Storage (`perp_intraday_bars`, migration 18)

One table for every intraday timeframe, unique per `(source, coin, timeframe, open_time)`:
`open_time, close_time, open, high, low, close, volume, trades, derivation,
first_observed_at, first_run_id, observed_live, revision, updated_at, ingest_run_id`.

- **No index.** A primary key's ART index measured **48 of 66 bytes per row** (73%). Without it a
  bar costs **~18 B** compacted. Uniqueness is enforced by the single writer instead: one writing
  process (the DuckDB file lock plus the runtime lock), batch de-duplication, NOT-EXISTS inserts,
  and a per-transaction duplicate check that rolls back. `market bars status` also reports
  duplicates and malformed rows. Range predicates on `(source, coin, timeframe, open_time)` let
  DuckDB prune row groups, because writes arrive in chronological runs per series.
- `perp_intraday_revisions`: superseded values (see below).
- `perp_intraday_coverage`: merged open-time ranges a provider was successfully asked for after
  they closed. This drives gap triage.
- `intraday_execution_shadow`: shadow timing (see below). It is not paper evidence.
- Provenance: one `ingestion_runs` row per venue per call (`dataset = perp_intraday_bars`; params
  record mode, `ingest_version = intraday_ingest_v1`, git commit, settle and the grouped requested
  windows). Every response is archived in **one gzip JSON-lines file per run**
  (`raw/<provider>_intraday/YYYYMMDD/<run_id>.jsonl.gz`), with a sha256 per response in
  `raw_sha256` and `raw_paths` entries of the form `<file>#<line>`.

## Revisions

A refetch that returns different values for an already-stored closed bar (OHLC, volume or
trades) is a **revision**. The canonical row takes the new values and `revision` increments,
while `first_observed_at` stays the same. The superseded values go to
`perp_intraday_revisions`, together with `old_observed_at` (when they were first held),
`revised_at` and both run ids. An identical refetch changes nothing.

This is enough to reconstruct exactly what Prism held at any instant:
`load_bars(..., known_at=t)` returns only bars first observed by `t`, each with the values held
at `t`. It is bounded by observation cadence: Prism knows a revision only from the fetch that saw
it. Revisions are detected only inside the overlap window that each update re-reads (15m: 4 bars,
1h: 3, 4h: 2, plus the newest stored bar). A revision older than that is not seen unless a
backfill re-reads the range.

## Backfill and incremental update

- `market bars backfill --venue V [--start --end --coin --tf] [--dry-run]` is deterministic and
  covers exactly `[start, end)`, defaulting to the configured history start and the newest closed
  bar. Requests go in fixed windows of `page_bars` (Hyperliquid 5,000; Binance 1,500), so there
  are no overlapping pages. It is idempotent. Provider retention limits are written into the plan
  notes and run params ("NOT SERVED …") rather than truncated silently. The disk guard runs before
  any request.
- `market bars update` (scheduled) does nothing for a series until a new bar has closed past its
  newest stored bar. Then it re-reads the overlap bars before that bar, which serves as the
  revision window and also recovers any missed runs, up to the newest closed bar. So 15m is
  fetched every run, 1h once an hour and 4h once every 4 hours. History-only venues update only
  when named (`--venue binance`).

## Gap detection

`market bars status` reports, per venue, coin and timeframe: first and last bar, expected vs
actual count, missing bars by category, duplicates or malformed rows, zero-volume bars,
revisions, live-observed count, newest-bar age and status. `market bars gaps` lists each missing
interval.

| category | meaning |
|---|---|
| `provider_missing` | Prism asked after the bar closed and the provider served nothing: exchange downtime or no trades. Not a local failure. |
| `recoverable` | never fetched after closing and still inside the provider's retention: a local miss. `market bars backfill` fills it. |
| `permanent` | never fetched and already beyond the provider's retention. |

Binance serves flat zero-volume bars through its maintenance windows instead of omitting them.
They are not gaps, but they are counted (`zero vol`) so research can exclude them.

Staleness is judged per timeframe for live venues: newest closed bar older than **45 min (15m),
120 min (1h) or 300 min (4h)**. A 4h feed 2 h old is healthy; a 15m feed 2 h old is not.
History-only series show `HISTORICAL`.

## Multi-timeframe alignment (`intraday/align.py`)

`align(times, bars, tf, availability)` returns, for each instant `t`, the newest bar that was
**closed and available by `t`**. `snapshot(store, venue, coin, at, tfs)` and
`market bars align COIN --at T` show the view for one instant.

- `Availability.observed()` (the default; live/prospective) uses `first_observed_at <= t`.
- `Availability.assumed(latency)` (historical research on backfilled bars) uses
  `close_time + latency <= t`. The result rests on an assumption and must be labelled as such.
- Among eligible bars the newest **open** wins. A late-published older bar can never displace a
  newer bar that was already available.
- With revisions supplied, each aligned bar carries the values it held at `t`.
- Nothing is forward-filled from an unfinished bar. If nothing is eligible the row is empty.
  `bars_behind` says how far the aligned bar is behind the newest bar that has theoretically
  closed. `max_bars_behind` turns stale alignments into empty rows.

Examples (tests): at 10:37 the eligible 15m bar is 10:15–10:30, the 1h bar 09:00–10:00 and the
4h bar 04:00–08:00. At 12:01 the 08:00–12:00 4h bar is eligible only if it was observed by 12:01.

## Shadow execution timing (observational)

Rule **`intraday_exec_timing_v1`** (frozen):

- `intended_entry_at` is the legacy reference time: the open of the order's fill day, which is the
  signal bar's close.
- `decision_at` is when the paper engine recorded `order_submitted`.
- The reference is the open of the first stored native Hyperliquid **15m** bar with
  `open_time >= max(intended_entry_at, decision_at)`. If that exact bar is missing, nothing is
  substituted and the observation waits.
- `ref_observed_at` is that bar's `first_observed_at`. Latency is
  `ref_observed_at − intended_entry_at`, and `timely` means latency ≤ 1 h.
- Recording is **prospective only**, within 48 h of the intended entry. Old paper trades are
  never backfilled, and a missed observation stays missed.

The `bars shadow --record` step runs after each intraday update and writes only
`intraday_execution_shadow`. `market bars shadow` reports each observation next to the
**legacy daily-open reference**, joined from the immutable `order_filled` event: side-adjusted
difference in bps (positive means the intraday reference was worse for the position), latency,
and how many hours earlier a notification could have been sent than the legacy
`position_opened` event.

**Notification timing is deferred.** `position_opened` is created when bar T+1 is processed,
after its daily close. Emitting it, or its Telegram message, earlier would change event
semantics or notify before the immutable event exists. So Phase 15 records only when a notice
*could* have been sent (`notification_possible_at`).

## Paper-run preservation

Paper run `paperrun_27b0a336e707c389a3fb574cd9d09a7800b563f0691612bf4fb4f1fd97029279` is
unchanged: same daily cadence, T+1 daily-open fill, 10-bar exit, costs, funding, risk, sizing
and events. The paper engine and co-pilot contain no reference to intraday data (enforced by a
test). Shadow recording writes no `paper_*` row (also tested). Policy IDs are pinned in
`tests/test_runtime.py`.

## Lab governance (datasets and point-in-time)

`SeriesSelection` accepts `perp_intraday_bars` and `perp_intraday_revisions` (15m/1h/4h, a
`[start, end)` range on `close_time`). Captures reuse the Lab's compressed canonical-row
SHA-256 snapshots, and the retained rows include `first_observed_at`, `observed_live` and
`revision`. So a frozen dataset preserves both the final values and what was known when. The
current evaluation-plan contract still preregisters **daily** inputs only, so intraday datasets
can be registered and fingerprinted but cannot yet back a confirmatory claim.

**Limitation:** backfilled bars (`observed_live = false`) have no true availability time. Their
first observation is the backfill instant. Research on them must use
`Availability.assumed(latency)`, and claims that depend on exact intraday publication timing or
revision state before Phase 15's live collection began are not supported.

## Historical coverage and footprint (production, 2026-10-05)

Seeded on the Railway runtime right after deployment. There are zero missing, duplicate or
malformed bars in all 36 series. Only the newest bar of each series was observed live; every
older bar is backfill (`observed_live = false`).

| venue | 15m | 1h | 4h |
|---|---|---|---|
| Hyperliquid (all six coins) | 2026-08-14 → now (5,000 bars each) | 2026-03-10/11 → now (4,999–5,000) | 2024-06-24 → now (4,999); HYPE 2024-12-05 → (4,013) |
| Binance BTC / ETH / SOL / LINK / AAVE | 2024-10-01 → now (70,495 each) | BTC 2019-09-08, ETH 2019-11-27, LINK 2020-01-17, SOL 2020-09-14, AAVE 2020-10-16 → now | same starts as 1h |
| Binance HYPE | 2025-05-30 → now (47,317) | 2025-05-30 → (11,829) | 2025-05-30 → (2,957) |

Binance zero-volume (maintenance) bars: BTC 1h 3, ETH 1h 1.

| measure | value |
|---|---|
| rows | Hyperliquid ≈ 89k; Binance ≈ 772k (15m 399.8k, 1h 298.2k, 4h 74.6k) |
| database growth | 140.8 MB (pre-deploy backup) → 158.6 MB: **≈ 18 MB** for ≈ 861k bars (~21 B/bar in place; 18 B compacted) |
| raw archive | Hyperliquid seed 2.5 MB, Binance history 37 MB (gzip JSON-lines) |
| runtime / memory | Hyperliquid seed ≈ 20 s; Binance history 341 s on Railway (local: 5 m 24 s, peak RSS ≈ 600 MB; the 8 GB limit is ample) |
| ongoing (Hyperliquid live) | 6 coins × (35,040 + 8,760 + 2,190) ≈ **276k bars/yr ≈ 5–11 MB/yr** of database; one ~2–4 KB raw file per 15-min run ≈ **140 MB/yr** of raw archive (4 KB blocks); 96 `ingestion_runs` + 96 `runtime_cycles` rows/day |
| backups | each retained copy grows with the database. Worst case at today's size: ≈ 18 MB × 18 copies ≈ 0.3 GB on a 4.5 GiB volume (3.9 GiB free after seeding) |

No volume expansion is needed. The disk guard re-checks before every write.

## Runtime, health, storage and backups

- **Schedule:** job `intraday` at :01, :16, :31 and :46 every hour (`--wait 600`). It runs
  `bars update` then `bars shadow --record` under the runtime lock, like every job, so it
  serialises with the prospective, OI, daily and backup jobs. A failure alerts once (the first
  failure after a success), not every 15 minutes. Strategy, co-pilot and paper jobs stay on their
  daily cadence.
- **`market status`** adds `Intraday 15m`, `Intraday 1h` and `Intraday 4h` rows for the live
  venue, using each timeframe's own stale threshold.
- **Disk guard:** a backfill or update first estimates its database growth × (1 + the retained
  backup copies, 18) + raw archive bytes. It refuses, before any request or write, if that would
  leave less than the runtime's **15% warning** free fraction. The refusal states the extra
  capacity required. Nothing is ever deleted.
- **Backups:** the intraday tables are in the database, so every verified backup includes them,
  and the continuity fingerprint lists intraday row counts (growth is allowed, loss is flagged).
  Raw archives are **not** copied into backups, as before. The database rows are authoritative.
  Raw files are provenance. Hyperliquid history beyond its 5,000-bar retention can be restored
  only from a database backup, never from the provider.
