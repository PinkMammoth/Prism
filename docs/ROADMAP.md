# Roadmap and V2 recommendations

The ordering reflects what is most likely to change a decision.

## Immediately (before any real capital)

1. **Run the real research.** On a networked machine:

   ```bash
   market update && market doctor && market research
   ```

   Then paste the verdicts into RESULTS.md §7 and answer the §8 checklist. Remove
   REJECTED setups from live gating rather than re-tuning them.
2. **Paper trade for 3–6 months.** Log every signal taken or skipped, and record whether
   you followed the system. After 30 or more closed trades, check whether live results
   match the backtest's MAE, hit rate and payoff ratio.
3. **Start accumulating point-in-time crypto snapshots now.** `market update` stores
   HYPE supply, USDC and revenue snapshots daily. That history is the only honest way to
   backtest the HYPE model later.

## V2: data

- **Survivorship-free equity universe.** Only if a free and legitimate source becomes
  available. Otherwise add a *control* basket of names chosen with hindsight
  *against* them (fallen leaders), to measure bias.
- **Longer crypto history, as explicit separate series.** Add Binance (USDT) or Bitstamp
  history as explicitly labelled series, with an explicit, reproducible cutover rule if
  stitching is ever wanted. The rule would itself be tested.
- **Crypto supply and market cap:** wire a keyed CoinGecko demo series for ETH, SOL, LINK
  and AAVE, so they get a valuation component (fees/market cap versus their own history).
  Snapshot-only until a history accumulates.
- **HYPE detail:**
  - Observe realised contributor claims from Hyperliquid's non-circulating balances over
    time, instead of the assumed schedule.
  - Track staking emissions.
- **Copper inventories:** LME/COMEX data is paid, so look for a legitimate free proxy
  before adding anything.
- **ETF fundamentals:** aggregate PIT EDGAR data for QQQ/SPY top holdings, weighted by
  holdings as of each date. This needs point-in-time holdings, and N-PORT filings are a
  candidate source.

## V2: research

- **Score calibration.** Only after the setups have a real-data verdict. Walk-forward, and
  one or two weights at a time. Target independent-event excess, not CAGR.
- **Setup refinements.** Each must be a *new named experiment* with a pre-registered
  hypothesis:
  - Quality Pullback with regime filter = RISK_ON only;
  - Breakout + Retest with weekly-base confirmation;
  - Re-rating with FCF-yield percentiles instead of P/E.
- **Block bootstrap** confidence intervals alongside the random-entry test, and a
  Benjamini–Hochberg correction across all experiment runs in `research_runs`.
- **Portfolio-level research:** correlation-aware sizing, and a cap on the number of
  concurrent crypto positions. Crypto assets are highly correlated.
- **Intraday execution study:** does 4h refinement improve fills versus next-day open?
  This needs stored 4h history (enabled for crypto).

## V2: product

- Notifiers: email, Telegram and Discord implementations of the `Notifier` protocol
  (`src/market_signal/portfolio/alerts.py`).
- A scheduled daily `update && scan`, using cron or a launchd plist, documented.
- An optional LLM analysis layer that reads only the structured outputs (scores, reasons,
  factors, reports) and writes commentary. It must never compute numbers.
- Real-position import: CSV import of broker statements, still without broker APIs.
