# Phase 21: fast prospective candidate incubation

> **Exploratory paper admission is intentionally permissive. False candidate activations cost
> no capital and are useful observations.**
>
> **No Phase 21 state authorizes real trading.**

Status: implemented 2026-10-05. Research and shadow records only. `copilot_policy v1`,
`autotrader_policy v1`, `paper_risk_policy v1`, the Phase 12 paper run, the forward tracker
and the Phase 20 lifecycle are unchanged, and no consumer reads any Phase 21 record (tested).

Code: `src/market_signal/research/incubation/` (`policy`, `evidence`, `machine`,
`opportunity`, `pool`, `synthetic`, `replay`, `inputs`, `prospective`, `events`), the CLI
`market lab incubation …` (`cli/incubation_cmds.py`), additive migration 21 (`incubation_*`
tables), one new runtime step and `tests/test_incubation.py` (23 tests).

## 1. Why paper admission differs from live eligibility

Phase 20 showed that the conservative lifecycle is right for capital and wrong for
experimentation: a planted +3%/event edge took ~180 days to activate, the historical
lifecycle was active ~2% of strategy-time, and the Phase 12 paper account has made no trade.

The two decisions have different costs:

| Decision | Cost of a false positive | Bar |
|---|---|---|
| Live capital (future) | real money | Phase 7–9 evidence, Phase 20 lifecycle, prospective record, risk + catastrophe layers |
| **Confirmed paper** (Phase 21, designed, unconnected) | attention | forward (shadow) outcomes only: ≥ 20, mean ≥ floor, t ≥ 1.5, ≥ 2 assets, not one asset, agrees with research, execution acceptable |
| **Exploratory paper** (Phase 21) | none — a useful observation | one frozen evaluation on recent evidence |
| Research evidence | — | unchanged |

They never share a threshold. Prism is **permissive about experimentation and strict about
promotion**: the global policies were not loosened; separate, frozen incubation policies
were added beside them.

## 2. CONSERVATIVE / BALANCED / AGGRESSIVE

Each is versioned and content-addressed; tests pin the IDs. They answer only *"should this
candidate generate prospective paper observations?"* (`grants_live: false`, a literal).

| Profile | Policy ID | Definition |
|---|---|---|
| CONSERVATIVE | `incpolicy_43561487…` | the **unchanged** Phase 20 lifecycle `lcpolicy_2fae77e8…` (recent mode, weekly cadence, t ≥ 2 at two consecutive evaluations, 4-failure continuation, CUSUM). PAPER_ACTIVE/DEGRADED → EXPLORATORY_PAPER, ACTIVE_CANDIDATE → WATCH. Its replay reproduces Phase 20's published results exactly (HL 15 activations, 208 outcomes, −1.28%/event; Binance 33, 711, +0.33%). |
| BALANCED | `incpolicy_aaec7841…` | 21-day window, ≥ 4 outcomes, ≥ 2 assets; admit at **t ≥ 2.0**; deactivate at t ≤ 1.0 |
| AGGRESSIVE | `incpolicy_c2f2fe35…` | 21-day window, ≥ 4 outcomes, ≥ 2 assets; admit at **t ≥ 1.0**; deactivate at t ≤ 0.0 |

**The small admission rule set** (BALANCED/AGGRESSIVE; all must hold at **one** evaluation):

1. sample: ≥ 4 independent outcomes from ≥ 2 assets resolved in the last 21 days;
2. after-cost mean ≥ the Phase 20 economic floor (0.40% per 10-day event);
3. t ≥ the profile threshold (+0.5 when an adequate lifetime is credibly negative, t ≤ −2);
4. not one asset: the mean without the best asset is ≥ 0;
5. not a timing artefact: causal excess over random entries ≥ −floor.

No cross-venue replication, no lifetime profitability, no long consistency. Lifetime is
context: a strong lifetime earns nothing (tested: lifetime t > 5, recent dead → not
admitted); a weak lifetime does not block (tested); a credibly negative one raises the bar
by 0.5 and never bans (tested).

