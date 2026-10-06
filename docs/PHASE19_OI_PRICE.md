# Phase 19: open interest × price × funding

> **EXPLORATORY.** Historical OI availability is reconstructed under an explicit latency
> assumption rather than observed in real time: every Binance OI row in the window was
> backfilled, so its publication time is *assumed*. The window is **23 days of one market
> regime**, the universe is six coins, and nothing here is untouched validation. Nothing in
> this document is validated, and no result feeds the forward tracker, the co-pilot or the
> paper account.

## Summary

Study `sstudy_da3df2355a79a9a6598f2165ed69353c7d69f993b8b675f4e019f0f2c7a9a358`
(`phase19_oi_price_v1`) was frozen on a scratch copy at software commit `37fb343` (clean
tree), then evaluated once (`srun_08a749000c1d4d93b8267815e7283dbd`). A fresh scratch copy
re-registered the identical study ID and reproduced the result digest `21abe032…` exactly
(`srun_fd98e9df…`), as did an explicit `rerun_of` run (`srun_10ed51f5…`). Before
registration the harness ran only on synthetic data (null calibration and a power check).

**Research question.** Does a change in perp open interest, read together with price
direction and funding, contain forward information beyond price alone?

**Answer from this window: not demonstrably.** No hypothesis reaches ROBUST (the rung also
needs a second venue, and Hyperliquid has no OI inside the window). Three member-directions
pass the frozen PROMISING policy. All three shrink by 30–45% against the matching price-only
control. Every family that tests "beyond price" directly (`oi_increment`, `magnitude`,
`ic_oi_beyond_price`) is NO EVIDENCE or INSUFFICIENT. The one coherent pattern is that **OI
contraction was followed by further weakness, not a rebound**, over 6–12 h. It is
consistent across the coin measure, the USD measure, z-shocks and the price-down/OI-down
quadrant, but it is not significant beyond price.

Verdicts (57 members × 2 directions = 114):

| ROBUST | PROMISING | WEAK | REJECTED | NO EVIDENCE | INSUFFICIENT |
|---:|---:|---:|---:|---:|---:|
| 0 | 3 | 3 | 7 | 51 | 50 |

## Key questions

| # | question | answer |
|---|---|---|
| 1 | Does OI change predict future price direction? | **Only on the contraction side, and weakly.** Large coin-OI contraction: −52 bps over 6 h vs every bar of the coin (WEAK, q = 0.11). Negative OI z-shock: −56 bps (WEAK, q = 0.17). Large USD-OI contraction: −65 bps (PROMISING, q = 0.032), but the USD measure carries the price move mechanically. OI expansion alone: +0.2 bps (coin), −2.9 bps (shock). Cross-sectional IC of OI change vs next return: −0.039 (NO EVIDENCE). |
| 2 | Does OI add information beyond price? | **Not demonstrated.** All four quadrants against the price-matched control are NO EVIDENCE (min q = 0.36). `ic_oi_beyond_price` = −0.033. Descriptively, the contraction effects keep −43 to −45 bps after price matching, but no "beyond price" test is significant. |
| 3 | Are the four price/OI quadrants different? | **Not significantly.** Vs every bar of the coin: up/up +16, up/down −33, down/up −12, down/down +35 bps (continuation-oriented); every q ≥ 0.66. The "capitulation rebound" reading of down/down is **REJECTED** (the upper bound of a rebound is below 10 bps). |
| 4 | Do OI shocks predict continuation or reversal? | Positive shocks: nothing (−3 bps; with a price move +23, NO EVIDENCE). Negative shocks: price lower afterwards (−56 bps, WEAK). Shocks conditioned on the price move: NO EVIDENCE. Shocks × funding: INSUFFICIENT (3 and 10 events). |
| 5 | Does OI contraction predict rebound / exhaustion? | **No; the rebound is REJECTED** for the large coin contraction, the negative shock and the down/down quadrant. The sign leaned to *continued weakness* in all of them. |
| 6 | Does funding improve OI interpretation? | **Mostly untestable here.** 14 of 18 `funding_oi` verdicts and all 8 `crowding_increment` verdicts are INSUFFICIENT. One cell passes: funding in either tail + price/OI divergence → **reversal** +44 bps (PROMISING, q = 0.056). It is HYPE-heavy (56%) and flat in the middle third. |
| 7 | Do funding + OI crowding signals contain edge? | **INSUFFICIENT.** Crowded-long (funding pct ≥ 0.9 + OI expansion) produced 5 independent events, crowded-short 23. Funding was positive 81–99% of hours, so the "low" tail means *low relative to its own last month*, not negative funding. |
| 8 | Does large OI growth without price progress predict anything? | **INSUFFICIENT** (8 events flat; 18 with little progress). The opposite case passes: large OI expansion **with** a large price move continued +85 bps (PROMISING, q = 0.036). It falls to +50 bps against the price-matched control and reverses by 24 h (−23). |
| 9 | Does OI predict volatility even without direction? | **No.** Every tested volatility member is within ±11 bps of its magnitude-matched control (q ≥ 0.96). Pooled Spearman of OI change vs future absolute return: +0.009. |
| 10 | Are effects broad across assets? | Mixed. USD-OI contraction: 6 of 6 assets negative. Coin-OI contraction: LINK + HYPE are 59% of the contribution. Expansion with progress: AAVE is 46% (3 events, +465 bps), but every leave-one-out stays positive. Divergence reversal: HYPE 56%. |
| 11 | Are effects stable through time? | Only partly, and "time" is 23 days in thirds of ~8 days. USD-OI contraction −73 / −53 / −58 (stable). Expansion with progress +21 / +93 / +133 (late-heavy). Divergence reversal +38 / +0 / +72 (flat middle). |
| 12 | Do Binance and Hyperliquid tell similar stories? | **Cannot be assessed.** Hyperliquid has **zero** OI snapshots inside the window (its first snapshot is 2026-10-03). The cross-venue family is INSUFFICIENT by its frozen coverage gate. |
| 13 | Any cross-venue agreement/disagreement effect? | INSUFFICIENT (deferred, not forced). |
| 14 | Does OI lead price or react to it? | **Mostly contemporaneous.** Mean per-coin Spearman: OI change vs same-window price change +0.149; OI now vs next executable return +0.084 (the IC test is NO EVIDENCE); price now vs next OI change +0.024. Neither clearly leads. |

