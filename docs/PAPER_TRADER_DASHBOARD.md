# Paper trader dashboard

Prism has a read-only Streamlit dashboard for the retired Paper v2 fixed-horizon baseline,
original `paper_nimble_v1`, and Phase 30's immediate and confirmed event cohorts.
It displays descriptive paper-execution performance. It does not
establish scientific validity, graduate hypotheses, or establish statistical superiority.

## Open the dashboard

From the checkout containing this change:

```bash
uv sync --frozen
uv run python scripts/pull_paper_snapshot.py
PRISM_PAPER_DB_PATH=data/remote/paper-dashboard.duckdb \
  uv run streamlit run dashboard/paper_app.py
```

The standalone entry point opens directly on performance and exposes only read-only
controls. The existing `dashboard/app.py` also includes **Records → Paper traders**;
its other legacy pages retain their existing behavior. The paper page never uses those
pages' scan persistence, journal, or portfolio write helpers.

`PRISM_PAPER_DB_PATH` selects this page's source independently of the main app's
`PRISM_DB_PATH`. If omitted, the main configured database is used. Missing files are
reported without creating databases or applying migrations. No synthetic runs or
fallback production IDs are injected into an empty snapshot.

## Data sources and deterministic queries

All values come from the configured DuckDB; the frontend contains no trading math.
`src/market_signal/paper/dashboard/report.py` is the read-only query/report layer.
`dashboard/paper_data.py` opens short-lived `Store(..., read_only=True)` connections.

| Information | Source |
| --- | --- |
| Run identities, names/version, activation | `paper_v2_runs`, `paper_nimble_runs` frozen definitions |
| Starting capital | Persisted `paper_v2_policies` risk definition, or nimble's frozen baseline risk definition; unavailable capital is N/A |
| Hypothesis identities/families | Frozen `paper_v2_universes` / `paper_nimble_policies` definitions; canonical persisted definition identity, never the current bootstrap registry |
| V2 positions/trades | Latest `opened`/`closed` event per trade, ordered by ledger `seq` |
| V2 current equity/marks/status | Latest `mark` and `run_state`; latest evaluation from `paper_v2_evaluations` |
| Nimble current account and positions | `paper_nimble_accounts`, `paper_nimble_positions`; frozen levels from `paper_nimble_theses` |
| Signals/admissions/rejections | SQL aggregation of fired `paper_*_opportunities`, bounded activity/rejection queries |
| Event classifications, waits, source gates and decisions | `paper_event_evidence` append-only records; `paper_event_pending` current wait projection, where present |
| Cash curves | Original `opened`, `closed`, and nimble `funding_adjustment` ledger events, ordered by `seq` |
| MTM and drawdown | Recorded `mark` / `account_mark` events; no new quotes or marks are generated |
| Capture/avoidance | Original primary `paper_nimble_research` path joined by opportunity and its frozen `research_primary_minutes` |

Queries use parameterized run IDs and time bounds. Table names come from the two internal
system prefixes; grouping and sorting fields have explicit allowlists. Reporting queries
are SELECT-only; there are no schema changes. Core closed-trade statistics reuse Phase
27's `paper.nimble.report.metrics`. Its existing capture arithmetic is extracted into a
shared `capture_metrics` helper with unchanged behavior so the dashboard and CLI agree.

## Run selection and comparison

Four distinct selectors list Paper v2, original nimble, immediate events and confirmed
events. Each defaults to its newest active run, or latest historical run when none are
active. V2 remains selectable after retirement; its stored status is displayed exactly,
even if a snapshot predates retirement. No dashboard action retires or pauses a run.
It shows stored run
name/version, complete ID, status, activation time, observed equity, and last evaluation
or ledger update. Historical runs remain selectable. Missing systems are explicitly absent.

Run metadata includes execution version, event variant, execution/risk policy IDs,
entry universe, registered v2 baseline, preserved nimble baseline IDs, thesis/exit
policy versions, review time, and hypothesis counts. These come from frozen stored
definitions. Event policies contain both variants; each cohort shows only its own
three event hypotheses plus the original 28. Event IDs use the canonical
`eventhyp_v1_` identity prefix; original hypotheses retain `paperhyp_v2_`.
Independent accounts are never pooled. The four overview columns and equity traces
are distinct. Event trade/open-position rows include event ID/class and entry variant;
closed trade rows also show provider, confirmation delay, event age at entry, pre-fill
movement missed and confluence where stored. Hypothesis names/families resolve against
that run's frozen registry.