**Design from synthetic data only.** Selection rules were written into
`synthetic.DESIGN_RULES` before the grid ran: AGGRESSIVE = maximise the captured share of a
30-day +3% edge subject to null participation ≤ 40%; BALANCED = maximise capture of a 60-day
+3% edge subject to null participation ≤ 15% and ≤ 4 null admissions/strategy-year. Grid:
window {21, 30, 45, 60} d × admission t. **Disclosure:** the first grid stopped at t = 1.5
and had no feasible BALANCED member (every configuration traded ≥ 18% of a pure null);
the grid was then extended to t ∈ {2.0, 2.5} — the constraints were not relaxed. The rules
selected `w21_t2` and `w21_t1`; a test checks the frozen values equal those members. The
historical replay ran afterwards and fed nothing.

| Grid (60 seeds) | null participation | null admissions / yr | 30-d edge captured | 60-d edge captured |
|---|---|---|---|---|
| w21 t 0.5 | 46.7% | 5.4 | 59% | 54% |
| **w21 t 1.0 (AGGRESSIVE)** | **34.8%** | **5.1** | **45%** | **43%** |
| w21 t 1.5 | 22.9% | 4.5 | 33% | 29% |
| **w21 t 2.0 (BALANCED)** | **14.3%** | **3.6** | **19%** | **21%** |
| w30 t 2.0 | 12.5% | 2.3 | 12% | 18% |
| w60 t 1.0 | 32.4% | 2.3 | 38% | 36% |
| w60 t 2.0 | 10.6% | 1.1 | 12% | 14% |

Shorter windows dominate at every noise level: a temporary edge must be seen while it lasts.

### Cadence and hysteresis

BALANCED/AGGRESSIVE evaluate at **every trigger-bar close** (daily for daily strategies) and
skip an evaluation whose window, episode and regime are unchanged — it could not change the
decision, and nothing evaluates faster than information arrives. There is **no weekly wait
and no second confirmation**: admission happens at the first daily evaluation meeting the
rule (tested by brute force against the rule). Hysteresis is one band — admission t ≥ 2.0 /
1.0, continuation while t > 1.0 / 0.0 — and the band is exercised (tested). Levels never
change twice on unchanged evidence (tested).

## 3. Levels, episodes, dormancy and reactivation

```text
INSUFFICIENT / NEUTRAL / WATCH --(one admission)--> EXPLORATORY_PAPER --(forward)--> CONFIRMED_PAPER
            ^                                             |                                  |
            +----------------- DORMANT <------(any deactivation)-----------------------------+
                                  +--(rule met again, with ≥ 1 new outcome)--> new episode
```

- **WATCH**: ≥ 3 outcomes in the window with a positive mean; not enough for paper.
- **EXPLORATORY_PAPER**: admitted; every independent signal becomes a shadow intent.
- **CONFIRMED_PAPER**: §7. **There is no LIVE state.**
- **DORMANT** (any one, fast — a zero-cost experiment needs no months of evidence to stop):
  window t at/below the exit level (evidence reversed); fewer than 2 outcomes in the window
  (signal conditions gone); ≥ 5 episode outcomes with t ≤ −1.5 (prospective evidence strongly
  adverse); the BTC trend regime changed since admission and the window's outcomes in the
  current regime are negative. Drawdown alone is never a reason.
- **Reactivation**: a dormant candidate re-enters when the frozen rule is met again and at
  least one new outcome resolved since it stopped. Each activation is a **new episode** with a
  deterministic ID `incep_` = hash(policy ID, strategy, admission time) recording admission
  time, policy, strategy, admission evidence and checks, lifetime at admission, deactivation
  time and reasons, confirmation time and the episode's outcomes. This is what lets later
  phases study edge lifespans and recurrence.

## 4. Temporary-edge calibration (synthetic)

`market lab incubation calibrate --workers 10` (`incubation_calibration_v1`; 177 s,
198 MB; identical for any worker count). Phase 20's generator (σ = 10%/event, half a common
weekly shock, t(5) tails, 5 assets, ≈ 8 independent events/month, 10-day horizon) with 540
null days of lifetime context, then the scenario, then 240 null days. 200 seeds per
scenario and edge size, 300 for the null. "New / already" splits a detection during the edge
into genuine detections and candidates that happened to be (falsely) admitted already.

