# Phase 23: context intelligence layer

> **Data, semantics, provenance, researchability. No trading.** Nothing in this layer places
> an order, sizes a position, changes risk policy, or is read by the forward tracker, the
> co-pilot, the paper trader or candidate incubation (a test enforces this).

Status: implemented 2026-10-06. Code `src/market_signal/context/` (`taxonomy`, `model`,
`entities`, `ledger`, `macro`, `positioning`, `opportunity`, `snapshot`, `thesis`,
`event_study`, `probes`, `feasibility`, `service`, `providers/{base,calendar,rss,hyperliquid,importer}`),
config `config/context/` (`entities.yaml`, `macro_calendar.yaml`, `sources.yaml`,
`import_example.json`), CLI `market context …` (`cli/context_cmds.py`), migration 22, runtime
jobs `context_macro` / `context_news` / `positioning`, tests `tests/test_context.py` (37).

## 1. Philosophy

Phase 22 established that unconditional, directional candle rules do not clear costs: zero
candidates, median gross edge ≈ 0 against ≈ 18–19 bps of round-trip costs, at ~43
independent opportunities a day. Throughput is not the bottleneck; information is. Phase 22
also found that abnormal range expansion and volume spikes carry information about the *size*
of the next move, not its direction. That result stands unchanged: no Phase 22 threshold,
verdict or variant was touched.

> **Price action is no longer assumed to contain sufficient information by itself. Prism
> treats price/volume primarily as market-state and execution information unless a
> directional edge is independently demonstrated.**

The working model:

- **context** creates the trade thesis (what is happening, and is there a catalyst?);
- **positioning** shows where the pain may be (is leverage building, and on which side?);
- **price, volume and microstructure** help with timing and execution.

> **Context does not automatically imply direction. Every directional thesis must still earn
> evidence.**

Nothing here encodes "CPI lower → BTC up", "hack → long XMR", "listing → long" or "unlock →
short". The layer reports facts with timestamps. The Lab decides later, in preregistered
studies, whether any of them predict anything.

## 2. Event model

Two record types, both append-only (migration 22):

**Observation** (`context_observation_v1`, `context/model.py`) is one report from one source,
normalised by a provider. **Event** (`context_event_v1`) is one logical event. Its row in
`context_events` is its *first* observation, never edited. Every later report is a
`context_event_updates` row.

| field | meaning |
|---|---|
| `event_id` | `ctxev_` + SHA-256 of the dedup key (deterministic) |
| `dedup_key` | the provider's stable key (`macro:us_cpi:2026-10-14`, `statuspage:kraken:<id>`, `hl_universe:listing:FOO`, `unlock:HYPE:2026-10-06`) or an `auto:` key |
| `source_id`, `source_type`, tier | first source; tiers 1 official/structured, 2 news/RSS/manual, 3 AI/social |
| `category`, `subcategory` | bounded, versioned taxonomy (`context_taxonomy_v1`, §4) |
| `title`, `summary` | as reported (no generated narrative) |
| `entities` → `context_asset_links` | explicit entity map (§9): `direct` / `ecosystem` / `market_wide` links, each with `mapping_version` and `linked_at` |
| `country`, `region`, `scope` | `asset` / `exchange` / `systemic` |
| `scheduled`, `event_time` | when it happens or happened (descriptive) |
| `published_at`, `provider_time` | source timestamps (provenance and latency only) |
| `reported_first_seen_at` | an *external* monitor's own first sighting (latency only) |
| **`first_seen_at`** | **when Prism first observed it: the only availability time** |
| `processed_at` | normalisation/classification completion (≥ `first_seen_at`, CHECK) |
| `last_updated_at` | derived: newest update known at the query time |
| `confidence` | `UNCONFIRMED` < `REPORTED` < `CONFIRMED` < `OFFICIAL`, plus `DENIED` |
| materiality / severity | derived at query time (`materiality_v1`, §8) |
| `relevance_end` | provider/manual override of the default window (§7) |
| `source_ref`, `provenance_hash`, `raw_sha256` | URL/id, content hash of the report, raw payload hash (raw payloads archived under `data/raw`) |
| `attributes` | typed: `macro`, `security`, `listing`, `unlock` or flat `generic` facts |
| `observation_mode` | `live` or `historical` (§3) |
| `schema_version`, `taxonomy_version` | versions recorded on every row |

Typed attributes:

- **macro**: `series_key`, `period`, `unit`, `previous`, `consensus`, `actual`,
  `revision_of_previous`, `importance` (1 = tier 1), `markets`, `timezone`, `time_precision`
  (`exact` / `approximate` / `date_only`), `schedule_source`.
- **security**: `protocol`, `chain`, `exploit_type`, `status` (suspected / confirmed / denied /
  resolved), `loss_usd_estimate`, `target_kind` (protocol / bridge / exchange / wallet),
  `deposits_paused`, `withdrawals_paused`, `recovery_status`.
- **listing**: `exchange`, `action` (listing / delisting), `market_type`, `announced_at`,
  `trading_open_at`, `deposit_open_at`, `trading_close_at`, `pairs`. Announcement and
  trading commencement are separate fields and never conflated.
- **unlock**: `unlock_time`, `amount`, `pct_circulating`, `recipient_category`.

Update kinds: `corroboration`, `revision`, `confidence_change`, `status_change`, `denial`,
`resolution`, `value_release`, `consensus`, `relevance_override`, `schedule_change`. Each
update records `observed_at`, `processed_at`, its source, its confidence after applying it, the
exact field changes, and its provenance hash. `state_asof(event, t)` folds the first
observation plus the updates observed by `t`. It also records, per attribute, when Prism first
held the current value (`attr_seen`, e.g. consensus vs actual).

## 3. First-seen semantics

> **Research can use an event only from `first_seen_at`: the moment Prism itself first
> observed it. Never from publication time.**

How hindsight is prevented:

- A provider **cannot** supply `first_seen_at` or `observed_at`. Observations forbid unknown
  fields, and the ledger stamps receipt time from Prism's own clock.
- Every query is as-of. Events need `first_seen_at ≤ t`, updates `observed_at ≤ t`, links
  `linked_at ≤ t`, provider runs `finished_at ≤ t`, intraday bars `first_observed_at ≤ t`
  (with revisions undone), Binance OI/ratios `ingested_at` and period end `≤ t`, Hyperliquid
  OI capture time `≤ t`, FRED values `available_at ≤ t`.
- A relevance window never starts before `first_seen_at`, even for an event published hours
  earlier.
- Nothing is updated or deleted (the context modules contain no `UPDATE`, `DELETE` or
  `INSERT OR REPLACE`; a test greps for them), so the past cannot be rewritten.
- **Historical records** (for example a backfilled corpus) keep the truthful import time as
  `first_seen_at`, keep the known publication time separately, and are flagged
  `observation_mode='historical'`. They never appear in past snapshots. Event studies may use
  them only with `basis='published'`, which labels every row
  `publication_basis_exploratory`. No confirmatory claim may rest on an unknown Prism latency.
- An AI/external monitor's first sighting is kept as `reported_first_seen_at` for latency
  analysis, never as availability.

Leakage fixtures (all in `tests/test_context.py`): publication before Prism saw it; a
provider trying to backdate; suspected→confirmed exploit (the earlier snapshot is re-computed
after the update and reproduces the same ID); a false rumour later denied; a macro consensus
revised before release; a consensus first seen after release (refused); a late Binance
backfill (unknown before ingestion, fully usable after).

### Latency

Every event view carries `information_latency_sec` (first_seen − published) and
`processing_latency_ms` (processed − first_seen). Provider runs record fetch latency.
Example from the smoke run: a Kraken status incident published 15:55:04Z was first seen 16:09Z
(poll cadence). That is exactly the gap later research needs to see.

## 4. Taxonomy (`context_taxonomy_v1`)

`market context taxonomy --json` prints it.

- **macro** (scheduled): `us_cpi`, `us_core_cpi`, `us_pce`, `us_core_pce`, `us_nfp`,
  `us_unemployment`, `us_gdp`, `us_ppi`, `us_retail_sales`, `us_ism_pmi`, `fomc_decision`,
  `fed_speech`, `treasury_auction`, `boj_decision`, `ecb_decision`, `boe_decision`,
  `rate_decision_other`, `inflation_release_other`, `liquidity_fixture`,
  `central_bank_statement`.
- **market_structure**: `exchange_outage`, `exchange_deposit_withdrawal_disruption`,
  `exchange_insolvency`, `listing`, `delisting`, `risk_parameter_change`, `stablecoin_depeg`,
  `etf_flow`, `treasury_flow`, `custody_disruption`, `exchange_maintenance`.
- **protocol**: `chain_halt`, `governance_proposal`, `governance_result`, `token_unlock`,
  `protocol_upgrade`, `treasury_action`, `validator_issue`, `regulatory_action`, `lawsuit`,
  `partnership`, `tokenomics_change`.
