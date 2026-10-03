# RESULTS — what the research actually found

## 1. Status

> **Update:** the perpetual-futures research has now been run on real data (Hyperliquid,
> plus Binance for unseen years). See §9. The spot-setup verdicts in §7 are still pending.

**No real-market research result exists yet.** The build environment's egress policy
blocked every market-data host (Coinbase, Hyperliquid, Bitstamp, Kraken, Binance, Tiingo,
Stooq, Yahoo, FRED/ALFRED, EIA, SEC EDGAR, DefiLlama, CBOE). Each attempt was logged as a
failed ingestion run.

I did not substitute mirrored or scraped data. Doing so would have broken the
provenance and no-stitching rules this project is built on. I am not going to report
numbers I do not have.

What *has* been established:

1. The research engine is **valid**: it finds planted edges and rejects noise (§3).
2. The engine's **statistical power** on samples of V1's size is quantified (§4).
3. The experiments and verdict rules are **pre-registered** (committed before seeing real
   data) (§5).
4. A single command produces the real results on a networked machine (§6).

## 2. How to produce the real results (about 10 minutes, £0)

```bash
cp .env.example .env              # add free TIINGO_API_KEY, FRED_API_KEY, SEC_USER_AGENT (EIA optional)
uv run market update              # prices, macro (ALFRED vintages), EDGAR facts, crypto fundamentals
uv run market doctor              # confirm freshness, no failed runs, no schema drift
uv run market research            # all experiments → results/RESULTS_generated.md (+ per-experiment reports)
```

Then replace §7 of this file with the generated verdict table and the interpretation
checklist in §8. The generated file has the per-class, per-regime, walk-forward and
sensitivity detail for every experiment.

## 3. Engine validation (what was actually tested)

| Test | What it proves | Result |
|---|---|---|
| Planted one-bar-ahead edge (`test_planted_edge_is_detected_at_correct_horizon`) | The engine *can* find an edge, at the right horizon | detected, p < 0.01 |
| Random signals on random walks (`test_random_signals_show_no_edge`) | The engine does not manufacture edges | excess ≈ 0, p > 0.01 |
| Nightly-gap series (`test_same_close_fill_is_impossible`) | No same-close fills | gap not captured |
| Truncation invariance: features, regimes, setups A/B, simulator | No look-ahead | identical |
| Future-shock invariance | Past features ignore future prices | identical |
| ALFRED vintage + EDGAR restatement tests | Revised data is not back-dated | correct as-of values |
| µs/ns timestamp regression | PIT joins compare the right instants | fixed a real bug found in development |

### Full pipeline on the SYNTHETIC demo DB

The demo DB is 18 random-walk assets with regime-switching drift, a 2-year HYPE history and
synthetic EDGAR-shaped facts. No exploitable structure exists by construction, so the
correct outcome is "no edge" everywhere:

| experiment | indep. events | excess vs random entry | p | MDE 80% | WF OOS (default) | sensitivity | verdict |
|---|---|---|---|---|---|---|---|
| quality_pullback | 243 | −0.04% | 0.53 | 2.9% | −3.14% | NO_EDGE | REJECT |
| quality_pullback_fundamental | 82 | −1.09% | 0.83 | 2.7% | −3.85% | NO_EDGE | REJECT |
| breakout_retest | 226 | +1.20% | 0.15 | 3.1% | +0.44% | PLATEAU | REJECT |
| rerating (3m) | 23 | −0.59% | 0.56 | 8.2% | +3.23% (7 ev.) | INSUFFICIENT | INSUFFICIENT_DATA |
| control_trend_only | 227 | −0.57% | 0.68 | 2.5% | −1.79% | – | REJECT |
| conditions_example | 106 | −1.83% | 0.88 | 3.5% | −3.38% | – | REJECT |

The engine rejected all of them, which is correct. Note `breakout_retest`: by chance it
showed +1.2% excess, a "plateau", and a positive out-of-sample number. A naive backtest
would have called that an edge. It was rejected because random-entry timing matches it
15% of the time. This is the kind of false positive the verdict rules exist to stop.

## 4. Statistical power: what V1 *can* and *cannot* detect

At the primary horizon (1 month), with about 100–250 independent events pooled across the
universe, the minimum detectable excess at 80% power is about **2.5–3.5% per month**.
Edges smaller than that will show as REJECT/INCONCLUSIVE even if they are real.

