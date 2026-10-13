# Shared intelligence layer review

Architecture and research only. Reviewed 2026-10-13 against approved `main` and local
`origin/main`, both **`401301ff2924a3684a423139113bd8f6867e603d`** (runtime profile split).
Repository content was read from an immutable `git archive` of that commit. Existing
workspace edits and other worktrees were excluded. The active Risk / Position Sizing
Research v1 branch was neither inspected nor modified. This review adds this document
only: no implementation, tests, trading changes, deployment, Railway changes or OVH changes.

All proposed contracts, packages, workloads and migration phases below are future design,
unless explicitly described as existing. Repository evidence establishes implementation
and recorded historical measurements; it does not establish today's deployed configuration,
live data coverage, current scientific maturity or a production performance guarantee.

## Executive recommendation

**A shared intelligence layer is warranted and achievable, but the current boundary is
not clean enough to expose wholesale.** Phase 23 already supplies most of its foundation:
append-only events and updates, causal positioning, macro context, content-addressed
snapshots and research theses. Later autotrader work added valuable acquisition, reaction
tracking, market discovery and evidence machinery. Most reuse should happen by publishing
bounded, causal read models, not by making Research or Copilot call paper-trader adapters.

Prism should have one set of market facts and versioned analytical meanings, with multiple
consumers. It should not require one live process, one writable database, identical
acquisition latency on two hosts, or one thesis interpretation. Humans, deterministic
hypotheses and future LLM proposals can disagree while citing the same immutable evidence.

The recommended boundary is:

```text
Public sources + Work facts + market/macro observations
                         |
       Causal ledgers, source provenance, versioned definitions
                         |
     Shared read models: context, positioning, market state, radar
                         |
                 Frozen evidence packages
             /               |                \
       Research        Manual Copilot      Autotrader Lab
       studies         human decisions     policy admission
       simulations     journal/monitor     local execution state

Deterministic risk contracts are a separate shared service/library boundary.
Each consumer supplies its own inputs; execution enforcement belongs to the Lab.
```

Five conditions make this feasible:

1. Shared analytical readers must work without paper accounts, Nimble tables or runtime
   authority, including on a deliberately minimal intelligence snapshot.
2. Context observation time, commit/read availability and receiving-runtime availability
   must remain distinct. Reuse cannot backdate facts or revisions.
3. Radar attention ranking must be separated from execution eligibility and rich collection.
4. Thesis and outcome records must be mode-neutral; paper fills and manual trade receipts
   remain separate records with separate ownership.
5. Shared code must preserve `prism_research_runtime_v1` and
   `prism_autotrader_lab_v1`. New scheduled workloads or gates require later, explicit
   profile versions; this document authorizes no activation.

## Current reusable systems and audit evidence

### Context and event infrastructure

The [Phase 23 design](CONTEXT_INTELLIGENCE.md) remains the strongest starting point, but
its original statements that no paper consumer reads it are phase-time descriptions.
Approved main now consumes context through
[`paper/v2/data.py`](../src/market_signal/paper/v2/data.py), inherited Nimble adapters and
[`paper/nimble/events.py`](../src/market_signal/paper/nimble/events.py). Classification
below follows current code rather than old isolation claims.

| Capability | What exists on main | Reuse judgment and limits |
|---|---|---|
| Event/update model | [`context/model.py`](../src/market_signal/context/model.py), [`ledger.py`](../src/market_signal/context/ledger.py): logical first observation plus append-only corroborations, revisions, denials, resolutions, confidence and schedule/value updates | Universally reusable factual history. A folded event is a view at a cutoff, not mutable latest truth. Preserve each report as well as the fold. |
| Causal clocks | `first_seen_at`, update `observed_at`, `processed_at`, `published_at`, provider and externally reported discovery clocks; link `linked_at`; gateway/free-source receipt and `context_available_at` clocks | Universally reusable. Publication and external discovery are provenance, never permission to act before local receipt. The original ledger and later transport have different availability granularity; see the clock contract below. |
| Provenance | Source registry/type/tier, source references, provenance/raw hashes, archived raw payloads, observation mode, versioned mappings, receipt histories | Universally reusable and essential for explanation. The default event display view does not expose this whole manifest. |
| Dedup/update chains | Stable dedup keys, URL matching and bounded entity/category/time linking; identical report hashes are idempotent | Reusable, with qualification: heuristic keyless linking is not guaranteed semantic identity across independently ingesting hosts. Preserve report IDs and explicit later links; do not merge only because titles look similar. |
| Materiality/severity | [`taxonomy.py`](../src/market_signal/context/taxonomy.py) deterministic relevance score using category, scope, confidence, mapping and loss estimate | Human-readable if components and definition version are exposed. Relevance is neither market direction, truth probability nor profitable edge. Provider incident impact is distinct from derived materiality severity. |
| Asset mappings | [`entities.py`](../src/market_signal/context/entities.py), [`entities.yaml`](../config/context/entities.yaml): direct/ecosystem/market-wide links, mapping version and link knowledge time | Shared. Venue listings do not automatically affect the venue token; unknown assets need registration and coverage checks. Mapping is broader than the six execution assets. |
| External macro/context | Calendar, ALFRED/FRED observations and release updates; RSS; Hyperliquid listing discovery; imports | Shared, with missingness. Macro consensus often requires an import; taxonomy membership does not imply an installed data source. No generic real-time terminal, gold feed or comprehensive catalyst corpus exists. |
| Work semantic events | [`gateway/contract.py`](../src/market_signal/context/gateway/contract.py), gateway spools/ingest, [`work_ingest.py`](../src/market_signal/context/work_ingest.py), [`work_monitors.py`](../src/market_signal/context/work_monitors.py) | Shared facts and sender provenance. Work reports remain `ai_monitor` with confidence capped at REPORTED; citing an official URL does not turn them into official observations. Current gateway rejects trading/sentiment/interpretation fields. Monitor selection is tuned toward immediate impact, so it is not a complete slow-catalyst feed. |
| Free-source ingestion | [`context/free_sources/`](../src/market_signal/context/free_sources/), [registry](../config/context/free_sources.v1.yaml), [source review](FREE_EVENT_SOURCES.md): status, RSS, GitHub releases/advisories; 18 selected feeds, disabled broad-discovery candidate | Shared acquisition with independent local spools, hashes, conditional requests, rate limits and source health. Broad news, social sentiment and token-unlock completeness are not supplied by this registry. |
| Notifications/reactions | Gateway/free ingest call [`ops/evaluation_triggers.py`](../src/market_signal/ops/evaluation_triggers.py); [`work_research.py`](../src/market_signal/context/work_research.py) enrolls descriptive reactions | Should be split. Commit notifications are useful to all modes, but currently write `paper_nimble_triggers` and a paper notice file. Reaction science is reusable, but the present measurement requires minute microstructure and short horizons. |

The ledger itself does not need an active paper account. The ingestion wrapper's outbox
does touch paper-named state even when no execution consumer runs. This is a real dependency,
not merely a naming issue. A future neutral committed-information stream should feed
mode-owned adapters; the current receipt/transaction and retry guarantees must survive.

### Snapshot and point-in-time limitations

[`context/snapshot.py`](../src/market_signal/context/snapshot.py) provides
`context_snapshot(asset, t, record=False)`, `ContextIndex`, operation-local reuse,
`market_context` and an information-only brief. The snapshot has a content hash, active
events, up to 25 recent events, up to 10 upcoming linked events, macro, positioning,
hourly activity and freshness warnings. Recording it is an explicit append operation.

Four boundaries need strengthening before calling this a complete intelligence contract:

- **The display view is lossy.** `event_view` includes event clocks, confidence, materiality
  and a source count, but omits full source URLs, report/update IDs, attributes,
  attribute-level knowledge clocks and raw hashes. A thesis package must resolve an
  evidence manifest from the ledger; a source count alone cannot support citations.
- **Market breadth bypasses revisions.** `market_context` queries current
  `perp_intraday_bars` directly with a `first_observed_at` cutoff. Later value revisions
  remain visible in that query. The revision-aware
  [`intraday/bars.py:load_bars`](../src/market_signal/intraday/bars.py) already reconstructs
  prior values and is used by activity calculations. Future snapshot breadth must use
  that causal path. This review identifies the gap; it makes no fix or claim that a
  synthetic counterexample was executed.
- **Definition identity is incomplete for general replay.** Snapshot version and some
  downstream rule versions exist, but a future public manifest must pin all analytical
  definitions, mappings, software and selection/truncation rules. Recomputing under new
  code is not guaranteed to reproduce a prior snapshot merely because history is retained.
- **Horizon and freshness are narrow.** Recent events default to seven days; event anchors
  are bounded to roughly the prior 30 days and next 45; activity is hourly; market breadth
  is six-asset 24h breadth. Provider freshness is only included when stale thresholds are
  supplied. These defaults do not constitute a weeks/months dossier or universal health
  assessment. Long-lived older event chains can fall out of the bounded view.

Snapshot content identity protects an already frozen payload. It is not proof that every
query composing that payload is causal. Validate both properties independently.

### Positioning

[`context/positioning.py`](../src/market_signal/context/positioning.py) combines factual
collection and analytical functions in one module. It exposes:

- Hyperliquid fixed-hour OI captures plus legacy irregular snapshots, with base-contract
  OI, notional, mark/oracle basis and predicted funding.
- Binance hourly OI and account/top-trader/taker-volume ratios, filtered by period end
  and `ingested_at` in strict mode. Assumed-latency research is explicitly separate.
- OI changes over 1h/4h/24h, 30-day level percentile and a standardized 24h change with
  minimum-history checks.
- Settled funding 24h mean, a percentile of daily rolling means over up to 90 days
  (120 days are loaded), annualized rate and observed settlement count.
- `crowding_v1` and `vulnerability_v1`, with explicit ingredients and unknowns. OI and
  ratios older than three hours are withheld. Funding coverage needs its own review;
  the presence of a 24h mean does not prove a complete settlement path.

Venues are kept separate. Aggregate OI always has equal long and short size; it cannot
identify which side owns the contracts or actual liquidation positions. Funding/ratios
can describe crowded-long-like or crowded-short-like conditions. Annualization is a
rate convention, not a promised return or long-term carry forecast.

