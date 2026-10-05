# Strategy Lab: repository audit and incremental design

Inspection date: 2026-10-03. This is a design grounded in the existing implementation,
including the local database and generated reports. Only the representation foundation
(section 6), research governance ledger (section 7), daily feature compiler (section 8),
fast screen (section 9), preregistered search batches (section 10), structured
strategy families (section 11), evidence profiles (section 12), prospective forward
tracking (section 13) and full research with independent validation (section 14) are
implemented, plus the first consumer of that evidence, the perps co-pilot (section 15),
cross-venue historical corroboration (section 16) and a second consumer, the paper-only
auto-trader (section 17: a simulated account that can never place a real order), with
read-only paper observability (section 18).
No production strategies, evaluation rules, paper records or scanners were changed; the
co-pilot and the paper trader reuse the existing Telegram client. The only schema changes
are the additive migrations described in sections 7, 10, 12, 13, 14, 15, 16, 17 and 18.

## 1. Current architecture

| Area | Existing implementation | Reuse |
|---|---|---|
| Spot strategy interface | `setups/base.py`: `Setup`, `SetupContext`, `SetupOutput(signal, stop, eligible, diagnostics)`, registry, `edge_trigger`, `conditions_mask`, `ConditionsSetup` | Preserve the eligibility/diagnostic contract and causal trigger helper. Adapt to this interface rather than replace it. |
| Spot strategies | `quality_pullback.py`, `breakout_retest.py`, `rerating.py`; defaults in `config/setups/`; experiment overrides in `config/experiments/` | Keep existing implementations and defaults. Breakout/retest is a state machine; the small conjunction DSL cannot faithfully replace it. |
| Spot experiments | `research/runner.py`: `Experiment`, `ResearchContext`, `evaluate_universe`, `run_experiment`, `save_report`; `research/run_all.py` | Reuse loaders, feature cache, PIT fundamentals/regimes, reporting and research orchestration. |
| Perp strategies | `perps/strategies.py`: `PerpStrategy`, `Signals`, `STRATEGIES`; each declares hypothesis, defaults, primary horizon, sensitivity and walk-forward choices | Retain registered Python strategies; eventually allow an explicitly supplied compiled candidate alongside them. |
| Price features | `features.py`: command-scoped `FeatureStore`; `indicators/technical.py`: causal SMA/EMA, Wilder RSI/ATR, ROC, ranges, volatility, relative volume | Reuse individual indicator functions. The full feature frame assumes daily calendar horizons and annualisation. |
| Event studies | `backtest/events.py`: `forward_returns`, `AssetEvents`, `run_event_study`, `baseline_bars`, `decluster`, random-entry test | Shared statistical core for both markets; reuse forward-return calculations for screening. |
| Perp returns | `perps/backtest.py`: `daily_funding`, `perp_frame`, `side_forward_returns`, `perp_asset_events` | Reuse daily, side-specific returns with funding and costs. |
| Robustness | `backtest/robustness.py`: `walk_forward`, `window_excess`, `sensitivity_grid`, `plateau_verdict`, `split_by_label`; shared `automatic_verdict` in `research/runner.py` | Stage 2 foundation. Preserve defaults, grids, seed and verdict implementation; add explicit completeness checks for Lab promotions. |
| Simulation | `backtest/engine.py` for spot; `perps/backtest.py` for perps; shared `backtest/metrics.py` | Preserve separate engines and market-specific execution assumptions. No new simulator is needed for daily candidates. |
| Persistence | `data/store.py`: DuckDB migrations, ingestion/run provenance, raw archives, scans, positions/journal, `research_runs`, perp tables | Extend using additive migrations and a single writer. Keep existing reports and identifiers. |
| Scanner and presentation | `scoring/engine.py`, `presenter.py`, `brief.py`, `dashboard/views/perps.py` | Later integration needs explicit versioned promotion records, not discovery of every Lab definition. |

Paths above are relative to `src/market_signal/` unless prefixed with `config/` or
`dashboard/`.

### Strategies, research and execution

Spot has three maintained strategies: Quality Pullback (trend plus pullback/momentum),
Breakout + Retest (a sequential breakout/retest state machine), and Fundamental Re-rating
(PIT equities; crypto current-only). The additional conditions DSL already supports named
boolean predicates and numeric `min/max/gt/lt` bounds. `control_trend_only` is an existing
economic control. Composite scanner scores are not themselves the backtested strategies.

Perps has `trend_ls`, `funding_fade`, and `breakout_ls`, each producing both long and short
signals and stops. `funding_fade` ranks trailing average daily funding against the coin's
history. No existing perp strategy uses OI. Perp `Signals` currently has no eligibility
mask, unlike spot `SetupOutput`.

Spot event returns enter at the next open and exit at the horizon close, on total-return
prices, net of configured fee/slippage assumptions. Signal features use split-adjusted
prices. Event baselines use each asset's eligible bars; independent events are spaced by
the horizon. Perp studies reuse this machinery with separate `coin:long`/`coin:short`
series and same-side baselines. Returns are on notional, without leverage, subtracting
signed funding weighted by price. Windows spanning missing funding are excluded.

Walk-forward uses anchored training, window-local baselines, training purges, at most two
varying parameters, and reports default-parameter OOS outcomes as well as the selected
parameters. Sensitivity uses up to two axes and assesses a plateau around defaults.
The spot runner also reports regime, benchmark-trend, volatility, rate-cycle and class
splits. The perp runner reports side and coin/side breakdowns but does not yet attach the
spot runner's regime/trend/rate splits.

Both simulators schedule entries for the next open, skip entries gapping through their
stop, use conservative adverse-first intrabar rules, and fill close-decided time exits at
the next open. Spot sizes by stop risk or a fixed investment allocation, with cash and
gross-exposure limits. Perps uses isolated margin, risk-sized notional, bounded leverage
derived from stop/liquidation distance, fees on fills, extra stop slippage, funding erosion
and conservative liquidation loss. These are distinct from event-study holding returns.
Perp missing funding during a position is carried from the last known rate (zero if none),
and imputed days are counted. Neither simulator models an exchange order book.

Current configured spot per-side fee + slippage is 10 + 10 bps for crypto (HYPE 10 + 20),
5 + 5 for equities/commodities, and 5 + 3 for ETFs. Perp taker fee assumptions are 4.5 bps
on Hyperliquid and 5 on Binance, plus coin-specific slippage and 10 bps extra stop
slippage. Perp risk defaults include 0.5% account risk, 3x leverage cap, 50% equity per
position's notional and 2x total gross notional. These are repository configuration values,
not a verification of current exchange fee schedules.

### Data and point-in-time handling

Spot bars are keyed by asset, timeframe, provider and open timestamp. The update path
validates OHLC, timestamps and duplicates, stores only closed bars, logs gaps/revisions,
and archives raw payloads with SHA-256 and ingestion metadata. Provider series are explicit.
Raw bars and corporate actions generate split/total-return views on read.

Configured crypto spot data uses Coinbase (4h aggregated from complete 1h buckets) and
Hyperliquid for HYPE; equities/ETFs/commodity proxies use Tiingo. Bitstamp, Stooq and CSV
paths also exist. Daily and 4h spot series are configured; `Timeframe.H1` and provider
capabilities exist, but the inspected database has no stored 1h series. Weekly bars are
derived on read. Macro uses FRED/ALFRED vintages and EIA release rules; equity fundamentals
use EDGAR filing availability. `data/pit.py` rejects reconstructed data for historical
research. Crypto fundamentals accumulate prospective snapshots as well as separately
labelled reconstructed history.

Perp `perp_bars`, `perp_funding` and `perp_snapshots` are separate from spot. Hyperliquid
and Binance ingestion/loaders currently hard-code **daily** perp candles. Funding history
has settlement timestamps and `available_at`; Hyperliquid snapshots contain mark/oracle
prices, premium, current funding, OI in coins, OI notional and max leverage. The funding
history and current predicted funding snapshot are different datasets. Binance currently
has candle/funding ingestion, not an OI feed. No liquidation tape, CVD or trade/order-flow
history is ingested.

Point-in-time primitives already exist: explicit `close_time`, backward regime/label
joins, vintage-aware macro lookup, closed-bar filtering, and complete-bucket aggregation.
They are useful building blocks, not a complete multi-timeframe feature service.

### Paper, scanner, Telegram and CLI

`portfolio/book.py` records manually entered paper/real positions and journal entries;
it does not submit orders. Separately, `perps/paper.py` records strategy checks during
`market update`, only for the newest closed bar within 36 hours. Checks are write-once,
missed days are not backfilled, and evaluation uses only live-checked bars as the baseline.
This is forward signal tracking, not an automated paper fill engine. Spot
`research/track_record.py` scores persisted ACTIONABLE/WAIT episodes.

The scanner evaluates the three spot setups, then combines technicals, fundamentals,
regime, entry zones and risk sizing. It persists assessments. `presenter.py` separates
research evidence from scores and caps rejected setups at WATCH for displayed decisions.
Evidence currently loads the latest saved run by **name**. Perp dashboard signals are
labelled candidates for PROMISING or WEAK_POSITIVE verdicts; all preregistered perp
strategies are paper-checked regardless of verdict. The daily Telegram brief renders the
stored spot scan, freshness and undelivered alerts through `portfolio/telegram.py` when
`market brief --send` is explicitly invoked. It does not currently run a general perp Lab
scanner. No Telegram messages were sent during this task.

Relevant commands:

- Data: `update`, `doctor`, `import-csv`, `assets`, `demo-data`.
- Spot research: `experiments`, `backtest [experiment-or-path]`, `research [--only …]`.
- Perps: `perps`, `perp-strategies`, `perp-research [name] [--venue binance]`, `perp-paper`.
- Scanner/output: `scan`, `asset`, `regime`, `hype`, `brief`, `track`, `telegram-setup`.
- Manual book: `open`, `close`, `portfolio`, `journal`, `alerts`, `alert-add`.

### Existing evidence inspected

README, RESULTS.md and parts of ROADMAP/PLAN still describe real research as pending.
The local artifacts and read-only database inspection show it has run:

- Six spot experiment reports dated September 25: saved `breakout_retest` is PROMISING;
  the other five are REJECT. Its saved positive excess differs substantially by asset
  class; it should not be read as proof across all classes.
- Latest October 3 reports for all three perp strategies are REJECT on both Hyperliquid
  and the earlier Binance windows. An earlier Binance `trend_ls` report is also retained.
- There are 13 saved `research_runs` rows. Inspecting these did not rerun the research or
  independently recertify the saved verdicts.
- Perp bars: 11,755 Hyperliquid and 11,923 Binance rows, all daily. Funding rows: 147,416
  and 35,845 respectively. Price history and usable funding history have different starts.
- OI: **18 Hyperliquid snapshot rows, all October 3**. This cannot support historical
  24-hour OI changes yet. A same-day sample must not be represented as a historical OI feed.

Generated reports live under ignored `results/` and include Markdown, summary JSON,
events, trades and equity CSVs. References: `results/RESULTS_generated.md`,
`results/perps/<strategy>/<run>/report.md`, `docs/BACKTESTING.md`,
`docs/PERPS_BACKTEST.md`, `docs/SETUPS.md`, `docs/DATA_SOURCES.md` and `PLAN.md`.

## 2. Gaps and compatibility constraints

| Requirement | Already present | Remaining work |
|---|---|---|
| Standard strategies | Spot protocol/DSL, perp callable strategies, YAML experiments | Typed, versioned Lab definitions; compiler/adapters without forcing state machines into conjunctions |
| Feature vocabulary | Price/volume indicators; daily funding and rolling funding percentile | Parameterised feature catalog with units, warmup, market, timeframe, availability and implementation versions; volume z-score/change, relative strength and OI research features |
| Many assets/timeframes | Multi-asset daily research, spot 4h/weekly display | Explicit dataset selection and general closed-bar MTF alignment; perp 1h/4h ingestion and execution validation |
| Cheap screening | Vectorised forward-return functions, baselines, basic summary metrics | Cached returns/features, batched descriptive summaries, fixed selection policy, resource limits; no simulations/WF/bootstrap per candidate |
| Full validation | `run_perp_research`, spot runner, shared robustness/verdicts | Explicit candidate injection, eligibility masks, immutable evaluation plans, provenance, perp regime splits, validation completeness |
| Permanent ledger | Successful report saves append `research_runs`, including negative verdicts | Record submissions and attempts before work, errors/cancellations/zero events, canonical identity, lineage, dataset snapshots and methodology versions |
| Multiple testing | Roadmap calls for block bootstrap and Benjamini–Hochberg across `research_runs` | Neither implemented; define trial families and holdouts before large-scale screening |
| Promotion | Current name-based evidence, live checks, manual positions | Version-specific research → paper → scanner eligibility, matched dataset/policy evidence and independent promotion records |

Details that constrain reuse:

1. **Feature meanings differ.** Spot ATR is Wilder-smoothed with a particular seed;
   perp `_atr` is a rolling simple mean including its first true range. Percentile
   conventions also differ: technical `rolling_percentile` and funding `rolling.rank`
   have different tie rules. A shared feature catalog must name/version both meanings.
   `compute_features` uses daily class horizons even when called on 4h data; do not call
   a 30-bar intraday ROC “one month” or apply daily volatility annualisation to 1h data.
2. **Eligibility is incomplete for new perp candidates.** Current research defaults to
   funding-defined bars rather than each strategy's full feature warmup. The adapter
   must supply explicit per-side eligibility to `perp_asset_events`; this changes the
   baseline for new Lab runs and needs a recorded methodology version. Regime filters
   and matched baselines also need an explicit policy: spot currently filters signals
   by regime but retains a broader eligible baseline. The trend-only control is a
   separate experiment; `automatic_verdict` does not automatically test a pullback
   candidate against that control, despite the economic comparison described in RESULTS.md.
3. **Existing verdicts are not a promotion state machine.** `automatic_verdict` skips
   checks for missing WF/sensitivity objects and can return PROMISING with a plateau
   even if WF is absent. `--no-wf`/`--no-sens` are valid existing research options, not
   evidence of complete Lab validation. A separate Lab completeness gate must require
   the prescribed checks before promotion. Preserve historical verdicts as issued.
4. **Statistical assumptions need explicit review at scale.** Random-entry draws match
   the number of declustered observed events, but do not enforce horizon spacing within
   each random draw. Coin/side independence also does not remove correlations between
   coins. Walk-forward purges training with a calendar-time approximation; test windows
   are selected by signal time and can have outcomes beyond a fold boundary. Arbitrary
   gaps/intraday horizons need outcome timestamps and explicit outcome boundaries.
   `run_event_study(n_boot=0)` currently yields a primary p-value of 1 when events exist;
   it is not an appropriate “screen only” public result without changing its contract.
5. **Funding is a daily approximation.** `daily_funding` infers cadence from the median
   spacing of the entire supplied history, rounds settlements to the nearest minute,
   and scales sufficiently covered partial days. Appending a different funding cadence
   could change earlier coverage calculations. Daily funding must not be broadcast into
   earlier intraday decisions; settlement availability and changing schedules need
   focused tests and a versioned policy before intraday or strict PIT Lab evaluation.
6. **Historical margin inputs are not fully PIT.** `load_perp_input` uses the latest
   Hyperliquid max-leverage snapshot (or config default) for the whole simulation. Label
   this as an assumption, or introduce as-of margin tables under a new methodology.
7. **Simulation timing deserves a separate audit before generalisation.** Both engines
   iterate by bar close, processing a whole symbol's bar before another symbol's bar at
   that timestamp. Their shared-equity sizing can therefore depend on another symbol's
   later-in-bar result when filling an earlier open. Test global open/close ordering
   before claiming an intraday or cross-calendar portfolio implementation. No execution
   changes are made here.
8. **Provenance and versioning are partial.** Spot fingerprints cover raw bar series,
   not every corporate-action/macro/fundamental dependency. Perp reports record counts,
   ranges and coverage but no full data content hash. Config hashes/commits omit some
   resolved strategy defaults, feature semantics, dirty code and dependencies.
   Perp upserts overwrite revisions. Paper hashes include name/defaults, not signal
   implementation; paper primary keys omit the hash. Latest evidence is selected by
   name rather than exact strategy version. Timestamp-to-the-second report directories
   can collide. These patterns are insufficient for a permanent high-volume ledger.
9. **Source isolation must be explicit in the Lab.** Default perp loaders can fall back
   to synthetic data; `load_snapshots` includes real and synthetic sources together for
   Hyperliquid. Lab manifests must pin exact sources and forbid silent fallback/mixing.

These are inspection findings and design constraints, not fixes hidden inside this first
step. Existing regression tests pass; they do not prove these additional properties.

## 3. Proposed architecture and control boundaries

```mermaid
flowchart TD
    H[Human hypothesis / future constrained proposal] --> L[Append submission to ledger]
    L --> D[Validate and identify strategy definition]
    D --> C[Compile using trusted feature catalog]
    P[Operator-owned immutable evaluation plan] --> C
    M[Dataset manifest and PIT loaders] --> F[Causal features and availability alignment]
    C --> S[Signals, stops, eligibility, diagnostics]
    F --> S
    S --> Q[Fast descriptive screen on discovery data]
    Q --> R[Full Prism research on prescribed validation data]
    P --> Q
    P --> R
    R --> G[Completeness and statistical promotion gate]
    G --> T[Version-specific prospective paper eligibility]
    T --> A[Explicit scanner eligibility]
    A --> B[Existing scanner / Telegram presentation]
    Q --> E[Append run results and failures to ledger]
    R --> E
    T --> E
```

Keep three separate objects:

- **Hypothesis/definition:** economic explanation, source, declared ancestry and immutable
  rule. Single directional candidates simplify side-specific baselines. A future composite
  can refer to long/short child IDs; existing two-sided Python strategies remain supported.
- **Evaluation plan:** operator-owned universe, exact venues/price bases, discovery and
  validation windows, warmup, horizons, costs, execution model, null model, seeds, grids,
  thresholds and required checks. Snapshot before evaluation. The hypothesis cannot
  override it. Regenerating a plan after seeing outcomes is another recorded research
  decision, not a silent retry. A future AI process gets submission access only, not
  writable source/config, result rows, database credentials or evaluation settings.
- **Run:** definition ID + plan ID + dataset manifest ID + feature/compiler/code version +
  attempt ID, status history, results and artifacts. Same strategy on another asset,
  timeframe/period or venue is traceable without pretending to be independent evidence.

### Feature and MTF contract

Feature calculation should be pure and cached by exact dataset, asset, market, venue,
price basis, native timeframe, feature version and parameters. Reuse existing individual
indicators and complete-bar aggregation. Calculate only requested dependencies; keep
forward outcomes in a separate cache that signal evaluation cannot read.

Every feature output needs its value, native bar identity, `available_at`, validity and
coverage/staleness metadata. At trigger close T, select only the latest completed source
bar available at or before T with a backward as-of join. A daily bar opened at midnight
is unavailable until its following midnight close; it cannot inform that day's 1h bars.
Equal close timestamps may be used only under a documented availability/latency policy.
Normalise UTC and timestamp resolution; require sorted, unique series. Snapshot data is
available at capture time, not retroactively at the market timestamp it describes.

Missing input makes a condition **undefined** and the decision **ineligible**, not an
ordinary false observation. No backfill. Set maximum source age and completeness rules
in the trusted plan/catalog, rather than filling through long outages. Crossing operators
must eventually require valid consecutive native observations and documented behaviour
on the trigger clock. Warmup is loaded before a window; signal/output eligibility is
masked to the window. Enforce outcome cutoffs separately from signal cutoffs.

Daily price/volume/funding features come first. Add OI only when a manifest establishes
coverage and capture times. Choose explicitly between OI in contracts/coins and USD
notional: price changes alone can move USD OI. Price-up/OI-up and the other three quadrants
must use matched elapsed windows, not an unqualified number of irregular snapshots.
Funding percentile needs explicit average/lookback/minimum history and tie convention.
Basis from snapshots is prospective; liquidation/CVD need new provider datasets.

### Fast screen versus full research

Fast screen reuses `forward_returns` / `side_forward_returns` with cached horizon arrays.
Aggregate finite, eligible events by asset, side, horizon and predeclared time block:
signal/evaluable/completed counts, mean/median return, hit rate, excess versus eligible
same-asset/same-side/window baseline, dispersion and asset/time consistency. A rough
mean/standard-deviation ratio is descriptive; overlapping event returns are not an
annualised portfolio Sharpe. Missing horizons stay missing, with exclusions reported.
Keep costs and daily funding inexpensive and consistent with the selected policy.

Screen outcomes are `REJECT_SCREEN`, `REVIEW_FULL` or `INSUFFICIENT_DATA`, never PROMISING
or live-eligible. No parameter search, bootstrap, WF, sensitivity or portfolio simulation
is implicitly invoked. Screens use designated discovery data only. Full validation must
retain untouched data; running ordinary WF after screening on its test periods does not
restore out-of-sample status.

For Stage 2, inject a compiled candidate into `perps/research.py` explicitly, keeping
`get_strategy(name)` as the default for existing callers. Adapt signals/stops into
`PerpInput` and explicit eligibility into `AssetEvents`. Reuse `_events`, the shared
event study, robustness functions, `simulate_perps`, verdict calculation and report
rendering. For spot, adapt to `Setup`/`SetupOutput` and allow explicit setup injection in
the runner rather than mutating the production registry. Existing stateful strategies
can use implementation-reference adapters with code/config hashes; they need not be
translated into a lossy DSL.

Extend completeness/provenance and missing splits in small follow-ups. Lab reports get
their own namespace and link back to existing `research_runs`; the legacy latest-by-name
evidence reader must never interpret a screen as validation. Daily engine reuse does not
establish intraday execution correctness.

### Permanent ledger and multiple testing

Use additive tables in the existing DuckDB, written through a Lab-only service. Do not
create another result database or overwrite old `research_runs`. Suggested tables (the
schema actually implemented in Step 2 is described in section 7):

| Table | Immutable record |
|---|---|
| `lab_submissions` | Receipt ID/time, raw proposal, source, authored timestamp, hypothesis text, validation outcome/error, canonical ID when valid; includes invalid submissions |
| `lab_definitions` | Full canonical definition, schema/catalog version, content ID; duplicate submissions link here, not to a fresh strategy |
| `lab_lineage` | Parent/child IDs, declared family and revision reason; ancestry never deletes a failed parent |
| `lab_plans` / `lab_datasets` | Resolved methodology JSON/hash and exact data manifest/hash; venue, assets, timeframes, periods, coverage, input versions |
| `lab_runs` | Attempt ID, stage, strategy/plan/data/code IDs, start time and old research-run link if applicable |
| `lab_run_events` | Append-only lifecycle/results/errors/cancellation records, artifact paths and checksums; current status is a projection |
| `lab_promotions` | Version-specific paper/scanner eligibility, supporting run IDs, reason and later revocation |

Commit a submission/run before invoking evaluation. Record failed, cancelled, timed-out,
zero-event and unavailable-data attempts. A crash leaves an observable unfinished run;
recovery appends an interruption record. Never auto-replace earlier results. Use run IDs
in directories (`results/lab/<strategy_id>/<run_id>/`), atomically publish artifacts and
check their hashes. Store resolved plans and canonical rules with reports. A hash verifies
an input but cannot reconstruct overwritten history: retain exact local input snapshots
or reconstructible revision sets, with backups of the DB and artifacts. Keep provider
data local. Application-level append-only access is not tamper-proof storage against a
database owner.

Content identity detects renames and condition reordering. It cannot prove general
logical/economic equivalence or catch every near-duplicate parameter tweak. Preserve all
submissions, parameter variants, family/parent links and similarity diagnostics. Explicitly
register the testing family and all planned endpoints before evaluation. Identical reruns
are reproducibility checks, not new independent discoveries; changing data, horizon,
methodology or inspected results is visible in the trial history.

`docs/ROADMAP.md` already proposes block bootstrap and Benjamini–Hochberg. Add a batch
manifest listing all candidates tested, including screen failures, with raw p-values and
adjusted q-values where valid. Do not run BH only over survivors evaluated on the same
data that selected them. A defensible initial design screens on discovery data and runs
preregistered family tests on an untouched validation window. Define how asset/side/horizon
claims and revisions count, and address correlated strategies/assets and repeated batches
before asserting FDR control. Ledger visibility alone and a per-run p < 0.05 threshold do
not control data mining; BH does not repair invalid or adaptively selected p-values.

## 4. Phases and review boundaries

| Phase | Files/modules | Reviewable completion criterion |
|---|---|---|
| 1 — Representation (implemented) | New `research/lab/spec.py`, `config/lab/examples/`, `tests/test_lab_spec.py`, this report | Strict data-only documents, explicit timeframes/ATR meaning, stable identity, no production imports or evaluation |
| 2 — Ledger and locked plans (implemented, section 7) | Add `research/lab/ledger.py`, `policy.py`, `datasets.py`; additive migrations in `data/store.py`; Lab-only `cli/lab_cmds.py` | Every attempted submission/run is durably recorded before work; duplicate/lineage/crash tests; plan/data/code hashes; exact inputs retained. No mass screening yet |
| 3 — Daily compiler/features (implemented, section 8) | Added `research/lab/vocabulary.py`, `features.py`, `compiler.py`; reuse `indicators/technical.py`, `data/prices.py`, perp helpers. `alignment.py`/`adapters.py` deferred: daily-only needs no MTF alignment, and adapters belong with Phase 5 injection | Small daily vocabulary compiles to signals/stops/eligibility; synthetic hand-calculation, missing-data, truncation/future-shock and legacy parity tests; no registry mutation |
| 4 — Fast screen (implemented, section 9) | Added `research/lab/screen.py`, plan schema v2 in `policy.py`; Lab CLI `preregister`, `screen`. `report.py`/`compare`/`history` deferred: results are inspected through `market lab experiment` | Bounded deterministic batches on discovery data; cached returns; all failures persisted; asset/time/side summaries; measured runtime/memory; cannot invoke full research or promote |
| 5a — Search batches and FDR (implemented, section 10) | Added `research/lab/batch.py`, migration 8, `market lab batch create/run/show`, `market lab batches` | Frozen testing families, BH over one primary test per strategy, family-aware statuses, holdout refusal, immutable analyses |
| 6 — Structured strategy families (implemented, section 11) | Added `research/lab/families.py`, `config/lab/families/*.yaml`, `market lab families`, `family show`, `batch generate` | Versioned economic families with bounded grids generate deterministic Phase 1 variants and Phase 5 manifests without running them |
| 7 — Evidence profiles (implemented, section 12) | Added `research/lab/evidence.py`, migration 9, `market lab evidence build/report/show` | Consumer-neutral, versioned, append-only profiles with stage provenance; VALIDATED reserved |
| 5b/9 — Full research and independent validation (implemented for daily perps, section 14) | Added `research/lab/adapter.py`, `research/lab/validation.py`, migration 12, profile schema 4, `market lab research …` / `validation …` | Frozen registrations; adapter reuses the screen, event study and Prism robustness functions (parity-checked); validation only on plan-reserved periods, looked at once, exposure recorded; final holdout never read; completeness-gated statuses; VALIDATED reserved. Spot and portfolio simulation not yet supported |
| 6 — Perp MTF and OI | Extend `perps/data.py`, `perps/binance.py`, provider loaders and dataset manifests; extend Lab features/alignment and research policy | Stored 1h/4h data with completeness/availability; settlement-level funding treatment; same-close/gap/stale/snapshot tests; portfolio timing validated before intraday execution claims |
| 8 — Prospective forward tracking (implemented, section 13) | Added `research/lab/forward.py`, migration 11, profile schema 3, `market lab forward …` | Explicit enrollment freezing the evidence profile; live-only daily evaluations (no backfill); write-once T+1-open/T+h-close outcomes; descriptive `paper_forward` profiles; no execution |
| 10 — Perps co-pilot (implemented, section 15) | Added `copilot/` (policy, engine, render), migration 13, `market lab copilot …`; shared live-window helpers in `research/lab/forward.py` | Versioned consumer policy over governed evidence; live-window-only decisions recorded once per policy/strategy/asset/bar; append-only delivery attempts; no Lab writes; no execution fields |
| 7 — Paper and scanner eligibility | Add `research/lab/promotion.py`; additive versioned paper storage and opt-in adapters in `perps/paper.py`, `scoring/engine.py`, presenter/brief/CLI | Exact strategy + implementation + policy versions carry evidence; paper starts prospectively, no backfill; explicit scanner allowlist; existing strategies unchanged |
| Later — Constrained hypothesis generation | Submission client only, reading the versioned catalog and writing proposals through ledger service | No AI dependency until separately requested; model cannot edit evaluator, plans, tests, data/results or promotion policy |

The MTF contract is specified now and encoded in references. Enabling additional data and
execution clocks is a later capability, not an implicit consequence of accepting an MTF
document. Ledger and discovery/validation boundaries must precede bulk evaluation.

## 5. Risks and decisions still required

- **Leakage/alignment:** close time versus bar open, publication/capture latency, incomplete
  higher bars, stale as-of matches, irregular OI samples, funding schedule changes, weekly
  completeness, adjusted prices and portfolio event order. The existing `weekly_from_daily`
  docstring mentions `weekly_asof`, but no such helper is implemented. Add boundary tests
  before a general MTF research path uses weekly data.