- **Setups A and B** can detect only large edges. A genuine +1%/month edge would usually be
  missed. The honest conclusion from a REJECT is therefore "no *large* edge", not "no edge".
- **Setup C** (re-rating) covers 6 equities over about 15 years. A 3-month horizon with a
  42-bar cooldown gives roughly 20–40 independent events, so the MDE is about **8% per
  quarter**. Expect `INSUFFICIENT_DATA` or a very wide interval. That is a limitation of
  free data and a small universe, not evidence in either direction.
- **HYPE** has about 2 years of price history, so it cannot be tested on its own. Its
  events contribute to the pooled crypto sample only.

## 5. Pre-registered hypotheses and rules (committed before real data)

| ID | Hypothesis | Experiment | Primary horizon | Must beat |
|---|---|---|---|---|
| H1 | Quality Pullback has positive excess over random entry | `quality_pullback` | 1m | random entry *and* `control_trend_only` |
| H2 | PIT fundamentals improve Quality Pullback on equities | `quality_pullback_fundamental` | 1m | the equity slice of H1 |
| H3 | Breakout + Retest has positive excess | `breakout_retest` | 1m | random entry |
| H4 | Re-rating/dislocation beats random entry on equities | `rerating` | 3m | random entry |
| C0 | Trend-only control | `control_trend_only` | 1m | (reference) |

Verdict rules (docs/BACKTESTING.md §7) are applied mechanically.

- **REJECT** if excess ≤ 0, or p ≥ 0.10, or default parameters fail out-of-sample.
- **PROMISING** needs p < 0.05, a parameter plateau, a positive walk-forward, and
  cross-asset consistency.
- Any setup that does not beat **C0** is not a pullback edge, whatever its own verdict. Its
  returns are explained by the trend.

Commitments:

- **No tuning after seeing results.** If a setup is rejected, it is rejected. Do not edit
  thresholds and re-run until it passes.
- Any follow-up variant must be a *new, named* experiment, and every run is logged in
  `research_runs` so the number of attempts is visible.

## 6. What the scanner does in the meantime

The scanner, scores and HYPE model work on current data and are useful as
decision-support without a proven edge. Until a setup earns PROMISING:

- statuses are advisory;
- sizing stays at the "standard" tier (0.5% risk);
- the dashboard shows each setup's research verdict next to every signal.

## 7. Real-data verdicts

*Pending: run `uv run market research` and paste `results/RESULTS_generated.md` here.*

## 8. Interpretation checklist for the real run

When the real run comes back, answer each question from the generated reports:

1. Which setups have positive excess *and* p < 0.10 at 1m? On which asset classes (by-class
   table)?
2. Over which periods: which walk-forward folds are positive? Is the edge concentrated in
   one bull run (for example crypto 2020–21)?
3. Where did they fail: splits by regime / trend / volatility / rate cycle?
4. Parameter sensitivity: PLATEAU, MIXED or FRAGILE?
5. Drawdowns and MAE: is the worst MAE survivable at the configured risk?
6. Sample sizes against the MDE: is a REJECT informative, or just underpowered?
7. Does Quality Pullback beat the trend-only control?
8. Which ideas should be discarded? Anything REJECTED is removed from live scoring
   (set `setup_required_for_actionable` to exclude it) rather than re-tuned.

## 9. Perp research: what we tested and found

*Run on real data, October 2026. Engine and execution model: `docs/PERPS_BACKTEST.md`.*

### 9.1 What was tested

Three strategies were **pre-registered** in `perps/strategies.py` before any real perp data
was examined. All three are long/short on BTC, ETH, SOL, HYPE, LINK and AAVE, using daily bars:

| Strategy | Idea | Defaults | Primary horizon |
|---|---|---|---|
| `trend_ls` | Trade in the direction of the 50/150 trend on a 20-day breakout | stop 3 ATR, max hold 30d | 1m |
| `funding_fade` | Fade crowded positioning: funding above its 95th percentile (1y lookback, 7d average) and rolling over | stop 2.5 ATR, max hold 14d | 2w |
| `breakout_ls` | Break out of a tight 30-day base (≤ 8 ATR wide), in either direction | stop 2 ATR, max hold 21d | 1m |

How each strategy was judged:

- **Edge** comes from an event study on notional. Returns are side-aware and net of taker fees, slippage
  and **actual funding paid or received**. Each trade is compared with a *same-side*
  random-entry baseline, so a long strategy isn't credited for a bull market.