**Detection speed and capture** (useful delay = admission while the edge still pays):

| Scenario | Policy | detected during edge (new / already) | useful delay, median (p90) | edge captured | deactivation delay, median | trades in 120 d after end |
|---|---|---|---|---|---|---|
| 14 d, +3% | CONSERVATIVE | 0% / 10% | – | 10% | 116 d | 3.8 |
| | BALANCED | 11% / 15% | 8 d (12) | 12% | 13 d | 4.7 |
| | AGGRESSIVE | 17% / 34% | 8 d (13) | 31% | 17 d | 12.2 |
| 30 d, +3% | CONSERVATIVE | 1% / 8% | 21 d | 9% | 82 d | 5.2 |
| | BALANCED | 40% / 13% | 18 d (27) | 21% | 11 d | 6.0 |
| | AGGRESSIVE | 40% / 36% | 15 d (26) | 41% | 22 d | 13.7 |
| 30 d, +6% | CONSERVATIVE | 1% / 8% | 21 d | 9% | 103 d | 6.5 |
| | BALANCED | 48% / 13% | 19 d (27) | 25% | 15 d | 6.9 |
| | AGGRESSIVE | 51% / 36% | 18 d (26) | 47% | 24 d | 14.9 |
| 60 d, +3% | CONSERVATIVE | 2% / 13% | 49 d (53) | 13% | 115 d | 7.6 |
| | BALANCED | 56% / 16% | 28 d (52) | 21% | 8 d | 5.9 |
| | AGGRESSIVE | 56% / 38% | 22 d (43) | 48% | 18 d | 14.2 |
| 60 d, +6% | CONSERVATIVE | 8% / 13% | 49 d (56) | 15% | 157 d | 13.3 |
| | BALANCED | 73% / 16% | 27 d (50) | 35% | 14 d | 7.1 |
| | AGGRESSIVE | 60% / 38% | 20 d (35) | 62% | 23 d | 15.8 |
| 90 d, +3% | CONSERVATIVE | 9% / 17% | 56 d (77) | 18% | 120 d | 11.1 |
| | BALANCED | 78% / 14% | 33 d (72) | 27% | 12 d | 6.0 |
| | AGGRESSIVE | 67% / 32% | 24 d (52) | 52% | 19 d | 13.4 |
| 90 d, +6% | CONSERVATIVE | 19% / 17% | 56 d (84) | 21% | 183 d | 19.0 |
| | BALANCED | 86% / 14% | 29 d (56) | 44% | 17 d | 7.5 |
| | AGGRESSIVE | 68% / 32% | 22 d (42) | 68% | 24 d | 14.6 |
| intermittent 3×30 d, +3% | CONSERVATIVE | 1% / 8% | 21 d | 16% (0.5 of 3 bursts) | 102 d | 9.7 |
| | BALANCED | 39% / 12% | 16 d (27) | 19% (1.3 bursts) | 14 d | 6.0 |
| | AGGRESSIVE | 44% / 32% | 17 d (27) | 40% (2.2 bursts) | 20 d | 13.5 |
| reversing 45 d +3% → 45 d −3% | CONSERVATIVE | 2% / 17% | 42 d | 16%; 19% of the losing period | 81 d | 6.0 |
| | BALANCED | 53% / 14% | 26 d (40) | 21%; 16% of the losing period | 12 d | 4.9 |
| | AGGRESSIVE | 57% / 32% | 22 d (38) | 44%; 37% of the losing period | 17 d | 11.2 |

**False activation (null, no edge anywhere).**

