# Phase 20: time-varying edge and strategy lifecycle

> **Phase 20 does not lower Prism's evidence standards. It changes the hypothesis from
> "timeless edge" to "currently credible edge" while retaining full historical context.**
>
> **Temporary profitability is not assumed to be durable. Active status must continually be
> re-earned.**

Status: implemented 2026-10-05. Research only. Edge states, lifecycle states and edge
profiles grant nothing: `copilot_policy v1`, `autotrader_policy v1`, `paper_risk_policy v1`,
the paper cohort, the forward tracker and the runtime schedule are unchanged, and no
consumer reads any Phase 20 record (tested).

Code: `src/market_signal/research/lifecycle/` (`policy`, `outcomes`, `estimators`, `market`,
`changepoint`, `state`, `machine`, `profile`, `synthetic`, `study`, `run`, `report`), the CLI
`market lab edge …` (`cli/edge_cmds.py`), the manifest
`config/lifecycle/phase20_edge_lifecycle.v1.yaml`, additive migration 20
(`lab_edge_profiles`) and `tests/test_edge_lifecycle.py` (30 tests).

## 1. Philosophy

Phases 17–19 asked whether a strategy works robustly across long history, assets, venues and
regimes. That question remains. But crypto is strongly non-stationary — liquidity, leverage,
participants, venues and regulation change — so an edge may be real for months or a regime
and then vanish. An autonomous trader therefore needs to:

1. detect currently credible edges;
2. tell whether they are strengthening, stable, decaying or dead;
3. switch strategies on and off reversibly;
4. accumulate prospective evidence;
5. tolerate ordinary drawdowns;
6. leave ruin prevention to a separate safety layer (§14).

Phase 20 adds the second dimension — current, regime-local evidence — *beside* lifetime
evidence. It never replaces it.

### Recency is not a loophole

Every window, gate, weight, floor, threshold, regime and stress definition is frozen in a
versioned, content-addressed policy (`lab_edge_lifecycle_policy` v1,
`lcpolicy_2fae77e8…`, pinned by a test). Studies embed the full policy and its ID. Agent
queries accept no window, threshold or date range: `--as-of` only selects an evaluation that
is already stored. A different window is a new policy version, with v1 results unchanged.

## 2. Inspection: what was reused

| Existing piece | Phase 20 use |
|---|---|
| Phase 3 compiler (`compile_strategy`), Phase 4 screen semantics | the outcome ledger: same eligibility, rising edge, cooldown, T+1 open entry, T+h close exit; parity with `screen` is tested |
| `perps.backtest.side_forward_returns`, `backtest.events.decluster` | per-event net / gross / MAE / MFE / funding; causal greedy independence |
| Phase 7 evidence profiles | sample standard (30 events, 3 assets), consumer-neutral key scan, `extends`-style immutability (new profile per evaluation) |
| Phase 8 forward tracking | read-only `forward_summary` as of the profile time, reported in every profile |
| Phase 9 adapter / walk-forward | the idea of frozen-strategy chronological blocks; Phase 20 generalises it to rolling, causal windows |
| Phase 17 study adapter (`structure_study.py`) | governance of the historical study: frozen content-addressed definition, run row before evaluation, explicit reruns, EXPLORATORY CHECK. A fourth study kind `edge_lifecycle_v1` — no new governance tables |
| Phase 18/19 inference | block-clustered uncertainty because co-timed cross-asset outcomes share shocks |
| Lab datasets | retained, hash-verified daily `perp_bars` + `perp_funding` snapshots |
| Lab family catalogue | the 128 perp variants of the nine v1 families as methodology fixtures |

No parallel scoring system was built: evidence tiers, FDR, full research and validation are
untouched, and an edge state is not a tier.

## 3. Evidence dimensions

Every edge profile carries all of these side by side:

| Dimension | Definition |
|---|---|
| **Lifetime** | all independent outcomes resolved by the evaluation time |
| **Recent** | rolling calendar windows 30/90/180/365/730 d and event-count windows (latest 20/50/100) |
| **Recency-weighted** | half-lives 30/90/180/365 d, with effective sample size and contribution by age |
| **Regime-local** | outcomes whose entry market state matches the current one; recent outcomes in that state; dependence on each label |
| **Prospective** | Phase 8 forward evidence as recorded by the evaluation time (read-only) |
| **Stress-split** | all / normal / stress periods, always together |

