# Prism: local multi-asset trading research & signal platform

Phase 27 adds **Nimble Paper Execution v1**, a deterministic minutes-to-hours
perp experiment with profit objectives, thesis invalidation, expiry, funding-aware
cost decay and a maximum-hold backstop. The existing 28-entry Paper v2 universe
remains the fixed-horizon baseline. Research horizons and position exits are separate.
See [the nimble execution design](docs/NIMBLE_PERP_EXECUTION.md),
[the Phase 27 report](docs/PHASE27_EXECUTION_REPORT.md), and
[Paper v2](docs/EXPLORATORY_PAPER_V2.md). All automated execution remains paper-only.

Phase 28A adds [five specialist Work semantic sensors](docs/WORK_INTELLIGENCE_MONITORS.md),
independent provider metrics and causal reaction research. Prism support is deployed;
Work schema refresh, saved-task activation and a qualifying live-event pilot remain gated.
Work is a semantic sensor, not a trader; Phase 27 execution is unchanged.

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
docs/             DATA_SOURCES, SCORING, SETUPS, BACKTESTING, PERPS_BACKTEST, HYPE_MODEL, ROADMAP
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
uv run market scan                   # score & rank everything today; evaluate alerts (and record the calls)
uv run market track [--calls]        # live track record of past ACTIONABLE / WAIT calls
uv run market brief [--send] [--update-exit N]   # today's answer + freshness warnings + new alerts (Telegram with --send)
uv run market telegram-setup         # find your Telegram chat id and send a test message
uv run market perps                  # perp funding & open-interest monitor (context, not signals)
uv run market update --only perps    # just the perp data: candles, hourly funding, OI snapshot + Binance OI backfill
uv run market oi collect             # just OI: Hyperliquid snapshot + Binance ~30-day backfill (for a scheduler)
uv run market oi status [--gaps]     # OI coverage per venue/coin: latest, gaps, rows (data only, not a signal)
uv run market perp-strategies        # the pre-registered perp strategies and their hypotheses
uv run market perp-research [NAME]   # research perp strategies → verdicts + results/perps/<strategy>/…/report.md
uv run market perp-research --venue binance   # same strategies on Binance years before Hyperliquid's data (unseen)
uv run market perp-paper             # paper-tracking: live-recorded strategy signals and how they did (not traded)
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

A daily routine is `market update && market scan`, then open the dashboard. Run the scan
every day, even when you don't look: each stored scan becomes part of the live track record,
and a missed day is a missed call. Opening the dashboard also stores a scan.

## Dashboard

```bash
uv run streamlit run dashboard/app.py              # real data
uv run streamlit run dashboard/app.py -- --demo    # synthetic demo DB (banner on every page)
```

The dashboard uses progressive disclosure: the daily answer first, the evidence one click
deeper, the full research behind tabs.

| Page | Contents |
|---|---|
| **Today** (default) | The daily answer in ~30 seconds. A headline ("2 ACTIONABLE OPPORTUNITIES" or "NO ACTIONABLE OPPORTUNITIES TODAY" plus the closest candidate). A compact market state (crypto; equities & macro) with headwinds. Then up to 5 opportunity cards, each with status, setup, why it ranks, current vs preferred entry, distance to entry, invalidation, key risk, suggested size, and **current setup strength shown apart from research evidence** (verdict, data coverage, independent sample). Assets needing no action sit in a collapsed, scannable list. |
| **Asset decision** | One asset. A summary comes first: what Prism suggests, Thesis, Entry, Evidence (pooled research for the active setup: independent events, excess vs random entry, hit rate, p-value, walk-forward folds) and Caution. Next comes the weekly, daily or 4h chart with zones. Then **Research details** tabs: score breakdown, price zones and sizing, setup states, the pooled setup research (all horizons, walk-forward, sensitivity, simulation, provenance), this asset's own event studies (on demand) and fundamentals. |
| All assets | The dense screener: regimes with factor votes and the full ranked, filterable table. Click a row to open the asset. |
| Backtest Lab | Pick an experiment, setup, universe, dates, regime filter and parameters. Shows verdict, event study, per-asset excess, equity and drawdown curves, trades, splits, walk-forward and the sensitivity heatmap. |
| HYPE Monitor | Price, supply, revenue run-rates, USDC, AQAv2 revenue, structural bid, buyback yield, net yield, AF balance, band thresholds, the inverse table, sensitivity, and inputs split into observed / assumed / derived. |
| Perps | Positioning context from Hyperliquid perpetual futures. Shows annualised funding (latest, 7-day and 30-day), where the 7-day reading sits against the coin's own past year (crowded long, crowded short or neutral), who pays whom, open interest with its 7-day change, and the venue's maximum leverage. It also charts funding history and open interest, and shows **strategy research**: each pre-registered perp strategy's verdict, and what it signals today. Signals count as candidates only if the strategy passed research. |
| **Track record** | Live evidence. Every stored scan is a dated record of what Prism said. ACTIONABLE calls are scored at 1 and 3 months against a random pick from the same asset class, net of costs. WAIT calls are scored on whether the price reached the preferred entry, and whether waiting beat buying immediately ("wait edge"). Results are split by the research verdict at the time of the call. Only calls recorded at the time count: nothing is backfilled. |
| Portfolio / Journal | Record and close paper/real positions, with thesis, evidence, invalidation, "followed the system?", MAE/MFE, current score. Answers "which setups am I good at?" and "where do I break my rules?". |
| Data health & alerts | Freshness, macro vintages, quality issues, ingestion provenance, provider changes, alert rules and events. |