The overview shows each run's own selected window. The concise comparison uses their
common prospective interval for the selected **Comparator** and **Compared run**,
starting at the later activation/window start, and includes
only trades entered in that interval. Outcomes require a close by its end. Differences
are **compared run minus comparator**: net PnL, average hold, observed drawdown, notional capital
utilization, trades/day, cost drag, and MFE capture where both sources exist. Selecting a
baseline other than nimble's frozen `baseline.run.run_id` produces an explicit notice.

Fewer than 30 closed trades in either common entry cohort displays exactly:
**Insufficient sample for conclusion.** This is a UI sample-size threshold, not a
significance test. At larger counts the page still says statistical superiority has not
been established. Different fills, activation times, costs and unpaired admissions can
confound simple differences. No paired experiment or hypothesis validity is inferred.

## Decision analysis and event cohorts

**Decisions / event cohorts** lets you inspect any selected account, including the
retired v2 history. `src/market_signal/paper/dashboard/decisions.py` uses deterministic
SELECT queries with parameterized filters and stable timestamp/source/ID ordering.
The three stages remain distinct:

- Fired opportunity evaluations: persisted signal/availability/evaluation timestamps,
  admission/support/rejection and engine reason from `paper_*_opportunities`.
- Execution ledger: pending intents, entries, exit intents, closes, support, expiries,
  cancellations and funding corrections from `paper_*_events`.
- Event evidence: classifications, enrollment, confirmation waits, terminal decisions,
  source viability and duplicate observations from `paper_event_evidence`.

Filter by stage, exact asset/reason, logical event ID or trade ID. SQL counts matches
and fetches 50 rows per page (maximum query page size 200). Only a selected record's
payload is fetched. The inspector presents saved admission/input gates and source
timing as readable fields, plus expandable complete evidence, frozen entry thesis,
latest trade state and related event/execution timelines. Related timelines are limited
to 50 recent rows; applying their event/trade filter exposes all pages.
Latest trade state can postdate the selected decision and is labeled separately.
No later state is substituted for entry-time evidence, and missing context or gates
are not reconstructed. Publication, collector, gateway, context and evaluator clocks
are displayed where persisted, without counterfactual source/fill claims.

Event overview counts distinct logical events **within that cohort/window**;
classification rejections, enrollment, confirmation waits and terminal decisions are
counts of evidence records at their respective stages. A wait is not a rejected trade.
An unsupported classification has no assigned hypothesis and remains in the ledger;
it is not falsely attributed to every event hypothesis. Pending count describes the
current snapshot, regardless of historical window. Top reasons show 30 stage/reason
combinations. The three cohort-specific event hypotheses are listed even without trades;
outcome metrics remain N/A. Original-vs-event contribution aggregates the existing
report metrics separately inside that account, with no extra or duplicated PnL.

Older snapshots can contain event run metadata/trades but omit event evidence tables.
The page warns explicitly and preserves available performance. Retrieve a newer
artifact with the updated snapshot script to inspect those decisions; local reread
cannot add missing tables. Compact snapshots retain fired opportunities only;
this ledger does not claim to show every non-fired evaluation or an unstored/retriable
engine decision.

## Windows and metric definitions

Default: **All time**. Other options: 24h, 7d, 30d, and custom UTC dates. For local
snapshots, windows end at the backup timestamp, or the last recorded paper update when
provenance is absent. Production mode ends at current UTC time. Custom dates are inclusive
through the chosen end date, bounded by available observation time.

Current equity, realized cash, and open positions always describe the latest stored state;
they are not reconstructed historical portfolios when a custom end is selected. Age is
measured at the snapshot/live observation time. Windowed outcomes select **exit time**;
trade frequency selects **entry time**; opportunity counts select **evaluation time**.

