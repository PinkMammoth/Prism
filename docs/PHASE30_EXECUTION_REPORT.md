# Phase 30 execution report

## Current wiring audit

Phase 29 public collectors → durable receipts → authoritative ingestion → Phase 23 causal logical event/update/provenance → committed information outbox and notice → Phase 27 paper worker → Phase 30 per-report classifier → frozen event opportunity/thesis → Phase 27 admission → future executable Hyperliquid bid/ask → paper position. Phase 26A OAuth Work ingestion and Phase 28A specialist monitors join at the same context/outbox boundary. Providers never place trades. Quote ticks run every 2s and complete-minute state/research work every minute. Runtime locks, independent per-run receipts and restart recovery remain.

The event trader seeks to exploit short-term market reaction, not determine the ultimate truth of the underlying narrative.

Source speed matters only insofar as a viable tradeable move remains when Prism can execute.

## Run identity

Adding hypotheses changes the frozen policy/universe, so the existing active run is preserved. `paper_nimble_event_v1` has six new identities plus the unchanged original 28. Two independent cohorts each select 31 identities: original 28 plus their three immediate or confirmed event hypotheses. Each starts at $100, unchanged 20% notional, 2x isolated leverage, $10 minimum, three-position/60% gross caps and lot rounding. Separate accounts prevent an immediate position from mechanically blocking its paired confirmed experiment. Their actual dispatch delay and capital conflicts remain observable, and evidence is never pooled.

Frozen event policy: `paperexecution_event_12208f088bec2bcfaa8dfe10af41ccac81975d012cd2e5b0919ec5918712c885`.

Immediate universe: `paperuniverse_event_1caa53ff64e6f2afe727bbf1c9df485acccac8ea9054eb292292175f2ba7324f`.

Confirmed universe: `paperuniverse_event_4a76847cbd314501804511b4278e4a878c8ac0e79454683cd7514339f0cb180a`.

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
| quiet: 60 active minutes | 2.435 | 2.922 | 212.54 | 0 | 122 | 18,696 | 6,828,032 |
| 10 events/day | 4.268 | 5.419 | 263.75 | 60 | 182 | 640,114 | 13,905,920 |
| 100 events/day | 43.706 | 56.104 | 318.15 | 600 | 1802 | 6,464,458 | 43,266,048 |
| 100 duplicate reports | 14.338 | 18.503 | 318.15 | 10 | 406 | 169,134 | 15,740,928 |

Quiet 1,000 unactivated filesystem polls: 0.027s wall / 0.026s CPU, zero DB writes. Quiet active-event CPU extrapolation from 60 sampled minutes: 70.13 CPU seconds/day across both cohorts, before technical adapters. Quiet execution rows include two activations and 120 account marks; duplicate-burst execution rows include explicit monitoring/account marks. RSS is cumulative process peak, not incremental cohort memory. Database allocations include schema/checkpoint overhead. No synthetic PnL tuning.

Benchmark excludes network, writer contention, existing technical adapters and steady billing; it is not a production latency SLA. Existing infrastructure and all 18 free sources remain; additional data/API subscription cost **£0/month**. Actual production combined-worker sample: **15.35 CPU seconds / 60.004s wall**; RSS 285.27 → 285.55 MiB. The pre-deployment single-run sample used 3.99 CPU seconds / 60.010s, RSS ending 231.66 MiB. These different-time samples are not a controlled marginal-cost comparison. Three independent accounts each retain their original 28 evaluators; their technical work and ordinary runtime writer contention are included. During the measured minute, the execution ledger wrote exactly **three account marks / 492 payload bytes**, including two successor marks / 288 bytes, and **zero event-evidence rows**. A separate sample overlapping operator audit locks reached 698.71 MiB RSS at its endpoint; the later sample ended at 285.55 MiB. The existing container reports an 8,000,000,000-byte memory ceiling. No infrastructure tier or service was added. Source data subscriptions remain £0; compute usage is not claimed to be free.

The final supervised worker heartbeat at 2026-10-10T19:23:58.782731Z is OK, with all three children IDLE after successful scheduled evaluations. Natural BUSY states are lock-contention retries, not forced fills; no ERROR was observed in these samples. Full measurements and limitations: [production runtime](evidence/phase30/production-runtime.json), [measured ledger writes](evidence/phase30/production-final-ledger.json).

## Production activation

Exact production code revision: `57e7a01fabcce940da00a370410dfd33a12ac4c7`, Railway deployment `6ff52df0-e990-453b-a886-29f9c0da2b46`, schema **30**, authoritative runtime `railway-prism-runtime`. Verified pre-deploy schema-29 backup: 445,394,944 bytes, SHA-256 `4cdac6665d06e3bbdd7c7aef2b1349eb1952ebef96acef34fdaa1ebe4c7a70e6`. Post-deploy preflight reports ready_for_jobs=true; existing authority claim unchanged. Operations deployment audit `rtev_0ebcd144611f407ab352331616f094bd` is recorded.

