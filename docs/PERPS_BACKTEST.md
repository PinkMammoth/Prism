# Perp backtesting: execution model and assumptions

Code: `src/market_signal/perps/backtest.py`. Config: `config/perps.yaml` (`costs`, `margin`,
`risk`, `event_study`). Validated by `tests/test_perp_backtest.py` on synthetic data with
known answers. The spot engine (`backtest/engine.py`) is separate, long-only and unchanged.

## Two layers

**1. Event study: is there an edge?** Each signal's forward return is measured on
*notional*, without leverage, so leverage can't make a weak edge look strong.

- Entry: the next bar's open. Exit: the close `h` bars later. No same-close fills.
- Return = side × price change − 2 × (taker fee + slippage) − funding paid over the window.
  Longs pay positive funding; shorts receive it. The notional moves with the price.
- Each side of each coin is a separate series (`BTC:long`, `BTC:short`). Its baseline is
  **random entry on the same side**, so a short strategy gets no credit just because the
  market fell.
- Independence, the random-entry p-value and the minimum detectable effect come from the
  same event-study code as the spot research.
- A day with too few funding settlements is *missing*. Any return spanning it is missing,
  never treated as zero funding.

**2. Portfolio simulation: what would it have done to an account?** Each position has its
own isolated margin.

- **Size from risk:** notional = risk per trade (0.5%) × equity ÷ stop distance. It's capped
  at 50% of equity per position and 2× equity in total.
- **Leverage is an output:** the highest leverage, up to the 3× cap and the venue's maximum,
  at which the liquidation price stays at least 2× the stop distance away. If even 1× can't
  manage that, the trade is skipped.
- **Maintenance margin** = 1 ÷ (2 × the venue's max leverage). The max leverage comes from
  the latest stored snapshot, or a config default if there's none.
- **Each bar, in order:**
  1. a scheduled exit at the open;
  2. a pending entry at the open, skipped if the open is already through the stop;
  3. the adverse intraday move, judged along the path from the open (below);
  4. the profit target, if any;
  5. funding for the day;
  6. close-of-bar decisions (time stop, new signals).
- **The adverse move:** a gap through the liquidation price liquidates at the open, and a
  gap through the stop fills at the open. Otherwise, whichever of the stop and the
  liquidation price is nearer the open is hit first. Stops pay extra slippage.
- **Funding erodes margin.** Funding paid comes out of the position's margin every day, so
  the liquidation price creeps towards the entry. On a long hold with high funding it can
  overtake the stop, and the position is then liquidated *before* the stop is reached.
- **A liquidation loses the position's whole remaining margin.** This is deliberately
  pessimistic.
- **Missing funding during an open trade:** the last known rate is carried forward, and
  each such day is counted per trade in `funding_imputed_days`.

## Known simplifications

- Daily bars: the order of moves *within* a day is unknown, hence the path rule above.
  Intraday wicks that a 1h/4h model would see as separate events are merged into one.
- One funding rate per day (the sum of the hourly settlements). Hourly variation inside the
  day isn't modelled.
- Fees are the base-tier taker fee; no maker rebates or volume tiers.
- Hyperliquid's real liquidation engine (partial liquidations, insurance fund, ADL) is not
  modelled. Losing the whole margin is the worst case for an isolated position.

## Strategy research (Phase 3)

Strategies live in `src/market_signal/perps/strategies.py`. Their defaults are
**pre-registered**: fixed before any real-data result. Each one declares its sensitivity
axes and walk-forward choices up front.

| Strategy | Hypothesis | Primary horizon |
|---|---|---|
| `trend_ls` | A new 20-day closing high in an uptrend (close and 50-day average above the 150-day) keeps rising; mirrored for shorts. | 1m |
| `funding_fade` | When 7-day funding is in the top 5% of the coin's own past year after a run-up, longs are crowded and the move unwinds (short); mirrored for extreme negative funding after a sell-off (long). | 2w |
| `breakout_ls` | A close outside a tight 30-day range (≤ 8 ATR) continues in the breakout direction. | 1m |

`market perp-research` runs, for each strategy:
1. the event study per side;
2. a walk-forward (1-year anchored training, 6-month tests; it may only choose among the
   declared values, using past data);
3. a parameter-sensitivity grid with the plateau test;
4. the portfolio simulation.

The verdict comes from the **same `automatic_verdict` used for spot setups**. Reports go to
`results/perps/<strategy>/<timestamp>/`, and a `research_runs` row (`perp_<strategy>`) is
added that the dashboard reads.

The validation tests plant persistent trends in synthetic data: `trend_ls` then earns a
passing verdict, out-of-sample walk-forward included, while the same pipeline on pure noise
returns REJECT.

**With only ~2–3 years of perp history, expect few independent events and at most 2–3
walk-forward folds.** INSUFFICIENT_DATA and INCONCLUSIVE are honest outcomes, not failures.