- **security**: `exploit_suspected`, `exploit_confirmed`, `bridge_exploit`,
  `stolen_funds_movement`, `key_compromise`, `emergency_pause`.

Tier-1 macro, used for "next tier-1 catalyst": CPI, core CPI, FOMC, NFP, PCE, core PCE, BOJ,
ECB, plus any macro event whose `importance` is 1.

## 5. Macro calendar

**Sources (both official):**

| source | events | how |
|---|---|---|
| FRED release calendar (`/fred/release/dates`, future dates served) | CPI + core CPI (BLS), Employment Situation (NFP + unemployment), Personal Income & Outlays (PCE + core PCE), GDP, PPI, retail sales | dates live from FRED; time = standing 08:30 New York |
| central-bank schedules in `config/context/macro_calendar.yaml` | FOMC (14:00 NY), BOJ (≈12:00 Tokyo, `approximate`), ECB (14:15 Frankfurt), BOE (12:00 London), 2026–2027 | copied from each bank's page, verified 2026-10-06, source URL stored on every event |
| ALFRED vintages (`fred_actuals`) | **actual** (first print as of the release date), **previous** (prior period as known the day before), **revision_of_previous** (prior period in the new vintage) | `value_release` update stamped when Prism fetched it; `live` if within 24 h of release, else `historical` |
| `market context import` | **consensus** (no free official source), decision outcomes, other fixtures | `consensus` updates with their own first-seen time |

Times are stored in UTC from the source time zone, so DST is handled (FOMC 2026-10-28 → 18:00Z,
2026-12-09 → 19:00Z). Treasury auctions, Fed speeches and ISM/PMI are in the taxonomy and
importable, but no free structured feed was wired for them. The aim is not to recreate
Bloomberg.

Smoke result (scratch copy, 2026-10-06): 30 FRED-calendar events and 36 central-bank
decisions stored. First prints arrived for NFP (+29k vs 162k previous, prior month revised
to 133k), unemployment (4.2% vs 4.1%), PCE and core PCE. GDP was correctly deferred ("vintage
not yet published") and retried next run.

### Pre-event state (`macro_state`)

From the events Prism knew at `t`: `next_tier1` / `last_tier1` (with minutes),
`tier1_within` = {5m, 15m, 30m, 60m}, `in_post_event_window` (subcategory post window),
`next_24h`, and `sensitive_next_24h` = {usd, jpy, global_rates, crypto_specific}.

**Blackout awareness is context, not a veto.** Nothing stops, pauses or gates trading around
events. These are conditioning variables for later research (`window_flags` gives the same
per event).

### Post-release surprise (`macro_surprise_v1`)

`surprise(state, history)` reports actual − consensus, `abs_surprise`, `direction_vs_consensus`
(above / below / inline), a standardised surprise (÷ population SD of at least 8 earlier
surprises of the same series), `vs_previous` and `revision_impact`. A consensus first seen
after release is refused (`consensus_after_release`). There is no market-direction mapping.

## 6. News and events: providers

`ContextProvider.fetch(now, last_state) → FetchResult`. The provider only fetches and
normalises. `run_provider` does the rest: timing, raw archiving, `ledger.ingest`, and one
`context_provider_runs` row. A failure writes nothing and is isolated per provider (and per
feed inside RSS).

| tier | provider | source | status |
|---|---|---|---|
| 1 | `fred_calendar`, `fred_actuals` | FRED/ALFRED | live |
| 1 | `central_bank_calendar` | Fed/BOJ/ECB/BOE schedules | live (config) |
| 1 | `hyperliquid_universe` | `POST /info {"type":"meta"}` diff → listing/delisting | live; first run baselines 234 perps |
| 1–2 | `rss` (`rss_classifier_v1`) | Coinbase + Kraken status pages, Fed press, SEC press, CFTC press | live |
| 3 | `import_chatgpt_work` / `import_manual` | `context_import_v1` files | live (CLI) |

**Quality over quantity.** The RSS classifier is deterministic and conservative. A status
item is kept only if it is a trading/market-access outage (cards, payments, support,
prediction markets and similar are dropped), or names a mapped entity (a deposit delay for an
unmapped coin is dropped). Regulator and Fed items are kept only if they name a mapped entity
or a crypto/stablecoin/digital-asset term. "Federal Reserve issues FOMC statement" is attached
as a `value_release` update to the scheduled FOMC event. In the smoke run 105 feed items gave
11 events. Re-polling is idempotent.

Not wired (documented gaps): a general news API (no reliable free one), protocol blogs,
GitHub/security advisories, social feeds. They plug into the same interface. Until then the
importer covers them.

**Primary-source preference** is structural. Tier caps stop a first report from a tier-3
source arriving above `REPORTED`, or a tier-2 source above `CONFIRMED`. Only primary sources
start at `OFFICIAL`.

## 7. Lifecycle, dedup, materiality, relevance

**Lifecycle.** Rumour → acknowledgement → confirmation → loss estimate → pause → recovery are
updates on one event. Status changes (`exploit_suspected` → `exploit_confirmed`) are
`status_change` updates. A denial sets `DENIED`, after which the event is inactive. The first
report is never overwritten.

**Dedup/linking** (deterministic, in order):

1. the provider's `dedup_key`, which is authoritative: no key match means a new event (many
   scheduled events share one calendar URL);