| Metric | Exact definition |
| --- | --- |
| Starting equity | Frozen starting capital, in simulated USD/USDC |
| Current equity | Last recorded engine equity, including open-position MTM and recorded reserves |
| Realized equity/cash | Starting capital plus closed net PnL minus entry fees of still-open positions; nimble uses its authoritative account cash projection |
| Realized net PnL | Sum of current stored `net_pnl` for qualifying closed trades; provisional closed accounting is identified separately |
| Price PnL | Stored nimble reference-price PnL; for v2, side sign × units × (exit reference − entry reference) |
| Gross PnL | Stored `gross_pnl` using engine fill/cap treatment; slippage must not be deducted from gross a second time |
| Fees | Sum of both entry and exit fees for qualifying closed trades |
| Slippage | Stored `slippage_cost`, including spread where nimble's fill model includes it |
| Funding | Stored signed funding cost; positive is paid, negative is income |
| Total trades | Count entered during the window, open and closed; pending intents are excluded |
| Closed trades | Count with exit in the window |
| Open positions | Latest OPEN or EXIT_PENDING positions, regardless of selected window |
| Trades/day | Window entry count divided by elapsed seconds/86,400 since max(activation, window start); partial days are fractional |
| Win rate | Closed trades with net PnL > 0 / all closed trades; breakeven is not a win |
| Average winner/loser | Arithmetic mean strictly positive / strictly negative closed net PnL, in currency |
| Expectancy/trade | Closed net PnL / closed count, in currency |
| Profit factor | Sum positive closed net PnL / absolute sum negative closed net PnL; no loss denominator → N/A |
| Max drawdown | Maximum recorded MTM `drawdown` in the window (1 − equity / engine historical peak); no mark observations → N/A |
| Average hold | Mean (exit timestamp − entry timestamp), in minutes, across closed trades |
| Capital utilization | Sum entry notional × seconds held overlapping the window / (starting equity × window seconds), including currently open positions; both sides contribute positively and leverage can exceed 100% |
| Cost drag | Closed fees + spread/slippage + signed funding; reserves/adjustments remain separate |
| Gross/net trade return | Gross PnL / entry notional; stored net `trade_return` respectively, expressed as fractional returns |

A measured zero (e.g. zero funding on a completed trade) stays zero. Missing data,
no closed observations, missing denominators, and unavailable metric paths show **N/A**.
Count metrics can correctly be zero. The table column suffix `minutes` is literal;
returns, MFE/MAE and ratios are fractional values unless shown with a percent sign.

## Equity and PnL attribution

Cash is an accounting curve, not hypothetical flat-account performance. At entry,
`cash -= entry_fee`; at closure, `cash += net_pnl + entry_fee`; at delayed nimble funding
settlement, `cash += funding_cash_adjustment`. This preserves the historical settlement
timestamp instead of rewriting prior equity with the final funding value. Original
engine ledger order is used. The activation point is shown at actual activation time.

Observed MTM is a separate diamond series that includes open PnL and conservative funding
reserves. It is always provisional as an account valuation. Realized cash uses steps only
when unsampled; sampled history uses points. Charts use actual UTC timestamps and never
smooth, average, linearly interpolate, or generate returns between observations. Long
histories retain bucket endpoints and extrema at their original timestamps, bounded to
approximately 1,000 points per series per run. Historical peaks used for drawdown can
precede a selected window; it is not a rebased-window drawdown estimate.

Cumulative and selected-window closed PnL attribution reconciles:

```text
reference price PnL − spread/slippage + liquidation adjustment
  − fees − signed funding − unsettled adverse funding reserve = net PnL
```

Nimble uses stored liquidation adjustments and funding reserves. V2's margin/catastrophe
residual is derived only from its authoritative recorded gross/net/fee/funding values;
it is never silently presented as price profit. Provisional closed trade counts and
`accounting_final` are visible. Open entry fees affect cash but are not double-counted in
closed-trade attribution.

## Trade, side, hypothesis and exit details

Open positions show asset, side, entry timestamps, fills/references, recorded mark and
mark time/stale flag, unrealized PnL, recorded funding/reserve, primary hypothesis,
snapshot age, target/invalidation and state rule, maximum hold, and exit state. V2 shows
fixed horizon and due time. Its marks do not record a separate accrued-funding number,
so that field is N/A. Nimble levels come from its frozen execution thesis; no exit
condition is evaluated by the dashboard.

