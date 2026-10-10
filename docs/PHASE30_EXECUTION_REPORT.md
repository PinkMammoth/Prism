# Phase 30 execution report

## Current wiring audit

Phase 29 public collectors → durable receipts → authoritative ingestion → Phase 23 causal logical event/update/provenance → committed information outbox and notice → Phase 27 paper worker → Phase 30 per-report classifier → frozen event opportunity/thesis → Phase 27 admission → future executable Hyperliquid bid/ask → paper position. Phase 26A OAuth Work ingestion and Phase 28A specialist monitors join at the same context/outbox boundary. Providers never place trades. Quote ticks run every 2s and complete-minute state/research work every minute. Runtime locks, independent per-run receipts and restart recovery remain.

The event trader seeks to exploit short-term market reaction, not determine the ultimate truth of the underlying narrative.

Source speed matters only insofar as a viable tradeable move remains when Prism can execute.

## Run identity

Adding hypotheses changes the frozen policy/universe, so the existing active run is preserved. `paper_nimble_event_v1` has six new identities plus the unchanged original 28. Two independent cohorts each select 31 identities: original 28 plus their three immediate or confirmed event hypotheses. Each starts at $100, unchanged 20% notional, 2x isolated leverage, $10 minimum, three-position/60% gross caps and lot rounding. Separate accounts prevent an immediate position from mechanically blocking its paired confirmed experiment. Their actual dispatch delay and capital conflicts remain observable, and evidence is never pooled.

Frozen event policy: `paperexecution_event_82920446103d0b5e33c4b6bd9c1bbe5d84562a4e0caa23a6be0f5696829ef375`.

Immediate universe: `paperuniverse_event_034495acbea55406842ddeebf225d29ed607cdb971edbdb367dcb1245f44a2a2`.

Confirmed universe: `paperuniverse_event_22cffc742555b639476a00656193835750f72b3f5a4274f5155069caacbb2be9`.

Preserved baseline policy: `paperexecution_nimble_ec40a2968bcc33f7c7654a1c8d7dcc200361e88bca99079cb4f45738ba785501`.

Original baseline run: `paperrun_nimble_a8aef8be36c02700688053a1e57d2f908573fe8898860450b00b889d4f98f54c`.

Paper v2 fixed-horizon baseline: `paperrun_v2_3811cccc40db4fbe6413ebd018c129d1026748f3cd72e619ec80ba88e47ada95`.

## Event families

Exactly six registered names:

- `venue_disruption_immediate_v1`
- `venue_disruption_confirmed_v1`
- `protocol_stress_immediate_v1`
- `protocol_stress_confirmed_v1`
- `sec_spot_etf_approval_surprise_immediate_v1`
- `sec_spot_etf_approval_surprise_confirmed_v1`

The regulatory pair is deliberately dormant without structured official contrary-expectation evidence. Today's ordinary SEC RSS parser does not populate it. Stablecoin, bridge, general regulatory/institutional announcements, ordinary releases/advisories and withdrawal-only problems remain research-only. No activity quota, generic panic rule or tiny-sample fitting.

## Immediate vs confirmed

Immediate requires deterministic event eligibility and execution gates, with no agreeing market signal. Confirmed additionally requires a fully post-context-availability COMPLETE continuous minute with signed mid return >0 and signed taker fraction ≥0.10. Fraction is `(buy_notional-sell_notional)/(buy_notional+sell_notional)`; long sign +1, short −1. Signal ingestion must be causal and its minute close ≤90s old. Missing signals wait only while freshness/window survives; waiting evidence deduplicates by minute. Record observed/available confirmation clocks and extra delay. No six-filter stack or shared results.

## Direction mappings

Severe Coinbase/Kraken core spot-trading outage → BTC short only: interruption of important BTC spot liquidity/access while another venue remains executable. SOL official cluster halt → SOL short only. Confirmed native protocol exploit/emergency pause → its explicitly affected supported native token short: bitcoin→BTC, ethereum→ETH, solana→SOL, hyperliquid→HYPE, chainlink→LINK, aave→AAVE. Require the exact protocol field, entity and asset; an application/bridge on Ethereum does not map to ETH. Narrow SEC spot ETF approval against a prior official contrary expectation → underlying BTC or ETH long only. All mappings are economic hypotheses, not established edges.

## Eligibility

Each report uses its own capped Phase 23 confidence and structured facts, never merged attributes or headline sentiment. Require live, non-denied, causally published data; confidence at least REPORTED. Work remains capped at REPORTED even when it cites official material. Registry source class controls confidence.

| Family | Required structured evidence | Minimum Phase 23 materiality | Max information age |
|---|---|---:|---:|
| Venue | Allowlisted Coinbase/Kraken status provider/hostname, exchange notice, impact major/critical, investigating/identified, frozen core trading component major outage | 40 | 300s |
| Protocol | Official SOL major/critical cluster outage; or protocol/security/AI source, confirmed exploit/pause, target_kind=protocol, loss ≥$10m, exact native mapping | 60 | 3600s |
| Regulatory | Official SEC provider/class/hostname, approved spot_etf, underlying BTC/ETH, docket, prior official expected_decision=rejected for same docket known before first_seen within 24h | 60 | 5400s |

