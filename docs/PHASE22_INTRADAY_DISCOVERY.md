# Phase 22: intraday strategy discovery (`intraday_catalogue_v1`)

> **Discovery is exploratory.** The best possible Phase 22 verdict is
> `STRONG_INCUBATION_CANDIDATE`: eligible to *enter* exploratory paper incubation. Nothing
> here is validated, production-ready or live-ready, and nothing here places an order.

Status: implemented 2026-10-05/06. Code `src/market_signal/research/discovery/`
(`primitives`, `catalogue`, `signals`, `spec`, `collect`, `analysis`, `run`, `synthetic`,
`calibration`, `cook`, `report`), manifest `config/discovery/phase22_intraday_discovery.v1.yaml`,
CLI `market lab discovery …` (`cli/discovery_cmds.py`), tests `tests/test_intraday_discovery.py`
(33 tests). Governed through the Phase 17 study adapter (`research/lab/structure_study.py`,
study version `intraday_discovery_v1`); no migration.

**The real-data discovery run has NOT been executed yet** (§14): this session had no access
to the Prism database, the Railway runtime or the market-data hosts. Everything that can be
fixed before outcomes is frozen and committed — catalogue, windows, statistics, verdict rules —
and the harness is calibrated on synthetic nulls and planted temporary edges. All numbers in
§6–§12 are **synthetic** unless stated otherwise.

## 0. Phase 21 baseline (housekeeping)

- Phase 21 was committed (`f4b86f6`, `c6e3610`) and pushed on
  `origin/claude/fervent-volta-7ldupy`; this branch merged it unchanged (tree identical).
- Its frozen identities are pinned by `test_phase21_baseline_is_unchanged` and cannot move
  through Phase 22 (Phase 22 changes no file the freeze hashes: daily catalogue, vocabulary,
  compiler, costs, policies, incubation code):
  - freeze definition `incfreeze_f34b719a715d02b2415ae056354d0c77bdf450508e981ed1f9b6ba1ce342a12f`
  - pool `incpool_346515921d0eff2d9929f1cb2d72c99c35d2cbbb43dcf67905e9f2410411a81e` (128 members)
  - CONSERVATIVE `incpolicy_4356148786bda18d98916ca14b53caf48bd1a0d31d3e63d51cc4ea5d8048d76b`,
    BALANCED `incpolicy_aaec7841f4b18d5222c6c50a0f34ced1c86e7c1decb77aa91da0d5608228a042`,
    AGGRESSIVE `incpolicy_c2f2fe3583daa7203ff0e80a3d73966c192cdf5ccfdb0dda5691bd93c72f35f6`
- **Runtime freeze: not verified and not performed from this session** (no Railway CLI or
  token in the container). `docs/CANDIDATE_INCUBATION.md` §11 recorded it as not yet frozen.
  The operator step, unchanged:

  ```text
  railway ssh --service prism-runtime -- market lab incubation freeze \
      --reason "Phase 21 prospective baseline before intraday strategy discovery"
  railway ssh --service prism-runtime -- market lab incubation status --json   # verify
  ```

  The registered `freeze_id` must equal the definition hash above (it is content-addressed;
  the registration time is its prospective start). If a freeze already exists, do not freeze
  again — `status` shows its ID, policy IDs, pool ID and timestamp.

## 1. Why the old catalogue was too slow

Phase 21 measured the daily catalogue: 128 strategies ≈ 8 raw signals/day in total (≈ 2 per
strategy per month); AGGRESSIVE ≈ 0.46 independent opportunities/day; 74% of days with none;
the conservative paper trader at 0 trades. Daily bars, 10-day horizons and stacked
conditions starve an active perp sleeve of information: a 30-day edge resolves its outcomes
after it has ended. Phase 22 moves the unit of research to the hour.

## 2. Architecture (reuse, not a parallel stack)

