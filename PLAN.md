# PLAN — Prism: local multi-asset trading research & signal platform

Status is tracked in the **Phase log** at the bottom and updated after each phase.

## 1. Purpose (unchanged from the brief)

Answer one question: *where is the best risk-adjusted place to deploy my next increment
of capital — if anywhere?* Prism is a scanner, research engine, backtester, watchlist and
paper portfolio. It never executes trades and requires no broker/exchange keys. "Nothing is
attractive" is a first-class output.

## 2. Architecture

Single Python package (`src/market_signal`), one local DuckDB file, one Typer CLI
(`market`), one Streamlit app. No services, no background daemons.

```
config/*.yaml ──► config loader (pydantic) ──────────────────────────────┐
                                                                         ▼
providers (Coinbase, Hyperliquid, Tiingo, FRED/ALFRED, EIA, EDGAR, DefiLlama, CSV)
   │  raw payload archived (gz + sha256)   ◄── provenance: ingestion_runs
   ▼
validation (schema, OHLC sanity, dupes, gaps, jumps, staleness, tz)
   ▼
DuckDB store  (assets, bars, corporate_actions, macro_observations,
               fundamental_facts, crypto_snapshots, ingestion_runs, ...)
   ▼
features: indicators (pure functions) + point-in-time as-of joins
   ▼
regimes ─► setups (A/B/C, causal) ─► backtest (event study + trade sim,
                                      walk-forward, sensitivity, regime splits)
   ▼
fundamental modules (HYPE, crypto, equity-PIT, commodity-macro)
   ▼
scoring (0–100, configurable weights) ─► price zones ─► risk sizing ─► alerts
   ▼
CLI  /  Streamlit dashboard  /  paper portfolio + journal
```

Key design rules (architectural, enforced in code and tests):

1. **Provider isolation.** Business logic depends on `MarketDataProvider`,
   `FundamentalDataProvider`, `MacroDataProvider` protocols only. Providers return
   normalised frames; nothing downstream imports a provider module.
2. **No silent stitching.** Bars are keyed by `(asset, timeframe, source)`. Each asset's
   series declares one provider in `config/universe.yaml`. Changing the provider creates a
   new, separate series; the old one is kept and the change is logged in `series_changes`.
   Reads always name the source (default: the configured primary).
3. **Point-in-time everywhere.** Every macro/fundamental row carries `available_at`
   (UTC timestamp) and `pit_method` (`vintage`, `filing_date`, `release_rule`,
   `snapshot`, `reconstructed`). Backtests only accept PIT methods on an allow-list;
   `reconstructed` data (e.g. DefiLlama history, today's supply figures) is
   *current/prospective only* and is rejected by the backtest loader.
4. **Next-bar execution.** A signal computed on bar *t*'s close fills at bar *t+1*'s open
   plus configured slippage and fees. Same-close fills are not an option in the engine.
5. **Missing stays missing.** NaN propagates. Indicators require full windows. Score
   components with no data are `N/A`, excluded from the denominator and reported as reduced
   *coverage*; low coverage caps the status at WATCH. No zero-filling, no invented proxies.
6. **Observed vs assumed vs derived** are separate typed objects in the HYPE model and in
   every score explanation.
7. **Reproducibility.** Raw payloads archived with hashes; each research run stores config
   hash, data fingerprint (per-series row count / last timestamp / content hash), code
   version and git commit.
8. **Deterministic core.** No LLM anywhere in V1.

## 3. Provider choices (details and evidence in `docs/DATA_SOURCES.md`)

| Domain | Primary | Fallback / notes |
|---|---|---|
| Crypto daily + 1h (4h resampled) — BTC, ETH, SOL, LINK, AAVE | Coinbase Exchange public REST (USD pairs, no key) | Bitstamp public OHLC (native 4h). Binance not used (jurisdictional restrictions). |
| HYPE price daily/4h | Hyperliquid public `info` API (spot HYPE/USDC) | none free with longer history — HYPE only exists since Nov 2024 |
| US equities, ETFs, commodity ETPs (GLD, SLV, CPER, USO) | Tiingo EOD (free key: 1,000 req/day, 500 symbols/month) | Stooq CSV (now needs a free key), manual CSV import |
| Macro | FRED API with ALFRED vintages (free key) | none — without a key, macro is disabled, not faked |
| Energy | EIA API v2 (free key) | optional module |
| Equity fundamentals | SEC EDGAR XBRL `companyfacts` (no key, UA header) — has per-fact `filed` dates | — |
| Crypto fundamentals | DefiLlama public API + Hyperliquid API | **current/prospective only** (no defensible historical availability) |

Rejected: Yahoo Finance (unofficial, ToS), Alpha Vantage as backbone (25 req/day),
ICE BofA OAS for history (FRED truncated to 3 years since 2026 licence change — used for
display only), CoinGecko for OHLC (demo tier ~1 year history).

## 4. Implementation phases

| Phase | Scope | Exit criteria |
|---|---|---|
| 0 | Discovery, this plan, DATA_SOURCES.md, scaffold | Docs committed; `uv sync` works |
| 1 | Asset registry, providers, DuckDB schema, idempotent upserts, provenance, validation, `update`/`doctor`/`import-csv` | Ingestion + idempotency + validation tests pass |
| 2 | Indicators, macro ingestion (ALFRED vintages), PIT as-of joins, regime engine | Indicator reference tests, PIT tests, regime tests |
| 3 | Backtest framework: event study, trade simulator, metrics, walk-forward, sensitivity, regime splits, declarative experiments | Synthetic no-look-ahead proofs pass |
| 4 | Setups A/B/C; run research; write RESULTS.md honestly | Backtest reports generated; rejected ideas documented |
| 5 | Scoring 0–100 (config weights), price zones, risk sizing | Scoring tests |
| 6 | Fundamentals: HYPE module first, crypto, equity (PIT EDGAR), commodity | HYPE valuation + inverse tests |
| 7 | Streamlit dashboard (5 pages) | Pages render against a demo DB |
| 8 | Paper portfolio, journal, alerts | Portfolio/risk tests |

## 5. Assumptions (decided without asking; revisit if wrong)

- The user is in the UK; all providers chosen are accessible from the UK and cost £0.
- Free API keys (Tiingo, FRED, EIA) are acceptable — they cost nothing. Everything else
  runs keyless. The app degrades module-by-module if a key is missing.
- Crypto prices are USD-quoted (Coinbase USD books; Hyperliquid HYPE/USDC treated as USD,
  documented basis risk).
- Commodity exposure is via ETPs (GLD, SLV, CPER, USO). USO's 2020 restructuring and
  reverse split are flagged as a known discontinuity.
- Equities: technicals use split-adjusted prices; returns use split+dividend (total return)
  adjustment; both are derived deterministically from stored raw bars + corporate actions.
- Horizons are expressed in bars per asset class (equities: 1/3/5/10/21/63/126/252;
  crypto: 1/3/7/14/30/91/182/365).
- Default costs: equities/ETFs 5 bps fee + 5 bps slippage per side; crypto 10 bps fee +
  10 bps slippage per side; HYPE 10 + 20 bps. Configurable.

## 6. Known limitations (V1)

- **Survivorship/selection bias.** The universe is today's large winners (NVDA, MSFT, BTC,
  SOL, HYPE...). Free delisted-security data is not available; equity and crypto results
  are biased upward and must be read *relative to each asset's unconditional baseline*,
  which the event study always reports.
