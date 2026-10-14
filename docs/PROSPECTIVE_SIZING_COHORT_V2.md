# Prospective sizing cohort v2 — specification freeze

**Freeze the experiment before the market gets a vote.** This is a local, paper-only
specification, based on approved `main` at `6c2530a08d902c356cd6f76c74d5954c2e481763`.
No registration, activation, account creation, deployment, runtime/profile change, or
Railway/OVH action is authorized by this document. The zero-authority soak continues
independently. Dynamic Universe and candidate sizing remain disabled.

The normative machine specification is
[`prospective_sizing_cohort_v2.json`](../specs/sizing/prospective_sizing_cohort_v2.json).
Its external [SHA-256 record](../specs/sizing/prospective_sizing_cohort_v2.sha256) hashes
the **entire** canonical JSON, including parameter snapshots, source manifests, evidence
rules, analysis and activation contracts. This document explains those rules. A discrepancy
is a protocol incident, never permission to pick the more favorable interpretation.

## Scientific question

> Does deterministic risk-normalized sizing improve the account-level return/drawdown
> profile relative to Prism’s current fixed-allocation sizing when both consume the same
> prospective trade stream?

The intended experimental intervention is how much capital/risk is assigned to an
otherwise identical opportunity. Signal quality, exits, universe optimization, leverage
optimization, LLM confidence and hypothesis ranking are outside scope.

**Pre-activation successor:** v2 supersedes the preserved v1 draft to remove its cost-model
confound and replace the 72-hour commissioning floor with 24 hours plus all existing
flow/diversity/incident gates. Neither draft has been registered or started. Reserved
cohort names remain unchanged; after activation any semantic change requires successor
cohort IDs. The historical sizing policy catalogue and replay evidence remain untouched.

## Control cohort

`dynamic_lab_fixed_v1` starts independently with **$100 cash/equity/peak** and zero
positions/debt. Its frozen sizing policy is `fixed_allocation_baseline_v1`:

`sizingpolicy_f967b56580b521ac27b99a3d29990e1894d324212b01176eb38639760108040e`

Preserve the current Dynamic Nimble control admission, fill, monitoring, accounting and
halt behavior from the pinned revision, rather than substituting the historical replay
sizer for the runtime control:

- Floor `0.20 × marked equity / executable fill` at venue `szDecimals`; fixed **2×
  isolated** leverage; $10 minimum; no averaging/pyramiding or increased supported exposure.
- Three open/pending positions, one position per asset, at most two satellites, 60% gross
  cap. Preserve the admission reservation check and fill-time cap; skip rather than shrink.
- Existing invalidation/state predicates, target, expiry, max hold and **10% emergency
  guard**; existing isolated loss cap and explicit liquidation adjustment.
- Current catastrophe halt when marked equity is **≤25% of running peak**. Preserve its
  stopped-account exit behavior. No drawdown ladder is added for symmetry.
- Same current fee, slippage, settled funding/adverse reserve, cooldown, episode deduplication,
  support/conflict and hypothesis degradation behavior. No control cleanup or improvement.

The current risk definition/ID, Dynamic execution definition/ID and relevant source-file
hashes are embedded in the specification. Hypothesis degradation remains the current
account-local rule: at least 100 finalized trades for a hypothesis, last 50 mean
`trade_return ≤ -0.005`, at least 40 losses; no upstream hypothesis removal.

## Candidate cohort

`dynamic_lab_risk_sizing_v1` independently starts with **$100 cash/equity/peak** and zero
positions/debt. Preserve every risk/account parameter of the approved exploratory parent policy
`drawdown_throttled_risk_v1[r=0.5%,lev=2]`:

`sizingpolicy_4a388694567e95a884f5e42e4736240bce6690a3fce07caf10105f051c962983`

The complete parent sizing-v1 definition, including inactive/default fields, is embedded
and checked against [the original policy evidence](evidence/risk-position-sizing-v1/policies.json).
No risk/account parameter is retuned. The derived prospective policy changes only its name
and `cost_model.impact` to the common control law (`none`), consistently for sizing estimates
and simulated fills. Its effective ID is:

