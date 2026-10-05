# Phase 18: relative strength, BTC beta/correlation and cross-asset dislocation

> **EXPLORATORY.** Historical intraday availability is reconstructed under an explicit
> latency assumption rather than observed in real time. The data is backfilled history,
> not untouched validation, and the universe is five alts. Nothing in this document is
> validated, and no result feeds the forward tracker, the co-pilot or the paper account.

## Summary

Study `sstudy_1fd9f7272ac68f8c2569f3ce825a986fe426e030b263bd19074f9f606bed8361`
(`phase18_relative_strength_v1`) was frozen at 2026-10-05 13:42 UTC and first evaluated at
13:42–13:43 UTC (`srun_265b35bd…`). A reproducibility rerun on a fresh scratch copy
(`srun_8cf749fc…`) reproduced the result digest `e22f8a0a…` exactly. Before registration
the harness ran only on a correlated synthetic market (null calibration and a power check).

It tested 30 preregistered two-sided members per venue and timeframe in six BH families.
That is 120 tests in all (4h primary and 1h secondary, Hyperliquid and Binance, never
pooled), with continuation and reversal read from each test's sign.

**No relative-strength, residual, dislocation, consolidation or pullback hypothesis
survives correction on the primary 4h timeframe of either venue. Nothing is ROBUST.** The
only member that passes the frozen policy is a 1h Hyperliquid rank-probability statistic
(PROMISING). It is not tradeable after costs, not reproduced on Binance, and its 4h
counterpart depends on HYPE (see [Verdict](#verdict)).

| question | answer |
|---|---|
| 1. Does BTC-relative strength persist? | No evidence. Continuation excess after a z ≥ 2 BTC-relative move: +13 bps (HL 4h, 95% CI [−71, +97]), +32 (BN 4h, [−29, +94]), +28 (HL 1h), +4 (BN 1h). Every q ≥ 0.21. The sign is stable across parameter neighbours (PLATEAU on both 4h venues), but it is a plateau of a non-significant effect. |
| 2. Does BTC-relative weakness persist? | No. Weak alts did not keep underperforming. Continuation is −31 / −44 bps on 4h (that is, a mild relative *rebound*) and +0 / +31 bps on 1h. Every q ≥ 0.21. The 1h HL rebound reading is WEAK only. |
| 3. Raw vs beta-adjusted residual momentum? | Indistinguishable. Residual continuation has the largest 4h point estimates (+58 HL, +64 BN; BN one-sided p = 0.022, q = 0.22: WEAK). Unadjusted BTC-relative and raw momentum sit at −19 to +43. No member of either family passes BH, and the cross-sectional rank ICs are ≈ 0 for both (−0.019 … +0.031). |
| 4. Do extreme residual moves continue or revert? | Neither, after correction. Positive residual shocks lean towards continuation on 4h (both venues positive, CIs straddle zero). Negative shocks are mixed (BN +18, HL −58 bps continuation). On 1h everything is within ±10 bps. |
| 5. Does correlation breakdown predict anything? | No usable effect. 4h: +10 bps (BN, n = 76), −58 bps (HL, n = 44). 1h Binance: +38 bps excess, q = 0.074, but net −3 bps after both legs: WEAK. 1h HL: INSUFFICIENT (26 events). Only 7–10% of residual shocks are ever "confirmed" by a breakdown. Measured from the shock (not executable), confirmed chains show +103 (BN) / +268 (HL) bps. Entered when the breakdown is observable, they show +5 / +157 bps (n = 24 / 11). The Phase 17 pattern again: confirmation selects moves that already happened. |
| 6. Is relative strength more useful during BTC consolidation? | No. In-vs-out contrasts for strong alts: −75 (BN 4h), −2 (HL 4h), −32 / −20 bps (1h). Consolidation does not add information, and if anything it subtracts. Weak alts in consolidation: 1h Binance rebound (contrast −53 bps, q = 0.14, WEAK reversal) is the OPPOSITE of 1h HL (+49). |
| 7. Do strong assets stay strong in market pullbacks? | No. Leaders entering the top rank during a pullback: −11 (HL 4h), +20 (BN 4h), +1 / +10 bps (1h); pullback-vs-not contrasts all q ≥ 0.6. Descriptively, on HL 4h, *laggards* outperformed BTC more than leaders in pullbacks (+31 vs +16 bps over the next day). |
| 8. Do cross-sectional ranks persist? | Barely, and not consistently. Rank IC is ≈ 0 on every venue/timeframe (−0.016 … +0.031, every q ≥ 0.15). The leader finishes in the top half 5.3 pp more often than chance on HL 1h (q = 0.022, PROMISING) and 4.0 pp on HL 4h (q = 0.19, WEAK, flips sign without HYPE). Binance excludes ±3 pp (REJECTED). |
| 9. Does sophistication add anything beyond simple raw momentum? | No. Raw, BTC-relative, market-relative and residual measures are all non-significant. Continuous gradients are flat: pooled Spearman of signal vs forward target ranges from −0.05 to +0.003, with no monotonic quintile pattern on any venue. "Beta-adjusted" does not beat "unadjusted", and neither beats zero. |
| 10. Tradeable as USD-directional positions? | No. The absolute family (the alt alone vs USD) is NO EVIDENCE on all 16 venue/timeframe/member cells. Even where a relative spread leaned positive, the USD excess of the same events was ≈ 0 (e.g. BN 4h residual strength: +64 bps residual, +2 bps USD). |
| 11. Broad across assets? | Not where anything leaned. BN 4h residual strength: 3 of 4 alts positive, SOL is 52% of the absolute contribution, and AAVE is negative. HL 4h leader persistence: HYPE carries it, and without HYPE it is −2.3 pp. The 1h HL leader persistence is the broadest (4 of 5 leaders positive, ETH negative, every leave-one-out positive). |
| 12. Do Hyperliquid and Binance agree? | On "nothing", mostly. 4h pairs: 46 SIMILAR, 8 MIXED, 2 OPPOSITE, 4 INSUFFICIENT. 1h pairs: 26 SIMILAR, 30 MIXED, 2 OPPOSITE, 2 INSUFFICIENT. OPPOSITE: tight-correlation residual weakness (4h) and consolidation × weakness (1h). Neither venue's WEAK/PROMISING cell is reproduced on the other. |

Verdict counts (two directions per member):

| | ROBUST | PROMISING | WEAK | REJECTED | NO EVIDENCE | INSUFFICIENT |
|---|---:|---:|---:|---:|---:|---:|
| primary 4h, Hyperliquid (60) | 0 | 0 | 1 | 1 | 54 | 4 |
| primary 4h, Binance (60) | 0 | 0 | 2 | 5 | 53 | 0 |
| secondary 1h, Hyperliquid (60) | 0 | 1 | 4 | 10 | 43 | 2 |
| secondary 1h, Binance (60) | 0 | 0 | 3 | 13 | 44 | 0 |

## Core distinction

Four quantities are never conflated, and every member names exactly one target:

| quantity | definition | used as |
|---|---|---|
| BTC's return | `R_BTC` | reference, regime anchor, never ranked against itself |
| alt (USD) return | `R_a` | the **absolute** family's target (`usd`) |
| BTC-relative return | `R_a − R_BTC` | **relative momentum**, **interactions** and **cross-section** target (`rel`) |
| beta-adjusted residual | `R_a − β · R_BTC` | **residual** and **dislocation** target (`res`) |

A family has one declared target (tested). An alt can beat BTC while both fall: that is
reported as relative edge, never as a long-alt trade (see
[Absolute vs relative](#absolute-vs-relative)).

## Hypotheses

Each member is one two-sided test of the mean net excess (or IC / probability) at the
primary horizon. Its sign decides between **continuation** (positive) and **reversal**
(negative). Neither is assumed, and both get verdicts.

| family (target) | member | signal (edge-triggered unless stated) |
|---|---|---|
| **relative_momentum** (`rel`) | `rel_strong` / `rel_weak` | H1 / H2: BTC-relative z ≥ +2 / ≤ −2 |
| | `raw_strong` / `raw_weak` | control: raw (unadjusted) return z, ordinary momentum |
| | `mkt_strong` / `mkt_weak` | control: market-relative z (vs equal-weight alt basket) |
| **residual** (`res`) | `res_strong` / `res_weak` | H3 / H4: beta-adjusted residual z ≥ +2 / ≤ −2 |
| | `rel_strong` / `rel_weak` | control: unadjusted BTC-relative z, same target |
| | `ic_res` | cross-sectional Spearman: residual rank vs forward residual rank |
| **dislocation** (`res`) | `breakdown` | H5: `corr_S(t) ≤ corr_W(t−S) − 0.3` after `corr_W(t−S) ≥ 0.7`, oriented with the residual move over those S bars |
| | `tight_res_strong` / `_weak` | H6: residual z crosses ±2 while `corr_W(t−L) ≥ 0.7` |
| **interactions** (`rel`) | `consol_rel_strong` / `_weak` | `rel_strong` / `rel_weak` during BTC consolidation, vs consolidation entries |
| | `consol_vs_not_rel_strong` / `_weak` | contrast: inside minus outside consolidation (each vs its own condition-matched baseline) |
| | `pullback_leader` / `_laggard` | alt becomes the cross-sectional #1 / last during a broad pullback |
| | `pullback_vs_not_leader` / `_laggard` | contrast: inside minus outside pullbacks |
| **cross_section** (`rel`) | `ic_rel` | Spearman: rank of `R(L)` vs rank of forward BTC-relative return |
| | `top_persists` / `bottom_persists` | the leader (laggard) finishes in the top (bottom) half, minus the null expectation ⌊n/2⌋/n |
| | `top_minus_bottom` | long the leader / short the laggard (4 legs of costs) |
| **absolute** (`usd`) | `rel_strong`/`_weak`, `res_strong`/`_weak` | the same events, traded as the alt alone vs USD |

Ranking by raw, BTC-relative or market-relative return is **identical** at a timestamp,
because the three differ by a term common to all alts (tested). So `ic_rel` covers all
three, and only the residual (`ic_res`) can reorder the cross-section.

## Data

Inputs are a **scratch copy** of the local database plus a bounded public backfill
(2026-10-05), not the Railway production database. Binance 4h history from 2020-09 was
backfilled for this phase (41,100 bars, 18 s). Every 4h/1h series has zero gaps and zero
zero-volume bars. Windows were fixed from coverage before any outcome was computed. Data
selects `[data_start, event_end)` on close time, and signals are bars closing in
`[event_start, event_end)`.

| architecture | venue | data | events | alts (eligible universe) |
|---|---|---|---|---|
| **primary 4h** | Hyperliquid | 2024-06-24 → 2026-10-01 | **2024-11-01 → 2026-10-01** | ETH, SOL, LINK, AAVE; HYPE from 2025-01-07 (n = 4 for 405 bars, 5 for 3,789) |
| | Binance USD-M | 2020-09-01 → 2024-11-01 | **2021-03-01 → 2024-11-01** | ETH, SOL, LINK, AAVE (n = 4 throughout); **HYPE excluded: not listed until 2025-05-30** |
| **secondary 1h** | Hyperliquid | 2026-03-11 → 2026-10-01 | 2026-04-15 → 2026-10-01 | all five (n = 5) |
| | Binance USD-M | 2024-09-01 → 2026-04-01 | 2024-11-01 → 2026-04-01 | four, HYPE from 2025-06-07 (n = 4: 5,249 bars, 5: 7,135) |

- Funding: Hyperliquid hourly 2024-06-24 → 2026-10-01; Binance 8-hourly 2020-09-01 →
  2026-04-01 (Binance HYPE has no retained funding, so its funding column is empty, never
  assumed zero).
- Venue windows are disjoint within each architecture: Binance ends where Hyperliquid
  begins (Phase 11's rule). Binance has been used in earlier Prism research, so it is
  cross-venue exploratory evidence, **not independent validation**.
- Nothing after 2026-10-01 was read. That data stays untouched, as does the Phase 9 window.
- Assumed latency **60 s**. Timestamps are evaluated on BTC's grid. An alt enters the
  cross-section only once it has W + L + 1 contiguous bars of its own (no backward
  reconstruction of today's universe).

Retained datasets (`perp_intraday_bars` per used timeframe + `perp_funding`, content SHA-256):

| coin | Hyperliquid (4h + 1h) | Binance (4h + 1h; HYPE 1h only) |
|---|---|---|
| BTC | `dataset_0d58f0f7c49ef0d35ec0e748659a7769d69629fba6b673e328617c6f6b202225` | `dataset_337459d527338ae07306907a349d470896874cf749ecc404e1189464b4efa4dd` |
| ETH | `dataset_ee6f2453c5833be3d6ec9990d38d270888eade34b862ab6cef6aa9edbecf6686` | `dataset_68140c301703db679006c657729bf5adae20da1f57e5ec52c132d9af70dd0e88` |
| SOL | `dataset_bf7af758027f00b645928770f28cc22f5281ce552324aff76d08298326bd5f1b` | `dataset_273780b01bfb6dffb4c651c82f9614b210f5c2e167758734e983bfc093a092bd` |
| HYPE | `dataset_5b1db9f8cfef88465937348656205d9bcbba806699e35938400010fd139752c1` | `dataset_5baf4d84288187b36884d779807c4c53ce1fe2dcd9e663b0c2ee571136374d04` |
| LINK | `dataset_979daf76e816966ec5e095a8bf1be9c18c0dd9c5757b3a6517f4e5d41156c7a2` | `dataset_a87faa3c9fc458989fc197b3fc9007cf036ac4495e64c1b3604d3e30256ebf87` |
| AAVE | `dataset_172c1f893aba164333b4ff34af3d7baec46c889c7062a96a6ce1b633d226c8c8` | `dataset_73e5564789120253b1d6147a9032017d39f007fcd344d2a4c5bb90587b55947d` |

### Why 4h primary, 1h secondary, no daily, no 15m

- **4h primary.** Hyperliquid keeps 5,000 4h bars (≈ 2.3 years), enough for a 30-day beta
  (180 bars) to be stable and for ≈ 700 non-overlapping daily cross-sections per venue.
  Horizons of 4 h / 1 day / 3 days are interpretable.
- **1h secondary** (central parameters only, separate families). It asks whether a faster
  bar tells the same story. Hyperliquid 1h covers only 5.5 months of events.
- **Daily** was not run. Hyperliquid daily history gives ≈ 20 independent z ≥ 2 events per
  alt at multi-day horizons, too few to gate. Daily bars also live in a different table
  (`perp_bars`) that the intraday study adapter does not read.
- **15m** was not run (prompt default; ≈ 52 days of Hyperliquid history).

## Definitions (`relative_strength_primitives_v1`)

Every quantity at bar `t` reads bars `≤ t` and is available at the newest bar's
availability. Windows containing a missing bar are undefined (never forward-filled).

| quantity | definition |
|---|---|
| raw return | `R_a(L) = c_a[t] / c_a[t−L] − 1` (the Lab's `ret_L`), every bar of the window present |
| BTC-relative | `R_a(L) − R_BTC(L)` |
| market-relative | `R_a(L) − mean_j R_j(L)` over alts defined at t (equal weight, self included) |
| rolling beta / corr | sample moments of the W one-bar return pairs ending at t, computed exactly per window. Undefined if any pair is missing, if BTC's per-bar std < 1e-5 (near-zero variance), or if a variance is 0 |
| residual | `R_a(L) − β_a(t−L) · R_BTC(L)`: the beta of the W bars **ending where the residual window starts**, i.e. "what its historical beta predicted", never fitted on the move |
| z-scores | each L-bar quantity / (its one-bar std over that same prior window × √L). Raw `sd(x_a)`, BTC-relative `sd(x_a − x_BTC)`, residual `sd(x_a)·√(1−ρ²)`, market-relative `sd(x_a − x_basket)` |
| correlation breakdown | `corr_W(t−S) ≥ high` and `corr_S(t) ≤ corr_W(t−S) − drop`, edge-triggered, oriented by the sign of the S-bar residual vs `β(t−S)` |
| cross-sectional rank | eligible alts only, rank 1 = largest, ties by alt order. Leader / laggard entry is edge-triggered and needs ≥ 4 eligible alts |
| BTC consolidation | Phase 17's frozen range rule on BTC's own series: efficiency ratio ER(20) < 1/√20 (no more directional than a random walk). Trend = sign(close − EMA 50). Volatility = ATR(14)/close ranked over the trailing 30 days, terciles |
| broad pullback | `R_BTC(L) < 0` **and** equal-weight alt basket `R(L) < 0` |

Edge triggering: a persistent excursion fires once, on the bar it begins (`z[t] ≥ thr` and
`z[t−1] < thr`, both defined). Raw persistent bars are reported beside edge and
independent events (4h HL, BTC-relative strength: 979 bars beyond +2 → 199 edges → 155
independent).

### Entry, targets, costs

- **Entry:** the open of the first bar opening at/after availability (close + 60 s). For a
  4h signal that is the open **two** bars later (`t + 2`), one full bar after the signal
  closed. This is conservative, and the same for every member. A non-executable
  signal-close reference is reported beside it.
- **Exit:** the close of the H-th bar. Horizons are 1 / 6 / 18 bars, and the **primary is 6**
  (1 day at 4h, 6 hours at 1h) for every family, fixed before running.
- **Targets with orientation d** (+1 long the alt side):
  - `rel` = d·(r_a − r_BTC), both legs' costs (2 × (fee + slip) on the alt **and** on BTC),
    funding on both legs;
  - `res` = d·(r_a − β(t)·r_BTC), costs on the alt and |β| × BTC;
  - `usd` = d·r_a, the alt alone.
- **Costs** from `perps.backtest.perp_costs`, frozen per venue/coin: fee 4.5 bps (HL) /
  5.0 (BN); slippage BTC/ETH 2, SOL 4, HYPE/LINK 6, AAVE 8 bps per side. A BTC-relative spread
  on SOL costs 2 × (8.5 + 6.5) = 30 bps round trip on HL, so **no spread is free**.
- **Baseline / excess:** every eligible bar of the same coin, entered the same way, with the
  same orientation, target, costs and horizon. The matched cell is (coin, orientation,
  horizon, BTC volatility tercile), plus the condition's own label for conditional members.
  Excess = event net − cell mean net. This removes the unconditional drift (e.g. alts
  bleeding against BTC), so "weak stays weak" cannot pass on the drift alone.
- **Independent events:** greedy declustering per (coin, orientation), gap = horizon, one
  event per entry bar (Phase 17's rule).

## Preregistration

Frozen before any outcome was computed, by `market relative study register` on the Phase
17 governance adapter (same tables, same lifecycle: the run row is committed before
evaluation, there is one result per run, reruns are explicit, and `EXPLORATORY` is a
CHECK). The adapter now dispatches on `study_version`. Nothing else in it changed.

- Manifest `config/relative/phase18_relative_strength.v1.yaml`: windows, universe and
  exclusions, central parameters, sensitivity axes, horizons, regime vocabulary, pullback
  rule, statistics, gates and verdict policy.
- **Family membership** (`spec.FAMILIES`) is hashed into the definition. A definition whose
  families differ is refused.
- Frozen costs, 12 retained datasets, semantic versions (`relative_strength_v1`,
  `relative_strength_primitives_v1`, `relative_inference_v1`, `phase18_verdicts_v1`).
- Software `software_384ec68e…` (commit `c4f807c`, clean tree) for registration and both runs.

### Exact grids

Central (4h and 1h, in bars) and the one-at-a-time neighbours (primary only: 13 variants,
never a grid):

| parameter | central | neighbours |
|---|---|---|
| lookback L | 18 (3 days at 4h) | 6, 42 |
| beta / correlation window W | 180 (30 days) | 90, 360 |
| z threshold | 2.0 | 1.5, 2.5 |
| prior correlation "high" | 0.7 | 0.6, 0.8 |
| correlation drop | 0.3 | 0.2, 0.4 |
| short correlation window S | 42 (7 days) | 18, 84 |
| H6 → breakdown confirmation window | 18 | – |
| minimum eligible alts in a cross-section | 4 | – |

### Inference (`relative_inference_v1`) and why it differs from Phase 17

Relative signals fire on several alts at once (a BTC move moves every BTC-relative
spread), and their forward spreads share the BTC leg. Phase 17's matched random-entry
null draws each event's comparison independently. On co-timed events that understates the
variance.

The primary test is therefore a **block-clustered t-test**:

- events are grouped into calendar blocks of 6 bars shared by all assets;
- the variance adds the adjacent-block covariance (truncated, floored at the pure cluster
  variance);
- the reference distribution is Student t with G − 1 degrees of freedom, two-sided.

Per-timestamp statistics (IC, buckets, spread) use non-overlapping cross-sections every
6 bars, each its own block. The Phase 17 independent-draw p (2,000 draws) is reported beside
it as `p naive`, for comparison only.

Families (BH, q = 0.10, the Lab's implementation), per venue × architecture, never pooled:

| family | m | members |
|---|---:|---|
| relative_momentum | 6 | rel / raw / mkt × strong / weak |
| residual | 5 | res / rel × strong / weak + ic_res |
| dislocation | 3 | breakdown, tight_res_strong, tight_res_weak |
| interactions | 8 | consolidation × 2, contrasts × 2, pullback × 2, contrasts × 2 |
| cross_section | 4 | ic_rel, top_persists, bottom_persists, top_minus_bottom |
| absolute | 4 | rel / res × strong / weak, USD target |

Sensitivity is descriptive only and never tested.

### Gates and verdicts (`phase18_verdicts_v1`)

**Sample gates:**

- event members: ≥ 30 independent events, ≥ 3 assets with events, ≥ 80% evaluable,
  ≥ 20 clusters;
- per-timestamp members: ≥ 100 timestamps.

Gates were never relaxed.

**Floors:** 10 bps net excess (events, contrasts, spreads), 0.03 mean Spearman (IC) and
0.03 probability (buckets).

| verdict | rule (per member and direction) |
|---|---|
| ROBUST ENOUGH FOR NEXT RESEARCH STAGE | q ≤ 0.10 with this sign; statistic ≥ floor; net > 0 (positions); positive-asset share ≥ 60%; every leave-one-asset-out keeps the sign; PLATEAU; the other venue agrees (same sign, one-sided p ≤ 0.05) |
| PROMISING — NEEDS VALIDATION | as above without the plateau or the agreement |
| WEAK / EXPLORATORY | q ≤ 0.10 but a substantive gate fails, or one-sided p ≤ 0.05 without correction |
| REJECTED | q ≤ 0.10 with the opposite sign, or the 95% upper bound is below the floor |
| NO EVIDENCE | otherwise |
| INSUFFICIENT | a sample gate failed |

## Null calibration (before the real run)

The full pipeline was run with central parameters on a **correlated synthetic market**:

- a BTC factor with slowly varying volatility regimes and Student-t(4) innovations;
- alts = β × BTC + a shared alt factor + fat-tailed idiosyncratic noise;
- a late-listed alt;
- **no forward-return information**.

4 seeds, 477 testable members (`market relative study calibrate`):

| | block-clustered (primary) | Phase 17 independent draws |
|---|---:|---:|
| share p < 0.05 | **1.9%** | 6.3% |
| share p < 0.10 | **5.9%** | 12.3% |
| BH discoveries (q ≤ 0.10), 4 seeds × 48 families | 3 (1 / 0 / 2 / 0) | – |

- p deciles (primary): 28 / 42 / 46 / 51 / 63 / 55 / 56 / 46 / 33 / 57. The test is
  slightly conservative, as t-tests are under fat tails. The independent-draw null is
  already inflated on this mildly correlated market, which is why it is not the primary
  test.
- **Power check:** with planted residual momentum (each alt's idiosyncratic return loads 0.3
  on its own previous 18-bar mean), 50% of all tests have p < 0.05 and BH makes 115
  discoveries in 2 seeds. `ic_res` detects the planted effect at p ≈ 0 on every
  venue, timeframe and seed. Threshold-event tests detect it in most cells. The harness is
  conservative, not blind.
- On the real data the same contrast holds: in the 82 event members, 4 have clustered
  p < 0.05 versus 12 with the naive p.

## Results — primary 4h

Columns:

- *stat* is continuation-oriented: bps of net excess, or IC / probability for
  cross-section members;
- *net* is the position's mean net return after costs;
- *p* is the two-sided clustered p, and *p naive* the Phase 17 independent-draw p.

### Hyperliquid (2024-11-01 → 2026-10-01)

| family | member | n | stat | 95% CI | net | p | p naive | q | continuation / reversal |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| relative_momentum | rel_strong | 155 | +13.3 | [−70.5, +97.2] | −4.2 | 0.753 | 0.624 | 0.897 | NO EVIDENCE / NO EVIDENCE |
| | rel_weak | 75 | −31.5 | [−121.6, +58.6] | −80.6 | 0.486 | 0.446 | 0.897 | NO EVIDENCE / NO EVIDENCE |
| | raw_strong | 137 | +18.9 | [−77.7, +115.4] | +2.4 | 0.699 | 0.546 | 0.897 | NO EVIDENCE / NO EVIDENCE |
| | raw_weak | 88 | −25.5 | [−101.7, +50.7] | −73.2 | 0.505 | 0.485 | 0.897 | NO EVIDENCE / NO EVIDENCE |
| | mkt_strong | 123 | +39.9 | [−56.7, +136.5] | +23.7 | 0.415 | 0.234 | 0.897 | NO EVIDENCE / NO EVIDENCE |
| | mkt_weak | 125 | +3.8 | [−54.0, +61.6] | −38.1 | 0.897 | 0.928 | 0.897 | NO EVIDENCE / NO EVIDENCE |
| residual | res_strong | 159 | +58.2 | [−29.3, +145.7] | +37.3 | 0.190 | 0.043 | 0.630 | NO EVIDENCE / NO EVIDENCE |
| | res_weak | 65 | −58.3 | [−159.3, +42.7] | −114.7 | 0.252 | 0.183 | 0.630 | NO EVIDENCE / NO EVIDENCE |
| | rel_strong | 155 | +23.8 | [−60.5, +108.2] | +2.4 | 0.577 | 0.387 | 0.721 | NO EVIDENCE / NO EVIDENCE |
| | rel_weak | 75 | −35.3 | [−128.1, +57.5] | −92.5 | 0.449 | 0.364 | 0.721 | NO EVIDENCE / NO EVIDENCE |
| | ic_res | 698 | +0.006 | [−0.036, +0.048] | – | 0.785 | – | 0.785 | NO EVIDENCE / NO EVIDENCE |
| dislocation | breakdown | 44 | −58.3 | [−226.2, +109.6] | −92.4 | 0.486 | 0.258 | 0.628 | NO EVIDENCE / NO EVIDENCE |
| | tight_res_strong | 92 | +22.0 | [−68.1, +112.1] | −0.2 | 0.628 | 0.425 | 0.628 | NO EVIDENCE / NO EVIDENCE |
| | tight_res_weak | 45 | −73.2 | [−173.9, +27.6] | −113.6 | 0.150 | 0.079 | 0.449 | NO EVIDENCE / NO EVIDENCE |
| interactions | consol_rel_strong | 80 | +12.3 | [−112.6, +137.2] | −10.2 | 0.845 | 0.742 | 0.980 | NO EVIDENCE / NO EVIDENCE |
| | consol_rel_weak | 20 | −125.5 | – | −171.8 | – | – | – | INSUFFICIENT |
| | consol_vs_not_rel_strong | 80 | −2.0 | [−162.8, +158.8] | – | 0.980 | – | 0.980 | NO EVIDENCE / NO EVIDENCE |
| | consol_vs_not_rel_weak | 20 | −135.2 | – | – | – | – | – | INSUFFICIENT |
| | pullback_leader | 281 | −11.0 | [−63.3, +41.4] | −34.0 | 0.680 | 0.588 | 0.980 | NO EVIDENCE / NO EVIDENCE |
| | pullback_laggard | 282 | −14.8 | [−65.8, +36.1] | −67.7 | 0.567 | 0.494 | 0.980 | NO EVIDENCE / NO EVIDENCE |
| | pullback_vs_not_leader | 281 | +16.1 | [−51.2, +83.3] | – | 0.639 | – | 0.980 | NO EVIDENCE / NO EVIDENCE |
| | pullback_vs_not_laggard | 282 | −10.0 | [−69.9, +49.8] | – | 0.742 | – | 0.980 | NO EVIDENCE / NO EVIDENCE |
| cross_section | ic_rel | 698 | −0.002 | [−0.045, +0.041] | – | 0.922 | – | 0.922 | NO EVIDENCE / NO EVIDENCE |
| | top_persists | 698 | +0.040 | [+0.001, +0.080] | – | 0.047 | – | 0.186 | WEAK / REJECTED |
| | bottom_persists | 698 | +0.009 | [−0.030, +0.048] | – | 0.666 | – | 0.888 | NO EVIDENCE / NO EVIDENCE |
| | top_minus_bottom | 698 | +22.5 | [−12.6, +57.7] | −17.5 | 0.209 | – | 0.417 | NO EVIDENCE / NO EVIDENCE |
| absolute | rel_strong | 155 | −5.7 | [−112.7, +101.3] | −7.3 | 0.917 | 0.847 | 0.917 | NO EVIDENCE / NO EVIDENCE |
| | rel_weak | 75 | −44.4 | [−162.7, +74.0] | −83.1 | 0.456 | 0.472 | 0.691 | NO EVIDENCE / NO EVIDENCE |
| | res_strong | 159 | +44.7 | [−71.6, +161.0] | +43.0 | 0.448 | 0.222 | 0.691 | NO EVIDENCE / NO EVIDENCE |
| | res_weak | 65 | −59.6 | [−243.5, +124.3] | −96.4 | 0.518 | 0.316 | 0.691 | NO EVIDENCE / NO EVIDENCE |

### Binance (2021-03-01 → 2024-11-01)

| family | member | n | stat | 95% CI | net | p | p naive | q | continuation / reversal |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| relative_momentum | rel_strong | 263 | +32.3 | [−29.4, +94.0] | +7.3 | 0.304 | 0.180 | 0.669 | NO EVIDENCE / NO EVIDENCE |
| | rel_weak | 137 | −44.4 | [−150.4, +61.6] | −86.2 | 0.408 | 0.181 | 0.669 | NO EVIDENCE / NO EVIDENCE |
| | raw_strong | 240 | −19.2 | [−83.5, +45.2] | −42.4 | 0.557 | 0.496 | 0.669 | NO EVIDENCE / NO EVIDENCE |
| | raw_weak | 129 | −79.9 | [−289.2, +129.3] | −123.8 | 0.449 | 0.031 | 0.669 | NO EVIDENCE / NO EVIDENCE |
| | mkt_strong | 218 | +46.5 | [−28.4, +121.4] | +22.8 | 0.222 | 0.112 | 0.669 | NO EVIDENCE / NO EVIDENCE |
| | mkt_weak | 199 | +3.5 | [−54.8, +61.9] | −37.4 | 0.905 | 0.905 | 0.905 | NO EVIDENCE / NO EVIDENCE |
| residual | res_strong | 263 | +63.8 | [+1.8, +125.8] | +36.0 | 0.044 | 0.018 | 0.218 | WEAK / REJECTED |
| | res_weak | 179 | +18.4 | [−50.2, +87.1] | −28.2 | 0.596 | 0.553 | 0.745 | NO EVIDENCE / NO EVIDENCE |
| | rel_strong | 263 | +42.7 | [−19.0, +104.4] | +14.6 | 0.174 | 0.088 | 0.436 | NO EVIDENCE / NO EVIDENCE |
| | rel_weak | 137 | −52.2 | [−147.5, +43.1] | −97.9 | 0.280 | 0.124 | 0.467 | NO EVIDENCE / NO EVIDENCE |
| | ic_res | 1340 | +0.003 | [−0.029, +0.036] | – | 0.834 | – | 0.834 | NO EVIDENCE / REJECTED |
| dislocation | breakdown | 76 | +9.7 | [−105.9, +125.3] | −29.0 | 0.867 | 0.830 | 0.867 | NO EVIDENCE / NO EVIDENCE |
| | tight_res_strong | 166 | +46.9 | [−18.4, +112.2] | +14.3 | 0.158 | 0.093 | 0.236 | NO EVIDENCE / NO EVIDENCE |
| | tight_res_weak | 144 | +60.9 | [−10.2, +132.0] | +20.7 | 0.092 | 0.043 | 0.236 | WEAK / NO EVIDENCE |
| interactions | consol_rel_strong | 146 | +0.1 | [−84.0, +84.2] | −32.4 | 0.998 | 0.981 | 0.998 | NO EVIDENCE / NO EVIDENCE |
| | consol_rel_weak | 34 | −40.8 | [−292.2, +210.6] | −78.1 | 0.742 | 0.498 | 0.989 | NO EVIDENCE / NO EVIDENCE |
| | consol_vs_not_rel_strong | 127 | −74.7 | [−189.5, +40.0] | – | 0.201 | – | 0.598 | NO EVIDENCE / NO EVIDENCE |
| | consol_vs_not_rel_weak | 34 | −2.1 | [−258.7, +254.5] | – | 0.987 | – | 0.998 | NO EVIDENCE / NO EVIDENCE |
| | pullback_leader | 416 | +20.0 | [−24.5, +64.5] | −13.2 | 0.377 | 0.281 | 0.602 | NO EVIDENCE / NO EVIDENCE |
| | pullback_laggard | 478 | −33.8 | [−79.1, +11.4] | −70.5 | 0.143 | 0.073 | 0.598 | NO EVIDENCE / NO EVIDENCE |
| | pullback_vs_not_leader | 416 | +23.9 | [−29.0, +76.8] | – | 0.375 | – | 0.602 | NO EVIDENCE / NO EVIDENCE |
| | pullback_vs_not_laggard | 478 | −32.2 | [−84.1, +19.7] | – | 0.224 | – | 0.598 | NO EVIDENCE / NO EVIDENCE |
| cross_section | ic_rel | 1340 | +0.001 | [−0.031, +0.033] | – | 0.964 | – | 0.979 | NO EVIDENCE / NO EVIDENCE |
| | top_persists | 1340 | −0.001 | [−0.028, +0.025] | – | 0.913 | – | 0.979 | REJECTED / REJECTED |
| | bottom_persists | 1340 | +0.007 | [−0.019, +0.034] | – | 0.585 | – | 0.979 | NO EVIDENCE / REJECTED |
| | top_minus_bottom | 1340 | +0.3 | [−25.6, +26.3] | −40.2 | 0.979 | – | 0.979 | NO EVIDENCE / NO EVIDENCE |
| absolute | rel_strong | 263 | −3.3 | [−82.0, +75.4] | −5.8 | 0.934 | 0.948 | 0.950 | NO EVIDENCE / NO EVIDENCE |
| | rel_weak | 137 | −81.9 | [−246.2, +82.4] | −120.8 | 0.325 | 0.071 | 0.816 | NO EVIDENCE / NO EVIDENCE |
| | res_strong | 263 | +2.4 | [−73.8, +78.7] | +0.8 | 0.950 | 0.953 | 0.950 | NO EVIDENCE / NO EVIDENCE |
| | res_weak | 179 | +46.6 | [−64.6, +157.9] | +6.6 | 0.408 | 0.289 | 0.816 | NO EVIDENCE / NO EVIDENCE |

Contrast rows show no *net* value because a difference of two positions is not itself a
position. Their verdicts use the inside position's net.

## Results — secondary 1h (central parameters only)

The full tables are in the stored result (`market relative study report <run>`). Only
cells that are not NO EVIDENCE:

| venue | member | stat | q | verdicts (continuation / reversal) |
|---|---|---:|---:|---|
| Hyperliquid (2026-04-15 → 10-01) | cross_section `top_persists` | **+0.053** [+0.016, +0.091] | **0.022** | **PROMISING — NEEDS VALIDATION** / REJECTED |
| | cross_section `top_minus_bottom` | +14.5 bps gross, **−26.4 net** | 0.094 | WEAK (net < 0) / REJECTED |
| | relative_momentum `mkt_strong` | +40.9 bps | 0.213 | WEAK (raw p only) / REJECTED |
| | relative_momentum `rel_weak` | +30.5 bps | 0.213 | WEAK (a weak alt *keeps* underperforming) / REJECTED |
| | interactions `consol_rel_weak` | +49.4 bps (n = 30) | 0.565 | WEAK / REJECTED |
| | 10 × REJECTED | mostly the reversal side of positive cells and ICs bounded inside ±0.03 | | |
| | dislocation `breakdown` | n = 26 | – | INSUFFICIENT |
| Binance (2024-11-01 → 2026-04-01) | dislocation `breakdown` | +38.1 bps excess, **−3.3 net** | **0.074** | WEAK (net < 0) / REJECTED |
| | interactions `consol_rel_weak` / contrast | −32.5 / −53.4 bps | 0.139 | REJECTED / WEAK (reversal: weak alts rebound in consolidation) |
| | cross_section `ic_rel`, buckets, spread; residual `ic_res`; relative `mkt_weak` | CIs within the floors | – | REJECTED (13 in all) |

The 1h Hyperliquid window is 5.5 months in one regime. The 1h Binance window is 17 months
and finds nothing beyond one WEAK breakdown cell that loses money after costs.

## Continuous relationships (descriptive; never used to choose thresholds)

These are pooled over non-overlapping cross-section samples (4h: 3,422 HL / 5,360 BN
alt-timestamps). Quintile means of the forward target are in bps.

| venue | signal → target | Spearman | quintile means (low → high) |
|---|---|---:|---|
| HL 4h | BTC-relative L → forward BTC-relative | −0.050 | +31.5, −2.6, +4.8, +10.8, +6.4 |
| HL 4h | residual L → forward residual | −0.051 | +40.3, +6.7, −15.0, +10.1, +11.9 |
| HL 4h | correlation change → oriented forward residual | −0.001 | +2.1, −4.9, +4.5, −9.8, −11.7 |
| BN 4h | BTC-relative → forward BTC-relative | −0.016 | +16.4, −1.8, −0.5, +5.7, +24.8 |
| BN 4h | residual → forward residual | −0.006 | +20.6, +4.1, −4.7, −11.5, +38.2 |
| HL 1h | BTC-relative → forward BTC-relative | +0.000 | −2.1, +4.1, +1.2, +8.9, +15.7 |
| BN 1h | BTC-relative → forward BTC-relative | −0.020 | +6.0, +4.7, −7.2, −1.5, +3.4 |

There is no gradient. The 4h extremes are U-shaped: both tails beat the middle. That
reflects volatility (both tails are the most volatile states, and the excess is measured
against all bars), not direction. It is the opposite of a monotonic momentum or reversal
relation. The HL 1h quintiles rise monotonically (monotonicity 0.9), but the pooled
Spearman is 0.000: the gradient lives in the tails of a few observations.

## Cross-sectional persistence

| | HL 4h | BN 4h | HL 1h | BN 1h |
|---|---:|---:|---:|---:|
| timestamps (every 6 bars) | 698 | 1,340 | 675 | 2,063 |
| rank IC (BTC-relative) | −0.002 | +0.001 | +0.031 | −0.016 |
| rank IC (residual) | +0.006 | +0.003 | +0.024 | −0.019 |
| leader finishes top half, minus chance | +0.040 (q 0.19) | −0.001 | **+0.053 (q 0.022)** | −0.004 |
| laggard finishes bottom half, minus chance | +0.009 | +0.007 | +0.028 | +0.003 |
| leader − laggard, gross / net (bps) | +22.5 / −17.5 | +0.3 / −40.2 | +14.5 / −26.4 | −2.2 / −43.7 |

Leader / middle / laggard forward returns (gross bps over the next 6 bars, descriptive):

| | HL 4h: USD / vs BTC | BN 4h: USD / vs BTC |
|---|---|---|
| leader | +37.2 / +31.9 | +18.8 / +10.7 |
| middle | +6.6 / +3.0 | +15.5 / +7.3 |
| laggard | +14.6 / +9.4 | +18.5 / +10.3 |
| leader in pullbacks | +24.0 / +15.7 | +4.9 / +1.9 |
| laggard in pullbacks | +38.7 / +30.5 | −2.3 / −5.3 |

The HL 4h leader bucket's gross +32 bps against BTC is the largest descriptive number in the
study. It is HYPE: HYPE was the leader at 219 of 698 timestamps and trended up through
2025. Leader persistence is +14.3 pp when HYPE leads, and without HYPE it is −2.3 pp
(leave-one-out). On Binance (no HYPE) leaders and laggards are indistinguishable.

## Correlation dislocation

| | HL 4h | BN 4h | HL 1h | BN 1h |
|---|---:|---:|---:|---:|
| breakdown events (independent) | 44 | 76 | 26 | 104 |
| breakdown excess (bps, continuation) | −58.3 | +9.7 | −25.5 (INSUFF.) | +38.1 (q 0.074, net −3.3) |
| H6 residual shocks while tightly correlated | 173 | 383 | 193 | 566 |
| … later confirmed by a breakdown (≤ 18 bars) | 17 (9.8%) | 29 (7.6%) | 14 (7.3%) | 37 (6.5%) |
| median confirmation delay (bars) | 8 | 10 | 4.5 | 8 |
| median entry move before confirmation (bps, trade direction) | +412 | +183 | +211 | −12 |
| confirmed, measured from the shock (**not executable**) | +268 (n = 13) | +103 (n = 25) | +29 (n = 12) | +3 (n = 28) |
| confirmed, entered at the breakdown (executable) | +157 (n = 11) | +5 (n = 24) | −59 (n = 8) | +23 (n = 26) |
| never confirmed, from the shock | −3 (n = 126) | +50 (n = 286) | +2 (n = 143) | −7 (n = 430) |

A rolling correlation reacts slowly, so by the time it "breaks" the divergence is mostly
done (a median 183–412 bps of travel on 4h). The executable excess is what remains. The
HL 4h +157 bps rests on 11 events. Correlation change has no continuous relationship with
the oriented forward residual (Spearman −0.001 / +0.007 / +0.014 / +0.002). "Decoupling"
is not used anywhere: the event is the objective rule above.

## BTC consolidation

BTC was in a consolidation state (Phase 17 range rule) on 52–56% of bars on every venue.
Relative strength during consolidation:

- 4h: +12 (HL) / +0 (BN) bps, versus consolidation-matched random entries.
- Consolidation-minus-otherwise contrast: −2 (HL) / −75 (BN) bps for strong alts. Weak
  alts on HL are INSUFFICIENT (20 events).
- 1h: strong −11 / +15 bps. Weak alts in consolidation *rebound* on Binance (−32.5 bps
  continuation, contrast −53, q = 0.14) and *continue* on HL (+49, n = 30): OPPOSITE.

The claim that relative strength is "especially informative when BTC is consolidating" is
not supported. Every consolidation interaction is known at the signal bar, so there is no
confirmation delay to decompose.

## Pullback leadership

Pullback (BTC and the alt basket both down over 3 days) covers 38–40% of bars.

- Alts becoming the leader during a pullback: −11 (HL 4h), +20 (BN 4h), +1 (BN 1h),
  +10 (HL 1h) bps.
- Laggards: −15, −34, −2, +4.
- Every pullback-vs-not contrast has q ≥ 0.6.

Descriptively, on HL 4h laggards beat leaders in pullbacks (+31 vs +16 bps against BTC).
On Binance 4h leaders do slightly better (+2 vs −5). Strong assets do not measurably stay
strong in pullbacks.

## Absolute vs relative

The same independent events, measured three ways (4h, continuation, excess in bps):

| venue | event | BTC-relative | residual | USD (alt alone) | BTC forward mean | relative won but alt lost |
|---|---|---:|---:|---:|---:|---:|
| BN | residual strength | +47.8 | +63.8 | +2.4 | −36.8 | 9% |
| BN | BTC-relative strength | +32.3 | +42.7 | −3.3 | −27.1 | 9% |
| BN | weakness (rel) | −44.4 | −52.2 | −81.9 | +48.6 | 15% |
| HL | residual strength | +55.7 | +58.2 | +44.7 | −8.2 | 7% |
| HL | BTC-relative strength | +13.3 | +23.8 | −5.7 | −16.1 | 6% |
| HL | weakness (rel) | −31.5 | −35.3 | −44.4 | +15.6 | 9% |

Strength events tend to be followed by a falling BTC (−8 to −37 bps on average). Where a
spread leaned positive, BTC's fall supplied part of it, and the USD-directional alt leg
earned ≈ 0. The **absolute** family is NO EVIDENCE in all 16 cells. **No relative signal
here is a USD-directional trade**, and no relative signal is a significant spread either.

## Asset breadth

| cell (the ones that leaned) | assets + / − | median asset effect | largest share of \|contribution\| | leave-one-out |
|---|---|---:|---:|---|
| BN 4h residual strength | 3 / 1 (AAVE −69 bps) | +68 bps | 52% (SOL) | all positive (+16 … +113) |
| BN 4h tight residual weakness | 3 / 1 (ETH −19) | +94 bps | 37% (SOL) | all positive |
| BN 1h correlation breakdown | 4 / 1 (HYPE, n = 3) | +32 bps | 40% (LINK) | all positive |
| HL 4h leader persists | 3 / 2 (LINK, SOL) | – | HYPE leads 31% of timestamps | **drop HYPE → −0.023** |
| HL 1h leader persists | 4 / 1 (ETH −0.11) | – | HYPE 35% | all positive (+0.017 … +0.036) |

## Stability

### Chronological thirds (primary statistic per third)

- **HL 4h leader persists:** −0.026 / +0.025 / +0.122. It is concentrated in the late
  third (2026).
- **HL 1h leader persists:** +0.073 / +0.036 / +0.051. This is the most stable cell, but
  the whole window is 5.5 months.
- **BN 4h residual strength:** +132 / +15 / +37 bps. It is concentrated in 2021.

### BTC regime and volatility

There is no consistent pattern:

- HL 1h leader persistence is +0.109 in BTC uptrends and +0.033 in consolidation.
- BN 4h residual strength is flat across regimes (+60 to +72 bps) but varies with
  volatility: +131 high, +50 low, −9 mid.

These are descriptive cells, never tested and never selected.

## Sensitivity (one-at-a-time, descriptive)

Plateau verdicts (Prism `plateau_verdict`, frozen 0.5 / 0.5 / 60%) per member and
direction, primary 4h:

| | PLATEAU | NO_EDGE | FRAGILE | MIXED | INSUFFICIENT |
|---|---:|---:|---:|---:|---:|
| Hyperliquid (60) | 13 | 28 | 8 | 7 | 4 |
| Binance (60) | 14 | 30 | 11 | 5 | 0 |

- The positive-leaning 4h cells hold their sign across lookback, beta window and threshold.
  BTC-relative strength ranges +8 … +71 bps (HL) and +1 … +74 bps (BN), and BN residual
  strength +15 … +64 bps. These are broad plateaus of effects that are never significant.
  No neighbour holds a hidden large effect.
- Raw-momentum controls are FRAGILE or MIXED on Binance (sign agreement 29–71%).
- No neighbour was tested, selected or promoted.

## Multiple testing

| family | HL 4h: m, min p, min q | BN 4h: m, min p, min q | HL 1h: m, min q | BN 1h: m, min q |
|---|---|---|---|---|
| relative_momentum | 6, 0.415, 0.897 | 6, 0.222, 0.669 | 6, 0.213 | 6, 0.467 |
| residual | 5, 0.190, 0.630 | 5, 0.044, 0.218 | 5, 0.432 | 5, 0.696 |
| dislocation | 3, 0.150, 0.449 | 3, 0.092, 0.236 | 2, 0.637 | 3, **0.074** |
| interactions | 6 (2 insufficient), 0.567, 0.980 | 8, 0.143, 0.598 | 8, 0.565 | 8, 0.139 |
| cross_section | 4, 0.047, 0.186 | 4, 0.585, 0.979 | 4, **0.022** | 4, 0.779 |
| absolute | 4, 0.448, 0.691 | 4, 0.325, 0.816 | 4, 0.756 | 4, 0.559 |

There are three q ≤ 0.10 cells out of 117 tested:

- HL 1h `top_persists`, q = 0.022, PROMISING;
- BN 1h `breakdown`, q = 0.074, WEAK because it loses money after costs;
- HL 1h `top_minus_bottom`, q = 0.094, WEAK because it loses money after costs.

Under the calibrated null, about 3 discoveries per 4 seeds × 48 families is the
false-discovery background, and this run has 3 in 24 families (two of them unprofitable after costs). Every raw p and q is in the
stored result (`market relative study report <run> --json`).

## Cross-venue (never pooled)

| | SIMILAR | MIXED | OPPOSITE | INSUFFICIENT |
|---|---:|---:|---:|---:|
| primary 4h (60 member × direction pairs) | 46 | 8 | 2 | 4 |
| secondary 1h (60) | 26 | 30 | 2 | 2 |

- **OPPOSITE:**
  - 4h tight-correlation residual weakness: BN continues (+61 bps, one-sided p = 0.046),
    HL reverses (−73, p = 0.075);
  - 1h consolidation × weakness: HL continues (+49, p = 0.035), BN reverses (−33,
    p = 0.017).
- **The PROMISING HL 1h leader persistence is not reproduced on Binance 1h** (−0.004, CI
  within ±0.026: REJECTED in both directions). The WEAK BN 4h residual strength is
  NO EVIDENCE on HL (+58 bps, p = 0.19). The two venues tell the same story: no stable
  relative-strength edge.

## Verdict

- **ROBUST: none.**
- **PROMISING — NEEDS VALIDATION:** one member, HL 1h `top_persists` (continuation).
  - The leader by 18-hour BTC-relative return finishes the next 6 hours in the top half
    45% of the time against a 40% chance rate (top 2 of 5), +5.3 pp, q = 0.022.
  - It passes every frozen gate: floor 3 pp, all leave-one-outs positive. It is not ROBUST
    because the secondary has no sensitivity axes and Binance disagrees.
  - What it is **not**:
    - not tradeable: long the leader / short the laggard earns +14.5 bps gross and
      −26.4 bps net;
    - not reproduced on Binance 1h, where an effect larger than ±3 pp is excluded;
    - not reproduced on HL 4h with HYPE removed;
    - measured over 5.5 months of one venue.
  - It is a rank-probability statistic, not an edge. This study recommends **no** follow-up
    effort on it beyond observing whether it holds on untouched post-2026-10-01 data if
    some later phase reads that data anyway.
- **WEAK / EXPLORATORY:**
  - BN 4h residual continuation (+64 bps, raw p 0.044, q 0.22; 2021-heavy, SOL-heavy);
  - BN 4h tight residual weakness (+61, raw p 0.09, OPPOSITE on HL);
  - HL 4h leader persistence (HYPE-dependent);
  - BN 1h correlation breakdown (q 0.074, negative after costs);
  - HL 1h `top_minus_bottom` (negative after costs), `mkt_strong`, `rel_weak`,
    `consol_rel_weak`;
  - BN 1h weak-alt rebound in consolidation.
- **REJECTED:** 29 member-directions, mostly because an economically useful effect is
  excluded:
  - Binance cross-sectional persistence and rank IC (CIs within ±0.03);
  - the reversal side of every positive-leaning cell.
- **INSUFFICIENT:** HL 4h consolidation × weakness (20 events) and HL 1h correlation
  breakdown (26 events). Rarity is a finding; the gates were not relaxed.
- **NO EVIDENCE:** everything else, including the central claims:
  - BTC-relative strength continuation;
  - weakness continuation;
  - residual momentum or reversal;
  - correlation breakdown;
  - the BTC-consolidation amplifier;
  - pullback leadership;
  - every USD-directional reading.

Nothing is promoted to the Phase 6 catalogue, prospective tracking, the co-pilot or paper
trading.

## Reproducibility

| run | attempt | software | digest |
|---|---|---|---|
| `srun_265b35bd23df478495efd2c5b74783e7` (governed, scratch DB) | 1 | `c4f807c` (`software_384ec68e…`) | `e22f8a0ac9933c8d51a0779487def81d32d4fb3571d799c24ddff15049e59ad9` |
| `srun_8cf749fcff8c4cd198f800a5d8a2c51f` (fresh scratch copy, `rerun_of` attempt 1) | 2 | `c4f807c` | `e22f8a0a…` (**identical**) |

The payload has no clock field. Timings and memory go to run metadata, as fixed in Phase 17.

## Performance

| | primary HL | primary BN | secondary HL | secondary BN |
|---|---:|---:|---:|---:|
| source bars read | 28,848 | 45,305 | 29,354 | 76,568 |
| grid bars / signal bars in window | 4,972 / 4,194 | 9,131 / 8,046 | 4,892 / 4,056 | 13,847 / 12,384 |
| baseline outcome rows (central; all horizons, both orientations) | 123,390 | 193,104 | 121,680 | 340,026 |
| event outcome rows (central) | 8,769 | 14,799 | 8,580 | 24,927 |
| cross-sectional alt-timestamps | 3,427 | 5,364 | 3,380 | 9,445 |
| parameter variants | 13 | 13 | 1 | 1 |
| seconds | 16.4 | 16.8 | 7.5 | 13.1 |

- Tests: 120 preregistered members (117 testable).
- Whole run: **54 s wall, 0.92 GB peak RSS** (WSL2, Python 3.12, one core).
- Registration (12 datasets captured and hashed): 15.7 s, 0.41 GB.
- Null calibration: about 40 s per seed.
- The per-timestamp cross-section statistics are vectorised: the first version spent
  25 s of a 37 s venue in a Python loop.

## Live compatibility

- No forward-tracking, co-pilot, paper-trading or runtime code was changed, and none of it
  imports `research.relative` (test `test_no_live_consumer_reads_phase18`). No job was
  added to the runtime schedule.
- **No migration.** Phase 18 studies live in the Phase 17 tables (`lab_structure_*`),
  distinguished by `study_version`. The adapter change is a dispatch, and Phase 17's
  governance tests pass unchanged.
- The study ran on a scratch copy (`PRISM_RUNTIME_ROLE=scratch`). `data/prism.duckdb` and
  the Railway database were not written.
- No policy, strategy family, evidence profile, paper rule or co-pilot rule changed.
  Nothing was enrolled for prospective tracking.

## Limitations

- **Historical intraday availability is reconstructed under an explicit latency assumption
  (60 s) rather than observed in real time.** The bars are backfilled.
- **Entry lag:** the conservative entry is one full bar after the signal closes (4 h on the
  primary). The signal-close reference is reported beside every event member:
  - for strength cells it differs by −15 to +26 bps;
  - on Hyperliquid, weakness cells are 16–63 bps more negative from the signal close
    (weak alts rebound within the first bar);
  - on Binance the differences are mixed (−28 to +26 bps).

  None of them would change a verdict. A 60 s execution would sit between the two.
- **Small universe:** four or five alts. Cross-sectional statistics are coarse (the
  "top quintile" is one asset), and HYPE's 2025 trend dominates Hyperliquid's leader
  statistics.
- **Short Hyperliquid history:** 23 months at 4h and 5.5 months at 1h. Binance is longer
  but earlier (2021–2024 and 2024–2026), a different venue and USDT-margined. Neither is
  untouched validation.
- **Dependence:** the block-clustered test handles co-timed events and adjacent overlap.
  BH within a family is still approximate because members share events, as in Phases 5
  and 17. The test is slightly conservative under the synthetic null (1.9% at 0.05).
- **One definition per concept** (`*_v1`): other consolidation, pullback or breakdown
  definitions are untested, by design.
- **Funding** is in every net-including-funding column. Binance HYPE has none.
- **Scratch data:** a public backfill into a scratch database on 2026-10-05, not production.

## Verification

| check | result |
|---|---|
| `pytest tests/test_relative_strength.py` | **28 passed**. Covers: causal beta (hand OLS, future rows rewritten), causal correlation, zero BTC variance, hand-calculated relative / residual / z-scores, eligible-universe ranks, future listings, missing bars, correlation-breakdown timing (brute-force reference), BTC-consolidation rule, rank persistence by hand, edge triggering, entry timing, absolute vs relative never mixed, both-leg spread costs, Student-t / clustered variance by hand, co-timed events as one cluster, verdict policy, direction mirroring, family/BH membership, venue isolation, determinism, future immunity, decomposition, sensitivity, manifest validation, governed preregistration lifecycle, governed evaluation from retained snapshots, null-calibration smoke, consumer isolation |
| `pytest` Phase 17 + intraday + Phase 18 | passed (Phase 17's 27 unchanged) |
| `pytest` (full) | **765 passed** (exit 0); before Phase 18: 737 |
| `ruff check src tests dashboard` / `ruff format --check src tests` / `git diff --check` | passed |
| Null calibration (4 seeds, 477 tests) | 1.9% p < 0.05, 5.9% p < 0.10, 3 BH discoveries; naive independent-draw null 6.3% / 12.3% |
| Power check (planted 0.3, 2 seeds) | 50% p < 0.05, `ic_res` p ≈ 0 everywhere |
| Governed run | `srun_265b35bd…` COMPLETED, 54 s / 0.92 GB |
| Reproducibility rerun (fresh scratch copy) | digest `e22f8a0a…` reproduced exactly |

## Next recommended phase

**Phase 19: open-interest × price research.** It is already planned to follow this phase.
It should use the same governed study adapter (a frozen manifest, retained OI datasets,
per-venue families, block-clustered inference for co-timed signals) and Phase 18's
conditional-versus-executable decomposition.

Phase 18 gives it three lessons:

- use clustered inference whenever signals fire across assets at once;
- treat Hyperliquid cross-sectional results as HYPE-sensitive until shown otherwise;
- count a relative edge only when it survives both legs' costs.

No Phase 18 hypothesis earned a confirmatory follow-up.
