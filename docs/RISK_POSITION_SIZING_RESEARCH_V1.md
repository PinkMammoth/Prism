# Risk and position-sizing research v1

Offline research and disabled implementation only. Built on approved `main` (`b34bcea`).
Nothing was deployed. Railway, the OVH VPS zero-authority soak, Dynamic Universe and every
existing paper run were untouched. No authoritative paper run was created.

**Central principle.** Prism should risk according to the amount it can lose if the thesis is
wrong, not according to how excited it is about the trade.

**What this is.** A *counterfactual sizing replay*. It changes only position size, account
risk, leverage/margin use and the portfolio admission that size causes. Trade selection,
direction, entry time and price, hypothesis, exit logic, exit time and reason, exit price,
fees, funding and the observed market path all stay frozen. It is **not** a complete
strategy backtest. It does not optimise signals, thresholds, ranking, TP/SL or direction.

**Verdict: <<VERDICT>>.** See [Verdict](#verdict).

Code: `src/market_signal/sizing/` (policy, engine, model, replay, metrics,
bootstrap, stress, audit, ledger, scenario, study, prospective), `market sizing …` CLI
(`src/market_signal/cli/sizing_cmds.py`), `tests/test_risk_sizing.py`.
Evidence: [`evidence/risk-position-sizing-v1/`](evidence/risk-position-sizing-v1/).

<<TOC>>

## 1. Existing behaviour (from code)

The control is reconstructed exactly as `fixed_allocation_baseline_v1` and is not improved.

| Rule | Current paper behaviour | Source |
|---|---|---|
| Size | `units = floor(equity × 0.20 / entry_fill, szDecimals)`; notional = units × fill | `paper/v2/engine.py::size`, `RiskPolicy.notional_fraction` |
| Equity used | Current marked account equity (cash + floating) at the fill | `paper/nimble/engine.py::monitor` |
| Leverage | Fixed 2× isolated; margin = notional / 2 | `RiskPolicy.leverage`, `margin_mode` |
| Max positions | 3, counting pending intents | `RiskPolicy.max_positions`, `admit` |
| Gross cap | Admission: open/pending notional + 0.2 × equity ≤ 0.6 × equity; fill: open notional + fill notional ≤ 0.6 × equity, else `EXPOSURE_CAP` (skip, never shrink) | `admit`, `monitor` |
| Minimum | $10 notional, else `MIN_NOTIONAL` | `RiskPolicy.min_notional` |
| Rounding | Decimal floor to BTC 5, ETH 4, SOL 2, HYPE 2, LINK 1, AAVE 2 dp; satellites by venue `szDecimals` | `RiskPolicy.size_decimals`, `universe/execution.py::size` |
| Per asset | One position; same-side signal = `SUPPORTED` (no added exposure), opposite = conflict | `admit` |
| Satellites | ≤ 2 satellite positions (dynamic, not active); same 20% sizing | `admit`, `dynamic_spec` |
| Order of simultaneous entries | Six-asset runs: SHA-256 of (asset, hypothesis, signal time); dynamic: RV/cost ratio desc, score desc, asset, hypothesis, signal | `admit.priority` |
| Loss bounds | Thesis invalidation price; 10% emergency guard exit; net loss capped at margin + entry fee (isolated); run stops at equity ≤ 25% of peak | `exit_condition`, `_close`, `_mark` |
| Costs | 4.5 bps taker per side; slippage BTC/ETH 2, SOL 4, HYPE/LINK 6, AAVE 8 bps; satellite depth-impact classification; hourly settled funding plus adverse reserve | `spec.EXECUTION`, `ExecutionPolicy` |
| Economic gate | Target move ≥ 2 × (spread + fees + slippage + adverse funding reserve) | `model.economics` |

Consequences. Leverage is nearly irrelevant to the control: three 20% positions use at most
60% gross and 30% margin. Account risk is never chosen; it falls out of the stop distance.
Risk at invalidation = 0.20 × (stop distance + costs), so a 0.3% stop risks ~0.08% of equity
and a 5% stop ~1.0%. <<BASELINE_RISK_DISPERSION>>

## 2. Data available

Counts are from committed evidence (as of 2026-10-10 19:20 UTC). This environment has no
access to the production databases (by instruction) and no market-data egress (Hyperliquid,
Coinbase and Binance hosts are denied by the network policy).

| Source | Identity | Closed trades | Qualifies for |
|---|---|---|---|
| Original Nimble run | `paperrun_nimble_a8aef8be…` | 1 (equity 99.86) | Both analyses structurally (frozen thesis, executable quotes, MFE/MAE, fees, funding); statistically nothing |
| Phase 30 immediate cohort | `paperrun_nimble_a5fafb2c…` | 0 (1 cancelled) | Nothing yet |
| Phase 30 confirmed cohort | `paperrun_nimble_981fac86…` | 0 (1 cancelled) | Nothing yet |
| Retired Paper v2 | `paperrun_v2_3811cccc…` | 6 (+1 drained) | `EXPIRY_ONLY`: fallback-notional independent analysis only; excluded from risk-normalised and sequential work. Only row hashes are in the repository |
| Nimble/Phase 30 research curves | `paper_nimble_research` | n/a | Not trades: fixed-horizon descriptive returns with no executable invalidation path |
| Opportunity ledger | `paper_nimble_opportunities` | n/a | Shows capacity rejections, but rejected opportunities have no frozen dynamic-exit outcome |
| OVH zero-authority soak | lab rehearsal | 0 authoritative | Nothing (and untouched) |
| Dynamic Universe | not activated | 0 | Nothing |

Runs are never pooled: `market sizing replay --db` refuses several runs unless each is named.

**Implication.** There is no trustworthy prospective sample large enough to estimate a sizing
policy's performance. The real-ledger path is implemented and tested end-to-end: a trade closed
by the frozen Nimble engine is read back read-only and the control reproduces its units,
notional and net PnL exactly (`test_nimble_ledger_adapter_reconciles_real_engine_trade`). Run it
against an **offline backup copy**:

```
market sizing replay --db /path/to/offline-copy.duckdb --run <run_id> \
    --equity 100 --equity 200 --reconcile --out sizing-replay.json
```

The research below therefore uses **synthetic scenario streams** (§5). They answer how each
rule transforms the same trade stream under stated edge assumptions. They are not evidence
of Prism's edge, and no conclusion here says otherwise.

## 3. Invalidation audit

Every registered hypothesis (`market sizing audit`, [`audit.json`](evidence/risk-position-sizing-v1/audit.json)).
No stop was fabricated.

<<AUDIT>>

Findings.

- **Phase 22 technical (14, incl. 7 rejected short controls): `DERIVABLE_INVALIDATION`.**
  Hourly signals use a stop at the prior 15-minute extreme plus one spread. That stop is
  frequently far tighter than hourly noise. Pure invalidation sizing would lever these hardest.
  This is the main reason the recommended policy has a volatility floor.
- **Phase 23 positioning (6): `DERIVABLE_INVALIDATION`.** The price stop is a generic
  structural convention. The positioning thesis itself fails by state (`oi_expansion_lost`,
  `crowding_normalized`), which has no price.
- **Phase 24 microstructure (8) and Phase 30 events (6): `EXPLICIT_INVALIDATION`.**
  Event stops (pre-event level plus a spread) can be extremely tight.
- **Retired Paper v2: `EXPIRY_ONLY`.** **Unregistered event contracts (2): unsuitable.**
- **Volatility is not persisted** in the frozen thesis for structural, risk-multiple and
  event families. For those, the volatility floor cannot be reconstructed from the ledger.
  The replay then falls back to the 25 bps absolute floor and says so. A prospective sizing
  cohort must record `volatility_move` at entry (see §15).

## 4. The unit: R

**1R = the intended account loss if the causal thesis invalidation is reached**, including
modelled exit slippage, both taker fees and the adverse funding reserve, before any gap
beyond modelled execution. Every trade is reported in dollars, % of equity and R (+2R winner,
−1R intended loss, −1.3R slipped/gapped loss, +0.4R expiry). When a trade has no invalidation,
R is defined at the 10% emergency guard and flagged `EMERGENCY_GUARD`. R is defined the same
way for the control, so control and candidates are compared in the same units.

## 5. Methodology

### Pure sizer

`engine.size(SizingRequest, AccountSnapshot, SizingPolicy) -> SizingDecision` is deterministic
and has no I/O. `SizingRequest` contains only entry-time information. It has no confidence,
score, LLM or outcome field (tested). It never sees MFE/MAE, exit reason, realised PnL, later
volatility, later rank or future funding. Mutating every outcome field leaves every decision
in the catalogue unchanged (tested).

Order of operations:

1. Equity, quote and side validity; drawdown halt; count limits (one per asset, max
   positions, satellites, per setup).
2. Risk budget = equity × risk fraction × drawdown multiplier × regime multiplier
   (× 0.5 if costs exceed 25% of the loss distance; reject above 50%). Every multiplier is ≤ 1.
3. Loss distance from the executable fill: the invalidation, or the volatility floor, or the
   25 bps floor, whichever is largest (policy-dependent). Costs are added to the distance.
4. Notional = budget / (distance + costs). Square-root impact above a $100 reference is
   iterated twice, so impact only ever worsens the fill.
5. Hard caps: notional ≤ 1× equity; ≤ 10% of displayed depth (when known).
6. Leverage = the smallest integer that keeps margin ≤ 34% of equity, capped by policy and
   venue. Leverage never depends on signal strength.
7. Portfolio capacity: gross ≤ 1.5×, total margin ≤ 90%, open risk ≤ 3× base budget,
   same-direction crypto-beta cluster risk ≤ 2× base budget (satellites weighted 1.5), net
   directional ≤ 1.5×. Candidates shrink to fit; the control skips, as it does today.
8. Venue rounding is a Decimal floor and never rounds up. Below $10 the trade is rejected.
9. Liquidation safety: isolated liquidation distance ≈ 1/L − 1/(2 × venue max leverage). It
   must be ≥ max(2 × loss distance, 12.5%), else the trade is rejected.

Worked explanation (actual `size()` output, ETH long, $200, `drawdown_throttled_risk_v1[r=1%]`
at a 7% drawdown):

<<EXPLAIN>>

### Two analyses, never conflated

- **Trade-independent**: every trade is sized from the same fresh account. This shows the
  payoff transformation only.
- **Portfolio-sequential**: one account in chronological order. Exits at or before a decision
  release capital first. Ties use the frozen admission priority, never row or dict order
  (tested by permuting input). Sizing and drawdown use realised equity. The engine uses
  marked equity, a disclosed difference. Sequential replay can only skip or shrink source
  trades. It cannot add trades a policy would have had capacity for, because those have no
  frozen outcome.

### Synthetic scenario streams (not market data)

`scenario.py` generates 150 days (30 warm-up) of 15-second executable quotes for the six core
assets and four satellites. Each asset has its own volatility, spread, slippage and szDecimals
and the true venue max leverage. The quotes include:

- fat tails (Student-t, df = 4) and 0.6 cross-asset correlation;
- two hourly volatility regimes (calm, and stressed at 2.75× with doubled spreads);
- three market-wide crash windows (−4% to −8% in 30 minutes; satellites β = 1.5);
- hourly funding.

Candidate signals arrive independently of the future path for all 28 frozen hypotheses
(satellites only for portable phases). Each signal then goes through Prism's own code:

- `build_thesis` with the frozen 15-minute prior-structure geometry and Phase 22 Wilder ATR;
- `economics` (the 2× cost gate) at decision and again at the fill;
- the unbounded-invalidation check.

Exits use a vectorised replica of `exit_condition`, tested tick-by-tick against the real
function (`test_vectorized_exit_matches_frozen_exit_condition`). Fills happen at the next
tick after a decision (15 s; production polls every 2 s, so this is conservative).

Edge is *planted*, following Prism's engine-validation convention. After the entry fill, each
signal's price drifts toward its frozen target over the hypothesis' reaction window. The drift
size is a stated multiple of the target distance. Each signal's thesis is frozen from the path
known at its signal, before its drift exists, so geometry stays causal.

| Scenario | Planted edge (calm / stressed) | Meaning |
|---|---|---|
| NULL_EDGE | 0 / 0 | No edge; costs alone |
| NEGATIVE_EDGE | −0.5 / −0.5 | Signals wrong |
| WEAK_EDGE | 1.5 / 1.5 | Weak positive edge after costs |
| MODERATE_EDGE | 2.2 / 2.2 | Optimistic |
| REGIME_FLIP | 2.2 / −1.0 | Edge that fails in clustered, correlated stressed regimes |

These edge values were set so the control's expectancy lands in a named band. They are
scenario definitions and were never tuned on any sizing result. Not simulated: state-rule
invalidations, cost-decay funding exits, data-failure exits and ambiguous-wick ordering.

<<STREAMS>>

The frozen geometry has a striking property. Take-profit exits average **under +1R**, while
invalidations average about **−1.1R** (§9). Prism's targets are often closer than its stops
after costs. This is recorded for the later Exit Geometry / TP-SL phase and not acted on here.

### Bootstrap

This is a calendar-day stationary block bootstrap (Politis–Romano): whole UTC days are
resampled with geometric block length, mean 5 days. Whole days preserve intra-day concurrency
and capital contention exactly, and blocks preserve multi-day regime clustering. Each policy
gets 200 replications, seed 7, and every replication runs the full sequential replay. An
IID-day variant (block length 1) measures how much serial dependence matters. Ruin = path
equity ≤ 50% of start, the level at which a $100–200 learning account cannot continue
meaningfully.

### Selection rule (declared in code before the full run)

<<RULE>>

## 6. Policies tested

Every policy is a content-hashed `SizingPolicy` (`market sizing policies`,
[`policies.json`](evidence/risk-position-sizing-v1/policies.json)). Its identity includes risk
fraction, leverage cap/mode, invalidation treatment, volatility floor, cost model, drawdown
rules, every exposure cap and rounding. Any parameter change changes the id.

| Family | Definition | Status |
|---|---|---|
| `fixed_allocation_baseline_v1` | Exact control (§1) | CONTROL |
| `fixed_fractional_risk_v1[r]` | r ∈ {0.25, 0.5, 0.75, 1, 1.5, 2}%; distance = invalidation; no floor | CANDIDATE |
| `volatility_normalized_risk_v1[r]` | r ∈ {0.5, 1}%; distance = 1.0 × frozen causal volatility move (floor 25 bps) | CANDIDATE |
| `invalidation_vol_risk_v1[r]` | r ∈ {0.5, 0.75, 1, 1.5}%; distance = max(invalidation, 0.5 × volatility move, 25 bps); cost-aware; cluster cap. Ablations: costs off, cluster off | CANDIDATE |
| `drawdown_throttled_risk_v1[r]` | hybrid + ladder: ×0.75 at 5% DD, ×0.5 at 10%, ×0.25 at 20%, halt new entries at 35%; leverage caps 1/2/3/5× | CANDIDATE |
| `regime_throttled_risk_v1[r]` | throttled + ×0.5 when trailing 24 h core-basket RV ≥ 2 × its trailing 30-day level | CANDIDATE |
| `fractional_kelly_research_v1[k]` | k ∈ {1/8, 1/4, 1/2 (stress)}; per hypothesis or pooled; causal expanding-window pessimistic (one-sided 95%) mean R; ≥ 100 samples or fall back to 0.5%; ceiling 1.5% | RESEARCH_ONLY / STRESS_ONLY |
| `evidence_weighted_research_v1` | 0.75% × {0.5 if mean R ≤ 0, 1.25 if lower bound > 0} with ≥ 100 samples; ceiling 1% | RESEARCH_ONLY |
| `leverage_stress_v1[lev, safety]` | 1% risk, notional ≤ 3× equity, leverage 5×/10×, liquidation check on/off | STRESS_ONLY |

Frozen values and why (none chosen from replay PnL):

- Volatility k = 0.5: Nimble's own volatility target is 0.5 × volatility move, so a stop
  tighter than the trade's own target scale is inside ordinary noise.
- 25 bps absolute floor: below this, spread, slippage and fees alone exceed half the stop.
- Ladder 5/10/20%: roughly 5, 10 and 20 full losses at 1% risk.
- 35% halt: a $100 account keeps about $65 to keep learning with.
- Liquidation ≥ 2 × loss distance and ≥ 12.5% (1.25 × the 10% emergency guard).
- Impact: square root above the universe's $100 reference notional; ≤ 10% of displayed depth.
- Regime: 2× trailing RV, multiplier 0.5.

No martingale: every multiplier is ≤ 1 (the model forbids > 1) and risk only falls after
losses. Recovery restores risk as equity regains its peak (tested).

## 7. Account sizes

<<ACCOUNT>>

## 8. Trade-independent replay

<<INDEPENDENT>>

## 9. R distributions

<<RDIST>>

## 10. Portfolio-sequential replay and drawdowns

<<SEQUENTIAL>>

## 11. Ruin and tail risk (block bootstrap)

<<BOOT>>

## 12. Stresses and costs

<<STRESS>>

## 13. Leverage

<<LEVERAGE>>

## 14. Recommended policy and live-risk envelope (not activated)

<<RECOMMEND>>

## 15. Recommended prospective experiment (designed, not created)

`prospective.design()` ([`prospective-design.json`](evidence/risk-position-sizing-v1/prospective-design.json))
specifies `dynamic_lab_fixed_v1` (control policy) vs `dynamic_lab_risk_sizing_v1` (the
recommended candidate). Both consume one frozen signal/thesis stream, so sizing and
size-induced admission are the only differences. Each would be a new, separate paper run
identity, never the six-asset or Phase 30 cohorts, starting at $100.

Before a conclusion, require at least 200 closed trades per cohort and 60 calendar days. The
report pairs trades by signal id and covers R distributions, drawdowns and skipped trades.

Each entry must record what today's thesis does not: sizing policy id and explanation,
`volatility_move`, regime ratio, predicted funding, venue max leverage, szDecimals, depth, and
equity/peak/open exposures at sizing.

`ENABLED = False` and `REGISTERED = False` are module constants with no environment override.
`runtime_size()` raises until a reviewed activation phase changes them.

Dynamic Universe interaction. Satellites get no privilege from volatility. They carry their
own conservative slippage and depth cap and count 1.5× toward the correlated-cluster cap. The
two-satellite limit is kept. Higher volatility means a *smaller* position at the same risk.

LLM boundary. A future LLM may provide a structured thesis. It cannot choose dollar size,
leverage, account risk or liquidation tolerance: those fields do not exist on the request.

## 16. Implementation

| Added (disabled / offline) | Purpose |
|---|---|
| `sizing/policy.py` | Versioned content-hashed policies and the frozen catalogue |
| `sizing/engine.py` | Pure deterministic sizer with structured explanation |
| `sizing/model.py` | Causal `SizingRequest`, `TradeRecord` (outcomes are evaluation-only), account |
| `sizing/replay.py`, `metrics.py` | Independent and sequential replay; every metric in §19 of the brief |
| `sizing/bootstrap.py`, `stress.py` | Day-block bootstrap; deterministic stresses (sizer unaware of realised costs) |
| `sizing/audit.py` | Invalidation audit |
| `sizing/ledger.py` | Read-only Nimble ledger adapter; JSONL; control reconciliation |
| `sizing/scenario.py` | Synthetic streams on frozen Prism geometry |
| `sizing/study.py` | Evidence generation and mechanical selection |
| `sizing/prospective.py` | Disabled cohort design and runtime adapter (raises) |
| `cli/sizing_cmds.py` | `market sizing policies | audit | scenario | replay | study` |

`market research sizing-replay` was not possible: `market research` is already a leaf command.
The group is therefore `market sizing`. Counterfactual results go to files only. Each records
source run, source trade ids, policy identity, code revision, replay time, starting account
and assumptions. Nothing writes to any ledger. The paper, universe and ops packages do not
import `sizing` (AST-tested), and the Nimble, event and risk policy ids are unchanged
(tested against the committed Phase 30 evidence values).

Regenerate the evidence with `market sizing study --out-dir docs/evidence/risk-position-sizing-v1`
(deterministic seeds).

## 17. Tests

<<TESTS>>

## 18. Production state

Railway and the OVH VPS soak were not contacted, restarted, redeployed or reconfigured. No
deployment ran. Dynamic Universe stays inactive. No paper run was created or modified, and
no production database was opened (this environment cannot reach them). All work ran in an
isolated container against synthetic data and test fixtures.

## 19. Uncertainty

<<UNCERTAINTY>>

## Verdict

<<VERDICT_TEXT>>