2. keyless reports: the same `source_ref` (URL);
3. keyless, unscheduled: same category, first seen within `LINK_WINDOW` before this report
   (macro 12 h, market structure 48 h, protocol 72 h, security 7 d), sharing a primary entity
   or direct asset;
4. otherwise a new event.

An identical report (same provenance hash, which ignores the raw-payload hash) is a duplicate.
Ten articles about one hack are one event with 1 first source, N corroborating sources and a
confidence history.

**Confidence evolution (`corroboration_v1`).** It never silently drops (only an explicit
denial or `confidence_change` lowers it). Two distinct tier-1/2 sources at `REPORTED` or
better make it at least `CONFIRMED`.

**Materiality (`materiality_v1`)** = min(100, base importance by subcategory + scope bonus
(systemic +10, exchange +5) + loss bonus (≥ $1M/$10M/$100M: +5/+10/+15)) × confidence weight
(0 / 0.4 / 0.7 / 0.9 / 1.0) × asset-link weight (direct 1.0, ecosystem 0.6, market-wide 0.5).
Components are returned with the score. **Materiality means potential market relevance; it
is not directional edge.** No LLM assigns it.

**Relevance windows (`relevance_windows_v1`).** Per subcategory (pre, post) around the
anchor: event time for scheduled events, unlock time for unlocks, trading open/close for
listings, otherwise first seen. Examples: CPI 24 h / 4 h, FOMC 48 h / 24 h, confirmed exploit
0 / 7 d, token unlock 72 h / 48 h, delisting 72 h / 72 h. A provider or manual
`relevance_end` overrides the default, and a denied event is never active. Stale news does
not stay active.

## 8. Asset mapping

`config/context/entities.yaml` is the only source of event→asset relationships. Matching is
exact, whole-word aliases (case-insensitive), plus upper-case-only tickers (`ETH`, never
"eth"). Aliases must be unambiguous: bare "circle" was removed after it matched "Banking
Circle" in the smoke run. Link types are `direct` (Hyperliquid → HYPE, Monero → XMR, Binance
→ BNB), `ecosystem` (Aave → ETH, "privacy coins" → XMR + ZEC) and `market_wide` (macro and
systemic events → BTC, ETH, SOL, HYPE, LINK, AAVE, XMR, ZEC, BNB). The mapping version (file
hash) is stored on every link. Venue-scoped events (listing, delisting, maintenance,
per-coin transfer delays) do not link the venue's own token: a Hyperliquid listing of FOO is
not a HYPE event. An outage on a venue does link it. Unknown entities are rejected on import,
never guessed.

**Privacy-asset research support.** XMR and ZEC are mapped, and a `privacy` sector entity
exists. A later study can therefore ask whether hacks/exploits, sanctions/privacy narratives,
exchange restrictions or security incidents relate to XMR/ZEC moves. Nothing here encodes
such a relationship.

## 9. Positioning / OI (`positioning_context_v1`)

Per venue, never merged, as known at `t`:

| field | Hyperliquid | Binance USD-M |
|---|---|---|
| OI level (base units), notional | fixed-hour capture + legacy snapshots | `perp_oi_history` 1h |
| OI change 1h/4h/24h (by elapsed time), OI level pct (30d), **z of 24h change** (vs its own 30d distribution, ≥ 48 points) | ✓ | ✓ |
| funding 24h mean, 90-day percentile, annualised | settled hourly funding | where stored (not HYPE) |
| basis | mark vs oracle (bps), predicted funding | n/a |
| long/short skew | none published | global account ratio + 30-day percentile, top-trader account and **position** ratios, taker buy/sell volume |
| price change 24h | mark | n/a |

