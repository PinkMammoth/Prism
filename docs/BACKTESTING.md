# Backtesting methodology

This is the part of Prism that decides whether we have an edge. Its job is to make that
decision hard to fake, including by accident.

## 1. Timing contract (no look-ahead)

| Step | When | Price |
|---|---|---|
| Features and signal | bar *t* close, using bars ≤ *t* and non-price data with `available_at` ≤ close_time(*t*) | — |
| Entry | bar *t+1* **open** | open × (1 + slippage), plus fee |
| Horizon *h* exit (event study) | bar *t+h* close | close × (1 − slippage), minus fee |
| Stop / target (simulation) | intrabar, bars ≥ entry bar | stop price, or the open if the bar gapped through it |
| Time / trend exit (simulation) | decided at a close, filled at the next open | open |

- **No same-close fills exist in the engine.** There is no code path or configuration flag
  for them. `test_same_close_fill_is_impossible` builds a series that gaps up every night
  and checks the engine cannot capture the gap.
- **Entries that gap through the stop are skipped.** Nobody should buy a setup that is
  already invalidated.
- **Stop and target touched in the same bar:** the stop is assumed to fill first.
- **Crypto and US equities have different clocks.** Crypto bars close at 00:00 UTC; NYSE
  bars close at 16:00 New York time. Cross-asset information (regimes, macro) is joined
  *backwards* on `close_time`. A crypto bar closing at 00:00 UTC therefore sees the
  previous US close, never the next one.

### How look-ahead is tested

1. **Truncation invariance:** features, regimes, signals and simulated trades computed on
   `data[:k]` match those computed on the full data for every bar before *k*.
2. **Future-shock invariance:** multiplying all prices after bar 400 by 5 leaves every
   feature up to bar 399 unchanged.
3. **Random-signal null:** random signals on random walks show no excess, and the
   random-entry p-value is not significant.
4. **Planted edge:** a signal that peeks one bar ahead *is* detected at the 1-day horizon.
   This proves the measurement window starts at t+1 and that the engine can detect a real
   edge.
5. **Unit regression:** timestamps in µs (DuckDB) and ns (pandas) are compared correctly.
   A real bug of this kind was found and fixed during development.

## 2. Price bases

- **Signals** use split-adjusted prices. Ratios such as close/SMA are then continuous
  across splits.
- **Returns** use total-return (split + dividend) prices. Dividends are credited in both
  the event study and the simulator, via a per-bar dividend factor.
- Both bases are derived from stored **raw** bars plus corporate actions. Stored data is
  never adjusted in place.

## 3. Costs (configured assumptions, `config/backtest.yaml`)

Costs are per side and include fees and slippage: crypto 10 + 10 bps, HYPE 10 + 20 bps,
equities 5 + 5 bps, ETFs 5 + 3 bps, commodity ETPs 5 + 5 bps. Every return reported is net
of costs.

## 4. Event study

- **Forward returns** are computed for the class horizons: 1d, 3d, 1w, 2w, 1m, 3m, 6m and
  12m. In bars: equities 1/3/5/10/21/63/126/252; crypto 1/3/7/14/30/91/182/365. If the exit
  bar does not exist yet, the return is **NaN**, not a truncated holding period.
- **Baseline:** the same forward return measured from *every eligible bar* of the same
  asset. A setup's **excess** is its return minus that baseline. Beating a bull market is
  not an edge; beating random entry into the same asset over the same period might be.
- **Independent events:** for a horizon of *h* bars, events closer than *h* bars to the
  previous kept event are removed. Every t-stat and p-value is computed on independent
  events only. Raw event counts are also shown, but they overstate the evidence.
- **Random-entry p-value:** we repeatedly draw the same number of entries per asset,
  uniformly from that asset's eligible bars without replacement, and record how often
  random timing matches or beats the observed mean excess. The test is one-sided and uses
  2,000 draws.
