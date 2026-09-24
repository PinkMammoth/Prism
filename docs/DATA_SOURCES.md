# Data sources

Researched 2026-09-24. Provider facts change: re-check before relying on them, and
`market doctor` checks live reachability and schema on every run.

**Verification status.** Provider facts below come from official documentation and
current reports, cited per provider. This build container's egress policy blocks every
data host listed here (proxy 403), so parsers were built against the documented response
schemas and fixture payloads rather than live calls. Run `market doctor --live` on first
use. It fails loudly if a schema differs from what the parser expects.

## Principles

1. **£0/month.** Free tiers only. Free API keys (Tiingo, FRED, EIA) are fine because they
   cost nothing. They live in `.env`.
2. **Official/public APIs only.** No scraping, and no unofficial endpoints (Yahoo).
3. **One provider per series.** Bars are stored per `(asset, timeframe, source)`.
   Providers are never merged silently. A provider change creates a new series and logs it.
4. **Point-in-time or not at all (for research).** Every macro/fundamental row has
   `available_at` and `pit_method`:

   | pit_method | Meaning | Allowed in backtests |
   |---|---|---|
   | `vintage` | Real-time period from ALFRED: the value as it was published at the time | yes |
   | `filing_date` | SEC `filed` date + 1 day (the acceptance time may be after the close) | yes |
   | `release_rule` | Documented publication schedule + conservative lag (EIA weekly) | yes, flagged |
   | `market_close` | Market-determined value, not revised (e.g. daily Treasury yields), available the next day | yes |
   | `snapshot` | Captured live by Prism at `fetched_at` | yes, for dates after capture |
   | `reconstructed` | History rebuilt today by a third party (DefiLlama, supply histories) | **no**: display only |

5. **Provenance.** Every fetch writes an `ingestion_runs` row: provider, endpoint, params,
   timestamps, row count, schema fingerprint, and the path + SHA-256 of the gzipped raw
   payload under `data/raw/`.

---

## Crypto prices

### Coinbase Exchange public REST: PRIMARY (BTC, ETH, SOL, LINK, AAVE)

- Endpoint: `GET https://api.exchange.coinbase.com/products/{BASE}-USD/candles?granularity=&start=&end=`
- Auth: none. Cost: free.
- Granularities: 60, 300, 900, 3600, 21600, 86400 s. **There is no native 4h.** Prism
  aggregates 4h bars from Coinbase 1h bars, from the same provider, deterministically, and
  labels the source `coinbase:agg1h`.
- Limit: 300 candles per request. Public rate limit is about 10 req/s; Prism uses 3 req/s.
- Response row: `[time, low, high, open, close, volume]`, where time is the bucket start in
  UTC epoch seconds. Rows come back newest-first.
- Coverage (approximate listing dates): BTC-USD 2015-07, ETH-USD 2016-05, LINK-USD 2019-06,
  AAVE-USD 2020-12, SOL-USD 2021-06.
- Reliability: Coinbase documents that historical data "may be incomplete" and that no
  candle is published for intervals with no trades. Prism records missing daily bars as
  gaps. It never forward-fills them.
- Why primary: USD-quoted, regulated venue, long history, keyless, accessible in the UK.
- Source: [Coinbase – Get product candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)

### Bitstamp public REST: FALLBACK

- `GET https://www.bitstamp.net/api/v2/ohlc/{pair}/?step=&limit=&start=&end=`
- Steps include 3600, **14400 (native 4h)** and 86400. Max 1,000 candles per request.
- Caveat: when both `start` and `end` are supplied, `end` wins, so pagination walks backwards
  from `end`.