[`research/oiprice/primitives.py`](../src/market_signal/research/oiprice/primitives.py)
adds gap-aware OI/price/funding relationships; the Phase 19 study is exploratory and
explicitly reports no demonstrated incremental OI edge beyond price in its reviewed
window ([study](PHASE19_OI_PRICE.md)).
[`research/relative/primitives.py`](../src/market_signal/research/relative/primitives.py)
adds BTC-relative, residual and cross-sectional state; Phase 18 did not establish a
stable tradable relative-strength edge ([study](PHASE18_RELATIVE_STRENGTH.md)). Reuse
the descriptive primitives and their evidence labels, not an invented directional signal.

Volume acceleration is not one interchangeable field: context activity uses closed-hour
volume relative to a prior mean; radar `volume_acceleration` compares rolling 24h venue
volume captures; microstructure records actual minute flow. The radar ratio is not
incremental volume or a validated acceleration forecast.

### Dynamic Universe / Market Radar

The [Dynamic Hyperliquid Universe design](DYNAMIC_HYPERLIQUID_UNIVERSE_V1.md) has discovery,
bounded light sampling, pure ranking, rich warming, promotion/demotion, risk pins,
portability audits and prospective outcomes. **It is prepared, not activated by either
explicit v1 profile.** Research declares all DU tiers absent; Lab declares them gated.
Turning on `ranking` or `warming` under those profiles currently refuses startup.

The attractive reuse is Tier 0 discovery and selected Tier 1 market measurements:
venue contract inventory, active/inactive changes, prospective first sighting,
price/movement, rolling volume, OI/base OI change, funding, sampled spread/depth,
activity and relative cross-sectional ranks. Missing book/candle measurements must
remain missing. Native and HIP-3/deployer markets must keep explicit capability labels;
quarantine for bot execution need not make a market invisible to research.

The current ranking is not a generic opportunity score:

- [`universe/engine.py`](../src/market_signal/universe/engine.py) has pure market inputs,
  but its score includes execution/liquidity terms, hard gates, short RV/cost economics
  and completeness. It measures short-horizon tradeability/attention, not 1–6 month return.
- [`universe/source.py`](../src/market_signal/universe/source.py) `prepare` and `commit`
  call `ledger.risk_pins`; commit also runs hypothesis availability audits. Even a
  non-warming refresh follows execution-related orchestration.
- [`universe/ledger.py`](../src/market_signal/universe/ledger.py) `risk_pins` reads Nimble
  positions, pending research, event lifecycle and Paper v2 state; `status` consults
  dynamic paper run/account activity. The dashboard reader is consequently not an
  intelligence-only query.
- Ranking weights do not read equity, PnL or available capital, but pins influence light
  collection obligations and rich selection. Separate pure ranking from the inputs
  selected because of account obligations.

Share raw discovery and independently versioned movement/participation/positioning/liquidity
components. Publish a descriptive radar rank and coverage explanation. If its cohort,
missing-value treatment or gates differ from frozen DU v1, give it a new definition;
never relabel changed output as the existing experimental score.

### Microstructure

[`microstructure/`](../src/market_signal/microstructure/) supplies a DB-free public
collector, spools, minute aggregation, revision history, coverage/health, causal loaders,
spread/depth, signed taker flow, print grouping and prior-day large-print calibration
([Phase 24A](MICROSTRUCTURE_COLLECTION.md)). Raw addresses are dropped; full raw books
are not retained. Raw collection supports science as well as the bot, so it is not
conceptually execution-only. Its rich operational cadence is expensive to reproduce
for every human-facing asset.

[`research/microdir/`](../src/market_signal/research/microdir/) supplies Phase 24B
15-minute flow/response/absorption research, a primary next-hour outcome, quality and
prior-complete-window normalization, preregistration, contrasts and maturity/verdict
rules ([study](MICROSTRUCTURE_DIRECTION_STUDY.md)). A replenishment proxy can also count
cancel/re-add flicker. Absorption is a tested hypothesis/state, not proof of informed
passive buying or selling. The original study doc's scheduling description is historical:
main now has research-profile daily/weekly checkpoints.

| Exposure | Research | Manual Copilot | Autotrader Lab |
|---|---|---|---|
| Raw public event journal / collector internals | Offline replay/calibration where retained; restricted study artifact | No default endpoint; no wallet narratives | Local collection and diagnostic replay |
| Complete minute primitives and revision/coverage manifest | Read-only historical study inputs | Optional bounded preparation/review summary for covered assets | Local causal adapters |
| Spread/depth | Liquidity/cost studies by venue and sampling method | Dated liquidity summary; sampled depth is not guaranteed fill capacity | Fresh quote checks and execution economics |
| Large prints | Descriptive study with warmup, threshold version and venue | Optional unusual-size context, with coverage; never a whale-intent claim | Exact frozen predicate inputs where required |
| Flow/absorption | Phase 24B science and separate horizon tests | Optional near-entry context for minutes/hours; hidden by default for weeks/months | Exact hypothesis/state checks |
| Rich subscriptions, fast quote cache, warmup segments and pins | Collection provenance available in exported datasets | No operational controls | Lab-owned subscription/execution lifecycle |

The immediate proposal does not stop Railway's existing core-six collector or move the
governed Phase 24B study. Longer-term offload requires preservation of its source cutover,
history, checkpoint authority and collection continuity; an OVH export cannot silently
pretend to be a continuation of Railway's dataset.

### Evidence, prospective tracking and existing human workflows

Prism already has more than paper-trade evidence:

- [`research/lab/`](../src/market_signal/research/lab/) freezes strategy/evaluation
  definitions, content-addressed dataset selections, software identities, evidence profiles,
  validation and forward enrollment. These govern evidence without needing a paper account.
- [`research/lifecycle/`](../src/market_signal/research/lifecycle/) and
  [`research/incubation/`](../src/market_signal/research/incubation/) track changing evidence,
  candidate episodes, opportunity rates and prospective outcomes. Their admission labels
  remain research-policy judgments rather than generic manual-trade instructions.
- [`context/event_study.py`](../src/market_signal/context/event_study.py) measures
  event returns, BTC-adjusted reactions, activity and positioning changes. It uses final
  historical bars for descriptive outcomes and also for its reference lookup; it is not
  a fully causal entry-reference builder. Its default horizons are 15/60/240/1440 minutes.
- [`context/work_research.py`](../src/market_signal/context/work_research.py) already
  enrolls Work **and free-source** event/asset pairs independent of paper fills and records
  1/5/15/30/60/120/240-minute reactions, MFE/MAE, missingness and receipt attribution. It
  requires complete minute data and a causal pre-availability reference. Mapping XMR/ZEC/BNB
  or USDC does not create market-data coverage or an outcome for them.
- [`paper/nimble/engine.py`](../src/market_signal/paper/nimble/engine.py),
  [`report.py`](../src/market_signal/paper/nimble/report.py) and
  [`events.py`](../src/market_signal/paper/nimble/events.py) preserve frozen execution
  theses, opportunity/admission/rejection evidence, latency, entry/exit receipts and
  independent forward curves after exit, including rejected opportunities with valid
  references. Cohort/run identities preserve baseline and immediate/confirmed differences.
- [`copilot/engine.py`](../src/market_signal/copilot/engine.py) already provides
  governed daily-perp strategy watches, ALERT/SUPPRESS decisions, evidence caveats and
  separate delivery attempts. This is an existing narrow signal-watch product, not a
  general manual thesis/position assistant.
- [`portfolio/book.py`](../src/market_signal/portfolio/book.py) already stores manually
  entered `paper`/`real` positions and journals, daily-bar excursions and R statistics.
  It is predominantly long-oriented, uses entry/exit dates and text evidence, and mutates
  position projections. It does not freeze a causal entry evidence manifest. Full entry-day
  bars can include pre-entry extrema; daily excursions cannot become intraday precision.

Reusable scientific contracts include causal reference selection, independent outcome
enrollment, pending/missing states, dataset/definition identity, explicit costs and run
attribution. Paper tables and state machines cannot simply become the manual journal.
Distinguish descriptive event reaction, thesis outcome, hypothetical strategy result and
actual manual or simulated fill result in every view.

## Top five transfers and reuse priority

Ratings are relative engineering judgments for this repository, not measured delivery
estimates. Complexity: low = reader/contract wrapper; medium = targeted separation and new
projection; high = multiple state/authority migrations. Cost means incremental runtime cost.

| Rank | Transfer | User value | Effort | Infrastructure cost | Scientific value | Coupling risk | Why first |
|---|---|---|---|---|---|---|---|
| 1 | Causal context snapshot plus resolvable evidence manifest and event-chain diff | Very high | Medium | Low, bounded/on demand | Very high | Low–medium | Immediately answers what changed, what was known and where it came from; fixes the main explanation boundary. |
| 2 | Per-venue positioning summary with daily/weekly persistence projections | High | Low–medium | Low | High | Low | OI/funding/crowding already exist independently of execution and help dossiers, carry assessment and review. Longer windows need new definitions and coverage. |
| 3 | Cheap market discovery/radar, isolated from DU lifecycle | High | Medium | Low–medium | High | Medium | Expands attention beyond six assets without warming a bot universe; raw fields and pure rank primitives already exist. |
| 4 | Neutral thesis/evidence enrollment and outcome path contracts | Very high | Medium–high | Low at manual scale | Very high | Medium–high | Converts frozen context and existing reaction/forward machinery into manual thesis tracking and honest post-trade review. |
| 5 | Event acquisition/update chain and source-quality views for slower catalysts | High | Low–medium | Low using existing collectors | High | Medium at outbox boundary | Reuses Work/free acquisition and source histories for event feed and catalyst evolution; broaden sensor coverage later without adding an interpretation engine. |

Risk interface alignment is important but intentionally not a transfer implementation here;
the active risk research must complete separately. Optional microstructure summaries rank
below these five because coverage is narrow, horizons are short and storage can dominate.

## Shared vs mode-specific matrix

Locations below are relative to `src/market_signal/` unless prefixed otherwise. Consumers
are current main paths, including prepared/gated paths, not claims of active deployment.
Each row has exactly one proposed classification. `SHOULD_BE_SPLIT` identifies a currently
mixed subsystem; the intended pieces appear in the refactor map.

