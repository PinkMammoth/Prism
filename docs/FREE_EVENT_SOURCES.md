# Free event sources — Phase 29

`free_event_sources_v1` is an information acquisition layer feeding Phase 23. The hard economic constraint is **£0/month in new market-data/news subscriptions and API charges**. Every selected provider has `DATA_COST = £0`. Existing Work access is unchanged; no API model calls or local inference run in the polling path.

**Prism will not pay for premium event data while the strategy remains unproven.**

**Fast deterministic/public feeds are the primary event-discovery layer.**

**Work is a slower semantic layer, not the primary trigger for short-horizon execution.**

**Opportunity quality matters more than minimum holding period. A profitable 4-hour trade is preferable to an unprofitable 5-minute trade.**

## Baseline and scope

This branch starts from fetched `main` (`c5eceda`) and applies the two committed Phase 28A dependencies (`92e61f7`, `42d5d76`). Phase 28A was deployed but not present on main at implementation time. Existing uncommitted Work monitor changes are preserved in the original checkout.

No paper baseline, nimble execution definition, funding model, frozen Phase 24B definition, execution universe or Work prompt definition is changed. The six execution assets remain AAVE/BTC/ETH/HYPE/LINK/SOL. Event entities and context asset links can include other assets; source metrics explicitly report those cases. Unmapped entity names remain provenance without an invented asset association.

## Curated registry and exact endpoints

`config/context/free_sources.v1.yaml` is versioned and capped at 20 entries: 18 selected, plus one disabled GDELT candidate. Source class, parser, provider identity, cadence, freshness, enabled/stage flags, expected domain, authentication, terms/access review, subscription/API cost and rate policy all live in the registry. Mutable source health is in durable source-state files rather than editing versioned configuration.

| Provider ID | Parser | Cadence | Useful freshness | Stage | Registry state | Endpoint |
|---|---|---:|---:|---:|---|---|
| `hyperliquid_status_v1` | statuspage | 60s | 2h | 1 | selected | https://hyperliquid.statuspage.io/api/v2/incidents.json |
| `coinbase_status_v1` | statuspage | 60s | 2h | 1 | selected | https://status.coinbase.com/api/v2/incidents.json |
| `kraken_status_v1` | statuspage | 60s | 2h | 1 | selected | https://status.kraken.com/api/v2/incidents.json |
| `solana_status_v1` | statuspage | 60s | 2h | 2 | selected | https://status.solana.com/api/v2/incidents.json |
| `circle_status_v1` | statuspage | 120s | 2h | 2 | selected | https://status.circle.com/api/v2/incidents.json |
| `base_status_v1` | statuspage | 120s | 2h | 2 | selected | https://status.base.org/api/v2/incidents.json |
| `sec_rss_v1` | rss | 300s | 6h | 3 | selected | https://www.sec.gov/news/pressreleases.rss |
| `fed_rss_v1` | rss | 300s | 6h | 3 | selected | https://www.federalreserve.gov/feeds/press_all.xml |
| `cftc_rss_v1` | rss | 300s | 6h | 3 | selected | https://www.cftc.gov/RSS/RSSGP/rssgp.xml |
| `sec_trading_suspensions_rss_v1` | rss | 300s | 6h | 3 | selected | https://www.sec.gov/enforcement-litigation/trading-suspensions/rss |
| `sec_administrative_proceedings_rss_v1` | rss | 300s | 6h | 3 | selected | https://www.sec.gov/enforcement-litigation/administrative-proceedings/rss |
| `sec_litigation_releases_rss_v1` | rss | 300s | 6h | 3 | selected | https://www.sec.gov/enforcement-litigation/litigation-releases/rss |
| `github_geth_releases_v1` | github_releases | 300s | 24h | 4 | selected | https://api.github.com/repos/ethereum/go-ethereum/releases?per_page=10 |
| `github_geth_security_advisories_v1` | github_advisories | 300s | 24h | 4 | selected | https://api.github.com/repos/ethereum/go-ethereum/security-advisories?per_page=10 |
| `github_bitcoin_releases_v1` | github_releases | 300s | 24h | 4 | selected | https://api.github.com/repos/bitcoin/bitcoin/releases?per_page=10 |
| `github_bitcoin_security_advisories_v1` | github_advisories | 300s | 24h | 4 | selected | https://api.github.com/repos/bitcoin/bitcoin/security-advisories?per_page=10 |
| `ethereum_official_rss_v1` | rss | 300s | 6h | 3 | selected | https://blog.ethereum.org/en/feed.xml |
| `bitcoin_official_rss_v1` | rss | 300s | 6h | 3 | selected | https://bitcoincore.org/en/rss.xml |
| `gdelt_crypto_discovery_v1` | gdelt | 900s | 6h | 5 | disabled | https://api.gdeltproject.org/api/v2/doc/doc?query=(bitcoin%20OR%20ethereum%20OR%20crypto)&mode=ArtList&format=json&timespan=1h&maxrecords=20&sort=DateDesc |