| | CONSERVATIVE | BALANCED | AGGRESSIVE |
|---|---|---|---|
| null signals traded (% of null periods) | 8.2% | 13.8% | 33.4% |
| false admissions / strategy-year | 0.19 | 3.5 | 5.0 |
| false-active spell, median | 141 d | 14 d | 24 d |
| shadow trades / strategy-year | 8.8 | 14.8 | 36.0 |
| net per null trade (≈ 0, as it must be) | −0.75% | −0.56% | −0.36% |
| false CONFIRMED per 2.6-year run | 0.06 | 0.00 | 0.15 |
| episodes shorter than 7 days per run | 0.00 | 1.4 | 0.7 |
| transitions / strategy-year | 1.3 | 10.0 | 11.5 |

Reading:

- **Weeks, not months.** Median useful detection is 15–33 days (BALANCED/AGGRESSIVE) versus
  50–180+ days for CONSERVATIVE. Deactivation takes 8–24 days instead of 75–180.
- **The floor is information, not policy.** With a 10-day horizon an outcome is unknown
  until 10 days after its signal, and the rule needs ≥ 4 resolved outcomes. At ≈ 8 events per
  month that puts the floor near 15–20 days. A **14-day edge is essentially uncapturable** at
  this information rate: its outcomes resolve after it has ended. AGGRESSIVE's 31% "capture"
  is mostly null luck (34% already admitted) — exactly its null participation.
- **30-day edges are the frontier**: ~40–50% of them are newly found while alive and
  AGGRESSIVE captures ~41–47% of their outcomes (BALANCED 21–25%). From 60 days, both find
  most edges; strength (+6%) matters more than policy.
- **Recurring edges** are re-found: AGGRESSIVE participates in 2.2 of 3 bursts as separate
  episodes; CONSERVATIVE catches half of one.
- **The price**: AGGRESSIVE trades a third of all null signals and admits ~5 false candidates
  per strategy-year. In a 128-strategy pool that is hundreds of false admissions a year —
  deliberately tolerated, because they cost nothing and are observed.

### Edge-lifetime response surface

What kinds of temporary edges can Prism capture? 90 cells (lifespan 14/30/60/90/180 d ×
edge 1.5/3/6% × frequency × σ 5/10%), 25 seeds each; full table in the calibration report
(`response_surface`). Frequency: *low* = 3 assets, ≈ 3.5 events/month; *base* = 5 assets,
≈ 8/month; *high* = 20 assets, ≈ 43/month. Cells read *admitted while the edge was alive
(including candidates already admitted by null luck) / share of edge outcomes captured*;
σ = 10%.

| Edge (σ 10%) | Freq | 14 d | 30 d | 60 d | 90 d | 180 d |
|---|---|---|---|---|---|---|
| +3%, AGGRESSIVE | low | 8% / 8% | 32% / 16% | 48% / 15% | 56% / 15% | 80% / 22% |
| +3%, AGGRESSIVE | base | 52% / 32% | 80% / 47% | 96% / 48% | 100% / 58% | 100% / 58% |
| +3%, AGGRESSIVE | high | 48% / 30% | 72% / 35% | 100% / 51% | 100% / 60% | 100% / 63% |
| +3%, BALANCED | base | 20% / 4% | 44% / 18% | 84% / 29% | 96% / 24% | 100% / 30% |
| +3%, BALANCED | high | 24% / 9% | 48% / 16% | 84% / 30% | 100% / 43% | 100% / 44% |
| +3%, CONSERVATIVE | base | 16% / 16% | 28% / 22% | 32% / 21% | 28% / 21% | 48% / 26% |
| +6%, AGGRESSIVE | base | 52% / 32% | 84% / 53% | 100% / 62% | 100% / 69% | 100% / 75% |
| +6%, BALANCED | high | 24% / 9% | 64% / 23% | 100% / 52% | 100% / 61% | 100% / 68% |

- **Strength and volatility act through their ratio** (per-event Sharpe): +3% at σ 5% behaves
  like +6% at σ 10% throughout the surface.
- **Signal frequency is the binding constraint.** At *low* frequency nothing under 60 days is
  reliably captured by any policy. *High* frequency mostly helps BALANCED at 60–180 days; at
  14–30 days the common weekly shock and the 10-day horizon still dominate (more assets do
  not remove a shared market shock).