| Subsystem | Current location | Current consumers | Proposed classification | Coupling concerns / justification | Migration complexity |
|---|---|---|---|---|---|
| UTC/model validation, canonical JSON and content identity | `research/lab/common.py`, `models/domain.py` | Context, Lab, copilot, paper, universe | SHARED_CORE | Basic contracts currently live under a research package; extract only neutral primitives and preserve serialized identities. | Low–medium |
| Bars, funding, OI and PIT/revision readers | `data/pit.py`, `data/prices.py`, `intraday/bars.py`, `perps/data.py`, `perps/open_interest.py` | Research, scans, context, paper adapters | SHARED_WITH_MODE_ADAPTER | Availability conventions differ by dataset/consumer; collectors write, readers must be independently read-only. Do not assume all historical prices have prospective availability. | Medium |
| Context observation/event ledger | `context/model.py`, `ledger.py` | Providers, gateway/free ingest, snapshot, event research, paper events | SHARED_CORE | Facts and folds are reusable; depend on a storage reader contract, not monolithic runtime state. | Low–medium |
| Taxonomy, confidence, materiality, entity links | `context/taxonomy.py`, `entities.py`, `config/context/` | Context, paper event classification, research | SHARED_CORE | Freeze definitions/mapping provenance; materiality is relevance only. | Low |
| Macro calendar/surprise and PIT macro state | `context/macro.py`, `providers/calendar.py`, `data/pit.py`, `regimes/engine.py` | Context, research/scans, paper snapshots | SHARED_WITH_MODE_ADAPTER | Calendar facts shared; selected state windows, asset sensitivity and regime definitions are horizon-specific. | Medium |
| Source acquisition and normalized adapters | `context/providers/`, `service.py`, `free_sources/{collector,parsers,registry,spool,health}.py` | Scheduled data jobs and context ingestion | SHARED_WITH_MODE_ADAPTER | Same normalization, independent host receipts/spools/rate state; source classes cannot be raised by consumers. | Low–medium |
| Work gateway transport and semantic receipts | `context/gateway/{auth,contract,server,spool}.py`, `work_ingest.py`, `work_monitors.py` | Railway ingress, context, source/reaction reporting | SHARED_WITH_MODE_ADAPTER | Railway-owned ingress; factual contract shared, monitor policies/transport separate. No LLM thesis through fact ingress. | Medium |
| Authoritative gateway/free-source commit orchestration | `context/gateway/ingest.py`, `free_sources/ingest.py`, workers | Context writer, reaction enrollment, Nimble outbox | SHOULD_BE_SPLIT | Core commit/receipt semantics shared; neutral notification separated from execution wakeup and reaction subscription. | Medium |
| Fast evaluator trigger consumption | `ops/evaluation_triggers.py`, `paper/nimble/triggers.py` | Context/micro producers, Nimble worker | SHOULD_BE_SPLIT | Generic information publication shared; `paper_nimble_*` queues, run acknowledgments and minute fallback are execution adapter state. | Medium |
| Context snapshot, brief and caches | `context/snapshot.py` | CLI, thesis builder, research/microdir, v2/Nimble | SHARED_WITH_MODE_ADAPTER | Causal market-loader gap, lossy provenance, fixed six-market breadth and short defaults; on-demand reads cannot implicitly record. | Medium |
| Positioning collection and summaries | `context/positioning.py` | Context refresh/snapshots, research, paper adapters | SHOULD_BE_SPLIT | Collection writer and pure crowding/summary calculations share a file; retain venue/period/availability distinctions. | Low–medium |
| Hourly activity state | `context/opportunity.py`, `research/discovery/primitives.py` | Context, paper attribution, discovery research | SHARED_WITH_MODE_ADAPTER | Frozen hourly range/volume/RV facts shared; slower aggregates get new definitions, not altered thresholds. | Low–medium |
| Relative and OI/price descriptive primitives | `research/relative/primitives.py`, `research/oiprice/primitives.py` | Their studies, paper positioning adapters | SHOULD_BE_SPLIT | Features reusable; assumed-latency grids, forward returns and entry-index study helpers remain research. | Medium |
| Relative/OI/intraday/structure study harnesses | `research/{relative,oiprice}/study/`, `research/discovery/`, `research/structure/study/` | Lab/CLI research and governed candidate tooling | RESEARCH_ONLY | Statistical tests, calibration and verdicts are science, not dossier runtime dependencies. Publish evidence read models. | Low for exports; no wholesale move |
| Discovery metadata and cheap market measurements | `universe/source.py:{decode,book_metrics,enrich_history}` | Gated DU source/ranking | SHARED_WITH_MODE_ADAPTER | Provider facts and quality reusable; sampling request budgets differ. Current orchestration cannot be the shared adapter. | Medium |
| DU score, gates and orchestration | `universe/engine.py`, `policy.py`, `source.py:{prepare,fetch,commit}` | Gated DU worker, dynamic paper integration | SHOULD_BE_SPLIT | Pure components shared; execution score/gates, warming obligations and tier state stay Lab. New radar policy needed. | Medium–high |
| Universe storage/status/outcomes | `universe/ledger.py`, `dashboard/views/universe.py` | Radar dashboard/CLI, DU worker | SHOULD_BE_SPLIT | `risk_pins` and `status` read paper state; split neutral history/read rank from execution status and short forward studies. | Medium |
| Rich universe warming/portability/subscriptions | `universe/rich.py`, `execution.py`, `portability.py`, `subscriptions.py`, `budget.py` | Dynamic Lab adapters and collector | AUTOTRADER_ONLY | Entry readiness, promotion/demotion, exit/accounting/outcome pins and subscription allocation. Shared readers may see provenance, never drive this state. | Leave in place |
| Microstructure collector/minute/query definitions | `microstructure/{aggregate,engine,definitions,ws,spool,ingest,query,health}.py` | Both v1 data profiles, microdir, paper | SHARED_WITH_MODE_ADAPTER | Common dataset semantics; host authority/cutovers and collection cadence separate. Rich collection is not required for a dossier. | Medium for exports |
| Live executable quote cache | `microstructure/live.py`, `paper/nimble/data.py` quote helpers | Collector, Nimble worker | AUTOTRADER_ONLY | Fresh executable BBO, session/runtime identity and post-intent checks cannot be a general context field. | Leave in place |
| Phase 24B research and checkpoints | `research/microdir/` | Research scheduler/CLI; features reused by paper adapters | SHOULD_BE_SPLIT | Feature definitions can be shared; inference/calibration/checkpoint authority research-only, execution predicate adapters Lab-only. | Medium |
| Descriptive event-study/reaction metrics | `context/event_study.py`, `work_research.py` | CLI/research, gateway/free workers, source dashboards | SHARED_WITH_MODE_ADAPTER | Outcome primitives reusable; final-history versus causal references and minute versus slow bars must be explicit. | Medium |
| Definition, dataset, software and evidence identity | `research/lab/{spec,datasets,provenance,evidence,ledger}.py` | Studies, forward, copilot, candidate/paper governance | SHARED_WITH_MODE_ADAPTER | Readable definitions/evidence shared; research registration authority and promotion remain research-owned. Dataset kind registry currently lacks general context/positioning/radar package capture. | Medium |
| Scientific forward/lifecycle/incubation evaluation | `research/lab/forward.py`, `research/lifecycle/`, `research/incubation/` | Research runtime, copilot evidence, candidate reports | RESEARCH_ONLY | Episode/admission/maturity policies are governed research machinery. Generalize path contracts, not automatic enrollment of human ideas into validated science. | Medium for evidence readers |
| Research thesis v1 and future general thesis | `context/thesis.py`, `context_theses` | Context CLI/manual or AI proposal validation; no order consumer | SHOULD_BE_SPLIT | Existing research-only status and 30-day expiry are frozen; general multi-mode lifecycle needs separate version and ownership. | Medium |
| Nimble execution thesis, intents, fills and exits | `paper/nimble/{model,engine,events,spec,event_spec,worker}.py` | Paper Lab and paper reports | AUTOTRADER_ONLY | ExecutionThesis contains objectives, max holds and execution policy. It is not the general thesis object. | Leave in place |
| Paper opportunity/forward/attribution records | `paper/nimble/engine.py`, reports; `paper_nimble_research*`, `paper_event_evidence` | Nimble worker, event/paper dashboard | SHOULD_BE_SPLIT | Neutral outcome calculations/exports reusable; paper run enrollment, admission and fill receipts stay Lab. Preserve existing ledgers. | Medium–high |
| Legacy paper accounts, risk/execution and retired v2 drain | `paper/{account,risk,execution,policy}.py`, `paper/v2/`, `perps/paper.py` | Paper engines; research-profile legacy drains | AUTOTRADER_ONLY | Operational execution category even while frozen drains remain on Railway. No transfer to manual intelligence; do not interrupt drains. | Leave in place |
| Existing manual sizing suggestion and future shared risk contract | `scoring/risk.py`, `paper/risk.py`, Nimble policy/engine | Scans/presenter, paper admission | SHOULD_BE_SPLIT | Current models differ. Future pure deterministic risk contract needs mode adapters; no proposed formulas, implementation or active-branch review. | Contract design only; later coordinated work |
| Existing daily strategy Copilot | `copilot/{engine,policy,render}.py` | Research prospective job, CLI, Telegram delivery | COPILOT_ONLY | Watch policy and delivery are human-mode decisions; consumes shared research evidence. Do not reinterpret WATCH as bot admission. | Low–medium to add context consumption |
| Manual portfolio/journal and monitoring | `portfolio/{book,alerts}.py`, portfolio CLI/dashboard | Human-entered books/review | COPILOT_ONLY | Long/date/text limitations; manual receipts and mutable projections need future append evidence, not paper account reuse. | Medium–high |
| Dashboard data access / projected read API | `dashboard/common.py`, `paper_data.py`, views; `paper/dashboard/` | Streamlit/CLI reports | SHOULD_BE_SPLIT | Neutral intelligence reads separated from paper reporting and write-capable helpers; immutable exports for external clients. | Medium |
| Runtime profiles/locks/authority and backups | `ops/{profiles,runtime,lab,backups}.py`, `deploy/` | Host processes, scheduler, seed/rehearsal | SHARED_WITH_MODE_ADAPTER | Common tooling, separate profile identities/claims/state. Monolithic Store/schema is not a shared writable DB contract. | Low for continued split; high for state migration |