- **Splits** are computed at the primary horizon:
  - the regime engine label (`RISK_ON` / `NEUTRAL` / `RISK_OFF`);
  - benchmark trend (bull/bear: BTC or SPY versus its 200DMA);
  - volatility (the asset's realised-vol percentile ≥ 0.5 or below);
  - the rate cycle, from the 126-observation change in fed funds, available point-in-time:
    hiking > +25 bp, cutting < −25 bp, otherwise on hold;
  - asset class and individual asset.

## 5. Portfolio simulation

- Positions are sized at `risk_per_trade × regime multiplier / stop distance` for TRADE
  setups, or at a fixed fraction for INVESTMENT setups.
- Each position is capped at `max_position_fraction` (25%).
- Gross exposure is capped at 100%, so there is **no leverage**. Orders that cannot be
  funded are skipped and counted. They are never scaled up.
- Orders arriving at the same time are processed in symbol order. This is deterministic,
  not optimised.
- Metrics:
  - per trade: trade count, hit rate, average and median return, payoff ratio,
    expectancy (% and R), average holding period, MAE/MFE;
  - portfolio: CAGR (only if ≥ 90 days), Sharpe, Sortino, max drawdown with dates, time in
    market, average gross exposure, turnover per year.
- The equity curve is daily (UTC calendar). Portfolio value is carried forward between
  marks. This is valuation, not data filling.

## 6. Robustness

### Walk-forward

- Anchored (expanding) training windows. The defaults are 4 years of training followed by
  2 years of test, rolled forward. Each fold's selection is based **only** on its training
  window.
- **Purging:** training events whose forward window could extend into the test period are
  dropped.
- **Window-local baselines:** excess returns are recomputed using only that window's
  baseline, so full-sample information does not leak into selection.
- At most **two** parameters are calibrated at once. This is enforced: more raises an
  error.
- The objective is the median excess of independent events. Returns and CAGR are not used
  as objectives.
- **Default parameters are always evaluated out-of-sample as well.** The pooled
  out-of-sample result for the defaults is the headline number.

### Parameter sensitivity (plateaus)

- A grid of up to two parameters is evaluated around the defaults. The default point is
  classified as follows:
  - `INSUFFICIENT`: fewer than 30 independent events;
  - `NO_EDGE`: excess ≤ 0;
  - `FRAGILE`: at least half the neighbouring grid points have the opposite sign (a
    "magic number");
  - `PLATEAU`: at least 60% of neighbours keep ≥ 50% of the default's excess;
  - `MIXED`: anything else.

## 7. Automatic verdict (no discretion)

| Verdict | Rule |
|---|---|
| `INSUFFICIENT_DATA` | < 30 independent events at the primary horizon |
| `REJECT` | excess ≤ 0, **or** random-entry p ≥ 0.10, **or** default parameters show no out-of-sample excess (with ≥ 30 OOS events) |
| `INCONCLUSIVE` | none of the REJECT conditions, but any of: fewer than half the WF folds positive, FRAGILE, or positive on < 50% of assets |
| `WEAK_POSITIVE` | passes the above, but p ≥ 0.05 or no clear plateau |
| `PROMISING` | p < 0.05, plateau, WF-positive, cross-asset positive |

`PROMISING` does not mean "trade it". It means the idea survived the tests available with
free data and deserves paper trading.

## 8. Known biases that cannot be removed with free data

- **Survivorship and selection bias.** The universe is today's winners. Delisted equities
  and dead coins are not included, so raw returns are biased upward. The baseline-excess
  framing mitigates this but does not remove it.
- **Crypto fundamentals are not point-in-time.** Setup C is therefore tested historically
  on equities only.
- **Short histories:** SOL (2021), AAVE (2020), HYPE (Nov 2024). Their samples are small and
  are labelled as such.
- **Multiple testing.** Every experiment run is logged in `research_runs`. Treat a single
  PROMISING result out of many experiments with suspicion.