Closed trades have run/ID, trade ID, asset, side, primary hypothesis/family, entry/exit
time, hold duration, fill/reference prices, gross return, fees, slippage, funding,
reserve/adjustment, net return/PnL, exit reason, horizon, MFE/MAE, capture/avoidance, and
accounting finality. Filters: asset, side, hypothesis and exact exit reason. Server-side
sort choices: entry/exit time, asset, side, net PnL, net return, hold and exit reason.
Pagination returns at most 50 rows to the frontend, including a total and page count.

Long and short share exactly the same reporting definitions. Hypothesis analysis groups
by any combination of **hypothesis ID, frozen hypothesis family, asset, side** (all four
by default). Signals/admissions are independent observation counts. Trades/PnL are
attributed to the primary admitted hypothesis; supporting signals never duplicate PnL.
Stored hypothesis names accompany the immutable IDs in filters and detail/group tables. Groups show signals, admissions, entries, closes, win rate, expectancy, net PnL, average
hold, available MFE/MAE and capture/avoidance. Group tables paginate at 100 rows.

Nimble exit analysis retains exact mechanical reasons: TAKE_PROFIT, PRICE_INVALIDATION,
STATE_INVALIDATION, THESIS_EXPIRED, COST_DECAY, MAX_HOLD, CATASTROPHE_GUARD, and other
recorded valid reasons (including INVALIDATED if recorded). Empty listed reasons have
count zero and N/A outcomes. Each category shows count, net PnL, average fractional net
trade return and average hold. V2 trades retain FIXED_HORIZON or their actual mechanical
exception such as LIQUIDATION/KILL_SWITCH; exceptions are not relabeled horizon exits.

MFE/MAE are stored favorable/adverse signed reference-price excursions during the trade.
For nimble capture, the **complete original primary research path** is normalized from
its research reference back to the actual entry reference. Capture = positive realized
reference-price return / full-path MFE, requiring both > 0. MAE avoidance =
max(0, trade MAE − normalized full-path MAE). Research-path capture can finalize after exit and describes the final available path, not knowledge at a historical window end. Missing/incomplete paths are N/A. V2's
available observed MFE/MAE remain visible; capture/avoidance are not invented for v2.

Activity uses expandable details: the latest 50 fired signals/admissions/rejections,
entries, exit intents and exits per run. It retains timestamps and shows the top ten
rejection reasons for the selected window. It does not load raw signal history in the
browser.

## Freshness and production snapshot workflow

The header shows source mode, exact file path, snapshot timestamp and its basis, snapshot
age, last production sync, production backup path, last paper update, and stale status.
Each run separately shows its update time and a stale warning. Thirty minutes is the
freshness threshold. This threshold is observability-only and changes no engine policy.

- **Local snapshot** is the default, even when a copied database contains production's
  authority claim. **Reload local snapshot** only clears frontend caches and rereads it.
- **Live / production-backed** requires explicitly setting `PRISM_PAPER_DATA_MODE=production`
  and reading the actual production DB. Its control is **Reread production database**.
  This is not a remote fetch or an indication that every run is healthy.
- **Stale** means snapshot/most recent paper update is older than 30 minutes or unknown;
  individual run staleness is checked against current wall time. A newer file mtime or
  new download never makes an old backup current.
- Without a provenance sidecar, snapshot creation time and last sync are unknown. The
  page uses the last ledger/evaluation as an explicit observation-time fallback. It never
  claims this fallback is the production backup creation time.

`uv run python scripts/pull_paper_snapshot.py` requires the existing Railway login/project
link. `PRISM_RAILWAY_SERVICE` defaults to `prism-runtime`; `PRISM_RAILWAY_VOLUME` defaults
to `prism-runtime-volume` for optional full downloads. The script:

1. Lists existing backups under `/data/backups/{daily,manual,weekly}` and selects the
   newest validated filename timestamp, regardless of directory.
2. Reads that immutable backup's checksum. It never creates a backup or checkpoints the
   live database.
3. By default, opens that backup **read-only**, in a consistent read transaction, and
   transfers compressed paper-table data. Only fired opportunities and relevant nimble
   account/entry/exit/funding events are needed; repetitive `position_mark` events are
   omitted because current position projections supply those fields. The allowlist also
   includes Phase 30's `paper_event_evidence` and `paper_event_pending` when present.
   Older backups may omit those optional tables and remain valid for historical reporting.
   No raw market, context histories, research registry or scientific verdict tables are retrieved.
