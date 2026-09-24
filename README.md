# Prism: local multi-asset trading research & signal platform

Prism is a **personal, local, £0/month** research system for slow, conservative capital
accumulation across crypto, US equities/ETFs and commodity proxies. It does the
following:

- ingests free market, macro and fundamental data into a local DuckDB;
- computes deterministic indicators and market regimes;
- detects three setups;
- backtests them with **point-in-time** data and robustness tests;
- scores and ranks opportunities with fully explained 0–100 scores;
- proposes entry zones and conservative position sizes;
- tracks paper/real positions and a decision journal;
- shows it all in a Streamlit dashboard.

**It never places trades.** No broker or exchange keys exist in V1. It is decision support.
"Nothing is attractive right now" is a normal, valid output: cash is a position.

> **Status of the evidence:** see [RESULTS.md](RESULTS.md). The engine is validated. It
> finds planted edges and rejects noise. The real-data research run is **pending**
> because the build environment had no network access to data providers. Run
> `market update && market research` on your machine to produce it. Until a setup earns a
> PROMISING verdict on real data, treat every signal as unproven.

---

## Philosophy

1. **Is there an edge? Not "prove one exists".** Every setup is compared with random entry
   into the same asset over the same period, net of costs. Tests use non-overlapping
   events, out-of-sample walk-forward windows and parameter plateaus. Negative results are
   reported and not re-tuned.
2. **Point-in-time or nothing.** Every macro or fundamental datum carries an
   `available_at` time and a `pit_method`. Today's reconstructed numbers are never used as
   if known historically. Crypto fundamentals have no defensible history, so they are
   used for current evaluation only.
3. **No silent stitching, no fabricated data.** One provider per series. A provider change
   creates a new, logged series. Missing data stays missing (N/A), and its absence is
   visible as reduced *coverage*.
4. **Deterministic core.** No LLM computes anything. An AI layer could later sit on top of
   the verified structured outputs.
5. **Next-bar execution.** A signal at the daily close fills at the next open, with costs.
   The engine has no same-close fill path at all.

## Architecture

```
config/*.yaml ─► providers (Coinbase, Hyperliquid, Tiingo, FRED/ALFRED, EIA, SEC EDGAR, DefiLlama, CSV)
                    │  raw payloads archived (gzip + SHA-256) → ingestion_runs (provenance)
                    ▼
               validation (schema, OHLC sanity, tz/alignment, duplicates, gaps, jumps, staleness)
                    ▼
               DuckDB (bars per provider, corporate actions, PIT macro, PIT fundamentals, crypto metrics,
                       scans, research runs, positions, journal, alerts)
                    ▼
   features (split-adjusted) + total-return prices ─► regimes (crypto / macro)
                    ▼
   setups A/B/C ─► backtest (event study, simulator, walk-forward, sensitivity, splits) ─► reports
                    ▼
   fundamental modules (HYPE, crypto, equity-PIT, commodity-macro) ─► scoring ─► zones ─► sizing ─► alerts
                    ▼
   CLI (`market …`)  ·  Streamlit dashboard  ·  paper portfolio + journal
```

```
src/market_signal/
  cli/            Typer commands
  data/           providers, http, store (DuckDB schema), validation, calendars, prices, PIT, updaters
  indicators/     technical indicators (each with a stated research purpose)
  regimes/        deterministic regime classifier
  setups/         quality_pullback, breakout_retest, rerating (+ declarative conditions DSL)
  backtest/       events (forward returns, baselines, p-values), engine (simulator), metrics, robustness
  research/       experiment runner, reports, run-all
  fundamentals/   equity (SEC EDGAR PIT), hype, crypto_data (DefiLlama/Hyperliquid), modules
  scoring/        engine (components, zones, status), risk (sizing)
  portfolio/      book (positions, journal analytics), alerts (rules, notifiers)
  models/         domain types
dashboard/        Streamlit app (app.py + views/)
config/           universe, providers, macro, regimes, backtest, scoring, hype, catalysts, alerts, setups/, experiments/
docs/             DATA_SOURCES, SCORING, SETUPS, BACKTESTING, HYPE_MODEL, ROADMAP
tests/            101 tests (synthetic data proofs of no look-ahead, PIT, timing, sizing, HYPE maths, UI render)
```