`sizingpolicy_6ab51a0af05c0d4a28fd5e04c8f301bf63b3dfc4f4163ad62409bd4efb402cd1`

This explicit derivative is not a claim that the historical candidate hash changed.
Parent and effective policy IDs/hashes are both required in activation evidence.

| Parameter | Frozen behavior |
|---|---|
| Base risk budget | 0.5% of current marked equity before throttles/caps |
| Sizing distance | `max(thesis invalidation distance from fill, 0.5 × causal volatility_move, 0.0025)` |
| Risk denominator | Sizing distance plus modeled exit slippage, both 4.5 bps fees, adverse predicted funding reserve per started hour |
| Cost throttle | Costs/distance >25% halves budget; >50% rejects; preserve approved computation order |
| Drawdown | ≥5%: ×0.75; ≥10%: ×0.5; ≥20%: ×0.25; ≥35%: halt new admissions |
| Leverage | Smallest feasible integer, maximum 2× and venue cap; never confidence-based |
| Per-position cap | Notional ≤1× equity; margin ≤34% equity; ≤10% displayed depth |
| Account caps | Gross ≤1.5× equity; total margin ≤90%; net directional ≤1.5× |
| Risk caps | Open intended risk ≤3× base risk budget; same-direction crypto-beta cluster ≤2×, satellites weighted 1.5 |
| Counts | Three positions, two satellites, one position per asset |
| Capacity | Reduce to fit approved caps; reject unavailable/zero capacity or rounded quantity below $10 |
| Liquidation safety | `1/L - 1/(2 × venue_max_leverage) ≥ max(2 × loss_fraction_including_costs, 0.125)` |
| Venue handling | Decimal floor, never round up; $10 minimum; causal size/leverage/depth evidence required |
| Impact | Common current paper `impact=none` cost law for both sizing estimates and fills; approved engine ordering unchanged |

The **budget** and intended **1R at the actual thesis invalidation** are distinct. A wider
volatility floor, rounding, cost throttling or a binding cap can make intended stop risk
smaller than 0.5%. Record both. Preserve the exact engine order, including the initial
cost-ratio gate and portfolio caps based on the unthrottled base budget. Do not improve
iteration convergence or reapply gates under this cohort ID.

Drawdown uses the monotonic marked-equity peak. The approved ladder is state-based:
recovery can restore the relevant multiplier up to 1.0. Every change logs prior equity
peak, current equity, drawdown percentage, old/new multiplier, trigger timestamp,
snapshot ID and cohort ID. A 35% halt is absorbing: stop new admissions/cancel unfilled
entry intents; preserve monitoring, exits, funding and accounting of filled positions.
No reset, capital injection or restart under the same ID.

## Shared signal contract and pairing

> Cohort assignment must not change whether the underlying trade idea exists.

Generate **one causal source opportunity before account-dependent admission**. Freeze
one thesis/reference quote and send that exact immutable record to both cohorts. Do not
run two account-conditioned signal generators. Each cohort decides admission and quantity
from its own account snapshot. Every source ID must receive exactly two decision records,
including rejected, unavailable, supported and halted decisions.

Use `sourceopportunity_ + SHA256(canonical JSON)` over hypothesis ID/version, signal
timestamp, asset, side and source episode ID. No run/cohort/account identifier enters this
identity. All timestamps use UTC ISO-8601 with six fractional second digits and a
`+00:00` suffix, normalized before hashing. Reject aliases and naive timestamps. Separately hash the complete source record. Re-observations deduplicate; a
conflicting payload under an existing ID is an incident. Finalized records are immutable;
new lifecycle/accounting evidence appends events rather than overwriting decisions.