Both cohorts activated atomically at **2026-10-10T16:15:55.610293+00:00**:

| Variant | Run ID | Starting equity |
|---|---|---:|
| Immediate | `paperrun_nimble_a5fafb2cf008686ad99d2e1e039d428cd95349423d2e31778409810be6cce3d3` | $100 |
| Confirmed | `paperrun_nimble_981fac864a79b04053e1ca081731a692db95b5e69cec9f29e74618e6e84e0528` | $100 |

Policy and universe IDs are under Run identity. Three active runs appear in the supervised worker manifest. Each successor has 31 hypotheses, original baseline has 28, all original hypothesis IDs match, and fixed-horizon paper v2 prefix/content integrity verifies for all three. No synthetic event was sent to production. All 18 sources are OK and collection/ingestion backlog is zero at final observation. A transient research-only Fed RSS HTTP 404 recovered on its next ordinary poll; no source gate was weakened. Work gateway runs with OAuth configured and zero backlog. [Activation evidence](evidence/phase30/production-activation.json), [source health](evidence/phase30/production-source-health.json).

The implementation is committed on `codex/phase30-event-reaction` from Phase 29 main `946c066f682a3ebbe0691b288e030ef4bd3bd884`. GitHub HTTPS and SSH authentication were unavailable; the user will push the complete branch after the final documentation/evidence commit. No merge to main is claimed. A local verified Git bundle is available for recovery. Production was deployed from the exact committed archive; later evidence-only commits do not change the deployed code.

## Prospective evidence

Authoritative initial audit at **2026-10-10T19:20:15.073936Z**, confirmed at **2026-10-10T19:23:56.173483Z**: each cohort has **one observed logical event, one UNSUPPORTED_EVENT_CLASS classification rejection, zero eligible events, zero event trades, zero open positions and $100 equity**. The same event is counted separately in each scientific cohort, not as two distinct real events. Rejected-opportunity evidence has begun accumulating. These are initial observations several hours after activation, not fabricated activation-time outcomes; no event edge is claimed. [Initial prospective ledger](evidence/phase30/prospective-initial.json). A lack of qualifying events is acceptable. Per-hypothesis reports retain counts, rejects, latency, missed movement, MFE/MAE, duration, fees/funding, net expectancy/win rate and exit reasons. No-reaction expiry is a false-positive proxy, not a truth verdict. Immediate/confirmed paired entries show extra delay/missed movement and whether immediate had already exited; rejected waits plus forward outcomes show missed opportunities. No automatic graduation and no edge claim.

## Tests

Final full pytest: **1,305 passed, 0 failed, 1 warning in 1,243.41s**. This includes Phase 30, Phase 29, Phase 28A, Phase 27, Phase 26A, Phase 25A, Phase 24B, Phase 24A, Phase 23 and runtime tests. The focused Phase 30 + nimble runtime run passed **56 tests in 16.01s**. Separate worker/intent-clock integration checks passed **2 tests in 3.94s**. Warning: existing Starlette httpx test-client deprecation. Ruff check passed; Ruff formatting check passed (361 files); deployment-helper shell syntax and git diff --check passed.

An earlier full run had 1,304 passes and one untouched Lab CLI time-order failure; all 21 tests in that module passed independently, and the final full run passed. An earlier requested 399-test regression run had 398 passes and one untouched scheduling alert-count failure; all nine scheduling tests passed independently and the final full run passed. These attempts are retained in [validation evidence](evidence/phase30/validation.json). Safety assertions were updated only for the authorized Phase 30 paper context consumer and migration-18's own schema contract; source-side/live-execution prohibitions remain.

Deterministic synthetic scenarios cover all requested families' immediate/no/delayed/failed reactions, confirmation help/delay/missed target, entry clocks, reference, move missed, episode updates/corroboration, TP, price/flow/resolution invalidation, expiry, funding cost/credit/decay, conflict/exposure, post-exit research and Hyperliquid entry/exit outage. Historical sanity is restricted to the frozen Solana 2024 outage and SEC 2024 approval mechanics, with unknown historical Prism discovery/executable prices left null; no profitability inference or parameter fitting.

## Safety

Paper-only deterministic execution, no private order transport, no LLM hot-path decision, no headline-only direction, no new assets/data subscription/service, no forced trading, no sizing changes and no automatic graduation. Provider labels cannot bypass Phase 23 confidence. Append-only event and execution evidence never rewrites entry knowledge. Reports/dashboard preserve cohort and baseline comparisons. One existing daily Telegram summary includes cohort totals; no per-trade/event spam.

## Recommended next phase

**Continued prospective observation.** The implementation has causal mechanics and frozen economic hypotheses; it has no prospective profitability evidence yet. Broader universes or additional hypotheses would increase experimental ambiguity before the immediate-versus-confirmed question can be measured. Do not begin another phase as part of this change.