- **Lifespan relative to the horizon decides capturability**: an edge must outlive horizon +
  sample-accumulation time (≈ 3 weeks here) to be worth anything. Faster edges need shorter
  horizons / intraday strategies, which a later phase would have to test.

## 5. Opportunity rate (a diagnostic, never a target)

Per policy (`opportunity`): candidate signals/day, paper-admissible signals/day,
**independent opportunities/day** (distinct asset × side × UTC day among admissible signals,
so correlated variants firing together count once), median days between opportunities and
the share of days with none. Always reported beside expectancy, uncertainty, drawdown, false
activation and costs; no policy is ranked by frequency.

## 6. Retrospective diagnostics (historical replay; NOT tuning)

`market lab incubation replay` (`incubation_replay_v1`): the three frozen profiles replayed
causally over the frozen 128-strategy pool to the 2026-10-01 cutoff on a scratch copy of the
local database (backfilled data, assumed availability at the close). 77 s, 382 MB. Digest
`c649db93…`, reproduced by an explicit rerun through the CLI. Thresholds were frozen before
it ran.

| Hyperliquid (913 days from 2024-04-01) | CONSERVATIVE | BALANCED | AGGRESSIVE |
|---|---|---|---|
| strategies ever admitted / 128 | 13 | 90 | 103 |
| admissions (reactivations) | 15 (2) | 230 (140) | 341 (238) |
| mean active share of strategy-time | 1.4% | 3.5% | 6.0% |
| shadow trades (per year) | 208 (83) | 459 (184) | 765 (306) |
| net per trade ± se | −1.28% ± 1.38% | −1.17% ± 1.33% | −0.66% ± 1.30% |
| net sum (event-notional units) / pool max drawdown | −2.66 / 5.2 | −5.39 / 11.2 | −5.03 / 15.1 |
| mean cost + funding per trade | 0.32% | 0.33% | 0.37% |
| independent opportunities / day | 0.20 | 0.29 | 0.46 |
| days with zero opportunities | 88% | 82% | 74% |
| median days between opportunity days | 2 | 2 | 2 |
| median episode length / episodes < 14 d | 112 d / 0 | 19 d / 68 | 21 d / 63 |
| episodes with a negative realised mean | 6 / 15 | 86 / 230 | 126 / 341 |
| WATCH → admission, median | 7 d | 6 d | 4 d |
| CONFIRMED episodes | 1 | 0 | 0 |
| transitions / strategy-year | 0.5 | 5.0 | 4.8 |

| Binance (2,278 days from 2020-07-06) | CONSERVATIVE | BALANCED | AGGRESSIVE |
|---|---|---|---|
| ever admitted / admissions | 25 / 33 | 112 / 429 | 119 / 672 |
| active share | 2.5% | 2.7% | 4.7% |
| trades (per year) | 711 (114) | 744 (119) | 1,294 (208) |
| net per trade ± se | +0.33% ± 1.12% | +1.09% ± 1.70% | +0.48% ± 1.32% |
| independent opportunities / day; zero-opportunity days | 0.23; 85% | 0.17; 89% | 0.30; 81% |
| CONFIRMED episodes | 2 | 0 | 0 |

Static use of every signal: −0.09% (HL, 7,327) and −0.10% (Binance, 15,840) per event.

What this shows (and does not):

- **No policy found an edge in this catalogue.** Every per-trade mean is within about one
  standard error of zero; Hyperliquid is negative for all three. Speed does not manufacture
  edge, and these numbers must not be used to pick a policy.
- **The catalogue's information rate is the real bottleneck.** The 128 strategies produce
  ≈ 8 candidate signals per day *in total* — ≈ 0.06 per strategy per day, ≈ 2 per month,
  *below* the synthetic "low" frequency. Most 21-day windows hold fewer than 4 outcomes, so on
  real data even AGGRESSIVE was active only 6% of strategy-time (vs 33% on the synthetic
  null), and 74–88% of days had no admissible opportunity.
