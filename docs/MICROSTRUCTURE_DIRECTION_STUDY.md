# Phase 24B: microstructure direction study (`microstructure_absorption_v1`)

> **Phase 24B tests whether microstructure provides incremental directional information
> beyond OHLCV. A microstructure signal is not useful merely because it describes market
> activity.**
>
> **Aggressive flow does not automatically imply continuation. Failure of price to respond
> to aggressive flow is itself a testable market state.**

Status: implemented 2026-10-07; branch `claude/phase24b-microstructure-study`, stacked on
Phase 24A plus current `main`. Code: `src/market_signal/research/microdir/`, with modules
`spec`, `features`, `outcomes`, `analysis`, `synthetic`, `calibration`, `data` and
`governance`. CLI: `market lab microstructure …` (`cli/microdir_cmds.py`). Migration: 24
(`lab_prospective_*`). Tests: `tests/test_microstructure_direction.py`.

Research only. Nothing here places an order, creates an order intent, touches the paper
account, the co-pilot, risk policy or the Phase 21 incubation freeze, or sizes a position.
No runtime job runs it, and no consumer reads its tables (a test enforces this).
**No real prospective outcome has been read by anything in this phase.**

## 0. Production evidence at implementation time

| item | state (2026-10-07 ~18:15Z) |
|---|---|
| Phase 24A on `main` | **not merged** (`main` = Phase 23, PR #15); 24A is on `origin/claude/phase24a-microstructure` |
| Phase 24A on production | **deployed**: Railway deployment `prism eefbc8b`, 2026-10-07 18:01:51Z |
| production cutover / coverage | **not verified here** (`railway ssh` was not permitted in this session); first possible cutover = first ingest after boot (≈ 18:06–18:21Z) |
| prospective data | at most a few hours |
| large prints | NULL until 3 complete prior UTC days (≈ 2026-10-11) |
| feature normalization | needs 3 days of prior COMPLETE windows (≈ 2026-10-10/11) |
| OI hourly cutover / Phase 23 freshness | read with `market lab microstructure status --json` on the runtime |
| maturity | **WARMUP** (by construction) |

Verify with:
`railway ssh --service prism-runtime -- market lab microstructure status --json`.

**Correct conclusion today: study ready; evidence immature.** Every number in §9 is
**synthetic**.

## 1. Economic hypothesis

When taker flow becomes strongly one-sided, the price response it buys separates three
states:

* **Continuation (accepted flow).** Strong buying, a meaningful mid move up, ask depth
  consumed rather than replenished, healthy spread. Hypothesis: the pressure is accepted
  and continues.
* **Absorption (unaccepted flow).** Strong buying, the mid stalls or falls, asks replenish
  and stay resilient, and OI may rise (new leverage). Hypothesis: aggressive longs are being
  absorbed and are vulnerable, so price moves down. Sell absorption is the symmetric case,
  with price moving up.
* **Meaningless.** Neither state forecasts anything after costs.

Neither hypothesis is assumed to be profitable. Shorts are first-class: every rule exists
for both sides, and a mirrored-market test proves the event sets are exactly symmetric.

## 2. Data and timing

* Source: `microstructure_1m_v1`, read only through the 24A causal loader
  (`load_microstructure`), with:
  * `production_only=True`: nothing before the immutable cutover is loaded, and a database
    without a cutover yields nothing;
  * `known_at=as_of`;
  * availability = **finalization** (receipt clock, close + 5 s).
* Signal windows: UTC quarter hours built from the 1-minute primitives. Three 5-minute
  sub-windows are used for persistence.
* A window is **complete** only if all 15 minutes are COMPLETE. Incomplete windows never
  generate events and never enter a normalization. PARTIAL, GAP and MISSING minutes are
  never zero-filled.
* **Signal time** = the latest finalization among the window's 15 minutes.
* **Entry** = the end-of-minute mid of the first minute closing at or after signal time.
  That is 60 s after the window close in practice; the delay is recorded per event.
* **Exit** = the end-of-minute mid `h` minutes later. No stops and no targets.
* An exit beyond the data is **pending** (not yet matured). A gap inside the data is
  **missing**. Both are counted, and neither is ever filled.
* Primary test: **15m signal → next 1h**. The 15m, 30m, 2h and 4h horizons are descriptive.

## 3. Features (`microdir_features_v1`)

Every scale or percentile uses **prior** COMPLETE windows only: the trailing 7 days, with at
least 288 windows. `s` = +1 for buy flow and −1 for sell flow.

| feature | definition |
|---|---|
| signed flow | `delta = Σ buy_ntl − Σ sell_ntl` over 15 m; `flow_pct` = trailing percentile; `flow_z` = delta / trailing std |
| flow bucket | strong buy `pct ≥ 0.90`, extreme `≥ 0.975`; sell symmetric; else neutral |
| price response | `resp_z = s · log(mid_end / mid_end_prev) / σ15` (σ15 = trailing RMS of 15m mid returns) |
| response class | efficient `resp_z ≥ 0.75`; moderate `0.25–0.75`; weak `(−0.25, 0.25)`; opposite `≤ −0.25` |
| response efficiency | `resp_z / |flow_z|` (descriptive) |
| replenishment | opposing side `Σ ask_replenish / Σ buy_ntl` (bids for sell flow); trailing percentile |
| depth persistence | opposing top-5 depth at window end / at previous window end |
| book state | resilient = replenish pct ≥ 0.5 **and** depth ratio ≥ 1; consumed = both below; else mixed |
| OI | `oi_z` = 15m log OI change / trailing std; rising ≥ +0.5, falling ≤ −0.5; NaN → unknown, never assumed |
| funding / crowding | crowd-aligned = funding pct ≥ 0.8 (buy) / ≤ 0.2 (sell) **or** Phase 23 `crowding_v1` skew on the flow side (strict first-seen) |
| persistence | persistent = ≥ 2 of 3 sub-windows **and** ≥ 9 of 15 minutes with the window's sign; burst = one minute ≥ 50% of gross minute flow |
| large prints | aligned = net large-print notional on the flow side (only once the 24A warmup has passed) |
| market state | realised-vol tercile (4 h), session (Asia 0–8, Europe 8–13, US 13–21, other 21–24 UTC), BTC same/opposite direction |
| context | pre-tier-1 ≤ 60 m; post 0–15 m; post 15–60 m; active confirmed crypto event; none (Phase 23 folds from `first_seen_at` only) |

## 4. Replenishment audit

The 24A proxy is `Σ` of positive top-5 depth increments between consecutive fast snapshots
while that side's best price is unchanged. Synthetic streams through the real
`aggregate.book_stats` (tested) show:

* static book → 0;
* depth consumed then restored at the same price → counted;
* price walking up as asks are lifted → **not** counted (correct);
* cancel/re-add flicker without trades → **also counted** (a false positive).

The study therefore never uses the raw level. It uses replenishment **relative to the
notional that hit that side**, as a trailing percentile, **paired with net depth
persistence** (end vs start), which flicker cannot move.

No collector change was made, and `microstructure_1m_v1` rows are untouched. A future
`_v2` could add a depletion counter for net replenishment.

## 5. Hypotheses, families and contrasts

Each hypothesis has a long and a short version. Families F1–F5 can earn verdicts; F6 and
F7 are descriptive.

| family | members (action) |
|---|---|
| F1 flow_continuation | `flow_follow`, `continuation_core` (+efficient), `continuation_book` (+consumed opposing book): follow |
| F2 absorption | `absorption_core` (+weak/opposite response), `absorption_book` (+resilient opposing book): fade |
| F3 absorption_oi | `absorption_oi_rising`, `absorption_oi_not_rising`: fade |
| F4 absorption_crowding | `absorption_crowded`: fade |
| F5 persistence | `continuation_persistent`, `absorption_persistent` |
| F6 large_print | `continuation_lp_aligned`, `absorption_lp_aligned` (descriptive) |
| F7 context | breakdowns of every member by context category (descriptive; CPI/FOMC periods are reported, never removed) |

Contrasts (difference of means, shared time blocks), each per side:

* OI rising vs not (the primary OI question);
* crowded vs not;
* resilient vs not;
* consumed vs not;
* continuation with OI rising vs falling;
* persistent vs burst (absorption and continuation);
* large prints aligned vs not.

The OI interpretation stays neutral. Rising OI adds leverage on both sides; direction comes
only from flow plus response plus context.

## 6. Baselines

* **Candle twins.** For every hypothesis, the same idea from OHLCV only (a trade-price
  candle plus volume), evaluated with the same costs and statistics:
  * big candle move (return pct ≥ 0.90): follow;
  * candle momentum (volume pct ≥ 0.90 and |return z| ≥ 0.75): follow;
  * candle absorption (volume pct ≥ 0.90, small body, close location ≤ 0.35 → short,
    ≥ 0.65 → long).
* **Candle residual.** The next-1h return in σ(1h) units, minus the prediction of the
  candle-only OLS (return z, close location, momentum twin, absorption twin), fitted on
  every eligible window. Its block-clustered mean per member answers whether the member
  predicts more than the candle already says.
* **Layer test (the "ladder").** Nested OLS on identical rows: candle → +flow (`flow_z`,
  signed continuation and absorption indicators) → +book (imbalance, signed resilience) →
  +OI (signed OI-rising flow, absorption × OI rising) → +positioning (funding pct,
  absorption × crowding). Each layer's added terms get a block-clustered Wald F test,
  including adjacent-block covariance.
* **Random entry.** Excess = net minus the mean net of **every** eligible window of the same
  coin, volatility tercile and session, on the same side. This is deterministic; volatile
  periods cannot pass as edge.

## 7. Costs

* Frozen at registration from Prism's perp cost model, Hyperliquid taker 4.5 bp per side
  plus slippage: BTC/ETH 2, SOL 4, HYPE/LINK 6, AAVE 8 bp. That is a **13–25 bp round
  trip**.
* Funding: hourly settlements in (entry, exit], at the observed context rate; shorts
  receive positive funding.
* Every member reports gross, fees, slippage, funding and net, plus the **break-even round
  trip** (= its gross mean).
* **Passive sensitivity, never the verdict.** Maker entry at Hyperliquid's published
  1.5 bp base fee with no entry slippage, then a taker exit. Fill probability and adverse
  selection are not modelled, so this is an optimistic bound.

## 8. Inference, independence, maturity and verdicts (`microdir_inference_v1`)

* **Episodes.** Greedy refractory per (coin, member): an event within 60 m of the last kept
  one belongs to the same episode. Raw events, episodes and the overlap share are all
  reported.
* **Tests.** One-sided, block-clustered t on net per episode. Blocks are **4 h UTC, shared
  by all coins**, with adjacent-block covariance, so six coins reacting to one BTC move
  count as one cluster. BH q ≤ 0.10 is applied within each family; untestable members get
  p = 1.
* **Maturity (`microdir_maturity_v1`)**, needing coverage days (≥ 50% of the day's windows
  COMPLETE), episodes and assets:

  | level | coverage days | episodes | assets |
  |---|---|---|---|
  | WARMUP | below EARLY | | |
  | EARLY | 7 | 30 | |
  | DEVELOPING | 30 | 100 | ≥ 3 assets with ≥ 10 episodes each |
  | ADEQUATE | 90 | 300 | ≥ 4 assets with ≥ 20 episodes each |

* **Verdicts (`microdir_verdicts_v1`, first match):**
  1. **IMMATURE** at WARMUP.
  2. A candidate route (DEVELOPING or better) with every guard passing gives
     **INCUBATION_CANDIDATE**. Routes:
     * full sample: net ≥ 6 bp and t ≥ 2;
     * last 30 days: ≥ 30 episodes, mean ≥ floor and t ≥ 2.5. A recent edge is **never
       vetoed** by an earlier sample.

     Guards:
     * ≥ 3 assets;
     * leave-best-asset-out mean > 0;
     * top-5 episodes < 50% of the net;
     * ≥ half of assets positive;
     * gross > 0;
     * excess t ≥ 1;
     * candle-residual t ≥ 1;
     * ≥ 0.25 independent episodes per day.

     **STRONG_INCUBATION_CANDIDATE** additionally needs a full-sample route, ADEQUATE
     maturity, BH q ≤ 0.10, net t ≥ 2.5, candle-residual t ≥ 2 and a positive last-30-day
     mean.
  3. A route that fails a guard gives **INTERESTING**.
  4. **REJECTED** if net t ≤ −1, or if gross t ≥ 2 while net ≤ 0 (an edge only before
     costs).
  5. **INTERESTING** if net > 0 and t ≥ 1 (EARLY caps the verdict here).
  6. Otherwise **NO_EVIDENCE**.
* **Candidate composition is structural**: the hypothesis, flow side, conditions, action,
  direction and horizon. There is no opaque score.

## 9. Calibration (synthetic only, `market lab microstructure calibrate --workers 4`, ≈ 23 s)

Market model: six coins with a common factor and AR(1) order-flow imbalance. Price impact
is linear in the **flow innovation**, a Kyle-style martingale, so persistent flow is not by
itself a forecast. Volatility clusters with a session profile. Replenishment follows the
notional that hit a side, plus flicker. OI and funding are independent of future returns.
Outages produce GAP/PARTIAL minutes, and there are natural one-minute bursts.

Plants: an 80 bp drift over the next hour, 3 planted windows per coin per day, 40 days
(the temporary scenario uses 120 days with plants in the last 30).

| scenario (seeds) | result |
|---|---|
| null, zero cost (3) | raw one-sided p < 0.05 **1.7%**; 0.33 false candidates/run (one `absorption_book:long`); no ladder layer p < 0.11 |
| null, realistic costs (3) | **0 candidates**, 0 BH discoveries; 59/60 members REJECTED (cost drag), 1 NO_EVIDENCE; no ladder layer p < 0.13 |
| continuation (2) | `continuation_book` detected on **3 of 4 sides** (the 4th INTERESTING, t 1.3); flow and book layers p ≤ 0.003; absorption members REJECTED |
| absorption + OI rising (2) | **4/4 targets** on both seeds (`absorption_oi_rising` and `absorption_book`, both sides); OI-rising-vs-not contrast **+49 to +83 bp, t 4.7–9.0**; flow, book and OI layers p ≤ 0.002 |
| candle-equivalent (2) | **no candidates**; flow layer p 0.69 / 0.07; flow members gross-positive but candle residual ≈ 0 |
| temporary, last 30 of 120 days (2) | detected via the **recent route** on 7 of 8 target sides, including `absorption_core` whose full sample is negative (t −2.1 to −2.9); 1 STRONG |

Non-target candidates in planted runs (`absorption_core`, `absorption_persistent`,
`absorption_crowded`) are supersets of the planted windows: genuine side effects, not false
positives. Two fixes were made during calibration, before any real outcome existed:

* The first synthetic "null" let returns respond to the *level* of autocorrelated flow, so
  it was not a null. Impact now responds to the innovation.
* Verdict order: a whole-sample rejection had been vetoing the recent route.

## 10. Sequential prospective evaluation (no optional stopping)

```text
market lab microstructure register --reason "Phase 24B frozen before prospective outcomes"
market lab microstructure checkpoint --cadence daily     # every owed 00:00 UTC, in order
market lab microstructure checkpoint --cadence weekly    # every owed Monday 00:00 UTC (governed)
market lab microstructure history --json                 # every checkpoint, each member's path
```

* Checkpoints sit on a fixed UTC grid, starting at the first grid instant after
  registration. They are evaluated **in order without skipping**; missed ones are caught up
  in order.
* `as_of` must be at least 1 h old (so ingest has caught up). No command accepts a date,
  threshold, horizon or cost.
* The checkpoint row is committed **before** evaluation. FAILED results are kept.
* Daily checkpoints store a compact descriptive payload. Weekly checkpoints store the full
  governed payload (~200 KB).
* `history` reports, per member, the full path, the best verdict ever reached, the current
  verdict and `fell_back`. The best date is never shown on its own.
* Reproduction: `checkpoint --reproduce <id> --reason …` re-evaluates the same `as_of`. Each
  payload records a rows digest.
* Registering after the cutover is allowed, because the definition is frozen in git first.
  The payload flags `registered_after_cutover`, and confirmatory rows always start at the
  cutover. **Historical Hyperliquid S3 archives are not used**: they lack receipt-time
  semantics (24A doc §14). Any future exploration of them must stay a separate, labelled
  historical study that never counts toward maturity.

## 11. Agent interface

```text
market lab microstructure status --json        # production evidence + study state
market lab microstructure state ASSET --json   # latest window, members firing now, evidence
market lab microstructure events --member absorption_core:short --json
market lab microstructure explain EVENT_ID --json
market lab microstructure results --json
```

`state` is designed to be combined with `market context snapshot ASSET` and
`market microstructure inspect ASSET`, so no agent needs to scrape raw tables.

`explain` reports:

* why the event qualified: flow pct and z, response, book, OI, funding and crowding,
  context, candle;
* its outcome;
* its versions.

Examples in results are the **most recent** matured favourable, unfavourable and ambiguous
episodes, never the best ones.

## 12. Failure analysis (frozen flags)

`sample_immature`, `flow_has_no_edge`, `flow_works_but_costs_kill_it`,
`absorption_too_rare`, `oi_adds_nothing`, `book_adds_nothing`,
`candles_already_explain`. At WARMUP only the first flag is informative.

## 13. Limitations

* There is no real evidence yet. Even ADEQUATE (90 days) is one regime's worth of
  prospective data, on one venue and six coins.
* The replenishment proxy counts flicker, which the study mitigates but cannot remove.
  Top-20 depth has ~5 s resolution. OI and funding come from the asset context as of
  receipt.
* Mid-to-mid outcomes plus a cost model, not fill simulation. The passive scenario is an
  optimistic bound.
* The candle-residual model is linear in four candle terms. A non-linear candle effect could
  leave a residual that the flow layer absorbs.
* Large-print and context families are descriptive until their inputs mature. Macro
  windows will be tiny samples for months.
* Calibration uses one synthetic market model and two to three seeds per scenario. The power
  frontier is roughly 3 plants per coin per day at 80 bp for 30–40 days.

## 14. Recommended next phase

Deploy this branch (after merging 24A and 24B), register the study on the runtime, and run
daily and weekly checkpoints, through operator cron or a later runtime job. Read nothing as
evidence until the primary members reach **DEVELOPING** (≥ 30 coverage days). Not begun.
