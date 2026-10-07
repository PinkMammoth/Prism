# Phase 24A: Hyperliquid microstructure collection

> **Data collection only. No strategy, no signal, no trading.** Nothing here places an
> order, reads a key, sizes a position, changes risk, or is read by the forward tracker, the
> co-pilot, the paper trader, candidate incubation or the Phase 21/22/23 research state
> (tests enforce this).

Status: implemented 2026-10-07. Code `src/market_signal/microstructure/` (`definitions`,
`aggregate`, `engine`, `ws`, `spool`, `ingest`, `query`, `health`), CLI `market
microstructure …` (`cli/micro_cmds.py`), migration 23, runtime job `microstructure`, the
supervised collector in `deploy/railway/entrypoint.sh`, tests `tests/test_microstructure.py`.

## 1. Motivation

Phase 22 found that simple OHLCV directional rules have no edge after realistic costs.
Phase 23 added context (events, macro, positioning, fixed-hour OI). The missing piece is
what candles hide: who is actually crossing the spread, how much, how big, and how the book
is behaving while they do. Phase 24A starts recording that, **prospectively**, so that a later
phase can test whether context + positioning + order flow predicts direction better than
candles alone. The purpose is: *give Prism eyes on actual buying/selling pressure before
asking its new brain what the market is trying to do.*

## 2. Hyperliquid feeds consumed