- **Fast episodes rarely graduate.** BALANCED/AGGRESSIVE episodes last ~3 weeks — too short
  to accumulate 20 forward outcomes — so CONFIRMED_PAPER essentially never triggers.
  Promotion stays strict by construction; it is not a dial.
- Many short bursts (63–99 episodes under two weeks) and many negative episodes: the
  expected texture of permissive admission on noise.

## 7. Candidate graduation (designed, frozen, not connected)

`incubation_graduation_v1`, applied to **forward outcomes only** (in live operation: the
current episode's recorded shadow outcomes; retrospective outcomes never count):
≥ 20 resolved outcomes, ≥ 2 assets, mean ≥ floor, t ≥ 1.5, mean without the best asset > 0,
same sign as the admission evidence, and missed/unavailable intents ≤ 20%. CONFIRMED falls
back to EXPLORATORY if the forward mean turns negative; any deactivation ends it. Nothing
reads CONFIRMED_PAPER: it does not enter the paper account, the co-pilot or any risk policy.
There is no LIVE level.

## 8. Directional balance (long/short)

The pool is symmetric by construction: every v1 family file authors mirrored long and short
rules (1 − tail, 100 − level, crosses_below for crosses_above). Pool: **64 long / 64 short**;
by style trend 7/7, breakout 21/21, pullback 8/8, mean reversion 12/12, funding 16/16; no
asymmetric family (tested). No short variant was fabricated and none is penalised: admission
and the machine contain no side-dependent branch (tested), outcomes are side-signed (a short
profits when price falls), and a short strategy on a falling market is admitted exactly like
a long one on a rising market (tested through the real compiler).

Replay activity by side:

| | candidate signals L / S | admissions L / S | admissible signals L / S | net per trade L / S |
|---|---|---|---|---|
| HL CONSERVATIVE | 3,942 / 3,385 | 13 / 2 | 178 / 30 | −1.51% / +0.09% |
| HL BALANCED | | 139 / 91 | 306 / 153 | −0.88% / −1.76% |
| HL AGGRESSIVE | | 206 / 135 | 532 / 233 | −0.26% / −1.56% |
| Binance BALANCED | 8,268 / 7,572 | 245 / 184 | 511 / 233 | +3.11% / −3.36% |
| Binance AGGRESSIVE | | 385 / 287 | 896 / 398 | +1.80% / −2.47% |

Short signals are 46–48% of candidate signals and 40–43% of BALANCED/AGGRESSIVE admissions,
but only 13% (HL) and 21% (Binance) of CONSERVATIVE admissions: its long windows integrate
the 2019–2026 bull-market drift, so it behaves close to long-only. The fast policies judge
shorts on recent evidence and admit them almost in proportion. The remaining gap is
evidence (short outcomes were worse after costs and funding), not construction.

## 9. Shadow paper (prospective)

For every EXPLORATORY/CONFIRMED candidate, each independent signal at a live bar becomes a
**shadow intent** (`shadow_execution_v1`): direction; entry = open of bar T+1; exit = close of
bar T+10 (the research effect unit; no stop); **fixed standardised notional $1,000, 1×, no
compounding, never optimised per strategy**; frozen per-coin taker fee + slippage (HL: 4.5 bps
+ 2–8 bps per side); causal daily funding over T+1..T+10; one open intent per (policy,
strategy, asset); an intent recorded more than 12 h after the bar close is
`missed_entry_window`. The outcome is written once when final: `net = gross − round-trip
cost − funding`, `pnl_usd = net × notional` (hand-checked in tests), or `unavailable` after
a 7-day wait for missing bars/funding (never zero-filled). No order, no exchange, no account:
the Phase 12 paper tables are never read or written by the incubation package (tested).

## 10. Prospective evidence and immutability

Each daily candidate snapshot records signal assets, era evidence (**lifetime, last 730 /
365 / 180 / 90 days, the policy windows**, never averaged) and three separate blocks:
**retrospective** (signals before the freeze), **prospective candidate outcomes** (every
signal after the freeze, admitted or not) and **shadow** (recorded intents' outcomes, per
episode and overall). Forward data never enter a historical statistic silently; there are no
p-values.

Rules (as the Phase 8 forward tracker): only bars closing after the freeze; a bar is
evaluated only within one day of its close, never backfilled (a gap); deterministic IDs and
UNIQUE keys make every (freeze, strategy, bar) evaluation, every decision, transition,
intent and outcome write-once; nothing is updated or deleted; the decision at T is the
policy's causal replay over outcomes resolved by T, so later data cannot change it (tested by
corrupting every later outcome, and in the database across 20 runs).