## Installation

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is fetched automatically.

```bash
git clone <this repo> prism && cd prism
uv sync
cp .env.example .env    # then add the free keys below
uv run market doctor
```

### API keys (all free)

| Variable | Needed for | Get it |
|---|---|---|
| `TIINGO_API_KEY` | equities, ETFs, commodity ETPs (daily) | tiingo.com, free Starter tier |
| `FRED_API_KEY` | macro, regime (macro half), T-bill for HYPE AQAv2 | fred.stlouisfed.org |
| `SEC_USER_AGENT` | equity fundamentals (EDGAR). Not a key: a descriptive UA containing your email | — |
| `EIA_API_KEY` | oil inventories/production (optional) | eia.gov/opendata |

Crypto (Coinbase, Hyperliquid, Bitstamp) and DefiLlama need no keys. With a key missing,
that module is **disabled and reported**, never faked. See
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) for every provider's limits, coverage,
reliability and fallbacks.

## Commands

```bash
uv run market update                 # fetch latest prices, macro (ALFRED vintages), EDGAR, crypto fundamentals
uv run market update --only prices -s BTC -s HYPE --tf 1d
uv run market doctor [--live]        # keys, freshness, data-quality issues, failed runs, provider probes
uv run market scan                   # score & rank everything today; evaluate alerts
uv run market asset HYPE             # exactly why an asset scored what it did; zones; sizing
uv run market hype                   # HYPE valuation: observed / assumed / derived + inverse table
uv run market regime                 # current regimes and factor votes
uv run market backtest quality_pullback   # one experiment (config/experiments/*.yaml)
uv run market research               # ALL experiments → results/RESULTS_generated.md
uv run market experiments            # list experiments
uv run market open HYPE --price 38 --qty 10 --kind INVESTMENT --setup rerating --thesis "…"
uv run market close <position_id> --price 45 --reason valuation --lessons "…"
uv run market portfolio | journal | alerts | alert-add '{"kind":"price_below","symbol":"HYPE","price":56}'
uv run market import-csv file.csv --asset MSFT --source mycsv   # a separate, labelled series
uv run market demo-data              # SYNTHETIC demo DB for offline exploration
uv run market --demo scan            # any command against the synthetic DB (loud banner)
```

A daily routine is `market update && market scan`, then open the dashboard.

## Dashboard

```bash
uv run streamlit run dashboard/app.py              # real data
uv run streamlit run dashboard/app.py -- --demo    # synthetic demo DB (banner on every page)
```

| Page | Contents |
|---|---|
| Market Dashboard | Regimes and their factor votes; a ranked, filterable opportunity table (score, coverage, fundamental, valuation, trend, entry, macro, price, ideal entry, status, research verdict); click a row to research that asset. |
| Asset Research | Weekly, daily or 4h chart with MAs, entry, accumulate and fair-value zones and invalidation. Also: the score breakdown with the reason for every point, price zones, sizing, setup states, fundamentals, and "this setup occurred N times" history. |
| Backtest Lab | Pick an experiment, setup, universe, dates, regime filter and parameters. Shows verdict, event study, per-asset excess, equity and drawdown curves, trades, splits, walk-forward and the sensitivity heatmap. |
| Portfolio / Journal | Record and close paper/real positions, with thesis, evidence, invalidation, "followed the system?", MAE/MFE, current score. Answers "which setups am I good at?" and "where do I break my rules?". |
| HYPE Monitor | Price, supply, revenue run-rates, USDC, AQAv2 revenue, structural bid, buyback yield, net yield, AF balance, band thresholds, the inverse table, sensitivity, and inputs split into observed / assumed / derived. |
| Data Health & Alerts | Freshness, macro vintages, quality issues, ingestion provenance, provider changes, alert rules and events. |

