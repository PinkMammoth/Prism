# RESULTS — what the research actually found

## 1. Status

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