Measured on a scratch copy (128 candidates × 3 policies, HL to 2026-10-04): under a minute
and ≈ 360 MB for the first run of a day; later runs that day are no-ops.

## 11. The prospective freeze (what is locked)

`market lab incubation freeze --reason …` registers `IncubationFreeze` (`incfreeze_…`, a
hash of everything below); prospective collection starts at its registration time:

- candidate pool rule `incubation_pool_rule` v1 + all 128 member strategy IDs (`pool_id`):
  every perp variant of the nine v1 catalogue families, both sides, no performance-based
  selection; Phase 17/18/19 hypotheses are not catalogue families and are excluded;
- the three policy definitions and IDs (admission, deactivation, graduation, WATCH);
- Hyperliquid, AAVE/BTC/ETH/HYPE/LINK/SOL, bars from 2022-08-01, funding from 2023-10-01,
  replay from 2024-04-01, BTC as the regime reference, 10-bar horizon;
- frozen per-coin costs, `shadow_execution_v1`, cadence `each_trigger_bar_close`;
- semantic versions (compiler, vocabulary, machine, evidence, runner, resolver, …): a change
  stops evaluation of that freeze until a new one is registered.

Registering the same definition twice is refused (its start cannot move). **Once collection
begins these policies are not tuned on early results; a change is a new policy version and a
new freeze.**

**Not yet frozen on the live database.** The runtime step is deployed with the code but does
nothing until an operator runs, on the runtime after deploying:
`railway ssh --service prism-runtime -- market lab incubation freeze --reason "Phase 21 prospective start"`.

## 12. Existing paper account (conservative baseline)

The Phase 12 run `paperrun_27b0a336…` (live, read-only check 2026-10-05): ACTIVE, HEALTHY,
1 bar processed, 14/14 cycles OK, **0 trades, 0 intents, equity $10,000**. It is not
restarted, rewritten or forced to trade. `market lab incubation status|compare` report it
read-only beside the shadow policies.

## 13. Simple strategies and the volume + compression audit

The incubation layer is strategy-agnostic: any Lab `StrategyDefinition` can be a candidate;
nothing depends on multi-factor Strategy Lab studies.

Bounded audit of *"volume rising while price is compressed, then a directional breakout"*
(scratch copy, no profitability study):

| Question | Finding |
|---|---|
| Does OHLCV support it? | Yes. Daily perp bars carry volume on both venues. Hyperliquid has **zero-volume placeholder bars before each coin's listing** (to 2023-02-26 BTC/ETH, 2023-03-04 SOL, 2023-05-18 LINK, 2023-07-17 AAVE); none after 2023-10-01. Any HL volume feature must start after listing. |
| Is volume comparable across venues? | **Absolute: no** — HL volume is a median 3–9% of Binance's and the ratio swings 0–25%. **Relative: yes** — daily log-volume changes correlate 0.73–0.87 and 20-day relative volume 0.72–0.81 across venues. Use relative-volume features only. |
| Compression | ✓ `rvol_10 lt rvol_60` (realised-vol ratio) or `atr_pct_n`/`rvol_n` against a number. ✗ no **range-width** feature ((high_n − low_n)/close); conditions compare feature to feature or to a number, with no scaling. |
| Range position | partial: `dist_donchian_high_n` / `dist_donchian_low_n`; ✗ no normalised position (close − low_n)/(high_n − low_n). |
| Volume trend | ✓ `vol_sma_5 gt vol_sma_20` (feature vs feature). |
| Relative volume | ✓ `rel_volume_n`, `vol_z_n`. |
| Breakout direction | ✓ `close crosses_above donchian_high_n` / `crosses_below donchian_low_n`. |