Explicit research modes (`research_modes`): **lifetime**; **contemporary** (latest 730 d);
**recent** (180 d, with the event-count fallback); **rolling** (the chronological curve). No
mode silently truncates data.

### Outcome ledger (the unit of evidence)

One row per signal of a strategy on one venue: signal time, `resolved_at` (the exit bar's
close — when the outcome became knowable), net, gross, **causal excess**, MAE, MFE, round-trip
cost, funding paid, independence, evaluability, entry market state, stress flag.

- **Effect unit:** after-cost, after-funding net return on notional per independent event at
  the primary horizon (10 daily bars in the study). Net includes market beta; excess is
  reported beside it.
- **Causal excess** (`causal_trailing_baseline_v1`): net minus the mean net of every eligible
  bar of the same asset and side whose outcome had resolved by the signal close, over the
  trailing 365 days (≥ 60 bars, else excess is missing). Phase 4's full-period baseline would
  leak later bars into earlier decisions; this one cannot (tested by rewriting later bars).

Every estimate at time T reads only outcomes with `resolved_at ≤ T`.

## 4. Rolling windows and gates

| Window | Kind | Inclusion | Extra gate |
|---|---|---|---|
| `d30`, `d90`, `d180`, `d365`, `d730` | calendar | `resolved_at ∈ (T − W, T]` | the strategy's history must cover the whole window |
| `e20`, `e50`, `e100` | event-count | latest N resolved independent outcomes | span ≤ 365 / 730 / 1095 days |

**Minimum sample gates** (all must hold, every window): ≥ 20 independent events, ≥ 3 assets,
≥ 80% of signals in the window evaluable. Lifetime uses the Phase 7 standard (≥ 30 events,
≥ 3 assets). A 30-day window with 4 trades is **insufficient**, however large its mean
(tested). Recency never substitutes for sample size.

**Recent** = `d180` when adequate, else `e50` (low-frequency strategies are not forced into
calendar windows). **Short** = `d90`, else `e20`. **Contemporary** = `d730`. Which window was
used is recorded.

**Economic floor** (`EconomicFloor`, versioned): minimum after-cost per-event effect of
0.10% / 0.25% / 0.40% / 0.60% for 1 / 5 / 10 / 20-bar horizons — roughly 1.5–2× typical
round-trip fee + slippage + funding drag. A +1 bp effect with t > 20 is not an edge (tested).

## 5. Recency weighting and inference

Weights `w = 0.5^(age / H)`, age = days since the outcome resolved, H ∈ {30, 90, 180, 365}
fixed. Each weighted estimate reports the weighted mean, its uncertainty, Kish's effective
sample size `ESS = (Σw)² / Σw²` (gate ≥ 20) and each age bucket's share of weight
(0–30, 30–90, 90–180, 180–365, 365–730, 730+ days). Lifetime unweighted results stay visible.

**Uncertainty** (`time_block_cluster_robust_v1`), for weights w (1 when unweighted):

```
mean        = Σ w x / Σ w
var_cluster = G/(G−1) · Σ_g (Σ_{i∈g} w_i (x_i − mean))² / (Σ w)²      g = 7-day signal blocks
var_iid     = n/(n−1) · Σ_i w_i² (x_i − mean)² / (Σ w)²
se          = sqrt(max(var_cluster, var_iid))
```

This is the simplest defensible choice: a sandwich variance that respects co-timed shocks
(Phase 18 showed iid errors are anti-conservative here), never below the weighted-iid
variance. `t = mean / se` is reported as a standardised effect; **no p-values** are computed
on weighted or rolling evidence. Limitations: large-sample approximation; few clusters in
short windows (hence the `max`); overlapping rolling windows are not independent evidence.

## 6. Edge states (`edge_state_v1`)

Sign class per estimate: `NA` (gates fail), `POS` (mean ≥ floor **and** t ≥ 1), `NEG`
(t ≤ −1), else `FLAT`. With L = lifetime, R = recent, S = short:

| State | Rule (first match) | Meaning |
|---|---|---|
| INSUFFICIENT | L = NA and R = NA | not enough evidence either way |
| DEAD | R = NEG; or R ∈ {FLAT, NA} and L ≠ POS | no credible edge now |
| DECAYING | R = POS and S = NEG; or R = FLAT and L = POS | historical edge weakening |
| STABLE | R = POS, L = POS, \|R − L\| ≤ max(floor, 0.5·\|L\|) | recent agrees with lifetime |
| EMERGING | R = POS and L ≠ POS | recent edge without lifetime support |
| ACTIVE | R = POS otherwise | recent edge, materially different from a positive lifetime |
| DORMANT | R = NA and L = POS | historical edge, no current sample |