## OI source semantics

| | Binance USD-M (primary) | Hyperliquid (comparison) |
|---|---|---|
| table | `perp_oi_history` (`period='1h'`) | `perp_snapshots` (`source='hyperliquid'`) |
| what it is | provider statistic `sumOpenInterest` (coins) and `sumOpenInterestValue` (USD), stamped on the hour | Prism's own capture of `metaAndAssetCtxs`: `open_interest` (coins), `oi_notional = open_interest × mark_px` (Prism-derived) |
| timestamp | statistics timestamp `observed_at` (hour boundary) | capture time `snapshot_at` |
| history | ~30-day API window; older only if already stored | prospective only; missed captures are lost |
| availability in this study | **assumed** `observed_at + 1800 s` (one live check served the 21:00 point by 21:18) | observed (capture time) |
| role | primary historical exploratory study | corroborative / prospective, coverage-gated |

The two series are never merged, pooled or used to fill each other (tested). Every feature
records its venue, its source, its unit (coin or USD) and its availability. Coin OI is the
primary positioning measure. USD OI is a **separate** measure: its change equals the coin
change plus the log price return (correlation of the difference with the price return:
0.99993 in this window), so it is never substituted for coin OI.

**Not observable.** Prism holds no liquidation, trade-aggressor or account-level data.
"Short covering", "liquidation", "capitulation" and "crowded longs" cannot be observed.
This study uses *OI contraction*, *positioning reduction* and *crowding (as defined
below)* only. The study statement and a test enforce that no output is labelled
liquidation.

## Data coverage

Audit of a scratch copy of the local database (2026-10-05), `market oiprice coverage`:

| venue | coins | OI rows in [2026-09-03 22:00, 2026-10-01) | expected | gaps | collected prospectively | 1h price bars | funding cadence |
|---|---|---:|---:|---:|---:|---|---|
| Binance | BTC, ETH, SOL, HYPE, LINK, AAVE | 650 each | 650 | 0 | **0%** (all rows ingested 2026-10-03) | complete 2026-07-01 → 2026-10-01 (public backfill into scratch) | 8 h; **HYPE 4 h** |
| Hyperliquid | same | **0** | – | – | – | – | 1 h |

- Binance OI exists from 2026-09-03 22:00 UTC (the ~30-day API window at first
  collection, 2026-10-03). The local database holds 737 rows per coin to 2026-10-04 14:00.
- Hyperliquid OI: 13 snapshots per coin, 2026-10-03 10:20 → 2026-10-04 16:06, all after the
  window.
- Binance HYPE funding is not in the live funding configuration. It was fetched into the
  **scratch copy only** with the existing provider and upsert code (2,958 settlements from
  2025-05-30), recorded as its own ingestion run. The live configuration is unchanged.
- **Window.** Events are 2026-09-08 00:00 → 2026-10-01 00:00 (552 hours, 92 non-overlapping
  6-hour blocks). The start is set by warmup: the central OI scale needs W + 2L + 1 = 85
  OI hours (to 2026-09-07 10:00). Nothing after 2026-10-01 was read: that data stays
  untouched, as in Phase 18 and the Phase 9 validation window.

Retained datasets (content SHA-256, Lab ledger):

| coin | Binance (1h bars + funding + OI 1h) | Hyperliquid (snapshots; all empty) |
|---|---|---|
| BTC | `dataset_85d5f65c99ccca3a21424467e612abd1ca04f7ce9ba657124e31f64b363248cc` | `dataset_1b2ae69da52d49426fc288787531e2b3f30868e1decc08c29f467af432f7dafb` |
| ETH | `dataset_1c8ab460845c0e4f17f19ffcd5d8f719159732b7b2d4a4b3088d49942129b291` | `dataset_28690f60ef4423e458bf75344fc0bec782139d569af2c4c96761ab04b70ccfb2` |
| SOL | `dataset_7567e512b77297a4842aac7638baa94c0ce9190d771a9a14d5605eaea16ee9ee` | `dataset_2a78cbfa22846eaa528a147962e1e50048751f10635139946759eb89afae33b3` |
| HYPE | `dataset_84389caf3851e9ea8fb8fe2f0b5e711a504ed36089ef672251b0120c653b9cff` | `dataset_c226ad30b00b32770338b52beca7266bfad83011e805ecca1298f12bb6283ef8` |
| LINK | `dataset_a9c8d6496654522f30a4f3542a63443f7afbcd3f75143d0065bcefb0d4015c6d` | `dataset_ad1115a9addccdc5c7ff913444a3c2d7338ad29b99cd92f3e0f0747c76d6225b` |
| AAVE | `dataset_1317c81b9a39ea8576c86eb9127de7934c2ce1846a1533a4ec963a51b3004965` | `dataset_49ce070e20943488e31bfad79f1030855002236c12e555bd8f62aaccdd7f084e` |

