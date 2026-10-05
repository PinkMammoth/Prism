# Structural-price primitives and trade-path analytics (Phase 16)

> **These primitives are descriptive infrastructure. Phase 16 does not claim that any of them
> predict returns.**
>
> **Prism uses the term "sweep" only as shorthand for an objectively defined failed-breakout
> event. It does not imply knowledge of stop locations, manipulation or institutional intent.**

Phase 16 turns common discretionary vocabulary into exact measurements, each with a
timestamp that says when it could have been known. These terms are swings, equal highs,
breakouts, sweeps, rejection, structure shifts, retests and MFE/MAE. Nothing here tests
profitability (that is Phase 17), and no live consumer reads any of it (see
[Live-system isolation](#live-system-isolation)).

Code: `src/market_signal/research/structure/`. CLI: `market structure catalog | smoke`. Tests:
`tests/test_structure.py`.

## Architecture: state, event, path

| layer | module | what it is | examples |
|---|---|---|---|
| input | `series.py` | one venue, one coin, one timeframe; availability; ATR normaliser | `BarSeries.from_store(...)` |
| **state** | `levels.py` | something that *exists* at a time | swings, prior extremes, clusters, touches, level state |
| **event** | `events.py` | something that *happens* on a bar | breach, breakout outcome, failed breakout, rejection, structure shift, retest |
| chain | `chain.py` | events linked by `parent_id`, plus entry-delay analytics | `run_chain(ChainSpec, ...)` |
| **path** | `path.py` | what price did after an event | MFE/MAE, thresholds, R, ambiguity |
| registry | `registry.py` | versions and bounded parameters | `swing_v1`, `ChainSpec` |
| regime | `regime.py` | minimal conditioning measurements | `dist_ema_50`, `efficiency_ratio_20` |
| reproducibility | `manifest.py` | build manifest, digests, immutable export | `build_manifest`, `export` |

A level existing is not an event firing. A level is a state record. A breach of that level
is a separate event with its own identity and timestamp, and the breach's outcome becomes
known later still.

### Why not the Lab feature vocabulary?

`lab_features_v1` names per-bar *values* that a daily compiler compares (`gt`, `crosses_above`).
Structural objects are different in kind. A level has an origin, a confirmation and a
lifecycle, and an event refers to a specific level and predecessor event. Forcing them into
`lab_features_v1` would either change that vocabulary's frozen semantics or hide the
reference structure. So Phase 16 adds a separate, versioned registry
(`structure_primitives_v1`). It reuses existing implementations where one exists: ATR
(`technical.atr`), Donchian semantics (`prior_extreme_v1` reproduces `donchian_high_n`
exactly, which is tested) and the Lab regime features.

The declarative entry point for Phase 17 is `ChainSpec`: timeframes, plus the parameters of
each optional stage. A strategy manifest can say "failed breakout of 4h swing levels on 1h,
with rejection, then a 15m structure shift" without bespoke Python. Phase 16 does not change
`StrategyDefinition`. Wiring `ChainSpec` into a strategy schema (v2) is Phase 17's decision,
made with its preregistration.

## Input contract and timestamp semantics

A `BarSeries` is the bars of **one venue, one coin and one timeframe**. Mixing venues is
refused at construction. Every series in one detection must share venue, coin and
availability policy.

| array | meaning |
|---|---|
| `open_time`, `close_time` | UTC half-open bar interval (Phase 15 grid) |
| `available_at[i]` | when bar *i* was available. Observed: `max(close, first_observed_at)`. Assumed: `close + latency` |
| `ready_at[i]` | `max(available_at[0..i])`: when **everything up to bar *i*** was available |
| `segment[i]` | gap-free run id. A missing grid bar starts a new segment |
| `atr[i]` | Wilder ATR(14) including bar *i* |
| `atr_prior[i]` | `atr[i-1]`: the **normaliser** for anything measured on bar *i* |
| `rel_volume[i]` | `volume[i] / mean(volume[i-20..i-1])` |

**Rule 1: a measurement made on bar *i* is known at `ready_at[i]`, never earlier.** It may
read bars ≤ *i*. `ready_at` is cumulative, so a late-published older bar delays everything
after it.

**Rule 2: a level may be referenced by bar *i* of any timeframe only if `formed_ns ≤
open_time[i] < min(until_ns, retired_ns)`.** The level's price must be fully determined
before that bar began. Anything derived from the pair is known no earlier than
`max(ready_at[i], level.available_ns)`.

Rule 2 is what makes multi-timeframe use causal. A 4h swing confirmed by the 4h bar closing
at 12:00 cannot be breached by the 11:00–12:00 1h bar, even if that bar traded above it. The
first 1h bar that can breach it opens at 12:00. If the 4h bar was published late, the
breach is not known until the level was (tested).

**Rule 3: windows never span a data gap.** A swing whose left/right window, a prior-n
window, a breakout failure window, a shift or retest window, or a trade path that crosses a
missing bar is undefined (or `GAP`). It is never guessed.

**Rule 4: unresolved means no event.** If the data ends inside a window, the row is
`UNRESOLVED` and no event is emitted. Appending later bars can resolve it, but can never
change an event that was already available (the future-immunity test).

Normalisation: every "in ATR" value uses `atr_prior` of the bar the measurement is made on,
on that bar's own timeframe. A bar's own range never inflates its unit. "bps" values divide
by the absolute level (or entry) price.

### Observed versus assumed availability (provenance)

Phase 15's distinction is preserved end to end:

- `Availability.observed()` uses Prism's own first observation. It is meaningful for bars
  collected live. For backfilled bars, `first_observed_at` is the backfill instant, so
  observed-mode history is "available" only from the backfill date.
- `Availability.assumed(latency)` is for historical research on backfilled bars. Every
  timestamp derived from it rests on that assumption.

Every level and event row carries `availability_mode` and `assumed_latency_s`. The policy is
part of the event's identity (the same price event under a different latency is a different
event record). Levels and breaches also carry `inputs_live`: whether every bar they read was
observed live. Build manifests record `availability_basis` (`assumed_latency` / `observed`)
per input. **Historical intraday events computed from backfilled bars do not have true
observed-time provenance, and results built on them must say so.**

## Primitive definitions (v1)

All detectors are written once, for the **high** side. The low side runs the same code on
the negated frame `(o, h, l, c) → (−o, −l, −h, −c)` (`BarSeries.oriented`). Bullish and
bearish definitions therefore cannot diverge. Mirrored fixtures produce identical bars,
outcomes, delays and ATR-normalised values (tested).

### `swing_v1` (state)

- **Concept:** a swing high/low (pivot).
- **Definition:** bar *p* is a swing high with `left` L and `right` R when
  `high[p] > max(high[p−L..p−1])` (strict) and `high[p] ≥ max(high[p+1..p+R])`
  (non-strict), all inside one gap-free segment. In a plateau of equal highs only the first
  bar qualifies.
- **Timestamps:** `origin_ns` = pivot bar open. `confirm_idx = p + R`. `formed_ns` = close of
  bar *p+R*. `available_ns = ready_at[p+R]`. **A swing does not exist before its right bars
  have closed.** A "visual pivot" whose right side later exceeds it is never a swing.
- **Parameters:** `left`, `right` ∈ {1, 2, 3, 5}. As a reference level: `max_age_bars` ∈
  {50, 100, 200, 500}, counted from confirmation.
- **Limitations:** pivots are positional, not time-based. Equal highs inside the window
  follow the strict/non-strict rule above.

### `prior_extreme_v1` (state)

- **Concept:** the prior N-bar high/low (Donchian channel), as a level with an origin.
- **Definition:** after bar *j* closes, the level for bar *j+1* is the max high of bars
  *j−n+1..j* (one segment). Its origin is the **latest** bar attaining it. A bar becomes a
  level at the first window where it is that origin. That can be long after its own bar,
  once an older, higher bar has left the window. Reference price at bar *i* ≡
  `donchian_high_n[i]` (tested on every breach).
- **Lifecycle:** a higher bar breaches it (one event). An **equal** bar supersedes it
  (`retired_by = superseded_equal`, no event). It leaves the window after bar `origin + n`.
  A data gap retires it.
- **Parameters:** `n` ∈ {10, 20, 50, 100, 200}.
- Also available as continuous regime features (`donchian_high_n`, `donchian_pos_n`).

### `level_cluster_v1` (equal highs / equal lows; state)

- **Concept:** "equal highs", a group of tops at nearly the same price.
- **Definition:** swings are processed in **confirmation order**. When swing S (price H,
  pivot *p*) is confirmed at bar *j*, candidates are earlier *confirmed* swings of the same
  side, newest first, at most `max_age_bars` older. Walking back, `between` is the highest
  high strictly between the candidate's pivot and *p*. The walk stops when
  `between > H + tol`. A candidate joins if the set's width (max − min) stays ≤ `tol` **and**
  `between` ≤ the set's max. A higher high in between separates two tops; they are not
  "equal". `tol` is fixed at *j*: `bps` of H, or `atr` × ATR at *j*.
- **Record:** `upper` (max member; the reference a breach must exceed), `lower`, `center`
  (mean), `members`, `first_member_ns`, `latest_member_ns`, `tolerance_price`,
  `formed_ns`/`available_ns` (the newest member's confirmation).
- **Lifecycle (versioned, never rewritten):** each new qualifying swing emits a **new
  snapshot**. A snapshot containing an earlier snapshot's members records `extends`. The
  earlier one is `retired` from the new one's `formed_ns`, so there are never duplicate
  events. A snapshot expires `max_age_bars` after its newest member's confirmation, and
  stops at its first breach. The snapshot record never changes after it is available.
  Lifecycle facts learned later live only in `retired_ns`/`retired_by` (tested).
- **Parameters:** swing params; `tolerance` = (`bps` ∈ {5, 10, 20, 30}) or (`atr` ∈ {0.1, 0.2,
  0.3, 0.5}); `max_age_bars` ∈ {50, 100, 200, 500}.
- **Limitations:** greedy, newest-first membership is deterministic but is one choice among
  several possible clusterings.

### `level_touch_v1` and level state (state)

- **Touch:** an eligible bar whose high is within `tolerance` below an intact level.
  **Consecutive in-zone bars are one touch.** Another touch needs a bar entirely below the
  zone first. Touches stop at the breach. Tolerance: `bps` of the level or `atr` × the bar's
  prior ATR.
- **`lifecycle()`** per level: `status` (BREACHED / EXPIRED / OPEN), first breach bar, first
  close-beyond bar, and touches, each with its known time.
- **`level_state()`** per bar: the latest level known by the bar's `ready_at`, with `price`,
  `origin_time`, `available_at`, `age_bars`, `age_hours`, signed `distance` (price, bps,
  ATR), `touches` known so far, `last_touch`, `breached`, `closed_beyond`.

### `level_breach_v1` and `breakout_outcome_v1` (events)

- **Breach:** the **first** eligible bar *b* with `high[b] > level`. The level is retired
  there whatever happens next (one breach per level). It is an event only if
  `high[b] − level ≥ max(min_excursion_bps × |level|, min_excursion_atr × atr_prior[b])`. A
  smaller poke retires the level with no event. Not every higher high is a breakout.
- `breach_kind`: `CLOSE_BEYOND` if `close[b] > level`, else `WICK_ONLY` (intrabar breach only).
- **Outcome**, within `failure_window` *w* (inside = `close ≤ level`):
  - `FAILED` at *b+k*, *k* ∈ 0..*w*: the first close back inside. A WICK_ONLY breach is
    FAILED at *k* = 0;
  - `HELD` at *b+w*: every close *b..b+w* beyond (a sustained breakout);
  - `GAP` / `UNRESOLVED` (no event).
- **Recorded:** `level_price`, `overshoot` (price, bps, ATR), `close_vs_level_bps`,
  `max_excursion` (price, bps, ATR) over *b..resolve*, `extreme` (the swept price),
  `bars_beyond`, `failure_delay_bars`, `close_back` (price, bps, ATR), `ret_breach_bar`,
  `ret_to_resolve`, `rel_volume`, `atr`.
- **Parameters:** level (swing / prior_extreme / cluster), `min_excursion_bps` ∈ {0, 5, 10,
  25}, `min_excursion_atr` ∈ {0, 0.1, 0.25, 0.5}, `failure_window` ∈ {0, 1, 2, 3, 5}.

### `failed_breakout_v1` ("sweep") and `held_breakout_v1` (events)

The FAILED (resp. HELD) outcomes become events of their own. They are timed at the
resolving bar (the close back inside, resp. the last bar of the window), known at that
bar's `ready_at`, with `parent_id` = the breach. A failed **high** breakout is "bearish" only
as a label (`structure_shift_v1` uses it). No direction is assumed for Phase 17, which tests
reversal and continuation.

### `rejection_metrics_v1` (continuous) and `rejection_v1` (event)

Rejection is first a set of continuous measurements per bar. It is not one binary
indicator:

| metric | definition |
|---|---|
| `wick_high` / `wick_low` | `high − max(o, c)` / `min(o, c) − low` |
| `wick_body_{side}` | wick / \|c − o\| (NaN for a zero body) |
| `wick_range_{side}` | wick / range |
| `clv` | `((c − l) − (h − c)) / range` ∈ [−1, 1] (+1 = close at the high) |
| `reversal_atr_{side}` | distance from the side's extreme back to the close, in prior ATR |
| `range_atr` | range / prior ATR |
| `range_expansion` | range / mean range of the 20 bars before |
| `rel_volume`, `ret` | volume ratio; close-to-close return |

The thresholded event `rejection_v1`: for a failed breakout, the first bar from the breach
bar to `within_bars` (∈ {0, 1, 2}) after the failure bar whose wick on the swept side is
≥ `min_wick_range` (∈ {0.33, 0.5, 0.66}) of its range. Status: REJECTION / NONE / UNRESOLVED.
It is known no earlier than the failed breakout.

### `structure_shift_v1` (event)

For a failed **low** breakout (bullish; bearish is the mirror):

1. Take the swing highs of the **confirmation** series (`swing` params) that were **known
   when the failed breakout was known** (`available ≤ sweep available`) and formed by its
   failure-bar close. Use the latest one still intact (no close, or wick if
   `break_on = wick`, above it since it formed), searching back at most 10 swings. It must
   be ≥ `min_excursion_atr` × ATR above the swept extreme; otherwise `NO_REFERENCE`.
2. Scan confirmation bars opening at or after the failure-bar close, at most `window` bars.
   The first bar closing (or trading) above the reference is the **SHIFT**, known at its
   `ready_at`. A bar closing below the swept extreme first is `INVALIDATED`. This is checked
   before the break on the same bar, because OHLC cannot say whether the wick came first.
   Otherwise `EXPIRED`, `GAP`, `UNRESOLVED`, or `NO_DATA` (the confirmation series does not
   cover the sweep, e.g. Hyperliquid 15m history is shorter than 1h).
- **Recorded:** sweep times and extreme, structure level (ID, price, pivot time,
  availability), shift bar, `delay_bars`, `delay_hours`, `move` (shift close − sweep close:
  price, bps, ATR).
- **Parameters:** `swing`, `window` ∈ {5, 10, 20, 50}, `break_on` ∈ {close, wick},
  `min_excursion_atr` ∈ {0, 0.5, 1}.
- A shift can never be timed before its reference swing exists or before its break bar
  (tested).

### `retest_v1` (event)

After a level is broken upward (a structure shift, or any break given as
`break_side/level_price/close_ns`), scan bars opening at or after the break close, at most
`max_delay` (∈ {5, 10, 20}) bars. The first bar whose low comes within `tolerance` (`bps` /
`atr` sets as above) above the level, or goes below it, is the **RETEST**.

- **Recorded:** `delay_bars`, `delay_hours`; `closest_distance` up to and including that
  bar (signed: positive = stayed beyond); `penetration` through the level (price, bps,
  ATR); `first_retest_price` (the bar's open if it gapped into the zone, else the zone
  edge); `held` = the retest bar closed back at/beyond the level. Otherwise `NO_RETEST`,
  `GAP`, `UNRESOLVED`.
- A retest is not assumed to be beneficial. It is only measured.

### Minimal regime primitives

`regime_features(series, names)` measures, causally and in bars of the series' timeframe:
the non-daily `lab_features_v1` tokens (`dist_ema_n`, `dist_sma_n`, `ret_n`, `rvol_n`,
`atr_pct_n`, `donchian_high_n`, ...), plus `donchian_pos_n`, `ma_slope_n_k` and
`efficiency_ratio_n` = |c<sub>t</sub> − c<sub>t−n</sub>| / Σ|Δc| (1 = straight line).
Daily-only and funding tokens are refused. There is no classifier. Phase 17 may freeze
simple definitions from these.

## Event chains and entry-delay analytics

`run_chain(ChainSpec, structure, event, confirm)` runs breach → failed breakout →
[rejection] → [structure shift] → [retest] from the shared detectors. Each stage keeps its
own ID and timestamps and references its predecessor (`parent_id`). Rejection and shift
both hang off the failed breakout; the retest hangs off the shift. Stages are siblings
where they are logically independent, so ablations are joins.

`ChainSpec.ablation` names the deepest stage: `A_breach`, `B_failed_breakout`,
`C_rejection`, `D_structure_shift`, `E_retest` (and `*_no_rejection` variants). A Phase 17
ladder is a list of ChainSpecs that differ only by truncation. Their upstream event IDs are
identical (tested).

The `chain` table (one row per failed breakout) measures what each confirmation stage
**cost** relative to the raw failed breakout:

- `<stage>_delay_hours`: from the failed breakout's availability to the stage's;
- `<stage>_delay_bars`: in event-timeframe bars;
- `<stage>_price_diff`, `_bps`, `_atr`: stage-bar close − failure-bar close;
- `<stage>_entry_cost_bps`: the same, signed in the **reversal** direction (long after a
  failed low breakout). Positive means waiting meant a worse reversal entry price;
- `chain_available_ns`: availability of the deepest included stage (NaT if any included
  stage did not occur).

This is the measurement Phase 17 needs to ask whether confirmation adds information or
only enters later.

## Multi-timeframe semantics

A ChainSpec names `structure_tf` ≥ `event_tf` ≥ `confirm_tf` (e.g. 4h levels, 1h breach,
15m shift). The API enforces:

- levels come from the structure series and are referenced by event bars only under
  Rule 2 (formed before the bar opened; known no earlier than the level);
- the shift's reference swings come from the confirmation series, known by the time the
  failed breakout was known;
- the shift/retest scan starts at the failure-bar close on the confirmation grid;
- all series share venue, coin and availability policy.

Daily bars (`perp_bars`) can be used as structure through `BarSeries.from_frame` with an
assumed latency. They have no observed availability column.

## Trade-path analytics (`trade_path_v1`)

`trade_paths(series, entries, TradePathParams, resolve_with=None)`. Entries are
`event_id`, `entry_after_ns` (normally the event's `available_ns`), `direction` (+1 long /
−1 short), optional `atr` and `invalidation`.

- **Entry:** the **open of the first bar opening at or after** `entry_after_ns`, which is an
  achievable action. The path is that bar plus the next *H − 1* bars (`horizons` ⊂ {1, 2, 4,
  8, 16, 24, 48, 96}). A path with a gap or past the data is **incomplete**: all measures
  NaN, never truncated.
- **Sign convention (one for every output):** shorts are measured on the negated frame, so
  favourable is always "up". **MFE and MAE are non-negative fractions of the entry price**
  (MAE is the positive magnitude of the adverse move), floored at 0. Long: favourable = high
  / close up, adverse = low / close down. Short: the reverse.
- **Close-based vs intrabar are never mixed:** `mfe`/`mae` use highs/lows;
  `mfe_close`/`mae_close` use closes only. `terminal_return` is close-based.
- **Time to excursion:** `bars_to_mfe`/`bars_to_mae` = 0-based offset of the first bar
  reaching the extreme (NaN if the excursion is 0). `mfe_by_ns`/`mae_by_ns` = that bar's close
  (the extreme happened at or before it; OHLC does not say when inside the bar).
  `mfe_mae_order`: which came first.
- **Thresholds** (descriptive, never optimised): symmetric pairs `pct_x` (± x % of entry, x ⊂
  {0.5, 1, 2, 3, 5}) and `atr_x` (± x × event ATR, x ⊂ {0.5, 1, 2, 3}). For each: first
  favourable bar, first adverse bar, by-times and `order`.

### Ex-ante risk and R

Only where the event has an **objective** invalidation, e.g. the swept extreme of a failed
breakout for the reversal direction:
`risk = direction × (entry − invalidation)`, recorded as price, bps, % and ATR.
`risk_status`: `OK`, `UNAVAILABLE` (no natural invalidation: **never invented**) or
`INVALID_AT_ENTRY` (the entry already lies beyond the invalidation). With risk:
`r_terminal`, `r_mfe`, `r_mae`, and per target *k* ∈ {1, 2, 3, 5}: `r_k` = +kR target versus
the −1R stop, using the same ordering rules. Phase 16 characterises paths. It does not build
or tune a stop/target strategy.

### Same-bar ambiguity

OHLC cannot reveal whether a bar's high or low came first. When both levels are first
reached inside **the same bar**, the order is **`AMBIGUOUS_INTRABAR_ORDER`**. It is never
resolved in whichever direction looks better. The only resolution OHLC supports is the
open: a bar that **opens** at or beyond a level touched that level first (a gap). The entry
bar opens at the entry price, so an entry bar touching both stays ambiguous. Other labels:
`FAVOURABLE_FIRST`, `ADVERSE_FIRST`, `NEITHER`, `UNDEFINED` (a level could not be computed,
e.g. no R).

### Nested-timeframe resolution (optional)

`resolve_with=<faster series>` resolves ambiguous rows using the child bars of the ambiguous
parent bar (e.g. four 15m bars inside a 1h bar):

- coverage must be **complete**: every child of the parent interval present, starting at
  the parent open, in one segment. Otherwise `INCOMPLETE_COVERAGE`;
- children must reach both levels too (provider consistency). Otherwise `INCONSISTENT`;
- the child sequence uses the same first-touch rule, so a child bar touching both is
  `STILL_AMBIGUOUS`;
- recorded: `resolution`, `resolution_tf`, `coverage_complete`, `resolved_available_ns`.

The children are closed by the parent's close, so resolution uses no information later
than the parent path. Coverage is limited by history: Hyperliquid 15m covers only its newest
~52 days.

## Identity, versioning and parameters

- **Level IDs** (`slvl_…`): SHA-256 of the canonical JSON of (series identity, primitive
  version, parameters, side) plus the level key (origin open time, or a cluster's sorted
  member open times).
- **Event IDs** (`sev_…`): SHA-256 of the canonical JSON of (provenance, primitive version,
  parameters) plus the reference (level or predecessor event ID) and the event bar's open
  time.
- **Provenance in identity:** venue, coin, timeframe role(s), availability mode and latency,
  and `dataset_key`. That is a registered Lab dataset ID, or `store:<venue>` for direct
  store reads. Content hashes are **not** in the ID. They go in the build manifest, so
  appending newer bars never changes an earlier event's ID. Re-running reproduces every ID
  (tested).
- **Versions:** `swing_v1`, `prior_extreme_v1`, `level_cluster_v1`, `level_touch_v1`,
  `rejection_metrics_v1`, `efficiency_ratio_v1`, `level_breach_v1`, `breakout_outcome_v1`,
  `failed_breakout_v1`, `rejection_v1`, `structure_shift_v1`, `retest_v1`, `trade_path_v1`
  (registry `structure_primitives_v1`). Changing a definition means adding a version.
- **Bounded parameters:** every field accepts only the listed values, normalised so that one
  parameterisation has one spelling (`5` = `5.0`). The registry bounds what is
  *expressible*. Phase 17 preregisters what it actually tests. No combination grid is
  generated in Phase 16.

## Research storage decision

Structural events are **generated on demand**, not persisted in the database. Like the Lab
compiler's masks, they are a pure function of (input bars, availability policy, versions,
parameters), and they are cheap to recompute (below). A persisted copy could go silently
stale. For reproducibility, `manifest.build_manifest` records the source hash per input
series, versions, parameters, software and generation time, plus a digest per output table.
`manifest.export` writes an **immutable** directory `<build_id>/` (Parquet + `manifest.json`)
and refuses to overwrite it. There is no mutable cache.

## Performance

Detectors are numpy over bounded windows: per-level forward scans bounded by `max_age_bars`,
sliding windows, a monotonic stack for prior extremes, and a backward walk with an early
stop for clusters. Hashing reuses a canonical "head" per detector run. Measured on this
machine (WSL2, Python 3.12), full chains (breach → failed → rejection → shift → retest) plus
trade paths with 3 horizons and 8 criteria:

| synthetic 15m bars | swing chain | prior-20 chain | cluster chain | paths (swing) | whole script wall / peak RSS |
|---|---|---|---|---|---|
| 50,000 | 0.58 s | 0.62 s | 0.46 s | 0.21 s | 2.7 s / 0.43 GB |
| 100,000 | 1.08 s | 1.23 s | 0.87 s | 0.36 s | 4.6 s / 0.65 GB |
| 200,000 | 2.22 s | 2.69 s | 1.78 s | 0.64 s | 7.5 s / 1.1 GB |
| 400,000 | 5.02 s | 6.02 s | 3.49 s | 1.40 s | 18.5 s / 2.0 GB |

Scaling is close to linear. Two quadratic paths found while benchmarking were fixed: the
negated low-side frame and the live-bar prefix sum are now built once per series. Memory is
dominated by holding three chains' tables plus ~1.3 M threshold rows at once.

Real data, all six coins: `market structure smoke` (same-timeframe chains on 15m, 1h and 4h,
a 4h/1h/15m chain, and trade paths with nested resolution):

| venue | bars | wall | peak RSS |
|---|---|---|---|
| Hyperliquid (all retained history) | 119,012 | 5.6 s | 234 MB |
| Binance (2026-04-01 → 2026-10-05) | 168,300 | 7.1 s | 275 MB |

## Real-data smoke (descriptive only, 2026-10-05)

Run on a scratch copy of the database, after a bounded public backfill. Hyperliquid: all
retained history; Binance: 2026-04-01 → 2026-10-05. Defaults throughout: swing 2/2, level age
200, min overshoot 0.1 ATR, failure window 2, rejection 0.5, shift window 20 (close), retest
0.2 ATR within 10 bars, assumed latency 60 s. Path rows are per (failed breakout, horizon) for
horizons 4 and 16 bars, in the reversal direction. Venues are never pooled. **No returns are
reported, nothing is compared across parameters, and these counts are not evidence of
anything.** They show that the detectors behave as defined on real data.

| | HL 15m | HL 1h | HL 4h | BN 15m | BN 1h | BN 4h |
|---|---|---|---|---|---|---|
| bars | 30,000 | 29,999 | 29,014 | 107,712 | 26,928 | 6,732 |
| swings (both sides) | 8,309 | 8,425 | 7,940 | 29,857 | 7,585 | 1,844 |
| cluster snapshots | 1,785 | 1,865 | 1,771 | 6,626 | 1,694 | 425 |
| qualifying breaches | 6,331 | 6,473 | 6,026 | 23,038 | 5,856 | 1,392 |
| of which close beyond | 3,709 | 3,581 | 3,430 | 12,652 | 3,035 | 792 |
| held breakouts | 2,120 | 2,149 | 2,064 | 7,245 | 1,838 | 446 |
| failed breakouts | 4,210 | 4,319 | 3,960 | 15,793 | 4,017 | 944 |
| of which same-bar | 2,622 | 2,892 | 2,596 | 10,386 | 2,821 | 600 |
| rejections | 1,632 | 1,690 | 1,468 | 6,287 | 1,589 | 325 |
| structure shifts | 1,329 | 1,329 | 1,202 | 4,860 | 1,230 | 292 |
| retests | 1,167 | 1,161 | 1,045 | 4,357 | 1,087 | 261 |
| path rows / complete | 8,420 / 8,400 | 8,638 / 8,622 | 7,920 / 7,908 | 31,586 / 31,563 | 8,034 / 8,014 | 1,888 / 1,873 |
| R available | 7,461 | 7,571 | 6,944 | 27,894 | 7,065 | 1,657 |
| +1R vs −1R ambiguous in OHLC | 513 | 653 | 577 | 2,190 | 618 | 111 |
| … resolved by child bars | – | 78 | 68 | – | 295 | 64 |
| … still ambiguous | 513 | 575 | 509 | 2,190 | 323 | 47 |

Per coin, failed-breakout counts are similar (HL 1h: 660–775; BN 15m: 2,424–2,783). HYPE is
lowest on Hyperliquid 4h (523, against 603–751 for the other coins) because its 4h history
starts at its 2024-12 listing.

The 4h/1h/15m chain (HL: 1,123 failed breakouts, 80 shifts, 860 `NO_DATA`; BN: 967 failed, 284
shifts, 0 `NO_DATA`) shows the coverage limit working as designed. Hyperliquid 15m starts
2026-08-14 while its 1h starts 2026-03-11, so most Hyperliquid 1h sweeps have no 15m
confirmation data and are reported as such, not as "no shift". The same limit explains why
Hyperliquid's 1h ambiguity is resolved far less often than Binance's.

R is unavailable on ~11–12% of path rows. On the 1h runs this is 1,051 of 8,638 (HL) and 949
of 8,034 (BN) `INVALID_AT_ENTRY`: the next open was already beyond the swept extreme. Another
16 and 20 rows are incomplete paths at the end of the data. None is `UNAVAILABLE`, because a
failed breakout always has its swept extreme. This is recorded, never patched.

## Live-system isolation

No live consumer reads these primitives. The paper engine, co-pilot, forward tracker, Phase
11 corroboration and the runtime schedule contain no reference to `research.structure`, and
the structure package imports no paper/co-pilot/ops/network module and writes no database
row (`test_live_consumers_never_use_structural_primitives`). `market structure` opens the
store read-only. No structural detector can trigger an alert.

## Limitations

- **OHLC ambiguity is irreducible without finer data.** Same-bar orderings are recorded as
  ambiguous. Nested resolution only helps where complete child bars exist; Hyperliquid 15m
  history is ~52 days.
- **Historical availability is assumed.** Backfilled bars have no true publication time. All
  historical intraday events rest on `Availability.assumed(latency)`. Observed-time
  provenance exists only for bars Prism collected live (since Phase 15).
- **Intrabar timing of a level versus a breach.** Rule 2 guarantees the level's *price* was
  set before the event bar began. A level that became *known* (latency) part-way through
  that bar still qualifies, with the event's availability pushed to the level's. The breach
  may have traded before the level was known within that bar. This is recorded in the
  timestamps, not hidden.
- **One breach per level.** After its first qualifying (or sub-threshold) breach a level is
  retired. A second sweep of the same price needs a new level (often the sweep bar's own
  swing).
- **Clustering is greedy.** Newest-first and width-bounded: one deterministic choice, not
  the only defensible one.
- **ATR is Wilder(14) on the event's timeframe only.** An HTF level's distance on LTF bars
  is normalised by LTF ATR.
- **Daily structure** works through `from_frame` with an assumed latency only.
- **No profitability claims.** Phase 16 makes the terms measurable. Phase 17 asks whether
  they contain edge.
