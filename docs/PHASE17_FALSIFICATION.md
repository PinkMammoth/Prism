# Phase 17: falsifying discretionary price-action claims

> **EXPLORATORY.** Historical intraday availability is reconstructed under an explicit
> latency assumption rather than observed in real time. The data is backfilled history,
> not untouched validation. Nothing in this document is validated, and no result feeds the
> forward tracker, the co-pilot or the paper account.
>
> **Prism uses "sweep" only as shorthand for an objectively defined failed-breakout event.**
> It implies no knowledge of stop locations, manipulation or institutional intent.

## Summary

Study `sstudy_908e4387cf3d73c636e651a834c94362bfbfa6b2f1b163ffe3105aa10a6b0d23`
(`phase17_structure_falsification_v1`) was frozen at 2026-10-05 11:50 UTC and first evaluated
at 11:50–11:54 UTC (`srun_b2a805a9…`). Its content was reproduced bit for bit on a scratch
copy, after a fix that moved leaked timing fields out of the digest (see
[Reproducibility](#reproducibility)). Before registration the pipeline ran only on synthetic
random walks. It tested 74 primary hypotheses (2 venues × 38),
72 secondary ones, 12 contrasts and 108 regime/volatility cells.

**After correction, no claim survives on either venue. Nothing is PROMISING or ROBUST.**

| question | answer |
|---|---|
| 1. Does breaching a level predict anything? | No. Breach excess is within ±21 bps on every level type and venue, and every q is ≥ 0.83. On 15m Binance (8,627 independent breaches) a 10 bps effect is excluded: **REJECTED**. |
| 2. Does failure ("sweep") add anything beyond the breach? | No. Failed and held breakouts are indistinguishable (contrast q ≥ 0.29 on Hyperliquid, ≥ 0.64 on Binance). Failed swing breakouts on Hyperliquid have no continuation edge (CI upper bound +9 bps: REJECTED). |
| 3. Does rejection add anything beyond failure? | No. Where it is significant, it points the wrong way: on Hyperliquid cluster sweeps, rejection goes with **worse** reversals (−101 vs +48 bps, contrast q = 0.031, n = 50), which is not reproduced on Binance (q = 0.96). Wick size, close location and reversal distance have Spearman ≈ 0 against outcomes (−0.22 … +0.03). |
| 4. Does structure confirmation pay after entry delay? | No. Chains that later shift look excellent measured from the sweep entry: **+105 to +243 bps**. That is a selection that is not executable. Entered when the shift is observable (5–8 h later, after a median **+75 to +229 bps** of adverse entry price), they return **−6 to −78 bps**. Confirmation selects the moves that already happened. |
| 5. Does waiting for a retest help? | It barely filters: 83–93% of shifts retest within 10 bars, with a median wait of 1 h. The entry price improves by about 5–13 bps (except Hyperliquid clusters, +11 bps worse on 10 events), and outcomes do not improve (E ≈ D). The few runaways that never retest are 2–32 independent events: too few to price the opportunity cost. |
| 6. Reversal or continuation? | Neither, at the primary horizon. Reversal and continuation excess are exact mirror images (same entry, opposite sign), so the pair is one two-sided question, and no member of it passes BH. On 15m Binance, failed swing breakouts *continue* by +5.8 bps (q = 0.090). That is statistically detectable but below the 10 bps floor and negative after costs: WEAK. |
| 7. Do equal-high/low clusters add anything? | No. 99.6–100% of cluster events are also swing events on the same bar. Cluster − prior-extreme excess differences have 95% CIs 90–140 bps wide that straddle zero. |
| 8. Regime dependent? | Not consistently. Hyperliquid shows reversal after breaches in low-vol / range regimes (A, B and H alike, so not specific to failure; best q = 0.045 for held breakouts in low vol). Binance shows the opposite for held breakouts in low vol (continuation, q = 0.049), and its with-trend sweep reversals are the worst cell (−95 bps, q = 0.065). |
| 9. Volatility dependent? | Same as 8. Effects that pass the subgroup family on one venue flip sign or vanish on the other. |
| 10. MFE/MAE asymmetry despite weak returns? | No. Median MFE ≈ median MAE on every rung (e.g. Hyperliquid swing B: 178 / 148 bps; Binance: 218 / 242 bps). MFE-before-MAE shares are 36–54% (Hyperliquid) and 45–53% (Binance). |
| 11. Are the R:R opportunities real? | No. On decided paths P(+2R before −1R) is 16–36% at A–C, around the 33% breakeven before costs. Net of costs, expectancy is negative at A, B, C and H for every target, using bounds that keep unresolved same-bar orderings honest. D/E stops sit 2–4% away, so 2R+ is rarely reached in a day: R ≈ 0 (−0.22 … +0.05 R). |
| 12. Do Hyperliquid and Binance agree? | Broadly yes, on "nothing": 26 of 38 primary pairs are SIMILAR (both null), 8 MIXED, 4 INSUFFICIENT, 0 OPPOSITE. Every apparent Hyperliquid effect is MIXED or absent on Binance. |

Verdict counts:

| | ROBUST | PROMISING | WEAK | REJECTED | NO EVIDENCE | INSUFFICIENT |
|---|---:|---:|---:|---:|---:|---:|
| primary, Hyperliquid (38) | 0 | 0 | 1 | 2 | 31 | 4 |
| primary, Binance (38) | 0 | 0 | 0 | 0 | 38 | 0 |
| secondary, Hyperliquid (36) | 0 | 0 | 1 | 4 | 27 | 4 |
| secondary, Binance (36) | 0 | 0 | 2 | 17 | 17 | 0 |


## Hypotheses

The study tries to falsify the discretionary claim behind a five-step ladder. Each step is
a restriction of the step before it, built from the frozen Phase 16 detectors
(`structure_primitives_v1`). Each step is entered **only once it is observable**:

| stage | definition (Phase 16 primitive) | known at |
|---|---|---|
| **A** breach | `level_breach_v1`: the first 1h bar whose high (low) exceeds an eligible 4h level by ≥ 0.1 × prior 1h ATR | the breach bar's `ready_at` (and never before the level was known) |
| **B** failed breakout ("sweep") | `breakout_outcome_v1` = FAILED: a 1h close back inside the level within 2 bars (WICK_ONLY breaches fail at once) | the failure bar |
| **C** + rejection | B with `rejection_v1`: a bar from the breach to the failure bar whose wick on the swept side is ≥ 50% of its range | the failure bar (same as B: the wick is decided by then) |
| **D** + structure shift | C followed by `structure_shift_v1`: a 1h close through the latest intact opposite 1h swing (2/2) known at the failure, within 20 bars, before any close beyond the swept extreme | the shift bar |
| **E** + retest | D followed by `retest_v1`: price returns to within 0.2 ATR of the broken shift level within 10 bars | the retest bar |
| **H** held breakout (comparator) | `breakout_outcome_v1` = HELD: the same breaches, every close beyond the level for 2 bars | the last bar of the window |
| **M** stretch control | a 4-bar close change ≥ 2 × prior ATR (no level, no failure, no structure) | that bar |

For every stage both directions are tested, and neither is assumed:

- **reversal**: short after a high-side event, long after a low-side one (the article's
  claim for B–E);
- **continuation**: with the breached side.

Each (level type, stage, direction) is one hypothesis: *the mean net excess forward return
at the primary horizon is greater than zero*.

## Preregistration

Frozen before any outcome was computed, by `market structure study register` (study
`sstudy_908e4387cf3d73c636e651a834c94362bfbfa6b2f1b163ffe3105aa10a6b0d23`):

- the manifest `config/structure/phase17_falsification.v1.yaml` (windows, architectures,
  horizons, central parameters, sensitivity axes, regime vocabulary, statistics, gates,
  families, verdict policy);
- costs from Prism's perp cost machinery (`perps.backtest.perp_costs`), frozen per venue
  and coin;
- one retained, content-hashed Lab dataset per venue and coin (`perp_intraday_bars` at 4h,
  1h and 15m plus `perp_funding`). The run reads **only** those snapshots, verified against
  their hashes, never the live tables;
- semantic versions (`structure_falsification_v1`, `structure_primitives_v1`,
  `trade_path_v1`).

The study ID is the content hash of all of that. Migration 19 adds append-only tables:
`lab_structure_studies`, `lab_structure_study_datasets`, `lab_structure_runs` and
`lab_structure_results`. A run row is committed **before** evaluation starts (the exposure
record). A result needs a run, and a run needs a frozen study (foreign keys). A second run
needs an explicit `rerun_of` and a reason. `evidence_class = 'EXPLORATORY'` is a CHECK
constraint, and `validated_reachable` is the literal `False`.

Why not a Lab `StrategyDefinition`/batch? A Lab strategy is daily, has one side and one
condition tree, and its plan preregisters only daily inputs (Phase 15). A structural chain
is a family of linked event hypotheses on intraday snapshots. Phase 17 follows the
Phase 9/11 pattern instead: its own registration tables, plus everything else reused.

| reused | from |
|---|---|
| retained datasets, hash verification | `capture_dataset`, `Ledger.register_dataset/read_dataset` (Phase 2) |
| software identity | `SoftwareIdentity`, `lab_software` |
| clock ordering | `ledger.ordered_now` (WSL clock-step guard) |
| BH correction | `batch.benjamini_hochberg` (Phase 5) |
| independent events | `backtest.events.decluster` (greedy, gap = horizon) |
| random-entry test | Phase 4's `random_entry_mean_excess`, matched more tightly (below) |
| parameter neighbourhood | `backtest.robustness.plateau_verdict` (frozen thresholds 0.5 / 0.5 / 60%) |
| costs | `perps.backtest.perp_costs` (`config/perps.yaml`) |
| detectors, entries, paths, ambiguity | Phase 16 `run_chain`, `trade_paths` (`trade_path_v1`) |

## Data

Inputs are a **scratch copy** of the local database plus a bounded public backfill (2026-10-05),
not the Railway production database. Hyperliquid retains only its newest 5,000 bars per
interval, so this is all the Hyperliquid 1h/15m history a backfill can recover. Every
window was fixed from coverage before any outcome was computed, and each series selects
`[start, end)` on close time.

| venue | 4h | 1h | 15m | funding | event window (primary) | event window (secondary) |
|---|---|---|---|---|---|---|
| Hyperliquid | 2025-12-01 → 2026-10-01 | 2026-03-15 → | 2026-08-15 → | 2026-03-15 → | **2026-04-01 → 2026-10-01** | 2026-08-22 → 2026-10-01 |
| Binance USD-M | 2024-06-01 → 2026-04-01 | 2024-09-01 → | 2024-10-01 → | 2024-09-01 → | **2024-10-01 → 2026-04-01** | 2024-10-08 → 2026-04-01 |

- **Hyperliquid:** the 1h retention starts 2026-03-11, so 1h events start after at least
  two weeks of 1h warmup. 4h levels and regimes use four months of warmup. 15m is used only
  for ambiguity resolution, and covers the last 47 days of the window.
- **Binance:** the window starts where Phase 15's governed Binance 15m history starts (so
  15m resolution exists throughout). It **ends where the Hyperliquid window begins**
  (Phase 11's rule), so no hour contributes outcomes to both venues. Binance asks whether
  the result reproduces on another venue *and an earlier regime*. Binance history has been
  used in earlier Prism research, so it is cross-venue exploratory evidence, **not
  independent validation**. Binance HYPE starts at its 2025-05-30 listing and has no
  retained funding.
- Both windows end on or before 2026-10-01: Phase 17 read no bar after it. That leaves
  2026-10-01 onwards untouched for any future intraday validation. It also respects the
  Phase 9 daily validation window.
- Coins: BTC, ETH, SOL, HYPE, LINK, AAVE. Assumed latency: **60 s** (part of every event ID).

Retained datasets (`perp_intraday_bars` 4h/1h/15m + `perp_funding`, content SHA-256):

| coin | Hyperliquid | Binance |
|---|---|---|
| BTC | `dataset_d1459d9665a6511e8c74182c2373d97159000967a6cc857765d621340d8dac0e` | `dataset_d69c081217dad6c4c5f124f62f3e355ef0df98297199e6771ae4d7af5623ccf3` |
| ETH | `dataset_ea27afe484dee32ffe9a46f999c8bd0eb2a80123932698c06fe37469e4c95a90` | `dataset_7666dee01ea6de5335be95811fd87df5f628127cd0f612bd48afb604434b0d6c` |
| SOL | `dataset_1028669bd2c8695b7533490d6871c9aa8228832eab6e7fb6be5a4ee8f6b0f465` | `dataset_039c7a1c69a635b994f928639d39532fc83889559d465550f39b7047b73eac12` |
| HYPE | `dataset_a80c05817bd9adae5b47e5d282881b4ab036091d2ca03dea3df0bd54180ca5d7` | `dataset_abd20c586f74a4715db6b2ef313b2f24815f549589c4b97d156106323ead4e86` |
| LINK | `dataset_3eac816855ff22e0610ba45178f1d6214c6efb6796bae78325430218f54274cb` | `dataset_044be45ed6771e5d454a016c5ae6132aee30b164b607bbc3523136cc9041f992` |
| AAVE | `dataset_144f190ac81eff0b56ede9073a8fec40eec221c8ee8fbbd6b2d2c818694b27e1` | `dataset_75905e6b72e2cca1ef978f66ba77aecb17425d28ede5a823eb5d98728672a742` |

Per coin: Hyperliquid 1,824 (4h) + 4,800 (1h) + 4,512 (15m) bars + 4,800 funding
settlements. Binance 4,013 + 13,847 + 52,511 bars + about 1,730 settlements (HYPE fewer).


## Architecture

**Primary: 4h structural levels → 1h events and confirmation; 15m only to resolve
same-bar ordering.** Why:

- Hyperliquid retains 5,000 bars per interval: 15m covers about 52 days, 1h about 208 days,
  4h about 833 days. Confirmation on 15m would have left most 1h sweeps `NO_DATA` (Phase 16
  smoke: 860 of 1,123).
- A 4h level referenced by 1h bars is the most common discretionary framing (HTF level, LTF
  trigger) that this coverage supports causally.

**Secondary (separate family, central parameters only): 1h levels → 15m events and
confirmation**, primary horizon 16 bars (4 h). It was frozen together with the primary
family and corrected separately. On Hyperliquid it has only about 40 days of events.

No other timeframe combination was run.

## Entry, returns, costs

- **Entry:** the open of the first event-timeframe bar opening at or after the stage's
  availability (`trade_path_v1`'s rule). Under the 60 s assumed latency, a 1h bar closing
  at T is available at T + 60 s. The entry is therefore the open of the bar that starts at
  T + 1h, one full bar after the event bar closed. This is conservative, and it applies
  equally to every stage. A stage never inherits an earlier stage's entry price.
- **Horizons:** 4, 24 and 96 bars (4 h, 1 day, 4 days). Primary: **24 bars = 1 day**.
  Exit at the close of the H-th bar, with the entry bar included.
- **Costs:** two sides of (taker fee + slippage) per venue and coin from
  `config/perps.yaml`. Hyperliquid fee is 4.5 bps; Binance fee is 5.0 bps. Slippage is
  BTC/ETH 2, SOL 4, HYPE/LINK 6 and AAVE 8 bps per side. **Funding** is the sum of
  settlements in (entry, exit], paid by longs when positive. It is reported beside the
  primary metric, because Binance HYPE has no retained funding history.
- **Baseline / excess:** every 1h bar opening in the event window is a baseline entry for
  both directions, with the same costs and horizon. The matched cell is
  (coin, direction, horizon, volatility tercile). Excess = event net return − its cell's
  mean net return. Regime subgroups use cells that also match the regime label.
- **Independent events:** greedy declustering per (coin, trade direction) on the entry bar,
  with gap = horizon bars, and at most one event per entry bar. Raw, evaluable and
  independent counts are all reported.

## Statistics, families and verdicts

### Parameter grids (exact)

Central chain (identical for both architectures) and the one-at-a-time sensitivity axes
(primary only). Each axis has three values with the centre in the middle, giving **17
chain variants per level type (51 per coin and venue)**, never a grid:

| dimension | central | neighbours |
|---|---|---|
| swing levels: pivot left = right (structure tf) | 2, max age 200 bars | 1, 3 |
| prior-extreme levels: n (structure bars) | 20 | 10, 50 |
| cluster levels: tolerance (ATR) | 0.2 (swings 2/2, age 200) | 0.1, 0.3 |
| breach overshoot (× prior event ATR; bps floor 0) | 0.1 | 0.0, 0.25 |
| failure window (event bars) | 2 | 0 (same bar), 5 |
| rejection: wick / range on the swept side | 0.5 (within the failure bar) | 0.33, 0.66 |
| structure shift: window (bars), close break | 20 | 10, 50 |
| structure shift: confirmation swing left = right | 2 | 1, 3 |
| retest tolerance (ATR) | 0.2 | 0.1, 0.3 |
| retest max delay (bars) | 10 | 5, 20 |
| R targets (descriptive) | +1, +2, +3, +5 R vs −1 R | – |
| thresholds (descriptive) | ±1 ATR, ±1 % | – |
| stretch control | 4-bar close change ≥ 2 ATR | – |

### Regime and volatility vocabulary (frozen, no classifier)

These are measured on the structure series and read at the entry instant from the newest
structure bar already available:

- trend direction: sign(close − EMA 50);
- **range**: efficiency ratio ER(20) < 1/√20, its expectation for a random walk (no more
  directional than noise); range takes precedence over the trend labels;
- **with_trend / counter_trend**: the trade direction equals / opposes the trend;
- **volatility tercile**: ATR(14)/close ranked causally within the trailing 30 days of
  structure bars: low [0, ⅓), mid [⅓, ⅔), high [⅔, 1].

### Tests

- **Primary metric:** mean **net** excess at the primary horizon on independent events,
  versus the matched baseline cell (coin, direction, horizon, volatility tercile). Costs are
  paid by both the event and the baseline, so the excess measures what the *timing*
  adds. Whether the trade itself pays is the separate `net mean > 0` gate.
- **Primary test** (`matched_random_entry_mean_excess_v1`, one-sided): the same per-cell
  counts are drawn without replacement from each cell's centred eligible pool, 2,000 times;
  p = (hits + 1) / 2,001. This is Phase 4's random-entry test, matched on volatility as well
  as coin and side, which answers "are sweeps merely volatility events?".
- **Contrasts** (two-sided label permutation, 4,000 times) on disjoint independent sets:
  failed vs held breakouts, and rejection vs no rejection within failed breakouts. The
  second is a same-entry comparison, because the wick is decided by the failure bar.
- **Subgroups** (two-sided): the same matched null restricted to baseline bars with the same
  regime label.
- **Intervals:** percentile bootstrap (2,000) of the mean excess, and of the difference of
  means for transitions.
- **Seeds:** per hypothesis, derived from 12345 and the hypothesis ID, so results never
  depend on run order.
- **Calibration** (synthetic, before the real run): consistent 15m→1h→4h random walks, 4
  seeds, 142 primary tests. 4.9% had p < 0.05 and 7.0% had p < 0.10, with roughly uniform
  deciles. BH let through one false discovery in the four seeds (q = 0.085).

### Correction families (BH, q = 0.10, Lab implementation)

A family never spans venues or architectures (venues are never pooled):

| family | members (preregistered m) | per |
|---|---|---|
| primary | 3 level types × (A–E + H) × 2 directions + stretch × 2 = **38** | venue, primary architecture |
| contrasts | 3 level types × {failed vs held, rejection vs none} = **6** | venue (primary) |
| subgroups | 3 level types × {A, B, H} × 6 cells (reversal) = **54** | venue (primary) |
| secondary | 3 × 6 × 2 = **36** | venue, secondary architecture |
| sensitivity | none: descriptive only, never tested | – |

Members failing a sample gate are excluded from m (independent filtering on counts only)
and stay visible as INSUFFICIENT. Reversal and continuation of one stage are exact mirror
images (identical entries, opposite sign), so the primary family effectively holds 19
two-sided questions at twice the cost. That is deliberate: the direction is not assumed.

### Gates and verdicts (`phase17_verdicts_v1`)

Sample gates: ≥ 30 independent events, ≥ 3 assets with events, ≥ 80% of in-window
events evaluable (complete path, defined volatility cell). Economic floor: **10 bps** of
net excess at the primary horizon.

| verdict | rule |
|---|---|
| ROBUST ENOUGH FOR NEXT RESEARCH STAGE | q ≤ 0.10, excess ≥ floor, net > 0, ≥ 60% of assets positive, sign survives dropping the largest asset, parameter PLATEAU, and the other venue agrees (same sign, raw p ≤ 0.05) |
| PROMISING — NEEDS VALIDATION | as above without the plateau or the cross-venue agreement |
| WEAK / EXPLORATORY | q ≤ 0.10 but a substantive gate fails, or raw p ≤ 0.05 with positive excess |
| REJECTED | the opposite direction is significant after correction, or the 95% upper bound of the excess is below the floor (an economically useful effect is excluded) |
| NO EVIDENCE | otherwise |
| INSUFFICIENT | a sample gate failed (rarity is itself a finding; gates are never relaxed) |


## Results

### Main ablation (primary horizon 24 h; reversal shown; continuation excess is its exact negative)

Columns:

- *net excess*: versus the matched random-entry cell, after costs;
- *net*: absolute return after costs;
- *hit*: net > 0;
- *MFE / MAE*: median magnitudes;
- *entry vs A*: median hours and bps (direction-signed: + is a worse price) between the
  breach entry and this stage's entry, for the same chains.

#### primary — hyperliquid (2026-04-01 → 2026-10-01); family m = 34 of 38

| level | stage | indep. | assets | rev. net excess (bps) | 95% CI | net (bps) | hit | MFE / MAE med. (bps) | entry vs A: h / bps | p (rev / cont) | q (rev / cont) | verdict rev / cont |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| swing | A breach | 728 | 6 | +13.4 | [-12, +39] | -8.8 | 51.9% | +174 / +157 | 0 / 0 | 0.152 / 0.853 | 0.833 / 0.966 | NO EVIDENCE / NO EVIDENCE |
| swing | B failed breakout | 586 | 6 | +20.3 | [-8, +48] | -2.1 | 51.5% | +178 / +148 | 0 / +0 | 0.066 / 0.925 | 0.753 / 0.972 | NO EVIDENCE / REJECTED |
| swing | C + rejection | 275 | 6 | -2.8 | [-42, +35] | -23.7 | 49.8% | +161 / +155 | 0 / +0 | 0.560 / 0.449 | 0.936 / 0.936 | NO EVIDENCE / NO EVIDENCE |
| swing | D + structure shift | 99 | 6 | -14.4 | [-69, +39] | -37.2 | 41.4% | +135 / +152 | 5 / +104 | 0.696 / 0.318 | 0.936 / 0.833 | NO EVIDENCE / NO EVIDENCE |
| swing | E + retest | 91 | 6 | -19.0 | [-79, +46] | -41.5 | 40.7% | +135 / +168 | 7 / +90 | 0.714 / 0.266 | 0.936 / 0.833 | NO EVIDENCE / NO EVIDENCE |
| swing | H held breakout | 329 | 6 | +22.5 | [-18, +61] | +1.5 | 56.8% | +175 / +171 | – | 0.119 / 0.888 | 0.833 / 0.972 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | A breach | 484 | 6 | +3.6 | [-31, +38] | -21.9 | 53.1% | +181 / +175 | 0 / 0 | 0.414 / 0.588 | 0.936 / 0.936 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | B failed breakout | 416 | 6 | +14.4 | [-24, +50] | -10.9 | 52.4% | +191 / +166 | 0 / +0 | 0.194 / 0.797 | 0.833 / 0.966 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | C + rejection | 223 | 6 | -18.2 | [-68, +26] | -43.0 | 51.6% | +164 / +182 | 0 / +0 | 0.794 / 0.223 | 0.966 / 0.833 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | D + structure shift | 81 | 6 | -17.8 | [-73, +42] | -45.1 | 43.2% | +183 / +165 | 6 / +149 | 0.702 / 0.310 | 0.936 / 0.833 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | E + retest | 76 | 6 | -7.9 | [-75, +56] | -32.8 | 42.1% | +164 / +179 | 8 / +107 | 0.599 / 0.408 | 0.936 / 0.936 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | H held breakout | 256 | 6 | +12.4 | [-36, +57] | -13.1 | 51.6% | +181 / +190 | – | 0.283 / 0.716 | 0.833 / 0.936 | NO EVIDENCE / NO EVIDENCE |
| cluster | A breach | 241 | 6 | +21.1 | [-22, +63] | +1.1 | 58.1% | +190 / +151 | 0 / 0 | 0.164 / 0.846 | 0.833 / 0.966 | NO EVIDENCE / NO EVIDENCE |
| cluster | B failed breakout | 158 | 6 | +0.9 | [-50, +49] | -19.5 | 55.1% | +178 / +149 | 0 / +0 | 0.492 / 0.517 | 0.936 / 0.936 | NO EVIDENCE / NO EVIDENCE |
| cluster | C + rejection | 54 | 6 | -81.1 | [-191, +10] | -107.0 | 46.3% | +161 / +163 | 0 / +0 | 0.972 / 0.032 | 0.972 / 0.753 | REJECTED / WEAK / EXPLORATORY |
| cluster | D + structure shift | 12 | 5 | -38.9 | [-219, +118] | -49.6 | 50.0% | +118 / +78 | 5 / +75 | – / – | – / – | INSUFFICIENT / INSUFFICIENT |
| cluster | E + retest | 10 | 5 | -90.9 | [-320, +82] | -99.2 | 60.0% | +133 / +94 | 6 / +87 | – / – | – / – | INSUFFICIENT / INSUFFICIENT |
| cluster | H held breakout | 103 | 6 | +55.3 | [-8, +119] | +38.1 | 65.0% | +224 / +144 | – | 0.053 / 0.953 | 0.753 / 0.972 | NO EVIDENCE / NO EVIDENCE |
| — | M stretch control | 874 | 6 | -6.0 | [-30, +17] | -25.8 | 47.8% | +173 / +193 | – | 0.707 / 0.316 | 0.936 / 0.833 | NO EVIDENCE / NO EVIDENCE |

#### primary — binance (2024-10-01 → 2026-04-01); family m = 38 of 38

| level | stage | indep. | assets | rev. net excess (bps) | 95% CI | net (bps) | hit | MFE / MAE med. (bps) | entry vs A: h / bps | p (rev / cont) | q (rev / cont) | verdict rev / cont |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| swing | A breach | 1930 | 6 | +2.4 | [-18, +20] | -16.4 | 48.4% | +220 / +235 | 0 / 0 | 0.409 / 0.591 | 0.889 / 0.889 | NO EVIDENCE / NO EVIDENCE |
| swing | B failed breakout | 1561 | 6 | -9.3 | [-31, +13] | -27.0 | 48.0% | +218 / +242 | 0 / +0 | 0.799 / 0.199 | 0.940 / 0.865 | NO EVIDENCE / NO EVIDENCE |
| swing | C + rejection | 752 | 6 | +1.2 | [-31, +35] | -16.4 | 49.1% | +217 / +240 | 0 / +0 | 0.475 / 0.523 | 0.889 / 0.889 | NO EVIDENCE / NO EVIDENCE |
| swing | D + structure shift | 243 | 6 | -30.6 | [-83, +27] | -47.3 | 49.4% | +213 / +238 | 8 / +210 | 0.867 / 0.128 | 0.940 / 0.865 | NO EVIDENCE / NO EVIDENCE |
| swing | E + retest | 211 | 6 | -33.1 | [-90, +27] | -50.4 | 51.2% | +219 / +233 | 9 / +184 | 0.876 / 0.126 | 0.940 / 0.865 | NO EVIDENCE / NO EVIDENCE |
| swing | H held breakout | 872 | 6 | -6.5 | [-36, +23] | -26.0 | 48.2% | +212 / +250 | – | 0.679 / 0.323 | 0.889 / 0.889 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | A breach | 1401 | 6 | +9.4 | [-14, +33] | -8.9 | 50.6% | +238 / +241 | 0 / 0 | 0.205 / 0.800 | 0.865 / 0.940 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | B failed breakout | 1230 | 6 | +12.5 | [-14, +38] | -4.7 | 54.1% | +254 / +235 | 0 / +0 | 0.149 / 0.845 | 0.865 / 0.940 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | C + rejection | 686 | 6 | +14.0 | [-21, +46] | -3.1 | 52.8% | +254 / +244 | 0 / +0 | 0.203 / 0.820 | 0.865 / 0.940 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | D + structure shift | 254 | 6 | -6.3 | [-61, +45] | -23.3 | 53.1% | +230 / +228 | 8 / +229 | 0.595 / 0.410 | 0.889 / 0.889 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | E + retest | 227 | 6 | +7.6 | [-50, +69] | -10.1 | 52.4% | +250 / +233 | 9 / +205 | 0.377 / 0.616 | 0.889 / 0.889 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | H held breakout | 695 | 6 | -23.2 | [-60, +13] | -41.1 | 45.2% | +217 / +282 | – | 0.918 / 0.089 | 0.940 / 0.865 | NO EVIDENCE / NO EVIDENCE |
| cluster | A breach | 656 | 6 | -7.7 | [-43, +28] | -27.0 | 48.3% | +231 / +237 | 0 / 0 | 0.671 / 0.311 | 0.889 / 0.889 | NO EVIDENCE / NO EVIDENCE |
| cluster | B failed breakout | 453 | 6 | -4.7 | [-47, +34] | -22.3 | 50.6% | +241 / +229 | 0 / +0 | 0.588 / 0.415 | 0.889 / 0.889 | NO EVIDENCE / NO EVIDENCE |
| cluster | C + rejection | 196 | 6 | -0.2 | [-65, +64] | -18.5 | 52.0% | +256 / +222 | 0 / +0 | 0.516 / 0.478 | 0.889 / 0.889 | NO EVIDENCE / NO EVIDENCE |
| cluster | D + structure shift | 68 | 6 | -77.6 | [-190, +31] | -94.3 | 48.5% | +195 / +258 | 7 / +204 | 0.940 / 0.066 | 0.940 / 0.865 | NO EVIDENCE / NO EVIDENCE |
| cluster | E + retest | 61 | 6 | -87.4 | [-216, +33] | -108.2 | 52.5% | +212 / +257 | 10 / +183 | 0.939 / 0.059 | 0.940 / 0.865 | NO EVIDENCE / NO EVIDENCE |
| cluster | H held breakout | 258 | 6 | -8.5 | [-69, +48] | -29.6 | 50.4% | +213 / +248 | – | 0.634 / 0.363 | 0.889 / 0.889 | NO EVIDENCE / NO EVIDENCE |
| — | M stretch control | 2261 | 6 | -3.9 | [-23, +15] | -23.2 | 47.1% | +224 / +250 | – | 0.671 / 0.330 | 0.889 / 0.889 | NO EVIDENCE / NO EVIDENCE |

A, B and C share the breach entry for most chains, because 65–74% of failures are
same-bar (WICK_ONLY) failures. D and E enter 5–10 h later.

### Reversal versus continuation

At the primary horizon no direction contains information. The largest Hyperliquid
reversal effects (swing B +20 bps, p = 0.066; cluster H +55 bps, p = 0.053) do not survive
correction and are not reproduced on Binance (−9 and −9 bps). Continuation after a failed
swing breakout is REJECTED on Hyperliquid (CI upper bound +9 bps). The only correction-surviving
direction anywhere is 15m Binance continuation after failed swing breakouts (+5.8 bps,
q = 0.090). That effect is economically below the floor and negative after costs: WEAK.
At the 96 h horizon (descriptive, untested), Binance prior-extreme and cluster B-stage reversals
show +56 and +77 bps, while Hyperliquid shows −31 and −8 bps. This is a secondary metric and is
not promoted.

### Controls

- **Held breakout (H)** is the first-class comparator. Failed vs held contrasts:
  Hyperliquid p = 0.93 / 0.95 / 0.19 (swing / prior / cluster); Binance p = 0.89 / 0.11 /
  0.91. "Failure" carries no information beyond reaching the level.
- **Prior N-bar extreme** is the plain Donchian breach and the plain failed breakout,
  without the swing or cluster narrative. It performs no differently from swings or clusters.
- **Stretch control (M)**, no level at all: −6.0 bps (Hyperliquid) and −3.9 bps (Binance),
  indistinguishable from the level-based stages.
- **Matched random entry** is the null of every primary test.

### Entry delay: how much edge is consumed while waiting

| | | Hyperliquid | | | Binance | | |
|---|---|---:|---:|---:|---:|---:|---:|
| level | stage | from B entry\* | own entry | Δ entry price vs C (bps) | from B entry\* | own entry | Δ entry price vs C (bps) |
| swing | D + shift | +140 | −14 | +98 | +212 | −31 | +202 |
| swing | E + retest | +120 | −19 | | +187 | −33 | |
| prior_extreme | D + shift | +173 | −18 | +125 | +243 | −6 | +209 |
| prior_extreme | E + retest | +151 | −8 | | +221 | +8 | |
| cluster | D + shift | +105 (n = 12) | −39 | +75 | +188 | −78 | +199 |
| cluster | E + retest | +42 (n = 10) | −91 | | +146 | −87 | |

\*Conditional, **not executable**: the excess of these chains measured from the sweep's
own entry. Choosing them used information (that a shift would follow) that did not
exist at that entry. Executable research uses only the own-entry column.

This is the central result. Structure confirmation identifies chains whose move from the
sweep was excellent (+1–2.5% of excess). By the time the shift closes and can be acted on
(median 5–8 h after the breach entry, after a median 75–229 bps of price travelled in the
trade's direction), the remaining forward return is at or below a random entry. The
apparent value of "wait for confirmation" is a selection made in hindsight.

Transition summary (reversal; Δ excess with 95% bootstrap CI; populations overlap, so the
CIs are approximate):

| level | step | Hyperliquid indep. | Δ excess (bps) | Binance indep. | Δ excess (bps) |
|---|---|---|---:|---|---:|
| swing | A → B | 728 → 586 | +7 [−33, +46] | 1930 → 1561 | −12 [−41, +16] |
| swing | B → C | 586 → 275 | −23 [−71, +25] | 1561 → 752 | +10 [−27, +49] |
| swing | C → D | 275 → 99 | −12 [−81, +56] | 752 → 243 | −32 [−96, +33] |
| swing | D → E | 99 → 91 | −5 [−90, +79] | 243 → 211 | −2 [−83, +81] |
| prior_extreme | A → B | 484 → 416 | +11 [−41, +59] | 1401 → 1230 | +3 [−32, +39] |
| prior_extreme | B → C | 416 → 223 | −33 [−94, +28] | 1230 → 686 | +1 [−40, +42] |
| prior_extreme | C → D | 223 → 81 | +0 [−75, +75] | 686 → 254 | −20 [−84, +43] |
| prior_extreme | D → E | 81 → 76 | +10 [−80, +97] | 254 → 227 | +14 [−65, +90] |
| cluster | A → B | 241 → 158 | −20 [−88, +43] | 656 → 453 | +3 [−50, +55] |
| cluster | B → C | 158 → 54 | −82 [−202, +24] | 453 → 196 | +5 [−74, +82] |
| cluster | C → D | 54 → 12 | +42 [−194, +232] | 196 → 68 | −77 [−209, +53] |

Each layer keeps 20–69% of the previous stage's events (a retest keeps 83–93%). No layer
improves the conditional distribution enough to compensate for the lost sample or the
later entry.

### Rejection

- **Thresholded** (wick ≥ 50% of the range, decided by the failure bar, so C has the same
  entry as B), rejection vs no rejection: Hyperliquid swing −8 vs +37 bps (p = 0.12), prior
  −18 vs +37 (p = 0.15), **cluster −101 vs +48 (p = 0.0052, q = 0.031)**. Binance: −8 vs −10,
  +12 vs +13, +0 vs −8 (all p ≥ 0.85).
- The single significant contrast says rejection is *worse*, the opposite of the claim, on
  50 events, and it does not reproduce. Treat it as noise.
- **Continuous metrics** among failed breakouts (Spearman with reversal excess): wick/range
  −0.17 … +0.02, close location −0.07 … +0.03, reversal distance in ATR −0.22 … −0.01. No
  monotone quintile pattern on either venue.
- **Neighbourhood:** wick thresholds 0.33 / 0.66 behave like 0.5 (see Sensitivity).
- Rejection reduces the sample by about 60% and adds nothing.

### Structure shift

| | Hyperliquid swing / prior / cluster | Binance swing / prior / cluster |
|---|---|---|
| C chains | 352 / 293 / 59 | 1032 / 953 / 213 |
| SHIFT / INVALIDATED / EXPIRED | 113/183/56 · 89/156/48 · 12/36/11 | 319/525/185 · 279/495/178 · 73/109/31 |
| retention (shift / C) | 32% / 30% / 20% | 31% / 29% / 34% |
| median confirmation delay after C entry | 5 h / 6 h / 3.5 h | 7 h / 7 h / 7 h |
| median entry-price degradation vs C | +98 / +125 / +75 bps | +202 / +209 / +199 bps |
| conditional excess (from B entry) | +140 / +173 / +105 | +212 / +243 / +188 |
| post-entry (executable) excess | −14 / −18 / −39 | −31 / −6 / −78 |

About half of the rejected sweeps are **invalidated** (a close beyond the swept extreme)
before any shift. The objective stop at D is 2–4% away (about 3.5 ATR), versus about 1% at B.

### Retest

| | Hyperliquid swing / prior / cluster | Binance swing / prior / cluster |
|---|---|---|
| shifts that retest within 10 bars | 91% / 93% / 83% | 87% / 89% / 89% |
| median wait | 1 h | 1 h |
| entry change E vs D (median, + = worse) | −5 / −10 / +11 bps | −10 / −13 / −10 bps |
| excess at E entry (retested) | −19 / −8 / −91 | −33 / +8 / −87 |
| never retested: independent n, excess at D entry | 8: +101 · 5: +13 · 2: +140 | 32: +44 · 29: +62 · 7: +158 |

The retest is almost automatic, because a close just through a 1h swing usually comes back
within 0.2 ATR the next hour. It buys about 10 bps of entry price and no better outcome. The
moves it misses (the runaways) look better, but they are too few to measure (2–32
independent events).

### Level types

| | Hyperliquid | Binance |
|---|---|---|
| cluster events that are also swing events (same bar, side), A / B | 100% / 100% | 99.9% / 99.6% |
| cluster events that are also prior-extreme events, A / B | 66% / 55% | 69% / 60% |
| B reversal excess: cluster − prior (95% CI) | −13 [−76, +47] | −17 [−63, +30] |
| B reversal excess: swing − prior (95% CI) | +6 [−41, +51] | −22 [−57, +11] |

The "liquidity cluster" is a subset of swing breaches, about a fifth to a quarter of them, with no
measurable added information. Equal highs/lows perform no better than ordinary prior
highs/lows.

### Regime and volatility (predefined subgroups, reversal; q within each venue's family)

| venue | cells with q ≤ 0.10 | |
|---|---|---|
| Hyperliquid (m = 45) | swing H held, vol_low: +82 bps (n = 149, q = 0.045); swing A range +42 (q = 0.090); prior A vol_low +61 (q = 0.090); prior B vol_low +58 (q = 0.090) | reversal in quiet regimes, for breaches **and** held breakouts alike |
| Binance (m = 49) | prior H held, vol_low: **−80** (n = 270, q = 0.049); swing B with_trend **−95** (n = 149, q = 0.065); prior A vol_mid +54 (q = 0.065) | quiet-regime held breakouts *continue* |

- **Failed breakouts as mean-reversion events in ranges?** On Hyperliquid, reversal is better
  in ranges and in low volatility, but equally so for breaches that did *not* fail. On
  Binance it is not.
- **Trend-aligned sweeps work better?** No. With-trend sweep reversals are the worst cell on
  both venues: −53 (Hyperliquid) and −95 bps (Binance) for swings, −106 and −132 for prior
  extremes (n < 30).
- **Countertrend reversals worse?** No: counter_trend is about flat (+19 / −5 swing).
- **Held breakouts dominate in strong trends?** Held-breakout continuation is not
  significant in any trend cell. The two venues disagree in sign in low volatility.

The regime picture is venue-specific and does not survive cross-venue comparison. It is
recorded as exploratory subgroup noise, not a conditional edge.

### MFE / MAE

Median MFE and MAE are close to equal on every stage, for example (24 h, reversal):

- Hyperliquid swing B: 178 / 148 bps;
- Binance swing B: 218 / 242 bps;
- Hyperliquid D: 135 / 152 bps.

The ±1 ATR favourable-first share of decided paths is 40–67% on Hyperliquid (the extremes
are cluster D/E cells with ≤ 12 events) and 41–55% on Binance. The MFE-before-MAE share is 36–54%
and 45–53%. Fixed-horizon
returns are not hiding a favourable path structure.

### R paths (reversal; +kR before −1R within 24 h; net of round-trip costs in R)

| venue | level / stage | events | P(+1R first) bounds | P(+2R first) bounds | net R / trade at 2R (bounds) | OHLC-ambiguous at 1R → resolved by 15m |
|---|---|---:|---:|---:|---:|---:|
| Hyperliquid | swing A | 561 | 0.47–0.58 | 0.27–0.35 | −0.90 … −0.68 | 72 → 7 |
| Hyperliquid | swing B | 511 | 0.48–0.56 | 0.25–0.29 | −0.58 … −0.44 | 44 → 3 |
| Hyperliquid | swing D | 99 | 0.30 | 0.08 | −0.16 | 0 |
| Hyperliquid | prior B | 373 | 0.50–0.55 | 0.27–0.29 | −0.36 … −0.29 | 21 → 3 |
| Binance | swing A | 1483 | 0.46–0.52 | 0.29–0.34 | −0.67 … −0.53 | 202 → 105 |
| Binance | swing B | 1385 | 0.46–0.49 | 0.28–0.30 | −0.36 … −0.29 | 102 → 56 |
| Binance | prior B | 1091 | 0.48–0.52 | 0.29–0.32 | −0.33 … −0.25 | 83 → 40 |
| Binance | prior D | 252 | 0.29 | 0.09 | +0.02 | 0 |

- **Bounds:** lower = every unresolved same-bar ordering counted as a stop; upper = counted as
  the target. Nothing is assigned by preference.
- **15m resolution:** Hyperliquid's 15m covers only the last 46 days of its window, so most
  ambiguity stays unresolved there (raw ambiguous 6–13% of events at A/B, 7–15% of those
  resolved). On Binance about half is resolved.
- **Invalid at entry:** 22–24% of A events, 7–17% of H, 10–14% of B and 7–11% of C have
  `INVALID_AT_ENTRY` (the next open was already beyond the objective invalidation). They are excluded
  from R and reported.
- **Mean net R** is pulled down by events whose objective stop sits a few bps from the entry:
  costs alone exceed 1R for them. Even before costs, P(+2R first) on decided paths
  (16–36%) sits around the 33% breakeven.
- The only non-negative net-R cells are D/E (stops about 3.5 ATR away, few targets reached,
  "neither" dominates). There R ≈ 0.

### Sensitivity (one-at-a-time neighbours, descriptive)

- Plateau verdicts (Prism `plateau_verdict`) across (level, stage, direction):
  - Hyperliquid: 16 NO_EDGE, 11 PLATEAU, 4 MIXED, 4 INSUFFICIENT, 1 FRAGILE;
  - Binance: 18 NO_EDGE, 11 PLATEAU, 4 FRAGILE, 3 MIXED.
- Every PLATEAU is a plateau of a non-significant effect. Example: Hyperliquid swing B
  reversal, where all 6 relevant neighbours keep the sign, with excess from +3 to +24 bps. The
  neighbourhood is broad and flat, so no threshold is a hidden "magic number" that the
  centre missed.
- No neighbour was tested, selected or promoted.

### Multiple testing

| family | Hyperliquid: m, min raw p, min q, q ≤ 0.10 | Binance: m, min raw p, min q, q ≤ 0.10 |
|---|---|---|
| primary | 34, 0.032, 0.753, 0 | 38, 0.060, 0.865, 0 |
| contrasts | 6, 0.0052, **0.031**, 1 (cluster rejection vs none, wrong sign) | 6, 0.107, 0.645, 0 |
| subgroups | 45, 0.0010, **0.045**, 4 | 49, 0.0010, **0.049**, 3 |
| secondary | 32, 0.019, 0.608, 0 | 36, 0.0025, **0.090**, 1 (swing B continuation, +5.8 bps: WEAK) |

Every raw p and q is in the stored result (`market structure study report <run> --json`).

### Cross-venue (never pooled)

Primary pairs:

- **26 SIMILAR:** almost all "both null";
- **8 MIXED:** swing B, prior-extreme H, cluster C and cluster H, each in both directions.
  Hyperliquid's largest point estimates are absent on Binance;
- **4 INSUFFICIENT:** cluster D/E on Hyperliquid;
- **0 OPPOSITE.**

Secondary: 24 SIMILAR, 6 MIXED, 4 INSUFFICIENT, 2 OPPOSITE: the swing breach, in both directions.
Hyperliquid shows +10 bps reversal (raw p = 0.019) and Binance +3 bps continuation (raw p = 0.041). The two venues tell the same story: there is no stable edge.

### Secondary architecture (1h levels → 15m events; primary horizon 16 bars = 4 h)

#### secondary — hyperliquid (2026-08-22 → 2026-10-01); family m = 32 of 36

| level | stage | indep. | assets | rev. net excess (bps) | 95% CI | net (bps) | hit | MFE / MAE med. (bps) | entry vs A: h / bps | p (rev / cont) | q (rev / cont) | verdict rev / cont |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| swing | A breach | 737 | 6 | +10.2 | [-1, +20] | -8.5 | 47.8% | +83 / +71 | 0 / 0 | 0.019 / 0.977 | 0.608 / 0.977 | WEAK / EXPLORATORY / REJECTED |
| swing | B failed breakout | 590 | 6 | +5.9 | [-7, +18] | -12.3 | 46.4% | +84 / +70 | 0 / +0 | 0.141 / 0.849 | 0.875 / 0.876 | NO EVIDENCE / REJECTED |
| swing | C + rejection | 266 | 6 | +3.1 | [-17, +22] | -14.2 | 44.4% | +84 / +68 | 0 / +0 | 0.355 / 0.643 | 0.875 / 0.876 | NO EVIDENCE / NO EVIDENCE |
| swing | D + structure shift | 82 | 6 | +8.6 | [-23, +41] | -9.2 | 43.9% | +80 / +63 | 2 / +83 | 0.279 / 0.728 | 0.875 / 0.876 | NO EVIDENCE / NO EVIDENCE |
| swing | E + retest | 73 | 6 | +9.8 | [-25, +44] | -7.4 | 41.1% | +80 / +54 | 2 / +66 | 0.260 / 0.721 | 0.875 / 0.876 | NO EVIDENCE / NO EVIDENCE |
| swing | H held breakout | 276 | 6 | +4.9 | [-9, +19] | -13.7 | 43.8% | +77 / +78 | – | 0.275 / 0.720 | 0.875 / 0.876 | NO EVIDENCE / REJECTED |
| prior_extreme | A breach | 509 | 6 | -1.0 | [-13, +12] | -20.5 | 44.8% | +85 / +86 | 0 / 0 | 0.573 / 0.436 | 0.876 / 0.876 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | B failed breakout | 439 | 6 | +0.9 | [-14, +16] | -18.0 | 45.3% | +84 / +82 | 0 / +0 | 0.446 / 0.568 | 0.876 / 0.876 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | C + rejection | 214 | 6 | +6.8 | [-12, +28] | -10.8 | 43.0% | +79 / +81 | 0 / +0 | 0.252 / 0.773 | 0.875 / 0.876 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | D + structure shift | 60 | 6 | -17.8 | [-45, +8] | -36.2 | 36.7% | +61 / +88 | 2 / +108 | 0.832 / 0.169 | 0.876 / 0.875 | REJECTED / NO EVIDENCE |
| prior_extreme | E + retest | 52 | 6 | -15.6 | [-48, +17] | -33.3 | 36.5% | +66 / +75 | 2 / +72 | 0.805 / 0.192 | 0.876 / 0.875 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | H held breakout | 233 | 6 | -4.6 | [-26, +16] | -25.4 | 43.3% | +82 / +94 | – | 0.711 / 0.327 | 0.876 / 0.875 | NO EVIDENCE / NO EVIDENCE |
| cluster | A breach | 225 | 6 | -1.9 | [-20, +17] | -20.4 | 48.4% | +79 / +82 | 0 / 0 | 0.604 / 0.405 | 0.876 / 0.876 | NO EVIDENCE / NO EVIDENCE |
| cluster | B failed breakout | 152 | 6 | -11.2 | [-34, +13] | -29.4 | 44.7% | +85 / +75 | 0 / +0 | 0.849 / 0.160 | 0.876 / 0.875 | NO EVIDENCE / NO EVIDENCE |
| cluster | C + rejection | 56 | 6 | -7.9 | [-44, +27] | -25.7 | 44.6% | +88 / +73 | 0 / +0 | 0.677 / 0.339 | 0.876 / 0.875 | NO EVIDENCE / NO EVIDENCE |
| cluster | D + structure shift | 14 | 5 | +1.7 | [-47, +45] | -18.8 | 42.9% | +83 / +36 | 2 / +75 | – / – | – / – | INSUFFICIENT / INSUFFICIENT |
| cluster | E + retest | 11 | 5 | -29.7 | [-79, +8] | -47.9 | 18.2% | +66 / +52 | 2 / +53 | – / – | – / – | INSUFFICIENT / INSUFFICIENT |
| cluster | H held breakout | 94 | 6 | +9.3 | [-17, +37] | -8.1 | 46.8% | +85 / +82 | – | 0.241 / 0.747 | 0.875 / 0.876 | NO EVIDENCE / NO EVIDENCE |

#### secondary — binance (2024-10-08 → 2026-04-01); family m = 36 of 36

| level | stage | indep. | assets | rev. net excess (bps) | 95% CI | net (bps) | hit | MFE / MAE med. (bps) | entry vs A: h / bps | p (rev / cont) | q (rev / cont) | verdict rev / cont |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| swing | A breach | 8627 | 6 | -3.0 | [-7, +1] | -22.0 | 44.7% | +88 / +93 | 0 / 0 | 0.949 / 0.041 | 0.997 / 0.492 | REJECTED / REJECTED |
| swing | B failed breakout | 7031 | 6 | -5.8 | [-10, -1] | -24.9 | 44.2% | +87 / +93 | 0 / +0 | 0.997 / 0.002 | 0.997 / 0.090 | REJECTED / WEAK / EXPLORATORY |
| swing | C + rejection | 3058 | 6 | -6.3 | [-13, -0] | -25.0 | 43.0% | +85 / +90 | 0 / +0 | 0.976 / 0.019 | 0.997 / 0.342 | REJECTED / WEAK / EXPLORATORY |
| swing | D + structure shift | 902 | 6 | +1.2 | [-11, +12] | -17.6 | 44.8% | +88 / +79 | 2 / +100 | 0.420 / 0.579 | 0.940 / 0.944 | NO EVIDENCE / NO EVIDENCE |
| swing | E + retest | 821 | 6 | +0.9 | [-11, +12] | -18.0 | 45.2% | +83 / +77 | 2 / +88 | 0.451 / 0.545 | 0.940 / 0.940 | NO EVIDENCE / NO EVIDENCE |
| swing | H held breakout | 3383 | 6 | -2.5 | [-9, +3] | -21.3 | 44.3% | +90 / +90 | – | 0.800 / 0.216 | 0.990 / 0.870 | REJECTED / REJECTED |
| prior_extreme | A breach | 6447 | 6 | -1.4 | [-6, +4] | -20.5 | 46.4% | +94 / +97 | 0 / 0 | 0.742 / 0.266 | 0.990 / 0.870 | REJECTED / REJECTED |
| prior_extreme | B failed breakout | 5503 | 6 | -3.8 | [-9, +1] | -23.0 | 46.2% | +94 / +96 | 0 / +0 | 0.949 / 0.060 | 0.997 / 0.540 | REJECTED / REJECTED |
| prior_extreme | C + rejection | 2780 | 6 | -4.1 | [-11, +3] | -23.0 | 45.5% | +92 / +95 | 0 / +0 | 0.880 / 0.119 | 0.990 / 0.860 | REJECTED / NO EVIDENCE |
| prior_extreme | D + structure shift | 901 | 6 | +1.7 | [-11, +14] | -17.4 | 44.7% | +90 / +89 | 2 / +109 | 0.385 / 0.603 | 0.940 / 0.944 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | E + retest | 828 | 6 | +0.6 | [-13, +13] | -18.6 | 44.7% | +85 / +90 | 2 / +93 | 0.476 / 0.548 | 0.940 / 0.940 | NO EVIDENCE / NO EVIDENCE |
| prior_extreme | H held breakout | 2850 | 6 | -3.3 | [-10, +4] | -22.4 | 46.1% | +95 / +96 | – | 0.856 / 0.163 | 0.990 / 0.870 | REJECTED / NO EVIDENCE |
| cluster | A breach | 2623 | 6 | -0.2 | [-8, +7] | -19.4 | 44.9% | +89 / +90 | 0 / 0 | 0.513 / 0.474 | 0.940 / 0.940 | REJECTED / REJECTED |
| cluster | B failed breakout | 1851 | 6 | +1.9 | [-7, +11] | -17.5 | 44.4% | +87 / +87 | 0 / +0 | 0.331 / 0.675 | 0.916 / 0.990 | NO EVIDENCE / REJECTED |
| cluster | C + rejection | 749 | 6 | +5.7 | [-7, +20] | -13.2 | 43.5% | +84 / +83 | 0 / +0 | 0.177 / 0.826 | 0.870 / 0.990 | NO EVIDENCE / REJECTED |
| cluster | D + structure shift | 215 | 6 | +5.4 | [-17, +27] | -13.3 | 45.6% | +84 / +71 | 2 / +96 | 0.317 / 0.689 | 0.916 / 0.990 | NO EVIDENCE / NO EVIDENCE |
| cluster | E + retest | 202 | 6 | +7.7 | [-16, +31] | -11.3 | 44.6% | +81 / +74 | 2 / +93 | 0.259 / 0.731 | 0.870 / 0.990 | NO EVIDENCE / NO EVIDENCE |
| cluster | H held breakout | 932 | 6 | -4.2 | [-15, +6] | -23.0 | 42.9% | +85 / +91 | – | 0.771 / 0.232 | 0.990 / 0.870 | REJECTED / NO EVIDENCE |

On 15m Binance the samples are large (A: 8,627 independent swing breaches), which makes the
nulls tight. 17 of 36 hypotheses are REJECTED because their 95% upper bound is below 10 bps.
The same conditional-versus-executable gap appears: D chains have +103 to +120 bps from the
sweep entry and −18 to +9 bps from their own entry.

## Verdicts

- **REJECTED** (an economically useful effect is excluded, or the opposite direction is
  significant):
  - continuation after failed swing breakouts and reversal after rejected cluster sweeps
    (Hyperliquid, 1h);
  - on 15m Binance, 17 hypotheses: both directions of plain breaches on every level type,
    and most failed-breakout, rejection and held-breakout stages;
  - on 15m Hyperliquid, swing breach/failure/held continuation and prior-extreme shift
    reversal (4).
- **NO EVIDENCE:** every other primary hypothesis on both venues, including the full
  A → E reversal ladder on every level type.
- **WEAK / EXPLORATORY:**
  - Hyperliquid cluster rejection continuation (raw p = 0.032, the mirror of a REJECTED
    reversal, n = 54);
  - 15m Binance failed-swing continuation (q = 0.090, +5.8 bps, net negative);
  - 15m Binance swing rejection continuation (raw p = 0.019);
  - 15m Hyperliquid swing breach reversal (raw p = 0.019).
- **INSUFFICIENT:** cluster D/E (structure-shift and retest stages of equal-high/low sweeps)
  on both Hyperliquid architectures. Too rare to test in six months.
- **PROMISING / ROBUST: none.**

Nothing survives as a candidate for canonical strategy-family status or prospective tracking.

## Reproducibility

| run | attempt | software | digest |
|---|---|---|---|
| `srun_b2a805a9b534402bb9b10c4d2a4f3b11` (governed) | 1 | `dd23a8f` (`software_e7903507…`) | `e5bccfcd…` |
| `srun_afa9342b84aa41f68a6cb8f45bf9b3c1` (scratch copy) | 2 | `dd23a8f` | `3ee986e9…`: **differed** |
| `srun_09e8ac22a8e7461790e36b1684cbfb0f` (governed, after fix) | 2 | `b060f82` (`software_311f5cac…`) | `4ac47837ff32eaaf027501bfe37d9c79ea7708e5760c5874898685f4c7cc0255` |
| `srun_d981fc1b52a8421ca471894cc473f66b` (fresh scratch copy) | 3 | `b060f82` | `4ac47837…` (identical) |

The first reproducibility rerun did not reproduce the digest. A diff showed exactly 24
differing fields, all per-coin wall-clock `seconds_per_coin` timings that had leaked into
the result payload. Every scientific field was identical: with those fields removed, both
payloads hash to `4ac47837…`. Commit `b060f82` moved timings to the run metadata and added
a determinism test. Both later runs reproduce `4ac47837…` exactly, which is the digest of
attempt 1's content. All four runs are kept, append-only, with explicit `rerun_of` links
and reasons. Every number in this document is identical in all four.

## Performance

| | primary HL | primary BN | secondary HL | secondary BN |
|---|---:|---:|---:|---:|
| source bars read (4h + 1h + 15m) | 66,816 | 390,354 | 66,816 | 390,354 |
| event-timeframe bars in window | 26,346 | 72,968 | 23,034 | 288,528 |
| unique stage events (central) | 8,268 | 23,651 | 6,181 | 77,049 |
| independent (stage × direction) | 10,192 | 28,030 | 8,166 | 99,406 |
| chain variants per coin | 51 | 51 | 3 | 3 |
| seconds | 67 | 88 | 18 | 36 |

Whole run: **209 s wall, 2.4 GB peak RSS** (WSL2, Python 3.12, one core). Registration,
which captures and hashes 12 datasets (about 0.5 M rows), took 21 s and 0.44 GB. Most of the
time goes to the 51 one-at-a-time chain variants per coin and to the Monte Carlo nulls
(2,000 draws per test). This is practical for future studies of similar size.

## Live compatibility

- No forward-tracking, co-pilot, paper-trading or runtime code was changed, and none of it
  imports `research.structure` or `structure_study` (tests
  `test_live_consumers_never_use_structural_primitives` and
  `test_no_live_consumer_reads_phase17`). No job was added to the runtime schedule.
- Migration 19 is additive: four new `lab_structure_*` tables, no ALTER or DROP. The
  authoritative runtime will apply it on its next writable open. The tables stay empty
  there unless a study is registered.
- The study ran on a scratch copy (`PRISM_RUNTIME_ROLE=scratch`). `data/prism.duckdb` and
  the Railway database were not written.
- No policy, strategy family, evidence profile, paper rule or co-pilot rule changed.
  Nothing was enrolled for prospective tracking.

## Limitations

- **Historical intraday availability is reconstructed under an explicit latency assumption
  (60 s) rather than observed in real time.** The bars are backfilled. Their real
  publication times are unknown, and revisions before Phase 15's live collection are not
  represented.
- **Entry lag:** the conservative rule (the open of the first bar after availability) enters
  one full event bar after the event bar closes. A trader acting at the close would enter
  about one bar earlier. That lag affects every stage equally, so the ablation is fair, but
  absolute returns of very short-lived effects could be understated. The 4 h horizon (in
  the stored horizon profile) does not change the picture.
- **OHLC ambiguity** is irreducible without finer bars. Hyperliquid 15m covers only the last
  46 days of its window, so most Hyperliquid same-bar orderings remain unresolved
  (reported as bounds).
- **Short Hyperliquid history:** about 6 months of 1h events in one market regime. Binance is
  longer but earlier, and is a different venue. Neither is untouched validation.
- **Dependence:** stages are nested and assets correlated, so BH is approximate (as in
  Phase 5). Transition CIs treat overlapping populations as independent.
- **One definition per concept** (`*_v1`): different swing, cluster or shift definitions
  are untested.
- **Scratch data:** the inputs are a public backfill into a scratch database on
  2026-10-05, not production.
- Funding is included as a secondary column (Binance HYPE has none). It is ≤ 1 bps per
  24 h trade on average and changes nothing.

## Verification

| check | result |
|---|---|
| `pytest tests/test_structure_study.py` | **27 passed** (preregistration, dataset identity, assumed latency, entry timing, hand-calculated entry delay/returns/funding, reversal/continuation signs, mirror symmetry, held ≠ failed, costs, independent events, sample gates, BH family membership, no pooling, R ambiguity, rung linkage, future immunity, determinism, consumer isolation) |
| `pytest` structure + trade-path + intraday + Lab governance + Lab batch + Phase 17 | **167 passed** |
| `pytest` (full) | **737 passed** in 22 m 52 s (before Phase 17: 710) |
| `ruff check src tests dashboard` / `ruff format --check src tests` / `git diff --check` | passed |
| Synthetic null calibration (4 seeds, 142 tests) | 4.9% p < 0.05, 7.0% p < 0.10 |
| Governed run | `srun_b2a805a9…` COMPLETED (209 s, 2.4 GB) |
| Reproducibility rerun on a scratch copy | identical content; full digest `4ac47837…` reproduced by both post-fix runs |

## Next recommended phase

**Phase 18: relative-strength / BTC-dislocation research**, as already planned. It should
use the same governed study adapter: a frozen manifest, retained datasets, per-venue
families and explicit entry semantics.

Phase 17 gives Phase 18 two lessons:

- measure the gap between conditional and executable excess for every confirmation;
- run the null calibration on synthetic data before touching real outcomes.

No Phase 17 hypothesis earned a confirmatory follow-up.