These describe the apparent temporal state of an edge. They are not evidence tiers, co-pilot
or paper eligibility, or allocations.

### Recent-versus-lifetime divergence (first-class output)

`divergence.pattern` ∈ `emerging`, `strengthening`, `stable`, `decaying`, `historical_only`,
`reversing`, `consistently_absent`, `insufficient`, with the recent-minus-lifetime difference
and its standardised value (descriptive: the windows overlap). `historical_only` = lifetime
POS and neither recent nor contemporary POS; `decaying` = lifetime POS with the contemporary
window still POS but recent/short weak.

### Edge "half-life" (descriptive decay metrics, `lab_edge_decay_v1`)

No physical half-life is claimed. Each profile reports: recent 365 d versus older mean (and
standardised difference); peak rolling (`d180`) effect and days since the peak; decay from
the peak; slope of the rolling effect over the last year; consecutive monthly windows below
zero and below the floor; recency-weighted versus lifetime disagreement; and the observed
days from peak to half-peak (if it happened).

### Chronological evidence curve

`curve`: every 30 days from the first evaluation to the profile time, the `d180` window's
net mean, excess, events, assets, uncertainty, t, hit rate, MAE/MFE, cost contribution,
cumulative net and drawdown from peak — each point causal at its own date. Calendar-year
eras (fixed UTC years, no narrative eras) summarise structural non-stationarity.

### Continuous diagnostics

Recent effect, its uncertainty, evidence strength (t), `normal_approx_confidence_positive`
(Φ(t): a standardised summary, **not** the probability that the edge is real), economic
margin ((mean − floor)/se), decay score (contemporary vs short, standardised), current-regime
support, forward support (maturity, net mean, sample) and rolling slope. Lifecycle states
sit on top of these.

## 7. Change detection

| | Method | Causal? | Used by the lifecycle? |
|---|---|---|---|
| **Online** | `lifecycle_cusum_v1`: one-sided Page CUSUM on **resolution-week block means**, each block standardised by the mean and SD of all *earlier* block means; k = 0.5, h = 5 (in-control run length ≈ 900 blocks ≈ 18 years for Gaussian increments) | yes — a block is processed only once elapsed; appending later outcomes never changes an earlier alarm (tested) | yes, while participating, against the activation reference (§8) |
| **Retrospective** | `retrospective_binseg_v1`: binary segmentation of the whole series, BIC-style penalty 2·var·log n, ≤ 3 changes, ≥ 30 outcomes per segment | no — splits use outcomes after each point | never (a test makes it raise inside the simulation) |

Weekly blocks matter: co-timed outcomes share market shocks, so a per-event CUSUM read one bad
week as many bad events. Synthetic calibration exposed this (stable edges were cut ~1×/year
by false alarms) before any real-data run, and v1 was fixed accordingly (§11).

## 8. Lifecycle state machine (`lifecycle_machine_v1`)

```text
DISCOVERED → WATCH → ACTIVE_CANDIDATE → PAPER_ACTIVE ⇄ DEGRADED → DORMANT → RETIRED
                           ↑                                        │
                           └──────────── evidence returns ──────────┘
```