- **Parameter/data snooping:** specify untouched periods, hypothesis families, primary
  endpoints, revision policy and promotion thresholds before looking at new screen output.
  Earlier Binance windows already used by existing research are not fresh holdouts for
  hypotheses derived from those results. Today's universe remains selection-biased.
- **Versioning:** separate authored label, behaviour ID, code/catalog version, evaluation
  plan and attempt. A changed feature implementation cannot inherit an old strategy's
  paper evidence. Historical result imports must be labelled retrospective, with missing
  provenance explicit; do not invent preregistration timestamps.
- **Reproducibility:** snapshot all inputs, not just bar counts; record Python/dependency
  lock, git revision plus dirty patch/hash, seeds, exact venue/basis and compiler versions.
  Canonical definitions alone cannot reproduce a backtest.
- **Cost:** avoid recomputing full indicator frames, horizon outcomes and event DataFrames
  for every candidate/grid point. Bound features, candidates, retained detail and memory;
  share immutable arrays; use one DuckDB writer. Benchmark before adding process workers.
- **Compatibility:** maintain callable/stateful strategies and separate spot/perp engines.
  Schema v1's conjunctions intentionally cannot express every legacy setup. Any parity
  failure should produce an explicit new version, not a silent replacement.
- **Policy choices:** baseline conditioning, OI units/sampling/staleness, simultaneous
  conflicting directions, allowed timeframe/venue combinations and exact paper/live gates
  need operator-owned definitions. This task chooses no new return or significance hurdle.

## 6. Step 1 implemented

`research/lab/spec.py` adds immutable Pydantic models using Prism's existing dependency
and `Timeframe` enum: `FeatureRef`, `Condition`, `ExitIntent`, `StrategyDefinition`, and
`Hypothesis`, plus `load_hypothesis(Path)`. No existing module imports the Lab.

The minimal vocabulary is grounded in existing functions:

| Reference | Defined meaning for a future compiler |
|---|---|
| `close` | Native closed bar's close; declared market/source and price basis from the plan |
| `sma_20`, `sma_50`, `sma_200` | `technical.sma`, periods in native bars, full warmup |
| `ema_21` | `technical.ema`, recursive `adjust=False` semantics and warmup |
| `rsi_14` | `technical.rsi`, existing Wilder seeding |
| `rel_volume` | Volume divided by full 20-native-bar volume mean |
| `roc_1m` | Existing daily class-based month ROC, 30 crypto / 21 NYSE bars; daily only |
| `funding_day` | Existing daily funding aggregation semantics; perp/daily only, subject to the audit above |
| Exit `wilder_14` / `perp_sma_14` | Explicit distinction between `technical.atr` and perp `_atr`; sampled on trigger bars |

One definition is one market/side, a trigger timeframe, one to four conjunctive
comparisons (`gt/ge/lt/le`) against a finite number or another feature, a bounded cooldown
and exit intent. Dependencies must be at least as slow as the trigger. Spot shorts and
spot funding are rejected. Future evaluation will apply existing `edge_trigger` semantics
to the valid conjunction (first rising edge, then silence for the cooldown); unsupported
data/timeframes must fail at compilation, not fall back. This module does not evaluate
that contract yet.

Stop distance is positive and at most 10 ATR; optional target at most 20 R; holding/cooldown
counts are bounded at 10,000 native bars. These are representation limits, not optimized
values or research pass thresholds. Candidate definitions cannot specify fills, sizing,
costs, evaluation thresholds, test periods or results. Unknown fields are rejected at every
level; numeric strings, booleans in numeric parameters and nonfinite thresholds are rejected.
YAML uses a safe loader and rejects duplicate/merge keys. The JSON schema is available
through `Hypothesis.model_json_schema()` for a later constrained proposal client.

Full SHA-256 identity hashes the canonical behaviour, including schema version, resolved
defaults, feature references, thresholds and exits. Condition order and numeric spelling
do not change it; name, explanation, source, authored timestamp and ancestry are metadata.
Changing a rule/timeframe/side/exit produces a different ID. The timestamp must be aware;
parent IDs are validated structurally and cannot point to the same definition. Existence
and family/near-duplicate checks remain ledger responsibilities. This is **not** the
permanent ledger, and no proposal has been screened or promoted by this change.

Example: `config/lab/examples/daily_trend.yaml` is outside `config/experiments/`, so
`market research` will not automatically discover or run it. Load it locally with:

```python
from pathlib import Path
from market_signal.research.lab.spec import load_hypothesis

h = load_hypothesis(Path("config/lab/examples/daily_trend.yaml"))
print(h.strategy_id)
print(h.definition.canonical_json())
```

Not implemented in Step 1: feature evaluation, signals, screen runner, storage migrations, new CLI
commands, AI integration, or paper/live promotion. This is a small foundation for the
ledger/compiler phases, not a second backtesting engine.

## 7. Step 2 implemented: research governance

Modules: `research/lab/common.py` (canonical JSON, strict types), `policy.py` (evaluation
plans), `datasets.py` (snapshots/fingerprints), `provenance.py` (software identity),
`ledger.py` (append-only API), migration 7 in `data/store.py`, read-only `cli/lab_cmds.py`.
No existing module imports the Lab, and nothing here evaluates a strategy.

### Workflow

```python
ledger = Ledger(store)                                   # requires migration 7
receipt = ledger.submit(raw_json, family_id="trend_family", origin="manual")
plan_id = ledger.register_plan(plan)                     # EvaluationPlan, frozen name+version
dataset_id = ledger.register_dataset(capture_dataset(store, selections))
software = capture_software(Path("."))
exp = ledger.preregister(receipt.submission_id, plan_id, dataset_id, role="discovery",
                         assets=("BTC",), software=software, origin="manual",
                         batch_id="trend_batch_1")       # committed before evaluation
ledger.start(exp.experiment_id, software=software)       # records data exposure
# ... evaluation happens elsewhere (not implemented in the Lab yet) ...
ledger.record_result(exp.experiment_id, status="rejected", verdict="NO_EDGE",
                     metrics={...}, p_values=(PValue(...),))
```

Every mutation runs in its own transaction and commits before returning; do not wrap Lab
calls in an outer transaction (DuckDB will refuse the nested `BEGIN`).

### Identities and rerun semantics

| ID | Derivation |
|---|---|
| `strategy_…` | SHA-256 of canonical rule behaviour (Step 1) |
| `hypothesis_…` | SHA-256 of the authored document (name, text, source, authored time, sorted parents, canonical definition) |
| `plan_…` | SHA-256 of the canonical plan; list order and timestamp offsets do not change it |
| `dataset_…` | SHA-256 of the manifest (selections, per-series hashes, row counts, schema, observed bounds, ingestion run IDs) |
| `software_…` | SHA-256 of Python version, git commit, working-tree `src/**/*.py` + `pyproject.toml` hash, `uv.lock` hash, key package versions |
| `logical_…` | SHA-256 of strategy ID, plan ID, dataset ID, role, sorted assets, timeframes and stage |
| `experiment_…`, `submission_…`, `result_…`, `inspection_…` | Random UUIDs: each is a distinct immutable event |

A **logical experiment** is "this strategy, under this plan, on this exact dataset/role/
universe". An **attempt** (`experiment_id`, numbered `attempt` 1, 2, …) is one concrete
preregistered execution. Preregistering an existing logical experiment raises
`DuplicateExperiment` naming the prior attempt unless `rerun_of` (an attempt of the same
logical experiment) and `rerun_reason` are both given. Renaming a hypothesis, changing the
batch, or editing code does not escape this check: the hypothesis ID and software ID are
recorded on the attempt but are deliberately not part of the logical identity, so a rerun
under new code is an explicit, linked reproduction rather than a fresh discovery. `start`
refuses software that differs from the attempt's preregistration. Changing the data
(including a provider revision or re-ingestion), plan version, role, universe or rule is a
new logical experiment.

### Evaluation plans

`EvaluationPlan` is a narrow, versioned contract for a DAILY event study only: exact stored
source (no provider fallback), up to four non-overlapping dataset-role periods
(`discovery`, `development`, `validation`, `final_holdout`), per-asset fee/slippage, named
horizons and a primary horizon, a market-matched return model, next-open entry, the
current Prism funding approximation (pinned with its known full-history cadence
limitation), the existing independent-event/random-entry statistics, and
`multiple_testing="uncorrected"`, `promotion="disabled"`. Unknown fields at any level and
values other than these literals are rejected; walk-forward, sensitivity, portfolio,
thresholds and BH are not accepted. A `name`+`version` is frozen on first registration;
different content needs a new version. Plans hold no strategy rules and strategy documents
reject evaluation fields.

### Dataset snapshots and fingerprint strength

`capture_dataset` reads explicit `SeriesSelection`s (kind, symbol, exact source,
timeframe, `[start, end)`) in one transaction. The selected rows, **including provenance
columns** (`ingest_run_id`, `ingested_at`, funding `available_at`/`pit_method`), are
serialised canonically (floats losslessly as hex, NULL distinct from NaN), hashed with
SHA-256, gzip-compressed and stored content-addressed in `lab_snapshot_blobs`. Retained
rows can be decoded with `Ledger.read_dataset` after the market tables change; decoding
verifies hash, header and row count. Snapshots are bounded (250k rows / 64 MiB by
default) and fail rather than truncate. Empty selections are recorded as strong empty
snapshots.

Strengths are explicit: `content_sha256` (rows retained and hashed), `metadata_only` and
`unavailable` (must carry a `limitation` and cannot claim a hash). Weak manifests can be
catalogued but cannot be preregistered under the v1 plan. A hash describes the stored
snapshot only, not provider correctness, completeness or point-in-time validity.
Preregistration requires the dataset to contain exactly bars + actions (spot) or bars +
funding (perp) for each selected asset, at the plan's source/timeframe and the exact
window of the chosen role, and every selected asset to have a frozen cost assumption.

### Schema (migration 7, additive)

| Table | Contents |
|---|---|
| `lab_strategies` | Canonical definition, fixed `family_id` and sorted parent IDs (parents must exist in the same family) |
| `lab_hypotheses` | Authored document per hypothesis ID → strategy |
| `lab_submissions` | Every raw submission with receipt time/origin/family; either a hypothesis ID or the validation/lineage error |
| `lab_plans` | Canonical plan JSON; `UNIQUE(name, version)` |
| `lab_datasets`, `lab_dataset_blobs`, `lab_snapshot_blobs` | Manifests, manifest→snapshot links, compressed snapshots by SHA-256 |
| `lab_software` | Software identity payloads |
| `lab_experiments` | Attempts: logical ID, attempt number, strategy/hypothesis/submission/plan/dataset/software IDs, family, batch, role and period, stage, assets, timeframes, origin, time, `rerun_of`/`rerun_reason` |
| `lab_starts` | At most one start per attempt |
| `lab_results` | At most one terminal result per **started** attempt (FK to `lab_starts`): `succeeded`, `rejected`, `insufficient_data`, `failed`, `errored`, `cancelled`; verdict, metrics, uncorrected p-values by test/endpoint, error |
| `lab_inspections` | Exposure log: automatic `evaluation_started` and manual `manual_inspection` records |

Primary/unique/foreign keys and CHECK constraints enforce existence, one start and one
result per attempt even for direct SQL inserts. The API offers no update or delete;
failures, errors and rejections are retained exactly like successes, and status is a
projection (`preregistered` → `started` → terminal). Append-only is an application
guarantee, not tamper-proofing against the database owner. Read-only `Store`s do not
migrate; the Lab reports "tables are absent" until the DB has been opened writable once.

### Holdouts and multiple testing (recorded, not enforced)

`Ledger.exposures(start, end, family_id=…, strategy_id=…)` returns every recorded start or
manual inspection whose role window overlaps an interval, across dataset revisions. An
empty answer means no *recorded* exposure, never proof that a holdout is untouched (direct
market-table access is not observable). Family, lineage, batch, role and per-endpoint raw
p-values are stored so that a later phase can define testing families and apply BH; no
correction or holdout enforcement is implemented.

### CLI (read-only)

`market lab strategies | experiments | experiment <id> | plan <id> | dataset <id>` print
canonical JSON from a read-only store. `dataset` shows the manifest, never the rows. There
are no write, evaluation or promotion commands; registration is programmatic.

### Known limitations

- No evaluator: results are attached by whatever runs the evaluation; the Lab does not
  verify that metrics came from the registered inputs.
- Only DAILY event-study plans; full research, walk-forward, sensitivity and portfolio
  policies are rejected rather than represented.
- Software identity hashes `src/` Python, `pyproject.toml` and `uv.lock`; it does not
  archive source, cover `config/`, or prove the environment matches the lockfile.
- Spot corporate actions are selected by effective date, not publication availability.
- Snapshot size limits mean wide universes/long histories must be split or the limit raised.
- Audit items listed in section 2 (perp warmup, funding cadence, portfolio ordering,
  incomplete-check verdicts, name-based evidence matching, multiple testing) are unchanged.

## 8. Step 3 implemented: daily feature compiler

Modules: `research/lab/vocabulary.py` (names and parameter schema, no pandas),
`features.py` (pure calculations), `compiler.py` (snapshot → features → masks), read-only
`Ledger.get_strategy` and `market lab compile`. Phase 3 computes signals only; it makes **no
claim of profitability** and runs no statistics, screening, ranking or simulation.

### Feature vocabulary (`lab_features_v1`)

A feature reference is one canonical token, `family_p1[_p2]`: parameters are integers in a
fixed order, inside the name. This keeps the Phase 1 `FeatureRef {name, timeframe}` shape,
so every previously registered strategy ID is unchanged (the pinned ID test still passes),
and gives each logical feature exactly one spelling: no leading zeros, exact parameter
count, bounded values, and an implicit default cannot be spelled out (`rel_volume`, never
`rel_volume_20`). The compiler parses tokens into `FeatureKey(family, params)`; the
canonical token is the identity used in metadata and digests. The nine Phase 1 names keep
their meaning. All windows are in native (daily) bars, inclusive of bar T unless stated.

| Family | Parameters | Value at bar T | Warmup (bars) | Market | Implementation |
|---|---|---|---|---|---|
| `open`, `high`, `low`, `close`, `volume` | — | the closed bar's value | 1 | general | snapshot column |
| `ret_N` | N 1–1000 | close[T] / close[T−N] − 1 | N+1 | general | `technical.roc` |
| `roc_1m` | — (legacy) | `ret` over 30 (crypto) / 21 (NYSE) bars | 31 / 22 | general, daily | `technical.roc` |
| `range_pct` | — | (high − low) / close of bar T | 1 | general | — |
| `sma_N` | N 1–1000 | mean close of T−N+1..T | N | general | `technical.sma` |
| `ema_N` | N 2–1000 | recursive EMA, `adjust=False`, seeded at the first bar | N | general | `technical.ema` |
| `dist_sma_N`, `dist_ema_N` | N | close / MA − 1 | N | general | as above |
| `rsi_N` | N 2–1000 | Wilder RSI | N+1 | general | `technical.rsi` |
| `atr_N` | N 1–1000 | Wilder ATR, seeded from bar 1 | N+1 | general | `technical.atr` |
| `atr_sma_N` | N 1–1000 | simple-mean ATR incl. bar 0's high − low | N | general | `perps.strategies.sma_atr` |
| `atr_pct_N` | N | `atr_N` / close | N+1 | general | `technical.atr` |
| `rvol_N` | N 2–1000 | sample std of the last N log returns, **not annualised** | N+1 | general | `technical.realised_vol(bars_per_year=1)` |
| `donchian_high_N`, `donchian_low_N` | N 1–1000 | max high / min low of **T−N..T−1** | N+1 | general | as in `breakout_ls` |
| `close_high_N`, `close_low_N` | N 1–1000 | max / min close of **T−N..T−1** | N+1 | general | as in `trend_ls` |
| `dist_donchian_high_N`, `dist_donchian_low_N` | N | close / level − 1 | N+1 | general | — |
| `vol_sma_N` | N 1–1000 | mean volume of T−N+1..T | N | general | `technical.sma` |
| `rel_volume` / `rel_volume_N` | N 1–1000, bare = 20 | volume / `vol_sma_N` (window includes T) | N | general | as `technical` `rel_volume` |
| `vol_z_N` | N 2–1000 | (volume − mean) / sample std of **T−N..T−1**; undefined if std is 0 | N+1 | general | — |
| `funding_day` | — | settled funding in (open, open + 1 day], see below | 1 | perp, daily | `perps.backtest.daily_funding` |
| `funding_sum_N`, `funding_mean_N` | N 1–1000 | sum / mean of the last N `funding_day` | N | perp, daily | rolling, full window |
| `funding_pct_A_L` | A 1–365, L 2–2000 | percentile rank (average ties, in (0, 1]) of `funding_mean_A` among its last L values incl. T; full L required | A+L−1 | perp, daily | `perps.strategies.funding_percentile(min_history=L)` |

Deliberately absent: log returns (a monotone transform of `ret_N`, so it adds no
expressible rule), OI (only 18 same-day snapshots exist; no historical series is in any
snapshot), and any annualised measure (annualisation depends on asset class). Comparison
operators are `gt`, `ge`, `lt`, `le`, `crosses_above`, `crosses_below`. Equality is not
supported: exact float equality of continuous features is not a robust rule, and no
existing Prism condition uses it. Percentile and range ideas are features, not operators.
`vocabulary.catalog()` returns the vocabulary as data for a later proposal client.

### Compiler semantics

`compile_strategy(definition, snapshot, symbol, *, asset_class=None)` returns
`CompiledStrategy`; `compile_registered(ledger, strategy_id, dataset_id, symbols=…)` loads
both from the ledger. Every frame is indexed by bar `close_time` (UTC), the decision time.

1. **Inputs.** Only the retained snapshot rows (`Ledger.read_dataset`, which verifies
   hashes) are read; no feature function receives a Store. The definition, trigger and
   every reference must be `1d`, the bar series must be daily, open and close times must be
   strictly increasing and every bar must close after it opens. Missing series (bars,
   funding when a funding feature is used, corporate actions for spot), a foreign venue
   for funding/actions, several series for one symbol, a non-crypto perp or a spot run
   without an explicit asset class raise `CompileError`. Nothing falls back or is filled.
2. **Features.** Required features are the condition references plus the exit-intent ATR
   (`wilder_14` → `atr_14`, `perp_sma_14` → `atr_sma_14`). Each is computed once.
   `feature_valid` = finite value.
3. **Conditions.** A comparison at T is *defined* when both sides are finite at T. A
   crossover is defined when both sides are finite at T **and** T−1 (the previous native
   bar); `crosses_above` is left > right at T and left ≤ right at T−1 (mirrored for
   `crosses_below`). Only T and T−1 are read. `conditions` holds the truth, forced False
   where undefined, and `condition_defined` preserves the distinction.
4. **Warmup and eligibility.** `warmup_bars` = max feature warmup (+1 if any crossover).
   `eligible[T]` = T ≥ `warmup_bars` − 1 **and** every required feature finite at T **and**
   every condition defined at T. NaN or insufficient history therefore makes a bar
   ineligible, never a false observation. Gaps in the middle of a series (e.g. a funding
   day below coverage) are ineligible bars, and rolling windows containing them stay
   undefined until a full window of valid values exists again.
5. **Active state.** `active` = eligible and every condition true.
6. **Signal.** `signal[T]` fires when `active[T]`, bar T−1 was eligible and inactive, and
   more than `cooldown_bars` bars have passed since the last signal. This is
   `setups.base.edge_trigger` semantics (the Phase 1 contract), with one stricter rule: if
   T−1 was ineligible the previous state is unknown, so a condition already true when data
   becomes valid does not fire (including bar 0). With every bar eligible and bar 0 inactive
   the two are identical (tested).
7. **Stop.** `stop` = close ∓ `stop_atr` × exit ATR at T (below the close for longs), the
   level a later engine would use if it acted on that bar's signal, in the frame's basis.

**Signal versus entry.** A signal is a decision at bar T's close. The compiler does not
shift it; existing Prism research enters at the next bar's open (`EvaluationPlan.entry =
"next_bar_open"`), and that shift belongs to the screening/evaluation stage. No fills,
costs, exits or forward returns are computed or reachable here.

### Point-in-time rules

- Every value at T depends only on bars ≤ T. Breakout levels (`donchian_*`, `close_high/
  low`) and the `vol_z` baseline use T−N..T−1, so bar T never sets its own threshold.
  Rolling means, sums, std and percentiles are trailing windows with a full-window minimum.
- **Funding.** `funding_day` reuses `daily_funding` (sum of settlements in (open,
  open + 1 day], minute-snapped timestamps, pro-rata scaling at ≥ 80% coverage, otherwise
  missing) with two Lab differences: the expected settlements per day come from the
  median spacing of settlements in the **trailing 7 days** ending at the bar's end rather
  than the whole history (Prism's estimate lets later observations change earlier coverage;
  a test demonstrates this), and a bar is missing if any summed settlement's minute-snapped
  `available_at` is after the bar's close. On a constant cadence the result is identical to
  `daily_funding` (tested for hourly and 8-hourly funding). NULL rates are not settlements.
- **Spot prices** use **forward** split adjustment: `data.prices.adjustment_factors` /
  `apply_basis` normalised to the first snapshot bar's share terms. Prism's backward
  adjustment rescales bar T by splits after T; the two series differ by one constant, so
  all ratio features (returns, distances, RSI, percentages, relative volume) are identical
  to Prism's, but levels (`close`, MAs, ATR, Donchian, stops) are in first-bar share terms
  and a constant price threshold refers to those units. Dividends are ignored for signals,
  as in Prism; corporate actions are still selected by effective date (Phase 2 limitation).

### Reproducibility and result metadata

Results stay in memory: they are a pure function of (strategy ID, dataset ID, symbol,
asset class, compiler version). `CompileMetadata` records compiler and vocabulary
versions, strategy/dataset IDs, symbol, market, side, source, asset class, price basis,
features with warmups, canonical condition labels, cooldown, exit ATR, warmup bars, row
count, first/last close, largest bar spacing, eligible/active/signal counts, and a SHA-256
`digest` of the index, features and all masks, so a later stage can verify a bit-identical
recomputation. Nothing is persisted and no ledger row is written.

`market lab compile <strategy-id> <dataset-id> [--symbol BTC] [--asset-class equity]`
prints that metadata (never rows) from a read-only store.

### Reuse and refactors

- `technical.sma/ema/rsi/atr/roc/realised_vol` are called directly; parity tests show the
  Lab values are bit-identical to `compute_features` columns (rvol up to the annualisation
  factor, tolerance 1e-12 relative).
- `perps/strategies.py`: private `_atr` renamed to public `sma_atr` (pure rename; both
  internal call sites updated). `funding_percentile` reused as-is.
- `perps/backtest.py`: `daily_funding` gained an optional `per_day` array. Omitted, the
  existing full-history median is used exactly as before, so existing research is unchanged.
- `data/prices.py` adjustment functions reused for spot; `setups.base.edge_trigger`
  semantics reproduced with the missing-state rule above (it has no eligibility input).
- Known definition differences, named rather than merged: Wilder `atr_N` vs perp
  `atr_sma_N`; `rvol_N` unannualised; `rel_volume` includes bar T while `vol_z` excludes
  it; funding cadence as above.

### Spec changes (additive)

`FeatureRef.name` is now validated against the vocabulary instead of a nine-value literal;
`daily_only` and perp-only checks come from the vocabulary; `Condition.op` gained the two
crossover operators; a condition cannot compare a feature with itself. Schema version and
document shape are unchanged, and all previously valid documents keep their IDs.

### Known limitations

- DAILY only. No intraday, multi-timeframe or as-of alignment; MTF documents are still
  accepted by the spec and rejected by the compiler.
- No OI, basis, liquidation or order-flow features: no historical series exists in any
  snapshot. No cross-asset features (relative strength, regime, benchmark trend).
- Windows count native bars; calendar gaps are not filled (NYSE weekends are normal; the
  largest spacing is reported).
- Spot needs an explicit asset class (not part of a dataset); levels are in first-bar
  share terms. EMA/Wilder values depend on the first bar of the snapshot, so snapshots
  starting on different dates give slightly different recursive values (warmup reduces,
  but does not eliminate, this; exactly as in Prism).
- Funding: the 7-day cadence window is a fixed Lab choice; a venue cadence change is
  treated as missing until a week of the new cadence exists. (The plan-wording mismatch
  noted here at Phase 3 is resolved by plan schema v2, section 9.)
- No plan-period masking in the compiler: outputs cover the whole snapshot; the screen
  restricts signals to the role window (section 9).

## 9. Step 4 implemented: fast screen

Module `research/lab/screen.py`, plan schema v2 (`ScreenPlan`) in `policy.py`, small
ledger extensions, and `market lab preregister` / `market lab screen`. The fast screen is
a **cheap, deterministic rejection layer**. It answers "how often does this fire, what
happens next versus random eligible entry on the same asset and side, and does that hold
across assets and horizons?". It does **not** establish production readiness, sizing,
leverage, a portfolio, statistical validation after multiple-testing correction, or
eligibility for paper/live trading. **An `INTERESTING` result is only a candidate for
deeper research in Prism's full pipeline**; nothing is promoted automatically.

### Funding policy fix and plan schema v2

The v1 `EvaluationPlan` froze `prism_daily_full_history_median_v1`, which the Lab compiler
deliberately does not implement (it lets later observations change earlier funding). v1
is kept byte-for-byte: existing v1 plan IDs, ledger rows and payloads parse and mean what
they meant, but a v1 plan **cannot be screened** (`run_screen` refuses before recording
a start). Schema v2 (`ScreenPlan`, `schema_version: "2"`, stage `screen`) freezes:

- `funding: CausalFundingPolicy` = `lab_causal_trailing_7d_median_v1`, nearest-minute
  settlement alignment, `available_at_minute_by_bar_close`, coverage pinned to 0.8 (the
  compiler constant; changing it is a new policy version), pro-rata partial days, and
  `missing_window: exclude`;
- `warmup_days`, `outcome_boundary: exit_within_role_period`, triage `gates`, and an
  `asset_class` per asset (required for spot).

`Ledger.get_plan` dispatches on `schema_version` and checks the stored payload still
hashes to its plan ID. Non-Lab Prism funding (`daily_funding` default path, perp research,
paper checks) is unchanged. Compatibility consequence: any perp event study preregistered
under a v1 plan stays a valid record of that methodology but must be re-registered under a
v2 plan to be screened; the two are different logical experiments (different plan IDs).

### Warmup and window boundaries

A v2 dataset for role R must select `[R.start − warmup_days, R.end)` exactly (bars by
close time, funding by `available_at`); preregistration rejects anything else. Hence:

- Data before `R.start − warmup_days` is not in the dataset and cannot influence anything.
- Data in the warmup region feeds features, including recursive ones (EMA, Wilder RSI/ATR)
  whose in-window values depend on the warmup data, and the edge/cooldown state (a
  warmup signal can suppress an early in-window signal within `cooldown_bars`). It never
  produces a scored event or a baseline observation.
- Signals, eligible baseline bars and events are restricted to close times in
  `[R.start, R.end)`. The dataset ends at `R.end`, so an outcome whose exit bar falls after
  the window is not evaluable. No observation after the window can affect a result.
- The required warmup is the compiled `warmup_bars`. Each asset records `prewindow_bars`,
  `warmup_bars`, `warmup_shortfall_bars`, `first_eligible_close` and eligible counts. A
  shortfall (e.g. a coin listed during the window) is not padded with other data: those
  in-window bars are simply ineligible, and the shortfall is visible in the result.

### Screen semantics

| Item | Convention |
|---|---|
| Signal | Phase 3 `signal` at bar T's close, in-window only; one side per definition |
| Entry | Bar **T+1 open**. Never the signal bar. No T+1 bar ⇒ not evaluable |
| Exit | Bar **T+h close** for each plan horizon (≤ 8 horizons, ≤ 250 bars); missing ⇒ not evaluable |
| Perp return | `perps.backtest.side_forward_returns`: side × (exit/entry − 1) − 2 × (fee + slippage) − side × Σ_{T+1..T+h} funding_day × close / entry; any missing funding day ⇒ not evaluable (never zero) |
| Spot return | `backtest.events.forward_returns` on the total-return basis: exit × (1 − c) / (entry × (1 + c)) − 1, c = fee + slippage. Ratios within one window cannot depend on the adjustment constant |
| Gross | The same price move without costs or funding, for the same events |
| Costs | Per-asset `fee_bps` and `slippage_bps` from the plan; nothing invented |
| Eligibility | Compiled eligibility ∧ in-window; perps also require funding known at T (existing `perp_asset_events` convention) |
| Baseline | Mean forward return of all eligible in-window bars of the same asset and side with an evaluable outcome (`run_event_study`) |
| Independent events | `backtest.events.decluster`: keep the first, then only events ≥ h bars after the last kept one, per asset and horizon. All metrics use independent events; raw and evaluable counts are reported |
| Null | Existing `random_entry_pvalue`: draws without replacement from each asset's centred eligible pool, matching per-asset counts; one-sided; `random_entry_samples` draws, `seed` recorded; primary horizon only; uncorrected |

Existing Prism research semantics are reused, not changed. The known audit caveats apply:
random draws do not enforce horizon spacing within a draw, and assets are not independent.

### Metrics

Per asset × horizon: eligible bars, raw signals, evaluable events, independent events,
baseline n/mean, gross mean/median, net mean/median, hit rate, net std, a descriptive
`net_mean_over_std` (per event, not an annualised Sharpe), excess mean/median, worst and
best net. Aggregate per horizon: the same pooled statistics plus excess t, assets with
events, assets with positive/negative excess, positive-asset share, median/min/max/std of
asset-level excess, the largest single-asset share of events, and (primary horizon) the
random-entry p-value. Per-asset rows are always kept beside the aggregate, so a strategy
that is +3/+2/+1% on three coins and −12% on a fourth shows a 0.75 positive share and a
wide asset dispersion, unlike one that is mildly positive everywhere.

### Triage statuses (primary horizon, gates frozen in the plan)

| Status | Rule | Ledger status |
|---|---|---|
| `NO_EVENTS` | 0 independent evaluable events | `insufficient_data` |
| `INSUFFICIENT_EVENTS` | fewer than `statistics.min_independent_events` (default 30), or fewer than `gates.min_assets_with_events` (default 2) assets with events | `insufficient_data` |
| `INTERESTING` | pooled independent net excess > `gates.min_pooled_excess` (default 0) **and** positive-asset share ≥ `gates.min_positive_asset_share` (default 0.6) **and** random-entry p ≤ `gates.max_p_value` (default 0.10, uncorrected; `null` disables) | `succeeded` |
| `WEAK` | otherwise | `rejected` |
| `ERROR` | any exception after the start | `errored`, with exception type, message and short traceback |

The per-gate booleans are stored in `metrics.gate_checks`. No status claims profitability,
validation, approval or live readiness. Thresholds come only from the plan; strategy
documents cannot carry them.

### Governed lifecycle

```text
submit → register_plan (v2) → register_dataset (with warmup region)
       → preregister (stage "screen")       market lab preregister …
       → run_screen: start → compile → screen → record_result   market lab screen <experiment-id>
       → inspect                             market lab experiment <experiment-id>