| Source record | Cohort decision record |
|---|---|
| Source opportunity ID; hypothesis ID/version; episode; signal timestamp; asset; side | Source ID/hash; cohort ID; sizing policy ID/hash |
| Entry reference and quote ID/payload; causal market-data frontier; eligibility evaluation time | Content-addressed account snapshot; decision timestamp; admission/rejection and reason codes |
| Thesis evidence/hash; volatility_move and definition/version; full invalidation, target, expiry and max hold | Quantity; notional; risk budget; intended stop risk/basis; leverage; margin; cost estimate and sizing explanation |
| Policy-independent eligibility and reasons; unavailable-field reasons | Sizing input hash; trade/lifecycle IDs; fill quote/depth evidence; capacity before/after |
| Universe snapshot; frozen priority inputs; causal attribution | Subsequent append-only lifecycle/funding/accounting events |

Account snapshots include equity/cash/peak, marked open exposures, pending reservations,
gross/net/margin/open/cluster risk, cooldown/episode/degradation and halt state, and mark
quotes/frontier. Missing source fields are explicitly null with reasons, not invented.
Policy-independent ineligibility remains observable but does not inflate the eligible
paired-opportunity denominator.

The shared components are universe/ranking source, hypothesis definition/version, asset,
side, thesis, entry eligibility/timing, quote source, stop/invalidation, volatility_move,
target, expiry, max hold, exits, fee/slippage/impact model, funding treatment, data availability,
cooldown rule and lifecycle semantics. The current Dynamic manifest contains **28 Phase
22/23/24 hypotheses**. Event attribution is retained as an empty stratum; this does not
authorize adding Phase 30 hypotheses to the stream.

Use the same first observed fresh future bid/ask after intent, same market-data frontier
and current event ordering for both cohorts. Process source opportunities in current
Dynamic priority: economic ratio descending, score descending, then asset, hypothesis,
signal timestamp. Account state cannot reorder the source batch or select a better quote.
Rejection does not permit a later quote retry under the same source ID.

Existing cooldown, episode deduplication, support/conflict and degradation rules operate
**after fan-out**, locally. Their states may differ because admissions differ. Acquisition
must satisfy the union of lifecycle monitoring obligations; account-created risk pins
cannot grant upstream entry eligibility or change ranking/promotion.

## Permitted divergence

Quantity/notional, margin, available equity, gross/net exposure, concurrent capacity,
later capital/risk admissions, drawdown, throttle, cohort halt, funding dollars, quantity-dependent common cost dollars and isolated loss-cap accounting may diverge. So may account-local
cooldown, episode deduplication, support/conflict and degradation state under the same
rules. Size-cost-related fill failure is recorded explicitly.

Do not force matching trade counts once accounts diverge. A different source signal,
thesis, shared quote, input version, priority, exit geometry, fee/funding rule or unapproved
cost model is a protocol deviation. Log its frontier and incident; material deviations
invalidate confirmatory conclusions and kill new admissions.

## Start prerequisites

All prerequisites must have explicit evidence before any registration/activation:

1. OVH zero-authority soak PASS, or explicitly approved PASS WITH LIMITATION.
2. Rehearsal authority passes.
3. Dynamic Universe baseline path operational under separately approved authority.
4. Exact warmup/readiness gates satisfied naturally, without backdating or forced history.
5. Baseline-only commissioning passes with no unresolved material incident.
6. Current merged full suite passes on the eventual activation revision.
7. Real entry evidence records positive causal `volatility_move`,
   `entry_volatility_move_v1` definition/source/availability, and lifecycle propagation.
8. Offline read-only replay reconciles all finalized historical trades using existing
   control reconciliation tolerances; no unexplained discrepancy. Disclose legacy omissions.
9. Future dispatcher/adapter rehearsal proves complete pairing, exact control semantics,
   fail-closed inputs, throttle/halt and lifecycle behavior without execution authority.
10. Activation manifest sealed, paper-only credential/authority boundary verified.
11. **Explicit user approval** for both named cohorts under the frozen shared cost contract.

Neither code existence nor this freeze satisfies the start gate. No gate is asserted passed
here. The future profile, integration and source-account separation still require work.