- Used only if the user switches a series to it explicitly in `universe.yaml`.
- Source: [Bitstamp API](https://www.bitstamp.net/api/)

### Hyperliquid public `info` API: PRIMARY for HYPE

- `POST https://api.hyperliquid.xyz/info`, `{"type":"candleSnapshot","req":{"coin":"@107","interval":"1d","startTime":..,"endTime":..}}`
  - The spot HYPE/USDC pair id (`@107`) is resolved at runtime from `spotMeta` and pinned
    in `universe.yaml` as a fallback.
- Auth: none. Weight-based limit (1,200 weight/min per IP). Candle responses cost extra
  weight per 60 items.
- **Only the most recent 5,000 candles are available.** At 1d and 4h this covers all of
  HYPE's history (spot since 2024-11-29).
- Also used for fundamentals: `tokenDetails` (circulating/total supply, future emissions,
  non-circulating balances), `spotMetaAndAssetCtxs`, and `spotClearinghouseState` for the
  Assistance Fund address `0xfefefefefefefefefefefefefefefefefefefefe`.
- HYPE/USDC is treated as HYPE/USD. The basis risk is documented, not corrected.
- Sources: [candleSnapshot](https://www.quicknode.com/docs/hyperliquid/info-endpoints/candleSnapshot),
  [tokenDetails](https://docs.chainstack.com/reference/hyperliquid-info-token-details),
  [Rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)

### Considered and rejected

| Provider | Reason |
|---|---|
| Binance / data.binance.vision | Longest altcoin history and native 4h. Rejected because of jurisdictional restrictions (geo-blocked in the US, restricted UK status) and a USDT quote. Could be added as an explicit optional series later. |
| Kraken REST | Returns only the latest 720 candles. The bulk history is distributed via file-sharing links, which is awkward to automate reproducibly. |
| CoinGecko Demo | Free key, but the demo tier limits history to about 1 year. OHLC granularity is coarse. |

---

## Equities, ETFs and commodity ETPs

### Tiingo EOD: PRIMARY

- `GET https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate=&endDate=&token=`
- **Free key required.** Free "Starter" tier: 1,000 requests/day, 50/hour,
  500 unique symbols/month, 30+ years of history.
- Fields: `date, open, high, low, close, volume, adjOpen, adjHigh, adjLow, adjClose,
  adjVolume, divCash, splitFactor`.
- Prism stores **raw** OHLCV plus `divCash` and `splitFactor` in `corporate_actions`, then
  derives the following on read:
  - split-adjusted prices, used for signals and technicals;
  - total-return (split + dividend) adjusted prices, used for P&L and forward returns.
- Terms: data for internal/personal use only, no redistribution. The DuckDB file and
  `data/` are git-ignored.
- Universe: SPY, QQQ, MSFT, GOOGL, AMZN, META, NVDA, JPM, and commodity proxies
  **GLD** (gold, 2004-11), **SLV** (silver, 2006-04), **CPER** (copper, 2011-11) and
  **USO** (WTI, 2006-04). USO restructured and reverse-split in 2020, so it is flagged as a
  discontinuity.
- Budget: a full backfill of 12 tickers is 12 requests. Daily incremental updates are
  12 requests/day.
- Sources: [Tiingo pricing](https://www.tiingo.com/about/pricing),
  [Tiingo docs](https://www.tiingo.com/documentation/general/overview),
  [Tiingo ToS](https://app.tiingo.com/tos/)

### Stooq: FALLBACK

- Since roughly April 2026, CSV downloads (`https://stooq.com/q/d/l/?s=spy.us&i=d&apikey=`)
  **require an API key**, obtained by solving a CAPTCHA on the site. There is a daily hit
  limit.
- Stooq prices are split-adjusted and carry no dividend field, so total return is not
  available from this source. Use it only as a fallback, as an explicit separate series.
- Source: [pandas-datareader issue #1012](https://github.com/pydata/pandas-datareader/issues/1012)

### Manual CSV import: ALWAYS AVAILABLE

- `market import-csv --asset MSFT --source mycsv_2026 file.csv` imports a user-supplied
  file as its own labelled series. Its provenance is the file hash.

### Considered and rejected

| Provider | Reason |
|---|---|
| Yahoo Finance / yfinance | Unofficial endpoint. Yahoo's terms prohibit automated access. |
| Alpha Vantage | Free tier is 25 requests/day and 5/min. The brief says not to architect around it. |

---

## Macro: FRED / ALFRED (St. Louis Fed)

- `GET https://api.stlouisfed.org/fred/series/observations?series_id=&api_key=&file_type=json&realtime_start=1776-07-04&realtime_end=9999-12-31`
- **Free key required.** Rate limit: 120 requests/minute.
- Point-in-time: requesting the full real-time period returns one row per
  `(observation date, vintage)` with `realtime_start`, the date that value was published.
  `available_at = realtime_start + 1 day, 00:00 UTC`, which is conservative. Vintage history
  began later than some observation dates. Observations published before the first
  archived vintage therefore become "available" only at that first vintage, which is also
  conservative.
- Series used:

| Purpose | Series | Revised? | pit_method |
|---|---|---|---|
| 10Y yield | DGS10 | no | vintage |
| 2Y yield | DGS2 | no | vintage |
| Curve | T10Y2Y | no | vintage |
| Fed funds (effective) | DFF | no | vintage |
| 3M T-bill (AQAv2 reserve yield) | DTB3 | no | vintage |
| 10Y real yield (gold) | DFII10 (2003+) | no | vintage |
| 10Y breakeven (gold) | T10YIE (2003+) | no | vintage |
| Volatility | VIXCLS | no | vintage |
| Broad USD | DTWEXBGS (2006+) | occasionally | vintage |
| Credit spread (history) | BAA10Y (Moody's, daily, 1986+) | no | vintage |
| Credit spread (display) | BAMLH0A0HYM2 | — | **display only**: FRED now serves only a rolling 3-year window of ICE BofA series |
| Financial conditions | NFCI (weekly) | **yes** | vintage (required) |
| Inflation | CPIAUCSL (monthly) | **yes** | vintage (required) |
| Unemployment | UNRATE (monthly) | **yes** | vintage (required) |

- Fallback: none. Without a key, the macro module and the macro half of the regime engine
  are disabled and reported as such. Prism does not substitute undocumented proxies.
- Sources: [fred/series/observations](https://fred.stlouisfed.org/docs/api/fred/series_observations.html),
  [BAMLH0A0HYM2 truncation](https://fred.stlouisfed.org/series/BAMLH0A0HYM2),
  [BAA10Y](https://fred.stlouisfed.org/series/BAA10Y)

## Energy: EIA API v2 (optional)

- `GET https://api.eia.gov/v2/petroleum/...` or `/v2/seriesid/{legacy id}?api_key=`
- **Free key required.**
- Series: U.S. crude oil stocks excluding SPR (`PET.WCESTUS1.W`) and U.S. field production
  (`PET.WCRFPUS2.W`).
- Point-in-time: the API does not expose release timestamps. The Weekly Petroleum Status
  Report is published on Wednesdays at 10:30 ET for the week ending the previous Friday, one
  day later after federal holidays. Rule used: `available_at = period_end + 6 days,
  00:00 UTC` (the Thursday after release). pit_method is `release_rule`.
- Source: [EIA WPSR](https://www.eia.gov/petroleum/supply/weekly/)

## Equity fundamentals: SEC EDGAR XBRL

- `GET https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`
- No key. A **descriptive User-Agent with a contact email is required** (`SEC_USER_AGENT`
  in `.env`). Limit: 10 requests/second.
- Every fact carries `filed` (filing date), `form`, `fy`, `fp`, `start`, `end` and `accn`.
  - Restatements appear as later filings for the same period, each with its own `filed`
    date.
  - Prism's as-of query takes, for each period, the latest value filed on or before
    `as_of - 1 day`. This is genuine point-in-time data at day granularity.
- Coverage: XBRL-tagged filings from about 2009–2011 onwards. Before that, fundamentals are
  missing, not backfilled.
- Limitations:
  - Dimensional facts (e.g. per-class share counts for GOOGL) are excluded from
    companyfacts. Per-share metrics use diluted EPS and weighted diluted shares instead.
  - Banks (JPM) do not have meaningful FCF or "revenue" in the same concepts, so
    per-asset config restricts JPM to EPS/ROE-type metrics.
  - ETFs (SPY, QQQ, GLD...) have no company fundamentals.
- Source: [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

## Crypto fundamentals: DefiLlama + Hyperliquid (current/prospective only)

- DefiLlama, no key:
  - `GET https://api.llama.fi/summary/fees/{slug}?dataType=dailyFees|dailyRevenue|dailyHoldersRevenue`
    returns `totalDataChart`, a daily history.
  - `GET https://stablecoins.llama.fi/stablecoincharts/{chain}` returns stablecoin
    supply by chain.
- The free API has no key. The Pro tier (paid) is **not** used.
- **Point-in-time status: `reconstructed`.** DefiLlama rebuilds history from on-chain data
  using adapters whose methodology changes over time; known inconsistencies between fee and
  revenue endpoints have been reported. Today's history is therefore not what was knowable
  on each historical date. Prism:
  1. stores DefiLlama history with `pit_method='reconstructed'`. It is shown on charts,
     clearly labelled, and rejected by the backtest loader;
  2. stores each daily fetch's *latest* values as a `snapshot` with `fetched_at`, so
     genuinely point-in-time history accumulates from first use onwards.
- Hyperliquid AQAv2 (live for USDC since 2026-08-26): stablecoin deployers share about 90%
  of *cost-adjusted* reserve yield on Hyperliquid supply. Payouts go to the Assistance Fund
  in 30-day cycles with an 8-day transfer delay (first payout reported for 2026-10-03). The
  Assistance Fund buys HYPE and burns it.
  - Prism treats the share (90%), the cost adjustment and the eligible USDC fraction as
    **configured assumptions**.
  - The T-bill yield (DTB3) and USDC supply on Hyperliquid are **observed** inputs.
- Sources: [DefiLlama API docs](https://api-docs.defillama.com/),
  [DefiLlama pricing](https://docs.llama.fi/pro-api),
  [fee/revenue inconsistency report](https://github.com/DefiLlama/defillama-server/issues/4168),
  [AQA docs](https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/aligned-quote-assets),
  [AQAv2 activation coverage](https://blockonomi.com/hyperliquid-activates-aqav2-to-fund-hype-buybacks-with-usdc-reserve-yield/),
  [HYPE unlocks](https://tokenomist.ai/hyperliquid/unlock-events)

## Summary: what can be researched honestly in V1

| Question | Possible historically? | Why |
|---|---|---|
| Technical setups (A, B) on crypto, equities, ETFs, commodity ETPs | **Yes** | Price data is market-determined and unrevised |
| Regime conditioning on market + FRED macro | **Yes** | ALFRED vintages |
| Setup C (re-rating) on single-name equities | **Yes, small sample** | EDGAR filing dates; 6 names × ~15 years |
| Setup C on crypto / HYPE | **No**: current/prospective only | No point-in-time fundamentals exist for free |
| HYPE valuation history | **No**: displayed as reconstructed | DefiLlama history is reconstructed |
| Oil macro factors (EIA) | Yes, flagged `release_rule` | Rule-based availability |