```

`run_screen` refuses (before any start) a non-screen attempt, a v1 plan, different
software, or an already-started attempt. After the start every outcome, including
exceptions, becomes exactly one immutable terminal result. Reruns need `rerun_of` +
`rerun_reason` (Phase 2 rule) and reproduce the same metrics from the retained snapshot
even after the live tables change. The result's `metrics.provenance` holds the screen,
compiler and vocabulary versions, strategy/dataset/plan/experiment/logical/software IDs,
attempt, role, window, data start and warmup days, side, horizons, entry/exit/outcome
conventions, return model, costs of the selected assets, funding policy, baseline, the
statistics policy (seed, draws), gates and the independence rule; per-asset compile
digests sit under `metrics.assets`. Snapshots are not duplicated. `screen(...)` is a pure
developer function for tests; it writes nothing and is not a Lab result.

### Batches and performance

`ScreenWorkspace` (one per dataset) shares decoded rows, compiled inputs, every computed
feature (by canonical name) and forward returns between strategies; `run_screen(...,
workspaces=…)` reuses it. The ledger verifies each snapshot blob once per `Ledger`
instance (content-addressed, never rewritten) instead of on every preregistration and
start. Measured on a copy of the local DB, all six Hyperliquid perps (AAVE, BTC, ETH, HYPE,
LINK, SOL), 400-day warmup + two-year window, 140,024 retained rows, 5 horizons, 2,000
random-entry draws:

| Step | Time |
|---|---|
| Capture + register dataset | 3.5–4.5 s (once) |
| Verify + decode snapshot | 0.6 s (once per Ledger) |
| Compile without cache | 45 ms per strategy × asset (funding aggregation dominates) |
| Pure screen, fresh workspace | 0.54 s per strategy |
| Pure screen, shared workspace | 0.25 s per strategy (mostly the random-entry null) |
| Governed batch, shared workspace, incl. ledger writes | 0.29 s per strategy (was 1.44 s before verification reuse) |

Peak RSS 365 MB for 26 strategies. Hundreds of strategies on one dataset are minutes, not
hours. The random-entry loop is the next cost to vectorise if needed; workspaces grow
with distinct features (one float series per feature per asset), which is small at
daily resolution.

### Known limitations

- DAILY only; no intraday, multi-timeframe, OI, basis or order-flow inputs.
- No AI generation, bulk/batch CLI, comparison reports, holdout enforcement, testing
  families or multiple-testing correction; p-values are recorded uncorrected per endpoint.
- No stops, targets, sizing or portfolio simulation; horizon outcomes only.
- No automatic promotion to full research, paper or the scanner.
- Spot needs per-asset classes in the plan; dividends affect outcomes (total return) but
  not signals.

## 10. Step 5 implemented: preregistered search batches and FDR

Module `research/lab/batch.py`, additive migration 8, and the CLI commands
`market lab batch create <manifest.yaml>`, `market lab batches`,
`market lab batch run <id>` and `market lab batch show <id>`.

### Why

Screening 100 strategies on the same data at an uncorrected p ≤ 0.10 is expected to throw
up about ten "discoveries" even if none of them works. A **search batch** records, before
any member is screened, that *these N hypotheses were searched together*, so their
evidence is judged as one testing family with a frozen correction. On the real-data
benchmark below, two of 59 variants were `INTERESTING` under Phase 4's uncorrected
heuristic (best raw p 0.027). After BH across the family the smallest q was 0.93 and there
were no survivors. That is the failure mode this phase exists to prevent.

### Batch definition and identity

`BatchDefinition` (schema 1) holds the statistical identity: plan ID (must be a v2 screen
plan), dataset ID, role, primary horizon (must equal the plan's preregistered primary
horizon), the member strategy IDs, `correction` (`method: benjamini_hochberg`, `q`,
`family: one_primary_test_per_strategy_v1`, `test: random_entry_mean_excess_v1`,
`eligibility: phase4_sufficient_events_v1`) and `survivor` (`rule:
substantive_gates_and_q_v1`, required `min_pooled_excess`). `batch_id` is the SHA-256 of the
canonical definition with members sorted: member order cannot change it, and changing any
member, the plan, dataset, role, horizon, method, q or survivor rule does. Name,
description and origin are metadata. Each correction/survivor field is a versioned
literal, so Benjamini–Yekutieli or a permutation method would be a new value and never
reinterpret an old batch. Manifests are strict YAML (unknown keys, duplicate keys and
invalid definitions are rejected):

```yaml
name: trend_family_v1
description: Do EMA crossovers beat random timing on majors?
plan: plan_…
dataset: dataset_…
role: discovery            # discovery | development only
primary_horizon: 10d       # must equal the plan's primary horizon
correction: {method: benjamini_hochberg, q: 0.10}
survivor: {min_pooled_excess: 0.002}   # economic floor; no default
members: [strategy_…, strategy_…]
```

### Lifecycle: FROZEN → RUNNING → COMPLETED (or FAILED)

- **Freeze** (`freeze_batch`) validates everything and inserts one `lab_batches` row. There
  is no draft in the database (the draft is the YAML file) and no API to edit a batch, so
  membership, plan, dataset, role, horizon and correction are fixed from this point. A
  different family is a new batch with a new, permanent name. Freezing refuses:
  - an identical definition that is already frozen;
  - members that overlap another batch on the same plan/dataset/role;
  - any member already preregistered on these inputs: its outcome may be known, which is
    exactly the cherry-picking route (test, inspect, drop the losers, "re-correct").
- **Run** (`run_batch`) creates a `lab_batch_runs` row (attempt, software), then
  preregisters **every** member as a Phase 2 attempt tagged with the batch ID
  (`lab_batch_members` links them) before any member is screened. It then calls Phase 4
  `run_screen` for each member with one shared `ScreenWorkspace`; there is no second
  screening implementation. Only when every member has a terminal result does it compute
  and insert one `lab_batch_analyses` row.
- If preregistration fails, the run is closed with a `failed` analysis that states nothing
  was screened.
- If the process dies mid-run, the batch shows `RUNNING`. Running it again with the same
  software resumes: unstarted members are screened, and started members without a result
  get an `errored` "interrupted" result.
- A completed batch cannot be run again without `rerun_of` (an earlier run) and a reason.
  A rerun is a new run whose member attempts are explicit Phase 2 reruns (new software is
  allowed and recorded). Earlier runs, screens and analyses are never modified.

### The test family and BH

- **One test per strategy.** Each member contributes exactly its Phase 4 primary test: the
  `random_entry_mean_excess_v1` p-value at endpoint `pooled:<side>:<primary horizon>`,
  read from the stored, immutable screen result. Assets, other horizons, metrics and
  directions are not separate tests (each definition has one side).
- **Is that p-value coherent?** Yes, as a strategy-level one-sided test. Observed: the mean
  excess (versus the same-asset/side eligible baseline) of the strategy's independent
  events, pooled over its assets. Null: the same per-asset counts drawn without
  replacement from each asset's centred eligible pool. It is a Monte Carlo p-value,
  (hits + 1) / (draws + 1), so its smallest attainable value is 1/(draws + 1). A batch is
  refused at freeze if that floor exceeds BH's first threshold q/m, because no member
  could ever survive (2,000 draws allow at most 200 members at q = 0.10). Its known
  caveats are the Phase 4 ones: random draws are not horizon-spaced, and assets are not
  independent.
- **Who enters.** Members whose Phase 4 triage is `WEAK` or `INTERESTING`, i.e. that passed
  the plan's minimum independent events and minimum assets with events, and have a
  recorded primary p-value. `NO_EVENTS` and `INSUFFICIENT_EVENTS` members are
  `NOT_TESTABLE`, and errored members are `ERROR`. Both are excluded from m but stay
  visible with a reason. This sample-size filter depends only on event counts, not on the
  test statistic, which is the condition under which filtering before BH keeps its
  guarantee (independent filtering). Missing p-values are never replaced by 0 or 1. As a
  diagnostic only, each member also gets `q_if_all_preregistered_tested`: BH with every
  excluded member counted as p = 1. It is never used for statuses.
- **BH.** Sort the family by (p, strategy ID); q₍ᵢ₎ = min over j ≥ i of min(1, p₍ⱼ₎ · m / j).
  Tied p-values get equal q-values; input order never matters. Rejecting q ≤ target is
  exactly the BH step-up procedure. Raw p and q are both stored.

**Meaning of q.** q ≤ 0.10 means the procedure controls the *expected* proportion of false
discoveries among the discoveries at 10%, under its assumptions. It does **not** mean a
strategy has a 90% probability of being real.

**Dependence.** BH is guaranteed under independence and under positive regression
dependence (PRDS). Neighbouring variants (EMA 20/50, 21/50, 20/55) share signals, and
assets and time periods are shared across all members. Positive dependence of this kind
is often benign for BH, but it is not proven here. Treat the FDR level as approximate and
the family's effective size as smaller than m. A dependence-robust method (BY, or a joint
permutation null) would be a new `correction.method` value.

### Family-aware statuses (`substantive_gates_and_q_v1`)

| Status | Rule |
|---|---|
| `FDR_SURVIVOR` | in the family, q ≤ target, all four substantive Phase 4 gates true (independent events, assets with events, pooled excess > plan minimum, positive-asset share), and pooled independent net excess ≥ `survivor.min_pooled_excess` |
| `FDR_SIGNIFICANT_FAILS_SCREEN` | q ≤ target but a substantive gate or the economic floor fails: a tiny or asset-inconsistent effect cannot survive on q alone |
| `NOT_FDR_SIGNIFICANT` | q > target, however large the effect |
| `NOT_TESTABLE` | `NO_EVENTS`, `INSUFFICIENT_EVENTS`, or no primary p-value |
| `ERROR` | the member's screen errored (including interrupted) |

Phase 4's own `INTERESTING`/`WEAK` statuses are unchanged and are reported beside these.
Phase 4's uncorrected `max_p_value` gate is deliberately **not** part of the survivor rule:
corrected evidence replaces it.

**FDR survival is not final strategy validation.** It only qualifies a candidate for
deeper, independent research on data the batch did not touch.

### Holdout protection

- Batch roles are `discovery` or `development` only; `validation` and `final_holdout`
  are rejected by the schema.
- Freezing also refuses any batch whose dataset, **warmup region included**, overlaps a
  validation or final-holdout period in the plan.
- There is no override. Single, explicitly preregistered screens (`market lab screen`)
  remain possible on any role and are logged as exposures by Phase 2; bulk search is not.

### Analysis output (`market lab batch show`)

The analysis payload contains:

- **Counts:** preregistered, testable, correction family, no events, insufficient events,
  errored, Phase 4 interesting, FDR-significant, FDR survivors.
- **Per member:** strategy ID, Phase 2 family and parents, side, experiment and result IDs,
  Phase 4 status/triage, independent events, assets with events, pooled net and excess,
  positive-asset share, gate checks, raw p, q, conservative diagnostic q, inclusion and
  exclusion reason, batch status.
- **Per economic family (Phase 2 `family_id`):** variants, testable, survivors, best raw p,
  best q, and min/median/max excess.

Variants are never collapsed for BH; the family table only describes them. Per-asset
detail stays in each member's screen result rather than being duplicated.

### Performance

On a copy of the local DB: 59 members in 6 economic families, 6 Hyperliquid perps,
400-day warmup plus a two-year window, 4 horizons, 2,000 draws.

| Step | Time / memory |
|---|---|
| Freeze | 0.11 s |
| Full governed run (preregister + start + screen + result for every member, then BH) | 16.6 s, 0.28 s per member |
| Peak RSS | 377 MB |
| Pure screen per strategy | 0.48 s with a fresh workspace, 0.24 s with the shared one |

Outcome: 45 testable (13 insufficient, 1 no events), 0 FDR survivors.

### Known limitations

- Correlated variants (see Dependence). No BY or permutation methods yet.
- One primary test per strategy; no hierarchical or family-of-families control across
  batches. Separate batches on different data are separate families. Batches that share
  members on the same plan, dataset and role are refused at freeze rather than reconciled.
- Daily data only; no OI or multi-timeframe; no AI generation.
- No promotion to full research and no automated confirmatory validation.

## 11. Step 6 implemented: structured strategy families

Module `research/lab/families.py`, the catalogue in `config/lab/families/`
(`<family>.v<version>.yaml`), and the CLI commands `market lab families`,
`market lab family show <name> [--market perp|spot]` and
`market lab batch generate <request.yaml> [--register --out manifest.yaml]`.

### Strategy, family and batch

- A **strategy** is one Phase 1 definition with a canonical `strategy_…` ID.
- A **family** is a versioned economic hypothesis: a rationale, the markets and sides it
  applies to, a small explicit parameter grid with constraints, and one condition
  template per side. It generates strategies and knows nothing about data or plans.
- A **batch** (Phase 5) is a statistical testing family: chosen strategies on one dataset,
  plan, role, horizon and FDR target. The same family can feed many batches.

**Parameter grids are preregistered research spaces, not optimisation searches performed
after observing results.** The catalogue was written before any family was screened, and
the Phase 6 smoke run below changed nothing in it.

### Initial catalogue (v1, 9 families; 128 perp and 48 spot variants)

| Family | Hypothesised mechanism | Parameters (constraint) | Sides | Perp / spot |
|---|---|---|---|---|
| `ma_trend` | Positioning and slow participants adjust gradually, so price and a faster EMA on the same side of a slower EMA may keep drifting | fast {10,20,50}, slow {50,100,200} (slow ≥ 2.5 × fast) | long & short | 14 / 7 |
| `donchian_breakout` | A close beyond the prior range may reflect new information or persistent order imbalance | lookback {10,20,30,55,100}; close crosses the prior high/low | long & short | 10 / 5 |
| `trend_pullback` | Counter-moves inside a trend are often transient profit-taking; momentum turning back may end the pullback | trend SMA {100,200}, RSI {7,14}, level {35,40}; shorts use 100 − level | long & short | 16 / 8 |
| `rsi_exhaustion` | Momentum extremes can reflect forced or crowded flows that partly revert when pressure fades | RSI {7,14}, oversold {20,25,30}; shorts use 100 − oversold | long & short | 12 / 6 |
| `ma_distance_reversion` | Large deviation from the recent average can be temporary liquidity demand or overreaction | SMA {20,50}, stretch {0.10,0.15,0.20} | long & short | 12 / 6 |
| `vol_compression_breakout` | Low realised volatility can be a temporary balance; breaking out of it may start a larger move | fast vol {10,20}, slow vol {60,120} (slow ≥ 3 × fast), breakout lookback {20,40} | long & short | 16 / 8 |
| `volume_breakout` | Abnormal participation may separate committed breakouts from noise | lookback {20,55}, volume window {20,50}, z {2,3} (vol z-score vs the prior window) | long & short | 16 / 8 |
| `funding_extreme_fade` | Extreme funding versus a coin's own history may proxy for crowded leverage that unwinds against the crowd | mean days {3,7}, percentile lookback {90,180}, tail {0.90,0.95}; longs use the 1 − tail lower tail | short (high funding) & long (low funding) | 16 / – |
| `funding_momentum_exhaustion` | Extreme funding after a strong same-direction move may be late, fragile trend-chasing | tail {0.90,0.95}, return horizon {7,14}, move {0.10,0.20}; funding_pct_7_180 fixed | short & long | 16 / – |

Every family file states its rationale as a hypothesis, not a claim. OI families are
absent because no historical OI exists. Relative momentum, failed-breakout reversal and
range-expansion continuation were left out: they need cross-asset or state features the
daily vocabulary does not have, or have no clean expression yet. Spot generation keeps
only long sides (Prism's spot engine is long-only), and the funding families are perp-only.

### Generation semantics

- **Grid.** Parameters are enumerated by name, values ascending, as a Cartesian product
  filtered by the constraints (`left op factor × right`, with `right` a parameter or a
  constant). Then the long side, then the short. There is no sampling. A grid that would
  exceed the family's `max_variants` (≤ 64) makes the family invalid ("narrow the grid in
  a new version").
- **Templates.** Feature names are substituted from integer parameters and must be
  canonical vocabulary tokens. Right-hand sides are structured: a number,
  `{feature: …}`, or `{param: p, scale, offset}`, which lets shorts mirror thresholds
  explicitly (RSI 100 − level, funding 1 − tail, distance −stretch). There are no
  expression strings. Side semantics are explicit per family: compression and volume
  conditions are intentionally *not* mirrored.
- **Exit and cooldown.** Each family fixes its exit intent (the ATR meaning comes from the
  market: `perp_sma_14` or `wilder_14`) and its cooldown.
- **Variants.** Each variant is an ordinary `Hypothesis` + `StrategyDefinition` with the
  usual strategy ID:
  - `created_at` is the family's fixed `authored_at`, so the hypothesis documents are
    reproducible too;
  - the name is deterministic, e.g. `ma_trend_20_100_long` or
    `funding_fade_a7_l180_p95_short` (floats 0.95 → `p95`, 1.5 → `1p5`, minus → `m`);
  - the hypothesis text is the side's sentence with values substituted, followed by the
    family rationale;
  - `source` holds machine-readable lineage
    `{generator: lab_family_grid_v1, family, version, family_id, market, side, params}`.
- **Validity checks.** Duplicate names, two assignments producing the same strategy (an
  unused parameter), placeholders missing from the name, and invalid features or
  definitions all make a family invalid when it is loaded.

### Identity and versioning

`family_id` is the SHA-256 of the canonical family document: parameters by name with
sorted values, sorted constraints and sorted conditions, so YAML ordering never matters.
Any change to values, constraints, templates, exit, cooldown, cap, rationale or version
changes it. A test pins every catalogue family ID and variant count, so a frozen family
cannot be edited silently: write `<family>.v2.yaml` instead. In the ledger, a family's
strategies are registered under `family_id = <family name>` (stable across versions), and
the version lives in each hypothesis's lineage.

### Complexity limits

Templates have 1–4 conditions (the spec limit); the v1 catalogue uses at most 2. Families
have at most 4 parameters with 10 values each, at most 6 constraints and at most 64
variants per market. Each variant reports `conditions`, `unique_features` and
`free_parameters` for later analysis; no complexity penalty is applied yet. Families
combine only features that belong to one mechanism; there is no cross-family combining.

### Dry run and batch generation

`market lab batch generate request.yaml` (read-only) takes a strict request: name,
description, plan, dataset, role (`discovery`/`development`), correction, survivor and a
list of `{family, version}`. It prints:

- each family with its variants, sides, parameters, constraints, and per-variant name,
  strategy ID, parameters, complexity and registration state;
- the total;
- the statistical check: q, draws, smallest attainable p, and the largest family the Monte
  Carlo resolution supports, `floor(q × (draws + 1))` (capped at 1,000, the Phase 5 batch
  limit);
- every problem the Phase 5 freeze would raise: a member already preregistered on these
  inputs, a strategy registered under another family, duplicates across families, or a
  family that is too large.

It reads definitions and the preregistration log, never results.

`--register --out manifest.yaml` refuses if any problem remains. Otherwise it submits only
variants that are not yet registered (regeneration adds no records) and writes the Phase 5
manifest, with the primary horizon taken from the plan and members sorted. It **does not
freeze or run anything**: `market lab batch create` and `batch run` remain separate,
explicit steps. Oversized families are refused rather than truncated.

### Phase 6 smoke run (scratch copy of the local DB; not a search for winners)

- **Request:** `ma_trend` + `donchian_breakout` + `funding_extreme_fade` v1 on all six
  Hyperliquid perps, a two-year discovery window with 400-day warmup, primary horizon
  10d, 2,000 draws, q = 0.10, `min_pooled_excess` 0.002.
- **Dry run:** 40 variants (14/10/16), compatible (maximum 200).
- **Pipeline:** `--register` (40 submissions) → `batch create` → `batch run` (12.7 s) →
  COMPLETED.
- **Result:** 40 preregistered, 35 in the correction family, 5 insufficient events, 0
  errors, 0 FDR-significant, 0 survivors. One `ma_trend` variant was Phase 4 `INTERESTING`
  (raw p 0.068, q 0.83).

The catalogue was not changed after this run.

### Known limitations

- The catalogue is fixed and hand-designed; there is no LLM or automatic hypothesis
  generation.
- Daily data only; no OI, cross-asset or multi-timeframe features.
- Grids only (no paired sets or sampling); one version per family per batch.
- No automatic promotion to full research.

## 12. Step 7 implemented: evidence profiles

Module `research/lab/evidence.py`, additive migration 9 (`lab_evidence_policies`,
`lab_evidence_profiles`), and the CLI commands `market lab evidence build <batch-id>`,
`evidence report <batch-id>` and `evidence show <strategy-id>`.

### Evidence versus action

> Prism separates evidence from action. The Strategy Lab records what historical and
> forward research says about a setup. Separate, versioned consumer policies decide
> whether that evidence is sufficient for human alerts, long-horizon opportunity
> surfacing, or — in the future — automated execution.

> A co-pilot alert threshold may intentionally be lower than an automated-trading
> promotion threshold. This does not weaken the research record; it changes only how an
> application consumer uses that record.

Prism will have three consumers of one research record:

- a perp co-pilot (human decision support);
- a future perp auto-trader;
- a later 1–6 month opportunity radar.

**Statistical research** (Phases 4–5) asks whether an apparent edge survives systematic
testing: raw p, BH q, survivor status, frozen and unchanged. **Human decision support**
needs more than that. It needs to know what the history looks like, how broad and how
robust it is, and how uncertain it is. An evidence profile provides that view, and it
answers neither "should Prism trade this?" nor "should this alert?".

A profile has no field for alerting, trading, sizing or approval. The schema forbids extra
fields, and a test scans every key. Consumer policies are future, separately versioned
layers (documented below, not implemented), and changing one can never change an
evidence identity.

### Stages versus tiers

Each profile cites typed **sources**: `{stage, records, versions}`, where the stage is one
of `fast_screen`, `batch_fdr`, `full_research`, `validation`, `paper_forward` or
`live_forward`. Phase 7 emits only the two stages that exist:

- `fast_screen`: the experiment and result IDs, plan, dataset, software and screen/compiler
  versions;
- `batch_fdr`: the batch, run and analysis IDs and the analysis version.

No placeholder results are invented for later stages. When a later stage exists, it creates
a **new** profile listing the extra source with `extends` set to the earlier profile ID; the
earlier profile is never rewritten.

The **tier** is the human-readable summary, assigned under a versioned `EvidencePolicy`.
`profile_id` = SHA-256 of profile schema, policy ID, strategy ID, the cited sources and
`extends`. These inputs determine every derived field, so the same inputs and policy give
the same profile. A new policy version gives new profiles beside the old ones. Underlying
screen and batch records are only read.

### Tiers (`lab_evidence_policy` v1)

Every rule uses only the plan's **primary horizon** plus descriptive robustness context.

| Tier | Definition |
|---|---|
| `UNAVAILABLE` | The screen errored. No evidence either way, never treated as negative |
| `INSUFFICIENT` | Phase 4 `NO_EVENTS`/`INSUFFICIENT_EVENTS`, or fewer than 30 independent events, or fewer than 3 assets with events |
| `EXPLORATORY` | Adequate sample **and** every check below passes: pooled net excess ≥ 0.002; ≥ 60% of assets agree in the expected direction; no single asset holds > 50% of events **and** the pooled sign survives dropping the largest contributor; raw p ≤ 0.25 (a loose sanity bound, not the defining criterion); and either ≥ 50% of testable adjacent parameter variants agree **or** the effect is strong (≥ 0.01). FDR survival is **not** required |
| `RESEARCH_SUPPORTED` | All EXPLORATORY checks, **plus** a Phase 5 `FDR_SURVIVOR` (which already includes the substantive Phase 4 gates and the batch's economic floor), **plus** neighbourhood support from at least one agreeing testable neighbour (`plateau` or `mixed` with support ≥ 50%; an isolated spike or a strategy without family lineage cannot qualify) |
| `NEGATIVE` | Adequate sample, pooled excess ≤ 0 **and** ≤ 50% of assets agreeing: history does not support the hypothesis |
| `INCONCLUSIVE` | Adequate sample, neither supportive enough for EXPLORATORY nor adverse enough for NEGATIVE (e.g. a positive but tiny or narrow effect) |
| `VALIDATED` | **Reserved.** The model rejects it unless a `validation`-stage source is cited. Nothing in Phase 7 can produce it |

> EXPLORATORY does not mean a strategy has demonstrated persistent alpha. It means the
> historical pattern is sufficiently interesting to merit human inspection.

> RESEARCH_SUPPORTED is not equivalent to production validation.

Instead of one score, each profile carries named categorical **components**:

- sample: adequate / insufficient;
- effect: strong / meaningful / small / adverse;
- breadth: broad / narrow;
- concentration: spread / concentrated;
- neighbourhood: plateau / mixed / isolated / no testable neighbours / unavailable;
- horizon: strengthens / decays / reverses / mixed;
- statistics: fdr_survivor / raw_only / weak.

It also carries explicit `supporting` and `limiting` reasons and standard limitations.
There is no probability that a strategy "works".

### Raw p versus q

Every profile shows the Phase 4 raw p (`random_entry_mean_excess_v1` at the primary
horizon), the BH q, the target, the family size, the preregistered count, the batch status
and Phase 4 triage, exactly as recorded. A raw p below 0.10 with q of 0.83 can be
EXPLORATORY; the limiting reasons then state "not an FDR survivor (q = …)". It can never
be RESEARCH_SUPPORTED, and the q-value is never hidden.

### Parameter neighbourhood and family context

- **Neighbours.** Same family, version, market and side, differing by **one adjacent step
  in one parameter**. Steps use the sorted distinct values each parameter takes among
  that family-side's batch members, so constraint-removed grid points are skipped
  naturally.
- **Neighbourhood metrics.** For each target: neighbour and testable-neighbour counts, how
  many agree in sign, support share, median neighbour excess, the target minus that
  median, the neighbour range, and a label: `plateau` (≥ 2 testable neighbours, ≥ 67%
  agree), `isolated` (< 50% agree; `isolated_spike` when the target itself is positive),
  `mixed`, or `no_testable_neighbours`.
- **Family context** (all variants of that family-side): variant and testable counts,
  positive-effect share, median and standard deviation of excess, the target's rank,
  variants passing the substantive gates, and FDR survivors.

These are robustness descriptions, not significance tests. Plateaus are sign-agnostic
agreement; the batch report splits them into positive and adverse.

### Asset breadth (primary horizon, from Phase 4 per-asset rows)

Assets with events, positive and negative counts, positive share, median asset excess,
best and worst asset, events by asset, the largest single-asset event share, and the
**pooled excess without the largest contributor**. If that flips sign, the profile is
`dominated_by_one_asset`. A profile is `concentrated` if it is dominated or one asset
holds > 50% of events. This distinguishes "appears across several assets" from "pooled
result is mostly one asset".

### Horizon profile

Every plan horizon, ordered by bars, shows excess, net, events and sign. The raw p appears
**only** on the primary horizon; every other row carries `raw_p: null`. Descriptive
measures are the sign consistency with the primary horizon, the shape (strengthens /
decays / reverses / mixed), and the horizon of largest effect, labelled descriptive. A
reversal adds a limiting reason but never changes statistics or the tier. Picking the best
horizon afterwards cannot be passed off as preregistered. Horizons are generic
`label → bars`, so a 30/90/180-day radar plan fits the same schema (tested).

### Batch evidence report (`market lab evidence report`)

Computed on read from stored profiles. Per family-side it shows:

- tier counts;
- positive-effect share and median excess;
- positive and adverse plateaus;
- isolated spikes;
- a descriptive pattern (`broad_directional`, `uniformly_weak_or_adverse`, `mixed`,
  `insufficient`).

It also lists asset-specific and horizon-reversing variants (adequate samples only), and
variants sorted by tier, family and name. It states that it is not a ranking of trades
and never picks a winner.

### Future consumer policies (documented, not implemented)

- **Co-pilot policy:** may surface EXPLORATORY or stronger profiles that meet its own
  usefulness/risk criteria, always showing raw p, q and limitations.
- **Auto-trader promotion policy:** requires materially stronger stages (full research,
  untouched validation, realistic costs, paper/live forward observation, execution
  reliability) and explicit risk approval. RESEARCH_SUPPORTED alone never qualifies;
  EXPLORATORY can never become executable because it triggers an alert.
- **Long-horizon radar policy:** its own families, plans and horizons, consuming the same
  profile abstraction.

Each is versioned independently and reads profiles. None writes to them. The co-pilot
policy is implemented in Phase 10 (section 15); the other two do not exist yet.

### Descriptive run (Phase 6 smoke batch, scratch DB)

All 40 members were profiled. Tiers:

- EXPLORATORY 8: seven `ma_trend` variants (4 long, 3 short) and
  `funding_fade_a3_l180_p9_long`;
- INCONCLUSIVE 12;
- NEGATIVE 15;
- INSUFFICIENT 5;
- RESEARCH_SUPPORTED 0, because there are no FDR survivors. Every EXPLORATORY profile has
  q = 0.83.

By family:

- `ma_trend` long: positive effect on all 7 testable variants and 6 positive plateaus
  (`broad_directional`).
- `ma_trend` short: positive on 57%, 4 positive plateaus.
- `donchian_breakout` short: uniformly adverse (3 adverse plateaus).
- `donchian_breakout` long: mostly adverse, with an isolated positive spike at lookback 55.
- `funding_extreme_fade` short: negative on 6 of 8 variants (7 adverse plateaus), with one
  isolated spike.
- `funding_extreme_fade` long: mixed, one EXPLORATORY variant.

Several adequate variants are flagged asset-concentrated, and many reverse sign across
horizons (often at 1 day). The catalogue was not changed in response.

### Known limitations

- Not connected to live scanning, Telegram or any consumer policy.
- Daily only; no intraday, OI or long-horizon families yet.
- Profiles summarise one batch run's discovery-window evidence; there is no full
  independent validation adapter yet, so VALIDATED is unreachable.
- Tier thresholds are policy defaults chosen before the descriptive run. They are
  judgement calls, so a change creates a new policy version.

### Step 7.1 housekeeping (reproducibility)

No stored profile was modified. New semantics are introduced as new versions, and old
records keep their original meaning and IDs.

- **Builder provenance (profile schema 2).**
  - New profiles carry `builder = {version: lab_evidence_builder_v2, software_id}`.
  - The builder *version* enters the schema-2 profile identity. A future change to how
    profiles are computed must bump it, which yields new profiles rather than an identity
    collision.
  - The `software_id` is provenance only. An identical profile rebuilt by later software
    is the same evidence: the first recording is kept, and no collision is raised.
  - Schema-1 profiles (Phase 7) validate unchanged, forbid a builder field, and keep
    their original identity formula (tested).
- **Evidence policy v2 (the default) adds a horizon dead band.**
  - A non-primary horizon whose |excess| is below max(0.001, 0.25 × |primary excess|) is
    *flat* (sign 0). It neither agrees with nor reverses the primary horizon, and it is
    excluded from sign consistency.
  - A reversal now needs a material opposite-signed horizon. Each profile records the
    band and its threshold.
  - Tier rules are otherwise identical to v1, and tiers never used horizons.
  - v1 is kept in the `POLICIES` registry: it is strict (no dead band, enforced) and has
    exactly its recorded ID, `evpolicy_133e6746…` (pinned by a test), so v1 profiles
    remain reproducible.
- **Report policy.** The read-time report's wording thresholds now come from a versioned
  `ReportPolicy` (v1 = the Phase 7 values: broad directional at ≥ 67% positive with ≥ 2
  adequate variants). Every report cites its report-policy ID and version and the
  evidence-policy IDs it summarises, so an old batch is always described under an
  explicit version.
- **CLI.** `evidence build` and `evidence report` take `--policy-version`; `evidence report`
  also takes `--report-version`. Both default to the latest. `evidence build` records
  the builder's software identity.

**Real data (Phase 6 smoke batch, scratch DB).** v2 profiles were built beside the 40 v1
profiles, giving 80 rows; the 40 v1 rows are unchanged. Horizon shape "reverses" went from
26 to 20, "strengthens" from 2 to 5 and "mixed" from 12 to 15. No tier changed (8
EXPLORATORY, 12 INCONCLUSIVE, 15 NEGATIVE, 5 INSUFFICIENT). The report's horizon-specific
list went from 21 to 15. The remaining reversals are material: for example,
`ma_trend_20_100_long` has 20d excess of −0.45% against a 0.42% band.

### Open-interest collection

OI collection now lives outside the Strategy Lab. See [OPEN_INTEREST.md](OPEN_INTEREST.md)
for storage, the verified Binance API limits, rolling backfill, downtime recovery,
diagnostics and the schedule.

- **Hyperliquid:** OI is captured prospectively in `perp_snapshots` on every perp run, at
  irregular times. A missed capture can never be backfilled.
- **Binance USD-M:** 1h OI is backfilled into `perp_oi_history` from a ~30-day API window.
- **Scheduling:** the Windows Task Scheduler task "Prism OI collect" runs `market oi
  collect`. The 10:00 run noted in the 2026-10-03 audit comes from a separate Windows task,
  "Prism daily". At that audit the local DB held only 18 Hyperliquid snapshot rows, from
  manual runs.

Stored OI is data only. Future OI features still need explicit point-in-time rules:
availability time, maximum staleness, units (coins vs notional, where price alone moves
notional OI) and windows measured in elapsed time. These belong to the planned OI phase.
No OI feature, dataset kind or strategy family exists in the Lab.

## 13. Step 8 implemented: prospective forward tracking

Module `research/lab/forward.py`, additive migration 11 (`lab_forward_*` tables), profile
schema 3 in `evidence.py`, and `market lab forward candidates | enroll | list | show |
pause | resume | stop | check | resolve | run | evidence`.

> This forward tracker is not the simulated-execution paper trader. It measures whether
> historical signal behaviour persists prospectively. A later paper auto-trader will add
> sizing, portfolio state, simulated fills, margin, risk limits and execution rules.

It answers one question: *when Prism genuinely saw this setup live, before knowing what
happened next, did the outcomes resemble the historical research?*

### Relationship to `perps/paper.py`

Prism's existing paper tracker (`perp_paper_checks`) checks the three registered Python
strategies during `market update`. It only checks the newest closed bar within 36 hours,
writes each check once and never backfills. Phase 8 keeps those rules but is a separate,
Lab-only path because the legacy table cannot carry what Lab evidence needs:

- its primary key omits the parameter hash;
- it has no eligibility state, input fingerprint or compiler version;
- it has no enrollment record and no outcome rows;
- its evaluation uses the legacy full-history funding median.

`perps/paper.py`, its table, the scanner and Telegram are unchanged.

### Prospective versus retrospective (no backfill)

- **When a daily bar is observable.** Crypto perp daily bars close at UTC midnight
  (Prism's `close_time`). Bar T may be evaluated only while
  `bar_close ≤ evaluated_at < bar_close + 1 day`, i.e. from completion until the next
  bar completes, before even the 1-day outcome is known (`grace = one_bar_interval_v1`).
  A DuckDB CHECK on `lab_forward_evaluations` enforces the same window, so a direct
  insert of an old bar is refused too.
- **Only the newest completed bar.** `check` looks only at each asset's newest stored bar
  with `close_time ≤ now`. If that bar's window has passed, nothing is recorded. An older
  bar is never evaluated, so a restart after downtime cannot fill in earlier days.
- **Only after enrollment.** Bars must close strictly after `enrolled_at`. The first
  evaluable bar is the next UTC midnight.
- **Inputs must be complete.** The bar must be stored, and a funding settlement at or
  after the bar close must be ingested (minute-snapped). Otherwise the check notes "not
  ingested yet" and retries on the next run inside the window. Prism's local clock is
  never used to label days; only market `close_time` is.
- **What a miss looks like.** A missed bar has no row. `coverage` derives it on read: every
  expected (asset, daily close) after enrollment is `evaluated`, `gap`, `paused`/`stopped`
  or `pending_window` (window still open). Each gap carries a reason: no check ran inside
  the window, or a check ran and noted why it could not evaluate (from the append-only
  `lab_forward_runs` log). Expected evaluations exclude paused days and open windows.

With the PC on roughly 07:15–20:00 and 22:00–23:00 UK time, the bar that closes at 00:00
UTC is still inside its window all day (00:00–24:00 UTC). One successful
`market lab forward run` on any day captures that day's bar. A fully missed day (PC off
all day, or the trip) is a recorded gap, never backfilled.

### Enrollment (why the profile is frozen)

`enroll <profile-id> --reason …` accepts only a historical (schema 1/2) profile whose tier
is `EXPLORATORY` or `RESEARCH_SUPPORTED`. FDR survival and VALIDATED are not required, and
nothing is enrolled automatically. The `TrackingDefinition` freezes:

- the strategy ID, the **enrollment profile ID and its tier**, and the evidence policy;
- family, version and parameters;
- the plan, dataset, experiment, batch and analysis that produced the profile;
- market, side and venue;
- the assets (the screen's universe) and their frozen fee + slippage;
- the causal funding policy;
- the horizons (default: all plan horizons; a subset may be chosen, but the primary horizon
  is required);
- `lookback_days` (the plan's `warmup_days`);
- the grace, entry and exit conventions and the outcome wait;
- `semantics`: the compiler, vocabulary, evaluator and resolver versions.

`tracking_id` is the SHA-256 of that definition. Re-enrolling identical content is refused.
A different horizon set, `--label` or `--continues <tracking>` is a new tracking. The row
also stores `enrolled_at`, the reason, origin and software identity.

Freezing the profile means a later rebuild cannot rewrite what was known when the forward
clock started (e.g. a new policy that relabels the strategy). Future research can therefore
ask "how did strategies that were EXPLORATORY at enrollment perform?" (tested). The
lookback must cover the strategy's warmup plus cooldown, or enrollment is refused.
Status is an append-only event log: `active ⇄ paused`, then `stopped`, which is terminal.
Recorded signals keep resolving after a pause or stop.

`candidates <batch-id>` lists enrollable profiles per family and side and suggests one per
group by the deterministic `plateau_centrality_v1` rule:

1. Prefer neighbourhood `plateau` members, else `mixed`.
2. Never suggest isolated spikes or members without testable neighbours.
3. Pick the member with the most agreeing testable neighbours, then the most testable
   neighbours, then by name.

Effect size, p and q are deliberately not inputs, so the rule cannot pick the historically
best variant. Enrollment remains a manual, recorded choice.

### Evaluations: signal, no signal, not observed

Each evaluation is one row per (tracking, asset, signal bar), `UNIQUE` in the schema:

- `signal`: the Phase 3 `signal` fired at T;
- `no_signal`: eligible, did not fire;
- `ineligible`: inputs complete enough to compile, but the eligibility mask is false at T
  (a missing funding day, insufficient history).

All three are observations. Never-evaluated bars are gaps (above).

Signals come from `compile_strategy` itself, run on a fingerprinted in-memory snapshot of
bars with `close_time ≤ T` and funding available (to the minute) by T. The snapshot covers
`[T − lookback_days, T + 1 min)`. Eligibility, edge trigger and cooldown are therefore
exactly the compiler's. The edge/cooldown state is rebuilt from data legitimately
available at T inside that window, never from future bars or from the recorded rows. A
signal in that window on a day Prism did not observe can still suppress a signal within
its cooldown, because that data existed at T.

Each row records:

- evaluation and signal-bar times;
- the data cutoff and evaluation latency;
- strategy, tracking and enrollment-profile IDs;
- semantics versions and software ID;
- feature values and condition truths at T;
- the compile metadata, including its digest;
- an `input_id` (content ID of the snapshot's fingerprint manifest in
  `lab_forward_inputs`: per-series SHA-256, row counts, bounds, ingestion run IDs).

Rows are not retained as blobs, which would cost about 1 MB per day. The hash identifies
exactly what was read. Later edits to live tables cannot change a recorded evaluation
(tested).

**Write once.** Re-running `check` on a recorded bar recomputes it. An identical answer
(status, eligible, active, fired) is a no-op. A different answer, e.g. after a provider
revision, raises `ForwardConflict` after logging the run. Nothing is overwritten.

### Outcomes (T+1 entry, fixed horizons)

`resolve` mirrors Phase 4 on live tables:

- **Entry and exit.** A signal at T's close enters at **bar T+1's open**: the bar opening
  at T's close. Exit is bar T+h's close.
- **Contiguity.** The bars T..T+h must be contiguous daily bars (stricter than Phase 4's
  positional shift).
- **Returns.** Net uses `perps.backtest.side_forward_returns`: side × (exit/entry − 1),
  minus 2 × (frozen fee + slippage), minus side × Σ funding_day × close / entry over
  T+1..T+h.
- **Funding.** `funding_day` comes from the Lab's causal `lab_funding_day`. The resolver
  reads 8 days of context before T for its trailing 7-day cadence.
- **Gross** is the same price move without costs or funding.
- **Entries.** Signals get a write-once entry record (`entered` with the T+1 open, or
  `unavailable`). It is an analytical reference price, not a fill.

Outcome rows are written once, only when final:

| State | Meaning |
|---|---|
| pending | no row: `now` is before T+h's close, or data is still missing within 7 days of it |
| `resolved` | gross and net recorded with entry/exit prices, funding paid, cost, input ID, resolver version, software |
| `unavailable` | 7 days after the exit close, bars or funding are still missing or late; net and gross are NULL, never zero |

CHECKs forbid recording before the exit close and a `resolved` row without values. Later
price revisions do not change a resolved outcome (tested). No correction mechanism exists
yet: a correction would be a new, separately versioned record, never an update.

Eligible no-signal evaluations get outcomes too. They are the prospective same-asset/side
baseline, mirroring Phase 4's excess definition.

### Forward evidence and maturity

`forward_summary` is computed from immutable rows recorded by `as_of`. It contains:

- **Observation:** enrollment time, first and last evaluated bars, observed days, expected
  evaluations, evaluated, gaps, paused, coverage share, and signal / no-signal /
  inputs-incomplete counts.
- **Entries:** entered and unavailable counts.
- **Per horizon:** signals recorded, resolved, unavailable and pending; independent
  resolved events (Phase 4 greedy per-asset declustering); the net distribution (mean,
  median, hit rate, min, quartiles, max); gross mean; baseline bars and mean; forward
  excess.
- **Comparison with enrollment:** the enrollment profile's historical excess/net/events,
  `direction_vs_historical` (same / opposite) and the excess difference.
- **Maturity:** `lab_forward_maturity_v1`, based on independent resolved primary-horizon
  signal outcomes:

  | Level | Rule |
  |---|---|
  | `TOO_EARLY` | fewer than 10 |
  | `EARLY` | 10–29 |
  | `DEVELOPING` | 30 or more |
  | `MATURE` | ≥ 100 **and** ≥ 180 observed days |

  Maturity is sample size, not quality. A mature negative sample is evidence.
- **Record digests:** of the evaluation and outcome IDs.

No p-value or significance test is computed on forward samples.

`market lab forward evidence <tracking-id>` appends the summary (`lab_forward_summaries`,
content-addressed) and a **new** evidence profile (schema 3):

- `extends` = the enrollment profile;
- the original sources plus a `paper_forward` source citing the tracking, summary, `as_of`,
  record digests and versions;
- a `forward` block;
- the historical fields copied unchanged.

The **tier stays the historical tier**: forward wins can never turn EXPLORATORY into
RESEARCH_SUPPORTED. The enrollment profile and every historical record are only read.
Schema 1/2 payloads omit `forward`, so existing profile IDs and stored content are
unchanged (all Phase 7 tests pass).

### Consumer neutrality

Enrollment, evaluations, outcomes and forward profiles carry no alert, trade, sizing,
leverage or approval fields (tested with the Phase 7 key scan). Tracking an EXPLORATORY
profile is evidence collection only. Forward evidence does not mean trade approval;
consumer policies remain future, separate and versioned.

### Code changes

- Each evaluation and outcome records its software ID. Bug fixes that do not change
  semantics continue the same tracking (as with the Lab's builder provenance).
- If the compiler, vocabulary, evaluator or resolver **version** differs from the
  definition's `semantics`, `check` refuses to evaluate that tracking and says so. Resuming
  means enrolling a new tracking, optionally `--continues <old>`, so incomparable
  semantics are never mixed silently.
- Strategy definitions are immutable by ID, so a changed rule is always a new strategy and
  a new tracking.

### Scheduling (installed)

Use one idempotent command: `market lab forward run`. It ingests Hyperliquid perp candles
and funding (`update_perps`), then runs `check` and then `resolve`. Today only
`market update` ingests perp bars; `market oi collect` does not. A failed update does not
stop the check, and a conflict in `check` does not stop `resolve`.

The Windows Task Scheduler task **"Prism forward"** is installed (2026-10-04, at the
operator's request; Prism code never installs or edits scheduled tasks). It runs the
`forward_run.sh` wrapper, which runs `market lab forward run`: update → prospective
check → outcome resolution.

- **Wrapper:** repo root, with its own `flock`. DuckDB's lock handles overlap with
  `daily.sh` and `oi_collect.sh`. The log is `data/forward.log`.
- **Action:** `wsl.exe -d Ubuntu -- bash -lc "/home/matth/prism/forward_run.sh"`.
- **Triggers:**
  - at log on, delayed 5 minutes: the first chance each day (~07:20 UK), when data for
    the 00:00 UTC bar is complete;
  - daily at 12:00 and 19:30: retries in case an earlier run failed.
- **Settings:** run as soon as possible after a missed start; ignore a new instance while
  one runs; 30-minute limit; runs on battery.
- **Test run (2026-10-04 08:13 UTC, triggered through Task Scheduler):** result 0. The perp
  update reported 13 ok and 0 failed. The check recorded 0 evaluations, correctly, because
  every newest bar closed before enrollment. Resolve had nothing pending.

The command is idempotent: with nothing new, a run only refreshes data and appends a run
log entry, so several runs a day are harmless. The existing 10:00 `daily.sh` also refreshes perp
data; it does not run forward checks. A day with no run inside 00:00–24:00 UTC is a
recorded gap.

### Real cohort (live DB)

The live DB held no Lab records before Phase 8: the Phase 6/7 smoke batch had run on a
scratch copy. It was therefore reproduced in the live DB, governed and append-only, with
the documented parameters and an explicit window:

- **Plan** `hl_perp_smoke_discovery` v1 (`plan_a8e77687…`): six Hyperliquid perps,
  discovery `[2024-10-01, 2026-10-01)`, 400-day warmup, horizons 1d/5d/10d/20d (primary
  10d), taker 4.5 bps plus `config/perps.yaml` slippage, 2,000 draws.
- **Batch** `hl_perp_smoke_families_v1` (`batch_699ddabd…`), families `ma_trend`,
  `donchian_breakout` and `funding_extreme_fade` v1: 40 variants, 36 testable,
  4 insufficient, 0 FDR survivors (q = 0.91 for every EXPLORATORY profile).
- **Profiles** (policy v2): 6 EXPLORATORY, 12 INCONCLUSIVE, 18 NEGATIVE, 4 INSUFFICIENT.

These differ from the scratch run in section 12 (8 EXPLORATORY), whose exact window was not
recorded. Here no `ma_trend` short plateau of three and no EXPLORATORY funding-fade variant
appear. The cohort below comes from these recorded profiles only.

**Selection:** `market lab forward candidates` with `plateau_centrality_v1`, one per family
and side, applied without discretion and without forward outcomes (none exist):

| Strategy | Profile | Why |
|---|---|---|
| `ma_trend_10_50_long` | `evidence_ac9120d8…` | `ma_trend` long plateau (2/2 testable neighbours agree); tie with `ma_trend_20_50_long` broken by name |
| `ma_trend_20_100_short` | `evidence_168da1c0…` | the only EXPLORATORY short; plateau (2/2 agree) |
| `donchian_breakout_55_long` | `evidence_47a57457…` | the only EXPLORATORY non-`ma_trend` family-side; `mixed` (1/2 agree) in an otherwise adverse family. **Stopped before its first evaluation** (below) |

All three were EXPLORATORY, so enrollment granted no trading or alert status.

**Forward clock:** all three were enrolled at 2026-10-04 07:57 UTC with all plan horizons.
The first observable bar closes **2026-10-05 00:00 UTC**. The 2026-10-04 bar closed before
enrollment and is never evaluated.

**Cohort 1 (active):** the two `ma_trend` plateau representatives.

| Tracking | Strategy | Enrolled (UTC) | Status |
|---|---|---|---|
| `tracking_250f844a…` | `ma_trend_10_50_long` | 2026-10-04 07:57:27 | active |
| `tracking_65febaad…` | `ma_trend_20_100_short` | 2026-10-04 07:57:28 | active |

**Stopped:** `tracking_94289899…` (`donchian_breakout_55_long`).

- Enrolled 2026-10-04 07:57:31 UTC; stopped 2026-10-04 08:55:46 UTC through
  `market lab forward stop`.
- **Zero** prospective evaluations, signals, entries or outcomes were recorded before the
  stop, because no bar became observable in between.
- **Why:** its neighbourhood is mixed (1/2 neighbours agree) in an otherwise largely
  adverse family, and cohort 1 was meant to hold only the two `ma_trend` plateau
  representatives. It was enrolled because the rule admits `mixed` members when a
  family-side has no plateau. It is not replaced.
- **Audit trail:** stopping appends a status event. The tracking definition, enrollment
  record and reason stay in the ledger, so history shows it was enrolled and then stopped.
  `stopped` is terminal.
- **Effect:** `check` skips it ("not active (stopped)"). Coverage counts every later bar
  as `stopped`, never as an expected evaluation or a gap, so it contributes nothing to
  future coverage or forward evidence.

**Rehearsal** (scratch copy, back-dated enrollment): 18 evaluations of the 2026-10-03 bar
in 5.8 s; an idempotent re-check; a late check refused; outcomes pending; an extended
profile with an unchanged tier.

### Known limitations

- Daily perp strategies only (Hyperliquid source of the frozen plan); no spot, intraday,
  OI or long-horizon radar tracking.
- Coverage depends on the local PC. A future always-on deployment improves coverage;
  history can never be backfilled.
- No simulated execution: no fills, stops, targets, sizing, margin, portfolio or risk
  engine. Outcomes are fixed-horizon analytical returns.
- Rolling-window snapshots seed recursive features (EMA, Wilder RSI/ATR) `lookback_days`
  before T, unlike the historical screen's fixed window start. SMA/Donchian/funding
  features are unaffected. Recursive values converge but are not bit-identical to the
  research run.
- Evaluation inputs are fingerprinted, not retained. Feature values and provenance are
  recorded with each evaluation, but full source rows are not duplicated per evaluation.
  Exact recomputation of a prospective evaluation currently requires the original source
  rows or an external backup matching the stored fingerprint. Content-addressed input
  retention is a possible future infrastructure improvement.
- No outcome-correction mechanism; no automatic consumer action, alerting or promotion.

## 14. Step 9 implemented: full research and independent validation

Modules: `research/lab/adapter.py` (pure computations) and `research/lab/validation.py`
(governance). Also additive migration 12 (`lab_research_*` tables), evidence profile
schema 4, and the CLI groups `market lab research reserve-plan | register | list | show |
run | evidence` and `market lab validation preview | run`. This phase is research only:
it has no auto-trading, simulated execution, position sizing or consumer policy.

> Validation evidence tests a frozen historical hypothesis on untouched data. It does not
> optimise the strategy.

> Prospective forward evidence and historical validation are separate evidence sources and
> must not be pooled as if they were the same experiment.

### Discovery versus validation

| Stage | Data | Question | Independence |
|---|---|---|---|
| Historical (Phases 4–7) | plan `discovery`/`development` role | Did this look interesting historically? | none: the data selected the strategy |
| `full_research` (Stage A) | the **same** retained discovery snapshot | What do we know from discovery, examined with Prism's deeper methods? | none (recorded as `not_independent`) |
| `validation` (Stage B) | a period the plan reserves as `validation` | Did the frozen strategy's expected-direction effect persist on unseen data? | intact only if nothing touched the window before registration |
| `paper_forward` (Phase 8) | live bars after enrollment | Did it persist when Prism genuinely saw it live? | prospective; separate source |
| `final_holdout` | reserved, never read in Phase 9 | future confirmatory testing | untouched |

### Registration (explicit, frozen)

`market lab research register <profile-id> --validation-plan <plan-id> --reason …` freezes
a `ResearchRegistration`; its `registration_id` is a content hash. Eligibility:

- The source must be a historical (schema 1/2) profile whose tier is `EXPLORATORY` or
  `RESEARCH_SUPPORTED`. FDR survival is not required, but nothing is registered
  automatically.
- Extended profiles (schema 3/4) are refused.
- Re-registering identical content is refused. `--label` creates a deliberate new
  registration.

The registration freezes:

- the strategy ID, name, side, family, version and parameters;
- the source profile, its tier and evidence policy;
- the source plan, dataset, experiment, result, role, batch, batch run and analysis;
- the assets;
- the Phase 6/7 neighbour IDs (from the profile, so they cannot be re-chosen later);
- the validation plan, the validation period and its warmup;
- the full-research, validation and extension policy IDs;
- the semantic versions (screen, compiler, vocabulary, adapter, validation).

The row also stores the reason, origin, software identity and the recorded exposure state
at registration time.

**Frozen strategy.** Strategy definitions are immutable by ID, and the registration cites
one. The validation plan must equal the source plan in every field except `name`,
`version` and the extra reserved periods (`check_plan_compatibility`), so costs, horizons,
the primary horizon, funding, statistics and warmup cannot change either. A "nearby better
EMA", a changed threshold, side, horizon or stop is a different strategy ID: it needs its
own lineage, history and registration. If the semantic versions change, runs are refused
until a new registration is made, so incomparable results are never mixed.

`market lab research reserve-plan <source-plan> --name … --validation-start …
--validation-end … [--final-holdout-start … --final-holdout-end …]` builds such a plan by
copying the source plan and appending the reserved periods. This makes it compatible by
construction.

### Validation period integrity

Registration refuses:

- a plan with no `validation` role;
- a validation period that overlaps the source data region `[discovery start − warmup,
  discovery end)` (the Phase 5 convention: warmup counts as touched);
- a validation data region (warmup included) that would reach a `final_holdout` period.

Validation datasets select `[validation start − warmup, validation end)` and nothing else.
Warmup bars (which may lie in the discovery period) feed only features. Signals, baselines
and outcomes are restricted to the window by the Phase 4 screen. An outcome whose exit
would fall after the window is not evaluable. Tests show that rewriting every bar from the
final holdout onwards, and every discovery bar before the warmup, leaves the validation
result unchanged.

**Looked at once.** Validation is evaluated only when the reserved period is **complete**:
`now ≥ end` and stored bars reach `end − 1 day`. Before that, the attempt is recorded as
`VALIDATION_INSUFFICIENT` (gate `pre_outcome`). The same happens when a capacity bound shows
the stored bars could never reach the policy's sample: at most ⌈(bars − h)/h⌉ independent
primary events per asset. In both cases no validation-window outcome is computed and no
Phase 2 evaluation is started, so no exposure is consumed. `market lab validation preview`
shows the same gates read-only, together with:

- the period, the data region and the final holdout;
- assets, horizons, strategy and source profile;
- independence as recorded now;
- the sample projected from the discovery event rate.

This rule removes sequential peeking: nobody re-runs validation monthly until it "works".

### Exposure

An evaluated validation is a Phase 2 experiment with role `validation` and origin
`lab_validation:<registration>`. The order is:

1. preregister (committed; no exposure yet);
2. insert the Phase 9 run row citing that experiment;
3. Phase 4 `run_screen`, whose `Ledger.start` writes the permanent `evaluation_started`
   record in `lab_inspections`.

`Ledger.exposures(start, end, family_id=…)` therefore shows every look. Neighbour checks
(below) are their own experiments, so their exposure is recorded too.

`independence` labels each registration and evaluated run:

| Label | Meaning |
|---|---|
| `untouched_on_record` | no recorded Lab exposure of the strategy or its ledger family to the window, and no forward outcome in the window, before the registration |
| `compromised` | the strategy itself was exposed (any experiment), or forward outcomes in the window were recorded, before the registration |
| `family_exposed` | another member of the same ledger family was exposed before the registration |

Exposure recorded after the registration (including concurrent Phase 8 observation of the
same days) is listed but does not compromise a registration frozen beforehand. An empty
answer means no *recorded* exposure; direct market-table access is not observable.

**Reruns never restore independence.**

- A second evaluated validation needs `--rerun-of` and `--rerun-reason`. It reproduces the
  same look, is linked as a Phase 2 rerun when the data are identical, and is marked
  `first_look: false`.
- A new registration of the same strategy after a look is `compromised`. So is any family
  member whose own validation look already happened.
- Descendants are new strategies in the same ledger family (lineage is fixed at
  submission), so `family_id` exposure queries include them.

### Adapter: which Prism research steps generic Lab strategies receive

Every result stores this table (`steps`). The adapter reuses Prism code; it is not a
second engine.

| Step | Status | How |
|---|---|---|
| Event study | supported | Phase 4 `screen` = compiler + `perp_asset_events` semantics + `side_forward_returns` + `run_event_study` on the retained snapshot. **Parity** with the stored discovery result (aggregate, per-asset, triage) is checked bit for bit for the target and each neighbour. A test also recomputes it with Prism's own `perp_asset_events` → `run_event_study` |
| Independent events | supported | `backtest.events.decluster` |
| Costs/slippage | supported | per-asset values frozen in the plan |
| Funding | adapted | Lab causal funding (`lab_causal_trailing_7d_median_v1`), not Prism's full-history cadence |
| Random-entry baseline | supported | Phase 4 `random_entry_mean_excess_v1` (primary horizon, plan seed/draws) |
| Walk-forward | adapted | Prism `walk_forward` with a **one-point grid** (the frozen strategy) and zero training length via `make_folds`. Consecutive 6-month blocks, block-local baselines (`window_excess`), no reselection (asserted) |
| Sensitivity | adapted | the registered Phase 6 neighbours re-evaluated on the same data. Prism `plateau_verdict` classifies the target; the Phase 7 sign-agreement label is also reported. No winner is selected |
| Cross-asset | supported | per-asset rows, breadth, concentration, leave-largest-out |
| Horizon profile | supported | all plan horizons; only the primary is tested |
| Regime splits | skipped | the perp runner has none; Lab snapshots hold no regime/benchmark series |
| Portfolio simulation | **unsupported** | risk/sizing are not frozen in a governed policy; max leverage is the latest snapshot (not point-in-time); cross-symbol ordering is an open audit item |
| Legacy `automatic_verdict` | skipped | can return a positive verdict with checks missing |

The only change to existing code for reuse: `ScreenResult` now also carries the
`AssetEvents` it passed to `run_event_study` (a defaulted field), so walk-forward uses
exactly those inputs.

**Walk-forward for a frozen strategy** means evaluating the same definition on
consecutive chronological blocks of the role period, each with its own baseline, and
counting how many adequate blocks (≥ 5 independent events) agree with the expected
direction. A block's events are selected by signal time, so an outcome can end in the next
block (Prism's convention). With nothing trained or selected, this cannot leak.

**Sensitivity** answers "are nearby parameterisations similar?", not "which is best?".

- Rows: each neighbour's events, excess, sign and hit rate.
- Phase 7 label: sign-agreement share.
- Prism `plateau_verdict` for the target: `PLATEAU` if at least 60% of neighbours keep at
  least 50% of the target's excess; `FRAGILE` if at least 50% have the opposite sign. The
  thresholds are frozen copies of `config/backtest.yaml`.
- Effect dispersion (std and range).
- A `knife_edge` flag: `FRAGILE`, or an isolated spike.

The frozen target is always the row being judged.

### Statuses

**Full research** (`lab_full_research_policy` v1): positive only when every prescribed
check completed.

| Status | Rule |
|---|---|
| `FULL_RESEARCH_ERROR` | an exception (recorded with traceback), or parity with the stored discovery result failed |
| `FULL_RESEARCH_INSUFFICIENT` | < 30 independent events or < 3 assets, < 3 adequate walk-forward blocks, or sensitivity undefined/insufficient |
| `FULL_RESEARCH_INCONSISTENT` | excess ≤ 0, walk-forward inconsistent (≤ 34% of adequate blocks positive), or sensitivity `FRAGILE`/`NO_EDGE` |
| `FULL_RESEARCH_CONSISTENT` | walk-forward consistent (≥ 67%), sensitivity `PLATEAU`, not asset-concentrated |
| `FULL_RESEARCH_MIXED` | otherwise |

**Validation** (`lab_validation_policy` v1): descriptive, never an action. The sample
thresholds equal the discovery evidence policy's and are not loosened for a short window.

| Status | Rule |
|---|---|
| `VALIDATION_INSUFFICIENT` | pre-outcome gate (incomplete period, capacity); or < 30 independent events, < 3 assets, < 80% of in-window primary signals evaluable, or < 80% of in-window bars eligible. Too few events is never a rejection |
| `VALIDATION_SUPPORTIVE` | pooled net excess > 0 in the expected direction, ≥ 60% of assets agree, the sign survives dropping the largest contributor |
| `VALIDATION_ADVERSE` | excess ≤ 0 and ≤ 50% of assets agree |
| `VALIDATION_MIXED` | otherwise |
| `VALIDATION_ERROR` | an exception after the start (recorded) |

The p-value on the validation window is recorded as descriptive and never decides a
status. Each result carries a historical-versus-validation comparison, with nothing
pooled:

- effect, net mean/median, hit rate, events, assets and positive-asset share;
- historical raw p and q;
- whether the direction agrees, and the effect ratio;
- per-asset excess side by side.

Each result also carries the independence assessment and, if neighbours were checked,
`depends_on_single_parameterisation`.

### Evidence extension and tiers

`market lab research evidence <registration>` appends a **new** schema-4 profile:

- `extends` the frozen historical profile;
- cites the historical sources plus a `full_research` source and, if run, a `validation`
  source;
- copies the historical fields unchanged and adds `full_research` / `validation` blocks.

Historical (Phase 7) and forward (Phase 8, schema 3) profiles are never modified, and older
payloads stay byte-identical. Profiles are idempotent by identity.

Tier rules (`lab_evidence_extension_policy` v1):

- Full research alone never changes a tier: it re-reads the discovery data.
- `EXPLORATORY → RESEARCH_SUPPORTED` requires all of:
  - `VALIDATION_SUPPORTIVE`;
  - `first_look`;
  - `untouched_on_record`;
  - `FULL_RESEARCH_CONSISTENT`.
- `VALIDATION_ADVERSE` caps `EXPLORATORY`/`RESEARCH_SUPPORTED` at `INCONCLUSIVE`.
- `INSUFFICIENT`/`MIXED` validation leaves the tier unchanged.

**VALIDATED remains unreachable.** The extension policy's `validated_reachable` is the
literal `False`, and the schema-4 model rejects a `VALIDATED` tier outright. The Phase 7
contract only *represents* VALIDATED with a validation source; its existing test still
passes. Running the validation adapter does not meet the future standard, which should
include prospective/paper-forward evidence and probably final-holdout confirmation.
Phase 8 enrollment refuses schema-4 profiles: tracking starts from historical evidence.

### Interaction with Phase 8 forward evidence

Forward tracking keeps running unchanged:

- Phase 9 reads the forward tables only to report:
  - the independence assessment (forward outcomes recorded before a registration
    compromise it);
  - a separate `forward` section in `research show`.
- Forward rows never enter validation metrics. A test enrolls and tracks a strategy inside
  its validation window, then shows the stored validation metrics equal a pure screen of
  the validation snapshot.
- When a validation window covers the same days as prospective tracking (as for the
  current cohort), the two are **not** independent confirmations of each other. Each
  result flags `concurrent_prospective_tracking`.

### Audit items (section 2) and this phase

| Item | Affects Phase 9? | Handling |
|---|---|---|
| Perp warmup eligibility | No | the Lab compiler's explicit eligibility (full feature warmup, defined conditions, funding known) is what the screen passes to the event study |
| Funding cadence from full history | No | Lab plans freeze the causal trailing-7-day policy; Prism's `daily_funding` default path is untouched and unused here |
| Portfolio event ordering | Would | portfolio simulation is **unsupported** rather than run on known-unaudited ordering |
| Incomplete checks permitting positive verdicts | Would | `automatic_verdict` is not used; Phase 9 positive statuses require every prescribed check |
| Name-based evidence matching | No | everything is keyed by content IDs; Phase 9 writes no `research_runs` rows, so the legacy latest-by-name reader can never mistake Lab results for validation |
| Historical margin inputs not PIT | Would | only via simulation, which is unsupported |

### Ledger clock fix

While Phase 9 tests ran, `Ledger.start` intermittently raised "clock precedes
preregistration". This is the same class of error as the intermittent `Ledger.record_result`
failure ("completion clock precedes start") seen at Phase 8 closure. A wall-clock probe run
beside the tests showed the cause: **this WSL2 host steps its wall clock back by about
0.6 s every ~32 s** while it re-synchronises (47 steps of 592–685 ms during a
25-minute probe on 2026-10-04 from 10:02 UTC).

`ledger.ordered_now(not_before)` now handles the step:

- a regression of at most 5 s (`CLOCK_STEP_TOLERANCE`) is recorded as the earlier record's
  timestamp (equal, never earlier);
- a larger regression is still refused as an impossible ordering.

Program order already proves that a start follows its preregistration and a result follows
its start. A test simulates both cases. Phase 9's own run/result timestamps use the same
helper. Forward CHECKs compare against market close times far from "now" and are
unaffected.

### Real cohort

Run on the live DB on 2026-10-04 at about 10:30 UTC, with code `b24ce83`. The DB was backed up
first to `data/prism.pre_phase9.duckdb`. Candidates were the two active Phase 8 strategies,
chosen by `plateau_centrality_v1` from historical evidence before any forward outcome
existed. No forward data was used, and no third strategy was added.

**Validation plan.** `hl_perp_smoke_validation` v1 (`plan_5b936985…`) was made with
`research reserve-plan` from `hl_perp_smoke_discovery` v1. It is identical except for the
reserved periods:

| Role | Period |
|---|---|
| discovery (unchanged) | [2024-10-01, 2026-10-01) |
| validation | [2026-10-01, 2027-10-01) |
| final holdout | [2027-10-01, 2028-10-01) |

The source plan reserved **no** validation period. Hyperliquid funding starts on
2023-10-04, inside the discovery dataset's 400-day warmup. So the only untouched
Hyperliquid data starts on 2026-10-01. The 12-month length was fixed before any validation
outcome existed and will not be changed after this run.

| | `ma_trend_10_50_long` | `ma_trend_20_100_short` |
|---|---|---|
| Registration | `research_b9c29c47…` | `research_32a1dcf6…` |
| Historical profile / tier | `evidence_ac9120d8…` / EXPLORATORY | `evidence_168da1c0…` / EXPLORATORY |
| Historical 10d excess / net / hit rate | +1.38% / +2.17% / 53% | +2.61% / +1.32% / 56% |
| Historical sample / assets agreeing | 77 independent on 6 assets / 67% | 39 independent on 6 assets / 67% |
| Raw p / q (batch) | 0.152 / 0.915 | 0.112 / 0.915 |
| Parity with stored discovery result | exact (target and both neighbours) | exact (target and all three neighbours) |
| Walk-forward (6-month blocks, frozen) | consistent: 3/4 blocks positive (+1.75%, +2.18%, **−6.04%**, +1.81%; 20/23/12/22 events) | consistent: 3/4 positive (+11.35%, +3.80%, **−4.48%**, +2.17%; 8/7/10/14 events) |
| Sensitivity (Prism `plateau_verdict`) | PLATEAU: neighbours 20/50 (+1.90%) and 10/100 (+2.08%) agree and keep ≥ 50% | MIXED: neighbours 10/100 (+0.81%) and 20/50 (+0.49%) agree in sign but keep < 50% of the target's effect; 20/200 has too few events |
| Knife-edge | no | no (but the target sits above its neighbours) |
| Portfolio simulation | unsupported | unsupported |
| **Full research** | **FULL_RESEARCH_CONSISTENT** | **FULL_RESEARCH_MIXED** (sensitivity) |
| Validation independence | `untouched_on_record` | `untouched_on_record` |
| Validation bars stored | 4 per asset (closes 2026-10-01 … 10-04) of 365 days | same |
| Projected validation sample at completion | 38.5 independent 10d events (minimum 30) | **19.5 (below the minimum 30)** |
| **Validation** | **VALIDATION_INSUFFICIENT** (pre-outcome: period incomplete, capacity 0) | **VALIDATION_INSUFFICIENT** (same) |
| Validation exposure consumed | none (no Phase 2 experiment started) | none |
| Extended profile (schema 4) | `evidence_4f05b80c…`, tier **EXPLORATORY (unchanged)** | `evidence_a3c4eacc…`, tier **EXPLORATORY (unchanged)** |

Notes:

- The 2025-10 → 2026-04 block is negative for both strategies: the historical effect is
  not uniform in time.
- The validation window coincides with Phase 8 forward tracking (first observable bar
  2026-10-05). When it completes, validation and forward will describe the same market days
  and must not be counted as two confirmations.
- On the discovery event rate, the short strategy is unlikely to reach an adequate
  validation sample in 12 months. It will then be reported as insufficient; the window
  will not be stretched for it.

After the run:

- Phase 8 tracking definitions are unchanged (same content hash), with 0 forward
  evaluations, 2 active trackings and 1 stopped.
- All 40 historical profiles are byte-identical to the backup.
- There are no validation-role experiments and no new `lab_inspections` rows.
- `market lab forward check --dry-run` behaves as before.

### Known limitations

- **Short validation history.** On Hyperliquid the only data untouched by the discovery
  batch (warmup included) starts on 2026-10-01. Hyperliquid funding starts on 2023-10-04,
  inside the discovery warmup, so no earlier window is untouched. Binance pre-2023 history
  is a different venue, and legacy perp research already used it for EMA-trend hypotheses,
  so it is not a clean holdout for `ma_trend` without a separately justified cross-venue
  design.
- Daily perp strategies only. Spot registration, intraday data, OI and portfolio
  simulation are not supported.
- Neighbour checks during validation expose the neighbours' validation windows as well;
  this is recorded and makes later validation of those neighbours `compromised`.
- Recorded exposure is application-level, not tamper-proof; direct market-table access
  cannot be observed.
- Family exposure uses the ledger family; correlated strategies from other families are
  not tracked as exposed.
- Walk-forward blocks are 6 months, so a two-year discovery window gives four blocks with
  modest per-block samples.

## 15. Step 10 implemented: perps co-pilot

Package `src/market_signal/copilot/` (`policy.py`, `engine.py`, `render.py`), additive
migration 13 (`copilot_*` tables) and `market lab copilot …`. The co-pilot watches current
daily perp bars, detects when a watched Strategy Lab setup fires, reads the latest governed
evidence for it, applies a separately versioned **co-pilot policy**, and sends a short
Telegram message through Prism's existing `portfolio/telegram.py` client.

The message means: *this setup is happening now; here is what Prism knows historically,
how strong or weak that evidence is, and why it may be worth looking at.* It does not
mean "take this trade".

> Prism's co-pilot policy is intentionally less strict than a future automated-execution
> policy. This changes what is surfaced to a human, not what the research says.

> No co-pilot alert can directly trigger an order.

### Evidence versus consumer policy

- **The co-pilot consumes evidence and never modifies it.** It reads strategies, evidence
  profiles (historical, schema-4 full research/validation extensions) and the Phase 8
  forward summary. It writes only `copilot_*` tables, including its own software registry
  (`copilot_software`), so not even `lab_software` changes. No module under
  `research/` imports `copilot` (tested). Screens, FDR analyses, tiers, forward
  evidence and validation cannot depend on a co-pilot decision.
- **Exploratory signals may be surfaced.** EXPLORATORY and RESEARCH_SUPPORTED are
  eligible. VALIDATED is not required, nor are FDR survival, supportive validation or
  mature forward evidence. Requiring those would keep the co-pilot silent for years. They
  are shown as caveats instead.
- **Co-pilot priority is not the evidence tier.** An alert can be `EXPLORATORY` evidence
  with `STRONG WATCH` priority. Priority is a reading-order label for a human. The tier is
  never changed to make an alert look stronger.
- **An alert is not a trading recommendation.** The side is shown as LONG/SHORT BIAS: the
  tested hypothesis, not an instruction. There are no BUY/SELL/APPROVED labels and no
  probabilities.
- **Auto-trader promotion will use a separate future policy.** It must be materially
  stricter. Co-pilot eligibility never implies it. No co-pilot record has an
  executable/approval/order/size/leverage/stop/target/risk field (tested by a key and
  column scan). The compiled exit-intent stop is deliberately not copied into co-pilot
  records.
- **Validation and forward evidence are shown with their current maturity.** "Validation:
  not mature / insufficient (reserved period runs to 01 Oct 2027)" and "Forward: TOO EARLY
  (0 resolved, 0 prospective signals so far)" are printed instead of being omitted.

### Policy `copilot_policy` v1 (`copolicy_e235b9de…`)

The policy is a frozen model. Its content hash covers every threshold and rule, so changing
anything is a new version and a new ID (`market lab copilot policy`). It decides only
whether a **fired** signal is surfaced, and with what priority.

**Suppress** (SUPPRESS is recorded with every failing rule) if any of these hold:

| Rule | v1 condition |
|---|---|
| `versions_compatible` | the watch's frozen compiler/vocabulary versions equal the running code's, and every cited evidence source used the same versions |
| `data_quality` | the largest gap between daily bars in the signal's lookback is above 24 h |
| `signal_new` | this strategy/asset/bar was already surfaced (under any policy) |
| `strategy_not_retired` | the strategy has forward trackings and every one is stopped |
| `evidence_available` | no usable evidence profile |
| `tier_eligible` | tier is not EXPLORATORY or RESEARCH_SUPPORTED (NEGATIVE, INSUFFICIENT, UNAVAILABLE and **INCONCLUSIVE** are blocked) |
| `sample_adequate` | fewer than 30 independent primary-horizon events, or fewer than 3 assets |
| `effect_positive` | primary-horizon net excess in the tested direction is ≤ 0 |
| `breadth_ok` | one asset dominates (pooled sign flips without it) or holds > 50% of events |
| `not_isolated_spike` | the parameter neighbourhood is an isolated spike |
| `full_research_not_adverse` | full research is `FULL_RESEARCH_INCONSISTENT` |
| `validation_not_adverse` | validation is `VALIDATION_ADVERSE` (Phase 9 also caps such a tier at INCONCLUSIVE) |
| `forward_not_adverse_mature` | forward maturity is MATURE **and** forward net excess is ≤ 0 or points opposite to history |

Not suppressed, shown as caveats: q above 0.10 ("did not survive family correction"),
validation insufficient or not run, forward TOO_EARLY/EARLY (even if negative), full
research MIXED/INSUFFICIENT/not run, and a non-plateau neighbourhood.

**Priority** (ALERTs only, explicit rules, no weighted score):

- `STRONG_WATCH` if at least one of these holds and no downgrade applies:
  - tier RESEARCH_SUPPORTED;
  - validation SUPPORTIVE;
  - full research CONSISTENT, a plateau neighbourhood and broad support (≥ 5 assets with
    events, ≥ 60% positive).
- Downgrades to `WATCH`: validation MIXED, or DEVELOPING/MATURE forward evidence pointing
  the other way.
- Otherwise `WATCH`.

Before outcomes are mature, `ma_trend_10_50_long` reads as STRONG WATCH (consistent full
research, plateau, 6 assets) and `ma_trend_20_100_short` as WATCH (full research MIXED on
sensitivity).

### Watchlist

A strategy is never alerted automatically. A **watch** registration (`market lab copilot
watch <historical-profile-id> --reason …`) freezes these, and its ID is their content hash:

- the strategy;
- its historical baseline profile and tier;
- the evidence policy and the co-pilot policy ID;
- market, side, venue, assets, primary horizon and lookback (derived read-only from the
  screening plan with Phase 8's `build_definition`);
- the compiler/vocabulary versions.

Statuses are `active`, `paused` or `stopped` (terminal), recorded as appended events. A
strategy has at most one open watch.

**Changing the policy** means stopping the watch and registering a new one. A new watch
only evaluates bars that close **after** its registration, so historical signals are
never replayed. The cross-policy `signal_new` rule is a second guard.

### Signal semantics

Live evaluation is the Phase 3 compiler on Phase 8's live window. Phase 8's helpers are
now shared functions (`newest_bar`, `in_live_window`, `live_snapshot`,
`funding_ready`), with behaviour unchanged and forward tests unchanged and passing.

- Bar T is the newest completed daily bar. It is evaluated only while
  `T <= now < T + 1 day`, from a snapshot of rows available at T's close: bars with
  `close_time <= T`, funding judged to the minute, and the plan's lookback.
  Edge-trigger and cooldown state is rebuilt from that snapshot.
- Only `signal` (the rising edge, with cooldown) can alert. Conditions that merely remain
  true do not. A bar outside its window is `OUTSIDE_WINDOW` and never alerts: there are no
  "you missed this yesterday" messages.
- If funding through T is not ingested yet, the state is `WAITING_FOR_DATA` and a later
  run inside the window retries.
- The co-pilot and the forward tracker observe the same signal with the same semantics.
  They are separate consumers. A decision records what the tracker recorded for that bar,
  if anything (`forward_tracker_recorded`), as a consistency check. The co-pilot never
  writes forward records; `market lab forward run` stays responsible for them.

### Evidence presentation

For each signal the engine builds an evidence view from the governed records:

- the newest schema-4 profile extending the watch's baseline, or the baseline itself;
- plus a read-only `forward_summary` of the strategy's tracking.

Each stage stays separate: historical (tier, primary-horizon excess/net/hit rate,
independent events, asset breadth, neighbourhood), FDR (raw p, BH q, survivor status),
full research (status, walk-forward blocks, sensitivity), validation (status, sample,
window) and forward (maturity, prospective/resolved counts, and direction only once past
TOO_EARLY). There is no combined confidence number. The decision stores this view, so
"why did Prism alert me?" can be answered from the decision alone.

### Telegram message

Example: the live rendering check (`market lab copilot preview ma_trend_10_50_long --symbol
ETH`) on the 2026-10-04 bar. It is labelled PREVIEW because that bar was **not** a new
signal:

```
PREVIEW — no current signal. Rendering check only; not an alert.