`SHARED_CORE` means common semantics and library contracts, not universally available data
or a permanent process on both hosts. `SHARED_WITH_MODE_ADAPTER` means all modes can reuse
the factual/analytical capability while retaining explicit availability, horizon and host
adapters. `RESEARCH_ONLY` results may still be displayed through an evidence projection.

## Context pipeline reuse for slower/manual trades

### Clock contract: preserve observation and actual consumer access

Use an envelope containing `origin_runtime_id`, `source_report_id`, `event_id`, optional
`update_id`, `source_published_at`, `source_revision_at`, `external_discovered_at`,
`origin_observed_at`, `processed_at`, `origin_context_available_at`,
`received_at_here`, `available_at_here` and `query_cutoff`. Null means unobserved.

Existing Phase 23 `first_seen_at`/update `observed_at` answer when the origin observed
a fact. Later gateway/free-source `context_available_at` answers when authoritative
ingestion made it available to downstream consumers. For new runtime-consumable packages,
eligibility uses the selected access basis and every included input's applicable clock:
`available_at_here <= query_cutoff`. Preserve original fields and frozen historical
definitions; do not rewrite prior evidence under a stronger new basis.

For local data, use ingestion/closed-period/revision availability as applicable. For
replicated data, origin sighting is provenance; the Lab can only use a report after local
durable receipt/application. A transfer today cannot enroll a Lab opportunity yesterday.
Queries explicitly select **origin-observed history**, **origin-committed history** or
**consumer-available history**; exploratory publication/assumed-latency studies remain
labeled separately. Human entry reconstruction uses the consumer path actually accessible
at entry, not the earliest timestamp anywhere in Prism.

### Proposed Market Context Snapshot

This is an evolution of `context_snapshot_v1`, not a new parallel market interpretation.
The new version should be a structured, deterministic object with:

| Part | Required content | Existing foundation / missing work |
|---|---|---|
| Identity | Snapshot/content ID, schema/definition versions, asset/venue instrument IDs, `as_of`, access basis, query cutoff, runtime/source dataset manifests | Content hashes exist; add comprehensive manifests and explicit consumer access basis. |
| Selection | Horizon definition, time windows, asset links, market cohort, completeness/omission counts and paginated continuation | Existing bounded lists; make selection/truncation explicit and keep full evidence resolvable. |
| Events | Relevant active/recent/upcoming episodes plus new report changes since prior snapshot, confidence transitions, denials/resolutions, each report's citations | Existing folds/links; add report-level manifest and horizon-aware unresolved-chain selection. |
| Macro | Known calendar, surprise components where actually available, FRED vintage context, declared freshness/precision and missing sources | Exists; no implied consensus or causal direction when unavailable. |
| Positioning | Per-venue OI/change, funding/coverage, ratio/basis, crowding ingredients and versioned persistence summaries | Most immediate fields exist; daily/weekly features are new derivations. |
| Relative market state | Known price/volume/RV state, reference-relative strength, versioned breadth cohort and regime observations | Hourly activity/six-market breadth and study primitives exist; causal integration and longer horizons needed. |
| Liquidity | Venue, sampled time, spread/depth definition, coverage, stale/unknown status | DU sampled inputs or bounded micro summaries; no execution certification. |
| Contradictions | Deterministic observed disagreements with evidence IDs: denied claim, opposing source facts, venue positioning disagreement | No general contradiction service exists. Start with explicit factual conflicts; thesis-dependent counterevidence is separate. |
| Freshness/provenance | Per-field market time, observation/availability, age, coverage, source/report/raw hashes, versions and missing reasons | Data largely retained in ledgers; current snapshot exposes only part and may omit provider health. |

The snapshot reports market facts. It cannot answer whether a thesis is valid without
the user's thesis rules. It must expose insufficient coverage rather than fill gaps with
prose. Changing a relevance policy does not alter the underlying event history.

### Proposed Thesis Context Package

A compact causal packet references one snapshot and a selected immutable evidence manifest:

- Asset/instrument, horizon, cutoff/access basis and package definition identity.
- Selected event reports/update chains and catalyst schedule; factual claims separated from
  provider or proposer interpretations.
- Positioning persistence/carry inputs, relative/regime/activity state and optional dated
  liquidity/micro summaries appropriate to that horizon.
- Supporting evidence and contradictory evidence **relative to an explicitly supplied
  thesis or question**; without one, report observed disagreements only.
- Historical evidence references containing study/hypothesis/version/run, venue, tested
  horizon, date range, coverage, maturity, verdict and known limitations.
- Source report IDs/URLs/hashes, missingness, omitted evidence counts and resolver IDs.

Do not include later returns, revised facts or improved confidence in entry evidence.
Keep outcomes as a separate linked package with its own cutoff. Small packets use references
and bounded excerpts; source facts remain retrievable from immutable archives. Research,
human displays and future reasoning all receive the same packet definition.

Examples: a multi-day SOL preparation package can show a known halt/resolution chain,
venue-specific funding/OI and observed relative weakness without proposing a short. A
multi-week dossier can show persistent carry and a known macro release schedule without
claiming that one minute's flow predicts the week. Neither requires a paper account.

## Positioning reuse and horizon-aware intelligence

| Workflow | Existing support | Recommended shared consumption / qualification |
|---|---|---|
| Slow manual trade | Per-venue OI, 24h funding, percentile, basis/ratios, crowding/stress | Show funding persistence and historical settlement distributions over declared days/weeks, OI trends and disagreement. Separate modeled carry scenarios from actual settled cost. |
| Medium-horizon analysis | 1h/4h/24h OI changes, OI/price primitives, relative primitives | Derive 7d/30d changes and persistence from known history, with coverage and elapsed-time rules. No synthetic history for missed Hyperliquid captures. |
| Asset dossier | Descriptive leverage/skew evidence, activity and macro/catalyst proximity | Summarize each venue separately and compare labels/inputs, never pool account ratios or base OI blindly across different instruments. |
| Trade preparation | Current signed funding, settled history, OI/price context and liquidity samples | Supply dated cost/risk inputs; annualized rate is descriptive, settlement cadence is explicit, missing funding cannot become zero carry. |
| Position monitor | New captures and snapshots | Compare entry/current packages; flag deteriorating conditions or unknowns against frozen thesis criteria. No automatic order or sizing changes. |
| Post-trade review | Retained positioning observations and paper funding attribution patterns | Replay entry knowledge, later OI/funding evolution and actual/manual or modeled costs separately. Explain the association; do not assert that crowding caused the outcome. |

Introduce one horizon definition rather than duplicate collections:

| Horizon view | Typical selection/cadence | Meaningful existing inputs | What requires new validation |
|---|---|---|---|
| Fast: minutes/hours | Minute/15m state, event-triggered updates | Phase 24A/24B, current OI/crowding, event availability, spread | Existing frozen horizons/quality still apply; no rolling-window substitutions. |
| Daily: days | Hourly positioning retained, dossier hourly/on demand, daily summaries | Settled funding, 24h OI/activity, event chains, daily bars, macro | Multi-day persistence/carry/regime definitions and outcome paths. |
| Weekly/macro: weeks/months | Daily/weekly projections, known future calendar and unresolved episodes | Macro vintages, longer price/regime history, persistent funding/OI when collected, significant catalyst changes | 1–6 month ranking/thesis outcome studies, adequate prospective OI/events, long-horizon freshness and analogues. |

Each definition contains intended holding horizon, sampling interval, lookback, minimum
coverage, maximum input age, aggregation rules, normalization window, event relevance and
outcome schedule. Horizon is separate from collection cadence, thesis expiry and execution
maximum hold. Calendar months need declared calendar arithmetic rather than pretending
all months equal 30 days. Never loosen short-horizon data freshness simply because a trade
is slow: show old known facts as historical and current state as unavailable.

Signals that transfer most credibly to longer horizons are observed funding persistence,
base OI trends, macro/vintage changes, catalyst/update chains, relative strength and
volatility/liquidity distributions. They are useful descriptive conditioning inputs; their
directional predictive value must earn horizon-specific evidence.

Do not naively carry 15-minute absorption, a single book imbalance, a large print, one-hour
OI shock, two-second BBO, short reaction freshness, one-hour RV/cost gate or three-hour
activity rule into a weeks/months thesis. Historical funding percentiles are relative
to observed history, not an absolute crowded-side truth. Cross-asset panels must declare
the cohort known at the time and preserve missing/delisted assets to avoid survivorship.

## Market Radar reuse: cheap/shared and expensive/Lab

Proposed shared radar returns **observed attention/opportunity context**, with component
ranks, timestamp, cohort, exclusions, coverage and source lineage. It should support
sorting movement, participation, positioning and liquidity separately before any optional
newly versioned composite score. Absence of a bot-eligible hypothesis is not a reason to
hide an interesting market from a human.

| Shared cheap capability | Operational bound | Lab-owned counterpart |
|---|---|---|
| Public market inventory and active/inactive/listing-first-seen history | Batch metadata; retain lifecycle history and native/HIP-3 distinctions | Supported execution contract, leverage/lot requirements |
| Mark movement, sampled RV/range, volume/OI/funding and component ranks | 10–30 minute latest projection; bounded historical checkpoints | Frozen DU short-horizon economic gates and promotion score |
| Light spread/depth/trade-frequency samples | Bounded REST top shortlist; no all-market websockets | Rich satellite warming, full minute microstructure and high-frequency quote cache |
| Relative strength/activity summaries from already covered bars | No automatic acquisition of long history for every discovered asset | Hypothesis-specific technical/micro/positioning/funding readiness |
| Historical attention/outcome exports | Explicit sampling resolution and missingness; scheduled research enrollment only | Account-dependent priority, pending/open/settlement/outcome pins |

A 1–6 month opportunity radar is a distinct horizon projection using longer context,
macro, known catalysts and slower market/positioning trends. Current DU ranking alone
does not support that claim. Start with an annotated slow watchlist and preregister longer
outcomes; do not market a ten-minute tradeability score as long-term opportunity quality.

## Thesis model and lifecycle

Use four distinct objects and explicit relationships:

