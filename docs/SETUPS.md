# Setups (V1: exactly three)

Each setup is a deterministic, causal function of the feature frame and of explicitly
point-in-time context. Parameters live in `config/setups/*.yaml`. Experiments in
`config/experiments/*.yaml` override them without code changes.

The rules share these conventions:

- Distances are measured in **ATR(14) units**, so one rule set works for BTC and JPM alike.
- Signals are **edge-triggered**: they fire on the first bar all conditions hold, then stay
  silent for a cooldown. A setup lasting 10 days is one event, not ten.
- A bar where any input is missing is **not eligible**, and is excluded from both signals
  and baselines. It is never treated as "condition false".
- Execution is always at the **next bar's open**. See BACKTESTING.md.

## A. Quality Pullback (`quality_pullback`, TRADE)

**Idea:** buy an orderly pullback inside a constructive long-term trend.

| Condition | Default | Purpose |
|---|---|---|
| close > SMA200 | on | long-term trend |
| SMA50 > SMA200 | on | trend structure |
| SMA200 rising (20-bar slope > 0) | on | trend direction |
| 6-month ROC > 0 | 0.0 | positive medium-term momentum |
| pullback from 63-bar high | 2–6 ATR | a real pullback, not a crash |
| distance to SMA50 | −1.5 to +1.0 ATR | at the support area |
| RSI(14) | 30–50 | momentum cooled |
| realised-vol percentile (3y) | ≤ 0.90 | not in a volatility blow-off |
| PIT fundamental score (optional) | off | equities only: see the `quality_pullback_fundamental` experiment |

- **Invalidation:** 20-bar swing low − 0.5 ATR, but never more than 4 ATR below the close.
- **Exit:** stop, or a time exit after 63 bars.

**Robustness plan:**
- Sensitivity grid of RSI max {40…60} × minimum pullback {1…3 ATR}.
- Walk-forward calibrates the same two parameters.
- Control experiment `control_trend_only` checks whether the pullback adds anything beyond
  simply being in an uptrend.

## B. Breakout + Retest (`breakout_retest`, TRADE)

**Idea:** a tight base, then a decisive break, then an orderly retest that holds. Enter on
the retest, not the breakout candle.

1. **Base.** Resistance *L* is the highest high of the 40 bars *before* the breakout bar.
   The base range must be ≤ 8 ATR.
2. **Breakout.** A close above *L* and above the 200DMA, with expansion: true range ≥ 1.2
   ATR or volume ≥ 1.5× its 20-bar average.
3. **Retest.** Between 2 and 15 bars later, the low comes back within 0.5 ATR of *L*, the
   close holds above *L* − 0.25 ATR, and the close is **not extended** (≤ *L* + 1.5 ATR).
4. **Cancel.** The setup is cancelled if a close falls below *L* − 0.75 ATR before the
   retest.

- **Invalidation:** *L* − 1 ATR.
- **Exit:** stop, or a time exit after 42 bars.
- **Optional 4h refinement (live only):** the scanner flags a 4h over-extension. Backtests
  use daily bars only.

**Robustness plan:** sensitivity grid of base length {20…70} × touch tolerance {0.25…1.0
ATR}; walk-forward on base length.

## C. Fundamental Re-rating / Dislocation (`rerating`, INVESTMENT)

**Idea:** fundamentals intact or improving while price and valuation have compressed.

For **equities (point-in-time, backtestable)**, using SEC EDGAR facts available from their
filing date + 1 day:

- TTM revenue growth ≥ 5%, TTM net income growth ≥ 0%, profitable;
- P/E in the cheapest 30% of its *own* trailing 5-year point-in-time P/E history;
- price ≥ 15% below its 52-week high.

- **Exits:** there is no tight technical stop. Thesis exits (fundamentals deteriorate,
  valuation excessive, better opportunity, concentration) are manual in V1. The backtest
  uses a 126-bar time exit and fixed 10% sizing.

For **crypto (current/prospective only)**:

- Crypto fundamentals have no defensible historical availability time. DefiLlama history is
  reconstructed, and supply histories are rebuilt after the fact. The historical evaluator
  therefore marks every crypto bar **not eligible**.
- The live scanner evaluates HYPE with the dedicated valuation model: buyback yield ≥ 5%
  and price ≥ 25% below its 52-week high. Other crypto assets use a revenue-trend
  fundamental score.

**Known weaknesses:**

- The equity universe is six mega-cap *survivors*, so Setup C's historical sample is both
  small and survivorship-biased.
- Every report states the minimum detectable effect (MDE) for its sample. If that MDE is
  larger than any plausible edge, the result is "insufficient", not "no edge".

## What is deliberately *not* in V1

- There is no "macro trend" setup for commodities. Commodities are scored through the
  commodity macro module, and can trigger A or B like any other asset.
- No additional setups will be added until these three have been tested on real data.
- No machine-learning optimisation.