| Need | Reused |
|---|---|
| bars, availability, gaps | Phase 15 `perp_intraday_bars`; Phase 16 `BarSeries` (`ready_at`, segments, assumed latency) |
| retained datasets, study lifecycle | Lab `capture_dataset` / `Ledger`; Phase 17 study adapter (register → run → one result, append-only) |
| costs | `perps.backtest.perp_costs`, frozen per venue/coin at registration |
| funding | Phase 17 `funding_paid` (settlements in (entry, exit], NaN when uncovered) |
| time-blocked inference | Phase 18 `clustered_mean` / `clustered_difference` |
| BH | Lab `benjamini_hochberg` |
| edge states, stress | Phase 20 `classify` / `divergence`, `market_stress_v1` (read-only) |
| admission replay | Phase 21 `replay_incubation` and frozen BALANCED/AGGRESSIVE (read-only) |
| independence | `backtest.events.decluster` |

New: versioned primitives, a second catalogue, the discovery harness and its verdicts. The
daily catalogue (`config/lab/families`, `vocabulary_v1`, `istrat_` vs `strategy_` IDs) is
untouched; nothing outside `research/discovery`, the study adapter and the CLI imports it.

## 3. New features (`intraday_features_v1`, exact definitions)

All causal (bar `i` reads bars ≤ `i`, known at `ready_at[i]`), gap-aware (a window spanning
a missing bar is NaN), and computed on **log prices** so a price-mirrored market is exactly
the negated log market. `n` bars; `lc, lh, ll, lo` = log close/high/low/open.

| primitive | definition |
|---|---|
| `ret_z(n, 48)` | `(lc[i]-lc[i-n]) / (rv48[i-n]·√n)` — move in pre-move volatility units |
| `rv(n)` | std of one-bar log returns over the last `n` bars |
| `ema(n)`, `ema_slope(n,k)` | EMA of `lc` (span n, restart at gaps); `ema[i]-ema[i-k]` |
| `atr` | Wilder ATR(14) of the log true range; `atr_prior[i]=atr[i-1]` |
| `efficiency(n)` | `(lc[i]-lc[i-n]) / Σ|Δlc|` (signed Kaufman efficiency) |
| `roll_high(n)`, `roll_low(n)` | max high / min low of the **prior** n bars |
| `range_width(n)` | `log(roll_high/roll_low)`; `range_width_atr = width/atr_prior` |
| `range_mid(n)` | `√(roll_high·roll_low)` |
| `range_pos(n)` | `(lc - log roll_low)/range_width` (<0 or >1 = outside: a break) |
| `pct_rank(x, w)` | share of trailing `w` values ≤ current (≥ w/2 finite) |
| `compression(n, w)` | `pct_rank(range_width(n), 30 days)` — low = compressed |
| `rv_pct(n, w)` | `pct_rank(rv(n), 30 days)` (Family J terciles) |
| `rel_volume(n)` | `v[i]/mean(v[i-n..i-1])` |
| `vol_z(n)` | z-score of `log v[i]` vs the prior n bars |
| `vol_slope(n)`, `vol_accel(n)` | OLS slope of `log v` over n bars; its n-bar change |
| `close_location` | `(lc-ll)/(lh-ll)` |
| `body_atr`, `tr_atr`, `ema_dist_atr(n)` | `(lc-lo)/atr_prior`, log TR/`atr_prior`, `(lc-ema)/atr` |
| funding level / pct | mean settled funding rate over the last 24 h; 30-day percentile |

Volume: zero-volume bars are placeholders (Hyperliquid pre-listing, Binance maintenance) and
make every volume window containing them NaN; volume is only used relative to its own past
(never compared across venues).

## 4. Catalogue (`intraday_catalogue_v1`, `icat_2444a9f9…`)

**125 directional variants: 61 long / 64 short** (61 mirrored pairs + 3 short-specific);
105 on 1H, 20 on 15m; 4 with higher-timeframe context; median complexity score 5. Plus 5
non-directional volatility tests. Every identity encodes timeframe, side, family, rule,
parameters, entry/exit rule, horizons, feature version, universe and its simpler baseline.