```text
HypothesisDefinition (rule/question, version, preregistration)
       | zero or many evaluated observations
       v
ThesisRevision <---- EvidencePackage / ContextSnapshot
       | optional relationship to hypothesis; origin human/rule/LLM
       | zero or many recorded positions or simulations
       +--> ManualPositionRecord --> ManualTradeReceipts / Journal
       +--> ExecutionThesis --> PaperRun --> Intent / Fill / Close
       +--> ResearchEnrollment --> DescriptiveOutcome / SimulationResult
```

A human thesis need not reference a registered hypothesis. A deterministic hypothesis
can generate many thesis instances or none. A future LLM proposes an interpretation;
it does not preregister a validated strategy by producing text. A thesis without a trade
is still trackable. A paper trade is an execution result, not the thesis itself. One thesis
may lead to several position episodes; do not silently pool these as independent evidence.

The proposed general model is a new version beside existing `trade_thesis_v1`
(`research_only`, maximum 30-day life) and `execution_thesis_v1` (maximum 480-minute hold):

| Field group | Proposed requirements |
|---|---|
| Identity | Stable `thesis_id`, immutable `revision_id`/content hash, parent revision, schema/definition version; append revisions rather than editing rationale. |
| Clocks | `created_at`, `recorded_at`, evidence `as_of`/query cutoff/access basis; distinguish a thesis actually written at entry from a retrospective reconstruction. |
| Subject | Canonical asset plus venue/instrument where applicable; direction `long`, `short`, `neutral` or `undetermined`; horizon definition ID and optional start/end. |
| Origin | `human`, `deterministic_rule`, `llm_proposal`; author/proposer identity, rule/hypothesis or model/prompt version and proposal ID as applicable. |
| Evidence | Package/snapshot IDs, exact report/update and dataset references, supporting/contrary/unknown roles, analytical definitions and selection basis. |
| Interpretation | Rationale and structured claims with citations; explicit unverified assumptions; no implied statistical probability from prose. |
| Invalidation | Human-readable falsification concept plus optional deterministic observation criteria/price reference with owner and evaluation policy. An alert condition is not an executable stop. |
| Catalysts/time | Linked catalyst IDs, expected time/precision, expiry, review schedule and extension revision; horizon and expiry are separate. |
| Confidence | Assessment, provenance/source, supporting study/calibration if any; human conviction, LLM uncertainty, source reliability and empirical evidence tier are separate fields. |
| Lifecycle | Append `proposed`, `accepted_for_tracking`, `active`, `needs_review`, `invalidated`, `expired`, `resolved` or `withdrawn` events with actor, timestamp, policy and evidence. Mode adapters own acceptance/action. |
| Outcome | Linked outcome/enrollment records and review notes; separate thesis resolution, market-path result and trade net result, with definition/coverage and attribution. |

Maintain a mode-owned mutable status projection only as a rebuildable cache. Shared thesis
facts cannot mutate an execution position. Bot admission creates its own frozen execution
thesis and policy evaluation; a human accepting a thesis for tracking grants no Lab authority.
Preserve frozen entry revisions even when later evidence changes the current thesis.

## Manual Copilot backend capabilities

| Future view/workflow | Existing foundation | Required backend boundary and unsupported pieces |
|---|---|---|
| Market Radar | DU discovery/ranks and existing dashboard view | Neutral inventory/summary query; no paper account lookup. General rank split and horizon projections are future work. |
| Asset Dossier | Context snapshot/brief, positioning, macro, activity, daily scan/regime and relative primitives | Compose one causal package plus explicit changes since previous cutoff. Important events and risks can be shown; complete catalyst coverage, current multi-asset regime integration and validated historical analogues are not already a general service. |
| Thesis Builder | Research thesis v1, recorded snapshots, strategy evidence | Human supplies direction/rationale/invalidation/horizon; resolve supporting and contradictory facts, funding carry inputs and risk-calculator inputs. No automated approval or trade admission. |
| Position Monitor | Manual book/alerts, context update history and entry/current snapshots | Mode-owned position record and monitoring criteria. Return observations `supports`, `contradicts`, `approaching_reference`, `catalyst_resolved`, `needs_review`, `unknown` with citations; never universal “thesis valid” from a score. |
| Event Feed | Logical events, update chain, provider/source quality and receipt clocks | Read-only chronological changes grouped by episode, report-level provenance, known-at filter and confidence/denial evolution; no headline duplication or implied direction. |
| Risk Calculator | Main's existing sizing suggestions and paper risk inputs | Pure deterministic request/result contract below, with user-supplied account/stop/cost inputs and completeness. No live account mutation or execution token. |
| Trade Journal / Post-trade Review | `portfolio/book.py`, journal statistics, paper frozen-evidence/outcome patterns | Append manual entry/exit receipts, entry thesis revision/package, exact entry time/horizon/invalidation/target, costs/funding, path coverage and R denominator. Extend current long/date model rather than pretend it supports all sides/instruments. |
| Research Evidence | Lab definitions, evidence profiles, forward/maturity/validation records | Bounded immutable evidence views by asset/horizon/venue/definition; historical study labels remain qualified, no automatic scientific promotion. |

An asset dossier can already describe recently known events, macro/calendar facts,
OI/funding/crowding, elevated activity and limited market breadth. Relative state and
regime are supported by other modules but need a consistent causal adapter. Liquidity
requires a dated DU or microstructure source with actual coverage. “Relevant historical
analogues” should initially mean transparent filters over registered event/study evidence,
with sample sizes and matched criteria; no semantic analogue search or demonstrated
forecasting capability currently exists.

For monitoring, evaluate only criteria declared on the frozen thesis/revision: event
denial/resolution, adverse facts, OI/funding trend changes, crowding deterioration,
regime shift or distance to an invalidation reference. A rule change is a new monitoring
policy/revision. Monitoring never changes a position size, leverage or order state.

Post-trade review can reuse path/excursion and attribution **contracts**, plus immutable
execution exports where relevant. Reuse existing manual journal ownership for real trades.
Store side, instrument/multiplier, timestamps, actual quantity/fills, target/invalidation,
entry risk amount and policy, fees/funding and accounting completeness. Outcome in R uses
the frozen, explicitly defined initial risk denominator; missing/zero/non-price invalidation
risk yields unavailable R. Do not infer a stop from later lows. Exclude pre-entry extrema
in crossing bars or label coarser bounds. Distinguish actual realized PnL, modeled costs,
thesis-path returns and unexecuted opportunities. Strategy validation needs separate,
preregistered cohorts rather than favorable manual trade selection.

## Research mode shared capabilities

| Use case | Reuse | Remaining constraint |
|---|---|---|
| Hypothesis discovery | Filter event chains, positioning/activity states, radar history and evidence catalog | Discovery is exploratory; freeze new hypotheses before confirmatory testing. No forced directional mapping from events. |
| Event studies | Neutral report/availability manifest plus event/reaction outcome primitives | Separate event-time science, origin-first-seen, consumer availability and exploratory publication basis. Existing final-bar adapter must not supply entry knowledge. |
| Cross-asset studies | Relative/OI panels, explicit asset mappings and historical market inventory | Same cutoff/cohort/venue units, missing and delisted markets retained, common-time dependence accounted for. |
| Regime analysis | PIT macro, daily regimes, hourly volatility/activity and registered structure primitives | State definition and lookback pinned; final macro vintages cannot replace known vintages. |
| Slower strategy testing | Existing daily-perp Lab/compiler/forward evidence and historical bars | New horizon/cost/settlement rules; minute execution max-hold and live quote eligibility are not research entry abstractions. |
| 1–6 month radar | Known catalyst history, macro/regime, relative trends, persistence projections | Context/OI prospective history is young and market-wide catalyst coverage incomplete; start collecting future outcomes, not claiming a tested monthly predictor. |
| Causal reconstruction | Frozen packages, revisions, source raw manifests and software identities | Resolve known-at values and local access clocks; preserve archived objects/definitions through retention. |
| Strategy comparison | Registered definitions, evidence/run IDs, independent outcome curves and execution exports | Compare equivalent horizons/venue/cost/availability; retain baseline, dynamic, immediate/confirmed and manual-selection attribution rather than pooling accounts. |

Execution abstractions currently obstruct reuse when an otherwise descriptive query needs
`paper_nimble_runs`, an active account, a risk pin, an execution universe, a short max-hold,
a current quote cache or a funded position. Research should enroll an observation with
asset, side/reference when relevant, time basis, horizon and data-quality manifest; it
should not invent an intent/fill to get an outcome curve.

## Risk / sizing interfaces: main-only contract review

Main already contains different deterministic approaches:
[`scoring/risk.py`](../src/market_signal/scoring/risk.py) offers long-oriented,
unlevered tier/regime suggestions; [`paper/risk.py`](../src/market_signal/paper/risk.py)
has a pure batch allocation function using a supplied account snapshot, but includes
paper promotion, execution-window and account gates; Nimble specifications/admission use
their own frozen account/venue limits. Their differing semantics should not be unified by
silently selecting one as the universal policy.

Future interface requirements only:

| Contract part | Requirements |
|---|---|
| Request identity | Request/scenario ID, account-snapshot ID/time, context/input manifest hashes, deterministic policy ID/version and requested operation. |
| Position geometry | Side, instrument/contract/multiplier, units/currency, candidate entry/reference, stop/invalidation **only if a price stop is supplied**, target if supplied, horizon and cost/volatility inputs with versions. |
| Account/constraints | Explicit equity/cash, open exposure and reservations, user risk budget, concentration/correlation groups if actually available, existing leverage/margin/venue constraints. No hidden read of paper state. |
| Data quality | Observation/availability/time cutoff, source, completeness, missing and stale input diagnostics; unknown essential constraints must not silently default. |
| Result | Deterministic permissible quantities/notional/risk bounds or unavailable/rejected, binding constraints/reason codes, assumptions, rounding/currency details and identity of all inputs. No order, database write or claim that a stop guarantees loss bounds. |
| Batch behavior | Evaluate candidates against one frozen scenario/account snapshot with declared deterministic conflict/priority policy; preserve policy identity and order invariance where intended. |

Mode adapters: the Lab supplies local authoritative account/venue snapshots and owns final
admission, post-intent quote checks, margin/liquidation safety and execution. The manual
calculator supplies a user-declared scenario, returns an estimate with its omissions, and
does not change the book. Research supplies simulated scenario paths and records results
without accessing a live account. LLM confidence is never an account risk budget or leverage
input. Risk remains an orthogonal deterministic capability, not part of narrative generation.