Staleness: values older than 3 h are withheld (`None`) and the snapshot adds a warning.
Availability: strict by default (Prism's own sighting). After a late backfill, the whole known
history becomes usable, indexed by market time, never before ingestion.
`--assumed-latency` (period end + 15 min) is for research on backfilled history only, and is
labelled.

New collection (data only):

- `context_ls_ratios`: four Binance ratio series per coin, hourly, rolling ~30-day backfill,
  insert-only (17,034 rows on first run).
- `context_hl_oi_hourly`: the fixed-hour Hyperliquid capture (§11).

## 10. Crowding and vulnerability

> **Every perp contract has one long and one short.** Aggregate OI is never "mostly long".
> Skew is inferred only from funding, basis and venue ratios, combined with OI change.

**`crowding_v1`** returns `leverage`: expanding / contracting / stable / unknown (24h OI change
z ≥ ±1.5, or ±10% when history is too short for z), and `skew`: `crowded_long_like` /
`crowded_short_like` / neutral / unknown. A skew label needs a skew measure (funding
percentile or account-ratio percentile) **and** two agreeing conditions out of {funding ≥ p90
(≤ p10), leverage expanding, ratio ≥ p90 (≤ p10)}. With no skew measure, skew is `unknown`,
however much OI grew. The evidence list and a fixed terminology note go with every result.
The `_like` suffix is deliberate: these describe observable conditions, not intent. No
manipulation or liquidation targeting is claimed.

**`vulnerability_v1`** counts the observable ingredients of positioning stress per side:
funding extreme, rapid OI growth, price failing to follow (OI rising while price moved
≤ +0.5% for longs, symmetric for shorts), ratio skew, and a catalyst within 24 h. The level is
`unknown` with fewer than 3 available inputs, otherwise low / elevated / high. These are
counts only: no direction, no trade, no liquidation levels (none are fabricated from OI).

## 11. Hyperliquid OI cadence

**Implemented: fixed hourly capture.** The `positioning` runtime job runs at `*:04` UTC and
writes one row per coin per UTC hour into `context_hl_oi_hourly`. The first capture in the
hour wins, a retry or duplicate trigger is a no-op, and a CHECK keeps `captured_at` inside its
grid hour (a capture that straddles the hour boundary is discarded). It stores OI, notional,
mark, oracle, mid, funding, premium, **impact bid/ask** and day volume.

Old irregular observations are untouched: `perp_snapshots` and the `oi` job carry on exactly
as before. **The cutover is recorded as the first stored grid hour.** `market context status`
shows it as `hl_hourly_cutover`, and `context.positioning.hl_cutover()` returns it. It is set
by the first scheduled run after this commit is deployed. The local smoke run captured
2026-10-06T16:00Z on a scratch copy only, so that is not the production cutover.

## 12. Liquidation data feasibility (verified 2026-10-06)

| venue | historical | live | limits | verdict |
|---|---|---|---|---|
| Binance USD-M | **none**: REST `allForceOrders` returns 404 (checked live) | WS `<sym>@forceOrder` / `!forceOrder@arr`, keyless | **at most one liquidation per symbol per 1000 ms** ("snapshot" in the docs), so cascades are undercounted; timestamps: event time E, trade time o.T | a lower-bound sampler only; never sum as totals |
| Hyperliquid | per user only (`userFills`) | per subscribed user (`userEvents` / `userFills` liquidation fields) | no public all-market liquidation stream; public trades carry buyer/seller addresses but **no liquidation flag** (checked live) | not feasible as aggregate data |
| Aggregators (Coinglass etc.) | yes | yes | paid; "liquidation levels" are model output | out of scope (£0 policy; models are not facts) |

**No ingestion interface was built**, because no reliable, unthrottled public liquidation feed
exists. Liquidation levels are never inferred from OI. Details: `market context feasibility
--json`.

## 13. Microstructure feasibility

Hyperliquid (the execution venue), checked live:

| item | source | prospective? |
|---|---|---|
| individual trades + aggressor side | WS `trades` / REST `recentTrades`: side B/A, px, sz, ms time, tid, both user addresses | yes, needs an always-on WS process |
| best bid/ask, spread | WS `bbo`, REST `l2Book` | yes |
| top-N depth, book imbalance | REST `l2Book` (20 levels/side), WS `l2Book` (5 fast / 20) | yes; periodic REST snapshots are cheap |
| impact prices (cost to trade size) | `metaAndAssetCtxs.impactPxs` | **implemented** (hourly) |
| large prints, trade-flow imbalance, CVD | derived from WS trades | yes with a WS collector |
| taker buy/sell ratio | Binance `takerlongshortRatio` (~30 d) | **implemented** (hourly) |

**Recommended next (not built):** a small always-on Hyperliquid WS collector for the six
perps. It would store 1-minute aggregates (aggressor-signed volume, large-print counts, CVD)
and top-5 bbo/depth snapshots every 10–60 s, not raw ticks. The current runtime is cron-style
(one-shot jobs) and cannot hold a WebSocket open, which is why this is a separate step.

## 14. External / traditional market data

| series | source | latency / history | licence | status |
|---|---|---|---|---|
| 2Y/10Y yields | FRED DGS2/DGS10 | daily, ~1–2 d; decades | public | already ingested |
| VIX | FRED VIXCLS | daily close, next day | public via FRED | already ingested |
| USD broad (DXY proxy) | FRED DTWEXBGS | daily, ~1 week | public (DXY itself is ICE) | already ingested |
| USDJPY | FRED DEXJPUS | daily noon NY | public | **added** to `config/macro.yaml` |
| S&P 500 | FRED SP500 | daily close; 10 years only | S&P licence via FRED | **added** |
| WTI | FRED DCOILWTICO | daily, several days' lag | public | **added** |
| gold | none free + official (LBMA left FRED) | — | licensed | not ingested |
| S&P/Nasdaq futures intraday | no free official feed; Tiingo IEX has SPY/QQQ intraday | near real time | free-tier terms | not ingested |

The new series arrive with the next `market update`. Snapshots expose every FRED value known
at `t` under `market.fred_known`, plus BTC 24h direction and perp breadth (share of the six
perps up over 24h, from intraday bars known at `t`). Whether any of this matters to crypto is
for later research. Nothing is assumed (no astrology for macro either).

## 15. ChatGPT Work integration

A monitoring session writes a `context_import_v1` JSON file (`config/context/import_example.json`)
and runs `market context import FILE --json`:

```json
{"schema": "context_import_v1", "producer": "chatgpt_work", "session": "…",
 "items": [{"event_time": "…Z", "first_seen_at": "…Z", "published_at": "…Z",
            "source": "Protocol status page", "url": "https://…", "assets": ["AAVE"],
            "entities": ["aave"], "category": "security", "subcategory": "exploit_suspected",
            "title": "…", "summary": "…", "confidence": "REPORTED",
            "attributes": {"kind": "security", "status": "suspected"}}]}
```

The importer validates and deduplicates. It enforces strict JSON (duplicate keys rejected),
no unknown fields, a known taxonomy, a category matching the subcategory, known entities only,
http(s) URLs, and `first_seen_at` / `published_at` not after receipt. **AI boundary:** any key
expressing a trade (direction, side, size, order, leverage, target, stop, entry, exit, signal,
recommendation, sentiment…) rejects the item. Imported AI reports are tier 3, so they are
capped at `REPORTED` until an independent tier-1/2 source corroborates them. Availability is
Prism's receipt time, and the monitor's own sighting is kept for latency. A re-import is a
duplicate, and a matching official report later becomes a corroborating update. Work is one
route, never the only one.

## 16. Context snapshots

`context_snapshot(store, asset, t)` / `market context snapshot HYPE [--at …] [--record] --json`
returns only what Prism knew at `t`: `active_events`, `recent_events` (7 d),
`upcoming_linked`, `next_scheduled_catalyst`, `macro` (§5), `positioning` (§9–10), `activity`
(§17), `market` (§14), `freshness` (provider health as of `t`, stale warnings), counts, and
`semantics: "context only: no direction, no recommendation, no veto"`.

The payload is canonical JSON, and `snapshot_id` is its content hash. Recomputing a past
snapshot from the unchanged history reproduces the same ID, which is the "what did Prism know
at 14:37 UTC?" check. `--record` appends it to `context_snapshots` so a thesis can cite it.
The `ContextIndex` loads events/updates/links once for research over many timestamps and
gives identical snapshots.

Smoke example (scratch copy of the local DB + live providers, 2026-10-06 ~16:20Z):

```text
HYPE  ctxsnap_8fcb3ab8a1c1…
  next tier-1: us_cpi 2026-10-14T12:30Z (188 h); last: us_nfp 2026-10-02T12:30Z
  tier1_within: 5m/15m/30m/60m all false; post-event window: false
  active: 5 market-wide regulatory items (materiality 38) + token_unlock HYPE (REPORTED, 31, direct)
  activity: quiet (tr/atr 1.45, rel volume 1.16, rv low)
  binance: OI 4.13M HYPE, 24h −2.1% (z −0.55), L/S account ratio 1.54 (pct 0.73),
           top-trader position ratio 2.40, taker buy/sell 0.66 → stable / neutral
  hyperliquid: OI 20.4M HYPE, basis −6.2 bps, funding pct(90d) 0.69, 24h change unknown
           (local snapshot history has gaps) → unknown / neutral
  warnings: hyperliquid: OI history too short for a 24h change; binance: no settled funding…
```

`market context brief` adds major upcoming macro, active crypto events, assets with unusual
positioning, elevated activity, and stale/missing-data warnings, with no recommendations.

## 17. Opportunity (activity) state

`opportunity_state_v1` keeps Phase 22's only robust findings as **non-directional** state,
using Phase 22's own primitives on closed 1h bars known at `t`: `range_expansion` (log TR /
prior ATR ≥ 2.5), `volume_spike` (volume / prior 20-bar mean ≥ 3), and `rv_state`
(realised-vol(24) 30-day percentile terciles). The state is `elevated` / `normal` / `quiet` /
`unknown`. It answers "is activity elevated?", never "which way?".