## Baseline commissioning gate

Require **all**: at least **24 hours** since baseline-only activation, **30 eligible source
opportunities**, **10 accounting-final closed trades**, at least **two assets** and **two
hypotheses**, and **zero unresolved material execution/accounting incidents**. After a
material fix, additionally require 24 clean hours and five finalized trades.

The baseline must demonstrate signal flow, quotes/fills, execution lifecycle, reconciliation,
exits, DB/runtime operation, throughput, entry volatility evidence and restart recovery.
No unaccounted opportunities/backlog; quote freshness and lifecycle deadlines must comply
with current policy. A 24-hour floor exercises a complete daily cycle and hourly funding
boundaries. No independent safety test was identified that requires 72 hours; the separate
OVH soak/readiness gates remain in force. This choice is made before observing outcomes.
Count minima stop
low-flow periods from passing on the clock alone. The first Dynamic paper trade cannot
start the paired cohort. Commissioning trades are excluded from paired results; paired
accounts start fresh together later, each with $100.

## Sample-size and evidence states

All thresholds in a row must pass. Determine the highest qualifying state only at scheduled
checkpoints, with ≥95% source-health coverage and no unresolved material incident.

| State | Settled paired opportunities | Finalized both-taken pairs | Finalized trades per cohort | Market days | Hypotheses / families / causal regimes |
|---|---:|---:|---:|---:|---|
| TOO_EARLY | Default before later minima | — | — | — | — |
| DESCRIPTIVE_ONLY | 50 | 30 | 30 | 7 | 2 / 1 / 0 |
| PRELIMINARY | 200 | 100 | 100 | 30 | 4 / 2 / 0 |
| DECISION_ELIGIBLE | 500 | 200 | 200 | 60 | 6 / 3 / 2 |

A settled paired opportunity has two terminal decisions and accounting-final outcomes
for associated trades. Both skipped can count only in that column. Supported existing
exposure is not a new trade. Hypothesis diversity requires ≥5 both-taken pairs per ID;
family diversity ≥10 per family; regime diversity ≥20 per existing causally defined class.
Unavailable regime labels do not count, and post-hoc labels cannot rescue eligibility.
This may prevent DECISION_ELIGIBLE within a practical horizon.

Formal primary assessment occurs at the first scheduled checkpoint on/after **day 90**
that meets DECISION_ELIGIBLE. Its frontier is then immutable; later checkpoints are
secondary. Fixed administrative end is **day 180**, regardless of outcomes: stop new
admissions and let obligations drain under existing exits. Do not extend based on PnL.
Checkpoint equity still uses the fixed frontier, without waiting for favorable closes.
Safe halts/depletion may make count thresholds impossible; report descriptive survival
failure without lowering minima or resetting accounts.

These are governance minima, not a powered superiority test. Correlated opportunities,
adaptive admissions and one account path per policy can make robust inference impossible
even at these counts. Inadequate diversity or wide uncertainty yields INCONCLUSIVE.

## Primary endpoint

Freeze **ordered account-level return/drawdown dominance**, not final balance alone:

1. Integrity: material defects/missing marks make the comparison invalid for decision.
2. Survival/safety: candidate liquidation/safety breach or candidate-only halt prevents
   candidate preference. Both halt means no superiority; control-only halt is evidence,
   not an automatic candidate win.
3. Meet evidence minima at the predeclared assessment horizon.
4. Candidate supports preference only with **positive net return**, return ≥control,
   maximum drawdown ≤control, and ≥**1 percentage point** greater return or ≥**0.5
   percentage point** lower drawdown. Symmetric dominance supports control. Both losing
   means no positive edge; mixed return/drawdown is inconclusive; within thresholds is
   no material difference.
5. Report intended versus realized risk, excess losses, continuity, halts and capacity
   skips with the decision. Impossible risk or safety violations veto preference.