| State | Meaning |
|---|---|
| DISCOVERED | registered; no adequate evidence yet |
| WATCH | evidence adequate; activation not met |
| ACTIVE_CANDIDATE | activation met; must hold at 2 consecutive evaluations, else back to WATCH/DORMANT |
| PAPER_ACTIVE | **simulated** participation (the brief's name; it grants nothing to the real paper account) |
| DEGRADED | continuation failed; still participating; 4 consecutive failures → DORMANT; recovery → PAPER_ACTIVE |
| DORMANT | not participating, still observed (every signal's hypothetical outcome keeps feeding evidence); activation can be re-earned — a *reacquisition* |
| RETIRED | dormant ≥ 730 days with a credibly negative lifetime; terminal for this track |

Every transition is appended with its time, reasons and evidence; nothing is overwritten. A
strategy never graduates forever.

**Activation** (all, at 2 consecutive weekly evaluations):

- recent window adequate;
- recent mean ≥ economic floor;
- recent t ≥ 2.0;
- HL-90 weighted mean > 0 with ESS ≥ 10;
- ≥ 50% of assets positive;
- lifetime not credibly negative (lifetime t > −2, or lifetime inadequate);
- recent excess ≥ −floor (not materially worse than random timing).

**Continuation** (weaker — hysteresis): recent window adequate, recent mean ≥ 0 **and** recent
t ≥ 0.5, HL-90 mean ≥ −floor, no CUSUM alarm in the last 90 days. The CUSUM reference is the
one-standard-error lower bound of the recent mean at activation, floored at the economic floor
(activation selects a lucky-high window, so its point estimate overstates what to expect).

**Hard failure** (straight to DORMANT): recent t ≤ −2, or a CUSUM alarm with a negative recent
mean. **One bad trade never deactivates.** If a strategy stops producing adequate recent
evidence, continuation fails: status must be re-earned.

**Drawdowns.** A drawdown is never a reason on its own. It becomes decay evidence only through
degraded per-event expectancy (recent mean / t), repeated adverse outcomes (the CUSUM against
the activation expectation), a change in distribution, or hostile regime evidence (regime
mode). A test shows a 90% cumulative drawdown streak inside a still-positive expectancy keeps
the strategy active. Tails are always reported (§10).

**Modes of the causal walk-forward simulation** (evaluation every 7 days; at T only outcomes
resolved by T are read; a signal at s participates iff the state from the latest evaluation
≤ s is PAPER_ACTIVE or DEGRADED):

- **static** — always on;
- **recent** — one track on all outcomes;
- **regime** — one track per BTC-trend label (`up`/`neutral`/`down`) on that label's outcomes;
  a signal participates iff the track of its own entry regime participates.

Reported per mode: net sum and per event, max drawdown, active-time share, outcomes, turnover
(transitions per year), activations, deactivations, reactivations, retirements, spells (count,
mean/median length), short bursts (< 56 days), effect while participating versus while not,
missed upside, avoided losses, reacquisition success.

## 9. Regime-local evidence (`market_state_v1`)

Coarse, causal labels at each daily close of the venue's BTC:

| Dimension | Labels | Rule |
|---|---|---|
| trend | up / neutral / down | BTC close vs SMA(100), ±3% band |
| vol | low / normal / high | BTC 30 d realised vol, tercile of its trailing 730 d (≥ 365 obs) |
| breadth | risk_on / mixed / risk_off | share of venue coins above SMA(50): ≥ 2/3, ≤ 1/3 (≥ 3 coins) |
| funding | low / normal / high | cross-coin median 7 d Lab funding, tercile of trailing 365 d (≥ 180 obs) |

**Current-regime similarity:** exact match on coarse bins of (trend, vol). No classifier
decides anything. Each profile reports the current state, *current-regime historical*
evidence (all matching outcomes), *recent current-regime* evidence (`d365`, else `e50`), the
per-label dependence table and an evidence hierarchy, e.g.:

```text
lifetime: POS   recent: FLAT   current_regime_historical: FLAT   recent_current_regime: NA   forward: none
```

The regime-local lifecycle keys on trend only (v1), because samples per (trend × vol) cell are
too thin for activation.

## 10. Stress periods and pain

**Stress days** (`market_stress_v1`, BTC of the venue): |1-day return| ≥ 10%, 7-day return
≤ −25%, 7-day realised vol ≥ 3× its trailing 365-day median (prior days), or a missing daily
bar (discontinuity). A liquidation proxy is recorded as *unavailable* — Prism holds no
liquidation tape. An episode is a run of stress days plus the next 7 days. An outcome is
stress-affected if its holding window touched an episode.

Stress labels are **descriptive only**: no outcome is excluded from any decision, and every
profile reports all / normal / stress side by side (lifetime and contemporary statistics,
net sums, drawdowns, stress share). The full series is always the evidence (tested:
all = normal + stress; the clean series is never shown alone). Black-swan periods are never
deleted.

**Pain profile** (every profile, never a veto): max drawdown and recovery duration of the
equal-notional cumulative event P&L, worst event, worst resolution day and week, worst
stress-affected event.

Units: drawdowns and net sums add per-event returns on equal notional (no compounding, no
sizing); a drawdown of 2.0 means the sum of event returns fell 200% of one event's notional.

## 11. Synthetic calibration

`market lab edge calibrate --workers 8` (`lab_lifecycle_calibration_v1`, deterministic for any
worker count; 220 s with 8 workers, 1,087 s serial). Planted per-event after-cost edge on 5
assets × 6 years, σ = 10% per event (half common weekly shock, Student-t(5) tails), signal
probability 1/8 per asset-day declustered at the 10-day horizon (≈ 25 independent events per
asset-year), change at year 3, evaluation weekly after a 120-day warm-up. Null: 300 seeds;
each planted scenario: 100 seeds per edge size (1.5% and 3% per event). Recent-mode results
(regime mode in brackets):

**Null (no edge).**

| Metric | Recent [regime] |
|---|---|
| share of null outcomes participating | 10.6% (median 8.7%) [8.3%] |
| false activations per strategy-year | 0.22 [0.35] |
| false reactivations per strategy-year | 0.08 [0.08] |
| mean false-active spell | 175 days [240]; 62/300 runs never activated |
| false-positive strategy-years per 5.7 strategy-years | 0.60 [0.47] |
| transitions per year | 1.4 [2.4] |
| net of participating outcomes | −0.13 [−0.09] (≈ 0, as it should be) |

**Planted edges.**

| Scenario | Metric | edge 1.5% | edge 3% |
|---|---|---|---|
| stable | participation | 42% | 77% (spells ~780 d mean) |
| stable | deactivations / year | 0.53 | 0.31 |
| emerging | detection delay, median (p90) | 264 d (699) | 180 d (383) |
| emerging | edge outcomes missed before activation, median | 94 | 54 |
| emerging | already (falsely) participating at emergence | 17% | 17% |
| emerging | not detected by end (3 years) | 4% | 0% |
| emerging | planted edge captured | 39% | 68% |
| decaying | participating at the change | 47% | 80% |
| decaying | deactivation delay, median (p90) | 145 d (348) | 156 d (358) |
| decaying | outcomes after edge death participated, mean | 55 | 71 |
| decaying | dead-period participation | 17% | 22% |
| reversing | deactivation delay, median (p90) | 103 d (174) | 96 d (167) |
| reversing | net after reversal while still participating | −0.44 | −0.83 |
| reversing | net: static vs recent | −0.46 vs +1.34 | −0.97 vs +5.57 |
| regime | planted-regime capture, recent vs regime mode | 20% vs 22% | 30% vs 46% |
| regime | bad-regime participation, recent vs regime mode | 16% vs 7% | 23% vs 6% |

**Responsiveness versus noise** (recent mode, edge 3%, 60 seeds, descriptive; v1 keeps 2.0):

| Activation t | Null participation | Null activations / yr | Emerging median delay | Captured |
|---|---|---|---|---|
| 1.5 | 20.9% | 0.41 | 152 d | 74% |
| **2.0 (v1)** | **13.1%** | **0.26** | **180 d** | **70%** |
| 2.5 | 8.7% | 0.16 | 208 d | 65% |

Reading: with crypto-level noise the tradeoff is stark. Even a large planted edge (3% per
10-day event, a per-event Sharpe of 0.3) takes about six months to activate, and a decayed
edge keeps participating for about five months. Halving the edge roughly halves capture.
The null is not continually "discovered", but about one false activation every 4–5
strategy-years is the price of detecting real edges within a year. The system captures a
meaningful share of a temporary edge without chasing noise; it does not and cannot time
entries and exits precisely.

**Calibration before freezing.** Three changes were made on synthetic data only, before any
lifecycle computation touched real data: (1) the CUSUM reference became the one-SE lower bound
at activation (selection bias), (2) the CUSUM moved to weekly block means (co-timed shocks),
(3) continuation gained recent t ≥ 0.5 (false spells on the null were too long). Nothing was
tuned on the historical study.

## 12. Historical methodology experiment

Study `sstudy_6c52cc92…` (`config/lifecycle/phase20_edge_lifecycle.v1.yaml`), EXPLORATORY,
run on a scratch copy of the local DB. Canonical run `srun_a5856bb8…`, digest `550c2ba1…`
(the first run `srun_1678be6d…` differed only in how the report's example list displayed an
inadequate recent window; explicit, reasoned rerun). An explicit determinism rerun and a
fresh-copy registration + run reproduce the same study ID and digest exactly. All 128 perp variants of the nine v1
families are **fixtures, not candidates**; data stop at **2026-10-01**, so the Phase 9
validation window and final holdout are never read. Horizon 10 daily bars, floor 0.40%.

| | Binance (primary) | Hyperliquid (secondary) |
|---|---|---|
| Coins | AAVE BTC ETH LINK SOL | AAVE BTC ETH HYPE LINK SOL |
| History | bars 2019-09 →, funding 2019-09 → | funding 2023-10 → (bars from 2022-08 for warm-up) |
| Weekly evaluations | 326 (2020-07-06 → 2026-09-28) | 131 (2024-04-01 → 2026-09-28) |
| Signal rows / independent outcomes | 16,591 / 15,840 in span | 8,395 / 7,327 in span |
| Stress | 27 episodes, 59 stress days (COVID 2020-03, May 2021, LUNA 2022-05, June 2022, FTX 2022-11, Feb 2026, …) | 8 episodes, 20 days |
| Current regime at cutoff | trend up, vol normal, breadth risk-on, funding high | same |

**Edge states at the cutoff.**

| | DEAD | DORMANT | DECAYING | INSUFFICIENT | EMERGING | ACTIVE | STABLE |
|---|---|---|---|---|---|---|---|
| Binance | 87 | 22 | 15 | 2 | 1 | 0 | 1 |
| Hyperliquid | 77 | 12 | 5 | 24 | 6 | 3 | 1 |

Divergence: Binance `consistently_absent` 87, `historical_only` 30, `decaying` 7, `emerging`
1, `stable` 1; Hyperliquid `consistently_absent` 77, `insufficient` 24, `decaying` 11,
`historical_only` 7, `emerging` 6, `strengthening` 3.

**Static versus lifecycle use** (sums of per-event net over all strategies; the lifecycle
parameters were never fitted to these results):

| Binance | static | recent lifecycle | regime lifecycle |
|---|---|---|---|
| participating outcomes | 15,840 | 711 | 268 |
| net sum (per event) | −16.31 (−0.10%) | +2.37 (+0.33%) | +5.32 (+1.99%) |
| outcomes not participating: per event | – | −0.12% | −0.14% |
| avoided losses / missed upside | – | 186.9 / 168.2 | 189.5 / 167.9 |
| mean max drawdown per strategy | 2.66 | 0.16 | 0.06 |
| mean active-time share | 100% | 2% | 1% |
| activations / deactivations / reactivations | – | 33 / 33 / 8 | 20 / 23 / 4 |
| short bursts (< 8 weeks) / mean spell | – | 5 / 218 d | 5 / 229 d |
| strategies better than static | – | 63 / 128 | 63 / 128 |
| reacquisition success (spell mean > 0) | – | 21% | 33% |

| Hyperliquid | static | recent lifecycle | regime lifecycle |
|---|---|---|---|
| participating outcomes | 7,327 | 208 | 3 |
| net sum (per event) | −6.92 (−0.09%) | −2.66 (−1.28%) | −0.46 |
| activations / deactivations / reactivations | – | 15 / 16 / 2 | 2 / 1 / 0 |

What this shows:

- **The lifecycle almost never switches these catalogue strategies on.** On Binance only 2% of
  strategy-time was active; most strategies sit in WATCH (101) or DORMANT (21), and 4 were
  retired. This is consistent with Phases 17–19 and the Phase 7 batch: the catalogue holds
  little credible edge, and the gates refuse to manufacture one.
- **Static use loses** (−0.10% per event on Binance after costs). The lifecycle mostly avoids
  that (outcomes it skipped averaged −0.12%), but on Binance avoided losses (187) and missed
  upside (168) nearly cancel: switching off mostly side-stepped noise, not a distinct regime.
  The +0.33% (recent) and +1.99% (regime, 268 outcomes, mostly long breakouts) per
  participating event are small samples, not a demonstrated edge.
- **On Hyperliquid's short history the lifecycle lost** (−1.28% per participating event on
  208 outcomes): activations followed strength that did not persist — the expected failure
  mode when two-and-a-half years of history barely fill the windows.
- Long families dominate static P&L on Binance (e.g. `donchian_breakout` long +35.5,
  `volume_breakout` long +35.3 summed): 2019–2026 was largely a bull market and net includes
  beta. Every short family is negative on both venues.
- Only one strategy participates at the cutoff (Hyperliquid `funding_fade_a7_l90_p9_short`,
  simulated PAPER_ACTIVE since 2026-08-17 on 7 outcomes). It is **not** promoted; it is shown
  in the multi-edge research view with HL-90 edge +2.6% ± 1.8% and a 365-day drawdown of 0.33.
- Calendar-year eras (Binance): the share of strategies with a positive yearly mean ranged
  from 57% (2020) to 39% (2025) — non-stationary, with no stable winners.

## 13. Recent versus lifetime: where the conclusions differ

Neither list is promoted; they show what lifetime-only evidence misses.

**Poor lifetime, stronger recent** (lifetime ≠ POS, recent POS):

- Binance `trend_pullback_100_rsi7_40_long`: lifetime +0.09% (t 0.06, 194 outcomes) vs recent
  `e50` +3.03% (t 1.12) → EMERGING. Last 365 d +5.9% vs older −0.5% (standardised +1.4).
  Not activated: recent t is far below 2.
- Hyperliquid `ma_trend_10_50_long` (a Phase 8 cohort strategy): lifetime +1.26% (t 0.84) vs
  `d180` +4.54% (t 1.47) → EMERGING. The **same strategy on Binance is DECAYING /
  historical_only**: lifetime +1.63% (t 1.24, 210 outcomes) vs `e50` +1.13% (t 0.46); its
  rolling peak was 1,170 days before the cutoff. Its Hyperliquid profile carries the Phase 8
  tracking (`tracking_250f844a…`, TOO_EARLY, 0 outcomes as of the cutoff) — reported, not
  weighted.
- Hyperliquid `funding_fade_a7_l90_p9_short` / `_p95_short`: lifetime −1.08% / −1.97% vs
  `d180` +4.05% (t 2.22) / +4.31% (t 1.86). On Binance the `_p9_short` variant is DEAD
  (lifetime −2.7%, t −1.93): a venue- and period-specific recent effect.

**Historically strong, recently gone** (lifetime POS, recent not POS; 37 on Binance, 17 on
Hyperliquid):

- Binance `donchian_breakout_20_long`: lifetime +3.17% (t 2.26, 303 outcomes) vs `d180` +1.00%
  (t 0.32) → DECAYING / historical_only. Last 365 d −1.9% vs older +3.9% (standardised −2.0);
  rolling peak +15.4% in 2021, 2,040 days before the cutoff; yearly means +8.3% (2020),
  +10.0% (2021), −4.9% (2022), +2.9%, +1.7%, +0.4%, −0.7% (2026). The lifecycle activated it
  twice (one reacquisition) and it is DORMANT at the cutoff. Pain: worst event −48% (COVID),
  max drawdown 2.27, unrecovered for 706 days.
- Binance `donchian_breakout_30_long`: lifetime +3.29% (t 2.07) → recent `e50` −1.17%.
- Binance `donchian_breakout_100_long`: lifetime +4.25% (t 1.97) → only 6 recent outcomes →
  DORMANT (no current sample; not called dead).

## 14. Risk philosophy (documented, not implemented)

Three separate layers:

| Layer | Question | Phase 20 |
|---|---|---|
| **Edge lifecycle** | does this strategy currently deserve capital? | implemented as research |
| **Risk system** | how much exposure, given the edge and its pain profile? | unchanged (`paper_risk_policy v1`) |
| **Catastrophe system** | prevent ruin and software/system malfunction | requirements only (below) |

A strategy does not become "dead" because of a large but plausible drawdown; sizing and
exposure limits are the risk system's job.

### Catastrophe-control requirements (future; no live execution change in Phase 20)

The future layer must, independently of any strategy or lifecycle state:

- halt on corrupted, stale or impossible market data (non-positive/absurd prices, OHLC
  violations, timestamps out of order, feed age above a hard limit);
- detect and block runaway or duplicate orders (idempotency keys, per-interval order caps);
- stop on exchange/API malfunction or persistent execution failure (error-rate and latency
  thresholds, rejected-order streaks);
- enforce a hard absolute leverage / gross-exposure maximum that no policy can raise;
- flag account-equity discontinuities inconsistent with intended positions (reconciliation
  against the ledger) and stop until reconciled;
- provide an operator emergency kill that flattens or freezes, plus automatic de-risking
  that does not wait for a human.

**Human kill switch.** Live Prism will include an explicit operator kill switch. The automated
layer is not designed under the assumption that it must survive every conceivable
geopolitical, exchange or market catastrophe on its own; but automatic safeguards must still
prevent avoidable software/system ruin — they cannot rely on human reaction time.

## 15. Edge profiles, persistence and agent queries

A profile (`EdgeProfile`, schema 1) holds: strategy identity, venue, horizon, evaluation time
(`as_of`), data cutoff, source (study, run, result digest), outcome-ledger digest, policy ID,
methodology version, edge state, divergence, lifetime, every window, every weighting, research
modes, regime, stress, tails, eras, decay, change detection (online and retrospective,
labelled), continuous diagnostics, the simulated lifecycle (per mode, with history), the
curve, forward evidence and limitations. `profile_id` hashes the whole profile: same data,
cutoff and policy → same profile (tested). It has no alert/trade/sizing/leverage/approval
field (Phase 7 key scan).

`lab_edge_profiles` (migration 20, additive) stores profiles append-only; identical profiles
are not rewritten, later evaluations are new rows, and `edge_state` is CHECK-constrained.

**Queries** (read-only, `--json` for agents):

```text
market lab edge status <strategy>     # credible edge now? state, divergence, hierarchy, diagnostics
market lab edge history <strategy>    # the curve, eras, decay
market lab edge compare <strategy>    # lifetime / windows / weighted / regime / stress / forward
market lab edge lifecycle <strategy>  # simulated transitions (--mode static|recent|regime)
market lab edge list [--state EMERGING]
market lab edge calibrate [--workers N]
```

Writes: `market lab edge study register|run|report` (governed, scratch copies) and
`market lab edge profile build <run>` (appends the run's cutoff profiles with read-only Phase
8 evidence).

**AI-agent safety.** An agent asking "what currently appears exploitable?" gets structured
lifetime, recent, trend-of-effect, regime dependence, edge age (days since peak), decay,
forward status and simulated lifecycle state. It cannot choose dates or windows: there is no
such parameter, the windows are the frozen policy's, and every stored profile cites its policy
ID and source run.

**Prospective evidence.** Profiles consume Phase 8 summaries read-only (`forward_summary` as
of the profile time). v1 reports forward evidence and gives it **no** weight in the edge state;
eventually prospective evidence should outweigh retrospective rolling evidence, which a later,
versioned policy must define. The forward tracker is untouched.

## 16. Performance

| Run | Result |
|---|---|
| Historical study (128 strategies × 2 venues; 292,480 rolling evaluations; 24,986 signal rows) | 179 s wall, 430 MB peak (fresh copy, uncontended; Binance 122 s, Hyperliquid 55 s; ≈ 1 s per strategy-venue for three simulations, the edge-state timeline and the profile) |
| Profile build (256 profiles, forward lookup) | 4.4 s, 353 MB |
| Synthetic calibration (1,300 + 360 runs) | 220 s with 8 workers (1,087 s serial), 167 MB |
| One weekly evaluation of one strategy | ≈ 1 ms |

The result payload is 18.7 MB (≈ 62 KB per profile). Fast enough for repeated agent research
cycles; the per-strategy work is independent and parallelises trivially if needed.

## 17. Limitations

- Historical outcomes come from backfilled daily bars and funding (assumed available at close);
  nothing historical was observed live.
- Detection is slow by construction (§11); short histories (Hyperliquid) barely fill the
  windows, and the lifecycle lost money there.
- The effect unit is a fixed-horizon analytical return, not a simulated position (no stops,
  sizing or compounding); drawdowns are in event-notional units.
- Rolling windows overlap and the weighted inference is a large-sample approximation; there
  are no p-values and no multiple-testing correction across 128 strategies.
- Regime labels are coarse and BTC-referenced; trend-only regime tracks.
- Stress labels need a liquidation proxy that Prism does not have.
- Forward evidence is reported but unweighted in v1.
- Single fixed horizon per study (10 bars); daily perps only.

## 18. Next recommended phase

**Phase 21 — prospective edge monitoring.** Run the frozen v1 lifecycle forward, read-only:
on the always-on runtime, after each daily close, append a new edge profile for the Phase 8
cohort and a small preregistered set of catalogue strategies from live data only, so
lifecycle states are earned prospectively rather than reconstructed. Define (versioned, before
looking) how prospective outcomes enter the edge state, and keep paper/co-pilot policies
unchanged until that prospective record exists. Not begun.