## 18. Thesis object (`trade_thesis_v1`)

`TradeThesis` combines catalyst/context, positioning, activity, an **optional** direction
hypothesis (long / short / none) with the rule that produced it, an invalidation idea, and an
expiry (at most 30 days). The status is always `research_only` (DB CHECK). There is no size,
leverage or order field.

Provenance is complete and derived, not typed in: the context, positioning and activity fields
are read from a **recorded snapshot**. `evidence` holds `snapshot_id`, cited `event_ids` (each
must be in that snapshot, otherwise the thesis is rejected), OI observations, the price/volume
state (bar close, TR/ATR, relative volume, RV state) and every rule version used. CLI:
`market context thesis SNAPSHOT_ID --direction … --invalidation … --expires …`.

## 19. AI reasoning boundary

An AI/LLM **may** summarise events, propose entity mappings (a human adds them to the YAML),
propose categories, generate hypotheses and explain context. It **may not** place orders,
choose size, override risk, invent facts or backdate knowledge. In practice:

- The core is LLM-independent. Every provider, classifier, materiality rule, crowding rule
  and snapshot is deterministic, so the layer runs with no model or API at all.
- AI output enters only through validated paths. `market context import` (facts; trade
  fields rejected; tier 3; receipt-time availability) and `thesis.proposal_from_ai` (only
  `ThesisProposal` fields; execution fields rejected; cited events must exist in a recorded
  snapshot; the thesis is rebuilt deterministically).