Use the same UTC frontier and observed two-second account mark grid. Return is marked
equity/start −1, net of fees, modeled execution costs, funding and reserves. Maximum
drawdown is the worst marked fall from running peak, including unrealized losses. Keep
halted accounts and their remaining cash in the path. Never interpolate stale marks to
suppress drawdown. This avoids unstable return/drawdown ratios near zero and an arbitrary
exchange rate between profit and additional risk.

Uncertainty sensitivity uses 10,000 paired joint seven-UTC-day moving-block resamples of
aligned intraday account mark-return increments, seed 20261014. Retain every observed
mark within each block; uniformly sample overlapping whole-day-start blocks with replacement
for both cohorts together, concatenate to the original market-time length and trim excess.
Reconstruct equity from $100 and recompute return and intraday maximum drawdown, reporting
95% percentile intervals. Daily-close drawdown cannot substitute for marked drawdown. Candidate preference remains descriptive unless the return
difference interval lower bound is ≥0 and drawdown difference upper bound ≤0. Report
block counts/dependence. This does **not** rerun adaptive sizing/admission and is not a
calibrated proof of policy superiority. Wide intervals remain inconclusive.

## Secondary metrics and paired analysis

Freeze terminal equity, total/geometric return, maximum drawdown/duration, expectancy in
R and realized R distribution, average intended risk, losses beyond intended risk,
gross exposure, average/maximum margin use, trade/paired-opportunity counts, skips and
admission divergence, cost/funding drag, halt events, liquidation near misses and
liquidations. Also retain budget-versus-stop risk and source-health/degradation events.

Geometric return requires positive equity/complete daily marks and is not annualized.
1R is frozen intended account loss at actual thesis invalidation including costs;
invalid/nonpositive denominators are unavailable. Report excess-loss counts/dollars,
quantiles and worst loss/R. Exposure/margin means are time-weighted. Drawdown duration
is wall time underwater, censored at checkpoint if unrecovered. Split spread, slippage,
fees and funding without double counting.

Near miss means observed adverse movement reaches ≥80% of frozen liquidation distance,
one count per trade. A threshold breach or nonzero isolated liquidation adjustment is
one modeled liquidation. Report data resolution/gaps and approximation limitations.

For both-taken source IDs compare size, intended risk, realized PnL/R, costs, capital use
and later capacity. Retain **control only, risk only, and both skipped**; never inner-join
away divergence. Report cancellations, support, unavailable and halt substatuses. Pending
trades are separately right-censored. Link snapshots and later capacity rejections; do
not invent outcomes for untaken trades. Funding true-ups append provenance; prior
checkpoint reports are retained.

Preserve attribution by family, asset, core/satellite, long/short, event/technical/
positioning/microstructure and existing causal regime/context. Subgroups remain descriptive
unless ≥100 both-taken pairs, ≥30 days and two causal regimes; exploratory multiple
comparisons cannot promote a subgroup. Confirmation requires a future preregistered study.

## Common prospective execution-cost contract

The historical policy definitions differ in exactly one cost-model parameter:
`impact=none` for the control versus `impact=sqrt_reference_notional` for the candidate.
The latter multiplies source slippage by `max(1, sqrt(notional / $100))` above $100.
At $400, for example, a 2 bps source becomes 4 bps only for the historical candidate.
Both already share the 4.5 bps per-side fee, $100 reference, 10% depth participation
setting, adverse predicted funding reserve, and venue floor semantics. The difference
was a research modeling addition, not a structural requirement of risk-normalized sizing.

For the prospective comparison, both arms call one frozen
[offline adapter](../src/market_signal/sizing/cohort_costs.py), at their independently
chosen final venue-rounded quantities. Common model ID:

`prospectivecostmodel_29df68d7eed12d84ad1b9f3a016d0e46a24f2f954bafcd23c34b2001a84aeac4`

Slippage model ID:

`prospectiveslippage_93f23619de057d5db33f26e9165b0d700e8558b843daf392f3ada58ddb25f1e1`

