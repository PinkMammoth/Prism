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
| `macro_shock` | A hot CPI print with the 2Y yield up ≥ 5bp, or a 2Y shock ≥ 2σ, is followed by a week of crypto weakness (short). Judged per event date on a coin basket. See below. | 1w |

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

## Two further checks on the same strategies

**Unseen years (Binance, 2019 → the start of the Hyperliquid data).**
`market perp-research --venue binance` runs the *unchanged* strategies on Binance history
that ends where the Hyperliquid data begins, so no year is used twice. It uses Binance's own
costs and margin assumptions (`venues.binance`). Its verdicts are saved as
`perp_<strategy>@binance`. The strategies weren't fitted to these years. They are generic
ideas, though, written with general knowledge of crypto history, so this is strong
evidence but not perfect evidence.

**Paper-tracking (live, from now on).** Each `market update` checks the newest closed bar
of every coin with every strategy and records the result in `perp_paper_checks`. It never
backfills days that were missed, and each row stores the strategy's parameter hash.
`market perp-paper` scores the recorded signals against random entry drawn from the same
live-checked bars. Nothing is traded or shown as advice.

## Pre-registered: `macro_shock` (registered October 2026, before any real-data run)

The first external-event experiment. The definition is in `perps/strategies.py` and the event
construction in `perps/macro_events.py`. It was committed (the "pre-registration" commit) before
anyone ran it on real data.

**Hypothesis.** A hawkish US rate surprise pushes crypto risk-off for about a week, so short
every configured coin. The verdict is about the *short* side only.

**Triggers** (either one fires a short):

1. **Hot CPI.** On a CPI release day R (the ALFRED `realtime_start` of the new month), the m/m
   change *as first reported* beats the mean m/m of the 12 months before it, using the vintage
   known at the release, **and** DGS2 rises by at least **X = 5bp** on R. There's no consensus
   forecast, so "hot" means "above the recent trend".
2. **Rate shock.** DGS2's daily change is at least **k = 2.0** standard deviations of its changes
   over the 365 days *before* that day (at least 200 of them; the shock is not in its own σ).

| Parameter | Default | Sensitivity grid | Walk-forward may choose |
|---|---|---|---|
| `cpi_dgs2_bp` (X) | 5 | 2, 5, 10, 15 | 5, 10 |
| `shock_k` (k) | 2.0 | 1.5, 2.0, 2.5, 3.0 | 2.0, 2.5 |

Primary horizon **1w** (1d–3m reported). For the simulation only: stop 2.5 ATR, max hold 7 days.

**When the data is knowable, and the lag this costs.**

| Input | Stored availability (config/macro.yaml) | Used here |
|---|---|---|
| DGS2 for day D | D + 2 days, 00:00 UTC (`market_close`, `lag_days: 2`) | the later of that and 00:00 UTC the day after the next business day. A **Friday** move counts from **Tuesday** 00:00, because FRED posts it on Monday. So paper-tracking, which reads FRED, sees what the backtest uses. |
| CPI released on R (12:30 UTC) | R + 1 day, 00:00 UTC (ALFRED `realtime_start` + 1) | unchanged |
| Hot-CPI event | – | max(both) = R + 2 days, 00:00 UTC |

The signal bar is the first daily bar closing at or after that time. Entry is the next open,
which for 00:00 UTC bars is exactly the availability time. **The market reacts to CPI and to the
2Y within minutes. This experiment enters 1.5–3 days later (Mon–Thu moves: ~1.5 days; Friday
moves: ~3.5 days; hot CPI: ~1.5 days after the print), so it tests the drift after the shock,
not the shock itself.** A positive result would mean the drift persists. A negative result says
nothing about the first hours.

**How it's judged (and why it differs from the other three).** A macro event fires on every
coin on the same date, so six per-coin events are one observation, not six. The verdict is
therefore computed on an **equal-weight basket of the coins**: one observation per event date.
The baseline is every basket date on the same side, and the random-entry p-value draws random
*dates*, which keeps the coins' co-movement. `n_independent` then counts independent event
dates. The per-coin table is still shown, as a diagnostic. The cross-coin consistency check
still uses it. Otherwise the criteria are the same `automatic_verdict` as everywhere else, and
the existing gating applies: only PROMISING or WEAK_POSITIVE makes its signals CANDIDATEs.

**Reported separately, never in the verdict:** hot-CPI-only, rate-shock-only, and the **mirror**
(a 2Y shock *down* → long), a secondary hypothesis labelled as such.

**Expected power.** That is ~12 CPI releases a year, of which perhaps a third qualify, plus a few
rate shocks a year, declustered at 7 days. Hyperliquid's ~3 years should give well under 30
independent dates, so **expect INSUFFICIENT_DATA there**. Binance's unseen window (2019 → each
coin's Hyperliquid start, up to 2023-10-04) adds about 4 years, including 2022's rate shocks. It
may reach 30. The report prints the minimum detectable excess (`mde_80`); a REJECT with an MDE of
several % per week rules out only large effects. The two venues are never pooled.


All three strategies in Phase 3 were rejected (see RESULTS.md §9). The ideas below are
*candidates*. Each would become a new, named, pre-registered experiment, defined before
its data is examined, and tested on data not yet used: fresh Binance coins and/or forward
paper-tracking.

**Risk profile is not the lever.** The event study scores each trade on notional, without
leverage. Bigger size or more leverage scales an edge that exists; it can't create one. A
strategy with zero or negative excess loses faster at 3× than at 1×. Sizing becomes a
question only *after* an edge passes.

**External events.** These are setups triggered by something outside the price series. The engine
already supports them: an event list goes into `run_event_study` against the same-side
random baseline. What each candidate needs:

| Idea | Trigger (point-in-time) | Data | Main risk |
|---|---|---|---|
| Hot CPI → crypto risk-off (**registered as `macro_shock`**, above) | The CPI release beats the prior trend and the 2Y yield (DGS2) jumps on the day | CPIAUCSL ALFRED vintages and DGS2, both already ingested; release dates come from ALFRED | Without a consensus forecast, "hot" is a proxy. There are only about 12 releases a year (~60–100 since 2019), so the sample is small. |
| Large hack → privacy coins / ETH | An exploit above $X M, timestamped when it was *publicly reported* | DefiLlama hacks list (free); XMR prices (not in the universe, and delisted on many venues) | Report-time lags, few qualifying events, and XMR liquidity |
| Rate-expectation shock (**part of `macro_shock`**) | Any day the 2Y yield moves more than k standard deviations | DGS2 (ingested) | Overlaps with equity regime signals |

**A system that fires on setups.** This already exists. Any strategy that earns PROMISING or
WEAK_POSITIVE becomes a CANDIDATE on the Perps page and in the daily brief. Until one does,
signals show as RESEARCH ONLY.