Freshness is evaluated using the most recent factual update timestamp for incidents/advisories and the publication time for ordinary feeds. Incident creation/publication time remains separately preserved, even when a fresh resolution concerns an older incident. An unchanged old incident is not made fresh by a successful GET. Missing information time, more than five minutes of future clock skew, stale items and routine noise are rejected before context insertion.

Stage 1 adds three exchange feeds; stage 2 adds Solana/Circle/Base; stage 3 adds official RSS; stage 4 adds GitHub. Stage 5 reserves broad discovery, currently disabled. All 18 selected endpoints returned 200 and then 304 in the Railway scratch benchmark. Initial probes also showed some servers returning 200 for unchanged validators: content hashing still prevents repeated parsing/insertion.

## Current-source audit

Public endpoints were probed on 2026-10-10 with bounded GETs and conditional follow-ups. Exact response formats, item counts, validators, compressed/decoded bytes, timings, upstream headers and failures are retained in `docs/evidence/phase29/`. No authentication or credit card was required for the selected sources. No pay-per-request product is configured.

The selected sources are primary institutional/project/venue sources. Statuspage exposes page-level consumer APIs separately from paid/authenticated page-management APIs. We consume the former. GitHub documents unauthenticated access to public releases/security advisories. SEC advertises RSS for its four selected release classes. Fed/CFTC/project publishers expose their own RSS; no article HTML scraping is used.

Hyperliquid announced its status page in its official announcements channel; that announcement was used only for identity verification, not continuous social ingestion. The page has sparse historical incident reporting: healthy HTTP does not establish that every real outage will be reported promptly.

Binance has no verified suitable feed here. Solana/Base RPC or sequencer degradation is `validator_issue`; only an explicit chain/block-production halt becomes `chain_halt`. Circle mint/redemption disruptions are `custody_disruption`; they do not imply a market-price depeg. Technical relevance covers emergency/security-focused releases, high/critical advisories and consensus/chain-stability notices. Ordinary releases, draft/prerelease builds, documentation and tooling noise are ignored. Repository advisory feeds can legitimately be empty (Bitcoin was empty during validation); the official Bitcoin RSS also carries security notices.