No sizing algorithm, default budget, policy merge or active risk-branch compatibility is
decided in this review. Align future contracts after that separate work is available on
approved main.

## Data ownership and Railway / OVH split

### Shared conceptually; separate operationally

| Data family | Conceptual sharing | Operational ownership / exchange |
|---|---|---|
| Market bars, revisions, settled funding, OI/ratios | Shared definitions/units and causal reader contracts | Independently ingested local histories or immutable exports; receipt clocks remain local. |
| Macro/context facts, events/updates, asset links, source registry | Shared knowledge and provenance | Railway canonical ingress/history; Lab local public acquisition in v1, optional later one-way context feed. |
| Radar observations and analytical snapshots | Shared market state and versioned selection | Bounded read projections/export objects; no writable cross-host ledger. |
| Research definitions, software identities, scientific results | Shared immutable catalog/read views | Railway research authority; copies on Lab are frozen read-only inputs, not a second checkpoint authority. |
| Thesis revisions and evidence packages | Common contracts; origin/owner namespaces | Railway human/research writes; Lab autonomous writes; optional immutable cross-mode catalog with attribution. |
| Manual accounts/positions/journal | Copilot-owned | Railway/local user records; not an input to Lab trading without a separate future explicit interface. |
| Autotrader cash/exposure, pending intents, fills, execution receipts and funding adjustments | Mode-specific operations | OVH Lab-only writable state; historical predecessors remain with their original Railway archive. |
| Runtime authority, scheduler state, locks, spool offsets, delivery attempts | Host/mode-specific | Never replicated as authority. No shared runtime ID, DB mount or spool. |

Keep one local writer/authority per DuckDB. No shared writable database, remote writable
DuckDB, shared filesystem spool or synchronous Railway dependency in the Lab evaluation
loop. Shared code is not shared account state.

The approved `lab_seed_v1` is a useful existing precedent: it reads a verified snapshot
at a declared frontier, copies selected market/context history and frozen definitions,
and excludes execution history, host authority, ingress receipts, microstructure and DU
state ([seed policy](RUNTIME_PROFILE_SPLIT_AND_VPS_LAB.md)). Preserve that policy. Seeded
origin history can support later causal calculations after activation; it does not prove
that the new Lab had access before the seed arrived or continue a predecessor account.
An intelligence export is not permission to broaden this seed or backdate warmup.

### Assessing the proposed host allocation

The proposed Railway intelligence/Copilot and OVH execution allocation is sound as a
target, with important qualifications from approved profiles:

| Responsibility | Current v1 / recommended ownership |
|---|---|
| Work gateway, semantic submissions and public dashboard/read backend | Railway-owned. Lab v1 has no Work ingress. Future read backend must serve projections, not the live writer file. |
| Free-source context, macro, positioning and bars | Both profiles currently ingest locally. Share code/definitions now; retain independent freshness and authority. Railway owns central human/research history. |
| Research Lab, daily signals, forward science, Phase 24B checkpoints | Railway research authority. Lab seeded science labels stay frozen/descriptive until explicitly refreshed from immutable exports. |
| Core-six microstructure | Both v1 profiles permit it. Preserve governed Railway continuity; do not claim it already lives only on OVH. |
| Dynamic Universe radar/warming | Absent on research v1 and gated on Lab v1. Future general radar requires a new capability/profile review; rich warming belongs on Lab after separate activation validation. |
| Nimble evaluation/paper execution/new execution evidence | OVH Lab profile. Old Railway execution runs/evidence are predecessors, not continued Lab accounts. |
| Retired paper drains | Research v1 still permits legacy drains. Existing obligations remain until separately resolved; they do not justify new execution in the shared layer. |

Independent public context ingestion is acceptable in v1: small polling cost buys local
availability and isolation, and the approved profile split explicitly depends on it. It
does cause different observation times and potentially different keyless event grouping.
Those differences must be attributed when comparing studies/cohorts. “One brain” means
one normalization/definition/evidence contract; current feeds need not pretend identical
receipt histories.

Later **one-way Railway → OVH append-only context replication** is preferable for Work
coverage and canonical logical episode identity, provided it remains asynchronous and
does not replace local safety/data feeds blindly. Recommended design:

1. Railway exports committed report/update/link/source-definition envelopes in ordered,
   checksummed immutable batches, with schema, origin runtime, sequence/frontier and
   dependency manifest. Consumers fetch asynchronously with a local cursor.
2. OVH verifies sequence/dependencies/hash/version, fsyncs its local receipt and applies
   atomically under its own writer. It records local availability independently from
   origin clocks. Retry/dedup by origin report ID; gaps stay visible.
3. A snapshot bootstraps a frontier; batches continue after that frontier. A partial
   source failure or unavailable Railway leaves local known data and explicit stale state.
   No quote/execution action blocks waiting for Railway. Existing Lab policy behavior is
   unchanged until a separately versioned feed/consumer policy is approved.
4. When both local and replicated sources cover the same fact, keep both observations
   and earliest actually accessible local availability; add explicit cross-origin links.
   Avoid two copies of one report producing two independent corroborations.
5. Replicate no accounts, intents, notices, authority claims, raw OAuth identities/secrets,
   spool offsets or execution receipts into the context stream. A public provenance view
   can pseudonymize submitter identity while private audit records remain controlled.

Compact OVH → Railway liquidity/microstructure or execution-review exports are a separate
immutable artifact channel with origin/cutover/coverage identity. They are not a reverse
writable context feed and cannot shift research authority or rewrite predecessor evidence.
A synchronous read API is reasonable for an interactive human view if stale-cache fallback
is visible; it is not justified as a mandatory Lab decision dependency.

## Query/read architecture

Use the authoritative local writer to publish projections at a committed frontier; keep
human reads independent of file locking and scheduling. Future dashboard, desktop app and
Copilot UI consume a read-only API over immutable objects or an independently refreshed
read replica/snapshot. The API performs no migrations, ingestion, authority claims,
snapshot recording, evaluation or scan writes as a side effect of a read.

Conceptual queries are `context_snapshot(asset, cutoff, horizon, access_basis)`,
`context_changes(asset, from_package, cutoff)`, `positioning_summary`, `radar_snapshot`,
`evidence_resolve(ids)` and `thesis_review(thesis_revision, current_package)`. Each returns
versions/frontier/freshness/missingness and bounded pagination. These are proposed
contracts, not existing HTTP routes.

Research consumes partitioned Parquet plus revision/provenance manifests or verified
read-only DuckDB snapshots, with explicit dataset/frontier IDs. Desktop/offline use can
cache packages locally and show data age. Manual thesis/journal commands are separate
authenticated, mode-owned writes; their read API never publishes writes to the execution
runtime. A “latest” pointer may be replaced atomically; its content-addressed target is
immutable and cited entries retain that target rather than just the pointer.

Current [`dashboard/common.py`](../dashboard/common.py) opens the configured local Store
and allows write-capable helpers; its requested read-only open can become writable when
the file does not exist. [`dashboard/views/universe.py`](../dashboard/views/universe.py)
calls the paper-aware universe status reader. These are acceptable places to adapt later,
not contracts to expose to remote clients. Current paper reporting already tracks snapshot
manifest age and source identity ([report](../src/market_signal/paper/dashboard/report.py));
reuse that honesty instead of treating download time or file mtime as market freshness.

## Future LLM boundary

No LLM implementation or inference workload is proposed in this phase. The existing Work
fact ingress remains factual and must not become the thesis-generation endpoint.

| Structured input | Required contract |
|---|---|
| Request | Task/question, allowed assets, horizon, schema version, package ID/cutoff/access basis, model/prompt/version provenance. |
| Context | Frozen snapshot and bounded selected event reports/updates, facts, source reliability, exact citations and clocks. Source text is evidence, never an instruction to the system. |
| Positioning/market state | Typed venue-separated summaries, units, definitions, freshness, missingness and optional horizon-appropriate micro/liquidity summaries. |
| Historical evidence | Registered study/hypothesis/run references, tested horizon/venue/universe, sample/coverage/maturity, positive and adverse findings. Outcomes known at the requested cutoff only. |
| Thesis under review | Optional human/deterministic thesis revision and explicit invalidation/review criteria; no account secrets or execution controls needed. |

| Structured output | Responsibility / validation |
|---|---|
| Proposal identity | Proposal/schema ID, input package ID, output timestamp and model/prompt provenance; retain the original output. |
| Thesis/direction | Proposed thesis, `long`/`short`/`neutral`/`undetermined`, and horizon; allow abstention and insufficient evidence. |
| Rationale | Structured factual and interpretive claims, supporting/contrary evidence, assumptions; each factual claim cites available report/dataset IDs. |
| Invalidation/catalysts | Invalidation concept and expected catalyst references/time uncertainty, not executable stop/order instructions. |
| Uncertainty | Missing data, disagreement, alternative explanations and explicitly labeled uncalibrated assessment; no unsupported win probability. |
| Evidence references | Only IDs in the supplied manifest/resolver scope; no invented source, inaccessible revision or later fact. |

Deterministic validation checks schema, size limits, asset/horizon constraints, evidence
existence/accessibility and forbidden execution fields. Validation of citation existence
does not prove the cited fact supports the generated claim; unsupported interpretations
remain proposals. Citation checking, factual conflict rules and evidence selection are
shared services; LLM cross-source synthesis and contradiction interpretation can add a
separately attributed proposal layer. Record failures and abstentions for evaluation.

The LLM may summarize, interpret events, synthesize sources, propose theses and flag
possible contradictions. It must **not** own account risk, sizing, leverage, execution,
liquidation safety, quote validation, venue controls, subscription pins, admission or
scientific promotion. It has no execution tool, account writer or policy-edit authority.
Human tracking acceptance and any future autonomous thesis cohort are separate adapters;
the Lab must still apply deterministic policy/risk/venue controls to its own local inputs.

## Storage/runtime impact and performance bounds