Public WebSocket `wss://api.hyperliquid.xyz/ws` only. **No credential of any kind.** Universe:
BTC, ETH, SOL, HYPE, LINK, AAVE (Prism's six perps). One connection, four subscriptions per
coin (24 in total). Measured live on 2026-10-07:

| feed | subscription | content used | cadence (measured) |
|---|---|---|---|
| `trades` | `{"type":"trades","coin":C}` | `time` (ms), `px`, `sz`, `side`, `tid`, `hash` | every fill, batched per block |
| `book5` | `{"type":"l2Book","coin":C,"fast":true}` | 5 levels/side + `time` | ~0.54 s (0.46–0.63 s) |
| `book20` | `{"type":"l2Book","coin":C}` | 20 levels/side + `time` | ~5.4 s (5.35–5.49 s) |
| `ctx` | `{"type":"activeAssetCtx","coin":C}` | `openInterest`, `markPx`, `oraclePx`, `funding`, `impactPxs` | ~1 s, **no timestamp** |

* **Aggressor side.** `side` is Hyperliquid's taker side: `"B"` = the taker bought, `"A"` =
  the taker sold. It is used as given, never inferred (no tick rule, no quote rule). Live check:
  of 488 fills compared with a ~0.5 s-old book, `B` printed at/above the ask 210× vs at the bid
  7×, and `A` at/below the bid 190× vs at the ask 33×, consistent with a taker side (the
  mismatches are book staleness).
* **Prints.** One taker order that sweeps several makers arrives as several fills sharing the
  transaction `hash` (live: 668 fills, 251 distinct hashes). Fills with the same hash, side and
  exchange time form one **print**; a zero/absent hash makes the fill its own print. Trade-size
  statistics and large prints use prints, counts use fills.
* **Book depth flag.** WS book messages do not say whether they are fast or full depth. A
  side with more than 5 levels can only be the 20-level feed; a message with at most 5 levels
  per side is the fast feed (and, if both sides are thinner than 5 levels, also a complete
  20-level view). Irrelevant for six liquid perps, documented for completeness.
* **Addresses.** `trades` includes `users: [buyer, seller]`. They are **dropped at parse
  time** and never stored, spooled or archived: Phase 24A builds no wallet profiling, and the
  two 42-character strings per fill would dominate raw storage. Their availability is
  recorded here so a future phase can decide explicitly.
* **Impact prices.** Phase 23 keeps its hourly REST capture. `ctx` adds Hyperliquid's own
  `impactPxs` at minute end (same venue definition, different cadence), not a new measure.
* **Not used:** `bbo` (per-block top of book; ~13 msg/s for BTC alone, unnecessary given
  `book5`), `l2Book` `nSigFigs`/`mantissa` aggregation (would change depth semantics).

## 3. `microstructure_1m_v1`: stored fields

One row per (feature version, coin, UTC minute `[minute_open, minute_open + 1 min)`) in
`microstructure_minutes`. Notional = price × size in USD. Primitives are stored; ratios are
derived by the loader so they can be recomputed exactly after resampling.

| group | stored column(s) | definition |
|---|---|---|
| identity | `feature_version`, `coin`, `minute_open`, `revision`, `status`, `flags` | `revision` 0 = as finalized; 1 = the one bounded late-trade revision |
| coverage | `trade_cov` | fraction of the minute the trades subscription was acknowledged and the connection alive |
| | `book_samples`, `depth20_samples` | valid 1 s book samples (0–60), see §4 |
| trade flow | `n_buy`, `n_sell` | fills by taker side |
| | `buy_vol`, `sell_vol`, `buy_ntl`, `sell_ntl` | Σ size / Σ notional by taker side |
| | `n_buy_prints`, `n_sell_prints` | prints by taker side |
| price | `first_px`, `last_px`, `high_px`, `low_px` | from fills, ordered by (exchange time, arrival) |
| trade size | `max_print_ntl`, `med_print_ntl` | largest / median print notional |
| | `size_hist` (14 ints) | print counts in half-decade notional buckets: <$10, [$10, $31.6), …, [$3.16M, $10M), ≥$10M |
| large prints | `lp_threshold`, `lp_n`, `lp_buy_n`, `lp_ntl`, `lp_buy_ntl` | §6; NULL until warmup completes |
| top of book | `bid_end`, `ask_end` | best bid/ask at minute end |
| spread | `spread_mean` (price units), `spread_bps_mean`, `spread_bps_med`, `spread_bps_min`, `spread_bps_max` | over valid 1 s samples; bps = (ask − bid) / mid × 10⁴ |
| depth | `bid5_mean`, `ask5_mean`, `bid20_mean`, `ask20_mean` | time-weighted mean USD depth of the top 5 / top 20 levels per side |
| | `bid5_end`, `ask5_end`, `bid20_end`, `ask20_end` | the same at minute end |
| book dynamics | `book_updates` | fast-book snapshots inside the minute |
| | `bid_changes`, `ask_changes`, `mid_changes` | best bid / best ask / mid changes between consecutive fast snapshots |
| | `bid_replenish`, `ask_replenish` | Σ positive increments of top-5 side depth between consecutive fast snapshots while that side's best price is unchanged (a replenishment *proxy*; no queue reconstruction) |
| asset context | `oi_end`, `mark_end`, `oracle_end`, `funding_end`, `impact_bid_end`, `impact_ask_end` | latest `activeAssetCtx` **received** before close, if within 10 s |
| timing | `lat_p50_ms`, `lat_max_ms` | receipt − exchange time over the minute's fills and fast books |
| | `first_recv_at`, `last_recv_at`, `finalized_at`, `ingested_at` | §7 |
| integrity | `n_dup`, `n_late`, `session_id`, `content_sha` | duplicates dropped; late fills included by a revision; collector run; hash of the aggregate |

Derived by `load_microstructure` (never stored): `n_trades`, `n_prints`, `vol`, `ntl`,
`delta_vol` = buy − sell volume, `delta_ntl` = buy − sell notional, `buy_frac` = buy_vol / vol,
`sell_frac`, `vwap` = ntl / vol, `mean_print_ntl`, `lp_sell_n`, `lp_sell_ntl`, `mid_end`,
`spread_end_bps`, `imb5`, `imb20`, `imb5_end`, `imb20_end`, `ret_mid` (mid-to-mid return
from the immediately preceding row, NaN if not adjacent or either mid is missing),
`minute_close`, `available_at`, `complete`.

**Imbalance** is the bounded symmetric measure `(bid − ask) / (bid + ask)` ∈ [−1, 1] on USD
depth: +1 all bids, −1 all asks, 0 balanced. `imb5`/`imb20` use the time-weighted mean depths
(so they recompute exactly on resampling), `*_end` the minute-end depths.

**Never zero-filled.** Trade fields are NULL when the trades feed was not live at all in the
minute; book fields are NULL without a valid sample; a `GAP` row has every metric NULL.

## 4. Book sampling (time, not message count)

The book is sampled on a fixed **1-second exchange-time grid**, `g = minute_open + k s`,
`k = 0..59`. Each sample takes the latest snapshot with `time ≤ g` (carry-in from the previous
minute included). A sample is **valid only if the next snapshot followed within the maximum
gap** (3 s for `book5`, 15 s for `book20`; normal cadence 0.54 s / 5.4 s): the feed provably
continued through `g`. Around a disconnect, a silent subscription or an outage the samples are
invalid, so an unobserved book never counts as observed. Means, medians, minima and maxima of
spread and depth are over valid samples, so a burst of 200 updates in one second affects at
most one of 60 samples (tested). Dynamics counts are the only event-based fields, by design.

All of this uses exchange timestamps only, so the same event stream always gives the same
book fields regardless of when the collector processed it.

## 5. Trade flow and CVD semantics

Signed flow per minute is stored as primitives (`buy_*`, `sell_*`). CVD is **derived at query
time** (`query.cvd`): `reset="daily"` (cumulative from 00:00 UTC), `window="15min"/"1h"`
(rolling), or cumulative from the frame's first row (arbitrary anchor: only differences mean
anything). `cvd_valid` is False whenever any contributing minute is not COMPLETE or a rolling
window is not full. **No absolute all-time CVD is stored**; it would silently absorb every
outage and carries no meaning by itself.

