# Phase 27 execution report

## Current Paper v2 audit

The baseline is `EXPLORATORY_FIXED_HORIZON_BASELINE`, run
`paperrun_v2_3811cccc40db4fbe6413ebd018c129d1026748f3cd72e619ec80ba88e47ada95`,
activated `2026-10-09T10:19:06.981566+00:00`. Its 28 hypotheses, 14 long/14 short,
$100 start, sizing, policies, signals and historical trades are preserved.

V2 evaluates at :07/:22/:37/:52, opens using delayed complete-minute/mid or coarse-bar
references and normally exits after the research horizon. ETH/LINK signal-to-notice
was 356.57/352.12 seconds. All three observed positions used intent
12:07:03.539457, reference quote 12:08, quote availability 12:21:03.550685 UTC.
The latest production audit still has those ETH/LINK/SOL positions open, zero closes,
and missing funding prevents their scheduled exits. That behavior cannot manage
minute-scale reaction theses. It is retained as baseline evidence, not repaired in place.

[Read-only audit](evidence/phase27/v2-production-audit.json) includes definition and
evidence digests; activation captures ordered prefix digests for future verification.

## Nimble architecture and latency

Public context gateway → bounded gateway spool → authoritative context commit →
information-only durable evaluation outbox and filesystem notice → authoritative
nimble worker → relevant registered source/asset entries → immutable thesis/intent →
a later fresh executable public bid/ask → paper fill.

The public WebSocket collector writes a two-second atomic latest-book cache and
finalizes minute features after its existing grace. The worker polls cheaply every
two seconds with no DB open between ticks. It obtains the existing runtime lock only
for new information, minute monitoring, or a possible position action; all DB mutations
use the authoritative writer. No whole-universe evaluation follows a context event.
Technical and positioning availability are discovered once per minute, targeted by asset.

Entry and exit store condition/trigger time, evidence availability, evaluator notice,
intent, quote exchange/receipt/availability and fill. Quotes must be ≤3 seconds old.
Synthetic engineering tests show two-second intent-to-future-fill timing. This is
not a production SLA: writer contention, feature computations, missing quotes and
sampled touches can delay or miss a crossing. Phase 26A's measured ~3s Work→context
and ~1.5s receipt→context remain source metadata, not assumed universal Work speed.
Production latency observations are recorded in the activation evidence after deployment.

## Research versus execution; reused universe

Reuse 14 Phase 22 technical probe/control, six Phase 23 positioning and eight
Phase 24B microstructure identities. Original scientific verdicts and predicates
are unchanged. In particular, microstructure entry features retain their frozen
15-minute grid and 288-window prior normalization; a new minute wakes evaluation,
but does not invent a new one-minute entry strategy under an old scientific ID.

Freeze primary plus 15/30/60/120/240/480-minute research horizons separately from
position lifecycle. Independent forward observations persist after close, including
fired rejected signals where a causal reference quote exists. Their reference is
known quote at notice, with delayed/missing paths explicitly flagged. They are a
separate descriptive execution study and never overwrite original scientific outcomes.

## TP, invalidation, expiry and maximum holds

| Entry family, both sides | Target | Reaction window | Max hold |
|---|---|---:|---:|
| Continuation core | Half causal realized range-equivalent move | 5m | 15m |
| Persistent continuation | Half volatility move | 15m | 30m |
| Book/OI absorption | Opposite causal local extreme | 30m | 60m |
| OI price stall | 1R from structural invalidation | 30m | 60m |
| OI directional pressure | 1R | 60m | 120m |
| Crowding vulnerability | 1R | 60m | 240m |
| Hourly trend technical pair | Half ATR-equivalent move | 240m | 480m |
| Remaining technical pairs | Half ATR-equivalent move | 60m | 240m |

Price invalidation freezes the causal low minus one spread for longs, high plus one
spread for shorts. Geometry uses 15 continuous complete known minutes; technical
and positioning families can fall back to a fresh known 1h/15m bar respectively.
State invalidation is two post-entry consecutive opposing-flow minutes at signed
fraction ≤−0.35; absorption defence falls below half its original depth while price
moves adversely; crowding becomes neutral without response; OI expansion becomes
nonpositive. Missing state is unknown, not evidence of failure.

Expiry fires after its reaction window when executable favourable progress is below
25% of the fixed objective distance. Max hold is a backstop from actual opening.
Entry rejects already-expired signals. Every exit records its class, exact detail and
clocks. A separate 10% emergency guard and isolated-margin gap cap protect modeled
loss; raw price losses and explicit liquidation adjustment are reported, not hidden.

Immediate/confirmed event contracts are supported but **unregistered**, with distinct
future identities, 5m/15m reaction windows, pre-event-price invalidation and 15m/30m
backstops. Their future research curves are 1/5/15/30/60/120m. The 28-entry universe
has no event-direction predicate: context wakes record relevant dispatch/corroboration
with zero unregistered event admissions. A transfer is a market-reaction hypothesis,
not a claim that a government is selling. No direct watcher is added in this phase.

## Funding, costs and fills

Record current signed predicted funding, next hourly settlement, incremental cost or
credit, cumulative observed settlements, missing settlements and conservative reserves.
When a thesis is at least half its reaction age, below 80% of TP progress, funding is
within 120s and adverse marginal funding exceeds 25% of remaining target distance,
exit as `COST_DECAY`. Beneficial funding does not trigger this rule. Missing settlement
does not block an exit: release capital on confirmed future quote, reserve adverse
estimates, and append later funding reconciliation without rewriting the close.