4. Verifies transfer integrity, reconstructs **local-only** reporting tables, verifies
   required paper schemas, and atomically replaces the local snapshot.
5. Writes `<database>.snapshot.json`: original backup timestamp/path/checksum, transfer
   checksum, local file checksum/size/mtime binding, scope, event-evidence availability,
   and UTC download timestamp.

The compact copy is a reporting artifact, not a writable execution database or complete
backup. Policies, definitions and run identities are copied verbatim. File replacement
invalidates the Streamlit cache; a mismatched provenance sidecar is ignored. A failed
retrieval/checksum/schema check preserves the previous snapshot. Snapshot and sidecar
are replaced separately; an interruption can make provenance unknown, never falsely live.
For a full verified backup instead, use:

```bash
uv run python scripts/pull_paper_snapshot.py --full-backup
```

The legacy `deploy/railway/pull_backup.sh daily` also downloads an existing full backup,
but produces no new provenance sidecar; last sync is then N/A. Railway transfer can be
slow for large databases. The compact path was added to keep this workflow practical.
The frontend deliberately has no **Refresh from Railway** button: retrieval is explicit
in the terminal, not an implied reload or an embedded production command.

## Performance and deployment

Normalized latest trade payloads stay server-side; raw scientific/context blobs are
removed from the report cache. V2 reduces its ledger to one latest payload per entered
trade in SQL. Nimble uses its projections with one research/thesis join, avoiding a
per-trade research query. Opportunity histories are grouped in SQL; activity and
rejections are bounded. Charts bucket in SQL. Report construction is linear in entered
trade count; rendering is bounded by page/chart limits. Memory grows with normalized
trade count, not raw signal histories. Reports cache for 30 seconds with 24-entry bounds;
changing the source file size/mtime invalidates keys. Very large histories can still
require server-side ledger scans; there is no claim of constant query time.

The frontend is Python/Streamlit, not a JavaScript app: no npm lint/typecheck/build exists.
Validation uses AppTest interactions, Ruff lint/format, scoped Pyright checks, Python compilation, an HTTP
Streamlit smoke check, paper/reporting and Phase 27 compatibility tests, and full pytest.
Run the scoped frontend/report type check with:

```bash
uvx --from pyright pyright --project pyrightconfig.paper.json
```

The stress fixtures use 3,001 closed trades, 3,001 event evidence rows, and bounded
charts across 1,500 observations. Decision-page cache entries are limited to 64;
selected payload/event overview caches to 24, all with 30-second TTLs.

**No backend runtime deployment or database migration is required.** Do not redeploy or
restart the trading runtime to use this dashboard. Deploy the frontend checkout/package,
`uv sync --frozen`, retrieve its local snapshot, set `PRISM_PAPER_DB_PATH`, and run
`streamlit run dashboard/paper_app.py`. For a separate host, copy the verified snapshot
and its `.snapshot.json` together. Expose the Streamlit port through your existing
frontend hosting/authentication arrangement; no trading credentials are needed by the
web process. Railway credentials are only needed by the explicit retrieval command.

## Read-only safety and limitations

The page cannot create/open/close trades, pause runs, edit hypotheses/policies, change
risk/sizing/admission/funding, or recover/write execution state. Its only actions are view
selection, sorting/filtering, paging, and cache reread. All DB connections remain
read-only; even missing sources cannot trigger a writable fallback. Tests hash the
source DB before/after reporting and UI interactions and verify DuckDB refuses mutation.

Production retrieval executes only `find`, `sha256sum`, and a Python SELECT-only read
transaction against an existing backup (or Railway file download). It has no production
write path. Local snapshot files are the only files it replaces. Retrieval refuses an authoritative runtime and rejects the configured execution database or `/data/prism.duckdb` as destinations. No execution engine,
Phase 27 policy, run identity, risk/sizing/funding/admission, or Phase 30 work is modified.

Snapshots can be older than the latest production event, and data refresh remains manual.
Missing quotes, missing settlements, incomplete research paths, absent schemas and small
samples remain visible limitations. A custom window filters final stored closed-trade
outcomes; later funding corrections can revise a trade's outcome attributed to its exit
window. The cash curve retains the correction's actual settlement time instead.