ETH · LONG BIAS
MA Trend 10/50 · STRONG WATCH

Setup (daily close 04 Oct 00:00 UTC)
• Close above 50D EMA (2,687 vs 2,474)
• 10D EMA above 50D EMA (2,678 vs 2,474)
• Not a new signal on this close (10-bar cooldown)

Evidence · EXPLORATORY
10d excess in the tested direction vs same-asset baseline: +1.4% (net +2.2%, hit rate 53%)
77 independent events · 6 assets (4 positive)
Parameter neighbourhood: plateau
Raw p 0.15 · BH q 0.91 (did not survive family correction)

Research
Full research: CONSISTENT · walk-forward 3/4 blocks positive · sensitivity plateau
Validation: not mature / insufficient (reserved period runs to 01 Oct 2027)
Forward: TOO EARLY (0 resolved, 0 prospective signals so far)

Why surfaced: data current and complete, signal is new, evidence tier eligible,
adequate sample, positive historical effect, broad asset support, full research not adverse
Priority: consistent full research, plateau neighbourhood and broad asset support
Exploratory evidence — human review only. Not an automated trade signal.
```

**Several signals in one run.** Up to 3 ALERTs are sent as individual messages. More are
sent as one compact digest of 3 lines each, so no valid signal is dropped and Telegram is
not flooded. A condition shown in a preview that is false is marked "— not met".

### Audit trail

| Table | What |
|---|---|
| `copilot_policies` | released policy payloads by content ID |
| `copilot_watchlist`, `copilot_watch_status` | frozen watches and appended status events |
| `copilot_runs` | every run, with a note per watched strategy/asset (`NO_SIGNAL`, `WAITING_FOR_DATA`, `OUTSIDE_WINDOW`, `BEFORE_REGISTRATION`, `ERROR` …), so non-firing days are auditable without a row per day |
| `copilot_decisions` | one row per fired signal: `ALERT` (with priority) or `SUPPRESS` |
| `copilot_deliveries` | append-only delivery attempts (`attempted`, then `sent` or `failed`) |
| `copilot_software` | the co-pilot's own software registry |

Each `copilot_decisions` row stores:

- the evidence view and the profile ID used;
- the signal (conditions, feature values, data cutoff, compile digest, input fingerprint);
- every rule result, the caveats and the priority reasons;
- the policy and software IDs and the engine/render versions;
- the rendered message and its SHA-256.

The decision row is `UNIQUE (policy_id, strategy_id, symbol, bar_close)`. It also has a
CHECK that `evaluated_at` lies inside the bar's live window, and a CHECK that only ALERTs
carry a priority.

**Delivery.** Decisions are committed before anything is sent, so a Telegram failure cannot
lose or alter them.

- A failed attempt is retried by the next run while the bar is still live.
- An `attempted` row with no outcome (the process died mid-send) is reported as `unknown`
  and never resent automatically. Retries therefore cannot duplicate a message.
- If Telegram is not configured, that is recorded as a failed delivery.
- `market lab copilot run` exits 1 when a delivery failed.

### CLI

| Command | |
|---|---|
| `market lab copilot policy [--version N]` | the frozen policy and its ID |
| `market lab copilot watch <profile-id> --reason … [--policy-version N] [--label] [--dry-run]` | WRITE: register a watch |
| `market lab copilot watchlist` | watches, status, decision counts |
| `market lab copilot pause/resume/stop <watch-id> --reason …` | WRITE: status events |
| `market lab copilot candidates [--full] [--now ISO]` | read-only: each watched strategy/asset's state, conditions, new-signal flag, and the decision (for a live signal) or `if_it_fired` (inspection only) |
| `market lab copilot preview <strategy> --symbol X` | read-only rendering check (labelled PREVIEW unless genuinely fired) |
| `market lab copilot run [--dry-run] [--update]` | WRITE: evaluate → record decisions → send new ALERTs; `--dry-run` writes and sends nothing and prints the messages it would send |
| `market lab copilot decisions [--limit]`, `show <decision-id> [--message]` | recorded decisions with delivery state; one decision in full |

### Scheduling

There is no new scheduler. `forward_run.sh`, which the installed **"Prism forward"** Task
Scheduler task already runs at log-on (+5 min) and at 12:00 and 19:30, now runs:

1. `market lab forward run` (perp update → check → resolve);
2. then `market lab copilot run --no-update`.

The co-pilot's exit code is logged as `copilot=` in `data/forward.log`. The wrapper exits
with the forward code if that failed, otherwise with the co-pilot code. Both commands are
idempotent. The first run after 00:00 UTC is the first chance to alert on the new bar.
Later runs inside the day only retry failed deliveries or pick up late data.

### Live setup (2026-10-04)

The DB was backed up to `data/prism.pre_phase10.duckdb` first. Migration 13 was applied on
the first writable open.

**Rehearsal on a scratch copy.** Both watches were registered; `candidates`,
`preview` and `run --dry-run` were checked. The results matched the live run below,
including identical watch IDs.

**Watchlist (live).** Both watches are active and were registered at about 11:52 UTC. The
first alertable bar is the 2026-10-05 00:00 UTC close.

| Strategy | Watch | Baseline | If it fired today |
|---|---|---|---|
| `ma_trend_10_50_long` | `copwatch_32cde88c…` | `evidence_ac9120d8…` EXPLORATORY | ALERT · STRONG WATCH (no rule blocks) |
| `ma_trend_20_100_short` | `copwatch_c0e4e977…` | `evidence_168da1c0…` EXPLORATORY | ALERT · WATCH (full research mixed) |

`donchian_breakout_55_long` is not watched. Its forward tracking was stopped, and the
policy would block it (`strategy_not_retired`).

**State on the 2026-10-04 00:00 UTC bar.**

- `ma_trend_10_50_long`: both conditions hold on all 6 assets (close and EMA10 above
  EMA50), but on none is it a **new** signal. The edge fired earlier and the cooldown or
  continuation applies.
- `ma_trend_20_100_short`: neither condition holds on any asset.
- The bar also closed before registration, so it is `BEFORE_REGISTRATION` and could not
  alert anyway.

`run --dry-run` gave 0 decisions and 0 messages. One real `run` and one wrapper run
recorded run summaries with 0 decisions and 0 deliveries. **No Telegram message was sent
and no signal was manufactured.** The first real alert will be sent when the next genuine
signal fires.

### Known limitations

- Daily perp strategies only: there are no intraday, OI or long-horizon families.
  Cross-venue corroboration (section 16) exists, but co-pilot policy v1 does not read it.
- Only two strategies are watched.
- The forward sample is empty (TOO_EARLY). Validation is not mature until the reserved
  period completes (2027-10-01), and the short strategy is projected to stay insufficient.
- The alert window equals the forward window (one bar). If the PC is off all day, that
  day's signals are never alerted, by design.
- The `data_quality` rule checks only bar contiguity in the lookback and funding
  readiness. Stale data appears as `OUTSIDE_WINDOW`/`WAITING_FOR_DATA` notes, not as
  messages. The daily brief's freshness warnings remain the place to notice a broken
  update.
- Evidence profiles are read as recorded. A newer Phase 9 extension is picked up
  automatically, but a new historical batch needs a new watch.
- A crash between sending and recording leaves an `unknown` delivery that needs a manual
  look (`market lab copilot show`).

## 16. Step 11 implemented: cross-venue historical corroboration

Module `research/lab/corroboration.py`, additive migration 14 (`lab_corroboration_*`
tables), dataset role `corroboration`, evidence stage `cross_venue_corroboration`,
evidence profile schema 5 and the CLI group
`market lab corroboration register | run | show | list | evidence`. Research only: no
consumer, eligibility or auto-trader field exists, and co-pilot policy v1 does not read it.

> Binance historical corroboration is supporting historical evidence, not independent
> validation, because parts of the Binance history have been used in prior Prism research.

The question is narrow: *does the same frozen strategy show broadly similar behaviour on
another major perp venue, in earlier market regimes?*

| Evidence | Venue / data | Independence |
|---|---|---|
| Hyperliquid discovery + full research (Phases 4–9) | Hyperliquid 2024-10-01 → 2026-10-01 | none (selected the strategy) |
| Hyperliquid reserved validation (Phase 9) | Hyperliquid 2026-10-01 → 2027-10-01 | **independent validation** (untouched on record) |
| **Cross-venue corroboration (Phase 11)** | **Binance 2019-09-01 → 2024-10-01** | **none: historically exposed, non-independent** |
| Prospective forward (Phase 8) | live Hyperliquid bars | prospective, separate |

### Machinery (reused, not a second backtester)

- **Plan.** The corroboration plan is the strategy's source plan with only three things
  changed: `source` (the venue), the venue's per-asset costs, and one `corroboration`
  period (`check_plan_compatibility`). Horizons, the primary horizon (10d), Lab causal
  funding, statistics, warmup (400 days), T+1-open entry, T+h-close exit, the outcome
  boundary and the gates are identical.
- **Governed look.** The look is a Phase 2 experiment (role `corroboration`, origin
  `lab_corroboration:<registration>`) run by Phase 4 `run_screen`. Its start is the
  permanent `evaluation_started` record in `lab_inspections`, so `Ledger.exposures` shows
  it. The pure screen is recomputed on the same retained snapshot and must match the
  governed result exactly (parity) before the deeper summaries use its events.
- **Deeper summaries.**
  - Walk-forward: the Phase 9 `frozen_walk_forward`, 6-month blocks with no reselection.
  - Neighbours: `neighbour_sensitivity` (Prism `plateau_verdict`). Each registered
    neighbour is its own governed experiment. This is descriptive only and never
    affects the status.
  - Breadth and horizons: Phase 7 `asset_summary` / `horizon_summary`.
  - Regime blocks: Prism `window_excess`.
- **Only venue rows.** Dataset selections name the venue and nothing else. The evaluator
  refuses a snapshot with any other source. A test rewrites Hyperliquid prices for the
  same window and shows nothing changes.

### Registration (explicit, frozen)

`market lab corroboration register <profile> --start … --end … --reason …` accepts a
historical (schema 1/2) or full-research (schema 4) profile. `--dry-run` writes nothing
and shows:

- the frozen strategy definition;
- the period and its warmup-inclusive data region;
- included and excluded assets;
- per-asset coverage: first and last bar, bar gaps, funding settlements and irregular
  settlement days;
- venue costs and funding semantics;
- the recorded historical exposure.

The registration (content-hashed `xvenue_…`) freezes:

- the strategy, side, family and parameters;
- the base and historical profiles;
- the source plan, dataset, experiment, result and period;
- the batch;
- the neighbours (taken from the profile, so they cannot be re-chosen);
- the venue plan and period, the included and excluded assets;
- the full-research and corroboration policy IDs;
- the semantic versions.

Rules:

- **Earlier regimes only.** The period must end no later than the source discovery period
  starts, so no market day contributes an outcome to both venues. Reserved Hyperliquid
  validation and final-holdout windows are therefore never touched.
- **Frozen strategy.** The strategy is cited by its immutable ID. There is no parameter
  option, and changed semantics refuse the run.
- **Looked at once.** A second run needs `--rerun-of` and `--rerun-reason`, is marked
  `first_look: false`, and never replaces the earlier result.
- **Never independent.** `independent` is the literal `False` in the registration model,
  the policy (`satisfies_independent_validation: false`), every result, the profile
  block, and a database `CHECK (NOT independent)`.

### Historical window (frozen before evaluation)

**Binance USD-M, [2019-09-01, 2024-10-01)**, primary horizon 10d. The data region with
the plan's 400-day warmup is [2018-07-28, 2024-10-01). Only rows from 2019-09-09 exist.

- **Start:** Prism's configured Binance history start (`venues.binance.history_start`,
  the USD-M launch month). This keeps every stored year.
- **End:** the Hyperliquid discovery start.

Both boundaries were fixed from coverage alone, before any outcome was computed.

| Asset | First Binance bar (close) | Bars in period | Gaps | Funding settlements | Notes |
|---|---|---|---|---|---|
| BTC | 2019-09-09 | 1,849 | 0 | 5,543 | 3/day (8-hourly) |
| ETH | 2019-11-28 | 1,769 | 0 | 5,309 | 3/day |
| LINK | 2020-01-18 | 1,718 | 0 | 5,156 | 3/day |
| SOL | 2020-09-15 | 1,477 | 0 | 4,510 | 3/day; 2022-11-09 → 11-18 (FTX crash) up to 12/day |
| AAVE | 2020-10-17 | 1,445 | 0 | 4,337 | 3/day |
| HYPE | — | 0 | — | 0 | **excluded**: no Binance perp history stored (HYPE launched Nov 2024, after the period) |

Each asset's first day also has a partial settlement count. Assets listed after the
period start are included from their listing. Their features warm up from their own first
bar, and nothing is back-filled. Asset composition therefore differs from Hyperliquid
(no HYPE; fewer alts in 2019–2020).

### Venue semantics

| | Hyperliquid (source) | Binance (corroboration) |
|---|---|---|
| Fee per side | 4.5 bps | 5.0 bps (`venues.binance.taker_fee_bps`) |
| Slippage per side | BTC/ETH 2, SOL 4, LINK 6, AAVE 8 (HYPE 6) | same per-coin table (`perp_costs(cfg, coin, "binance")`) |
| Funding | hourly settlements | 8-hourly (shorter intervals in a few volatile episodes) |
| Contract | USDC-margined perps | USDT-margined linear perps (last-price candles) |

- Costs are frozen in the venue plan through Prism's own `perp_costs`. Stop slippage
  does not apply because the event study has no stops.
- Funding uses the same Lab causal policy on both venues. Each daily bar sums the settled
  rates in (open, close], with the cadence inferred from the trailing 7 days. A full day
  takes its actual sum, so the SOL 2022 episode is not scaled down. Both venues therefore
  yield a daily funding fraction of notional.
- Binance's baseline funding (about 0.01% per 8h) differs in level from Hyperliquid's.
- Daily bars open at 00:00 UTC on both venues.

A difference in outcome may reflect market structure, cost, funding or asset composition
as well as strategy quality.

### Exposure (recorded honestly)

Recorded at registration, for both strategies:

- **Legacy Prism research used this Binance history.** On 2026-10-03 at 12:32–12:44 UTC,
  `perp_trend_ls@binance` (×2), `perp_funding_fade@binance` and
  `perp_breakout_ls@binance` were run on Binance per-coin windows from 2019-09-11 to
  2023-10-03. All were REJECT except the first `trend_ls` run (INSUFFICIENT_DATA).
  `trend_ls` is a related moving-average trend rule (SMA 50/150 plus a 20-day breakout,
  long and short), not the Lab's EMA `ma_trend` rule. Those windows cover **79.9% of the
  corroboration period**.
- The `ma_trend` family file was committed about seven hours later (2026-10-03 19:45
  UTC), and both strategies were registered after the legacy runs
  (`before_strategy_registered: true`).
- **Not recorded:** which human design decisions (family design, parameter grids,
  strategy choice) those results influenced. The record says only that they existed
  first.
- The last 20% of the period (2023-10-03 → 2024-10-01) was not in legacy Binance runs.
  Those market days were, however, in legacy Hyperliquid runs and in the Hyperliquid
  discovery warmup (features only).
- There were no earlier Lab experiments on Binance. The two corroboration looks, plus 5
  neighbour looks, are now recorded exposure of this window.

### Statuses (`lab_cross_venue_corroboration_policy` v1, `xvpolicy_712462d8…`)

Descriptive only; the p-value never decides a status. Sample thresholds equal the
discovery evidence policy's.

| Status | Rule |
|---|---|
| `CROSS_VENUE_INSUFFICIENT` | < 30 independent primary events or < 3 assets with events (never an adverse finding) |
| `CROSS_VENUE_CORROBORATIVE` | excess > 0, ≥ 60% of assets positive, sign survives dropping the largest contributor, walk-forward `consistent`/`mixed`, regimes `broadly_persistent`/`unstable` |
| `CROSS_VENUE_ADVERSE` | excess ≤ 0 and ≤ 50% of assets positive |
| `CROSS_VENUE_MIXED` | otherwise (reasons listed) |
| `CROSS_VENUE_ERROR` | exception or parity failure (recorded) |

**Regimes:** the period is split into three equal-length chronological blocks:

- early [2019-09-01, 2021-05-12);
- middle [2021-05-12, 2023-01-21);
- late [2023-01-21, 2024-10-01).

Each block uses block-local baselines. A block is adequate with ≥ 5 independent events.
Labels:

| Label | Adequate blocks positive |
|---|---|
| `broadly_persistent` | all |
| `unstable` | ≥ 2, but not all |
| `concentrated` | exactly 1 |
| `adverse` | none |
| `insufficient` | fewer than 2 adequate blocks |

### Real run (live DB, 2026-10-04 ~12:52 UTC)

The DB was backed up first to `data/prism.pre_phase11.duckdb`. The candidates were the two
active Phase 8/10 strategies, chosen by `plateau_centrality_v1` before any forward or
co-pilot outcome existed. Nothing was tuned or added after the results.

Plan: `hl_perp_smoke_discovery_xv_binance_20190901_20241001` v1 (`plan_a79e2b01…`).

| | `ma_trend_10_50_long` | `ma_trend_20_100_short` |
|---|---|---|
| Registration | `xvenue_e77932f8…` | `xvenue_07cd8c5f…` |
| Binance independent 10d events / assets | 142 / 5 | 86 / 5 |
| Pooled net excess (expected direction) | **−0.34%** | **+1.08%** |
| Median excess / net mean / net median | −1.47% / +1.75% / +1.10% | +3.50% / −1.26% / +0.84% |
| Hit rate (net > 0) | 52% | 52% |
| Per-asset excess | BTC +2.43%, ETH +0.58%, LINK +0.83%, SOL −3.10%, AAVE −3.26% | BTC +1.16%, ETH +2.90%, LINK +3.37%, SOL +1.64%, AAVE −2.40% |
| Breadth | 3/5 positive; sign flips without AAVE (+0.44%): dominated | 4/5 positive; max event share 27%; survives leave-largest-out (+0.44%) |
| Walk-forward (6-month blocks) | mixed: 4/9 adequate blocks positive | mixed: 3/7 adequate blocks positive |
| Regimes early / middle / late | +0.19% (31) / −0.17% (52) / −0.75% (59): **concentrated** | −5.95% (6) / **+3.81% (40)** / −2.42% (40): **concentrated** |
| Neighbours (descriptive) | NO_EDGE: 20/50 −2.24%, 10/100 −2.49% | PLATEAU: 10/100 +2.05%, 20/50 +2.45%, 20/200 +0.002% |
| Horizons 1d/5d/10d/20d | −0.63 / −0.03 / −0.34 / −1.35% | +0.28 / +0.11 / +1.08 / +3.51% |
| Random-entry p (descriptive) | 0.59 | 0.29 |
| Parity (governed = recomputed) | exact | exact |
| **Status** | **CROSS_VENUE_MIXED** (excess not positive; one-asset sign; regimes concentrated) | **CROSS_VENUE_MIXED** (regimes concentrated) |
| Schema-5 profile | `evidence_73a008c2…`, tier **EXPLORATORY (unchanged)** | `evidence_124ab50f…`, tier **EXPLORATORY (unchanged)** |

Notes:

- **Excess versus absolute.** Excess is net return minus the same-asset, same-side
  random-entry baseline. The long's positive absolute net mean (+1.75%) reflects a rising
  market, not timing: random long entries did as well.
- **The short's excess is relative.** Its gross and net absolute means are negative
  (prices rose after signals on average) but less so than random short entries. Its
  support comes almost entirely from 2021-05 → 2023-01 (the 2022 bear market).

**Cross-venue comparison (side by side, never pooled).**

| | Long HL | Long Binance | Short HL | Short Binance |
|---|---|---|---|---|
| Excess (10d) | +1.38% | −0.34% | +2.61% | +1.08% |
| Events / assets | 77 / 6 | 142 / 5 | 39 / 6 | 86 / 5 |
| Positive-asset share | 67% | 60% | 67% | 80% |
| Breadth | broad | asset-specific | broad | broad |
| Walk-forward | consistent (3/4) | mixed (4/9) | consistent (3/4) | mixed (3/7) |
| Status | FULL_RESEARCH_CONSISTENT | CROSS_VENUE_MIXED | FULL_RESEARCH_MIXED | CROSS_VENUE_MIXED |

- **Long:** opposite sign (ratio −0.25). Per-asset signs agree on only 1 of 5 common
  assets (LINK). Hyperliquid on the common assets alone is +1.29%.
- **Short:** same sign, smaller magnitude (ratio 0.42). Per-asset signs agree on 5 of 5
  common assets (AAVE is negative on both). Hyperliquid on the common assets is +4.34%.
- **Walk-forward:** consistent on Hyperliquid's two years, mixed across Binance's five.

### Evidence integration and tiers

`market lab corroboration evidence <registration>` appends a **schema-5** profile:

- It `extends` the registered base: the Phase 9 schema-4 profile here, or a historical
  profile.
- It cites the base's sources plus a `cross_venue_corroboration` source.
- It copies every existing field and block unchanged and adds a `corroboration` block:
  - venue, period and assets;
  - exposure summary;
  - status, sample, effect and breadth;
  - walk-forward, regimes and neighbours;
  - comparison, policy and `independent: false`.
- A `cross_venue_corroboration` component appears in `components`, with one line in
  `supporting` (if CORROBORATIVE) or `limiting` (otherwise).

Existing profiles stay unchanged. All 42 pre-existing profiles are byte-identical, and
historical, Phase 9 and forward payloads omit the new block.

**Tiers never change:**

- The policy's `tier_effect` is the literal `"none"`, and the builder refuses a tier
  different from the base's.
- The schema-5 model rejects `VALIDATED` and any non-independent block.
- A `cross_venue_corroboration` source cannot satisfy anything that requires a
  `validation` source.
- Corroboration therefore can neither create RESEARCH_SUPPORTED nor unlock VALIDATED. The
  reason is that the data is historically exposed and not independent of the research
  that led to these strategies.

Phase 8 enrollment and Phase 9 registration refuse schema-5 profiles. The batch evidence
report excludes them, so its output is identical to before.

**Future consumers.** A future auto-trader promotion policy can read the block's
`status`: cross-venue supportive, mixed or adverse. No eligibility, approval or
readiness field is created here.

### Co-pilot compatibility

`copilot_policy` v1 (`copolicy_e235b9de…`) is unchanged. The co-pilot reads the newest
**schema-4** extension, so schema 5 is invisible to it. On the live DB, after the run:

- every `copilot_*` table (watchlist, status, runs, decisions, deliveries, policies,
  software) is byte-identical to the backup;
- the co-pilot `evidence_view` for both watches is identical;
- `lab_forward_*` and `lab_research_*` are byte-identical.

The only DB changes are migration 14 and:

- +1 plan;
- +2 schema-5 profiles;
- the corroboration registrations, runs and results;
- 7 corroboration experiments (2 targets + 5 neighbours), with their starts, results,
  inspections, dataset and snapshot blobs.

### Known limitations

- **Not independent.** About 80% of the period was used by legacy Binance research on a
  related trend rule before these strategies were defined. Which decisions it influenced
  is not recorded. A positive result could never have counted as validation; neither
  result here is positive.
- Different asset composition (no HYPE), different costs and funding levels, and USDT
  versus USDC quoting. Venue differences are confounded with regime differences: Binance
  covers 2019–2024, Hyperliquid 2024–2026.
- Regime blocks are equal calendar thirds, not economically defined regimes. The early
  block is warmup-limited (the short has only 6 events there).
- Daily perps and two strategies only. Neighbours are the registered Phase 6 neighbours.
  No other family member was examined.
- The Lab compiler's EMA warmup and the plan's 400-day warmup are reused unchanged.
  Assets listed late contribute fewer events.
- Recorded exposure is application-level. Direct market-table access is not observable.

## 17. Step 12 implemented: paper auto-trader

Package `src/market_signal/paper/` (`policy.py`, `execution.py`, `risk.py`, `account.py`,
`engine.py`, `render.py`), additive migration 15 (`paper_*` tables) and
`market lab paper …`. This is the **paper-only auto-trader foundation**: a stateful,
prospective, simulated perp account that behaves as if Prism controlled an account, under
frozen promotion, risk, execution and exit policies. It places **zero real orders**.

> The paper auto-trader is structurally incapable of placing real orders.

> Paper results are forward evidence. They do not automatically authorize real-money
> execution.

> Any future transition to real trading requires a separate explicit promotion phase after
> sufficient paper-forward evidence.

> Paper trading evaluates a trading-policy implementation built around the historical
> signal, not merely the raw signal's forward-return statistic.

### Purpose and architecture

Phase 8 asks: *did the raw signal's forward outcome persist?* Phase 12 asks: *did an actual
simulated trading system, with sizing, costs, funding, margin, risk limits and exits, make
sensible decisions and survive operational constraints?* Both observe the same governed
signals independently.

```
governed Lab signal + evidence (read only)
  -> autotrader_policy   (promotion: may this strategy generate paper intent?)
  -> paper_risk_policy   (account gates, sizing, exposure, conflicts, kill switches)
  -> strategy intent     (one per fired signal)
  -> PaperExecutionAdapter (simulated order lifecycle and fills; the ONLY adapter)
  -> paper account       (replay of the append-only event ledger)
  -> paper_execution evidence