- No sentiment score is computed or stored. If sentiment is ever added, it is one feature
  that must prove its value in the Lab.
- If the evidence is insufficient, the output says `unknown`. No narrative is created because
  an event and a price move happened close together.

The intended workflow is:

```text
context layer → candidate thesis generation → deterministic validation
             → Lab study / prospective incubation → paper → eventually live
```

and **not** `LLM reads Twitter → places trade`.

## 20. Research workflow

- **Event studies** (`context.event_study`, `context_event_study_v1`). For any set of events
  × assets: pre-event return, post returns at 15m/1h/4h/24h, BTC-adjusted abnormal return,
  volatility and volume ratios against the prior 7 days, Hyperliquid OI change, mean funding,
  and **reaction-time analytics** (minutes to ±0.5/1/2%, peak move and time, final move,
  retrace share, persistence). The basis is `first_seen` by default (the only "could Prism
  have traded it?" basis), `event_time` for scheduled releases, or `published` (historical,
  exploratory-labelled). Descriptive only; no hypotheses run.
- **Macro response research**, possible now and not run: BTC vs CPI surprise
  (`surprise.standardised` × `ret_*`), alts after FOMC, BOJ surprise propagation (DEXJPUS),
  yields/risk alignment (DGS2/DGS10/SP500), and pre-event positioning (snapshot at
  `event_time − Δ`) vs response.