The plain-English text on Today and Asset decision comes from `dashboard/presenter.py`, a
deterministic presentation layer over the scan and the saved research runs. It has no LLM
and computes no new analytics. Decisions on those pages are ACTIONABLE / WAIT / WATCH /
IGNORE. The engine's EXCEPTIONAL / STRONG / ACTIONABLE all display as ACTIONABLE, and the
band is shown as the strength of the current setup. A setup whose research verdict is
REJECT is never displayed as ACTIONABLE or WAIT: it is shown as WATCH, with the reason.

### When an update is running

Prism's database (DuckDB) lets one process write at a time. While `market update` or a scan is
running, the dashboard shows the last answer with a "Prism is updating" notice. If it has no
answer to show yet, it shows a short holding page that reloads by itself when the database is
free. CLI commands, including the scheduled job, wait for the database instead of failing:
up to 10 minutes by default, or set `PRISM_LOCK_TIMEOUT=<seconds>` to change it.

## Daily brief on Telegram (optional)

Prism can message you each morning with Today's answer: actionable and waiting ideas with
their preferred prices, the market state, any new alerts, and a warning if the data is out
of date or the update failed. It is the same text the dashboard shows, from the same rules.

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token into `.env` as
   `TELEGRAM_BOT_TOKEN`.
2. Open your new bot and press **Start** (or send it any message).
3. Run `uv run market telegram-setup`. Copy the chat id it prints into `.env` as
   `TELEGRAM_CHAT_ID`, then run it again to receive a test message.
4. Add `uv run market brief --send --update-exit <update's exit code>` after `market scan`
   in your scheduled job.

Alerts are delivered once each, in the next brief, whether they were raised by the scheduled
scan or by opening the dashboard. The bot only ever sends to your chat id.

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

The [Strategy Lab audit and phased design](docs/STRATEGY_LAB.md) maps the current research
infrastructure and documents the isolated strategy-definition foundation.
Intraday market data is described in [docs/INTRADAY.md](docs/INTRADAY.md). The descriptive
structural-price and trade-path primitives (swings, equal highs/lows, failed breakouts,
structure shifts, retests, MFE/MAE/R with explicit OHLC ambiguity) are in
[docs/STRUCTURE.md](docs/STRUCTURE.md). The Phase 17 preregistered falsification study of
sweep / rejection / structure-shift / retest claims (EXPLORATORY; nothing survived) is in
[docs/PHASE17_FALSIFICATION.md](docs/PHASE17_FALSIFICATION.md). The Phase 18 preregistered study of
relative strength, BTC beta/correlation, residuals and cross-asset dislocation (EXPLORATORY;
nothing robust, no USD-directional or after-cost relative edge) is in
[docs/PHASE18_RELATIVE_STRENGTH.md](docs/PHASE18_RELATIVE_STRENGTH.md). The Phase 19 preregistered
study of open interest × price × funding (EXPLORATORY; 23 days of Binance OI; nothing robust,
no OI information demonstrated beyond price, Hyperliquid OI insufficient) is in
[docs/PHASE19_OI_PRICE.md](docs/PHASE19_OI_PRICE.md). Phase 20 adds time-varying edge and
strategy-lifecycle evidence — rolling, recency-weighted, regime-local and stress-split
evidence beside lifetime evidence, edge states, a hysteretic lifecycle simulated causally,
synthetic calibration and `market lab edge` queries (research only; nothing promoted) — in
[docs/EDGE_LIFECYCLE.md](docs/EDGE_LIFECYCLE.md). Phase 21 adds fast prospective candidate incubation — frozen
CONSERVATIVE / BALANCED / AGGRESSIVE exploratory-paper admission policies, shadow paper
intents and episodes, opportunity-rate and long/short diagnostics, temporary-edge calibration
and `market lab incubation` queries (exploratory paper only; no state authorizes real
trading) — in [docs/CANDIDATE_INCUBATION.md](docs/CANDIDATE_INCUBATION.md). Phase 22 adds the first intraday
strategy-discovery catalogue — 125 simple, causal, long/short-symmetric 1H/15m hypotheses
evaluated after costs with opportunity frequency as a first-class metric, synthetic null and
planted-edge calibration and `market lab discovery` queries (exploratory; real-data run
pending) — in [docs/PHASE22_INTRADAY_DISCOVERY.md](docs/PHASE22_INTRADAY_DISCOVERY.md). Phase 23 adds the context
intelligence layer — an append-only market-event ledger with first-seen semantics, an official
macro calendar with first prints and surprises, official status/regulator feeds, a validated
ChatGPT Work import route, venue-separated positioning/crowding context, fixed-hour Hyperliquid
OI capture, point-in-time context snapshots, research-only theses and `market context` queries
(data and semantics only; nothing trades) — in [docs/CONTEXT_INTELLIGENCE.md](docs/CONTEXT_INTELLIGENCE.md). Phase 24A adds
an always-on Hyperliquid WebSocket microstructure collector — deterministic, versioned
1-minute aggregates (`microstructure_1m_v1`: taker-signed flow, prints, causal large prints,
time-weighted spread/depth/imbalance, book dynamics, minute-end OI/funding) with explicit
COMPLETE/PARTIAL/GAP quality, bounded revisions, a single-writer spool → DuckDB ingest, a
causal research loader and `market microstructure` queries (data only; prospective from its
production cutover) — in [docs/MICROSTRUCTURE_COLLECTION.md](docs/MICROSTRUCTURE_COLLECTION.md).

Phase 29 adds [free public event acquisition](docs/FREE_EVENT_SOURCES.md): causal status/RSS/GitHub collection through the existing context/outbox boundary, with £0 new data subscriptions.