```

A future live phase is meant to reuse the promotion policy, risk engine, intent logic,
account semantics and order lifecycle, replacing only the execution adapter and adding
much stronger safety controls. Nothing here was built as a throwaway simulator, and
nothing here can be switched to live.

| Module | Role |
|---|---|
| `policy.py` | Frozen, content-addressed policies: `AutotraderPolicy`, `RiskPolicy`, `ExecutionModel`, `ExitPolicy`, `PaperMaturityPolicy`, and the pure `evaluate_promotion` |
| `risk.py` | Pure `allocate`: every intent of one bar against one account snapshot |
| `execution.py` | `PaperOrder`, the order states, `PaperExecutionAdapter` (pure fills) and the `require_paper_adapter` guard |
| `account.py` | `AccountState`, `apply`/`replay`: the account is a fold of the event ledger |
| `engine.py` | Runs, the frozen daily cycle, evidence view, status changes, notifications and evidence summary |
| `render.py` | PAPER/SIMULATED Telegram texts |

### Inspection before building (what was reused, what was not)

- **`perps/backtest.py` `simulate_perps`** is not reused as an engine. It sizes from a stop,
  carries the last funding rate forward over missing days, and reads max leverage from the
  latest snapshot. Its pure `liquidation_price` formula *is* reused.
- **`perps/paper.py`** and **Phase 8** stay unchanged. Phase 8's live-window helpers
  (`live_snapshot`, `in_live_window`) and the Phase 3 compiler are reused for signals, so
  the paper trader sees exactly the signal the forward tracker and co-pilot see.
- **Strategy exit intent.** Both cohort strategies carry the `ma_trend` *family template*
  `ExitIntent` (3 × ATR stop, 30-bar hold). No Phase 4–11 evidence evaluated it: every
  result is the fixed 10-bar horizon (T+1 open → T+10 close). Paper v1 therefore does
  **not** use it (below).
- **Phase 9 audit items** that made portfolio simulation unsupported are resolved for the
  paper engine (the Phase 9 research adapter is unchanged and still reports portfolio
  simulation as unsupported for *historical* research):

  | Phase 9 concern | Paper engine |
  |---|---|
  | Risk settings not frozen in a governed policy | `paper_risk_policy` v1 is a content-hashed policy; any change is a new policy and a new run |
  | Max leverage not point-in-time | leverage and per-asset maintenance rates are frozen constants in the policy; `perp_snapshots` is never read |
  | Cross-symbol ordering | every intent of a bar is decided against the same snapshot; capacity is allocated by a hash lottery, notional by one proportional factor (below) |

### Safety boundary

There is no path in this phase that can place a real exchange order.

- The `paper` package imports no network client (`httpx`, `requests`, `urllib`, `socket`, …),
  no provider/registry/updater module, no exchange SDK and no Telegram client. It reads no
  environment variable or secret (all tested by an AST scan).
- Prism contains no order-placement code at all. It talks to Hyperliquid's public `/info`
  endpoint only. A repo-wide test fails on `/exchange`, `ccxt`, `eth_account`,
  `place_order`/`create_order`/`submit_order`, `api_secret`, and any
  `READY_FOR_LIVE`/`APPROVED_LIVE`/`ENABLE_LIVE` string.
- `PaperExecutionAdapter` is the only adapter. `transmits_orders` is the literal `False`,
  fills are arithmetic on stored bars, and its public surface is exactly `mode`,
  `transmits_orders`, `model`, `validate`, `fill`. The engine's `require_paper_adapter`
  refuses any other type, including a subclass or a look-alike object.
- Every policy and run has `mode: "paper"` as a literal. The DB CHECKs `paper_runs.mode =
  'paper'`.
- The CLI has no `--live`, `--real`, `--execute`, key or secret option, and no command for
  real execution (tested over the whole command tree).
- A full engine cycle runs with sockets disabled (tested).
- Run statuses are `ACTIVE`, `PAUSED`, `STOPPED`, `KILLED`. No status, field or
  transition expresses live readiness.

### Promotion policy `autotrader_policy` v1 (`appolicy_1f0b69cf…`)

The question it answers: *is this strategy allowed to generate **paper** trading intent?* It
is separate from `copilot_policy` (different ID and content, no shared code path) and
stricter. It is evaluated for every fired signal at decision time, on the evidence that
exists then (the evidence chain is point-in-time by `recorded_at`).

**Blocking rules** (every one must pass):

| Rule | v1 condition |
|---|---|
| `strategy_enrolled` | the strategy is in the run's frozen, manually chosen cohort |
| `versions_compatible` | compiler/vocabulary versions equal the run's and every evidence source's |
| `forward_tracking_active` | the member's frozen Phase 8 tracking is active (a stopped/retired strategy cannot trade) |
| `evidence_available` | a usable profile chain exists |
| `tier_eligible` | `EXPLORATORY` or `RESEARCH_SUPPORTED` |
| `sample_adequate` | ≥ 30 independent primary-horizon events on ≥ 3 assets |
| `effect_positive` | expected-direction net excess > 0 |
| `breadth_ok` | not dominated by one asset; largest asset share ≤ 50% |
| `not_isolated_spike` | parameter neighbourhood is not an isolated spike |
| `full_research_adequate` | full research **was run** and is `CONSISTENT` or `MIXED` (`INCONSISTENT`, `INSUFFICIENT`, `ERROR` or not run block) |
| `validation_registered` | a Phase 9 validation registration exists |
| `validation_not_adverse` | not `VALIDATION_ADVERSE` or `VALIDATION_ERROR` |
| `corroboration_not_adverse` | Phase 11 status is not `CROSS_VENUE_ADVERSE` |
| `forward_not_adverse` | forward maturity `DEVELOPING`/`MATURE` with excess ≤ 0 or opposite direction blocks |

**Not required** (recorded as caveats): FDR survival, supportive validation, cross-venue
corroboration (`MIXED`/`INSUFFICIENT`/not run), forward maturity, a plateau
neighbourhood. Final validation is deliberately not required: paper trading is how
stronger forward evidence is gathered. The corroboration choice is frozen: the Binance
data is historically exposed, so it may only veto (`ADVERSE`), never qualify.

Compared with `copilot_policy` v1, this policy additionally requires full research to have
been run, a validation registration and an active forward tracking. It also blocks adverse
corroboration and adverse forward evidence at `DEVELOPING` rather than only `MATURE`.

### Risk policy `paper_risk_policy` v1 (`riskpolicy_e755fb81…`)

All values are **arbitrary conservative research defaults**, fixed before any paper result
existed. They are not optimised and must not be tuned on paper outcomes. Changing any of
them is a new policy and a new paper run.

| Setting | v1 |
|---|---|
| Starting equity | 10,000 USDC (a research account, unrelated to any real portfolio) |
| Sizing | `fixed_equity_fraction_v1`: notional = 20% of current marked equity |
| Minimum position (after scaling) | 5% of equity |
| Margin | isolated, fixed 2× leverage (cap 2×), so margin = 10% of equity per position |
| Max open positions (incl. unfilled entry orders) | 3 |
| Max gross notional | 60% of equity |
| Max per asset | 20% of equity (so one position per asset) |
| Max per strategy | 40% of equity |
| Cash reserve after margin + fees | 25% of equity |
| Daily loss halt | bar-to-bar marked equity loss ≥ 3% → no new entries on that bar |
| Drawdown kill | ≥ 15% below peak marked equity → run `KILLED` |
| Repeated errors | 3 consecutive failed cycles → automatic `PAUSED` |
| Signal data gap | a gap > 24 h between daily bars in the signal lookback blocks the entry |
| Maintenance rates | 1 / (2 × venue max): BTC 1.25%, ETH 2%, SOL 2.5%, HYPE/LINK/AAVE 5% (frozen constants) |
| Liquidation model | `isolated_full_margin_loss_v1` |
| Conflicts | `one_position_per_asset_no_hedge_no_add_v1` |
| Allocation | `same_snapshot_hash_lottery_proportional_v1` |

**Why fixed-fraction, not stop-based risk.** The strategies carry no stop that their
evidence evaluated. Inventing an ATR stop just to size positions would make the paper
result depend on an untested component. There is no Kelly or volatility targeting. The
per-trade loss is therefore bounded by notional, isolated margin and the kill switches,
not by a stop.

### Account model

- A paper run has a frozen `PaperRunDefinition`: cohort, the five policy IDs, engine
  version, the frozen cycle order, semantics and `created_at`. Its `run_id` is the content
  hash. `continues` records lineage to a stopped/killed run. A new policy means a new
  run; old runs are never overwritten.
- Each cohort member is frozen from the strategy's **active Phase 8 tracking definition**:
  baseline profile and tier, assets, venue, lookback, primary horizon and tracking ID.
- Statuses are `ACTIVE`, `PAUSED` (no new entries), `STOPPED` (terminal, manual) and
  `KILLED` (terminal, automatic). In every status, open positions are still managed to
  their scheduled exits.
- At most one non-terminal run exists at a time.
- **State** is always `replay(paper_events)`. It holds cash, open positions (entry
  reference/fill, side, units, notional, isolated margin and margin balance, accrued
  funding, fees, last mark), unfilled orders, closed trades, daily marks, peak equity and
  halted bars. Every account mark records equity, drawdown, day PnL, gross/per-asset/
  per-strategy exposure and unrealised PnL. There is no mutable "current position" table.

### Execution model `paper_execution_model` v1 (simulated fills)

| | v1 |
|---|---|
| Venue / order type | Hyperliquid perp, market orders |
| Entry reference | **the stored open of bar T+1** (the bar opening at T's close) |
| Exit reference | the stored close of the scheduled exit bar |
| Slippage | reference ± frozen per-asset slippage, always against the order (BTC/ETH 2, SOL 4, HYPE/LINK 6, AAVE 8 bps) |
| Fee | 4.5 bps taker on filled notional |
| Fills | `full_immediate_v1`: full, immediate. No partial fills, queue or order book |
| Entry window | 12 h after T's close |
| Funding | settled hourly rates, priced at the bar close (below) |

Costs are frozen from the cohort's research plan, so paper costs equal the costs the
evidence assumed. High/low are never used to produce a favourable fill. They are used only
for the adverse liquidation check.

Each fill records the reference price, slippage, fill price, units, notional, fee and
execution-model ID.

**Entry timing.** A daily signal at bar T is computed only from data available at T's
close (bars with `close_time ≤ T`, funding to the minute, Phase 3 compiler). The intent is
recorded **within 12 hours of T's close**. Its market order fills at T+1's open once that
bar is stored: Prism stores only completed bars, so the fill is recorded the next day.
It never fills at T's close; in live data T+1's open differs from T's close by up to
~50 bps.

**Missed execution window.** If the engine first sees bar T more than 12 hours after it
closed (PC off, data late), the signal and intent are still recorded, and the risk
decision is `REJECTED: missed_execution_window`. No order is created, and nothing ever fills
retrospectively. With Prism's schedule (first run ~07:20 UK, retries 12:00 and 19:30 UK),
the morning and midday runs are inside the window and the evening run is not.

**Order lifecycle.** `SUBMITTED → FILLED | REJECTED | EXPIRED | CANCELLED`. `PARTIALLY_FILLED`
exists in the vocabulary, but the v1 fill model never produces it. Exits use the same
lifecycle as reduce-only orders. An entry order whose fill bar is still not stored 12 h
after it closed `EXPIRES`.

### Exit policy `paper_exit_policy` v1 (`exitpolicy_4ef7a1b2…`)

`fixed_horizon_v1`: a position entered at T+1's open closes at **T+10's close** (the
primary research horizon, 10 bars). This reproduces the evidence's return window exactly.

- There is no protective stop, no take-profit and no opposite-signal exit
  (`uses_strategy_exit_intent: false`).
- The only other way a position ends is a simulated liquidation (risk policy).
- Run creation refuses a cohort member whose primary horizon differs from the exit
  policy's.
- A time exit processed late (the engine was off) still fills at the scheduled close. Its
  time was fixed in advance and does not depend on prices. The trade is flagged
  `late_processing` and counted as a late exit in the evidence.

### Funding

For every open position and every processed bar B, the engine reads the funding
settlements **actually stored** in (B − 1 day, B] (minute-snapped) and records one
immutable `funding_accrued` event:

- amount = Σ side × units × close(B) × rate (the research convention); a positive amount
  is paid (longs pay positive funding), a negative amount is received;
- the event lists every settlement, `present` versus `expected` (24) and `missing`.

Funding is debited from or credited to the position's isolated margin balance, which moves
its liquidation price. Nothing is estimated: a missing settlement is counted, never filled
in, and it appears in the evidence as `funding_missing_settlements`. Event keys are per
position and bar, so funding can never be charged twice.

### Margin, leverage and liquidation

- Isolated margin per position: margin = notional / 2. The margin and entry fee leave cash
  at the fill.
- Liquidation price: `perps.backtest.liquidation_price` with the frozen maintenance rate
  and the current margin balance. At entry it is about −47% to −49% for a
  long and +43% to +48% for a short.
- Each bar, if the adverse extreme (low for longs, high for shorts) reaches the liquidation
  price, the position is liquidated at that price, or at the open if the bar gapped
  through it. If funding at the close exhausts the margin, it is liquidated at the close.
- A liquidation loses the **whole remaining isolated margin** (proceeds 0). It records the
  reference and liquidation prices, the margin lost and any theoretical loss beyond the
  margin. Losses are not clamped silently: beyond-margin loss is recorded but not charged,
  because isolated margin caps the account's loss at the posted margin.
- This is an **approximation, not Hyperliquid liquidation parity**: there are no tiered
  margin tables, no partial liquidation, no liquidation fee and no backstop model. The v1
  sizing (2×, 20% notional) keeps normal operation far from liquidation.

### Conflicts and simultaneous signals

Frozen rules:

- **One position per asset.** No hedged long and short on one asset.
- **An existing position or unfilled order on the asset** blocks a new entry: the same side
  is rejected as `duplicate_position_no_add` (size is never added), the other side as
  `conflicting_position`.
- **Same-bar long and short on one asset** reject each other
  (`conflicting_signals_same_bar`).
- **Several same-side strategies on one asset** keep one (the lottery winner); the others
  are rejected as `duplicate_signal_same_bar`.

All intents of one bar close are decided together against **the same pre-trade snapshot**:
the account marked at that close, after exits.

1. **Gates:** account ACTIVE, no daily-loss halt, signal inside the entry window, promotion
   eligible, data contiguous, no conflict.
2. **Capacity in lottery order:** free position slots, then per-strategy and per-asset
   headroom. The lottery key is `SHA-256(run, bar, strategy, symbol)`: deterministic and
   reproducible, unrelated to symbol names, input order or past profitability. Over 200
   synthetic bars with 6 simultaneous signals and 3 slots, each asset won 60–140 times
   (tested).
3. **Notional:** the selected intents all request 20% of snapshot equity. If gross headroom
   or free cash (after the 25% reserve) cannot fund them, **all are scaled by one common
   factor**. If that leaves a position below 5% of equity, the last in lottery order is
   dropped and the factor recomputed.

Every rejection is recorded with all its reasons.

### Daily cycle (`paper_engine_v1`, frozen in every run)

For each bar close B after the last processed one, in time order:

1. **validate_bar_data:** held assets need B's bar and funding through B, otherwise the
   cycle waits (never invents). Signal assets that are merely lagging (yesterday's bar
   exists) are waited for while the entry window is open. A stale asset (missing for more
   than one bar) or a late bar proceeds without them, with a `data_issue` event.
2. **fill_entry_orders_at_open:** orders submitted after B − 1 fill at B's open.
3. **liquidation_check_on_bar_range**
4. **accrue_settled_funding**
5. **mark_at_close**
6. **scheduled_exits_at_close**
7. **account_state_and_kill_switches:** equity, drawdown and day PnL go into the
   `account_mark`. Then impossible state → KILLED, drawdown ≥ 15% → KILLED, and day loss
   ≥ 3% → halt for this bar.
8. **read_signals:** Phase 3 compiler on the Phase 8 live snapshot for every cohort
   strategy and asset. A `signals_evaluated` event records every state (SIGNAL, NO_SIGNAL,
   INELIGIBLE_BAR, DATA_MISSING, ERROR); fired signals add `signal_consumed`.
9. **intents_promotion_and_risk:** `intent_created` (with the promotion decision and
   evidence view), then one `risk_decision` per intent from `allocate`.
10. **submit_entry_orders:** these fill at B + 1's open, and the exit bar is B + 10.
11. **persist_then_notify:** all of the cycle's events and its `paper_cycles` row commit in
    one transaction. Telegram is attempted only after the commit.

Only bars closing **strictly after the run's `created_at`** are ever processed. Terminal,
flat runs stop processing.

### Immutable event ledger

`paper_events` is append-only, with `UNIQUE (run_id, seq)` and `UNIQUE (run_id,
event_key)`. Event IDs are content hashes of `(run, key)`, and keys are deterministic, for
example `funding:<position>:<bar>`, `filled:<order>` or `mark:<bar>`. A DB CHECK refuses
any bar event whose market time is not after the run's creation.

Event types: `run_created`, `status_changed`, `signals_evaluated`, `signal_consumed`,
`intent_created`, `risk_decision`, `order_submitted`, `order_filled`, `order_rejected`,
`order_expired`, `order_cancelled`, `position_opened`, `funding_accrued`, `liquidation`,
`exit_intent`, `position_closed`, `account_mark`, `kill_switch`, `data_issue`.

Other tables:

| Table | Contents |
|---|---|
| `paper_runs` | frozen definitions |
| `paper_policies` | released policy payloads by ID and kind |
| `paper_cycles` | every engine pass, ok or error with traceback |
| `paper_notifications` | delivery attempts |
| `paper_evidence` | content-addressed `paper_execution` summaries |
| `paper_software` | software identities |

**Crash and restart.** A cycle is computed in memory and committed in one transaction. A
crash therefore leaves nothing partial, and the next run recomputes the same bars. Re-runs
are no-ops: a processed bar is never processed again, and event keys make a duplicate fill,
funding charge, exit or position impossible. The state survives WSL/PC restarts because it
*is* the database. Before committing, the engine checks that the cycle's in-memory state
equals the replay of the old plus new events. Rehearsed with a real `SIGKILL` mid-cycle (see
the verification section).

**Manual intervention.** `pause`, `resume` and `stop` append `status_changed` events
with a reason. Historical fills and PnL cannot be edited; there is no command that does.
There is no manual close in v1: positions are short-lived, so a stop simply lets them reach
their scheduled exits. A future manual close must be recorded as `manual paper close`.

### Kill switches (paper mode)

| Switch | Effect |
|---|---|
| Manual pause / stop | no new entries; positions managed |
| Daily loss halt (≥ 3% bar-to-bar) | no entries on that bar |
| Drawdown kill (≥ 15% from peak) | run `KILLED` (terminal) |
| Stale/missing data | entries blocked for affected assets or bars; held positions wait for data |
| Repeated errors (3 consecutive failed cycles) | automatic `PAUSED` |
| Impossible account state (negative cash/margin, equity ≤ 0, replay contradiction) | `KILLED`, or the cycle fails and is recorded |

No live-execution kill switch exists, because live execution does not.

### Data outages

- Prices and funding are never invented.
- A held asset without B's bar or funding stops the account at B. The bar is not processed,
  exits and marks wait, and once the entry window passes a single `data_issue` records the
  unresolved state.
- A lagging signal asset delays the bar inside the entry window only.
- A stale asset (missing for more than one bar) no longer blocks the other assets. Its
  signals are `DATA_MISSING`.
- Signals first evaluated after the window are rejected `missed_execution_window`.
- Missing funding settlements are counted on the funding event.

### Relationship to Phase 8, the co-pilot and Phase 11

| | Phase 8 forward | Co-pilot | Paper auto-trader |
|---|---|---|---|
| Question | did the raw signal's outcome persist? | is this worth a human look? | did the simulated trading system behave sensibly? |
| Writes | `lab_forward_*` | `copilot_*` | `paper_*` only |
| Depends on the others? | no | reads forward evidence | reads forward evidence and tracking status; **not** co-pilot alerts or Telegram delivery |

- A signal can produce a co-pilot alert, a paper intent and a Phase 8 evaluation. Each
  consumer records it independently.
- A paper trade never causes a co-pilot alert, and Telegram failures cannot change a paper
  decision.
- `paper` imports nothing from `copilot`, and neither `copilot` nor `research/` imports
  `paper` (tested).
- After paper cycles, every `lab_*` and `copilot_*` table is byte-identical (tested), so
  paper results can never change a Lab tier.
- Phase 11 corroboration is shown in the evidence view and used only by the frozen
  `corroboration_not_adverse` rule. It never alters a strategy definition.

### Paper evidence (`paper_execution`) and maturity

`market lab paper summary` (read-only) and `market lab paper evidence` (appends a
content-addressed summary to `paper_evidence`) compute everything from the ledger:

- trades, win rate, gross PnL, slippage cost, fees, funding, net PnL, liquidations, late
  exits;
- return on starting equity, max drawdown, average/max exposure, unrealised PnL;
- signals, intents, accepted, rejections by reason, orders filled/expired/rejected;
- average holding period, and contribution by strategy and by asset;
- missing funding settlements and data issues;
- `reconciles_with_ledger`: starting equity + Σ closed net + unrealised = marked equity.

The stage is named `paper_execution`, distinct from Phase 8's `paper_forward`. It is
descriptive only: no p-value or significance claim is computed. It never writes
`lab_evidence_profiles`.

**Maturity** (`paper_maturity` v1) needs closed trades **and** observed days. It describes
sample size, never profitability.

| Level | Rule |
|---|---|
| `WARMUP` | otherwise |
| `EARLY` | ≥ 10 closed trades and ≥ 30 observed days |
| `DEVELOPING` | ≥ 30 and ≥ 90 |
| `MATURE` | ≥ 100 and ≥ 365 |

### Telegram (PAPER notifications)

Few messages, all starting `🧪 PAPER · SIMULATED — no real order was placed` and ending
`Research account only. Not an instruction to trade.`:

- paper position opened (sent when the T+1 open fill is recorded, i.e. the following day);
- paper position closed (time exit or SIMULATED LIQUIDATION);
- kill switch (daily loss halt, drawdown kill, impossible state);
- automatic pause after repeated errors;
- the start of an engine error streak (once per streak).

There are no BUY/SELL words and no co-pilot wording. More than 4 pending messages are sent
as one digest. Delivery follows the co-pilot's pattern: `attempted`, then `sent` or
`failed`. A failure is retried on later runs for 24 h, and an attempt with an unknown
outcome is never resent. Delivery is attempted only after the ledger commit and never
affects trading.

### CLI

| Command | |
|---|---|
| `market lab paper policy` | the released promotion, risk, exit and maturity policies with IDs |
| `market lab paper create <strategy>… --reason … [--label] [--continues RUN] [--dry-run] [--promotion-version/--risk-version/--exit-version N]` | WRITE: create a run (clock starts now) |
| `market lab paper run [--dry-run [--full]] [--now ISO (dry-run only)]` | WRITE: process new bars of every open run (or one holding positions), then notify |
| `market lab paper status [RUN]` / `positions` / `trades` / `events [--type] [--limit]` | read-only views (default: the newest run) |
| `market lab paper pause/resume/stop RUN --reason …` | WRITE: status events |
| `market lab paper summary [RUN]` / `evidence [RUN]` | `paper_execution` evidence (read-only / append) |

### Scheduling

There is no new scheduler. `forward_run.sh` is already run by the installed **"Prism forward"**
task at log-on (+5 min) and at 12:00 and 19:30. It now runs:

1. `market lab forward run`: perp update → Phase 8 check → resolve;
2. `market lab copilot run --no-update`;
3. `market lab paper run`.

The paper cycle runs last because it needs the freshly ingested bar and funding. It reads
forward tracking state, and it must not depend on the co-pilot's Telegram outcome. The log
line is `=== done forward=… copilot=… paper=…`. The wrapper exits with the first non-zero
code in that order. All three commands are idempotent.

### Live setup (2026-10-04)

The DB was backed up first to `data/prism.pre_phase12.duckdb`. Migration 15 was applied on
the next writable open.

**Rehearsals (scratch copies only, never evidence):**

- *Creation:* dry-run create, create, dry-run cycle, two real cycles (idempotent), and a
  refused second open run.
- *Mechanics replay:* a back-dated account replayed day by day over 2026-06 → 2026-10 on real
  Hyperliquid bars and hourly funding, with a scratch-only promotion variant (real tracking
  only began 2026-10-04). 20 signals produced:
  - 11 trades: every one held exactly 10 bars, with real funding (longs paid, shorts
    received);
  - rejections: 5 `conflicting_position`, 3 `max_strategy_allocation`, 1
    `max_open_positions`;
  - a reconciling ledger, and each retry run wrote nothing.

  Its PnL is not evidence and is not reported.
- *Crash:* a 60-day catch-up cycle was killed with `SIGKILL` mid-computation. Nothing was
  written. The recovered run was then identical, in all 311 event keys, types and payloads,
  to the same schedule without the kill. The catch-up recorded its 20 signals as
  `missed_execution_window` with **0** positions: no retrospective fills.

**Dry run against current data (live DB):**

- Both members are `PAPER_ELIGIBLE` under `autotrader_policy` v1. Their evidence chain
  tips are the schema-5 profiles `evidence_73a008c2…` (long) and `evidence_124ab50f…`
  (short). Caveats:
  - both: q 0.91 (no FDR survival), validation insufficient, cross-venue corroboration
    mixed, forward evidence too early;
  - short only: full research mixed.
- **Newest bar (2026-10-04 00:00 UTC):** no new signal on any of the 12 strategy/asset pairs.
  - Long conditions hold on all six assets, but the edge fired earlier (continuation or
    cooldown). The last long signals were in August.
  - Short conditions hold on none.
  - The bar also closed before creation, so it can never be processed.
- **Illustration (inspection only):** if all six longs fired on the first bar of the fresh
  account, the lottery would accept AAVE and ETH at 2,000 USDC each, and the 40%
  per-strategy cap would reject the other four.
- A cycle ran with sockets disabled. Every `lab_*` and `copilot_*` table (40 tables, 492
  rows) was byte-identical to the backup after creation and the first cycles.

**The paper run:**

| | |
|---|---|
| Run | `paperrun_27b0a336e707c389a3fb574cd9d09a7800b563f0691612bf4fb4f1fd97029279` |
| Created (paper clock start) | 2026-10-04 14:59:39 UTC |
| First processable bar | 2026-10-05 00:00 UTC (no earlier signal can ever trade) |
| Starting equity | 10,000 USDC |
| Cohort | `ma_trend_10_50_long` (`tracking_250f844a…`), `ma_trend_20_100_short` (`tracking_65febaad…`), both unchanged |
| Promotion / risk / execution / exit / maturity | `appolicy_1f0b69cf…` / `riskpolicy_e755fb81…` / `execmodel_3a94bdf5…` / `exitpolicy_4ef7a1b2…` / `papermaturity_0b19dee3…` |
| Status at activation | ACTIVE, flat (all cash), 0 positions, 0 orders; **no trade fired at activation** |

No Donchian or other variant was added, and the strategies were not changed after the
Phase 11 MIXED corroboration. A wrapper run (`forward_run.sh`) logged
`forward=0 copilot=0 paper=0`.

### Known limitations

- **Daily cadence on a home PC.** Entries need a run within 12 h of the 00:00 UTC close.
  A day the PC is off is a skipped entry (recorded), never a late fill. Exits processed
  late still use the scheduled close and are flagged.
- **Entry notifications lag a day.** Only completed bars are stored, so the T+1 open fill
  is recorded when T+1 completes.
- **Simplistic v1 execution:** full immediate fills at stored open/close ± frozen
  slippage. There is no order book, queue, partial fill, latency model, venue size
  increment or minimum notional, and fractional units are allowed.
- **Approximate margin:** isolated margin with frozen maintenance rates, not Hyperliquid's
  tiered tables. A liquidation loses the whole isolated margin, and there is no liquidation
  parity. Funding is priced at the bar close, not at each settlement's oracle price.
- **No stop.** The v1 exit is time-only, by design (above). The tail loss per position is
  bounded by its isolated margin (10% of equity), and the kill switches act only at
  daily marks.
- **Small, slow sample.** At most 3 positions with 10-bar holds, from two strategies that
  fire a few times a month in total. Maturity will take a long time, and early results
  are noise.
- Promotion decisions read the evidence as recorded at decision time. A later Lab record
  never rewrites an earlier decision.
- Telegram reuses the existing bot. Delivery problems are recorded but cannot affect the
  account.

## 18. Step 13 implemented: paper observability

Module `src/market_signal/paper/observe.py`, additive migration 16 (`paper_snapshots`,
`paper_briefs`), new and reworked `market lab paper …` views, and a top-level
`market status`. The question this phase answers: *is the paper trader healthy, what is it
doing, what has it missed, and how is the account evolving?*

> Phase 13 is read-only with respect to paper trading policy and execution. It observes the
> frozen Phase 12 experiment and does not adapt it.

> Operational misses are distinguished from deliberate policy or risk rejections.

No promotion, risk, exit, execution or maturity policy, no event semantics, sizing,
cohort or signal rule changed. The v1 policy IDs are pinned in a test. There is no feedback
loop: nothing reallocates, loosens a window or retunes a limit from results. The only
engine change is an optional `alerts` flag on `run_all` (default on, behaviour unchanged),
exposed as `market lab paper run --no-notify`.

### One source of truth

Every number is derived from the run's append-only `paper_events` (replayed with
`account.replay`) and its recorded `paper_cycles` and `paper_notifications`. Nothing is
cached or kept in a second account table. `account_summary` reports
`reconciles_with_ledger`: starting equity + realised + unrealised = marked equity.

### Views

| Command | Shows |
|---|---|
| `market lab paper status [--json]` | Everything important on one screen (detailed below the table) |
| `positions` | Per open position: asset, side, strategy, entry time, entry reference → simulated fill, current mark (last processed close), units, notional, leverage, margin committed and balance, unrealised PnL, funding to date, entry fee, **approximate** liquidation level and distance (model `isolated_full_margin_loss_v1`, not Hyperliquid parity), scheduled exit bar, bars remaining |
| `risk` | Headroom against the frozen limits: positions `n / 3`, gross exposure `x% / 60%`, per asset `/ 20%`, per strategy `/ 40%`, free cash vs the 25% reserve, last bar return vs the −3% halt, drawdown `/ 15%` kill threshold, failed-cycle streak `/ 3`, whether new entries are permitted, free slots, near-liquidation assets. No risk score |
| `trades` | Closed trades: strategy, asset, side, signal bar, entry/exit time, fills, bars held, gross, fees, funding, slippage, net, return on margin, exit reason, operational issues |
| `intents [--skipped]` | Every signal acted on: promotion result, risk result, disposition, category, reason |
| `gaps` | The operational gap audit plus notification lag |
| `contributions` | Descriptive per-strategy and per-asset totals (below) |
| `equity` | Daily equity curve: equity, peak, drawdown, exposure at each recorded close. No intraday values are inferred, and no plotting dependency was added |
| `snapshot` / `brief [--record] [--send]` | Snapshots and the daily brief (below) |
| `runs` | The Phase 12 JSON listing of all runs (`status` previously printed this) |

`status` shows on one screen:

- account: status and health, age, start/current equity, cash, free cash,
  realised/unrealised/net PnL, return, peak, drawdown and max drawdown, gross exposure,
  margin in use, open/pending/closed counts, maturity, observed days;
- risk headroom;
- coverage;
- component health;
- open positions;
- intent dispositions.

Every view has `--json`. Without a run ID, a view uses the open run, else the newest one;
observation never creates a run.

### Expected skip versus missed execution

Every intent gets one disposition:

| Disposition | Meaning |
|---|---|
| `ENTERED` | the entry order filled at T+1's open |
| `PENDING_FILL` | the order was submitted; T+1 has not completed yet |
| `EXPECTED_SKIP` | the system deliberately did not enter |
| `MISSED_EXECUTION` | the system would have traded, but operational timing or data prevented it |

Categories:

- **Expected skip** (deliberate):
  - `promotion_rejection`;
  - `risk_rejection`: position, strategy, asset or headroom limits, or the daily-loss halt;
  - `conflicting_position`: duplicate, conflict, or a same-bar conflict;
  - `account_paused_or_killed`.
- **Missed execution** (operational):
  - `offline_gap`: no paper cycle at all inside the 12 h entry window, e.g. PC off;
  - `data_late`: cycles ran inside the window but waited for a lagging asset;
  - `engine_error`: every cycle inside the window failed;
  - `stale_or_missing_data`: gapped signal data, or an entry order expired because its
    fill bar never arrived;
  - `execution_error`: an order rejected at fill.

A policy or risk rejection is never counted as an operational miss.

**Mixed reasons.** If any of an intent's reasons is deliberate, it is an expected skip, and
the operational reasons are listed alongside.

**Counterfactual.** An intent rejected *only* for operational reasons is re-decided once,
read-only:

- the run's frozen risk policy and execution model are applied to **the same pre-trade
  snapshot** the engine used, rebuilt by replaying the ledger up to that bar's decisions;
- timing and data are assumed fine;
- if the counterfactual accepts the intent, it is `MISSED_EXECUTION`; otherwise it is an
  `EXPECTED_SKIP` ("would have been rejected anyway").

No hypothetical fill, price or PnL is ever constructed for a missed trade.

### Coverage and the gap audit

Expected bars are every daily close after the run's creation that has already happened.
For each bar, the status comes only from recorded marks and cycles:

| Status | Meaning |
|---|---|
| `ON_TIME` | processed within 12 h of its close |
| `LATE` | processed after the window. The cause is `offline_gap`, `data_late` or `engine_error`, from the cycles recorded inside the window |
| `PENDING` | not processed; the window is still open |
| `UNPROCESSED` | not processed; the window has passed |

Coverage also reports:

- cycle counts: total, ok, error, consecutive ok;
- the last ok cycle and the last cycle;
- calendar days without any cycle (honest PC-off days);
- the next bar and its entry window.

`gaps` lists each late or unprocessed bar with:

- the cycles inside its window;
- the signals found once it was processed;
- each intent's disposition, and how many trades became impossible;
- the positions held through the gap;
- whether marks and funding were recovered later, and how many settlements are missing;
- data-issue events;
- what is permanently unknowable. Signals on a bar stay unknown until it is processed,
  and fills that were never placed are not reconstructed.

**Notification lag** (from Phase 12, not "fixed" here). `gaps` and `trades` report, for
each opened/closed position:

- the execution time the simulated fill represents (T+1's open, or the exit close);
- when it was recorded;
- when Telegram delivered it.

Opens are recorded about a day after the fill they represent, because only completed bars
are stored.

### Health

| State | When |
|---|---|
| `KILLED` / `STOPPED` / `PAUSED` | from the run's status |
| `STALE` | a bar is past its entry window and still unprocessed |
| `DEGRADED` | any of: a failed-cycle streak; a recent (last 7 bars) bar processed late; a recent data issue; recently missing funding settlements; a position within 20% of its modelled liquidation level (display threshold only; the position is never touched) |
| `HEALTHY` | otherwise |

Failed or unknown Telegram deliveries are reported next to health. They never make the
account unhealthy, because they cannot affect trading.

Component freshness is read with SQL only, and `observe` imports nothing from the co-pilot.
A component is `current` if it ran within 36 h:

- newest Hyperliquid daily bar;
- last forward check;
- last co-pilot run.

### Contributions (descriptive only)

**Per strategy:**

- signals, intents, entered;
- closed trades, wins/losses, net PnL, fees, funding;
- expected skips, missed executions, open positions.

**Per asset:**

- closed trades, net PnL, fees, funding;
- average exposure (mean of daily per-asset notional / equity);
- missed executions, and whether a position is open.

These are totals, not a ranking: nothing is labelled best, and nothing is disabled.

### Snapshots (`paper_snapshot_v1`)

An immutable `paper_execution` snapshot is keyed by `(run, ledger sequence, version)`. It
holds:

- status, observed days;
- closed and open trades, wins/losses;
- gross, realised, unrealised and net PnL; fees, funding, slippage; equity;
- return on starting equity, max drawdown, average exposure;
- signals and intents; entered, pending, expected skips and missed executions, each by
  category;
- strategy and asset contributions;
- maturity, ledger reconciliation, the events digest and the policy IDs.

It is ledger-derived only, so it can be reproduced exactly from `events[:as_of_seq]`
(tested). Recording again at the same sequence returns the stored row unchanged; nothing
is recomputed in place.

**Cadence:** the scheduled wrapper takes one snapshot after every paper cycle that appended
events, so at least one per completed paper day. `market lab paper snapshot` takes one by
hand. Snapshots are descriptive and change no eligibility or policy.

### Daily brief

`market lab paper brief` prints the compact brief, read-only:

- **PAPER ACCOUNT:** equity, PnL, drawdown vs kill, positions vs limit, gross exposure vs
  limit;
- **TODAY:** signals, opened, closed, orders submitted, rejected, missed, for the last
  completed paper day;
- **POSITIONS** (when any exist): one line each, with uPnL, exit date, bars left and
  approximate liquidation distance;
- **HEALTH:** paper engine state, last cycle, data, forward tracking, co-pilot, maturity.

It is labelled `PAPER · SIMULATED — no real order was placed` and ends with the research
footer.

`--record` takes a snapshot, then stores the brief **once per completed paper day**
(`paper_briefs`, unique per run, bar and version). A stored brief is never rebuilt.
Before the first completed day, nothing is stored.

### Telegram brief

The daily brief is **enabled**: the wrapper calls `brief --send`.

- One message per completed paper day, sent only after the paper cycle has committed and
  the snapshot is taken.
- Delivery uses the Phase 12 pattern in `paper_notifications`: `attempted`, then `sent` or
  `failed`. A failure is retried on later wrapper runs for 24 h. An attempt of unknown
  outcome is never resent. Delivery can never affect the account.
- It can be toggled independently of position alerts:
  - remove `--send` from `forward_run.sh` for a CLI-only brief;
  - use `market lab paper run --no-notify` to silence open/close/kill alerts.

### `market status`

One read-only table with five components:

| Component | Source |
|---|---|
| Data | the existing `check_freshness` |
| OI | the existing `oi_coverage` |
| Forward tracker | last check, active trackings, newest evaluated bar |
| Co-pilot | last run, active watches, alerts |
| Paper trader | health, equity, return, positions, maturity |

`market doctor` is unchanged and remains the detailed diagnostic.

### Scheduling

`forward_run.sh` (the installed "Prism forward" task) now runs:

1. forward run;
2. co-pilot;
3. `market lab paper run`;
4. `market lab paper brief --send`.

The log line is `=== done forward=… copilot=… paper=… brief=…`. The brief step only
reads committed state and writes observability rows.

### Live application (2026-10-04)

The DB was backed up first to `data/prism.pre_phase13.duckdb`, and migration 16 was
applied. Observability is attached to the **existing** run
`paperrun_27b0a336…`: no new run, no restarted clock, and the same balance and policies.

- **First governed snapshot:** `papersnap_ce392529…` (sequence 1, before any processed bar).
  It records exactly what exists: equity 10,000.00 USDC, 0 positions, 0 trades, 0 signals,
  0 intents, 0 skips, 0 missed executions, `WARMUP`.
- **Daily brief:** none stored, because no paper day has completed yet. The first is due
  after the 2026-10-05 00:00 UTC bar is processed.
- **Health:** `HEALTHY`. `market status` shows Data, OI, Forward tracker, Co-pilot and
  Paper trader all OK.
- **Immutability:** after migration, smoke tests, the snapshot and a wrapper run
  (`forward=0 copilot=0 paper=0 brief=0`), every `lab_*`, `copilot_*`, `paper_events`,
  `paper_runs`, `paper_policies`, `paper_evidence` and `paper_software` table (45 tables)
  was byte-identical to the backup.

### Known limitations

- **Home-PC uptime.** Coverage reports offline days honestly but cannot prevent them; a day
  without a run inside 00:00–12:00 UTC is a missed entry window.
- **Notification lag.** Open notifications still arrive about a day after the fill they
  represent. This is reported, not changed.
- **Daily granularity only.** Marks, the equity curve and liquidation distance use daily
  closes; there are no intraday marks.
- The counterfactual for operational misses uses the recorded snapshot and the frozen risk
  policy. It says whether an order *would have been submitted*, never what it would have
  earned.
- Component freshness uses a fixed 36 h threshold (display only).
- No chart: the equity history is a table. The Streamlit dashboard was not extended.

## 19. Step 14 implemented: always-on runtime

> **Exactly one runtime is permitted to write the live prospective Prism database.**
>
> **Moving hosts does not restart or alter any prospective research or paper-trading experiment.**

Phase 14 moved the live prospective runtime off the home PC, which had been the main cause of
missed evaluations, missed entry windows and coverage gaps. It now runs on an always-on
Railway service, `prism-runtime`, with the authoritative database on its `/data` volume. The
full operations guide is **docs/OPERATIONS.md**. It covers the architecture, scheduler,
backups, health, deploys, rollback, secrets and troubleshooting.

This phase is infrastructure only:

- **Same experiments.** Same paper run `paperrun_27b0a336…`, same balance, peak, maturity
  clock and event sequence, same trackings, same watches, same `copilot_policy v1`,
  `autotrader_policy v1`, `paper_risk_policy v1`, exit policy and execution model. No new
  run and no backfill.
- **Same commands, same order.** `market ops cycle prospective` runs `lab forward run` →
  `lab copilot run --no-update` → `lab paper run` → `lab paper brief --send` as child
  processes, exactly like `forward_run.sh` did. The business logic is untouched.
- **Migration 17** adds `runtime_events` (authority claims, deployments, verified backups) and
  `runtime_cycles` (one row per job run). These are deployment facts, never evidence.
  `market ops continuity` fingerprints every `lab_*`, `copilot_*` and `paper_*` table, and
  excludes these two.
- **Provenance.** `software_id` remains per-run provenance. The deployed image has no `.git`,
  so the commit comes from a `REVISION` file written by `deploy.sh`. The host name is recorded
  only in runtime rows and never enters research identity, so changing hardware creates no
  new evidence.
- **Authority guard.** The live database is claimed for `railway-prism-runtime`. Any other
  process (the home PC, a laptop with a pulled copy) can read it but cannot write it.
- **Schedule (UTC).** Prospective at 00:10, 00:45, 03:00, 06:00, 11:00 and 17:00, plus a boot
  catch-up; backup at 01:30; OI at 02:30, 08:30, 14:30 and 20:30; daily update/scan at 09:00.
  00:10 is the first run after each daily close, well inside the paper 12 h entry window.
- **Live application (2026-10-04).** Cut over at 17:46–17:48 UTC: home tasks disabled, the DB
  claimed and transferred (hash-verified), and continuity identical apart from the schema
  version (16 → 17). The service was then moved to eu-west, because Binance Futures blocks US
  IPs. Deployment recorded as `rtev_e767cf57…` (commit `34ab752`). The full timeline and
  baselines are in docs/OPERATIONS.md, "Phase 14 cutover log".
- **Unchanged.** Bar semantics: an "opened" notification can still arrive a bar after the
  fill it describes (section 18, Known limitations). The always-on host removes the scheduler
  delay, not the daily-bar design.

## 19a. Step 15 implemented: intraday market-data foundation

> **Phase 15 introduces intraday market data but does not introduce intraday strategy signals.**
>
> **The existing daily paper account continues using its frozen Phase 12 execution semantics;
> intraday execution data is observational/shadow-only in this phase.**

The full reference is **docs/INTRADAY.md**. For the Lab:

- **Data:** provider-native Hyperliquid 15m/1h/4h perp bars, collected live every 15 minutes,
  plus Binance USD-M history (research only). One table (`perp_intraday_bars`, migration 18)
  with UTC half-open `[open_time, close_time)` bars on the epoch grid. Bars are stored only
  once closed, carry `first_observed_at` (availability) and `observed_live`, and keep
  revisions in `perp_intraday_revisions`.
- **Causal access:** `intraday.align` returns, at any instant, only bars that were closed and
  available by then (observed availability by default; an explicit assumed latency for
  backfilled history). `load_bars(..., known_at=t)` reconstructs the values Prism held at `t`.
- **Datasets:** `SeriesSelection` kinds `perp_intraday_bars` and `perp_intraday_revisions`
  (15m/1h/4h) use the existing compressed SHA-256 row snapshots. Evaluation plans still
  preregister daily inputs only, so no confirmatory claim can rest on intraday data yet.
- **Unchanged:** every strategy, the compiler/vocabulary, the evidence and promotion policies,
  `copilot_policy v1`, `autotrader_policy v1`, `paper_risk_policy v1`, the exit policy, the
  execution model, the paper run and its events. The shadow table `intraday_execution_shadow`
  is not paper evidence, and the paper engine never reads it.
- **Not implemented (Phases 16–18):** swing/sweep/rejection/structure/retest primitives,
  relative strength, OI features, intraday signals and intraday backtests.

## 20. Verification

Baseline before changes: **173 tests passed**, repository Ruff checks passed, and all
89 existing source/test Python files passed format checking. New focused tests cover
canonical round trips, rename/reorder identity, rule changes, nested immutability,
methodology field rejection, finite numeric parameters, bounded counts, explicit MTF
references, market restrictions, lineage, schema versions and safe YAML parsing.

Final checks:

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest` | **205 passed** in 99.79 seconds, including existing backtest/PIT, research, paper, scanner, dashboard and Telegram tests |
| `.venv/bin/python -m pytest tests/test_lab_spec.py` | **32 passed**, including the pinned canonical ID and default resolution |
| `.venv/bin/ruff check src tests dashboard` | Passed |
| `.venv/bin/ruff format --check src tests` | All 92 files formatted |
| `git diff --check` | Passed |
| `.venv/bin/market --help`, example load and JSON-schema generation | Passed; existing CLI commands intact |