Exact component IDs and exclusions are in [the frozen rules](EVENT_REACTION_HYPOTHESES_V1.md). First publication anchors age; a meaningful official structured transition may use its new information time. Prose/corroboration never refreshes age. One logical event/asset per cohort is consumed once, including terminal rejected attempts; resolution/escalation cannot reopen it. Corroboration strengthens metadata with zero size increase. Assets outside AAVE/BTC/ETH/HYPE/LINK/SOL never trade.

## TP

One simple pre-event volatility objective: `P0*(1+s*0.5*V15*sqrt(W/15))`, where `V15=sqrt(15)*mean(causal minute true range)/P0`. P0 is the last complete executable bid/ask sample known strictly before Prism first_seen, quote age ≤120s; require 15 complete continuous causal production minutes. It is a research reference, never a fill. The target stays fixed while confirmation waits. No parameter grid search or recentring to rescue a missed move.

## Invalidation

Price reclaim level `P0-s*entry_observed_full_spread`, frozen at entry. Two consecutive fully post-entry minutes with opposing signed taker fraction ≤−0.35 invalidate state. Matching incident resolution/monitoring/denial or a qualifying opposite event within 900s invalidates the thesis. Conflicting new event entries are suppressed. Resolution never creates an opposite trade. Existing gap/emergency/data-failure guards remain.

## Reaction windows

Venue immediate/confirmed: **20m / 45m**. Protocol: **60m / 120m**. Regulatory: **120m / 240m**. Phase 27 thesis expiry applies when this interval from event context availability passes and progress is below 25% of target distance. Profit and invalidation exits take priority; time is not a compulsory fixed-horizon exit.

## Max holds

Venue immediate/confirmed: **60m / 90m**. Protocol: **120m / 240m**. Regulatory: **240m / 480m**, measured from actual fill; backstops only. Forward reaction curves at 1/5/15/30/60/120/240m plus each primary window continue after dynamic exit and for fired rejected signals with causal references.

## Funding

Retain current signed hourly funding, next settlement and expected first signed cost/credit; reserve adverse funding through ceil(max_hold/60) settlements. Observed settlements/reconciliation remain. COST_DECAY terminates stale marginal trades after half the reaction window, below 80% target progress, within 120s of settlement when adverse funding exceeds 25% remaining move. Beneficial funding does not trigger it. Missing admission funding fails data eligibility.

Taker fees remain 4.5bp per side; per-side slip BTC/ETH 2bp, SOL 4bp, HYPE/LINK 6bp, AAVE 8bp plus observed spread. Remaining target must cover ≥2× round-trip costs including adverse funding reserve. Recheck economics at actual fill. Reject UNECONOMIC_TARGET when the move is already consumed. Refresh clocks/quotes during bursts; intent is stamped after thesis preparation. Fills need both exchange and availability clocks strictly after intent and ≤3s old, and cross executable bid/ask plus adverse slip.

## Source latency

Store source_publication_at, source_revision_at, collector_observed_at, prism_first_seen_at, context_available_at, first_evaluator_notice_at, evaluator_noticed_at, decision_at, intent_at, quote_at, quote_received_at, quote_available_at, fill_at; Work adds work_discovery_at, work_sent_at and gateway_received_at where supplied. Null clocks remain unknown.

First provider/corroborators and latency differences are reported, with observed freshness/remaining-target-cost/confirmation/venue diagnostics for later sources. These do not invent historical executable quotes or counterfactual fills. Hourly Work remains useful within protocol/regulatory freshness when enough move remains; 30–60m late venue reports exceed this venue rule's short freshness by design.

## Move missed

`PRE_ENTRY_REACTION_CAPTURED`: reference_price, availability_price, fill_mid, fill_price, signed_move_before_availability, signed_mid_move_before_fill and signed_move_before_fill. Positive signed movement is expected-direction movement already missed before entry. Actual fill movement includes crossing/slip, so the mid measure isolates price movement. Availability price uses a causal observed quote/sample or stays missing; its basis/time are explicit. Subsequent MFE/MAE and independent forward curves stay separate.

## Rejection taxonomy

STALE_EVENT, LOW_MATERIALITY, UNSUPPORTED_EVENT_CLASS, AMBIGUOUS_DIRECTION, ASSET_OUTSIDE_UNIVERSE, VENUE_UNAVAILABLE, UNECONOMIC_TARGET, CONFIRMATION_MISSING, DATA_DEGRADED, DUPLICATE_EPISODE, CONFLICTING_EVENT, EXPOSURE_LIMIT. Detailed Phase 27 quote/funding/geometry/conflict/exposure/cancellation reasons are retained alongside event diagnostics.