The three-condition hypothesis (`rvol_10 < rvol_60`, `vol_sma_5 > vol_sma_20`, close crosses
the 20-day Donchian band) compiles today, long and short. It fires about 2–3 times per
coin-year (HL 51 long / 43 short signals over three years; Binance 111 / 71 over seven) —
another low-frequency daily hypothesis. A future simple-strategy phase should add
`range_width_n` and `range_pos_n` to a **new vocabulary version** (v1 is pinned) and consider
intraday triggers for frequency.

## 14. Future external-event integration (design only)

`events.ExternalEvent` (`external_event_v1`) defines the interface a future monitor (e.g. an
AI agent) would feed: category (hack, exchange failure, regulatory, listing, delisting,
governance, upgrade, ETF/treasury, other), assets, headline, event time, published time,
**first-seen time**, expiry, confidence and provenance (source, reference, recorder, raw-payload
hash). **Availability is derived, not supplied: `available_at = first_seen_at`.** An item
published at 09:00 that Prism first saw at 15:00 can inform nothing before 15:00; supplying
`available_at` is rejected, and expiry must follow first sight (tested). Nothing is ingested,
stored or traded, and no event → direction mapping exists ("hack → short", "listing → long"
are hypotheses for their own preregistered study). An event-derived strategy would plug into
the pool as one more candidate. `market lab incubation event-schema` prints the schema.

## 15. AI-agent interface

```text
market lab incubation candidates [--policy balanced] [--level EXPLORATORY_PAPER] [--side short] --json
market lab incubation candidate <strategy>        # eras, retrospective/prospective/shadow, every policy, episodes
market lab incubation status --json               # freeze, levels by policy × side, last run, paper baseline
market lab incubation opportunities --json
market lab incubation compare --json              # no winner is selected
market lab incubation episodes [--policy …] --json
market lab incubation policies | pool | event-schema
market lab incubation calibrate | replay --out …  # synthetic / retrospective diagnostics
market lab incubation freeze --reason … | run     # writes (append-only)
```

"What can we cook right now?" is `candidates --json`: admitted and WATCH candidates first,
each with structured fields — why admitted (passing checks), failed checks, direction, recent
expectancy, t, sample size, assets, economic floor, lifetime context, current regime, the
episode and its admission evidence, prospective shadow observations, and rule-generated
caveats (small sample, negative lifetime, concentration, no shadow outcomes yet). No window,
threshold or date can be passed (tested: `--window` is rejected).

## 16. Live compatibility

No order path, venue client, key, sizing of the real paper account or risk policy was
added. The incubation package imports no paper, co-pilot, ops, Telegram or HTTP code and
issues no UPDATE/DELETE; paper, co-pilot, forward and Phase 20 code do not import it;
`grants_live` is a literal `False` and a database CHECK; the only runtime addition is
`lab incubation run`, which writes `incubation_*` rows (tested).

## 17. Limitations

- Information rate: daily catalogue strategies are too sparse for edges shorter than about a
  month; the fast policies are mostly limited by data, not by thresholds.
- False activity is large by design, and multiplied by 128 strategies × 3 policies.
- Synthetic calibration uses one noise model; the response surface has 25 seeds per cell.
- The retrospective replay uses backfilled data and a single 10-bar horizon; it is diagnostics.
- Graduation rarely triggers for 3-week episodes; whether CONFIRMED needs a separate, slower
  track is open (§18).
- Hyperliquid only for prospective collection (the venue the runtime ingests daily).

## 18. Next recommended phase

**Phase 22 — prospective incubation operations and first-look review.** Deploy, register the
freeze on the runtime, and run the frozen Phase 21 incubation forward unchanged for a fixed,
preregistered period (e.g. 8 weeks), with a weekly read-only report of opportunity rate,
shadow outcomes by policy and side, episode lifespans and false-looking activity against the
synthetic expectations — no threshold changes, no paper/co-pilot integration. Not begun.