| BH family | variants (L/S) | rules (one-sentence hypotheses in `catalogue.RULES`) |
|---|---|---|
| momentum | 14/14 | `momentum_z` (1H n∈{4,12}×k∈{1.5,2}; 15m k∈{1.5,2}), `ema_cross` n∈{20,50}, `ema_cross_trend`, `efficiency_trend` er∈{.4,.6}, `momentum_htf` (4H context), `momentum_ltf_ctx` (15m trigger, 1H context) |
| pullback | 4/4 | `pullback` entry∈{immediate, confirm} × trend∈{1H, 4H} |
| breakout | 5/6 | `breakout` 1H n∈{12,24,48}, 15m n∈{16,48}; short-only `failed_bounce_breakdown` |
| compression | 10/10 | `compression_breakout` (1H n∈{12,24}×pct∈{.2,.35}; 15m), `compression_volume_breakout`, `compression_clue` clue∈{range_pos, signed_return, ema_slope, pressure} |
| mean_reversion | 7/7 | `range_reversion` (1H n∈{24,48}×width∈{≤8 ATR, any}; 15m), `ema_stretch_fade` k∈{3,4} |
| volume | 5/6 | `volume_continuation` r∈{2,3} (+15m), `volume_fade` r∈{2,3}; short-only `volume_downside_expansion` |
| expansion_exhaustion | 10/11 | `expansion_continuation` / `expansion_fade` (1H k∈{2,3}; 15m), `exhaustion` z∈{2,3}×strong_close; short-only `blowoff_exhaustion` |
| funding | 2/2 | `funding_trend` (extreme funding + momentum), `funding_reversal` (opposite extreme + recovery) |
| btc_context | 4/4 | alts only: `btc_aligned_momentum` aligned/against, `btc_flat_breakout`, `btc_strength_breakout` |
| volatility_forecast | 5 tests | `compression_state` (1H, 15m), `compression_volume_state`, `volume_spike_state`, `expansion_state` |

Short-specific rules (documented asymmetry, not invented for count): breakdown after a bounce
rejected at the 20-bar EMA in a downtrend; a large high-volume down-bar closing near its low;
a blow-off top (24-bar z ≥ 3, weak close, high volume).

Family J (volatility state) is a contrast on every variant (low/normal/high realised-vol
tercile), not extra strategies. Family K and L variants and every filtered rule declare a
simpler baseline (`breakout` for compression breakouts, `momentum_z` for funding/BTC/HTF
momentum, …) so a filter's added value and destroyed frequency are measured.

## 5. Timeframes, entry and horizons

- **Signal at bar close**; availability = close + 60 s (assumed latency, Phase 15 semantics).
- **Entry `next_15m_open_after_availability_v1`**: the open of the first 15m bar opening at
  or after availability — a 1H signal closing 10:00 enters 10:15; a 15m signal closing 10:15
  enters 10:30. Signal-to-entry delay is recorded per event (15 min). No same-bar hindsight
  (`test_entry_is_the_next_15m_open…`, truncation-invariance test).
- **Exit `fixed_horizon_close_v1`**: close of the 15m bar ending `horizon` after entry. No
  stops, no targets. Incomplete or gap-crossing paths are NaN.
- **Horizons**: 1H strategies {1, 2, 4, 8, 12, 24} h with one primary + two secondaries per
  rule (primary 4 h for momentum/breakout/range/expansion; 8 h for trend crosses, pullback,
  compression clues, funding; 2 h for volume); 15m strategies {30 m, **1 h**, 4 h}.
- **4H is context only**; 15m only for fast triggers; no timeframe matrix.

## 6. Windows (frozen from calendar + coverage before outcomes)

| window | span | role |
|---|---|---|
| contemporary | 2024-11-01 → 2026-10-01 (Binance, 15m coverage from 2024-10-01 + warm-up) | primary test, BH |
| recent-180 / 90 / 30 | 2026-04-04 / 07-03 / 09-01 → 2026-10-01 | emerging/temporary routes, "dead recently" guard |
| recency-weighted | half-life 90 days | reported |
| long context | Binance 2021-01-01 → 2024-11-01, 1H strategies, **1h execution grid** (60-min delay) | context only |
| Hyperliquid replication | 2026-08-15 → 2026-10-01 | descriptive (execution venue) |

Lifetime never vetoes a recent edge; a strong past never rescues a dead recent one (tests
`test_emerging_edge…`, `test_dead_recent_edge…`).