- **Phase 22 probes** (`context.probes`). `interesting_variants(payload)` lists the seven
  INTERESTING variants from the stored result. `attach_context(store, events)` adds `ctx_*`
  columns (minutes to tier 1, tier-1 within 60m, post-event, active linked events, max
  materiality, HL crowding, activity) to any Phase 22 event frame, as known at each signal's
  `signal_ns`. Nothing is rerun or rescored.
- **Historical corpus.** None was built in Phase 23. No bounded, reliable historical crypto
  event source with publication timestamps was available for free; the importer accepts one
  later with `observation_mode='historical'` (§3).

## 21. Runtime (Railway)

| job | schedule (UTC) | steps | notes |
|---|---|---|---|
| `context_macro` | 00:20, 12:42, 13:42, 19:12 | `context refresh --group macro` | just after 08:30 NY in EDT and EST, after FOMC statements |
| `context_news` | `*:08`, `*:38` | `context refresh --group news` | quiet alerts; wait ≤ 15 min |
| `positioning` | `*:04` | `context refresh --group positioning` | fixed-hour HL capture + Binance ratio top-up; wait ≤ 25 min so it lands in its hour |

All jobs go through the existing runtime lock and authority checks. Every step is idempotent
and preserves `first_seen_at`. A failure exits non-zero and alerts through the existing infra
alert path: the quiet jobs alert on the first failure after a success, and a provider with 3
consecutive failures shows `FAILING`. `market status` has a **Context** row, and `market
context providers` shows last success, last event, latency, errors, staleness and rate-limit
state per provider. Request budget: about 6 feeds + 1 HL call per news run, 1 HL call + 24
Binance calls per hour, and a handful of FRED calls per macro run.

## 22. CLI

All read commands take `--json` and `--at` (as-of): `status`, `providers`, `events
[--asset --category --active]`, `upcoming`, `asset SYM`, `snapshot SYM [--record]`,
`positioning SYM [--assumed-latency]`, `brief`, `event ID`, `taxonomy`, `feasibility`. The
write commands are `refresh [--group macro|news|positioning]`, `import FILE`, `thesis
SNAPSHOT_ID`.

## 23. Performance (benchmark, 2026-10-06)

Measured on a 5,000-observation synthetic ledger (858 events, 4,142 updates), with the full
test suite running concurrently on the same machine: ingest ≈ 60 ms per observation (a DuckDB
commit per observation; production volume is tens per poll), snapshot ≈ 0.3–0.45 s,
asset mapping ≈ 29 µs, linked-event lookup ≈ 0.7 ms. "What does Prism know about HYPE right
now?" never scans more than the asset's linked events in the ±30–75-day window.

## 24. Limitations

- **Short history.** Context collection starts now. Before Phase 23 there is no
  Prism-observed event history, so any study of event *responses* using Prism latency is
  prospective.
- **Consensus.** No free official consensus source exists, so surprises exist only where a
  consensus is imported. Without one, `vs_previous` is reported and labelled as not a
  surprise.
- **Positioning history.** Hyperliquid OI history is short and irregular until the fixed-hour
  capture accumulates. Binance ratios and OI only reach ~30 days back. Binance funding is not
  stored for HYPE.
- **Feeds.** The Fed/SEC/CFTC feeds are coarse (crypto-keyword gated). There is no news API,
  social feed, protocol blog or security-advisory feed yet. Token unlocks come only through
  the importer.
- **Classifier and mapping.** These are deterministic keyword rules and explicit aliases.
  They miss things and occasionally over-match, so a wrong match is fixed in the versioned
  YAML or classifier, never retro-edited in stored events.
- **Availability of some inputs.** FRED `available_at` and settled funding `available_at`
  are rule-based, not Prism-sighting times. Intraday bars and HL captures are true sightings.
- **No liquidation or tick data** (§12–13).
- **ALFRED first prints** are fetched by polling. If Prism polls later than 24 h after a
  release, the value is marked `historical`.
- The smoke results above come from a **scratch copy of the local database** (last written
  2026-10-04) plus live public API calls, not from production. Production starts collecting
  when this commit is deployed.

## 25. Live compatibility

No order path, risk policy, paper policy, Phase 21 freeze or Phase 22 threshold changed.
Migration 22 adds only `context_*` tables. The only shared-code changes are additive: two
impact-price columns in the Hyperliquid context parser (not stored in `perp_snapshots`),
three FRED series in `config/macro.yaml`, the `Context` row in `market status`, and three
new runtime jobs. Tests enforce that `paper`, `copilot`, `perps`, `research/incubation` and
the Lab forward tracker do not import `market_signal.context`.