The common law preserves current control fills: BTC/ETH 2 bps, SOL 4, HYPE/LINK 6,
AAVE 8; satellites use the same causal `depth_impact_conservative_v1` classification
(`max(3, 20 × 100 / depth_usd, venue impact bps, 0.05 × rv_bps)`) at the frozen $100
reference. There is no additional quantity impact multiplier in either prospective arm.
Entry buys use ask plus slippage and sells use bid minus slippage; exit transactions
reverse sides. Fees are 4.5 bps on fill notional each side. Same hourly settled funding
and adverse per-started-hour reserve, same source snapshots, depth evidence, quote source
and venue decimals. No arm-specific cost parameter or rounding override is permitted.

At a shared quote, slippage **bps** is quantity-invariant; cost **dollars** vary with
quantity. Equal quantities must yield exactly equal modeled costs. Candidate sizing
estimates and fills both use this law, rather than retaining square-root estimates and
substituting a different fill cost. Final quantity/notional/margin reconciliation is tested.
Candidate depth and risk caps remain its admission constraints, not another fill model.
The control retains its existing admission/halt rules without a new depth cap.

This removes role-dependent execution-cost confounding. Inference concerns account-path
sizing effects under the current shared simulated execution law, not real market-impact
performance. The historical candidate policy/replay keeps its square-root assumption.
Future runtime wiring and paired lifecycle rehearsal remain explicit start prerequisites;
the adapter added here performs pure offline calculations and has no activation API.

## Slippage limitation and exit separation

> Current size-dependent slippage is a modeled assumption, not yet prospectively validated.

For source opportunities and trades retain contemporaneous bid/ask/time/session evidence,
depth by side/price band, impact classification, reference notional, model IDs, predicted
cost, actual quantity and simulated fill quote/price/cost. Use these to study size sensitivity
later. Simulated agreement does not validate real venue execution. Never recalibrate the
slippage model within these cohorts.

TP/SL geometry stays out of scope. The observation of approximately +0.8–0.9R TP exits
versus −1.08R stop-outs motivates a later study only. Targets, stops, expiry, max hold
and exit predicates cannot change here.

## Operational kill conditions and missing data

Accounting corruption, duplicate identities, broken pairing, execution defects, policy/hash
mismatch, impossible risk, liquidation-safety violation or persistent stale required data
kill new admissions for both arms. Current quote age >3s rejects entry; continuous required
feed failure ≥180s triggers operational kill. Preserve existing lifecycle safety obligations,
incident evidence and last sound/first bad frontiers. Do not erase affected segments.

Scientific disappointment alone does not trigger retuning or an ad-hoc kill. A badly
performing safe cohort continues to its scheduled checkpoint or frozen account halt.

Candidate sizing fails closed for missing/invalid invalidation, volatility value/definition,
equity/peak/open exposure, quotes/provenance, costs/funding, depth evidence, venue rounding/
leverage/margin constraints or hold constraints. Nonfinite, noncausal, stale, breached or
policy-mismatched inputs receive explicit codes listed in JSON. Never default unknown
funding to zero or fabricate volatility/depth. The control may proceed only when its
existing rules permit; retain the resulting pairing divergence.

The offline checker exercises required-field rejection. It is **not runtime enforcement**:
the existing historical sizer permits absent volatility. A later approved prospective
dispatcher must enforce the complete stricter contract before calling the offline adapter,
with rehearsal evidence. Its geometry/cost snapshot checks do not replace source freshness,
provenance, margin readiness or account lifecycle gates.

## Immutability, activation metadata and future compatibility

Risk fraction, leverage cap, drawdown ladder, volatility formula, exposure caps, costs,
stops/targets, hypothesis/universe definitions, signal timing and admission priority require
new spec ID/version/hash **and successor cohort IDs**. So do changes to starting equity,
cooldown/degradation, evidence thresholds, endpoint, cadence or missing-data rules.
Result-affecting bug fixes require an incident, explicit impact, versioned successor and
clear cutover frontier. A same-ID restart is forbidden. Hashing is tamper evidence;
reviewed commit provenance remains necessary.