## 7. Statistics and verdicts (`intraday_discovery_inference_v1`, `phase22_verdicts_v1`)

- Unit: net return per independent event (after fees, slippage, funding), side-signed.
  Independence: greedy per coin, gap = primary horizon. Excess = net minus random entries
  on every bar of the same coin, side, horizon and volatility tercile.
- Test: one-sided block-clustered t, **UTC-day blocks shared by all coins** with adjacent-day
  covariance (six BTC-driven co-timed signals are not six observations;
  `test_cross_asset_cotimed…`). BH q ≤ 0.10 within each frozen family; untestable members
  get p = 1 (m never shrinks).
- Economic floor (net, per event): 15m 4 bp, 30m 5, 1h 6, 2h 8, 4h 10, 8h 15, 12h 18, 24h 25.
- Verdicts (first match): NO_EVIDENCE (< 30 events in every window) · REJECTED (too sparse:
  < 1/month unless t ≥ 3) · **INCUBATION_CANDIDATE**: a route — contemporary (mean ≥ floor,
  t ≥ 1.5), recent-180 or recent-90 (t ≥ 2.0), recent-30 (t ≥ 2.5) — with guards: ≥ 3 assets,
  the next more recent window not credibly dead (t > −1), recent-180 > 0 for the contemporary
  route, leave-best-asset-out > 0, top-5 events < 50% of net, ≥ half of assets positive,
  gross > 0, excess > 0, one-step parameter neighbour with t ≥ 1 (or t ≥ 2 alone), ≥ 1
  signal/week · **STRONG** adds BH q ≤ 0.10 and a positive most-recent window · INTERESTING
  (mean > 0, t ≥ 1) · REJECTED (t ≤ −1, or edge only before costs) · NO_EVIDENCE.
- **Disclosure:** the recent-90/recent-30 routes and the "next more recent window" guard were
  added after the planted-edge calibration (synthetic only, no real outcome computed) showed
  30–60-day edges are diluted below detection in a 180-day window. The synthetic null was
  also corrected to a martingale price (zero-mean log returns carry a +σ²/2 long drift).
  No real-data result has been seen by anything in this phase.

## 8. Null and planted calibration (`discovery_calibration_v1`, synthetic)

`market lab discovery calibrate --workers 3` — the full harness on the real manifest's
windows; six-coin market with a common BTC factor, AR(1) volatility clustering, t(5) tails,
wicks, volume tied to |return|, persistent funding; 21 runs, 347 s (≈ 55 s / 550 MB each).

| scenario (seeds) | truth | result |
|---|---|---|
| null, zero cost (4) | no edge | raw p<0.05 **7.2%** (4.1% on the seed without net market drift); BH false discoveries 2.25/run; false candidates 7.5/run, false STRONG 1.75/run — all on the side of the sample path's realised drift (long mean t +0.61, short −0.96) |
| null, realistic costs (3) | no edge | raw p<0.05 0.3%; BH 0; **false candidates 0.33/run, false STRONG 0** |
| 1H momentum +0.8%, last 60 d (2) | emerging | INTERESTING ×2 (long; recent-90 t 1.1–2.4) — below the power frontier |
| 1H momentum +1.5%, last 60 d (2) | emerging | long: CANDIDATE once (recent-90 t 4.0, state EMERGING, pattern "appears"), INTERESTING once (failed parameter support); short INTERESTING/REJECTED |
| 15m breakout +0.5%, last 30 d (2) | temporary | **3 of 4 sides found** via recent-30 (t 2.6–5.4); the 4th t 2.48 |
| short-only breakdown +1.2%, last 120 d (2) | short only | **short found 2/2** (recent-180, EMERGING/"appears"); long side not admitted |
| compression → vol ×2.5 for 8 h (2) | volatility | detected t 3.6 / 4.7 (null: compression predicts *smaller* moves, t −8 to −12 — volatility clustering) |
| +0.8% momentum only in year 1 (2) | dead recently | **never a candidate** (INTERESTING/REJECTED; state DEAD/DECAYING, pattern "decays") |