- **Verdicts** come from the automatic rules in §5. PROMISING needs at least 30 independent events,
  p < 0.05, positive walk-forward folds and a PLATEAU in parameter sensitivity.
- **Risk** is modelled with an isolated-margin simulator. Leverage is an output (risk 0.5% per
  trade, cap 3×, liquidation kept ≥ 2× the stop distance away), and the path order is
  checked: liquidation before stop.
- **Two venues, never mixed.** Hyperliquid covers its own history. Binance USD-M was then used only
  for the **years before** each coin's Hyperliquid history begins (per coin, up to
  2023-10-04). The strategies were not changed between the two runs.

### 9.2 Hyperliquid (in-sample period)

| Strategy | Verdict | Indep. events | Excess (primary) | p | Walk-fwd + | Max DD (sim) | Liquidations |
|---|---|---:|---:|---:|---|---:|---:|
| trend_ls | REJECT | 110 | +3.6% | 0.057 | 0/3 | −6.1% | 0 |
| funding_fade | REJECT | 54 | −0.6% | 0.578 | 1/3 | −4.0% | 0 |
| breakout_ls | REJECT | 145 | −0.6% | 0.624 | 1/3 | −12.1% | 0 |

### 9.3 Binance: unseen years (2019/20 → 2023-10-04, per coin)

| Strategy | Verdict | Indep. events | Excess (primary) | p | Walk-fwd + | Max DD (sim) | Note |
|---|---|---:|---:|---:|---|---:|---|
| trend_ls | REJECT | 109 | +3.8% | 0.158 | 2/6 | −7.8% | No out-of-sample excess at the default params |
| funding_fade | REJECT | 74 | −1.6% | 0.724 | 4/6 | — | Positive on only 22% of assets |
| breakout_ls | REJECT | 149 | +4.2% | 0.091 | 3/6 | −13.9% | Sign flipped from Hyperliquid; no OOS excess at the defaults |

### 9.4 What it means

- **`funding_fade` is closed.** It is negative in both eras and on most coins. Extreme funding
  alone did not predict reversals after costs and funding.
- **`breakout_ls` is inconsistent.** It was −0.6% on Hyperliquid and +4.2% on Binance. An edge that
  changes sign between eras is not an edge that can be traded.
- **`trend_ls` is the only recurring signal.** It averaged about +3.7% per trade in *both* eras,
  but it fails stability (walk-forward 0/3 and 2/6), and the p-values (0.06 and 0.16) don't clear the
  bar. The most likely explanation is a few big trending periods carrying the average.
  That is how trend-following is *supposed* to work, but it is also indistinguishable from luck
  at this sample size.
- **Risk was never the binding constraint.** There were zero liquidations, and the drawdowns were modest. The
  verdicts are about the *direction* of returns, not the sizing (see 9.6).

### 9.5 Caveats and bugs fixed before these runs

- **Fixed-horizon exits.** Every strategy is scored at a fixed horizon (1m/2w). That handicaps
  trend-following, which earns from letting winners run. A trailing-exit variant is a *new*
  hypothesis, not a re-tune.
- **The strategies are generic by design.** They are the textbook versions, chosen so that a pass would mean something.
- **Bugs that affected earlier (discarded) runs:**
  - Binance funding timestamps carry millisecond jitter, which pushed midnight settlements into the next day
    (about 11% of days lost, and about 97% of 1m trades voided). Timestamps are now rounded to the minute, and a
    funding-coverage table plus a warning below 95% are reported.
  - The unseen-years cut-off was global and used Hyperliquid's pre-launch candles. It is now per coin.
  - Walk-forward crashed on folds with zero events. It now returns an empty result.
  - The Binance and paper updaters were not registered.

  All of the numbers above come from after these fixes.
- **Attempts are counted.** Every run is logged in `research_runs`, including the discarded ones.

### 9.6 What happens next

- **Paper-tracking is live** (`market perp-paper`). Each day's signals are recorded once, live,
  never backfilled. This is the only clean out-of-sample test left for these three.
- **Phase 4 (execution) is on hold.** No strategy has earned it.
- **New hypotheses must be new, named experiments,** pre-registered before their data is looked at,
  and tested on data not yet used: Binance coins never touched (DOGE, XRP, ADA, AVAX, BNB…)
  and/or forward paper. Candidates:
  - a trailing-exit version of `trend_ls`;
  - event-driven setups using external data (macro releases, exploits/hacks).

  These are discussed in `docs/PERPS_BACKTEST.md` → "Next hypotheses".