## Reading the signals

- **Score (0–100)** is a weighted sum over *available* components. Weights (config):
  fundamental 25, valuation 20, structure 15, entry 15, macro 10, catalyst 10,
  liquidity 5. **Coverage** tells you how much of the weight had data.
- **Bands:** < 55 ignore · 55–64 watch · 65–74 actionable · 75–84 strong · 85+ exceptional.
  RISK_OFF raises the bar by 10 points.
- **Status:**

  | Status | Meaning |
  |---|---|
  | `ACTIONABLE` / `STRONG` / `EXCEPTIONAL` | The band is met, a setup is ACTIVE, and the price is in the entry zone. |
  | `WAIT ≤ $X` | Attractive, but the price is above the entry zone. |
  | `WATCH` | Watchlist. Also the ceiling when coverage is low or no setup is active. |
  | `IGNORE` | Below the watch band. |

- **Valuation ≠ entry.** An asset can be cheap (valuation 20/20) and still be a poor entry
  today, for example when its 4h chart is extended.
- **Sizing** follows the brief's risk model:
  - TRADE: 0.5 / 0.75 / 1.0% portfolio risk to invalidation, × regime multiplier.
  - INVESTMENT: 5 / 7.5 / 10% of the portfolio, with thesis exits.
  - No leverage. Each position is capped at 25%.
  - Sizing stays at the standard tier until a setup has a demonstrated edge.

Details: [docs/SCORING.md](docs/SCORING.md), [docs/SETUPS.md](docs/SETUPS.md),
[docs/HYPE_MODEL.md](docs/HYPE_MODEL.md).

## Backtesting caveats (read before trusting any number)

- **Survivorship and selection bias.** The universe is today's winners, and free
  delisted-security data is not available. Judge **excess vs random entry**, not raw
  returns.
- **Small samples.** Every report prints the *minimum detectable effect*. A REJECT on an
  underpowered sample means "no large edge", not "no edge".
- **Crypto fundamentals are current-only.** Setup C is tested historically on equities
  only (6 names), so expect INSUFFICIENT_DATA.
- **Costs are assumptions** (configurable). Slippage in fast markets can be worse.
- **Multiple testing.** Every experiment run is logged in `research_runs`. One PROMISING
  result among many attempts proves little.

Full methodology: [docs/BACKTESTING.md](docs/BACKTESTING.md).

## Limitations and known weaknesses

- **HYPE:** only about 2 years of price history, and its valuation inputs are
  current/prospective. The AQAv2 cost adjustment and contributor sell fraction are
  assumptions. Staking emissions are unknown, so the net yield is shown as partial.
- **Copper** has only a USD factor (no free inventory data). There is no ETF fundamentals
  module.
- **Catalysts** are user-maintained in `config/catalysts.yaml`. No free structured feed
  exists.
- **Equity data:**
  - EDGAR coverage starts around 2009–2011.
  - Dimensional share counts (for example GOOGL's share classes) are not in
    `companyfacts`. Per-share metrics use weighted diluted shares.
  - Banks get a reduced metric set.
- **Price bases:** USO's 2020 restructuring is a discontinuity. HYPE/USDC is treated as
  USD.
- **Composite score is not backtested.** It mixes admissible and current-only inputs. The
  setups are what gets tested.
- **Weights are uncalibrated.** Scoring weights are the brief's hypotheses.

## Development

```bash
uv run pytest            # 101 tests
uv run ruff check src tests dashboard && uv run ruff format --check src tests
```

The phase-by-phase build log is in [PLAN.md](PLAN.md). V2 ideas are in
[docs/ROADMAP.md](docs/ROADMAP.md).