Reading: the test is calibrated on the mean; a shared realised market drift moves every
same-side strategy together (exactly why drift is not edge — the excess guard catches most).
Under realistic costs false admissions are rare. Power frontier: ~60-day edges need ≈ +1.5%
per event (per-event net Sharpe ≈ 0.25); 30-day edges are found when the strategy fires
often (15m). Non-target candidates in planted runs are mostly genuine side effects of the
plant (e.g. 15m momentum on a market with planted breakout drift), not false positives.

## 9. Opportunity frequency (synthetic, indicative)

Signal rates depend mostly on market texture, so the synthetic market indicates scale only:

| BH family | variants | median raw / day | median independent / day | median zero-signal days |
|---|---|---|---|---|
| breakout | 11 | 7.6 | 6.3 | 6% |
| mean_reversion | 14 | 6.3 | 4.9 | 19% |
| momentum | 28 | 4.3 | 3.7 | 9% |
| pullback | 8 | 4.6 | 3.2 | 19% |
| volume | 11 | 4.2 | 4.2 | 14% |
| btc_context | 8 | 2.5 | 2.1 | 38% |
| compression | 20 | 1.1 | 1.0 | 53% |
| funding | 4 | 1.1 | 0.8 | 52% |
| expansion_exhaustion | 21 | 0.4 | 0.3 | 84% |

The whole catalogue: ≈ 500 independent signals/day; after (coin, side, 4 h) dedup ≈ **56
opportunities/day** (28 long / 28 short; zero-opportunity days 0%) — a *ceiling*, not a plan
(Phase 21's daily pool: ≈ 8 raw / 0.46 admissible per day). 9 variants are below 1/week, 3
below 1/month (flagged; the rare exhaustion/blow-off rules). Real rates: §14.

## 10. Costs

Binance 5 bp taker + 2–8 bp slippage per side → 14–26 bp round trip; Hyperliquid 4.5 bp +
2–8 bp. At 1–4 h holding periods this is the dominant term: on the costed null every
family's median cost drag ≈ 20 bp/event and **104–109 of 125 variants are REJECTED** with
gross ≈ 0. Every row reports gross mean, cost drag (fees + slippage + funding) and net; an
edge only before costs is REJECTED by rule.

## 11. Overlap, clustering, ensemble, Phase 21 replay, MFE/MAE, stress

- Duplicates: events match on coin + side within 2 h; overlap = matches/(|A|+|B|−matches);
  single-linkage at 0.5; the lowest-complexity member represents the cluster (then higher t).
- Shortlist: cluster representatives with a candidate verdict, ≤ 3 per BH family.
- Ensemble: shortlist, all candidates, interesting representatives and the whole catalogue,
  deduplicated by (coin, side, 4 h): raw / independent / long / short per day; share of days
  with 0, ≥ 1, ≥ 5; share of the 5/day reference line (never a quota).
- Phase 21 replay (diagnostics): BALANCED/AGGRESSIVE imported unchanged, evaluated at every
  trigger-bar close; the floor passed is Phase 22's intraday floor (Phase 21's table has no
  intraday horizon). Reports admission delay, episodes, active share, trades captured vs
  available, captured/missed net, opportunities/day.
- MFE/MAE for INTERESTING+ at the primary horizon: means/medians, time to each, favourable-
  before-adverse share at ±0.5 ATR (same-bar touches are "ambiguous").
- Stress: Phase 20 `market_stress_v1` on BTC daily closes rebuilt from 1h; all / normal /
  stress splits, never deletion.

## 12. Performance

Real-scale synthetic benchmark (both venues, long context, Hyperliquid replication,
≈ 876k source bars, 125 variants + 5 vol tests): **51 s wall, 716 MB peak RSS**, deterministic
rerun digest identical. Features are computed once per (coin, timeframe) and shared by every
variant; signals are vectorised masks; nothing is quadratic (clustering is pairwise only over
INTERESTING+ strategies).

## 13. AI-agent "cook" interface