The figures below separate **recorded evidence** from **design estimates**. No new benchmark
or implementation test was run. CPU estimates are planning budgets that require later
measurement on the chosen runtime. RAM numbers are total process-working-set allowances
for the existing Python/DuckDB stack, not necessarily incremental RAM if work shares an
existing process. MB is decimal; MiB is binary. No current cloud tariff/bill is inferred.

Relevant recorded anchors:

- DU public discovery plus bounded light requests: 72 requests, 288,851 response bytes,
  36.1s wall and 0.106 CPU-seconds; pure all-market rank about 13–15ms. Full scratch
  persistence/readiness was **0.804 CPU-seconds / 196 MiB peak** and a 60,188-byte compressed
  decision. A synthetic 32-evaluation history cycle reached **2.49 CPU-seconds / 362 MiB**.
  These are different workloads, not an API latency guarantee
  ([ledger evidence](evidence/dynamic-universe-v1/ledger-cost.json),
  [history evidence](evidence/dynamic-universe-v1/ledger-history-cost.json)).
- Free-source rollout recorded about **159 MiB RSS / 168 MiB peak**, 0.100 CPU-seconds
  over a 106s short interval, and 60s exchange / 300s RSS/GitHub configured polling.
  The older planning example used 100 accepted observations/day and 75 MB/month combined
  ledger/spool growth. This is an assumption, not observed sustained volume
  ([source report](FREE_EVENT_SOURCES.md)).
- DU review measured core-six collector about **174 MiB RSS and 0.53% of one core**.
  Its twelve-rich-asset sensitivity was about **20.1 MB/day permanent DB growth**, including
  DU evidence/outcomes, plus raw/spool reserves and full-backup amplification. This is
  why cheap shared views must not copy rich warming ([review](DYNAMIC_HYPERLIQUID_UNIVERSE_V1.md)).

| Proposed shared capability | CPU estimate/budget | RAM allowance | Storage estimate and bound | Cadence |
|---|---|---|---|---|
| Context fact acquisition, macro and source health | Reuse current jobs; free-source short-sample CPU was small, but network/writer contention dominates latency. Budget separate bounded ingestion batches, not busy UI polling. | Existing collector ~160–175 MiB; no new collector solely for Copilot | Existing append ledgers/raw already paid for; source-report planning example ~2.5 MB/day combined, rate-dependent. Preserve archive capacity/receipt bounds and actual growth accounting. | Exchange ~60s, RSS/GitHub ~300s; macro existing schedule |
| Market context snapshot/diff + evidence manifest | Target <1 CPU-second per warm bounded request; a cold query can exceed this. Precompute common hourly packages instead of rebuilding for each UI refresh. | ~200–400 MiB worker allowance | Six hourly assets = 144 snapshots/day; assuming 5–15 KB compressed/package, ~0.72–2.16 MB/day. Prefer shared event objects/references; freeze entry packages, archive old projections. | On demand + hourly/daily common views |
| Positioning/persistence projections | Target <2 CPU-seconds per core-six hourly batch; measure longer lookbacks and loader costs. | ~200–400 MiB if standalone; shared worker preferred | At 1–3 KB compressed/core asset-hour: ~0.14–0.43 MB/day incremental projections, source observations separate. Extend to 20 assets only after measured coverage/cost. | Hourly sources; daily/weekly aggregate views |
| Shared market/regime/relative summaries | Target <2 CPU-seconds per small-cohort hourly/daily batch; avoid rerunning study inference | ~200–400 MiB shared analytical worker | ~0.1–0.5 MB/day for compact core/shortlist projections; retain bars once, reference feature inputs | Hourly/daily/weekly according to definition |
| Cheap discovery/radar | Pure rank cheap; source fetch and persistence nontrivial. Target <1 CPU-second per compact refresh, bounded top shortlist; full current DU path is outside this target. | ~200–400 MiB total worker; no rich adapter frames | Current full DU decisions alone: 60,188 B ×144 = ~8.67 MB/day before outcomes/backups. Proposed hourly compact inventory archive + daily checkpoint: planning ~1–6 MB/day compressed at ~539 markets, schema/compression-dependent; keep ten-minute latest as bounded cache. | Latest 10–30m; hourly archive; daily slow view |
| Event/thesis outcome tracking | Budget up to 2 CPU-seconds per scheduled catch-up batch, reuse bounded enrollment; no full-history scan per UI | ~200–400 MiB analytical worker | Illustrative 100 event-asset enrollments/day ×8 outcomes ×1–3 KB ≈0.8–2.4 MB/day. Horizon count/asset links multiply cost; default manual volume much smaller. | On matured horizons; bounded backlog/daily batches |
| Thesis revisions, manual journal, monitor reviews | Trigger on committed relevant changes or scheduled reviews; target <1 CPU-second per small batch, no 2s loop | Existing backend allowance, no per-position daemon | Illustrative 20 tracked theses ×4 daily reviews ×2–5 KB ≈0.16–0.4 MB/day, plus frozen entry packages. Store meaningful transitions rather than identical heartbeat payloads. | Event-triggered/hourly/daily by thesis policy |
| Optional micro/liquidity summary exports | Derive on OVH/covered source; bounded hourly batch target <2 CPU-seconds. No Railway all-market raw collection | Existing source process; Railway only small projection/read worker | Six assets ×24 ×1–3 KB ≈0.14–0.43 MB/day; raw/minute datasets remain source-owned and separately archived | Hourly or 15m on explicit fast use case |
| Definition/evidence catalog and read API/cache | Indexed/bounded reads; expensive studies off request path | ~150–300 MiB thin Python service allowance, framework-dependent | Definition/evidence manifests grow per new study/version; cap API payload/page and cache size. Large datasets stored separately, referenced once | On registration/result publication; cache TTL by data freshness |
| Optional append-only context replication | Target <1 CPU-second per bounded verification/application batch; measure manifest lookup and writer contention | Reuse local ingest worker, ~200–400 MiB if standalone | A second host necessarily retains its own selected context copy. Using the source planning example, budget up to ~2.5 MB/day input growth plus bounded receipts/manifests, not another copy of every raw/source archive. Measure actual exported bytes and egress. | Asynchronous 1–5m batches or hourly for slow-only consumers |
| Deterministic risk calculator contract | Pure bounded request; benchmark after risk work lands, no current sizing implementation estimate | Shared backend; no new persistent risk daemon required | No market-data duplication; retain request/result manifest only when user/research/run explicitly records it | On demand/simulation batch |

Do not add these rows as a measured combined bill: capabilities can share processes and
source data, workload counts are assumptions, and macro/source/history reads can dominate.
Use per-job CPU/wall/RSS, retained compressed bytes, physical DB allocation, spool/raw,
backups, archive/egress and peak temporary workspace when sizing later.

Cheap in CPU does not imply cheap in storage. Avoid copying the full 1.37 MB uncompressed
DU decision/readiness payload into every dossier. Share immutable event/definition objects,
use projections with manifests, and retain larger scientific datasets in archive partitions.
Explicit caps should cover requested assets, date span, event/report count, payload bytes,
concurrent requests, outcome enrollments and pending-backlog catch-up time.

Future LLM compute/API cost is excluded from these deterministic shared features. No model,
token budget or inference host is selected, so an honest numeric estimate is unavailable.
Later proposals must meter calls/tokens, bound input packages, cache by package/model/prompt
identity and retain proposal provenance; they must not place a model call on the ingestion
or execution loop.

The hourly radar archive cannot reconstruct every uncaptured ten-minute rank. State the
archive resolution; freeze any intermediate snapshot cited by a decision/thesis, or retain
all ten-minute compact observations off-host if a preregistered study requires that history.
Do not discard source inputs/definitions needed for cited entry reconstruction. Cache eviction
is not evidence deletion; archive manifests and referential integrity need distinct policies.

## Refactor map: future package boundaries only

No file moves or renames now. Each proposed separation removes a dependency or establishes
ownership; package naming alone provides no benefit.

| Proposed boundary | Existing code to reuse/separate | Architectural benefit |
|---|---|---|
| `intelligence/contracts` and provenance | Neutral parts of `research/lab/common.py`, dataset/software/evidence identity, availability envelopes | Mode-neutral types/hashes/versions without importing research orchestration or execution. Preserve existing IDs through compatible serialization. |
| `context/` | Existing ledger/model/taxonomy/entities/macro/providers; split gateway/free commit from paper outbox | Own normalized facts and folds; publish committed-information envelopes independent of Nimble. |
| `positioning/` | Collection versus pure summaries in `context/positioning.py`; descriptive OI/price primitives | Separate writers/provider adapters from read-only venue-aware calculations and horizon projections. |
| `intelligence/market_state` | `context/opportunity.py`, causal breadth, relative primitives, regime adapters | One set of descriptive features with explicit cohorts/horizons; research forward paths and verdicts remain elsewhere. |
| `radar/` | DU decoding/light measurements/rank components and neutral ledger views | Serve discovery/attention without account pins, execution gates or rich subscriptions. |
| `thesis/` | General contract beside `context/thesis.py`; evidence/package links, append lifecycle | Reuse a thesis language while keeping human, deterministic and LLM origin and mode acceptance distinct. Preserve research-thesis v1. |
| `intelligence/evidence` | Neutral reference/path/coverage/MFE-MAE contracts from event reactions, forward and paper reports | Track unexecuted theses/opportunities without fabricating paper intents; immutable evidence resolution. |
| `research/` | Existing Lab, studies, inference, calibration, lifecycle/forward/incubation | Scientific authority and publication of evidence read models, independent of live account availability. |
| `copilot/` | Existing watches plus manual dossier/thesis/monitor adapters; `portfolio/` manual receipts/journal | Human-owned state and decisions consume shared packages; no execution mutator. |
| `risk/` | Future deterministic contract; existing sizing/risk adapters coordinated after active work lands | Explicit pure inputs/results with separate mode/account enforcement; no intelligence-to-risk hidden state. |
| `execution/` | Keep `paper/` internally compatible initially; Nimble worker/model, DU rich lifecycle, quote checks, pins | Own account authority, executable geometry, admission, fills/settlement/liquidation and run attribution. No general reader imports these. |
| `intelligence/projections` and read adapters | Dashboard local readers, export/snapshot manifests, neutral notifications | Publish bounded read objects; separate writer file, presentation and query API from execution status. |
| `ops/` | Profiles, locks, scheduling, backups and per-host storage adapters | Preserve fail-closed runtime split and independent data ownership; domain functions consume values/read repositories. |