Inputs per Binance coin: 2,207 hourly bars, 650 OI rows, 276 funding settlements (HYPE 552).
Two Lab dataset kinds were added for this: `perp_oi_history` (selection filters `period`)
and `perp_snapshots`.

## Availability assumptions (`oi_availability_v1`)

| input | when it is taken to be known |
|---|---|
| hourly price bar `t` | close + 60 s (assumed; backfilled bars) |
| Binance OI matched to bar `t` | the row with `observed_at == close_time[t]`, known at `observed_at + 1800 s` (**assumed**). A missing row leaves bar `t` without OI; the previous hour is never reused |
| funding | each settlement at its `time` (Prism's `available_at`); features read settlements with `time ≤ close_time[t]` |
| Hyperliquid snapshot | its capture time (observed); used only when ≤ 2 h old, with changes over **actual elapsed time** (L h ± 2 h) |
| **signal** of bar `t` | `max(price ready, OI ready)` = close + 30 min |
| **entry** | the open of the first bar opening at/after the signal: bar `t + 2`, one full hour after the close. Any OI latency in (0, 1 h] gives the same entry, so the 30-minute assumption cannot change a result unless the true lag exceeds an hour |

A non-executable reference entry at the signal bar's close is reported beside every event
member. For the PROMISING cells it is within 15 bps of the executable entry: +70 vs +85,
−54 vs −65, −42 vs −44.

## Definitions (`oi_price_primitives_v1`)

Causal trailing windows only, at bar `t`. `L` is the lookback (bars), `W` the normalisation
window. Nothing is forward-filled.

| feature | definition |
|---|---|
| raw OI | `sumOpenInterest` (coins) at the bar close; USD value `sumOpenInterestValue` |
| OI change `oi_chg` | `log(OI[t] / OI[t−L])` (coin); `usd_chg` the same on USD |
| OI delta | `OI[t] − OI[t−L]` (coins; USD separately) |
| OI relative change | delta / mean OI over the W bars ending at `t − L` |
| scaled change `oi_s` | `oi_chg / rms(prior)`, prior = the W L-bar changes ending at `t − L` (not overlapping the current change). Sign kept, not demeaned: "OI up" means OI went up |
| OI z-score `oi_z` | `(oi_chg − mean(prior)) / sd(prior)`: a shock relative to the recent trend |
| OI percentile | share of prior values below the current one (ties ½) |
| OI acceleration | `oi_chg[t] − oi_chg[t−L]` |
| OI trend | OLS slope of log OI over the last L + 1 observations |
| OI-to-volume | coin delta / coin volume over L bars (descriptive only) |
| price | `ret = c[t]/c[t−L] − 1`; `pz = ret / (sd₁ √L)`, sd₁ over the W one-bar returns ending at `t − L` |
| funding | `fund_24h` = sum of settlements in `(close − 24 h, close]` (cadence-free: 3 × 8 h or 6 × 4 h are both one day of carry); `fund_pct` = rank of `fund_24h` among its 720 prior hourly values (each coin against its own history, so no cadence is inferred); also latest rate and 24 h change |
| own volatility | std of 24 one-bar returns, tercile by rank within the 720 values ending at `t` |
| BTC regime (secondary) | Phase 17's rule on BTC: trend (EMA 50), volatility tercile ("quiet" / "volatile") |

**States.** Thresholds are on `pz` and `oi_s`:

- **P+ / P−:** `pz ≥ 0.5` / `pz ≤ −0.5`. **Flat:** `|pz| < 0.5`.
- **O+ / O−:** `oi_s ≥ 0.5` / `oi_s ≤ −0.5`. **Large (OL±):** `|oi_s| ≥ 2`. **Ordinary:** `0.5 ≤ |oi_s| < 2`.
- **Shock (SZ±):** `|oi_z| ≥ 2`.
- **FH / FL:** `fund_pct ≥ 0.9` / `≤ 0.1`. **Crowding OI (CE±):** `|oi_s| ≥ 1`.
- **Divergence:** P+O− or P−O+. **Little / much progress:** `|pz| < 1` / `≥ 1`.

An event is the **entry** into a state (edge-triggered), declustered per coin and
orientation with gap = horizon.

## Hypotheses

Each member is **one two-sided test**, and its sign is read afterwards. No member encodes
a folklore direction:

- price-oriented members (d = sign of the price move): positive = **continuation**,
  negative = **reversal**;
- long-oriented members: **up / down**;
- volatility: **larger / smaller** moves.

| family (m) | members | baseline |
|---|---|---|
| **quadrants** (6) | P+O+, P+O−, P−O+, P−O−; price-only controls P+, P− | every bar of the coin |
| **oi_increment** (4) | the same four quadrants | **price-only control**: same price direction and \|pz\| bucket |
| **magnitude** (6) | P±×OL± (4); large-vs-ordinary expansion and contraction (contrasts) | price-only control |
| **oi_only** (4) | coin OI large up/down; **USD** OI large up/down | every bar |
| **shocks** (6) | SZ+, SZ−; SZ± with a price move (price-matched); SZ+ × FH, SZ+ × FL | as stated |
| **funding_oi** (9) | crowd long (FH+CE+), crowd short (FL+CE+), each with the same-direction price move; unwind (FH/FL + CE−); controls FH, FL, CE+ alone | every bar |
| **crowding_increment** (4) | crowd long/short, unwind high/low | **funding-only control**: same funding bucket |
| **divergence** (6) | flat + OI surge, flat + OI drop, little / much progress with OL+, their contrast, funding tail + divergence | as stated |
| **volatility** (6) | OL+, OL−, flat + OL+, SZ+, SZ−, funding tail + CE+ → \|log return\| | coin, own-vol tercile, \|pz\| bucket |
| **lead_lag** (4) | cross-sectional Spearman of coin OI, USD OI, price (control), OI residualised on price, vs next return | – |
| **cross_venue** (2) | price move with Binance and Hyperliquid OI agreeing / disagreeing | price-only; **coverage-gated** |

## Preregistration

Frozen by `market oiprice study register` on the Phase 17/18 governance adapter (same
`lab_structure_*` tables and lifecycle; it now dispatches `oi_price_v1`; no migration).
Manifest: `config/oiprice/phase19_oi_price.v1.yaml`. Every value was fixed from coverage
before any outcome:

| item | frozen value |
|---|---|
| timeframe | 1h only (no 15m / 4h / daily) |
| primary horizon | **6 h for every family**; 1 / 12 / 24 h descriptive only |
| central | L = 6, W = 72, price threshold 0.5, OI threshold 0.5, large 2.0, shock z 2.0, funding tail 0.9, crowding OI 1.0, progress split 1.0, funding window 720 h |
| sensitivity (one at a time, 16 variants) | L 3/12, W 48/120, price 0.25/1.0, OI 0.25/1.0, large 1.5/2.5, shock 1.5/2.5, tail 0.85/0.95, crowding 0.5/1.5 |
| target | direction: the coin vs USD, net of 2 × (fee + slippage); funding reported. Volatility: \|log return\| |
| costs | `perp_costs`, Binance: fee 5.0 bps; slippage BTC/ETH 2, SOL 4, HYPE/LINK 6, AAVE 8 bps per side |
| inference | `oi_inference_v1` (below) |
| gates | ≥ 30 independent events, ≥ 3 assets, ≥ 80% evaluable, ≥ 20 time blocks; IC ≥ 60 timestamps; cross-venue ≥ 336 aligned Hyperliquid hours on ≥ 3 coins |
| floors | 10 bps (direction, volatility), 0.03 (IC) |
| verdicts | `phase19_verdicts_v1` = Phase 18's ladder; volatility members skip the position (net > 0) gate |

The IC gate (60) differs from Phase 18's 100 because the window holds exactly 92
non-overlapping cross-sections. It was fixed from that count, before any outcome. No gate
was relaxed afterwards.

## Inference and multiple testing

The primary test is Phase 18's **block-clustered t-test across assets**:

- events are grouped by entry bar into 6-hour calendar blocks shared by all six coins;
- the variance adds adjacent-block covariance;
- the reference is Student t with G − 1 df, two-sided.

Six alts entering on one market shock are therefore one block, not six events. An
event-level iid t-test is reported beside it (`p iid`) for comparison only. BH (q = 0.10,
the Lab's implementation) runs within each family. Untestable members are excluded from m.

| family | m tested / 11 preregistered | min q | survivors (q ≤ 0.10) |
|---|---|---:|---|
| quadrants | 6 / 6 | 0.658 | – |
| oi_increment | 4 / 4 | 0.360 | – |
| magnitude | 2 / 6 | 0.584 | – |
| oi_only | 4 / 4 | **0.032** | `usd_large_down` |
| shocks | 4 / 6 | 0.169 | – |
| funding_oi | 2 / 9 | 0.600 | – |
| crowding_increment | 0 / 4 | – | – |
| divergence | 2 / 6 | **0.036**, **0.056** | `progress_high`, `div_fund_extreme` |
| volatility | 4 / 6 | 0.965 | – |
| lead_lag | 4 / 4 | 0.480 | – |
| cross_venue | 0 / 2 | – | – |

That is 32 tested members in 9 families, 3 discoveries. Under the synthetic null the
harness made 8 discoveries in 181 family-runs (≈ 0.04 per family), which is ≈ 0.4 expected
here. Three is above that background. The discoveries are not independent of each other or
of the WEAK cells (see [Verdicts](#verdicts)).

## Null calibration (before the real run)

The full pipeline (central parameters) was run on a **synthetic market** with:

- a BTC factor with slowly varying volatility and Student-t(4) shocks;
- alts = β × BTC + a shared alt factor + idiosyncratic noise;
- per-coin OI as a persistent log random walk whose innovations are *contemporaneously*
  correlated with the coin's own return (0.3) and scale with volatility. So OI "explains"
  price after the fact and is louder when volatile, but never predicts the next return;
- funding as independent slow regimes (8 h; HYPE 4 h);
- **no planted relationship**.

`market oiprice study calibrate`, 20 seeds, 612 testable members:

| | block-clustered (primary) | event-level iid |
|---|---:|---:|
| share p < 0.05 | **2.0%** | 5.8% |
| share p < 0.10 | **4.6%** | 11.0% |
| BH discoveries (20 seeds × ~9 families) | 8 (≤ 3 per seed) | – |

- The p deciles are 28 / 46 / 52 / 57 / 62 / 65 / 71 / 79 / 66 / 86: conservative, as in
  Phase 18.
- The volatility family has 0 of 87 below 0.05: matching on own-vol tercile and \|pz\|
  bucket absorbs the built-in "OI is loud in volatile hours" confounding.
- **Power.** A planted effect makes the next hour's return load on the mean of the previous
  six OI innovations. At 0.3 (≈ 1% drift per 6 h per σ of OI), 28% of tests have p < 0.05,
  BH makes 38 discoveries in 5 seeds, and `ic_oi` is detected in 5 of 5 seeds. At 0.15,
  detection is weak (9% of tests).
- **The harness is conservative, not blind, but four weeks can only detect large effects.**
  Effects well below ≈ 0.5–1% per 6 h per σ of OI would usually be missed.

## Results (central parameters, primary horizon 6 h)

*stat* is the oriented excess over the member's baseline, in bps (positive =
continuation / up / larger). *net* is the oriented position after fees and slippage.
*p* is two-sided clustered.

### Price/OI quadrants and price-only controls

| quadrant | n | vs every bar | vs price-only control | net | q (both) | verdicts |
|---|---:|---:|---:|---:|---|---|
| price up, OI up | 98 | +16.0 [−31, +63] | +8.2 [−37, +54] | +12.2 | 0.71 / 0.72 | NO EVIDENCE |
| price up, OI down | 82 | −32.6 [−85, +20] | −35.2 [−76, +6] | −39.3 | 0.66 / 0.36 | continuation REJECTED, reversal WEAK (vs price) |
| price down, OI up | 75 | −11.7 [−71, +48] | −27.5 [−83, +28] | −43.5 | 0.71 / 0.42 | NO EVIDENCE |
| price down, OI down | 106 | +34.5 [−9, +78] | +21.8 [−19, +62] | +4.5 | 0.66 / 0.42 | reversal ("capitulation rebound") **REJECTED** |
| price up (control) | 202 | −9.9 | – | −16.4 | 0.71 | NO EVIDENCE |
| price down (control) | 184 | +8.8 | – | −23.0 | 0.71 | NO EVIDENCE |

64.8% of bars sit in a dead zone on price or OI. Occupancy: up/up 10.8%, up/down 7.5%,
down/up 7.1%, down/down 9.8%. The four quadrants are not significantly different from each
other or from price alone. The folklore labels do not survive as stated:

- "Price up + OI up = bullish": +8 bps beyond price.
- "Up + OI down = short covering, bearish": a reversal lean of −35, not significant after
  correction.
- "Down + OI up = bearish": −28, not significant.
- "Down + OI down = capitulation / reversal": a rebound is excluded at > 10 bps.

### OI magnitude

Large vs ordinary expansion (price-matched): +32.9 bps (NO EVIDENCE). Contraction: +16.9 (NO
EVIDENCE). The four price × large-OI cells have 14–26 events each (INSUFFICIENT). Large OI
change was not measurably different from ordinary OI change, given the price move.

### OI-only

| member | n | stat | vs price-only | net (+) | q | verdict |
|---|---:|---:|---:|---:|---:|---|
| coin OI large up | 42 | +0.2 | – | −1.2 | 1.00 | NO EVIDENCE |
| coin OI large down | 38 | −51.7 [−104, +1] | −43.2 | −59.8 | 0.109 | **down: WEAK**; up REJECTED |
| USD OI large up | 46 | +16.5 | – | +16.1 | 0.81 | NO EVIDENCE |
| USD OI large down | 42 | **−65.4 [−112, −19]** | −44.8 | −70.6 | **0.032** | **down: PROMISING**; up REJECTED |

A short on large USD-OI contraction nets +32.9 bps after fees and slippage (funding
≈ 0). The USD measure's extra strength over the coin measure is the price move it contains.
The coin measure, which isolates positioning, is WEAK.

### OI shocks

`shock_pos` −2.9 bps (n = 48, NO EVIDENCE). `shock_neg` −55.8 [−110, −2] (n = 45, q = 0.17):
**down WEAK**, up REJECTED. Conditioned on the price move: +22.7 / +7.9 bps vs price-matched
(NO EVIDENCE). Shocks × funding tail: 3 and 10 events (INSUFFICIENT).

### Funding interactions and crowding

**Crowding** is defined objectively as funding percentile in its tail (≥ 0.9 or ≤ 0.1
against the coin's own prior 30 days) plus OI expansion `oi_s ≥ 1`, optionally with a
same-direction price move.

| member | independent events | stat | verdict |
|---|---:|---:|---|
| crowd long / with price up | 5 / 2 | – | INSUFFICIENT |
| crowd short / with price down | 23 / 16 | −9.0 / −10.5 | INSUFFICIENT |
| unwind high / low | 5 / 19 | −34.5 / −25.9 | INSUFFICIENT |
| funding high (control) | 21 | −34.8 | INSUFFICIENT |
| funding low (control) | 31 | −19.2 | NO EVIDENCE |
| OI expansion alone (control) | 121 | +21.0 | NO EVIDENCE |
| crowding vs funding-only control (4) | 5–23 | −20 … +20 | INSUFFICIENT |

September 2026 funding was positive in 81–99% of hours per coin, and below its own prior
month most of the time (FL on 478 bars, FH on 75). The window simply contains too few
crowding events. **Whether combining funding and OI adds anything cannot be answered from
it**, so this is INSUFFICIENT, not a negative.

### Divergence and price progress

| member | n | stat | vs price-only | net (+ / −) | q | verdict |
|---|---:|---:|---:|---:|---:|---|
| flat price + OI surge | 8 | – | – | – | – | INSUFFICIENT |
| flat price + OI drop | 22 | – | – | – | – | INSUFFICIENT |
| OI surge, little progress | 18 | −1.6 | – | – | – | INSUFFICIENT |
| OI surge, much progress | 35 | **+85.0 [+16, +154]** | +49.6 | +75.1 / −111.1 | **0.036** | **continuation: PROMISING**; reversal REJECTED |
| little vs much progress (contrast) | 18 | −86.7 | – | – | – | INSUFFICIENT |
| funding tail + price/OI divergence | 41 | **−44.2 [−90, +1]** | (declared) | −51.4 / +14.5 | **0.056** | **reversal: PROMISING**; continuation REJECTED |

"Large OI expansion with little price progress = crowded positioning" cannot be tested
here: only 8–18 events. What passes is the opposite case. When a ≥ 1σ price move
comes with a ≥ 2σ OI expansion, the move continued for 6–12 h (+85, +101 bps), then gave
some back by 24 h (−23).

### Volatility prediction

| member | n | larger-move excess (bps) | q | verdict |
|---|---:|---:|---:|---|
| OI large up / down | 42 / 38 | +1.1 / −10.8 | 0.97 | NO EVIDENCE |
| OI shock + / − | 48 / 45 | +4.4 / −2.0 | 0.97 | NO EVIDENCE |
| flat + OI surge; funding tail + OI expansion | 8 / 28 | – | – | INSUFFICIENT |

There is no sign that OI predicts movement magnitude beyond the coin's own recent
volatility and price move. Pooled quintiles of OI change vs next absolute return
(112 / 139 / 98 / 117 / 142 bps) have no gradient. OI-to-volume is the same
(119 / 128 / 107 / 118 / 135).

### Lead / lag

| | IC (mean Spearman, 91 timestamps) | 95% CI | q |
|---|---:|---:|---:|
| coin OI change → next return | −0.039 | [−0.134, +0.055] | 0.48 |
| USD OI change → next return | +0.041 | [−0.049, +0.130] | 0.48 |
| price (control) → next return | +0.047 | [−0.058, +0.153] | 0.48 |
| OI beyond price → next return | −0.033 | [−0.125, +0.059] | 0.48 |

Time-series per coin (descriptive): contemporaneous OI vs price +0.149 (−0.15 … +0.55
across coins), OI now vs next return +0.084, price now vs next OI change +0.024. OI and
price move together far more than either leads the other.

## Asset breadth

| cell | per-asset (n, bps) | + / − | largest share | leave-one-out |
|---|---|---|---:|---|
| USD OI large down (down) | AAVE −98, BTC −7, ETH −37, HYPE −100, LINK −175, SOL −15 | 6 / 0 | 32% (LINK) | all negative (−51 … −81) |
| coin OI large down (down) | AAVE +6, BTC −3, ETH −6, HYPE −123, LINK −241, SOL −26 | 5 / 1 | 59% (LINK) | all negative (−23 … −68) |
| OI surge + much progress (cont.) | AAVE +465 (3), BTC +49, ETH −6, HYPE +74, LINK +23, SOL +100 | 5 / 1 | 46% (AAVE) | all positive (+49 … +104) |
| funding tail + divergence (rev.) | AAVE −25, BTC +12, ETH −23, HYPE +120, LINK +111, SOL +19 | 4 / 2 | 56% (HYPE) | all reversal (+17 … +61) |
| negative shock (down) | AAVE +33, BTC −19, ETH −2, HYPE −101, LINK −205, SOL −110 | 5 / 1 | 41% | all negative |

The contraction effects are carried by the higher-beta alts (LINK, HYPE). BTC and ETH are
near zero. The coin-measure cell is LINK-dependent: without LINK it is −23 bps.

## Stability

Chronological thirds (~8 days each; one regime, so this is not a test of regime
robustness):

| cell | early | middle | late |
|---|---:|---:|---:|
| USD OI large down | −73 (22) | −53 (6) | −58 (14) |
| coin OI large down | −80 | −62 | −20 |
| negative shock | −120 | −31 | −40 |
| OI surge + much progress | +21 | +93 | +133 |
| funding tail + divergence (reversal) | +38 | +0 | +72 |

BTC regime (secondary, descriptive):

- OI surge + progress: +122 in BTC downtrends, +36 in uptrends.
- USD OI contraction: −158 in uptrends, −36 in downtrends.

Every subgroup has 2–32 events. None was tested or selected.

### Sensitivity (one at a time, descriptive)

Plateau verdicts (Prism `plateau_verdict`) for the cells that leaned:

| cell | plateau (leaning side) | neighbour range (bps) | sign agreement |
|---|---|---|---:|
| OI surge + much progress | PLATEAU | +35 … +91 | 100% |
| USD OI large down | PLATEAU | −34 … −72 | 100% |
| funding tail + divergence | PLATEAU | −11 … −44 | 100% |
| coin OI large down | PLATEAU | −21 … −81 | 100% |
| negative shock | PLATEAU | −13 … −72 | 100% |
| price up / OI down (vs price) | PLATEAU | −14 … −53 | 100% |

The leaning cells keep their sign on every neighbour, with no isolated "97.5th percentile"
spike. The effects weaken at looser thresholds: OI large 1.5 gives −21 / −34 / +35 bps, and
shock z 1.5 gives −13. That is the expected shape if the information sits only in the
largest moves. No neighbour was tested or selected.

## Costs

Direction members report net after a round trip of fee (5 bps) and slippage (2–8 bps) per
side. Funding over the 6 h hold is reported separately (≈ 0 to 0.4 bps here) and included
in *net incl. funding*. After costs:

- OI surge + much progress continuation: **+75 bps**;
- short on USD-OI contraction: **+33**;
- short on coin-OI contraction: **+22**;
- funding-tail divergence reversal: **+15**.

Hit rates (gross > 0): 86% / 62% / 58% / 61%. The quadrant cells are negative or ≈ 0 after
costs. Volatility members carry no cost (they are not trades).

## Cross-venue

The cross-venue family is INSUFFICIENT by its frozen gate: 0 of 6 coins have any aligned
Hyperliquid hour (need ≥ 336 on ≥ 3). Hyperliquid's first OI snapshot is 2026-10-03, after
the window. Nothing was forced: no Binance value was copied into Hyperliquid or vice versa,
and Hyperliquid was not given an equal statistical role. Synthetic tests confirm the gate
passes with dense hourly snapshots and fails with sparse ones.

**Future requirement (not implemented; live collection untouched).** The always-on runtime
captures Hyperliquid OI as a side effect of the `prospective` job (6 times a day) and the
`oi` job (4 times a day): about 11 irregular captures per day, with gaps of up to ~3.7 h.
That should let a 6-hour change meet the 2-hour staleness limit at most hours, but not all,
and it never gives hourly-resolution OI. Whether the 336-hour coverage gate is met has to
be **measured** on the accumulated history, not assumed. A fixed hourly Hyperliquid OI
capture would remove the doubt. This is reported as a requirement, not changed mid-phase.

## Multiple testing

Raw p and q for every member are in the stored result (`market oiprice study report <run>
--json`) and in the tables above. Survivors at q ≤ 0.10:

| family | member | direction | raw p (two-sided) | q |
|---|---|---|---:|---:|
| oi_only | `usd_large_down` | down | 0.008 | 0.032 |
| divergence | `progress_high` | continuation | 0.018 | 0.036 |
| divergence | `div_fund_extreme` | reversal | 0.056 | 0.056 |

The divergence family has m = 2 because four of its members were INSUFFICIENT, so BH
barely adjusts there. That follows the frozen rule (untestable members leave m), but it
matters: had all 6 members been testable with no other small p-value, `progress_high`
would have q = 0.018 × 6 = 0.108 and **would not pass**. `div_fund_extreme` would not
either. Both divergence survivors therefore owe their status partly to the rarity of their
siblings.

## Limitations

- **23 days, one market.** Every result is from 2026-09-08 → 2026-10-01: 552 hours, 92
  six-hour blocks, in a **rising**, positively funded September (BTC +6%, ETH +8%, HYPE +7%,
  LINK +13%, SOL +14%, AAVE +22% over the window). Excess returns remove each coin's own
  drift, but the thirds are about 8 days each, and nothing here can speak to regime
  robustness.
- **Assumed OI availability.** Every Binance OI row was backfilled. The 30-minute latency is
  an assumption, informed by one live observation. Any true lag ≤ 1 h gives identical
  entries.
- **Power.** The calibration shows that effects below ≈ 0.5–1% per 6 h per σ of OI are
  usually missed. NO EVIDENCE here is weak evidence of absence. INSUFFICIENT (50 of 114
  verdicts) means the window lacks the events, not that the hypothesis failed.
- **No second venue.** ROBUST was unreachable by construction, because it needs
  cross-venue agreement.
- **USD vs coin.** The strongest cell uses the USD measure, which contains the price move.
  The coin measure of the same idea is WEAK.
- **Scratch data.** Bars and HYPE funding were backfilled into a scratch copy on
  2026-10-05. OI came from the local database copy (identical rows; the production
  database on Railway was not read).
- **Not observable.** There are no liquidation, order-book or account data. Contraction is
  not liquidation, and crowding is a definition, not an observation.
- **Dependence.** Members share events, so BH within a family is approximate, as in
  Phases 5, 17 and 18. The blocked test is conservative (2.0% at 0.05).

## Verdicts

- **ROBUST ENOUGH FOR NEXT RESEARCH STAGE: none.** It was unreachable here, since it
  needs a second venue.
- **PROMISING — NEEDS VALIDATION (3):**
  1. **Large USD-OI contraction → lower prices over 6 h** (−65 bps, q = 0.032; all 6 assets;
     plateau; +33 bps net as a short). The caveat is that USD OI contains the price move:
     −45 bps against the price-matched control, and the coin measure is only WEAK.
  2. **Large OI expansion with a large price move → continuation for 6–12 h** (+85 bps,
     q = 0.036; plateau; +75 bps net). The caveats are +50 bps beyond price (untested),
     AAVE 46% of contribution, late-heavy, reversal by 24 h, and survival only because four
     sibling members were too rare to test (with m = 6, q = 0.108).
  3. **Funding tail + price/OI divergence → reversal** (+44 bps, q = 0.056; +15 bps net). The
     caveats are HYPE 56%, a flat middle third, and that "funding tail" meant low relative to
     the prior month in a positive-funding month. It too depends on the family's small m.
- **WEAK / EXPLORATORY (3):** coin-OI large contraction → down (−52, q = 0.11, LINK-heavy);
  negative OI shock → down (−56, q = 0.17); price up + OI down → reversal against the
  price-only control (−35, q = 0.36).
- **REJECTED (7):**
  - the rebound after OI contraction (coin, shock) and after price down + OI down;
  - continuation of price up + OI down beyond price;
  - the opposite sides of the three PROMISING cells.
- **NO EVIDENCE (51):**
  - the four quadrants (with and without the price control);
  - OI expansion alone; positive shocks;
  - large-vs-ordinary OI change;
  - every volatility member; every lead/lag IC;
  - funding-low and OI-expansion controls.
- **INSUFFICIENT (50):** every crowding, unwind and funding × shock member, the
  flat-price / little-progress cells, and all cross-venue members.

Taken together, OI in this window behaves mostly like an **echo of price**. It is
contemporaneously correlated with price, and adds nothing significant once the price move
is matched. The one directional regularity, contraction followed by further weakness, is
coherent across four measures but has not been separated from price momentum in the
high-beta alts. Nothing is promoted to the Phase 6 catalogue, prospective tracking, the
co-pilot or paper trading.

## Reproducibility

| run | database | software | digest |
|---|---|---|---|
| `srun_08a749000c1d4d93b8267815e7283dbd` (governed, attempt 1) | scratch copy | `37fb343` (clean) | `21abe032ec4c9f6fad91e0c69be4875434dcdf57ff381b731a1936011b91a78c` |
| `srun_10ed51f5c89e49ffad4a8ea8e25ac501` (explicit `rerun_of` attempt 1) | same | `37fb343` | `21abe032…` (**identical**) |
| `srun_fd98e9dfb1864230ba0ea9cca9db3e3f` (fresh scratch copy, re-registered: same study ID) | fresh copy | `37fb343` | `21abe032…` (**identical**) |

The payload has no clock fields and no random draws. Timings and memory go to run
metadata, outside the digest.

## Performance

| step | wall | peak RSS |
|---|---:|---:|
| registration (12 datasets captured and hashed) | 1.6 s | 0.26 GB |
| governed run (central + 16 variants) | 9.6 s (analysis 8.2 s) | 0.30 GB |
| null calibration, 20 seeds | 81 s (≈ 4.3 s per seed) | 0.20 GB |

Rows: 2,207 grid bars, 552 window bars, 3,312 eligible coin-bars, 13,248 outcome rows
(6 coins × 552 × 4 horizons).

## Live compatibility

- `market oi` collection, `perps/open_interest.py`, `config/perps.yaml` and the runtime
  schedule are unchanged (tested). Binance HYPE funding was fetched into the scratch copy
  only.
- No forward-tracking, co-pilot, paper, risk-policy, execution or strategy-catalogue code
  changed. None of it imports `research.oiprice` (test `test_no_live_consumer_reads_phase19`).
  `research.oiprice` imports nothing from them and contains no SQL writes.
- **No migration.** Phase 19 studies use the Phase 17 tables via a `study_version`
  dispatch. Two Lab dataset kinds were added (`perp_oi_history`, `perp_snapshots`); the
  existing kinds are untouched.
- The study ran on scratch copies (`PRISM_RUNTIME_ROLE=scratch`). `data/prism.duckdb` and
  the Railway database were not written.

## Verification

| check | result |
|---|---|
| `pytest tests/test_oi_price.py` | **30 passed**. Covers OI-to-bar alignment, availability, hand-calculated changes / scale / z / percentile / trend, USD vs coin separation, causal z, quadrants, flat + expansion, shock timing, causal and cadence-free funding, volatility window, price-matched control, co-timed blocks, BH membership, verdicts, direction labels, venue separation, Hyperliquid elapsed-time alignment, sparse-Hyperliquid insufficiency, determinism, future immunity, null FPR, planted power, manifest, OI dataset kinds, governed lifecycle on retained OI, coverage audit, consumer isolation and untouched live OI |
| `pytest` (full) | **795 passed**, 0 failed (before Phase 19: 765) |
| `ruff check src tests dashboard` / `ruff format --check src tests` / `git diff --check` | passed |
| null calibration (20 seeds, 612 tests) | 2.0% / 4.6% at p < 0.05 / 0.10; 8 BH discoveries |
| power (planted 0.3, 5 seeds) | 28% p < 0.05, 38 BH discoveries, `ic_oi` detected in 5 of 5 |
| governed run | `srun_08a74900…` COMPLETED |
| reproducibility | digest `21abe032…` reproduced on a fresh scratch copy and by an explicit rerun |

## Next recommended phase

**Phase 20: a confirmatory replication of the Phase 19 OI cells on untouched prospective
data.** Freeze now, and change nothing:

- exactly the three PROMISING cells and the contraction-then-weakness theme (coin and USD
  measures, against the price-only control);
- the same definitions, horizons and gates;
- one-sided directions.

Evaluate it only once ≥ 12 weeks of post-2026-10-01 Binance 1h OI have accumulated in the
authoritative database (no earlier than late December 2026). The runtime's `oi` job keeps
that history beyond Binance's 30-day window. No new hypotheses, thresholds or families.
The same freeze can name Hyperliquid as the cross-venue confirmation, with the
unchanged coverage gate deciding whether it is testable (see the cadence requirement under
[Cross-venue](#cross-venue)).

This phase does not begin it.