Before registration, append and seal a separate content-addressed activation manifest:
cohort/schema IDs, sizing ID/hash, hypothesis/rule manifest, universe ID, cost/slippage IDs,
exact approved activation revision, runtime profile definition/hash, independent starting
equity, common start/data frontier, activation timestamp, approval evidence and parent
spec ID/hash. The timestamp/frontier and eventual profile are deliberately **pending facts**,
not mutable experiment parameters. No account starts with pending metadata.

Both start frontiers are identical and strictly exclude earlier source signals, including
queued commissioning ideas. The current `autotrader_lab` profile still gates Dynamic
Universe. A separately approved future infrastructure/profile successor must be pinned
before activation and preserve this spec’s scientific semantics. This task changes no
profile and supplies no activation implementation.

No transfers after start. $100/$200/$500/$1,000 sensitivity remains historical/synthetic
research only; no parallel prospective balances without separate approval. Paper only:
no real orders, exchange authority or fund-movement-capable keys. Live readiness is a
separate future decision requiring new evidence and approval.

Shared Intelligence improvements cannot change frozen input/availability semantics.
Manual Copilot cannot inject, remove or reprioritize cohort signals. No LLM thesis layer
feeds this v2; adding it changes inputs and requires successor cohorts. An LLM can never
set size, leverage, risk fraction or drawdown thresholds.

Daily operational checks and continuous integrity guards may show health, incidents and
raw account state. Formal evidence checkpoints are days **7, 14, 30, 60, 90, 120, 150,
180**. No winner leaderboard or superiority badges. Early checkpoints are descriptive;
no formal primary decision before day 90. No policy edits between or at checkpoints.

## Spec identity, validation and verdict

Specification ID: **`prospective_sizing_cohort_v2`**, schema version **2**.

Canonical SHA-256:

`8073b20d4cc61dd15b8e7ab55a12b3b343288a4a4172d1c81203753fbb40df17`

The v1 canonical JSON/hash remain preserved as superseded draft evidence. Canonical
hashes cover the full experiment, shared cost law, derived policy bindings, adapter source
hash, manifests and decision rules. Activation facts are sealed later in a separate manifest.

Offline [validator](../src/market_signal/sizing/cohort_spec.py),
[spec tests](../tests/test_prospective_sizing_cohort_spec.py) and
[cost parity tests](../tests/test_prospective_sizing_cohort_costs.py) check deterministic
hashing, parent/effective policy identity, historical artifact preservation, shared source
pairing, allowed divergence, successor requirements, fail-closed data and no I/O/activation.
The adapter is not imported by a runtime or CLI; all current sizing behavior is unchanged.

Focused validation: **141 passed**. Repository-wide Ruff lint, formatting (428 files) and
`git diff --check` pass. Full merged-tree validation: **1,715 collected and passed; zero failures/errors/skips;
zero uncompleted or excluded**, exit 0, in **2,367.77 seconds (39m 27s)**. The run used
the documented known-good local environment: outside sandbox, `.venv`, two workers with
`loadscope`, no wall-time cutoff. Two existing Starlette deprecation warnings were reported.
The preceding eight-worker attempt was interrupted after a timing-threshold failure and
a worker crash; both affected tests passed in the completed run. No assertions, timing
thresholds or exclusions were changed. Commands, counts, environment and preserved logs are in
[validation.json](evidence/prospective-sizing-cohort-v2/validation.json).

The current shared slippage model and liquidation approximation still need prospective
validation. Correlated single-account paths and missing causal regimes can limit inference.
Later dispatcher/profile integration, sealed activation metadata and explicit approval remain
required. The full suite must pass again on the eventual activation revision. This freeze
supplies no activation authority.

**SPEC FINALIZED AND READY FOR LATER ACTIVATION.** The two outstanding freeze checks are
resolved: full-suite completion and cost-model parity. Registration, start, account creation,
runtime/profile change, deployment and Railway/OVH actions remain unperformed.