The current monolithic `data/store.py` includes all schemas and runtime authority. Reuse
it through a narrow read repository/export adapter initially; do not attempt a broad database
split merely to introduce packages. A minimal intelligence-only fixture must fail if a reader
asks for execution tables, even if the usual Store creates those tables automatically.

## Migration sequence

Every phase below is a future proposal with its own review and validation. No legacy policy,
hypothesis, evidence identity, profile digest, scientific result or trading behavior is changed
by this document. Complete read contracts before enabling new workloads.

| Phase | Deliverable and dependency | Exit criteria |
|---|---|---|
| A — causal context and positioning read packages | Versioned snapshot/evidence/access envelope; use existing causal loaders, full provenance resolver, horizon/freshness definitions; split neutral information publication concept from paper wakeups. | Deterministic same-cutoff packages; revisions cannot leak; minimal fixture has no execution schema; existing frozen snapshots/IDs remain readable. Core dossier and event feed can consume these. |
| B — cheap radar separation | Neutral discovery/rank/component query and bounded projection, separate from `risk_pins`, `status` and DU execution gates. | Reproduce extracted primitives where unchanged; changed rank gets new ID; account-independent output and measured storage/request bounds. Later new research profile explicitly admits only cheap capability; no DU warming activation. |
| C — manual thesis and evidence lifecycle | General thesis beside v1; manual journal receipt adapter, causal entry freeze, independent slow outcome enrollment using A and B where needed. | Human/rule/LLM/paper identities separated; unexecuted theses track outcomes; truthful retrospective labels and side/price-basis/coverage/R handling. Risk interface alignment waits for separately approved risk work. |
| D — ongoing monitor and post-trade review | Compare entry/current evidence and declared criteria; bounded reviews, catalyst resolution/adverse changes, actual/manual costs and R. | Read-only intelligence plus manual-mode review events; no execution mutation, no forced thesis-valid answer and no 2s process. |
| E — asynchronous replication and historical study projections | Optional Railway → Lab context batches for Work/canonical episodes; compact source summaries and scientific artifacts as immutable exports. Advance earlier only if missing Work coverage becomes a demonstrated need. | Independent host receipt clocks, idempotent replay/gap recovery, no runtime authority transfer or synchronous execution dependency; clearly attributed new feed/cohort version. |
| F — future LLM proposal layer | Strict input/output contract and evaluated summarization/thesis proposals over existing packages, after A/C evidence validation. | Evidence-grounded proposals/abstentions, model/prompt provenance, forbidden control fields, no sizing/execution authority; separate factual ingress remains intact. |

Slow-horizon studies should start prospectively alongside C; months of evidence cannot be
produced by a refactor. Moving Railway's existing microstructure collection is not a prerequisite
for A–D and is excluded from their scope. Any later offload needs a separate collection/science
continuity plan and new profile review. Versioned cheap radar/monitor/profile additions must
not turn on legacy DU flags under `prism_research_runtime_v1` or
`prism_autotrader_lab_v1` or alter their existing fail-closed interpretation.

## Risks and accidental coupling

| Risk observed or anticipated | Clean interface / mitigation |
|---|---|
| Shared reader imports `paper.*` or needs an active account | Value/read-repository contracts and minimal intelligence-only fixtures; execution adapters depend on shared code, never reverse. |
| Context commit emits Nimble-specific trigger/file | Neutral committed-information publication; Lab adapter owns trigger IDs/acknowledgments and execution notices. Preserve atomic commit and restart recovery. |
| Market context silently uses revised bar truth | Causal loader and frozen definition/dataset manifest; separate final-history outcome basis from entry knowledge. |
| First-seen confused with publication, commit or replicated access | Multi-clock envelope and explicit query access basis; truthful retrospective studies and no consumer backdating. |
| Folded attributes from several reports manufacture official evidence | Report-level citations and source caps; keep official qualification separate from folded descriptive context. Existing event execution rules already demonstrate why this matters. |
| Current snapshot loses provenance or drops old unresolved episode | Resolver-backed manifest, omission/pagination, horizon selection and unresolved-chain policy; retained raw history remains authoritative. |
| DU ranking invokes risk pins or account-aware status | Separate pure components and neutral ledger reader; bot eligibility/collection state is a different adapter. |
| Cheap radar creates rich warming/storage costs | Profile capability gate, bounded REST shortlist, projection payload budget, no all-market micro subscription. |
| Research/Copilot dashboard reads live writer DB | Immutable frontier export/read-only service/local verified copy; reads never fall back to a writable missing DB. |
| Shared intelligence mutates position/account state | Shared queries return values; monitoring writes only manual review events. Lab account changes require its own admitted execution lifecycle. |
| Duplicated ingestion yields divergent clocks/events or false corroboration | Shared parsers/definitions plus origin report IDs, local access clocks and explicit cross-origin links; compare acquisition routes in studies. |
| Shared latest cache erases prior meaning | Content-addressed entry packages and append source revisions; atomic latest pointer separate from evidence archive. |
| Long-horizon product overstates coverage/predictive power | Show coverage/maturity/venue/horizon and unsupported sources; preregister new long-horizon outcomes. No fabricated analogues or directional OI story. |
| Manual journal mistaken for scientific sample or executable PnL | Separate actual receipts, thesis paths, hypothetical fills and preregistered studies; record selection and accounting basis. |
| Packaging changes frozen identities or profiles | Compatibility adapters, serialized identity/digest checks and explicit new definitions for changed semantics; never rewrite predecessor records. |
| Storage estimates omit backups/raw/temporary copies | Measure physical occupied/free space, retained evidence, cache/archive/spool and full-backup reserve; do not size from pure ranking CPU or compressed rows alone. |

## Future tests / validation plan

No implementation tests were run for this document. Future implementation should prove:

1. **Causal cutoffs:** late publication/import, source revision, consensus arriving after
   release, added asset link, bar/funding/macro revision, delayed commit and delayed replication
   cannot change an earlier frozen knowledge package. Exercise origin and consumer access
   bases separately, including crossing periods/bars.
2. **Snapshot determinism:** equivalent direct/index/operation readers and same immutable
   history/definitions/cutoff/selection produce identical content IDs; sorting, pagination
   and truncation are deterministic. New definition versions produce explicitly new identities.
3. **Provenance:** every cited fact resolves to the exact accessible source report/update,
   original clocks/raw hash/mapping; source caps and denial history survive export. Display
   source-count-only payloads cannot masquerade as full evidence packages.
4. **No execution dependency:** package readers run against minimal schemas without paper
   accounts/tables, Nimble, runtime claims or quote files. Account/pending/PnL changes cannot
   change shared radar component ranks for identical market inputs/cohort.
5. **Read-only behavior:** read endpoint with missing database fails/degrades instead of
   creating a file; no schema migration, authority claim, trigger write, snapshot recording,
   scan write or trading mutation; checksums of immutable inputs remain unchanged.
6. **Horizon semantics:** slow windows use closed causal data, correct settlement cadence,
   calendar/DST handling, explicit gap/coverage/freshness and prior-only normalization. Short
   features cannot be substituted into a monthly claim without new definition/evidence.
7. **Runtime profiles:** existing v1 process/job/dynamic-universe gates and frozen digests
   remain compatible; invalid research execution flags still fail closed. New profiles enable
   only explicitly approved workloads, not an implicit return to combined execution.
8. **Thesis/outcomes:** frozen entry revision persists after changes, no-trade theses and
   rejected opportunities can mature independently, missing references/paths remain missing,
   daily extrema cannot imply precise intraday excursions, and R uses original declared risk.
9. **Replication:** duplicate/out-of-order/partial batches, checksum/version failures, missing
   dependencies, clock skew, crash-before/after-local-commit and source outages recover without
   duplicate corroboration, backdating or execution blocking. No authority/account state crosses.
10. **Storage/cost bounds:** representative whole-cohort and historical-history requests with
    actual manifests, compressed payloads, peak RSS/CPU/wall, writer lock duration and physical
    growth; bounded outcome backlog and archive/backup reserve. Do not rely on empty-schema
    rank timing as an end-to-end measurement.
11. **Future LLM boundary:** unknown/future citations and forbidden sizing/leverage/order
    fields rejected; source text cannot grant tool authority; unsupported claims and model
    uncertainty remain proposals, and abstention is a valid output.

## What should remain autotrader-only

Explicitly retain these in the execution/Lab adapter and operational state:

- Two-second worker evaluation/quote monitoring and fast admission scheduling.
- Fresh **post-intent** bid/ask checks, exchange/receipt/session/runtime validation and
  executable quote caches; research price references do not become fills.
- Execution hypothesis eligibility, pending intents, conflict/reversal/cooldown and
  account-dependent priority/capital/position-slot allocation.
- Fill simulation, spread/slippage/fee crossing, entry/exit receipts and execution lifecycle.
- Account cash/equity/exposure reservations, settled/provisional funding accounting and
  liquidation/margin/venue safety enforcement. Pure future risk math may be shared, but
  live enforcement and account state stay local.
- Frozen executable objectives/invalidation, stop/target/expiry/max-hold enforcement and
  data-failure/emergency execution behavior.
- Dynamic rich satellite warming, hypothesis-specific execution readiness/portability,
  high-frequency desired subscriptions, promotion/demotion/dwell and collection segments.
- Open/pending/exit/unsettled-accounting/research-outcome/event-lifecycle risk pins and
  execution subscription obligations.
- Run activation, kill/recovery, paper-account authority and delivery/runtime controls.
- Immediate/confirmed event admission variants, execution-specific venue unavailability
  rules and remaining-target economics. Their observations can be exported; their policy
  decisions must not silently become human dossier logic.

These may consume shared facts and publish immutable descriptive evidence. They must never
be prerequisites for Research or Manual Copilot to explain an asset, record a thesis or
measure a market outcome.

## Verdict

**SHARED INTELLIGENCE LAYER RECOMMENDED**

Build one causal, versioned intelligence contract with separate consumers and operational
state. Transfer context/provenance and positioning first, split cheap radar from execution,
then add a neutral thesis/evidence lifecycle. Preserve the approved runtime split and frozen
scientific history; rich execution machinery stays in the Autotrader Lab.