Preserve 4.5bp taker fees per side; slip per side BTC/ETH 2bp, SOL 4bp, HYPE/LINK 6bp,
AAVE 8bp, plus observed full spread. The predeclared target must exceed twice total
estimated round-trip execution cost and adverse funding reserve or admission is
`UNECONOMIC_TARGET`. Costs never enlarge targets. Baseline fills cross ask/bid with
adverse slippage, use a quote after intent and never assume maker or historical wick fills.
Both TP/invalidation inside a post-entry coarse interval produces conservative
`AMBIGUOUS` ordering, not favourable TP precedence. Full exits only.

The $100 account, 20% notional, 2x isolated leverage, three positions, 60% gross,
original lot rounding and $10 minimum remain. Below approximately $50 equity the
minimum can obstruct exploration; no account enlargement or silent normalization.

## Throughput and resource audit

Local synthetic load measures execution engineering, not profitable market trading.

| Trades/day equivalent | Wall seconds | CPU seconds | Ledger events | Peak RSS MiB |
|---:|---:|---:|---:|---:|
| 10 | 0.875 | 0.675 | 91 | 196.4 |
| 50 | 6.671 | 3.330 | 451 | 202.9 |
| 100 | 10.833 | 6.860 | 901 | 207.1 |

500 signal decisions took 9.573 wall/5.942 CPU seconds and produced 500 opportunity
rows. One thousand unactivated polls used 0.0092 seconds and zero DB writes. One
hundred trades stored ~1.09MB of event payload, plus separate immutable theses and
opportunities. Closed history is not folded on each quote poll; minute marks are
compact deltas. Telegram defaults to daily summaries and unusual failures, with
optional trade details in one summary, never per-scalp spam by default.

The Railway **scratch snapshot**, without activation or spool-watermark mutation,
measured targeted one-asset adapters at 0.322–0.502 wall seconds; six-asset microstructure
work at 2.879 wall/3.379 CPU seconds. Peak RSS 636.5MiB during snapshot queries and
224.3MiB after closing DB; these are not steady production-worker billing measurements.
Microstructure entry warmup was insufficient, so no activity is manufactured.

The existing v2 average was 3.654 wall/4.211 CPU seconds per 15-minute evaluation,
about 404 CPU seconds/day (0.00468 average vCPU). Its compute supports a seven-day
parallel comparison, subject to production contention and disk monitoring. Faster
shared ingest improves future baseline data availability; this collection difference
must be disclosed in A/B interpretation. [Local benchmark](evidence/phase27/benchmark.json)
and [Railway source benchmark](evidence/phase27/railway-source-benchmark.json) include
limits, full measurements and policy identity. Actual Railway observations follow activation.

## Capital reuse, diagnostics and comparison

Confirmed exit updates cash/equity and releases exposure before subsequent admission
in the same transaction. Long→confirmed close→short is permitted; OPEN/EXIT_PENDING
opposing exposure is blocked. Fresh opposing entries cancel deterministically;
same-side support/corroboration records metadata with zero extra exposure. Canonical
event episodes deduplicate reporting; cooldowns prevent identical-state churn.

Independent reports compare trades, gross/net price PnL, fees, funding, slippage, holds,
turnover, drawdown, profit factor, win rate, expectancy/trade/day and capital utilization
from a common activation window. MFE capture and MAE avoidance require complete
original-primary-horizon paths; unavailable metrics remain null. Daily attribution
separates costs from thesis price performance. Opportunities record blocking positions,
staleness and tied-up notional. Shared Phase 25A postmortem gains a separate nimble
view of entries/exits, decisions and whether exposure survived or exited before a move.

Degradation requires at least 100 final-accounting trades and a last-50 mean net
return ≤−0.5% with at least 40 losses; a small streak never retires a hypothesis.
No retrospective tuning or automatic replacement occurs. A week of profit does not
graduate any strategy to live.

## Tests and safety

Phase 27 tests cover research after dynamic exit, future-quote TP timing, price/state
invalidation, expiry/backstop, funding cost/credit/reconciliation, fees/slip, uneconomic
entry, 5m/15m lifecycles, rapid capital reuse/reversal/conflicts, episode deduplication,
context/minute targeted wakeups, known-at, ambiguous ordering, restart/thesis recovery,
throughput, paper-only isolation, gap handling and scheduler integration.

The recovered initial Phase 27 run passed 53 tests. Final full-suite, Ruff, format,
shell syntax and diff-check results are recorded below after they complete. Migration
27 adds independent tables; v2 historical rows/policies stay intact. The gateway and
collector enqueue information only. No private keys, signing, live transport or LLM
exists in the engine. One authoritative writer persists atomic append-only evidence.

## Deployment sequence and activation evidence

1. Validate all phases/runtime plus full suite; freeze policy and commit/push exact revision.
2. Existing pinned Railway deployment helper takes a verified pre-deploy backup, then
   replaces only the authoritative production service with that committed revision.
3. Verify migrated DB, collector bid/ask cache, supervised idle worker and existing
   schedules. Register policy/create one fresh prospective run under runtime lock.
4. Record full run/policy/risk/thesis/exit IDs and activation; verify targeted receipts,
   worker timings, prefix integrity, restart state and actual resource/disk impact.
5. Leave v2 running for seven-day operational comparison unless measured cost makes
   a later reviewed freeze necessary. No test events/trades are injected into production.

Activation and final validation are appended after verified deployment.

## Exactly one next phase

Recommend **Phase 26B: a deterministic government-labelled BTC transfer source with
canonical transaction episodes and separately registered immediate/confirmed reaction
hypotheses**. It is not started here.
