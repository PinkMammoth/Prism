# Scoring (0–100)

The score answers: *how attractive is this asset as a place for the next increment of
capital, right now?* It is deterministic and explains every point it awards. The weights
are **hypotheses** from the brief, set in `config/scoring.yaml`. They are not optimised.

## Components

| Component | Weight | Equities | Crypto | Commodities (ETP) | ETFs |
|---|---:|---|---|---|---|
| Fundamental quality/trend | 25 | PIT EDGAR: revenue and NI growth, operating and FCF margin, ROE (banks: growth + ROE) | HYPE: revenue momentum + net structural yield; others: DefiLlama revenue momentum and YoY (current only); BTC: N/A | macro factors (see below) | N/A |
| Valuation | 20 | P/E and FCF yield versus the stock's **own** 5y point-in-time range | HYPE: buyback-yield band; others: N/A | N/A | N/A |
| Daily/weekly structure | 15 | price vs 200DMA, 50>200, 200DMA slope, 6m momentum, weekly 40w MA, 10w>40w (completed weeks only) | same | same | same |
| Entry quality | 15 | setup state; price vs entry zone; extension vs 50DMA; RSI; optional 4h over-extension penalty | same | same | same |
| Macro/regime | 10 | governing regime: RISK_ON 10 / NEUTRAL 6 / RISK_OFF 2 / UNKNOWN N/A | crypto regime | macro regime | macro regime |
| Catalyst | 10 | user-maintained `config/catalysts.yaml` (factual, sourced, dated, with expiry); N/A otherwise | same | same | same |
| Liquidity/risk | 5 | 20d traded value vs threshold; realised-vol percentile | same | same | same |

**Commodity factors** (all point-in-time):

| Commodity | Factors |
|---|---|
| Gold | 63-obs change in 10Y real yield (falling = bullish), broad USD, 10Y breakeven |
| Silver | real yield, USD |
| Copper | USD only. This is a known gap: free inventory data is not available. |
| Oil | EIA crude stocks vs their 5-year same-week average, 13-week change in production, USD |

## Aggregation rules

- **Missing is not zero.**
  `score = 100 × Σ points / Σ max(points) over available components`, and
  `coverage = available weight / 100`.
  Every row of the dashboard shows the coverage.
- **Coverage gate.** Below 60% coverage the status is capped at WATCH.
  - This applies to technical-only assets (BTC, SPY, QQQ: about 45% coverage).
  - Such an asset may be actionable at ≥ 40% coverage **only if** its setup earned
    PROMISING or WEAK_POSITIVE in real-data research.
- **Setup gate.** With no active setup, the status is at most WATCH. The system never
  manufactures a trade.
- **Regime gate.** The regime's `min_score_adjustment` is subtracted before banding.
  RISK_OFF requires 10 extra points.

## Bands and statuses

| Band | Score (after regime adjustment) |
|---|---|
| IGNORE | < 55 |
| WATCH | 55–64 |
| ACTIONABLE | 65–74 |
| STRONG | 75–84 |
| EXCEPTIONAL | ≥ 85 |

Statuses:

- `ACTIONABLE`, `STRONG` or `EXCEPTIONAL`: the band is high enough, **and** the setup is
  ACTIVE, **and** the price is inside the entry zone.
- `WAIT`: the band is high enough, but the price is above the entry zone. The status
  shows "wait ≤ $X".
- `WATCH` or `IGNORE`: otherwise.

If nothing is actionable, the headline says so and names the best candidate.

## Valuation vs entry quality

These are deliberately separate components. Example: HYPE can score 20/20 on valuation
(high structural yield) and still lose entry points when its 4h chart is short-term
extended (close > 4h EMA21 + 2 ATR). The output then shows cheap and not yet — wait.

## Price zones

- **Fair / accumulate / strong-buy:**
  - HYPE: prices at which the buyback yield hits the band edges.
  - Equities: current TTM earnings × the stock's own 5y P/E percentiles (p40–60 fair,
    p20–40 accumulate, < p20 strong buy).
  - Other assets: N/A.
- **Entry zone:**
  - Setup A: 50DMA ± 0.5 ATR.
  - Setup B: breakout level −0.25 / +0.5 ATR.
  - Setup C: at or below the valuation accumulate ceiling.
- **Invalidation:** the setup stop (TRADE), or a thesis review below 200DMA − 1 ATR
  (INVESTMENT).
- **Supports:** 50/100/200DMA, the 20-bar swing low and the breakout level, where below the
  current price.

## Sizing

| Kind | Rule | Tiers |
|---|---|---|
| TRADE | position = portfolio risk / distance to invalidation | 0.5% / 0.75% / 1.0% × regime multiplier (1.0 / 0.75 / 0.5) |
| INVESTMENT | fixed fraction | 5% / 7.5% / 10% × regime multiplier |

- Every position is capped at 25%. Gross exposure is capped at 100%, so there is no
  leverage.
- **Until a setup has demonstrated an edge on real data, suggested sizing is capped at the
  standard tier.**
- Investment exits are thesis-based: invalidated thesis, deteriorating fundamentals,
  excessive valuation, a better opportunity, or concentration.

## Not done (V1)

- The weights are not calibrated. The brief says to use them as hypotheses and to let the
  backtests speak first. A future calibration must be walk-forward and limited to one or
  two weights at a time.
- There is no historical score backtest. Crypto fundamentals are not point-in-time, so a
  historical *composite* score would mix admissible and inadmissible inputs. The setups
  themselves are what gets backtested.