| Candidate | Decision | Evidence/reason and free alternative |
|---|---|---|
| Binance status/announcements | Excluded/deferred | Public announcement page returned 202 without a usable structured feed. Unofficial/internal CMS routes are fragile and not integrated. Work semantic coverage; Coinbase/Kraken/Hyperliquid direct status. |
| Arbitrum status | Excluded/deferred | Candidate incidents.json returned 404. No verified current machine endpoint. Base/Solana status; Work chain monitoring. |
| Chainlink status/CCIP | Excluded/deferred | status.chain.link DNS failed. Official CCIP lane UI exists; no documented public machine endpoint verified. Work critical-infrastructure monitor. |
| Hyperliquid Python SDK releases | Excluded/deferred | Free API works; SDK updates are developer-tool noise rather than consensus/chain-stability notices. Hyperliquid official status. |
| Old SEC litigation/admin/suspension RSS routes | Excluded/deferred | 404; replaced by verified /enforcement-litigation/*/rss routes. Current official routes enabled. |
| Treasury press feed | Excluded/deferred | Candidate /news/press-releases/feed returned 404; official feed directory timed out. No fragile scraping. Existing official macro calendar and Fed press; Work unscheduled macro. |
| GDELT DOC API | Excluded/deferred | Two independent bounded probes returned 429 without Retry-After. Current availability/update cadence/publication delay cannot be verified. Disabled. Primary-source RSS/status; Work broader discovery. |
| Cointelegraph RSS | Excluded/deferred | Endpoint 200 and conditional 304, but current terms prohibit independent automated content pipelines without permission. Ethereum/Bitcoin official RSS. |
| Decrypt RSS | Excluded/deferred | Endpoint works; current terms prohibit automated collection/caching without written consent. Official project RSS and Work. |
| CoinDesk RSS | Excluded/deferred | Endpoint works, no cache validators; publicly promoted licensing products. Current free automated-use rights not established. Excluded rather than relying on an assumed licence. Official project RSS and Work. |
| BBC business RSS | Excluded/deferred | Endpoint works; current automated-reuse terms were not retrievable. Excluded from production pending rights review. Official macro/Fed sources and Work. |
| CryptoPanic API/free tier | Excluded/deferred | Developer route did not expose a usable current free contract. No verified no-payment/no-card rate allowance/terms. No architecture dependency. Primary RSS/status; Work. |
| Paid news/social/on-chain/exchange alerts (Reuters/Bloomberg/paid X/premium crypto intelligence) | Excluded/deferred | Excluded by hard £0 economic constraint; no credentials, subscription or commercial API integrated. Curated public feeds plus existing Work account. |
| Selected public on-chain event polling | Excluded/deferred | Deferred. No canonical transaction/log identity or bounded no-key reliable transport validated for this phase; existing OI/funding/microstructure remains active. Official exploit/incident notices plus Work. |
| BOJ/ECB/BOE/FOMC scheduled calendars and official sources | Excluded/deferred | Already Phase 23 territory; audited existing calendar/providers, no duplicate scheduler or calendar ingestion. Existing Phase 23 macro collection. |

Primary verification links:

- [Statuspage consumer API distinction](https://support.atlassian.com/statuspage/docs/what-are-the-different-apis-under-statuspage/), [Coinbase API](https://status.coinbase.com/api), [Kraken API](https://status.kraken.com/api), [Solana API](https://status.solana.com/api).
- [Official Hyperliquid status announcement](https://t.me/s/hyperliquid_announcements?before=115), [Circle status guidance](https://help.circle.com/support/en/contacting-circle-s-customer-care-team?id=kb_article_view&sysparm_article=KB0010914).
- [SEC RSS directory](https://www.sec.gov/files/about/secrss.shtml), [SEC developer resources](https://www.sec.gov/about/developer-resources).
- [GitHub public API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api), [security-advisory API](https://docs.github.com/en/rest/security-advisories).
- [Cointelegraph current terms](https://cointelegraph.com/terms-and-privacy), [Decrypt current terms](https://decrypt.co/terms-of-service).
- [GDELT DOC API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/), [2026 GDELT API migration](https://blog.gdeltproject.org/scaling-gdelt-for-a-new-era-migrating-to-spanner-with-agentic-interactive-gemini/), [2026 legacy infrastructure limitations](https://blog.gdeltproject.org/using-the-new-web-ngrams-dataset-to-find-relevant-coverage/).

## Rate limits and network economics

GitHub's free shared-IP limit is 60/hour; authenticated free access normally permits 5,000/hour. Four feeds at five minutes consume at most 48/hour before positive jitter. `PRISM_FREE_GITHUB_TOKEN` optionally uses an existing explicit token; it is never required, read from other applications or logged. Authenticated conditional 304 requests save primary quota. We also honor remaining/reset/Retry-After and secondary throttling.

The other selected public consumer feeds do **not** publish a numeric per-consumer rate contract. This is an explicit limitation, not an invented unlimited allowance. Registry cadence is Prism's known operating ceiling; observed request outcomes/rate headers and consecutive failures are measured in health. No attempt was made to hammer sources to discover an enforcement threshold.

- All requests use gzip, ETag and Last-Modified where supplied, with a shared connection pool.
- Polls use positive 0–10% jitter, bounded payloads (2 MiB decoded), connect/read timeouts and a total streaming-time bound.
- No inline sleep/retry loop: the next scheduler attempt is the retry. Exponential failure backoff caps at one hour; Retry-After dates/seconds and GitHub reset times may extend it.
- Daily unconditional refresh guards against a stale/broken ETag. Content/item hashes still reject unchanged bodies/reports.
- Four bounded polling workers isolate slow/failing sources; completion receipts are persisted by each worker independently.
- Source state caches next due times in memory. Idle ticks do not reread every feed state or parse every historical receipt. No continuously open DuckDB handle.

At stage 4 the configured ceiling is 10,656 requests/day; positive jitter reduces the typical ceiling to approximately 10,149/day, plus fetch duration/backoff. All 18 warm response bodies measured zero bytes (304). Cold compressed bodies total approximately 0.88 MB. These are short samples; headers, TLS and future content changes prevent claiming zero total bandwidth. Counters distinguish compressed payload bytes from decoded bytes; failed/oversize response accounting is best effort. Incoming payload is not Railway billable egress.

## Receipt, normalization and authority

```
public GET → deterministic parser/relevance/freshness → fsynced immutable receipt
          → authoritative worker/runtime lock → Phase 23 append-only ledger
          → committed event outbox → Phase 27 targeted PAPER evaluation
```

Publication/event times, canonical URL/provider, title, factual source summary, entities/assets, item content hash, source-specific ID, latest factual information time and Prism full-payload receipt time are preserved in the existing `Observation` model. Receipt time is sampled after the complete response body is received. Publication time, HTTP Last-Modified and aggregator `seendate` are never substituted for Prism knowledge time. GDELT availability time, if eventually measured, is distinct from unknown article publication time.

Receipts reuse Phase 26A immutable-file fsync and directory-fsync primitives on a separate bounded volume directory. Completion and attempts are append-only. Conditional validators/known IDs advance only after accepted observations are durably stored. A restart after receipt creation but before state publication safely reuses the original receipt and receipt timestamp. A full spool stops acknowledgement/validator advancement and backs off; pending receipts are never evicted to make room.

Ingestion runs under the existing runtime lock and Store writer lock, with an atomic receipt transaction. Ledger rows, source attribution and receipt result commit together. Completion is published only after commit and idempotent outbox/research enrollment. DB contention/failure retains the pending receipt for ordered catch-up. A crash after DB commit recreates missing outbox/enrollment/completion without duplicating events or triggers. Context availability is a conservative post-commit timestamp; receipt `first_seen_at` remains causal knowledge time. Collectors never open the production DB.

Phase 29 adds only `context_free_ingests` and `context_free_observations` attribution tables. The existing Phase 23 ledger is the sole event model; deterministic taxonomy/materiality scoring is unchanged. No maximum-importance field comes from a feed.

## Deduplication, corroboration and Work

Status IDs retain Phase 23 `statuspage:<entity>:<incident>` identity. Releases/advisories retain repository plus source event ID. Feed item hashing avoids revising an unrelated report when another item changes the feed. The ledger handles duplicates and append-only status/revision/resolution history. Exact canonical source URLs bridge later official reports to an earlier Work/news episode's existing key. Keyless news retains Phase 23's existing URL/entity-window model; this heuristic is imperfect and is not proof that arbitrary differently titled reports are identical.

Tests demonstrate a secondary report followed by official confirmation at the identical incident URL becomes one event with both provider identities and a 60-second discovery difference. Resolution appends history without changing `first_seen_at`. Production discovery comparisons use the retained original/update receipt history, so direct feed, broad discovery and Work can be compared for first discovery, relative delay and corroboration order when they converge on a logical event. No provider score becomes a trading weight.

Legacy Coinbase/Kraken/Fed/SEC/CFTC polling hands over only as each corresponding registry stage activates. It resumes automatically if this layer is disabled. Existing FOMC/BOJ/ECB/BOE schedules and official macro releases remain Phase 23; the Fed press classifier adds emergency/unscheduled context, not a competing calendar. Existing Work monitor prompts remain unchanged. Hourly Work monitors provide unusual-event coverage, semantic discovery, independent corroboration and later enrichment; their hourly scheduling is expected.

## Descriptive reaction research

The Phase 28A minute-data reaction collector is reused, with additive `free_event_reaction_v1` enrollment metadata. Existing Work-named pending/reaction tables are shared storage, not another event model. First enrolled post-commit availability is frozen per logical event/asset; later corroboration does not reset the reference price or erase history.

Horizons: **1m, 5m, 15m, 30m, 1h, 2h, 4h**. Existing measurements include returns, MFE/MAE for both directions, volume ratio, realized volatility, OI changes, funding endpoints and valid spread/depth data. Missingness is explicit; no interpolation manufactures minute outcomes for assets without prospective minute data. The crossing minute is excluded from extrema/volume. At least 24h grace allows delayed market-data catch-up. This is descriptive research, not PnL or a directional admission rule.

Provider metrics include detected/accepted/rejected/duplicate counts, logical events versus updates, publication-to-receipt samples, availability-to-receipt where genuinely measurable, later corroboration, reaction magnitude, filtered noise share and outside-execution-universe assets. False-report rate remains null unless independently classifiable; rejected/stale items are not presumed false. Comparison history is ordered by receipt times. HTTP acquisition health and interpretation quality are distinct.

## Hard cost gate and runtime evidence

`docs/evidence/phase29/free-source-cost-report.json` is the machine-readable **FREE_SOURCE_COST_REPORT**. Production collector startup and rollout commands reject a missing/stale registry hash or a selected source whose data/API/subscription cost is nonzero. No new paid service, account or paid tier is required. Total incremental data subscription/API cost is **£0/month**.

The preactivation Railway benchmark ran the exact acquisition modules in a temporary directory with a scratch DB on the existing `prism-runtime`; production DB/role/service configuration was untouched. It measured approximately 174 MiB collector RSS/peak, 0.480 CPU-seconds for two polls of each of 18 sources, 0.195 ingest CPU-seconds for two accepted receipts, ~22.6 KB scratch spool state/receipts and ~3.09–3.11 seconds receipt-to-context. The scratch DB was 9.45 MB including the entire empty schema, not incremental event storage.

Using [Railway's current service tariff](https://docs.railway.com/guides/right-size-cpu-memory) ($10/GB RAM-month, $20/vCPU-month, $0.15/GB volume-month, $0.05/GB egress), the conservative short-sample extrapolation is approximately **$1.87/month incremental infrastructure**. It assumes an illustrative 100 accepted observations/day and 75 MB combined ledger/spool growth/month, plus 0.1 GB outbound overhead allowance. The shared reaction collector's existing two-second/minute budget bounds additional research CPU at approximately $0.67/month in a continuously saturated worst case. These are estimates, not a measured bill or a claim of guaranteed arrival rate. No new permanent DB worker is added where the Phase 26A worker already runs.

Spool capacity is 128 MiB by default. Completed receipts are retained with history; no automatic archival deletion is introduced. Capacity health must be reviewed before long-term saturation. No historical news download/backfill is performed.

## Health, operation and staged rollout

- `market context sources status --json` reports registry and enabled/staged state, last poll/success/change, HTTP status, failure streak, rate state, next poll, events/bytes, process heartbeat and pending age.
- `market context sources status --check --json` fails for an unhealthy process, three consecutive source failures, or backlog older than five minutes. Supercronic checks it every minute when activated.
- `market context sources metrics --json` reports descriptive provider usefulness/discovery ordering. No paid/LLM scoring.
- `market context sources poll --stage 1` polls due sources once without opening a DB.
- `market context sources rollout 1|2|3|4` fsyncs a stage marker. Expansion takes effect on the next collector tick, without restarting paper execution. Lowering the stage is a reversible acquisition rollback.

Health request/byte/event windows use rolling hourly buckets (up to 25h), explicitly labeled; all-time counters are separate. Body counters exclude protocol headers/TLS overhead. Rate limits with no published numerical contract are explicitly marked undisclosed. Pending storage and process failures are independently observable.

Production activation is opt-in via `PRISM_FREE_SOURCES=on`; the default remains off in `.env.example`. The existing gateway writer drains free receipts every five seconds; if the gateway is disabled, a separate bounded authoritative ingestion worker provides the same handoff. Deployment and stage-by-stage health checks are recorded separately in `production-rollout.json`.

Production stages 1–4 completed on 2026-10-10. Final runtime revision is `1cf6c993297627de9300f37b4196d0c29c36e86d`: all 18 selected sources are healthy, both process heartbeats are healthy and the durable backlog is zero. GDELT remains disabled. The first two live observations (Kraken and Circle) became two logical events and two targeted outbox triggers; the existing paper evaluator consumed both. Receipt-to-context was **21.280s and 23.934s**, including existing runtime-lock contention. Their source publication timestamps describe older incidents with fresh updates, so they do not measure new-event publication latency.

The final collector used **158.6 MiB RSS, 168.0 MiB peak**. Over a measured 106.277-second interval it used **0.100 CPU-seconds**, made **10 requests** and received **31,512 compressed body bytes** (212,155 decoded). The short-sample incoming-body extrapolation is approximately **25.6 MB/day**; actual future traffic depends on feed changes and validators. Physical source spool/state/history was **26,166 bytes**. The conservative $1.87/month preactivation infrastructure estimate is retained rather than treating this brief quiet sample as a measured monthly bill.

There are **19 free-event/asset research enrollments** and **six valid 1-minute outcomes** already stored. Longer horizons continue prospectively. Seven enrolled event/asset cases currently have no minute data and six have minute coverage gaps; the shared collector retries within its existing grace window and preserves missingness. Initial outcomes include returns, OI, funding and valid spread/depth. Full-minute extrema/volume may be null at 1m because the crossing minute is excluded. No meaningful coverage/latency/provider ranking follows from two events. Outside the execution universe, these episodes preserve BNB, XMR, ZEC and USDC links without adding execution eligibility.

An initial startup watchdog check preceded the first ingestion heartbeat. Subsequent checks pass. Full registry JSON every minute hit Railway's log-rate limit during rollout; the final watchdog uses compact text, while manual status retains JSON. Paper run/activation, baseline, universe, risk, execution and thesis/exit policy identities match the predeployment snapshot; the run remains ACTIVE, paper-only, with zero open positions at validation. Production audit, health, reaction and usefulness snapshots are in `docs/evidence/phase29/`.

Expected exchange polling delay is at most roughly 60–66 seconds plus fetch/writer contention; RSS/GitHub use 300–330 seconds. These are configured bounds under healthy operation, not measured event-publication guarantees. No new incident first appeared during the short benchmark: the initial accepted reports were fresh factual updates on older incidents. Availability-to-Prism delay is therefore not yet measurable, and no 15-minute GDELT promise is made.

## Safety and validation

No source has order, sizing, balance, paper position or admission authority. A context event is not a trade. The outbox can dispatch existing registered hypotheses only; Phase 27 currently has no registered event-direction entries. Event wakeups retain the same fee/slippage/funding/current settlement/expected-cost/COST_DECAY admission and exit implementation. Existing tests exercise that implementation; this phase does not route around it.

Validation covers conditional 200/304, timeout/DNS, 403/429 reset/Retry-After, 500, retry/backoff, stale ETag, malformed XML/JSON/schema changes, RSS/Atom, relevant/irrelevant GitHub releases, status create/update/resolve, clock skew and freshness, causal timestamps, exact-URL cross-provider dedupe, fsync/restart, DB failure/catch-up, post-commit recovery, source failure isolation, shared writer handoff and £0 activation gate. Full requested phase/runtime and pytest/Ruff/format/diff results are recorded in `test-results.json`.

## Recommended next phase

**Specific event-reaction hypotheses.** Accumulate prospective direct-feed episodes and their 1m–4h descriptive paths, then define and falsify one bounded exchange/venue-incident reaction hypothesis against the unchanged six-asset universe, including realistic funding/fees/slippage. Discovery transport is now measurable; neither strategy profitability nor a benefit from a wider universe is established. Do not start that phase as part of acquisition rollout.