## 6. Large prints (`lp_p99_prior7d_v1`)

No static "whale" dollar threshold. Per coin and UTC day D: threshold = the **99th percentile
of print notional over the prior 7 UTC days** (D−7 … D−1, never D itself), computed from fine
log histograms (bin ratio 1.05, the threshold is the bin's upper edge). It is fixed for the
whole day. **Warmup:** at least 3 of those days with ≥ 720 observed minutes and ≥ 1,000 prints
in total; until then `lp_*` are NULL ("unavailable", not zero). Thresholds are recorded in
`microstructure_lp_thresholds` and each minute row carries the threshold it used. Production
large prints therefore start **3 complete UTC days after the cutover**. Coarse 14-bucket
histograms in every row keep the distribution queryable for a later, different definition.

## 7. Timing and availability

| field | clock | meaning |
|---|---|---|
| exchange `time` | Hyperliquid | assigns fills and book snapshots to minutes |
| `first_recv_at` / `last_recv_at` | Prism (collector host) | receipt of the first/last event contributing to the minute |
| `finalized_at` | Prism | the minute record was written **and fsync'd** to the spool (close + 5 s grace, normally) |
| `available_at` (loader) | Prism | `= finalized_at` (`availability="finalized"`) or `= ingested_at` (`"ingested"`) |
| `ingested_at` | Prism | the row entered the database (≤ 15 min later) |

**Availability is never backdated to exchange time.** Research uses a row only after
`available_at`; a live consumer that reads the database should use `availability="ingested"`.
Transport latency (receipt − exchange) is stored per minute and in the collector status;
processing latency (receipt → applied) is in the status (`processing_us`). The measured
latency includes host clock offset: on the development host (clock 1.31 s ahead of NTP) it
read ~1.3 s, i.e. ~0–100 ms of real transport. Railway's host clock is NTP-disciplined.

## 8. Grace, late events and revisions

* A minute is finalized at **close + 5 s** (receipt clock). Fills and books inside the grace
  are normal.
* A **late fill** (exchange time in a finalized minute, never a duplicate `tid`) arriving up
  to close + 5 s + **60 s** is recorded (`microstructure_late_events`, `revised`) and, at that
  deadline, the minute gets **exactly one revision** (`revision = 1`, `n_late`,
  `flags += late_revision`, new `finalized_at`). The revision-0 row is moved to
  `microstructure_revisions` (Phase 15 semantics), so `known_at` reconstructs both.
* After that window a late fill is **rejected** and recorded (`rejected`). Fills older than
  anything this run observed (the recent-trades snapshot Hyperliquid sends on subscribe) are
  counted (`replay_unobserved`) and ignored.
* A late **book** snapshot is counted (`late_book5`/`late_book20`) and never applied: the
  minute's book state is already final.
* Ingest never applies a same-or-lower revision with different content (a *conflict*): it is
  counted and the job fails loudly. Finalized history is never silently mutated.

## 9. Quality status, gaps, quiet vs offline

| status | meaning |
|---|---|
| `COMPLETE` | trades feed live ≥ 99% of the minute **and** ≥ 57/60 valid samples of both books |
| `TRADE_ONLY` | trades complete, no valid book sample (book feed stale/missing) |
| `BOOK_ONLY` | book complete, trades subscription never live (missing subscription) |
| `PARTIAL` | anything in between (connect/disconnect/restart inside the minute, stale book stretch) |
| `GAP` | nothing observed live; every metric NULL |
| `MISSING` (loader only) | no row at all for an expected minute |

**Quiet vs offline.** A COMPLETE minute with zero fills is a genuinely quiet market. A minute
the collector did not observe is GAP (explicit rows written at restart for up to 7 days of
outage, flag `collector_down`; outage minutes inside a running process from coverage) or
MISSING. Nothing is ever zero-filled. `flags` record `collector_start`, `connect`,
`disconnect`, `collector_down`, `late_revision`. Research must treat anything but COMPLETE as
not fully valid (`complete_only=True`, or `cvd_valid`).

**Backfill policy.** A 20-minute outage is recorded as 20 GAP minutes and stays that way.
Nothing is synthesized. There is no backfill route for book depth, and none for trades in
v1 (see §14).

## 10. Runtime topology and the DuckDB concurrency decision

```
Railway service "prism-runtime" (one container, one volume /data)
  tini → entrypoint.sh
     ├─ supervised loop: market microstructure collect      (persistent, public WS only)
     │       └─ appends /data/microstructure/spool/YYYYMMDD/HH.jsonl  (fsync per record)
     │          writes  /data/microstructure/status.json, raw/…, lp/…
     └─ supercronic → market ops cycle microstructure  (*:06, *:21, *:36, *:51 UTC)
             ├─ market microstructure ingest   spool → /data/prism.duckdb  (the only writer)
             └─ market microstructure health --check   (exit 1 = unhealthy → one infra alert)
```

**Audit: can the persistent collector write the database while the runtime writes it? No.**
DuckDB allows one read-write process per file, and while it holds the file other processes
cannot even open it read-only (`Store` documents and handles this as `DatabaseBusy`). A
collector holding `prism.duckdb` open would block every scheduled job; opening it briefly
every minute would contend with jobs that hold it for minutes (and wait up to 600 s), stalling
the WebSocket loop and corrupting receipt times. Options considered:

| option | verdict |
|---|---|
| collector writes `prism.duckdb` directly | **rejected**: two writer processes; blocks or is blocked by every job |
| second Railway service with its own DuckDB | rejected: Railway never mounts one volume into two services, so the runtime, its backups and research could not read it |
| collector owns a separate DuckDB file on the same volume | workable, but a second database to back up, attach and migrate, for no gain |
| IPC / message queue | unnecessary machinery for ~6 records a minute |
| **append-only spool on the same volume, ingested by the runtime** | **chosen**: the collector never opens the database; the existing single-writer model (authority claim + runtime `flock` + DuckDB lock) is untouched |

Writer coordination: the collector is the only writer of `spool/`, `raw/`, `lp/` and
`status.json` (an exclusive `flock` on `collector.lock` makes a second collector exit 75); the
ingest job is the only reader of `spool/` and only writer of the `.ingested` watermark; the
collector deletes a spool day only after the watermark has passed it. Ingest is idempotent
(per-file byte offsets in `microstructure_ingest_offsets` + content hashes), so a crash or
retry at any point re-records nothing. On a claimed (production) database ingest accepts only
records from the authoritative collector of the same runtime id; scratch records are refused.

**Same service, separate process** is the smallest safe option: same image and revision, same
volume, same authority env, no new Railway service. The collector is supervised by the
entrypoint (restart 10 s after any exit) and starts only when
`PRISM_RUNTIME_ROLE=authoritative` and `PRISM_MICROSTRUCTURE` is not `off`. On a deploy or
restart the container is replaced; the collector is killed with it, its open minute is lost
and the restart writes the outage as GAP rows. Measured overhead: see §13.

## 11. Health, status and alerts

`status.json` (rewritten every 5 s) holds: connected, last message age, per-coin subscription
set and last trade/book/ctx receipt ages, last finalized minute and status per coin, reconnect
count, backoff, last error, latency p50/p90/p99, processing time, counters (fills, duplicates,
late revised/rejected, replay ignored, invalid, statuses), messages and bytes per second, CPU,
max RSS, spool flush time, pending/dropped spool records, raw archive pause. A dead process
cannot refresh it: the health check treats a status older than 60 s as DOWN.

`market microstructure health --check` fails (exit 1) on: collector not running; disconnected
> 5 min (transient reconnects are normal and not alerted); a coin's subscription missing or
book silent > 30 s; spool write failures; no finalized minute for 3 min; newest stored minute
> 45 min old (ingest stuck); > 20% non-COMPLETE minutes for a coin over the last hour. It
warns (no failure) when a coin has had no trade for 30 min (quiet market or silent feed).
The `microstructure` job is a QUIET job: it alerts once (Telegram "PRISM INFRA") on the first
failure after a success, never every 15 minutes; the reasons are in the job log and in
`market microstructure health`. `market status` gets one **Microstructure** row (healthy/stale,
assets subscribed, latest stored minute, non-complete minutes in 24 h, cutover) only once the
capability exists (a status file or stored minutes).

The watchdog inside the collector reconnects when: no message for 10 s; any coin's fast book
silent for 10 s; any subscription not acknowledged 15 s after connecting. Backoff 1 s doubling
to 60 s (±20% jitter), reset after a connection that lasted 60 s.

## 12. Retention and storage

| data | where | retention |
|---|---|---|
| finalized 1-minute aggregates, revisions, late events, thresholds, runs, cutover | `prism.duckdb` (backed up daily) | indefinite |
| spool (JSONL, incl. fine histograms) | `/data/microstructure/spool` | 8 days (`PRISM_MICRO_SPOOL_DAYS`, ≥ 8), and never before ingested |
| raw event journal (normalized fills without addresses, book summaries, ctx, acks, ticks; gzip) | `/data/microstructure/raw` | 1 day (`PRISM_MICRO_RAW_DAYS`); paused automatically below 15% free disk |
| completed-day fine print histograms | `/data/microstructure/lp` | 10 days |
| raw WebSocket JSON / full order books | — | **not kept** (≈ 10 KB/s, ~0.9 GB/day uncompressed; no justification) |

The raw journal is exactly what the engine consumed, so `engine.replay` reproduces the minute
records from it bit-for-bit (tested), which is the reproducibility the raw data is for.

Measured 2026-10-07 from the live smoke (§20) and from replaying its real COMPLETE rows
through the real ingest path at the production cadence (open DB → ingest 15 min → close, 960
cycles = 10 days × 6 assets):

| item | per day (6 assets) | per month | per year |
|---|---|---|---|
| `microstructure_minutes` column data (8,640 rows/day, ~310 B/row compressed) | ~2.6 MiB | ~80 MiB | ~0.95 GiB |
| live DB file growth incl. DuckDB checkpoint slack at 15-min appends (measured, 10 days) | **~4.6 MiB** | **~0.14 GiB** | **~1.6 GiB** |
| other microstructure tables (runs, thresholds, late events, revisions) | < 0.05 MiB | — | — |
| spool (1.8 KB per minute record incl. fine histograms) | ~15.6 MB | rolling 8 days ≈ 0.13 GB | — |
| raw journal (gzip, ~38 KB/min) | ~55 MB | rolling 1 day ≈ 0.06 GB | — |

So the collector's own files level off at ≈ 0.2 GB; the database grows by ≈ 0.14 GiB per
month **and every verified backup is a full copy**. The volume is a fixed 5 GB (Railway Hobby
plan; resizing needs Pro), with ~1.46 GB used on 2026-10-07. To make room, backup retention
was reduced on 2026-10-07 from 7 daily / 5 weekly / 6 manual to **3 daily / 2 weekly /
3 manual** (at most 8 copies, usually ~6, instead of up to 18), and the collector defaults to
1 day of raw journal and 8 days of spool. With ~6 copies, total usage grows roughly
0.9 GB/month after deploy, so the 15%-free warning (~4.25 GB used) is about 3 months away.
Watch `market status` Disk; before the warning, either upgrade to Pro and grow the volume or
cut retention further. The 5% critical line stops *every* job, including `prospective`.
Decisions taken to keep growth down: primitives only (ratios derived),
REAL for depth/spread, integer counts, a 14-int histogram instead of raw sizes, no addresses,
no raw book, and no PRIMARY KEY on `microstructure_minutes` (its ART index measured ~120
B/row, +50%; uniqueness is enforced by the single ingest writer and checked by health).
Low-cardinality strings (`coin`, `status`, `feature_version`, `session_id`) are dictionary-
compressed by DuckDB (measured: effectively free).

## 13. Performance

Measured on the development host (WSL2) during the 24-minute live smoke and by benchmark:

| metric | value |
|---|---|
| inbound messages | ~21–25 msg/s, ~13.3 KB/s (24 subscriptions) |
| events applied | ~40/s (fills are batched per message) |
| processing time per event (receipt → applied) | p50 4.3 µs, p99 21.7 µs |
| parse | ~21–24 µs per message (20-fill trades batch or 20-level book) |
| engine throughput (synthetic burst) | ~228,000 fills/s incl. books and finalizations (~9,000× live load) |
| CPU | 3.84 s over 857 s = **0.45% of one core** |
| memory | max RSS 167 MB (mostly interpreter + numpy/pandas/typer imports) |
| aggregation latency | minute finalized at close + 5.05 s (median; the grace period) |
| spool flush (write + fsync per record) | p50 5.5 ms, max 218 ms (WSL fsync) |
| ingest | 156 records in 89 ms; re-ingest no-op 2 ms; production cadence ≤ ~1.4 s per 15-min batch (a rare 17 s outlier under heavy host load) |
| reconnect overhead | one real venue drop (0.5 s after connecting, "no close frame") recovered with resubscription in 2.5 s; outage minutes became PARTIAL |
| finalize 300 minutes at once (catch-up) | 65 ms |

## 14. Historical data (what can and cannot be backfilled)

Checked 2026-10-07 against Hyperliquid's documentation ("Historical data"):

| dataset | availability | usable for `microstructure_1m_v1`? |
|---|---|---|
| L2 book snapshots | S3 `hyperliquid-archive/market_data/[date]/[hour]/l2Book/[coin].lz4`, requester-pays (AWS account, transfer cost), "uploaded approximately once a month", "no guarantee of timely updates and data may be missing" | no: periodic snapshots with unknown completeness, not the 1 s grid; no live receipt time |
| asset contexts | S3 `asset_ctxs/[date].csv.lz4` (same caveats) | no (not minute-end, no receipt time) |
| fills / trades | S3 `hl-mainnet-node-data` `node_fills_by_block` (current), `node_trades`/`node_fills` (legacy), requester-pays | conceivably trade-flow fields only, as a separate *historical* version; not built |
| recent trades | REST/WS snapshot of the most recent fills only | no (minutes, not history) |
| candles | REST `candleSnapshot` | already used (Phase 15); no flow or book |

There is no reliable, free, complete historical source for trade-by-trade flow **and** book
depth with Prism receipt times. **High-quality microstructure research begins prospectively
from the Phase 24A production cutover.** No historical microstructure is fabricated, imported
or backfilled; if a future phase imports S3 fills it must be a separate feature version marked
historical/backfilled, never mixed with live rows.

## 15. Feature versioning and research freeze

Every row carries `feature_version = microstructure_1m_v1`. The complete definition (feeds,
grace, revision window, sampling step, staleness limits, completeness thresholds, histogram
edges, fine-bin ratio, large-print rule, column list) is data in `definitions.DEFINITION`; its
digest (`7c299a853f4ba141`) is recorded with every collector run and pinned by a test, so any
change fails the build until it ships as a **new** feature version. The digest covers the
constants; a change to the arithmetic in `aggregate.py`/`engine.py` that alters any stored
value is equally a new version (the hand-checked synthetic tests pin that arithmetic).
Historical rows are never reinterpreted. A future study cites: feature version, definition digest, `lp_version`, the
collection interval (`microstructure_cutover.first_minute` → its end), and the availability
basis (`finalized` or `ingested`).

## 16. Production cutover

The production dataset starts when the **deployed authoritative collector's first COMPLETE
minute is ingested** into the claimed production database. Ingest then writes one row to
`microstructure_cutover` (feature version, runtime id, first minute, its finalization time,
collector run, recorded_at) and never changes it. Scratch/development smoke data cannot create
it (different role/runtime id, refused at ingest), and the loader's default
`production_only=True` returns nothing before it. The cutover is not invented during
development; `market microstructure status` shows it once it exists.

## 17. Research interface (for Phase 24B; nothing here trades)

```python
from market_signal.microstructure.query import load_microstructure, cvd, align_asof

m1 = load_microstructure(store, "BTC", start, end, known_at=t)                 # 1-min, causal
q = load_microstructure(store, "BTC", start, end, known_at=t, freq="15min", complete_only=True)
c = cvd(m1, reset="daily")                                                      # or window="1h"
x = align_asof(q, context_frame, right_time="recorded_at")                      # no lookahead
```

* Context (`context_snapshot(asset, t)`), positioning (OI/funding/crowding, Binance taker
  ratios) and Phase 22 descriptive states (range expansion, volume spike, realized-vol state)
  stay in their own tables. Align them **at the microstructure row's `available_at`** with
  `align_asof` (backward as-of, `right_time ≤ available_at`) or by calling
  `context_snapshot(store, coin, row.available_at)`. Tables are never merged.
* Resampling sums flows and counts, recomputes ratios from summed primitives, time-weights
  book means by valid samples, takes first/last/high/low correctly, keeps end-of-bucket values
  from the last minute, drops non-recomputable medians (NaN), and marks a bucket COMPLETE only
  if all its minutes are. The bucket's `available_at` is its last minute's.

## 18. Operations

```
market microstructure status   [--json]   collector + stored rows + cutover + file usage
market microstructure health   [--check] [--json]
market microstructure coverage [--hours 24] [--json]
market microstructure latest BTC [--json]   newest stored and newest spooled minute
market microstructure inspect BTC [--minutes 10] [--json]
market microstructure ingest   [--json]   (runtime job; WRITE)
market microstructure collect  [--duration S] [--no-raw]   (supervised process)
```

### Deploying to Railway

0. Check disk headroom (`railway volume list --json`): see §12. The Hobby volume cannot be
   resized; backup retention was reduced instead.
1. Merge, push, then `deploy/railway/deploy.sh <commit>` as for any revision (pre-deploy
   backup, `git archive` upload). No new service, volume, secret or variable is required:
   the collector uses the existing `PRISM_RUNTIME_ROLE=authoritative` and `PRISM_RUNTIME_ID`.
   Optional variables: `PRISM_MICROSTRUCTURE=off` (disable), `PRISM_MICRO_DIR` (default
   `/data/microstructure`), `PRISM_MICRO_RAW_DAYS` (1), `PRISM_MICRO_SPOOL_DAYS` (8).
2. Boot applies migration 23 on the first writable open (the boot catch-up cycle).
3. Watch: `railway logs --service prism-runtime` (collector start, `run_start`, no
   reconnect storm); `railway ssh --service prism-runtime -- market microstructure health`.
4. After the next `*:06/21/36/51` ingest: `market microstructure status` shows stored rows
   and the **cutover** (first COMPLETE minute). Record the deploy with
   `market ops record-deploy --note 'phase 24A microstructure collector'`.
5. Health check: `market microstructure health --check` (also run by the job every 15 min).
   Restart policy stays **Always**; the collector's own supervision loop covers process exits.

## 19. Limitations

* Prospective only; large prints need 3 days of warmup.
* `ctx` has no exchange timestamp: minute-end context is as-of **receipt**.
* Book depth is sampled at 1 s from a ~0.54 s fast feed and a ~5.4 s 20-level feed; top-20
  means effectively have ~5 s resolution.
* Trade-feed coverage is judged from subscription acknowledgement + connection liveness; a
  venue that acknowledged a subscription and then silently stopped sending only trades (books
  still flowing) cannot be distinguished from a quiet market except by the 30-minute
  "no trade" warning.
* Fills are grouped into prints by transaction hash; Hyperliquid TWAP slices or iceberg-like
  behaviour across transactions are separate prints.
* A deploy or container restart loses the open minute and the few seconds of reconnect
  (recorded as PARTIAL/GAP).
* Receipt times use the host clock; latency figures include its offset.
* Spread/depth are the venue's L2 aggregation (no per-order queue), and the replenishment
  measure is a proxy.

## 20. Live smoke (2026-10-07, scratch only, not production history)

`PRISM_RUNTIME_ROLE=scratch market microstructure collect --dir <scratch>`: run 14:42:45Z →
`kill -9` at 14:52:16Z → down 2 min → restarted 14:54:21Z → stopped 15:08:38Z; ingested into
a scratch database (no cutover can be recorded from scratch data). Per asset: 22 COMPLETE,
2 PARTIAL (the two `collector_start` minutes), 2 GAP (`collector_down`, 14:52 and 14:53, written
on restart), 0 late fills, 0 revisions, 1 duplicate trade and 1 duplicate book dropped,
58 replayed pre-start trades ignored. Host clock was 1.31 s ahead of NTP, so the recorded
receipt − exchange latency (median 1.2 s) is ~0–100 ms of real transport. The first run
segment used an earlier book-validity rule; the sample minutes below are from the second.

Sample finalized minutes (descriptive only; no direction is inferred):

| UTC minute | asset | status | fills | taker Δ notional | buy frac | mean spread | top-5 imb | top-20 imb | max print |
|---|---|---|---|---|---|---|---|---|---|
| 15:05 | BTC | COMPLETE | 189 | −$225.0k | 0.385 | 0.139 bps | −0.372 | −0.298 | $250.0k |
| 15:07 | BTC | COMPLETE | 556 | +$65.0k | 0.507 | 0.189 bps | −0.294 | −0.391 | $1.27M |
| 15:06 | ETH | COMPLETE | 118 | +$1.84M | 0.983 | 0.410 bps | −0.151 | −0.015 | $184.1k |
| 15:07 | SOL | COMPLETE | 87 | −$56.6k | 0.247 | 0.878 bps | −0.400 | −0.012 | $22.4k |
| 15:06 | HYPE | COMPLETE | 455 | −$21.0k | 0.482 | 0.312 bps | −0.272 | +0.017 | $34.8k |
| 15:07 | LINK | COMPLETE | 20 | +$4.6k | 0.937 | 1.285 bps | +0.328 | +0.003 | $4.7k |
| 15:07 | AAVE | COMPLETE | 28 | +$7.1k | 0.923 | 1.073 bps | −0.221 | −0.004 | $5.9k |

Averages per COMPLETE minute: BTC 276 fills / $2.14M; ETH 109 / $0.49M; SOL 65 / $90k; HYPE
277 / $0.47M; LINK 19 / $3.0k; AAVE 20 / $4.4k. Median mean-spread: BTC 0.13, HYPE 0.21,
ETH 0.40, SOL 0.86, AAVE 1.06, LINK 1.20 bps. Large prints: unavailable (warmup, by design).