The repository does not configure a static type checker in `pyproject.toml`, so a separate
static type check was not run. No new type-checking tool or dependency is introduced.
Runtime model validation is covered by pytest. Database inspection was read-only, and
tests used their existing isolated fixtures; no real-data research or live/paper scan
was triggered.

### Step 2 verification

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest` | **258 passed** (121.5 s) |
| `.venv/bin/python -m pytest tests/test_lab_governance.py` | **53 passed**: plan/dataset/hypothesis identity, unknown/unsupported policy fields, strategy/policy separation, content and provenance revisions, retained snapshots after market-table edits, NULL vs NaN, weak fingerprints, snapshot corruption/limits, invalid submissions, fixed family/lineage, preregister→start→result ordering (also via SQL FKs), one immutable result per attempt for all six statuses, explicit reruns (including after rename or code change), universe canonicalisation, exposure records, persistence + read-only CLI, additive migration from a v6 DB, spot actions, software identity |
| `.venv/bin/python -m pytest tests/test_lab_spec.py` | **32 passed** |
| `.venv/bin/ruff check src tests dashboard` | Passed |
| `.venv/bin/ruff format --check src tests` | All 99 files formatted |
| `git diff --check` | Passed |
| Migration on a copy of the local `data/prism.duckdb` | v6 → v7; all pre-existing table row counts identical (only `schema_version` gained a row); reopen is a no-op |
| `market lab --help`, `market lab experiments/strategies` on the migrated copy | Passed (empty ledger) |

### Step 3 verification

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest` | **362 passed** (132.8 s); baseline before Step 3: 268 |
| `.venv/bin/python -m pytest tests/test_lab_compiler.py` | **94 passed**: canonical tokens/aliases, registry ↔ implementation correspondence, declared warmup = first valid bar for every family, hand calculations, Prism parity (`compute_features`, `sma_atr`, trend/breakout levels, `funding_percentile`, `daily_funding` hourly + 8-hourly, `edge_trigger`, trend_ls conjunction), per-family future-mutation and truncation invariance, compiled mutation/truncation invariance including a funding-cadence change that Prism's full-history estimate fails, breakout excludes bar T, percentile ignores later extremes, crossover/T−1 semantics, missing ⇒ ineligible, late-published funding, spot forward split causality + ratio parity, explicit failures, determinism/digest, snapshot isolation after live-table edits, read-only CLI |
| Lab suites (`test_lab_compiler/spec/governance`) | **179 passed**; pinned Phase 1 strategy ID unchanged |
| Sabotage checks (temporary, reverted) | Full-history funding cadence → mutation test fails (89.7% of pre-cutoff `funding_pct` values change); backward split adjustment → spot causality test fails (`atr_14` levels before T change) |
| `daily_funding` default path vs the pre-change implementation | Bit-identical on all 11 stored coin/venue series (read-only DB) |
| `.venv/bin/ruff check src tests dashboard` / `ruff format --check src tests` | Passed / 105 files formatted |
| `git diff --check` | Passed |
| `market lab compile` on a scratch copy of the local DB | BTC/ETH Hyperliquid snapshot (1,098 daily rows each), crossover + `funding_pct_7_365` rule: metadata printed in ~1 s; the real database was not modified |