```text
market lab discovery catalogue [--full] --json
market lab discovery calibrate [--workers N] [--out FILE]
market lab discovery register <manifest> --reason …        # WRITE: freeze + retain datasets
market lab discovery run <study_id> [--rerun-of RUN --reason …]   # WRITE
market lab discovery report <run_id> [--json] [--out FILE]
market lab discovery cook <run_id> --json [--candidates-only]  # "What did the Lab cook?"
market lab discovery shortlist <run_id> [--out FILE]           # eligibility record (ielig_…)
```

`cook` lists, per candidate: hypothesis, strategy ID, side, timeframe, frequency, recent
effect, long-context, gross/cost/net, evidence state, edge state, pattern, rule-generated
caveats, overlap cluster, and the recommended policy (STRONG → BALANCED, CANDIDATE →
AGGRESSIVE; INTERESTING → none). No command accepts a threshold, window or date.

## 14. Running the real study (not yet done)

On a **scratch copy** of the production database (never the live file):

```text
deploy/railway/pull_backup.sh                                  # verified copy -> data/remote/…
export PRISM_RUNTIME_ROLE=scratch
uv run market --db data/remote/<copy> bars status              # Binance 15m ≥ 2024-10-01, 1h ≥ 2020-11
uv run market --db data/remote/<copy> lab discovery register \
    config/discovery/phase22_intraday_discovery.v1.yaml --reason "Phase 22 intraday discovery v1"
uv run market --db data/remote/<copy> lab discovery run <study_id>
uv run market --db data/remote/<copy> lab discovery run <study_id> --rerun-of <run> --reason determinism
uv run market --db data/remote/<copy> lab discovery report|cook|shortlist <run_id>
```

Known data facts: Binance HYPE has no retained funding, so its events are non-evaluable
(never zero-filled); Hyperliquid 15m starts 2026-08-14, so the replication window is short.

## 15. Phase 21 compatibility and prospective eligibility

The shortlist is **eligible, not active**. The Phase 21 prospective runner is daily-only
(compiler strategies, daily bars, 10-bar horizon), so intraday candidates cannot be added to
it — and must not be: the Phase 21 freeze is immutable. Safe procedure for a later phase:

1. take the frozen `shortlist` record (exact `istrat_` IDs, catalogue/feature versions,
   entry/exit, horizons, expected frequency, study ID, result digest);
2. implement an intraday incubation runner (hourly trigger-bar evaluation, 15m-open shadow
   entries, fixed-horizon shadow exits) as a **new** versioned component;
3. register a **new, separate** freeze (e.g. `phase22_intraday_incubation_v1`) whose pool is
   exactly the shortlist; its registration time is its prospective start; the Phase 21
   freeze and its records are untouched and keep collecting;
4. never backfill: only bars closing after the new freeze count.

## 16. Live compatibility

No order path, venue client, key, paper account, co-pilot policy or risk policy was touched.
`research/discovery` imports no paper/co-pilot/ops/forward/Telegram/HTTP code and issues no
SQL writes; nothing in paper, co-pilot, ops, perps, intraday or the incubation package
imports it; no runtime job runs it (all tested). Study writes go only to the existing
append-only `lab_structure_*` and dataset tables via the Phase 17 adapter.

## 17. Limitations

- **No real-data discovery result yet** (§14); family results, the real shortlist, real
  opportunity rates and the BALANCED/AGGRESSIVE replay on real candidates are pending.
- Assumed-latency backfilled availability; 15m execution grid limits the evaluable span to
  23 months; long context uses a coarser 1h grid.
- Synthetic calibration uses one market model; ~2 seeds per planted scenario.
- One Binance discovery venue; Hyperliquid replication is ~6 weeks; Binance HYPE unfunded.
- Many routes = many looks: false candidates under a cost-free null are ~6%/variant-run;
  under realistic costs ~0.3 per catalogue run. Discovery admits to exploration, not capital.

## 18. Next recommended phase

First complete Phase 22 itself: execute the frozen study on a production data copy (§14) —
no code or threshold changes. Then **Phase 23 — intraday shadow incubation of the Phase 22
shortlist**: a new, versioned intraday incubation runner and a separate prospective freeze
for the shortlist (Phase 21 untouched), collecting shadow outcomes with no threshold changes.
Not begun.