## Confluence

Freeze aligned fired original hypotheses from the preceding 15m, their IDs/families and event_only / event+technical / event+microstructure / event+positioning / event+multiple. This is descriptive only. Existing Phase 27 invalidation-first conflict/reversal logic releases capital only after a future-quote close; opposite exposure is blocked. Same-side support has zero size increase. Original 28 identities/predicates/grids and baseline policy are unchanged.

## Hyperliquid outage safety

Active official Hyperliquid major/critical exchange outage blocks successor entries as VENUE_UNAVAILABLE, cancels pending intents and prevents **both entry and exit paper fills** until official recovery/monitoring. No expiry silently restores venue availability. Market reaction research continues. No registered Hyperliquid venue-outage short and no fictional normal executable trades on an unavailable venue. The baseline frozen policy is preserved; the new guard also covers technical trades in the successor accounts.

## Runtime

The [frozen engineering benchmark](evidence/phase30/benchmark.json) measured two cohorts on the existing local host:

| Scenario | Wall s | CPU s | Peak process RSS MiB | Event evidence rows | Execution ledger rows | Payload bytes | DB bytes |
|---|---:|---:|---:|---:|---:|---:|---:|
| quiet: 60 active minutes | 2.874 | 3.451 | 210.43 | 0 | 122 | 18,696 | 6,828,032 |
| 10 events/day | 5.633 | 6.844 | 262.48 | 60 | 182 | 640,114 | 13,905,920 |
| 100 events/day | 48.868 | 62.777 | 326.09 | 600 | 1802 | 6,464,388 | 42,741,760 |
| 100 duplicate reports | 16.713 | 21.569 | 326.09 | 10 | 406 | 169,134 | 15,740,928 |

Quiet 1,000 unactivated filesystem polls: 0.040s wall / 0.039s CPU, zero DB writes. Quiet active-event CPU extrapolation from 60 sampled minutes: 82.81 CPU seconds/day across both cohorts, before technical adapters. Quiet execution rows include two activations and 120 account marks; duplicate-burst execution rows include explicit monitoring/account marks. RSS is cumulative process peak, not incremental cohort memory. Database allocations include schema/checkpoint overhead. No synthetic PnL tuning.

Benchmark excludes network, writer contention, existing technical adapters and steady billing; it is not a production latency SLA. Existing infrastructure and all 18 free sources remain; additional data/API subscription cost **£0/month**. Actual combined-worker production sampling is recorded after activation.

## Production activation

Activation awaits final full validation and deployment of an exact recoverable committed revision. Both paper cohorts will activate atomically with one actual clock, preserving the existing nimble/v2 baselines. No synthetic event is sent to production.

## Prospective evidence

Initial authoritative production state will be recorded after activation. A lack of qualifying events is acceptable. Per-hypothesis reports retain counts, rejects, latency, missed movement, MFE/MAE, duration, fees/funding, net expectancy/win rate and exit reasons. No-reaction expiry is a false-positive proxy, not a truth verdict. Immediate/confirmed paired entries show extra delay/missed movement and whether immediate had already exited; rejected waits plus forward outcomes show missed opportunities. No automatic graduation and no edge claim.

## Tests

Final validation is in progress. The final Phase 30 plus runtime suite passed **55 tests**, one pre-existing Starlette deprecation warning, in **16.59s**. Separate worker/intent-clock integration checks passed **2 tests in 3.94s**. Ruff and formatting passed (361 files), and git diff --check passed. The prior requested regression run had one obsolete Phase 23 blanket consumer assertion; it now exempts only the authorized Phase 30 paper consumer, preserving source-side/live-execution prohibitions. An under-load gateway heartbeat failure passed in isolation and is being rechecked in final suites.

Deterministic synthetic scenarios cover all requested families' immediate/no/delayed/failed reactions, confirmation help/delay/missed target, entry clocks, reference, move missed, episode updates/corroboration, TP, price/flow/resolution invalidation, expiry, funding cost/credit/decay, conflict/exposure, post-exit research and Hyperliquid entry/exit outage. Historical sanity is restricted to the frozen Solana 2024 outage and SEC 2024 approval mechanics, with unknown historical Prism discovery/executable prices left null; no profitability inference or parameter fitting.

## Safety

Paper-only deterministic execution, no private order transport, no LLM hot-path decision, no headline-only direction, no new assets/data subscription/service, no forced trading, no sizing changes and no automatic graduation. Provider labels cannot bypass Phase 23 confidence. Append-only event and execution evidence never rewrites entry knowledge. Reports/dashboard preserve cohort and baseline comparisons. One existing daily Telegram summary includes cohort totals; no per-trade/event spam.

## Recommended next phase

**Continued prospective observation.** The implementation has causal mechanics and frozen economic hypotheses; it has no prospective profitability evidence yet. Broader universes or additional hypotheses would increase experimental ambiguity before the immediate-versus-confirmed question can be measured. Do not begin another phase as part of this change.