### Step 5 verification

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest` | **399 passed** (179 s); before Step 5: 385 |
| `.venv/bin/python -m pytest tests/test_lab_batch.py` | **14 passed**: BH against hand calculations, ties, capping, order invariance and the brute-force step-up rule (200 random families); canonical/complete batch identity; strict manifests; survivor classification (low q cannot pass a tiny or inconsistent effect, a big effect cannot skip q); full run with every member visible; raw p = stored primary endpoint only; q = BH over the family; freeze, overlap and cherry-pick refusals; primary-horizon and final-holdout refusals (warmup overlap included); Monte Carlo resolution refusal; errored members; crash, resume and software guard; explicit reruns reproduce raw p, q and statuses without modifying earlier analyses or screens; CLI lifecycle |
| Lab suites (batch/screen/compiler/spec/governance) | **216 passed** |
| `.venv/bin/ruff check src tests dashboard` / `ruff format --check src tests` | Passed / 109 files formatted |
| `git diff --check` | Passed |
| CLI on a scratch copy of the local DB | `batch create` (YAML) → `batches` (FROZEN) → `batch run` (1.8 s, 4 members, hand-checked q) → `batch show` (COMPLETED); a second run and a re-create are refused (exit 1) |
| Benchmark (scratch copy) | 59 members, 16.6 s, 0.28 s per member, 377 MB peak; 0 survivors |

### Step 6 verification

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest` | **420 passed** (187 s); before Step 6: 399 |
| `.venv/bin/python -m pytest tests/test_lab_families.py` | **21 passed**: pinned catalogue IDs and counts, determinism, YAML-order invariance, new identity for every frozen change, constraints, cap without truncation, lineage/naming/sides, short = mirror of long where intended, strict schema (bare strings, unknown placeholders, non-canonical features, unused parameters, file naming), every perp variant compiles on a retained snapshot, dry run writes nothing, Monte Carlo refusal, idempotent registration and family clashes, generated batch → freeze → run end to end, CLI |
| Lab suites (families/batch/screen/compiler/spec/governance) | **237 passed** |
| `.venv/bin/ruff check src tests dashboard` / `ruff format --check src tests` | Passed / 111 files formatted |
| `git diff --check` | Passed |
| CLI dry runs | `market lab families`, `family show <name> --market perp|spot` (perp-only family refused for spot), `batch generate` dry run and `--register --out` |
| Governed smoke run (scratch copy) | 40 variants → manifest → frozen batch → 40 screens → BH analysis, COMPLETED in 12.7 s (results above) |