- **Crypto fundamentals are not point-in-time.** No free source provides as-of-date
  vintages of protocol revenue/supply. Crypto fundamental scores are current-only and are
  excluded from historical tests; Setup C is tested historically on equities only.
- **Short histories.** SOL (2021), AAVE (2020), HYPE (Nov 2024) — HYPE cannot be backtested
  meaningfully. Small samples are reported as such, not extrapolated.
- **Credit spreads.** ICE BofA OAS history on FRED is limited to 3 years; the regime engine
  uses Moody's BAA–10Y spread (`BAA10Y`) for history.
- **4h data** is optional; Coinbase has no native 4h so 4h bars are aggregated from 1h
  bars of the *same* provider (explicit, deterministic).
- **Catalysts** have no free structured feed; the catalyst component is driven by a
  user-maintained `config/catalysts.yaml` and is `N/A` otherwise.
- **This build environment's egress policy blocks all market-data hosts**; see Phase log.

## 7. Technical risks

| Risk | Mitigation |
|---|---|
| Provider schema changes | Strict per-provider schema validation; fail loudly; raw payload archived |
| Look-ahead via indicators / joins | Causal-only computations; truncation-invariance tests (signals on data[:t] == signals on full data at t) |
| Look-ahead via fundamentals | `available_at` on every row; backtest loader filters by PIT method; EDGAR uses `filed` + 1 day |
| Overfitting | ≤2 parameters calibrated at once; plateau checks; walk-forward; baselines; declustered samples |
| Split mismatches in per-share fundamentals | Split factors from provider applied to per-share facts filed before a split |
| Rate limits | Per-provider limiter; incremental updates; bulk initial backfill once |

## 8. Test strategy

- Unit tests on pure functions (indicators vs hand-computed references).
- Parser tests using fixture payloads shaped like documented provider responses.
- Store tests: uniqueness, idempotent re-ingest, separate series per source.
- **Look-ahead proofs**: (a) truncation invariance for indicators, setups, regimes;
  (b) a synthetic "future-peeking" series where a leaky implementation would show a
  large edge and a correct one shows none; (c) random-walk data must show ~zero excess
  forward returns; (d) a planted edge must be detected.
- Backtest timing tests (next-bar open fills, gap-through stops, stop-before-target rule).
- PIT tests for ALFRED vintages and EDGAR restatements.
- HYPE valuation + inverse round-trip tests.
- Risk sizing tests (no leverage caps).
- `ruff` lint/format on every phase.

## 9. Phase log

- **Phase 0 (2026-09-24).** Repo empty; initialised. Researched providers (see
  DATA_SOURCES.md). Findings that changed the brief's suggestions: Stooq now requires an API
  key (2026); FRED ICE BofA series truncated to 3 years (2026); Alpha Vantage free tier is
  25 req/day; Hyperliquid candles limited to latest 5,000; AQAv2 went live for USDC on
  2026-08-26 with ~90% cost-adjusted reserve-yield share to the Assistance Fund.
  **Environment constraint:** this build container's egress policy returns 403 for every
  market/macro data host (Coinbase, Kraken, Binance, Bitstamp, Hyperliquid, FRED, EIA,
  SEC, DefiLlama, Tiingo, Stooq, Yahoo, CBOE). Code is built and tested against fixture
  payloads and synthetic data; live verification and real-data research require either
  network access in this environment or running `market update` on the user's machine.