### Step 7 verification

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest` | **432 passed** (216 s); before Step 7: 420 |
| `.venv/bin/python -m pytest tests/test_lab_evidence.py` | **12 passed**: FDR honesty (raw p < 0.10 with q 0.8 is at most EXPLORATORY; FDR survivor plus plateau gives RESEARCH_SUPPORTED); isolated spike flagged and blocking research support; plateau, edge and two-parameter adjacency; asset domination and concentration; horizon reversal descriptive only (statistics and tier unchanged by non-primary horizons); INSUFFICIENT, UNAVAILABLE (errors are not negative), NEGATIVE, INCONCLUSIVE; lineage-free strategies; determinism and policy versioning; consumer neutrality (forbidden keys, extra fields rejected, identity independent of derived text); long horizons, `extends` with later stages, VALIDATED reserved; governed batch integration (profiles cite records, append-only, idempotent, underlying records unchanged); CLI |
| Lab suites (evidence/families/batch/screen/compiler/spec/governance) | **249 passed** |
| `.venv/bin/ruff check src tests dashboard` / `ruff format --check src tests` | Passed / 113 files formatted |
| `git diff --check` | Passed |
| CLI on the Phase 6 smoke batch (scratch copy) | `evidence build` (40 profiles, idempotent), `evidence report`, `evidence show` |

### Step 7.1 verification

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest` | **437 passed** (209 s); before Step 7.1: 432 |
| `.venv/bin/python -m pytest tests/test_lab_evidence.py` | **17 passed** (5 new): v1 policy ID pinned and strict; v2 dead band (tiny flips flat, material flips still reverse; tiers unaffected); builder provenance recorded, version in identity, software ID not; schema-1 identity formula unchanged; report policy versioned and cited; v1 and v2 profiles coexist with old rows untouched |
| Lab suites | **254 passed** |
| `.venv/bin/ruff check src tests dashboard` / `ruff format --check src tests` | Passed / 113 files formatted |
| `git diff --check` | Passed |
| CLI on the scratch DB | `evidence build` (v2: 40 new; re-run: 0 new), v1 rows unchanged, `evidence report` cites report policy v1 |

### Step 8 verification

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest` | **473 passed** (395 s) |
| `.venv/bin/python -m pytest tests/test_lab_forward.py` | **20 passed** (see list below) |
| Lab, paper and perp suites (`test_lab_*`, `test_perp_paper`, `test_perps`, `test_perp_backtest`, `test_perp_research`, `test_binance`, `test_open_interest`, `test_store_lock`) | Passed; Phase 7 profile identities and payloads unchanged |
| `.venv/bin/ruff check src tests dashboard` / `ruff format --check src tests` | Passed / 118 files formatted |
| `git diff --check` | Passed |
| CLI | `forward --help`, `candidates`, `enroll --dry-run`, `enroll`, `list`, `check --dry-run` on the live DB; full lifecycle in the CLI test |
| Live DB | Backed up to `data/prism.pre_phase8.duckdb`; migration 11 additive; Lab batch reproduced; 3 trackings enrolled; 0 evaluations (correct: no bar has closed since enrollment). Closure: the Donchian tracking was stopped before any evaluation; 2 active |

`test_lab_forward.py` covers:

- no evaluation before a bar completes (plus the DB CHECK);
- no backfill after downtime, with gap reasons;
- idempotent re-checks;
- conflicts are refused without overwrites;
- funding readiness waits;
- signals match the Phase 3 compiler, with provenance and a data cutoff of T;
- a semantics change stops evaluation;
- isolation from live-table edits;
- pause and stop;
- hand-calculated long and short T+1-open outcomes with costs and funding;
- pending until exit data exists, then immutable;
- missing funding is unavailable, never zero;
- a missing T+1 bar makes the entry unavailable;
- a `paper_forward` profile extends history without changing it or its tier;
- no-signal is distinct from a gap;
- maturity levels;
- the candidate rule ignores effect size;
- the CLI lifecycle.

### Step 9 verification

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest` | **497 passed** (607 s); before Step 9: 473 |
| `.venv/bin/python -m pytest tests/test_lab_validation.py` | **24 passed** (see list below) |
| Lab, perp, paper, OI and lock suites (`test_lab_*`, `test_perp_*`, `test_perps`, `test_binance`, `test_open_interest`, `test_store_lock`, `test_macro_shock`) | **368 passed**; Phase 7/8 identities and payloads unchanged |
| `.venv/bin/ruff check src tests dashboard` / `ruff format --check src tests` | Passed / 121 files formatted |
| `git diff --check` | Passed |
| CLI | `research reserve-plan/register (--dry-run)/list/show/run/evidence`, `validation preview/run`, rerun refusal; full lifecycle in the CLI test, rehearsed on a scratch copy and run on the live DB |
| Live DB | Backed up to `data/prism.pre_phase9.duckdb`; migration 12 additive; 2 registrations, 2 full-research and 2 pre-outcome validation results, 2 schema-4 profiles; Phase 8 untouched |

`test_lab_validation.py` covers:

- registration freezing and eligibility (explicit; INSUFFICIENT refused; re-registration
  refused);
- period separation: no reserved period, a period inside the discovery data region, a
  changed methodology, or a final-holdout clash are all refused;
- the final holdout is never read: rewriting rows from it onwards (and discovery rows
  before the warmup) leaves validation unchanged;
- parity with Prism's `perp_asset_events` → `run_event_study`;
- frozen walk-forward equals a direct one-point `walk_forward`;
- sensitivity describes neighbours without selecting one;
- explicit reruns keep earlier results;
- incomplete and capacity gates give INSUFFICIENT with no exposure;
- post-outcome too-few-events is INSUFFICIENT, not a rejection;
- hand-built supportive and adverse toy markets;
- exposure is permanent for the strategy and its neighbours, and reruns never restore
  independence (later registrations are compromised);
- a semantic change stops the run;
- forward records never enter validation metrics, while forward outcomes before a
  registration compromise it;
- evidence extension: full research alone does not change the tier, supportive
  independent validation promotes, the historical profile is byte-identical, idempotent;
- VALIDATED is unreachable; compromised or non-first-look validation cannot promote;
- extended profiles cannot be enrolled for forward tracking;
- errors are recorded;
- the CLI lifecycle;
- the ledger orders events through small backward clock steps.

### Step 10 verification

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest` | **527 passed**; before Step 10: 497 |
| `.venv/bin/python -m pytest tests/test_copilot.py` | **30 passed** (see list below) |
| Scanner/Telegram/co-pilot/forward suites (`test_brief`, `test_portfolio`, `test_presenter`, `test_setups`, `test_scoring_hype`, `test_copilot`, `test_lab_forward`) | **104 passed**; forward tests unchanged after the live-window helper extraction |
| `.venv/bin/ruff check src tests dashboard` / `ruff format --check src tests` | Passed / 127 files formatted |
| `git diff --check` | Passed |
| CLI | `copilot policy/watch (--dry-run)/watchlist/candidates/preview/run --dry-run/run/decisions/show/pause/resume/stop`; full lifecycle in the CLI test, rehearsed on a scratch copy, then live |
| Live DB | Backed up to `data/prism.pre_phase10.duckdb`; migration 13 additive; 2 watches; 1 CLI run and 1 `forward_run.sh` run: 0 decisions, 0 deliveries. 28 of 30 `lab_*` tables byte-identical to the backup; the other two (`lab_forward_runs` +2, `lab_software` +1) are the forward tracker's own check/resolve rows from the wrapper run |

`test_copilot.py` covers:

- EXPLORATORY evidence alerts with q 0.91, and the message says it did not survive family
  correction;
- stronger evidence (consistent full research + plateau + breadth, RESEARCH_SUPPORTED, or
  supportive validation) maps to STRONG WATCH, while the evidence tier is unchanged;
- adverse validation suppresses, and mixed validation holds the priority at WATCH;
- insufficient validation and TOO_EARLY or EARLY forward evidence do not suppress;
- mature adverse forward evidence suppresses, and developing adverse forward evidence
  downgrades;
- NEGATIVE, INSUFFICIENT, UNAVAILABLE and INCONCLUSIVE suppress;
- each blocking rule is tested in isolation;
- policy identity and strictness (extra fields rejected);
- message wording (no BUY/SELL/APPROVED/probability) and the digest;
- watch registration rules;
- no signal means no decision and no send, with the state recorded in the run summary;
- an alert is recorded and sent once; re-runs are idempotent;
- bars outside their window never alert;
- a dry run writes nothing and sends nothing;
- consumer neutrality: every `lab_*` table is byte-identical, and the forward tracker
  agrees on the signal;
- Telegram failure: recorded, retried once inside the window, never duplicated; "not
  configured" is a recorded failure;
- an unknown delivery outcome is never resent;
- a digest is sent when many alerts fire;
- a policy change never replays old bars, and cross-policy duplicates are suppressed;
- a version mismatch suppresses;
- the newest schema-4 extension is used (INCONCLUSIVE + adverse validation → SUPPRESS,
  baseline untouched);
- a paused watch is respected, and the DB CHECK on the decision window holds;
- no auto-trader field or column exists anywhere in co-pilot records;
- `research/` never imports `copilot`;
- the CLI lifecycle.

### Step 11 verification

| Check | Result |
|---|---|
| `pytest` (full) | **545 passed**; before Step 11: 527 |
| `pytest tests/test_lab_corroboration.py` | **18 passed** |
| Lab + co-pilot + Binance/perp/OI suites (`test_lab_*`, `test_copilot`, `test_binance`, `test_perps`, `test_perp_*`, `test_open_interest`), on the final code | **404 passed** |
| `ruff check src tests dashboard` / `ruff format --check src tests` | Passed / 129 files formatted |
| `git diff --check` | Passed |
| CLI | `corroboration register --dry-run/register/run/show/list/evidence`; full lifecycle in the CLI test, dry-run rehearsed on a scratch copy, then live. `copilot policy/candidates`, `forward check --dry-run` and `evidence report` behave as before (report identical to the backup) |
| Live DB | Backed up to `data/prism.pre_phase11.duckdb`; migration 14 additive; 2 registrations, 2 runs (both CROSS_VENUE_MIXED), 2 schema-5 profiles; every `copilot_*`, `lab_forward_*`, `lab_research_*` table and all 42 earlier profiles byte-identical to the backup |

`test_lab_corroboration.py` covers:

- terminology: never independent (model, policy, DB CHECK); no status says VALID; the
  corroboration plan is refused as a Phase 9 validation plan;
- the frozen strategy, period, assets and venue costs; only venue, costs and period may
  differ from the source plan;
- the period must end before the source discovery starts;
- missing listing history: excluded with a reason or reported per asset, never
  fabricated; too few venue assets refuses registration;
- historical exposure: venue-specific legacy runs, their share of the period, the
  unrecorded caveat, and the governed look recorded as exposure;
- only venue rows: Hyperliquid rows in the same window change nothing; parity between
  the governed and recomputed screens;
- T+1-open entry and T+h-close exit; Binance fee and slippage; 8-hourly funding summed
  per day; the exact cost difference from Hyperliquid costs;
- adverse and insufficient results are recorded, never hidden; reruns are explicit;
  semantic changes are refused; errors are recorded;
- no pooling: the comparison is side by side and asset composition is reported;
- evidence extension: earlier profiles byte-identical; the tier is copied; idempotent;
  no consumer or approval field;
- tier safety: VALIDATED and an independent block are rejected; Phase 8 enrollment and
  Phase 9 registration refuse schema 5;
- co-pilot isolation: policy v1 ID, `latest_extension` and all `copilot_*` rows are
  unchanged; the batch report is unchanged;
- the CLI lifecycle.

### Step 12 verification

| Check | Result |
|---|---|
| `pytest` (full) | **585 passed**; before Step 12: 545 |
| `pytest tests/test_paper.py` | **40 passed** |
| `ruff check src tests dashboard` / `ruff format --check src tests` | Passed / 138 files formatted |
| `git diff --check` (+ trailing-whitespace scan of new files) | Passed |
| CLI | `paper policy/create (--dry-run)/run (--dry-run)/status/positions/trades/events/pause/resume/stop/summary/evidence`; full lifecycle in the CLI test, rehearsed on scratch copies, then live |
| Restart/idempotency | `SIGKILL` mid-cycle on a scratch copy: nothing written; recovery identical to the uninterrupted schedule (311 events); retry runs write nothing |
| Live DB | Backed up to `data/prism.pre_phase12.duckdb`; migration 15 additive; 1 paper run created (flat, no trade at activation); all 40 `lab_*`/`copilot_*` tables byte-identical to the backup after creation and first cycles; `forward_run.sh` logged `forward=0 copilot=0 paper=0` |

`test_paper.py` covers:

- **safety:** the paper package imports no network/provider/exchange/Telegram/co-pilot
  module and reads no secret or environment variable; repo-wide, no order-placement path
  and no live-readiness status; the adapter's public surface is exactly the five members
  and the guard refuses subclasses and look-alikes; policies reject `mode: live`; no
  live/real/execute/key option in the paper CLI and no real-trading command anywhere; a
  full cycle with sockets disabled; DB CHECKs refuse a non-paper run and a pre-creation
  event;
- **policies:** v1 admits the live cohort's evidence shape with MIXED corroboration as a
  caveat, and is stricter than the co-pilot rule by rule; identities and versions; frozen
  risk values; maturity counts only trades and days;
- **risk engine (pure):** fixed-fraction sizing; same-snapshot lottery allocation that is
  invariant to input order and gives no name advantage over 200 bars; proportional scaling
  and minimum-size drop; every gate and limit; conflict rules;
- **account (integration):**
  - creation freezes identity, refuses ineligible/second runs and allows lineage;
  - prospective only;
  - one intent per signal; T+1-open fills (with a T+1 open made to differ from T's close);
    fees and slippage; hand-calculated long and short trades;
  - funding: settled once with the right sign; missing settlements counted, never
    estimated;
  - stale data waits, never invents; a dead asset does not block other assets; a signal
    after the window is skipped;
  - pause/resume without replay; stop manages positions to exit; daily-loss halt;
    drawdown kill; liquidation; auto-pause after repeated errors;
  - reruns idempotent; crash mid-commit leaves nothing and recovers identically;
- **notifications:** PAPER-labelled, retried once, never resent, an unknown outcome never
  resent;
- **evidence and neutrality:** evidence reconciles with the ledger; the evidence chain is
  point-in-time; `lab_*`/`copilot_*` byte-identical and the forward tracker unaffected;
  the consumers never import each other; the fast funding-readiness check equals Phase 8's.

### Step 13 verification

| Check | Result |
|---|---|
| `pytest` (full) | **605 passed**; before Step 13: 585 |
| `pytest tests/test_paper_observe.py` | **20 passed** |
| Paper, co-pilot and CLI suites on the final code | Passed (the Phase 12 CLI test now reads `runs` / `--json`, since `status`, `positions` and `trades` render tables) |
| `ruff check src tests dashboard` / `ruff format --check src tests` | Passed / 141 files formatted |
| `git diff --check` (+ trailing-whitespace scan of new files) | Passed |
| CLI against the live run | `status`, `positions`, `risk`, `trades`, `intents [--skipped]`, `gaps`, `contributions`, `equity`, `brief`, `snapshot` (repeat is a no-op), `market status` |
| Live DB | Backed up to `data/prism.pre_phase13.duckdb`; migration 16 additive; first snapshot `papersnap_ce392529…` (10,000 USDC, 0 positions, 0 trades); no brief yet (no completed day); `forward_run.sh` logged `forward=0 copilot=0 paper=0 brief=0`; 45 `lab_*`/`copilot_*`/paper ledger and policy tables byte-identical to the backup |

`test_paper_observe.py` covers:

- **identity and reconciliation:** the v1 policy IDs are pinned; the account summary
  reconciles with replay and the Phase 12 summary;
- **views:** position metrics (marks, uPnL, margin, approximate liquidation distance, bars
  left); near-liquidation surfaced and the position untouched; risk headroom, and a pause
  blocks entries; trade history costs; strategy and asset attribution;
- **dispositions:** paused/risk rejections are expected skips; an offline gap is a missed
  execution, with an accepted counterfactual and no backfill; late data is `data_late`, not
  offline;
- **coverage:** missed cycles become unprocessed → `STALE`, then late → `DEGRADED`, with the
  PC-off days listed; error streaks are reported;
- **notification lag** timestamps;
- **snapshots:** immutable, idempotent, reproducible from the ledger prefix;
- **brief:** matches the account, is stored once, sent once, retried on failure; an
  unknown outcome is never resent; nothing is stored before the first completed day; a
  crash mid-brief leaves no partial row;
- **read-only guarantee:** every view, snapshot and brief leaves the ledger, policies,
  cycles and the Lab/co-pilot tables byte-identical, creates no run, and the next cycle is
  unaffected;
- **no feedback path:** the observe module calls no engine write path and constructs no
  policy;
- `market status`, and the CLI observability commands.
