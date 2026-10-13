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

**Verdict: READY WITH SPECIFIC LIMITATIONS.** See [Verdict](#verdict).

Code: `src/market_signal/sizing/` (policy, engine, model, replay, metrics,
bootstrap, stress, audit, ledger, scenario, study, prospective), `market sizing …` CLI
(`src/market_signal/cli/sizing_cmds.py`), `tests/test_risk_sizing.py`.
Evidence: [`evidence/risk-position-sizing-v1/`](evidence/risk-position-sizing-v1/).



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
and a 5% stop ~1.0%. On the synthetic streams the control's risk at invalidation is 0.04% / 0.11% / 0.34% of equity at the 5th / 50th / 95th percentile (max 1.49%): an 8× spread between ordinary trades, set by stop width rather than by any decision about risk.

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

| Hypothesis | Phase | Side | Objective | State rule | Classification | Vol recoverable |
|---|---|---|---|---|---|---|
| `ema_cross_trend[n=20]:1h:long` | 22 | long | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `ema_stretch_fade[k=3.0]:1h:long` | 22 | long | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `ema_stretch_fade[k=4.0]:1h:long` | 22 | long | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `compression_breakout[n=24,pct=0.35]:1h:long` | 22 | long | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `expansion_continuation[k=3.0]:1h:long` | 22 | long | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `expansion_fade[k=3.0]:1h:long` | 22 | long | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `exhaustion[n=24,z=3.0,strong_close=no]:1h:long` | 22 | long | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `ema_cross_trend[n=20]:1h:short` | 22 | short | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `ema_stretch_fade[k=3.0]:1h:short` | 22 | short | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `ema_stretch_fade[k=4.0]:1h:short` | 22 | short | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `compression_breakout[n=24,pct=0.35]:1h:short` | 22 | short | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `expansion_continuation[k=3.0]:1h:short` | 22 | short | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `expansion_fade[k=3.0]:1h:short` | 22 | short | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `exhaustion[n=24,z=3.0,strong_close=no]:1h:short` | 22 | short | volatility | price_structure_only | DERIVABLE_INVALIDATION | yes |
| `crowding_vulnerability_long_v1` | 23 | long | risk_multiple | crowding_normalized | DERIVABLE_INVALIDATION | no |
| `crowding_vulnerability_short_v1` | 23 | short | risk_multiple | crowding_normalized | DERIVABLE_INVALIDATION | no |
| `oi_price_stall_long_v1` | 23 | long | risk_multiple | oi_expansion_lost | DERIVABLE_INVALIDATION | no |
| `oi_price_stall_short_v1` | 23 | short | risk_multiple | oi_expansion_lost | DERIVABLE_INVALIDATION | no |
| `oi_directional_pressure_long_v1` | 23 | long | risk_multiple | oi_expansion_lost | DERIVABLE_INVALIDATION | no |
| `oi_directional_pressure_short_v1` | 23 | short | risk_multiple | oi_expansion_lost | DERIVABLE_INVALIDATION | no |
| `continuation_core_long_v1` | 24 | long | volatility | flow_reversal_two_minutes | EXPLICIT_INVALIDATION | yes |
| `continuation_core_short_v1` | 24 | short | volatility | flow_reversal_two_minutes | EXPLICIT_INVALIDATION | yes |
| `absorption_book_long_v1` | 24 | long | structural | absorption_defence_lost | EXPLICIT_INVALIDATION | no |
| `absorption_book_short_v1` | 24 | short | structural | absorption_defence_lost | EXPLICIT_INVALIDATION | no |
| `absorption_oi_rising_long_v1` | 24 | long | structural | absorption_defence_lost | EXPLICIT_INVALIDATION | no |
| `absorption_oi_rising_short_v1` | 24 | short | structural | absorption_defence_lost | EXPLICIT_INVALIDATION | no |
| `continuation_persistent_long_v1` | 24 | long | volatility | flow_reversal_two_minutes | EXPLICIT_INVALIDATION | yes |
| `continuation_persistent_short_v1` | 24 | short | volatility | flow_reversal_two_minutes | EXPLICIT_INVALIDATION | yes |
| `venue_disruption_immediate_v1` | 30 | short | event_reaction | event_reaction | EXPLICIT_INVALIDATION | no |
| `venue_disruption_confirmed_v1` | 30 | short | event_reaction | event_reaction | EXPLICIT_INVALIDATION | no |
| `protocol_stress_immediate_v1` | 30 | short | event_reaction | event_reaction | EXPLICIT_INVALIDATION | no |
| `protocol_stress_confirmed_v1` | 30 | short | event_reaction | event_reaction | EXPLICIT_INVALIDATION | no |
| `sec_spot_etf_approval_surprise_immediate_v1` | 30 | long | event_reaction | event_reaction | EXPLICIT_INVALIDATION | no |
| `sec_spot_etf_approval_surprise_confirmed_v1` | 30 | long | event_reaction | event_reaction | EXPLICIT_INVALIDATION | no |
| `event_reaction_immediate_short_v1` | – | short | event_reaction | event_reaction | UNSUITABLE_FOR_RISK_NORMALIZED_SIZING | – |
| `event_reaction_confirmed_short_v1` | – | short | event_reaction | event_reaction | UNSUITABLE_FOR_RISK_NORMALIZED_SIZING | – |
| `paper_exploratory_v2_2 (retired fixed-horizon execution of the same 28)` | – | both | fixed_horizon | – | EXPIRY_ONLY | – |

Counts: 14 `EXPLICIT_INVALIDATION`, 20 `DERIVABLE_INVALIDATION`, 1 `EXPIRY_ONLY`, 2 `UNSUITABLE_FOR_RISK_NORMALIZED_SIZING`.

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

| Step | Value |
|---|---|
| equity / peak | $200.00 / $215.00 (drawdown 6.98%) |
| base risk | 1.0% → $2.00 |
| drawdown multiplier | 0.75 |
| invalidation distance (from fill) | 1.53% |
| volatility floor (0.5 × 4.2%) | 2.10% |
| cost / distance | 0.05 (no cost reduction) |
| effective risk budget | $1.50 |
| effective loss distance | 2.10% + 0.11% costs |
| raw notional | $67.81 |
| caps | none bound |
| leverage | 1× (margin $67.62 ≤ 34% of equity) |
| units (floor, 4 dp) | 0.0169 ETH → notional $67.62 |
| liquidation distance | 98% (safety needs ≥ 12.5%) |
| final intended risk at invalidation (1R) | $1.11 |

The floor sizes the trade off the wider volatility distance, so the actual loss at the thesis
stop ($1.11) is below the $1.50 budget. The stop can never make the position larger than the
noise floor allows.

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

| Scenario | Trades | Days | Exit mix (TP/INV/EXP) | Stop dist p5/p50/p95 | Nominal R:R p50 | Stop < ½ vol | Trades / hypothesis |
|---|---|---|---|---|---|---|---|
| MODERATE_EDGE | 1618 | 119.9 | 1094/497/27 | 0.04% / 0.32% / 1.37% | 2.28 | 79% | 56 |
| NEGATIVE_EDGE | 1541 | 119.9 | 294/1080/167 | 0.04% / 0.31% / 1.34% | 2.26 | 78% | 54 |
| NULL_EDGE | 1534 | 119.9 | 410/940/182 | 0.04% / 0.30% / 1.35% | 2.25 | 78% | 54 |
| REGIME_FLIP | 1617 | 119.9 | 1020/556/41 | 0.04% / 0.32% / 1.35% | 2.28 | 79% | 56 |
| WEAK_EDGE | 1576 | 119.9 | 918/601/55 | 0.04% / 0.32% / 1.35% | 2.26 | 78% | 56 |

The median stop sits 0.31% from the fill and the 5th percentile only 4 bps. In 78% of trades the stop is tighter than half the thesis' own volatility move. Each hypothesis has about 55 trades in 120 days, so per-hypothesis statistics would be weak even here.

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

Eligible: status == CANDIDATE (never CONTROL, RESEARCH_ONLY or STRESS_ONLY).

Constraints:

- every scenario, $100 and $200: block-bootstrap P(path loss >= 50%) <= the control's
- WEAK_EDGE, MODERATE_EDGE, REGIME_FLIP, $100 and $200: P(path loss >= 30%) <= 5%
- WEAK_EDGE, MODERATE_EDGE, REGIME_FLIP, $100 and $200: median max drawdown <= 20%
- WEAK_EDGE $200 stresses: terminal >= 80% of start under every stress
- zero liquidations in every replay

Ranking: WEAK_EDGE $200 bootstrap median terminal equity; then prefer the LOWEST risk policy whose median lies within the best survivor's interquartile range (plateau preference, not the single best parameter).

Outcome: the strict rule admitted **no** survivor. Every throttled candidate failed only the composite stress (fees ×1.5 + slippage ×2 + payoff −25%), which turns the WEAK_EDGE stream negative for every policy. The control fell to 111 from 200 under it. The study also reports a variant that drops only that composite stress. It is labelled **post-hoc** in `selection.json`. Under it, six throttled policies survive and the plateau rule picks `drawdown_throttled_risk_v1[r=0.5%,lev=2]`, which is the policy `prospective.candidate()` held as a placeholder before the full run. The unthrottled `fixed_fractional[0.5%]` and `invalidation_vol[0.5%]` fail either way: in NULL and NEGATIVE worlds their P(ruin) is 1.00, above the control.

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

Sequential, terminal equity as a multiple of start (max drawdown; trades lost to the $10 minimum):

| Scenario | Policy | $100 | $200 | $500 | $1,000 |
|---|---|---|---|---|---|
| WEAK | Control | 1.46× (2%, 0) | 1.47× (2%, 0) | 1.47× (2%, 0) | 1.47× (2%, 0) |
| WEAK | throttled 0.25% | 1.45× (2%, 26) | 1.48× (1%, 6) | 1.39× (2%, 3) | 1.21× (2%, 3) |
| WEAK | **throttled 0.5%, 2×** | **2.14× (3%, 4)** | **1.95× (3%, 3)** | 1.40× (5%, 3) | 1.06× (9%, 1) |
| WEAK | throttled 1% | 2.77× (6%, 45) | 1.93× (9%, 39) | 1.15× (12%, 27) | 0.92× (18%, 5) |
| WEAK | fixed fractional 1% | 2.56× (11%, 58) | 1.61× (15%, 55) | 0.82× (35%, 54) | 0.48× (60%, 53) |
| NULL | Control | 0.50× (50%, 289) | 0.43× (57%, 0) | 0.43× (57%, 0) | 0.42× (58%, 0) |
| NULL | throttled 0.25% | 0.80× (20%, 817) | 0.77× (23%, 563) | 0.72× (28%, 55) | 0.71× (29%, 3) |
| NULL | **throttled 0.5%, 2×** | **0.73× (27%, 800)** | **0.65× (35%, 151)** | 0.65× (35%, 1) | 0.65× (35%, 0) |
| NULL | throttled 1% | 0.65× (35%, 47) | 0.65× (35%, 1) | 0.65× (35%, 0) | 0.65× (35%, 0) |
| NULL | fixed fractional 1% | 0.10× (90%, 574) | 0.05× (95%, 323) | 0.03× (97%, 110) | 0.02× (98%, 68) |

Small accounts do not scale linearly:

- **$100.** The $10 venue minimum is the dominant constraint. The control stops trading
  exactly at $50, because 20% of $50 is $10, so "ruin" there is the minimum order.
  The throttled policies stop trading on the minimum before reaching the 35% halt.
- **$500 and $1,000.** The risk-sized policies' advantage disappears. Their notionals reach
  1× equity, and the size-dependent impact assumption (square root above $100) then
  dominates their costs. With the control paying the same impact
  ([`control-impact-parity.json`](evidence/risk-position-sizing-v1/control-impact-parity.json)),
  the WEAK_EDGE control still ends at 1.20× at $1,000. The 0.5% candidate ends at 1.06×.
- With impact switched off, the candidate reaches 2.16× at $1,000 (§12). The direction of the
  larger-account result therefore depends entirely on an impact model Prism has not
  measured. Nothing above $200 is concluded.

## 8. Trade-independent replay

WEAK_EDGE, $200, each trade sized from a fresh account:

| Policy | Sized/rej | Exp. R | Median R | Avg win R | Avg loss R | Mean %eq/trade | Worst %eq | Risk p5/p50/p95 | p95/p5 | MAX_NOTIONAL bound | ≤ −1.2R |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Control | 1576/0 | 0.118 | 0.45 | 0.92 | −1.02 | 0.026% | −0.71% | 0.04/0.11/0.34% | 8.3 | 0 | 127 |
| fixed fractional 0.5% | 1576/0 | 0.069 | 0.43 | 0.83 | −1.01 | 0.043% | −1.00% | 0.23/0.50/0.50% | 2.2 | 581 | 114 |
| fixed fractional 1% | 1576/0 | 0.039 | 0.38 | 0.78 | −1.01 | 0.058% | −1.49% | 0.23/0.63/1.00% | 4.4 | 1192 | 107 |
| fixed fractional 2% | 1576/0 | 0.031 | 0.36 | 0.77 | −1.01 | 0.065% | −2.94% | 0.23/0.63/1.85% | 8.2 | 1514 | 107 |
| volatility-normalized 0.5% | 1576/0 | 0.112 | 0.44 | 0.91 | −1.02 | 0.044% | −0.93% | 0.09/0.19/0.64% | 7.5 | 49 | 125 |
| invalidation+vol 0.5% | 1544/32 | 0.112 | 0.45 | 0.89 | −1.02 | 0.049% | −0.70% | 0.10/0.25/0.50% | 4.8 | 0 | 117 |
| invalidation+vol 1% | 1544/32 | 0.069 | 0.40 | 0.82 | −1.01 | 0.068% | −1.40% | 0.21/0.49/1.00% | 4.8 | 600 | 111 |
| invalidation+vol 1%, costs off | 1576/0 | 0.054 | 0.39 | 0.81 | −1.01 | 0.060% | −1.40% | 0.22/0.55/1.00% | 4.5 | 950 | 112 |

Reading:

- **Risk normalisation works as intended.** The hybrid caps every trade at its budget. The
  control's risk varies 8× between ordinary trades.
- **Pure invalidation sizing is distorted by the tight stops.** At 1% or more, the 1×-equity
  notional cap binds on 76–96% of trades, so realised risk plateaus around 0.6% whatever
  the nominal fraction. Above about 0.75%, the risk fraction is mostly a label.
- **Expectancy in R falls as size grows.** Larger notionals pay more modelled impact, so R
  is not size-invariant once impact applies.
- **The cost-aware rule rejects 32 trades** whose costs exceed 50% of the loss distance. It
  improves expectancy (0.069 → 0.054 when switched off) without ever upsizing.

## 9. R distributions

Control, $200, R by exit reason (identical geometry under every policy; values differ only through costs):

| Scenario | Take profit | Invalidated | Thesis expired | R p1 / p5 / p50 / p95 / p99 | Payoff | Profit factor |
|---|---|---|---|---|---|---|
| WEAK_EDGE | +0.93R (918) | −1.08R (601) | −0.23R (55) | −1.57 / −1.28 / +0.45 / +1.60 / +3.00 | 1.09 | 1.55 |
| NULL_EDGE | +0.81R (410) | −1.09R (940) | −0.31R (182) | −1.61 / −1.32 / −0.97 / +1.11 / +1.66 | 0.91 | 0.34 |

Findings for the later Exit Geometry phase. These are observations, not changes.

- Nominal reward-to-risk is 2.26 at the median before costs. After round-trip costs on a
  ~0.3% stop, take-profit exits average only +0.8 to +0.9R.
- Invalidations average −1.08R. The extra 0.08R beyond intended is the next-tick fill after
  the stop is crossed.
- 5–8% of trades lose more than 1.2R. All of these are tight stops where one tick of slippage
  is a large fraction of 1R.
- The frozen geometry needs a win rate above about 55% to break even. MFE, MAE, target
  distance, stop distance and nominal R:R are recorded for every trade in
  [`streams.json`](evidence/risk-position-sizing-v1/streams.json) and the replays.

## 10. Portfolio-sequential replay and drawdowns

**WEAK_EDGE, $200, portfolio-sequential**

| Policy | Taken/skipped | Terminal | Max DD | Exp. R | Win | Avg/max risk | Avg lev | Max gross | Worst trade | Liq / near |
|---|---|---|---|---|---|---|---|---|---|---|
| Control (20% notional, 2×) | 1506/70 | 293.7 | 1.7% | 0.12 | 59% | 0.14% / 1.49% | 2.00 | 0.60× | -0.72% | 0 / 0 |
| fixed_fractional[r=0.5%,lev=3] | 1485/91 | 311.6 | 10.6% | 0.04 | 58% | 0.43% / 0.50% | 2.53 | 1.50× | -0.98% | 0 / 0 |
| invalidation_vol[r=0.5%] | 1473/103 | 390.8 | 3.2% | 0.09 | 59% | 0.29% / 0.50% | 1.83 | 1.50× | -0.70% | 0 / 0 |
| drawdown_throttled[r=0.25%] | 1470/106 | 295.7 | 1.5% | 0.12 | 59% | 0.14% / 0.25% | 1.11 | 1.05× | -0.35% | 0 / 0 |
| drawdown_throttled[r=0.5%] | 1473/103 | 390.8 | 3.2% | 0.09 | 59% | 0.29% / 0.50% | 1.83 | 1.50× | -0.70% | 0 / 0 |
| drawdown_throttled[r=0.75%] | 1463/113 | 404.2 | 6.1% | 0.05 | 59% | 0.43% / 0.75% | 2.41 | 1.50× | -1.05% | 0 / 0 |
| drawdown_throttled[r=1%] | 1444/132 | 385.1 | 9.3% | 0.03 | 59% | 0.51% / 1.00% | 2.65 | 1.50× | -1.16% | 0 / 0 |
| drawdown_throttled[r=0.5%,lev=2] | 1473/103 | 389.1 | 3.2% | 0.09 | 59% | 0.29% / 0.50% | 1.74 | 1.50× | -0.70% | 0 / 0 |
| regime_throttled[r=0.5%] | 1474/102 | 352.3 | 3.2% | 0.10 | 59% | 0.27% / 0.50% | 1.77 | 1.50× | -0.70% | 0 / 0 |
| fractional_kelly[k=0.25,pooled] | 896/680 | 324.2 | 6.4% | 0.09 | 59% | 0.41% / 1.46% | 2.29 | 1.50× | -1.16% | 0 / 0 |

**NULL_EDGE, $200, portfolio-sequential**

| Policy | Taken/skipped | Terminal | Max DD | Exp. R | Win | Avg/max risk | Avg lev | Max gross | Worst trade | Liq / near |
|---|---|---|---|---|---|---|---|---|---|---|
| Control (20% notional, 2×) | 1463/71 | 85.8 | 57.1% | -0.49 | 27% | 0.14% / 1.22% | 2.00 | 0.60× | -0.77% | 0 / 0 |
| fixed_fractional[r=0.5%,lev=3] | 1307/227 | 15.6 | 92.2% | -0.51 | 26% | 0.40% / 0.50% | 2.66 | 1.50× | -1.05% | 0 / 0 |
| invalidation_vol[r=0.5%] | 1400/134 | 41.9 | 79.1% | -0.49 | 27% | 0.28% / 0.50% | 1.87 | 1.50× | -0.70% | 0 / 0 |
| drawdown_throttled[r=0.25%] | 903/631 | 154.6 | 22.7% | -0.47 | 29% | 0.08% / 0.25% | 1.02 | 0.81× | -0.26% | 0 / 0 |
| drawdown_throttled[r=0.5%] | 1073/461 | 129.8 | 35.1% | -0.48 | 28% | 0.11% / 0.50% | 1.12 | 1.49× | -0.52% | 0 / 0 |
| drawdown_throttled[r=0.75%] | 691/843 | 129.9 | 35.1% | -0.50 | 28% | 0.16% / 0.75% | 1.27 | 1.50× | -0.79% | 0 / 0 |
| drawdown_throttled[r=1%] | 510/1024 | 129.9 | 35.1% | -0.51 | 27% | 0.21% / 1.00% | 1.52 | 1.50× | -1.04% | 0 / 0 |
| drawdown_throttled[r=0.5%,lev=2] | 1073/461 | 130.0 | 35.0% | -0.48 | 28% | 0.11% / 0.50% | 1.12 | 1.49× | -0.52% | 0 / 0 |
| regime_throttled[r=0.5%] | 1070/464 | 129.9 | 35.1% | -0.49 | 28% | 0.11% / 0.50% | 1.12 | 1.49× | -0.52% | 0 / 0 |
| fractional_kelly[k=0.25,pooled] | 93/1441 | 184.4 | 7.8% | -0.41 | 33% | 0.26% / 0.50% | 1.89 | 1.49× | -0.52% | 0 / 0 |

**REGIME_FLIP, $200, portfolio-sequential**

| Policy | Taken/skipped | Terminal | Max DD | Exp. R | Win | Avg/max risk | Avg lev | Max gross | Worst trade | Liq / near |
|---|---|---|---|---|---|---|---|---|---|---|
| Control (20% notional, 2×) | 1549/68 | 293.5 | 8.0% | 0.21 | 64% | 0.14% / 1.12% | 2.00 | 0.60× | -0.88% | 0 / 0 |
| fixed_fractional[r=0.5%,lev=3] | 1527/90 | 396.6 | 16.6% | 0.09 | 63% | 0.44% / 0.50% | 2.50 | 1.50× | -1.20% | 0 / 0 |
| invalidation_vol[r=0.5%] | 1512/105 | 455.3 | 10.1% | 0.16 | 64% | 0.29% / 0.50% | 1.82 | 1.50× | -1.07% | 0 / 0 |
| drawdown_throttled[r=0.25%] | 1511/106 | 331.9 | 4.5% | 0.21 | 64% | 0.14% / 0.25% | 1.11 | 1.04× | -0.53% | 0 / 0 |
| drawdown_throttled[r=0.5%] | 1512/105 | 452.1 | 9.2% | 0.17 | 64% | 0.28% / 0.50% | 1.80 | 1.50× | -1.07% | 0 / 0 |
| drawdown_throttled[r=0.75%] | 1503/114 | 476.0 | 12.3% | 0.12 | 64% | 0.41% / 0.75% | 2.32 | 1.50× | -1.23% | 0 / 0 |
| drawdown_throttled[r=1%] | 1490/127 | 453.5 | 15.9% | 0.09 | 64% | 0.50% / 1.00% | 2.53 | 1.50× | -1.63% | 0 / 0 |
| drawdown_throttled[r=0.5%,lev=2] | 1512/105 | 449.1 | 9.2% | 0.17 | 64% | 0.28% / 0.50% | 1.71 | 1.50× | -1.06% | 0 / 0 |
| regime_throttled[r=0.5%] | 1513/104 | 469.8 | 5.9% | 0.17 | 64% | 0.27% / 0.50% | 1.76 | 1.50× | -0.82% | 0 / 0 |
| fractional_kelly[k=0.25,pooled] | 1463/154 | 391.0 | 20.6% | 0.08 | 64% | 0.55% / 1.50% | 2.63 | 1.50× | -2.44% | 0 / 0 |

**WEAK_EDGE, $100, portfolio-sequential**

| Policy | Taken/skipped | Terminal | Max DD | Exp. R | Win | Avg/max risk | Avg lev | Max gross | Worst trade | Liq / near |
|---|---|---|---|---|---|---|---|---|---|---|
| Control (20% notional, 2×) | 1506/70 | 146.5 | 1.7% | 0.12 | 59% | 0.14% / 1.49% | 2.00 | 0.60× | -0.71% | 0 / 0 |
| fixed_fractional[r=0.5%,lev=3] | 1480/96 | 209.5 | 7.5% | 0.09 | 58% | 0.42% / 0.50% | 2.56 | 1.50× | -1.01% | 0 / 0 |
| invalidation_vol[r=0.5%] | 1472/104 | 215.7 | 2.9% | 0.12 | 59% | 0.28% / 0.50% | 1.85 | 1.50× | -0.70% | 0 / 0 |
| drawdown_throttled[r=0.25%] | 1453/123 | 145.0 | 1.5% | 0.11 | 59% | 0.14% / 0.25% | 1.11 | 1.04× | -0.35% | 0 / 0 |
| drawdown_throttled[r=0.5%] | 1472/104 | 215.7 | 2.9% | 0.12 | 59% | 0.28% / 0.50% | 1.85 | 1.50× | -0.70% | 0 / 0 |
| drawdown_throttled[r=0.75%] | 1459/117 | 264.6 | 4.6% | 0.10 | 59% | 0.42% / 0.75% | 2.43 | 1.50× | -1.04% | 0 / 0 |
| drawdown_throttled[r=1%] | 1439/137 | 277.3 | 6.3% | 0.07 | 59% | 0.51% / 1.00% | 2.72 | 1.50× | -1.39% | 0 / 0 |
| drawdown_throttled[r=0.5%,lev=2] | 1472/104 | 213.9 | 2.9% | 0.12 | 59% | 0.28% / 0.50% | 1.74 | 1.50× | -0.69% | 0 / 0 |
| regime_throttled[r=0.5%] | 1463/113 | 190.0 | 2.8% | 0.12 | 59% | 0.26% / 0.50% | 1.79 | 1.50× | -0.70% | 0 / 0 |
| fractional_kelly[k=0.25,pooled] | 881/695 | 191.2 | 4.5% | 0.13 | 59% | 0.41% / 1.46% | 2.34 | 1.50× | -1.16% | 0 / 0 |

Reading:

- **Positive edge.** Risk sizing at 0.5% beats the control by 30–55% in terminal equity at
  $100–200, at 3–9% max drawdown against the control's 2–8%. Return rises with r up to about
  0.75% and then falls as caps and impact bind.
- **No or negative edge.** Without a brake, every risk policy is far worse than the control:
  −79% to −95% against −57% to −70%, because it risks 2–4× more per trade on average.
  With the drawdown ladder and 35% halt, the loss stops at 35%.
- **Correlated, clustered failure (REGIME_FLIP).** The regime throttle halves the drawdown
  of the plain 0.5% policy (5.9% vs 10.1%), but only because the stressed regime is exactly
  what this scenario punishes. That is true by construction, not evidence.
- **Capacity.** Skips are mostly `ASSET_ALREADY_OPEN` and `MAX_POSITIONS`. The correlated-
  cluster cap reduced about 15 trades per stream. Reductions to fit, rather than skips, are listed per
  policy under `bound_by` in the replays.
- **Liquidations.** None anywhere in the catalogue except the leverage stress (§13).

## 11. Ruin and tail risk (block bootstrap)

$200, block bootstrap (mean 5-day blocks, 200 replications):

| Scenario | Policy | Terminal p5/p50/p95 | MDD p50/p95 | P(loss≥10/20/30/50%) | P(halt) |
|---|---|---|---|---|---|
| NULL_EDGE | Control (20% notional, 2×) | 77 / 86 / 97 | 57.1% / 61.7% | 1.00 / 1.00 / 1.00 / 0.99 | 0.00 |
| NULL_EDGE | fixed_fractional[r=0.5%,lev=3] | 13 / 15 / 18 | 92.3% / 93.3% | 1.00 / 1.00 / 1.00 / 1.00 | 0.00 |
| NULL_EDGE | invalidation_vol[r=0.5%] | 36 / 42 / 49 | 79.2% / 81.9% | 1.00 / 1.00 / 1.00 / 1.00 | 0.00 |
| NULL_EDGE | drawdown_throttled[r=0.25%] | 153 / 155 / 156 | 22.8% / 23.6% | 1.00 / 1.00 / 0.00 / 0.00 | 0.00 |
| NULL_EDGE | drawdown_throttled[r=0.5%] | 130 / 130 / 131 | 35.0% / 35.1% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NULL_EDGE | drawdown_throttled[r=0.5%,lev=2] | 130 / 130 / 131 | 35.0% / 35.1% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NULL_EDGE | drawdown_throttled[r=0.75%] | 130 / 130 / 131 | 35.0% / 35.2% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NULL_EDGE | drawdown_throttled[r=1%] | 130 / 130 / 131 | 35.1% / 35.2% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NULL_EDGE | regime_throttled[r=0.5%] | 130 / 130 / 132 | 35.0% / 35.1% | 1.00 / 1.00 / 1.00 / 0.00 | 0.85 |
| NULL_EDGE | fractional_kelly[k=0.25,pooled] | 178 / 182 / 189 | 9.3% / 11.3% | 0.27 / 0.00 / 0.00 / 0.00 | 0.00 |
| NEGATIVE_EDGE | Control (20% notional, 2×) | 53 / 60 / 70 | 69.9% / 73.6% | 1.00 / 1.00 / 1.00 / 1.00 | 0.00 |
| NEGATIVE_EDGE | fixed_fractional[r=0.5%,lev=3] | 10 / 10 / 11 | 94.9% / 95.0% | 1.00 / 1.00 / 1.00 / 1.00 | 0.00 |
| NEGATIVE_EDGE | invalidation_vol[r=0.5%] | 22 / 25 / 28 | 87.8% / 88.9% | 1.00 / 1.00 / 1.00 / 1.00 | 0.00 |
| NEGATIVE_EDGE | drawdown_throttled[r=0.25%] | 149 / 150 / 152 | 24.8% / 25.7% | 1.00 / 1.00 / 0.00 / 0.00 | 0.00 |
| NEGATIVE_EDGE | drawdown_throttled[r=0.5%] | 130 / 130 / 131 | 35.0% / 35.1% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NEGATIVE_EDGE | drawdown_throttled[r=0.5%,lev=2] | 130 / 130 / 131 | 35.0% / 35.1% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NEGATIVE_EDGE | drawdown_throttled[r=0.75%] | 130 / 130 / 131 | 35.0% / 35.1% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NEGATIVE_EDGE | drawdown_throttled[r=1%] | 130 / 130 / 131 | 35.1% / 35.2% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NEGATIVE_EDGE | regime_throttled[r=0.5%] | 130 / 130 / 131 | 35.0% / 35.1% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NEGATIVE_EDGE | fractional_kelly[k=0.25,pooled] | 174 / 177 / 182 | 11.5% / 13.4% | 0.85 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | Control (20% notional, 2×) | 257 / 291 / 348 | 1.8% / 2.6% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | fixed_fractional[r=0.5%,lev=3] | 230 / 295 / 411 | 12.6% / 19.3% | 0.06 / 0.01 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | invalidation_vol[r=0.5%] | 316 / 379 / 475 | 3.7% / 5.3% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | drawdown_throttled[r=0.25%] | 263 / 293 / 337 | 1.6% / 2.2% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | drawdown_throttled[r=0.5%] | 315 / 379 / 475 | 3.7% / 5.3% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | drawdown_throttled[r=0.5%,lev=2] | 315 / 377 / 474 | 3.7% / 5.4% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | drawdown_throttled[r=0.75%] | 307 / 385 / 507 | 6.8% / 9.4% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | drawdown_throttled[r=1%] | 285 / 368 / 487 | 9.3% / 12.3% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | regime_throttled[r=0.5%] | 301 / 343 / 416 | 3.4% / 4.9% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | fractional_kelly[k=0.25,pooled] | 211 / 307 / 421 | 9.2% / 14.9% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | Control (20% notional, 2×) | 375 / 437 / 542 | 1.7% / 1.7% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | fixed_fractional[r=0.5%,lev=3] | 520 / 652 / 848 | 5.5% / 8.3% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | invalidation_vol[r=0.5%] | 591 / 702 / 861 | 2.6% / 2.9% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | drawdown_throttled[r=0.25%] | 387 / 433 / 499 | 1.3% / 1.3% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | drawdown_throttled[r=0.5%] | 591 / 702 / 861 | 2.6% / 2.9% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | drawdown_throttled[r=0.5%,lev=2] | 586 / 697 / 853 | 2.6% / 2.8% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | drawdown_throttled[r=0.75%] | 640 / 788 / 1004 | 4.4% / 5.8% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | drawdown_throttled[r=1%] | 630 / 779 / 1009 | 6.1% / 8.4% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | regime_throttled[r=0.5%] | 549 / 620 / 732 | 1.9% / 2.6% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | fractional_kelly[k=0.25,pooled] | 563 / 707 / 926 | 8.4% / 11.7% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | Control (20% notional, 2×) | 251 / 292 / 342 | 7.5% / 11.2% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | fixed_fractional[r=0.5%,lev=3] | 327 / 404 / 507 | 13.4% / 19.8% | 0.03 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | invalidation_vol[r=0.5%] | 384 / 460 / 556 | 8.9% / 12.3% | 0.01 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | drawdown_throttled[r=0.25%] | 298 / 332 / 372 | 4.5% / 5.9% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | drawdown_throttled[r=0.5%] | 381 / 458 / 554 | 8.2% / 10.6% | 0.01 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | drawdown_throttled[r=0.5%,lev=2] | 380 / 456 / 551 | 8.1% / 10.6% | 0.01 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | drawdown_throttled[r=0.75%] | 389 / 479 / 594 | 11.2% / 14.3% | 0.01 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | drawdown_throttled[r=1%] | 364 / 454 / 572 | 14.0% / 17.5% | 0.03 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | regime_throttled[r=0.5%] | 405 / 470 / 548 | 5.9% / 7.4% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | fractional_kelly[k=0.25,pooled] | 309 / 390 / 497 | 17.8% / 22.5% | 0.01 / 0.00 / 0.00 / 0.00 | 0.00 |

$100:

| Scenario | Policy | Terminal p5/p50/p95 | MDD p50/p95 | P(loss≥10/20/30/50%) | P(halt) |
|---|---|---|---|---|---|
| NULL_EDGE | Control (20% notional, 2×) | 50 / 50 / 51 | 50.1% / 50.2% | 1.00 / 1.00 / 1.00 / 0.61 | 0.00 |
| NULL_EDGE | fixed_fractional[r=0.5%,lev=3] | 10 / 11 / 12 | 89.0% / 89.8% | 1.00 / 1.00 / 1.00 / 1.00 | 0.00 |
| NULL_EDGE | invalidation_vol[r=0.5%] | 23 / 25 / 28 | 74.8% / 77.3% | 1.00 / 1.00 / 1.00 / 1.00 | 0.00 |
| NULL_EDGE | drawdown_throttled[r=0.25%] | 80 / 80 / 81 | 20.0% / 20.1% | 1.00 / 0.50 / 0.00 / 0.00 | 0.00 |
| NULL_EDGE | drawdown_throttled[r=0.5%] | 72 / 73 / 74 | 27.1% / 28.5% | 1.00 / 1.00 / 0.00 / 0.00 | 0.00 |
| NULL_EDGE | drawdown_throttled[r=0.5%,lev=2] | 72 / 73 / 74 | 27.1% / 28.5% | 1.00 / 1.00 / 0.00 / 0.00 | 0.00 |
| NULL_EDGE | drawdown_throttled[r=0.75%] | 65 / 65 / 66 | 35.0% / 35.1% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NULL_EDGE | drawdown_throttled[r=1%] | 65 / 65 / 66 | 35.0% / 35.2% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NULL_EDGE | regime_throttled[r=0.5%] | 72 / 73 / 75 | 26.7% / 28.2% | 1.00 / 1.00 / 0.00 / 0.00 | 0.00 |
| NULL_EDGE | fractional_kelly[k=0.25,pooled] | 89 / 91 / 94 | 9.1% / 11.1% | 0.26 / 0.00 / 0.00 / 0.00 | 0.00 |
| NEGATIVE_EDGE | Control (20% notional, 2×) | 50 / 50 / 50 | 50.1% / 50.2% | 1.00 / 1.00 / 1.00 / 0.96 | 0.00 |
| NEGATIVE_EDGE | fixed_fractional[r=0.5%,lev=3] | 10 / 10 / 10 | 90.0% / 90.1% | 1.00 / 1.00 / 1.00 / 1.00 | 0.00 |
| NEGATIVE_EDGE | invalidation_vol[r=0.5%] | 18 / 19 / 20 | 81.5% / 82.4% | 1.00 / 1.00 / 1.00 / 1.00 | 0.00 |
| NEGATIVE_EDGE | drawdown_throttled[r=0.25%] | 80 / 80 / 80 | 20.0% / 20.1% | 1.00 / 0.69 / 0.00 / 0.00 | 0.00 |
| NEGATIVE_EDGE | drawdown_throttled[r=0.5%] | 69 / 70 / 71 | 29.9% / 31.0% | 1.00 / 1.00 / 0.39 / 0.00 | 0.00 |
| NEGATIVE_EDGE | drawdown_throttled[r=0.5%,lev=2] | 69 / 70 / 71 | 29.9% / 31.0% | 1.00 / 1.00 / 0.38 / 0.00 | 0.00 |
| NEGATIVE_EDGE | drawdown_throttled[r=0.75%] | 65 / 65 / 65 | 35.0% / 35.1% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NEGATIVE_EDGE | drawdown_throttled[r=1%] | 65 / 65 / 66 | 35.0% / 35.2% | 1.00 / 1.00 / 1.00 / 0.00 | 1.00 |
| NEGATIVE_EDGE | regime_throttled[r=0.5%] | 69 / 70 / 72 | 29.6% / 30.8% | 1.00 / 1.00 / 0.26 / 0.00 | 0.00 |
| NEGATIVE_EDGE | fractional_kelly[k=0.25,pooled] | 87 / 89 / 91 | 11.3% / 12.9% | 0.83 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | Control (20% notional, 2×) | 128 / 145 / 174 | 1.8% / 2.6% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | fixed_fractional[r=0.5%,lev=3] | 149 / 202 / 295 | 8.8% / 13.4% | 0.01 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | invalidation_vol[r=0.5%] | 171 / 211 / 274 | 3.2% / 4.5% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | drawdown_throttled[r=0.25%] | 130 / 144 / 164 | 1.6% / 2.2% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | drawdown_throttled[r=0.5%] | 171 / 211 / 274 | 3.2% / 4.5% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | drawdown_throttled[r=0.5%,lev=2] | 170 / 210 / 272 | 3.1% / 4.4% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | drawdown_throttled[r=0.75%] | 192 / 253 / 342 | 5.5% / 7.5% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | drawdown_throttled[r=1%] | 196 / 260 / 362 | 7.1% / 10.2% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | regime_throttled[r=0.5%] | 161 / 187 / 231 | 3.0% / 4.0% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| WEAK_EDGE | fractional_kelly[k=0.25,pooled] | 109 / 206 / 325 | 7.1% / 12.0% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | Control (20% notional, 2×) | 187 / 218 / 269 | 1.7% / 1.7% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | fixed_fractional[r=0.5%,lev=3] | 376 / 485 / 654 | 4.6% / 6.3% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | invalidation_vol[r=0.5%] | 352 / 429 / 541 | 2.5% / 2.6% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | drawdown_throttled[r=0.25%] | 192 / 215 / 247 | 1.2% / 1.3% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | drawdown_throttled[r=0.5%] | 352 / 429 / 541 | 2.5% / 2.6% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | drawdown_throttled[r=0.5%,lev=2] | 349 / 425 / 536 | 2.5% / 2.6% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | drawdown_throttled[r=0.75%] | 454 / 577 / 751 | 4.2% / 4.6% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | drawdown_throttled[r=1%] | 488 / 617 / 826 | 5.3% / 6.8% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | regime_throttled[r=0.5%] | 319 / 369 / 437 | 1.8% / 2.4% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| MODERATE_EDGE | fractional_kelly[k=0.25,pooled] | 463 / 585 / 788 | 6.7% / 9.3% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | Control (20% notional, 2×) | 125 / 146 / 170 | 7.4% / 11.3% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | fixed_fractional[r=0.5%,lev=3] | 229 / 289 / 374 | 12.7% / 17.4% | 0.03 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | invalidation_vol[r=0.5%] | 217 / 266 / 328 | 8.8% / 12.0% | 0.01 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | drawdown_throttled[r=0.25%] | 149 / 165 / 185 | 4.2% / 5.5% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | drawdown_throttled[r=0.5%] | 215 / 265 / 328 | 8.1% / 10.5% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | drawdown_throttled[r=0.5%,lev=2] | 212 / 262 / 326 | 8.1% / 10.5% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | drawdown_throttled[r=0.75%] | 255 / 326 / 423 | 11.0% / 13.5% | 0.01 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | drawdown_throttled[r=1%] | 261 / 337 / 437 | 13.4% / 17.0% | 0.03 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | regime_throttled[r=0.5%] | 229 / 273 / 323 | 5.6% / 7.0% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |
| REGIME_FLIP | fractional_kelly[k=0.25,pooled] | 210 / 297 / 402 | 17.4% / 21.7% | 0.00 / 0.00 / 0.00 / 0.00 | 0.00 |

Serial dependence (block vs IID-day, $200):

| Scenario | Policy | Block 5d: terminal p50 · MDD p95 | IID-day: terminal p50 · MDD p95 |
|---|---|---|---|
| NULL_EDGE | fixed_allocation_baseline_v1 | 86 · 61.7% | 85 · 62.0% |
| NULL_EDGE | drawdown_throttled_risk_v1[r=0.5%,lev=2] | 130 · 35.1% | 130 · 35.1% |
| NULL_EDGE | drawdown_throttled_risk_v1[r=1%] | 130 · 35.2% | 130 · 35.2% |
| NEGATIVE_EDGE | fixed_allocation_baseline_v1 | 60 · 73.6% | 60 · 73.5% |
| NEGATIVE_EDGE | drawdown_throttled_risk_v1[r=0.5%,lev=2] | 130 · 35.1% | 130 · 35.1% |
| NEGATIVE_EDGE | drawdown_throttled_risk_v1[r=1%] | 130 · 35.2% | 130 · 35.2% |
| WEAK_EDGE | fixed_allocation_baseline_v1 | 291 · 2.6% | 293 · 3.0% |
| WEAK_EDGE | drawdown_throttled_risk_v1[r=0.5%,lev=2] | 377 · 5.4% | 382 · 6.4% |
| WEAK_EDGE | drawdown_throttled_risk_v1[r=1%] | 368 · 12.3% | 365 · 12.2% |
| MODERATE_EDGE | fixed_allocation_baseline_v1 | 437 · 1.7% | 441 · 1.7% |
| MODERATE_EDGE | drawdown_throttled_risk_v1[r=0.5%,lev=2] | 697 · 2.8% | 704 · 3.3% |
| MODERATE_EDGE | drawdown_throttled_risk_v1[r=1%] | 779 · 8.4% | 786 · 8.8% |
| REGIME_FLIP | fixed_allocation_baseline_v1 | 292 · 11.2% | 294 · 11.4% |
| REGIME_FLIP | drawdown_throttled_risk_v1[r=0.5%,lev=2] | 456 · 10.6% | 454 · 10.7% |
| REGIME_FLIP | drawdown_throttled_risk_v1[r=1%] | 454 · 17.5% | 452 · 18.1% |

The bootstrap distributions are narrow because each stream has about 1,500 trades. They describe ordering and clustering uncertainty *within* a world, not uncertainty about which world Prism is in. That second uncertainty is the dominant one: compare the NULL and WEAK rows. Block and IID-day results are nearly identical, so these synthetic regimes do not create much ordering risk at a 5-day scale.

Kelly, research only:

| Policy, $200 | WEAK | NULL | MODERATE | REGIME_FLIP |
|---|---|---|---|---|
| fixed 0.5% throttled (reference) | 391 (3.2%) | 130 (35%) | 728 (2.6%) | 452 (9.2%) |
| Kelly ⅛, per hypothesis | 377 (3.2%) | 130 (35%) | 740 (2.6%) | 463 (9.7%) |
| Kelly ¼, pooled | 324 (6.4%) | 184 (7.8%) | 790 (7.9%) | 391 (20.6%) |
| Kelly ½, pooled (stress) | 307 (10.6%) | 184 (7.8%) | 791 (7.9%) | 392 (20.6%) |
| evidence-weighted 0.75% | 403 (6.1%) | 130 (35%) | 856 (4.2%) | 477 (12.3%) |

- **Per-hypothesis Kelly almost never has enough evidence.** With about 55 trades per
  hypothesis it stays on its 0.5% fallback, so it equals the reference policy.
- **Pooled Kelly does the one thing a fixed fraction cannot.** With no edge, it stops trading
  once 100 samples show a non-positive lower bound (−8% instead of −35%).
- **Pooled Kelly also does the dangerous thing.** In REGIME_FLIP it raised risk on stale good
  evidence just before the edge reversed: 20.6% drawdown, against 9.2%. Its fraction
  multiplier barely matters, because the 1.5% ceiling binds.
- **Kelly is not recommended.** It stays `RESEARCH_ONLY`. Its useful property, refusing to
  trade on non-positive evidence, is better added later as an explicit edge-gate experiment.

## 12. Stresses and costs

WEAK_EDGE, $200, terminal equity (max drawdown). Sizing assumptions are unchanged; only the realised world worsens:

| Stress | Control (20% notional, 2×) | fixed_fractional[r=0.5%,lev=3] | invalidation_vol[r=0.5%] | drawdown_throttled[r=0.25%] | drawdown_throttled[r=0.5%] | drawdown_throttled[r=0.5%,lev=2] | drawdown_throttled[r=0.75%] | drawdown_throttled[r=1%] | regime_throttled[r=0.5%] | fractional_kelly[k=0.25,pooled] |
|---|---|---|---|---|---|---|---|---|---|---|
| base | 294 (2%) | 312 (11%) | 391 (3%) | 296 (1%) | 391 (3%) | 389 (3%) | 404 (6%) | 385 (9%) | 352 (3%) | 324 (6%) |
| fees_x1.25 | 275 (2%) | 263 (14%) | 344 (4%) | 274 (2%) | 344 (4%) | 343 (4%) | 344 (7%) | 329 (10%) | 311 (4%) | 294 (8%) |
| fees_x1.5 | 257 (3%) | 220 (20%) | 301 (5%) | 254 (2%) | 301 (5%) | 301 (5%) | 292 (8%) | 281 (11%) | 273 (5%) | 263 (9%) |
| slippage_x1.5 | 235 (4%) | 175 (29%) | 268 (6%) | 241 (2%) | 265 (6%) | 266 (6%) | 251 (9%) | 224 (13%) | 243 (6%) | 234 (11%) |
| slippage_x2 | 188 (11%) | 100 (55%) | 186 (15%) | 195 (6%) | 186 (13%) | 186 (13%) | 178 (17%) | 163 (22%) | 179 (15%) | 188 (17%) |
| win_rate_minus_10pct_of_winners | 230 (4%) | 196 (25%) | 259 (7%) | 232 (3%) | 257 (7%) | 257 (6%) | 248 (10%) | 234 (14%) | 239 (6%) | 202 (2%) |
| payoff_degraded_25pct | 198 (7%) | 127 (42%) | 195 (11%) | 200 (5%) | 194 (9%) | 194 (9%) | 183 (14%) | 179 (17%) | 188 (11%) | 202 (2%) |
| losing_streak_10 | 289 (2%) | 301 (13%) | 378 (4%) | 290 (2%) | 378 (4%) | 377 (4%) | 387 (7%) | 373 (9%) | 341 (4%) | 297 (5%) |
| clustered_correlated_losses_2d | 252 (11%) | 245 (27%) | 318 (18%) | 263 (8%) | 320 (13%) | 317 (13%) | 325 (18%) | 311 (21%) | 297 (12%) | 240 (7%) |
| missed_fills_10pct_winners | 262 (2%) | 250 (13%) | 328 (4%) | 267 (2%) | 328 (4%) | 327 (4%) | 324 (7%) | 312 (10%) | 300 (4%) | 219 (2%) |
| adverse_funding_0.005pct_h | 292 (2%) | 308 (11%) | 387 (3%) | 294 (1%) | 387 (3%) | 385 (3%) | 398 (6%) | 381 (9%) | 349 (3%) | 322 (7%) |
| tail_gap_to_emergency_x1 | 288 (2%) | 295 (12%) | 371 (7%) | 286 (3%) | 369 (7%) | 367 (7%) | 379 (10%) | 372 (10%) | 332 (7%) | 197 (7%) |
| tail_gap_to_emergency_x2 | 282 (3%) | 271 (18%) | 371 (7%) | 286 (3%) | 369 (7%) | 367 (7%) | 379 (10%) | 372 (10%) | 332 (7%) | 197 (7%) |
| combined_fees1.5_slip2_payoff | 111 (44%) | 22 (89%) | 65 (67%) | 158 (21%) | 134 (33%) | 134 (33%) | 130 (35%) | 130 (35%) | 135 (32%) | 187 (6%) |

NULL_EDGE, $200:

| Stress | Control (20% notional, 2×) | fixed_fractional[r=0.5%,lev=3] | invalidation_vol[r=0.5%] | drawdown_throttled[r=0.25%] | drawdown_throttled[r=0.5%] | drawdown_throttled[r=0.5%,lev=2] | drawdown_throttled[r=0.75%] | drawdown_throttled[r=1%] | regime_throttled[r=0.5%] | fractional_kelly[k=0.25,pooled] |
|---|---|---|---|---|---|---|---|---|---|---|
| base | 86 (57%) | 16 (92%) | 42 (79%) | 155 (23%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 184 (8%) |
| fees_x1.25 | 81 (60%) | 14 (93%) | 37 (82%) | 154 (23%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 183 (9%) |
| fees_x1.5 | 76 (62%) | 12 (94%) | 33 (84%) | 152 (24%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 182 (9%) |
| slippage_x1.5 | 70 (65%) | 12 (94%) | 31 (85%) | 152 (24%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 181 (9%) |
| slippage_x2 | 57 (72%) | 10 (95%) | 24 (88%) | 150 (25%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 178 (11%) |
| win_rate_minus_10pct_of_winners | 78 (61%) | 14 (93%) | 35 (82%) | 153 (24%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 182 (9%) |
| payoff_degraded_25pct | 73 (63%) | 12 (94%) | 32 (84%) | 152 (24%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 181 (9%) |
| losing_streak_10 | 86 (57%) | 16 (92%) | 42 (79%) | 154 (23%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 184 (8%) |
| clustered_correlated_losses_2d | 81 (60%) | 15 (93%) | 38 (81%) | 154 (23%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 184 (8%) |
| missed_fills_10pct_winners | 82 (59%) | 15 (93%) | 39 (81%) | 154 (23%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 183 (9%) |
| adverse_funding_0.005pct_h | 85 (57%) | 15 (92%) | 41 (79%) | 155 (23%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 184 (8%) |
| tail_gap_to_emergency_x1 | 84 (58%) | 15 (93%) | 39 (80%) | 154 (23%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 175 (13%) |
| tail_gap_to_emergency_x2 | 83 (59%) | 14 (93%) | 39 (80%) | 154 (23%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 175 (13%) |
| combined_fees1.5_slip2_payoff | 50 (75%) | 10 (95%) | 20 (90%) | 146 (27%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 130 (35%) | 174 (13%) |

Cost-model sensitivity, WEAK_EDGE (control column shows its own constant-slippage model, see §7 for impact parity):

| Policy | Equity | sqrt impact ref $100 | no impact | sqrt impact ref $50 |
|---|---|---|---|---|
| Control (20% notional, 2×) | $100 | 146 (2%) | 146 (2%) | 146 (2%) |
| Control (20% notional, 2×) | $200 | 294 (2%) | 294 (2%) | 292 (2%) |
| Control (20% notional, 2×) | $500 | 736 (2%) | 736 (2%) | 600 (4%) |
| Control (20% notional, 2×) | $1000 | 1475 (2%) | 1475 (2%) | 964 (10%) |
| fixed_fractional[r=0.5%,lev=3] | $100 | 210 (8%) | 235 (6%) | 155 (11%) |
| fixed_fractional[r=0.5%,lev=3] | $200 | 312 (11%) | 471 (6%) | 214 (20%) |
| fixed_fractional[r=0.5%,lev=3] | $500 | 472 (24%) | 1178 (6%) | 313 (45%) |
| fixed_fractional[r=0.5%,lev=3] | $1000 | 627 (45%) | 2357 (6%) | 412 (62%) |
| invalidation_vol[r=0.5%] | $100 | 216 (3%) | 217 (3%) | 195 (3%) |
| invalidation_vol[r=0.5%] | $200 | 391 (3%) | 436 (3%) | 309 (5%) |
| invalidation_vol[r=0.5%] | $500 | 704 (6%) | 1090 (3%) | 512 (12%) |
| invalidation_vol[r=0.5%] | $1000 | 1025 (12%) | 2182 (3%) | 723 (32%) |
| drawdown_throttled[r=0.25%] | $100 | 145 (2%) | 145 (2%) | 145 (2%) |
| drawdown_throttled[r=0.25%] | $200 | 296 (1%) | 296 (1%) | 286 (2%) |
| drawdown_throttled[r=0.25%] | $500 | 697 (2%) | 742 (1%) | 606 (2%) |
| drawdown_throttled[r=0.25%] | $1000 | 1212 (2%) | 1484 (1%) | 1011 (6%) |
| drawdown_throttled[r=0.5%] | $100 | 216 (3%) | 217 (3%) | 195 (3%) |
| drawdown_throttled[r=0.5%] | $200 | 391 (3%) | 436 (3%) | 309 (5%) |
| drawdown_throttled[r=0.5%] | $500 | 703 (5%) | 1090 (3%) | 531 (9%) |
| drawdown_throttled[r=0.5%] | $1000 | 1062 (9%) | 2182 (3%) | 911 (13%) |
| drawdown_throttled[r=0.5%,lev=2] | $100 | 214 (3%) | 215 (3%) | 194 (3%) |
| drawdown_throttled[r=0.5%,lev=2] | $200 | 389 (3%) | 432 (3%) | 308 (5%) |
| drawdown_throttled[r=0.5%,lev=2] | $500 | 702 (5%) | 1081 (3%) | 531 (9%) |
| drawdown_throttled[r=0.5%,lev=2] | $1000 | 1063 (9%) | 2163 (3%) | 911 (13%) |
| drawdown_throttled[r=0.75%] | $100 | 265 (5%) | 292 (5%) | 202 (6%) |
| drawdown_throttled[r=0.75%] | $200 | 404 (6%) | 587 (5%) | 281 (8%) |
| drawdown_throttled[r=0.75%] | $500 | 637 (9%) | 1470 (5%) | 482 (13%) |
| drawdown_throttled[r=0.75%] | $1000 | 964 (13%) | 2941 (5%) | 830 (21%) |
| drawdown_throttled[r=1%] | $100 | 277 (6%) | 348 (6%) | 193 (9%) |
| drawdown_throttled[r=1%] | $200 | 385 (9%) | 697 (6%) | 261 (11%) |
| drawdown_throttled[r=1%] | $500 | 575 (12%) | 1746 (6%) | 459 (18%) |
| drawdown_throttled[r=1%] | $1000 | 918 (18%) | 3493 (6%) | 815 (23%) |
| regime_throttled[r=0.5%] | $100 | 190 (3%) | 191 (3%) | 175 (3%) |
| regime_throttled[r=0.5%] | $200 | 352 (3%) | 386 (3%) | 283 (4%) |
| regime_throttled[r=0.5%] | $500 | 651 (5%) | 966 (3%) | 505 (8%) |
| regime_throttled[r=0.5%] | $1000 | 1011 (8%) | 1933 (3%) | 898 (14%) |
| fractional_kelly[k=0.25,pooled] | $100 | 191 (5%) | 200 (4%) | 161 (6%) |
| fractional_kelly[k=0.25,pooled] | $200 | 324 (6%) | 403 (4%) | 257 (10%) |
| fractional_kelly[k=0.25,pooled] | $500 | 593 (11%) | 1010 (4%) | 483 (14%) |
| fractional_kelly[k=0.25,pooled] | $1000 | 967 (14%) | 2020 (4%) | 819 (21%) |

Reading:

- **The throttled policies degrade gracefully** under every single-factor stress. The 0.5%
  candidate stays at or above the control's terminal equity in every row except slippage ×2
  and payoff −25% (both about equal to the control) and the composite.
- **Slippage is the dominant execution risk.** Doubling it erases the 0.5% candidate's
  whole gain (389 → 186; the control falls 294 → 188) and puts the 1% policy below the
  control (163).
- **Size-dependent impact is the single largest unknown above $200.** At $1,000 it swings the
  0.5% candidate between 2.16× (no impact) and 0.91× (impact referenced at $50).
- **Tail gaps.** A gap through the tightest stop to the 10% emergency guard raises the
  candidate's max drawdown from 3% to 7%. Pure invalidation sizing would lose
  more. The floor is what keeps a 4 bps stop from becoming a near-1× equity position.

## 13. Leverage

$200, sequential:

| Policy | Scenario | Terminal | Max DD | Avg/max lev | Max margin use | Min liq. distance | Rejected (liq. before stop) | Liq / near-miss |
|---|---|---|---|---|---|---|---|---|
| Control (fixed 2×) | WEAK | 294 | 1.7% | 2.00/2 | 30% | 33.3% | – | 0/0 |
| throttled 1%, cap 1× | WEAK | 303 | 4.5% | 1.00/1 | 90% | 83.3% | 0 | 0/0 |
| throttled 1%, cap 2× | WEAK | 366 | 7.5% | 1.98/2 | 90% | 33.3% | 0 | 0/0 |
| throttled 1%, cap 3× | WEAK | 385 | 9.3% | 2.65/3 | 81% | 23.3% | 0 | 0/0 |
| throttled 1%, cap 5× | WEAK | 385 | 9.3% | 2.65/3 | 81% | 23.3% | 0 | 0/0 |
| stress 5×, safety on | WEAK | 254 | 29.3% | 4.21/5 | 100% | 15.0% | 36 | 0/0 |
| stress 10×, safety on | WEAK | 398 | 12.8% | 3.84/7 | 96% | 13.0% | 603 | 0/0 |
| stress 5×, safety off | WEAK | 239 | 31.6% | 4.23/5 | 100% | 10.0% | 0 | 0/0 |
| stress 10×, safety off | WEAK | 205 | 40.7% | 5.38/9 | 98% | 7.5% | 0 | 0/0 |
| stress 10×, safety off | NULL | 3 | 98.3% | 6.57/9 | 95% | 7.5% | 0 | 0/0 |

Reading:

- **The 5× cap is identical to 3×.** Margin per position is capped at 34% of equity and
  notional at 1× equity, so the minimal leverage never needs more than 3×.
- **Liquidation never fired.** MAE never reached a liquidation price, because the 10%
  emergency guard exits first. The liquidation-safety rule is what guarantees that ordering:
  at 10×, 603 of 1,576 trades would have had liquidation inside 2× their loss distance or
  inside 12.5%, and were rejected.
- **With safety off at 10×,** minimum liquidation distance was 7.5%, inside the emergency
  guard. Liquidation, not the thesis, would bound the loss. No trade relies on that in the
  recommended policy.
- **Liquidation is approximated** as 1/L − 1/(2 × venue max leverage). Hyperliquid's tiered
  maintenance margin is not modelled, and liquidation is checked against recorded MAE (minute marks in
  the ledger, 15 s ticks in synthetic streams).

Recommendation:

- Leverage cap **2×**, minimal-integer leverage, with margin ≤ 34% of equity per position.
- Liquidation distance ≥ max(2 × loss distance, 12.5%).
- 1× would be needlessly capital-hungry at $100–200, because margin, not risk, would bind.
- 3× adds return only by pushing more notional through the uncertain impact model.
- Anything above 3× is stress-only.

## 14. Recommended policy and live-risk envelope (not activated)

Selected (post-hoc variant of the declared rule, §5): **`drawdown_throttled_risk_v1[r=0.5%,lev=2]`**
(policy id in [`prospective-design.json`](evidence/risk-position-sizing-v1/prospective-design.json)).

| Element | Envelope |
|---|---|
| Base intended risk | **0.5% of equity at the thesis invalidation** (1R), including costs |
| Loss distance | max(invalidation, 0.5 × frozen volatility move, 25 bps) from the executable fill |
| Cost gate | halve risk if costs > 25% of distance; reject above 50%; never upsize |
| Drawdown ladder | ×0.75 at 5%, ×0.5 at 10%, ×0.25 at 20%; halt new entries at 35% |
| Leverage | minimal integer, cap 2×; margin ≤ 34% equity per position |
| Hard caps | notional ≤ 1× equity; ≤ 10% displayed depth; gross ≤ 1.5×; margin ≤ 90%; open risk ≤ 1.5% (3R); same-direction cluster ≤ 1% (2R, satellites ×1.5); net ≤ 1.5×; 3 positions; 2 satellites; 1 per asset |
| Liquidation | ≥ max(2 × loss distance, 12.5%) or reject |
| Rounding | venue szDecimals floor; $10 minimum |

Why 0.5% and not 1%:

- 0.5% sits on the plateau of the weak-edge results. 0.75% and 1% gain little or nothing and
  roughly double the drawdown.
- 0.5% is the most degradation-tolerant under slippage and payoff stresses.
- It is the largest risk at which the $100 account is not dominated by the venue minimum.
- It is still about 2.6× the control's average realised risk (0.29% vs 0.11–0.14%).
- The brief's 0.5–1.5% suggestion was not treated as a target. The evidence points to the
  bottom of that range.

The regime throttle is not included. Its benefit appears only in the scenario designed to
reward it. Fractional Kelly and evidence weighting stay research-only.

What this does **not** show:

- That Prism has an edge.
- That 0.5% beats the control on Prism's own trades.
- That anything holds above $200, where the impact model decides the answer.

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

- **No real-trade replay.** One closed Nimble trade exists in committed evidence. Every
  performance number here comes from planted-edge synthetic worlds whose geometry is Prism's
  but whose market paths and edges are invented. Treat the comparisons as properties of the
  rules, conditional on a world, never as forecasts.
- **The selection is post-hoc.** The declared strict rule picked nothing. The recommended
  policy comes from a variant that drops one composite stress after seeing results. It
  coincides with the placeholder chosen beforehand, and it is stated as such.
- **Size-dependent impact is assumed, not measured.** Square root above $100 decides every
  $500/$1,000 result.
- **Simplifications.** The synthetic exits omit state-rule invalidations (flow, OI,
  absorption), cost-decay and data-failure exits. Fills use 15 s ticks, against 2 s polling in
  production. Sequential sizing uses realised rather than marked equity. Impact is computed
  at the pre-cap notional, which is conservative.
- **Volatility floors from real ledgers are incomplete.** For structural, risk-multiple and
  event families the floor cannot be reconstructed, because the thesis does not persist
  `volatility_move`.
- **Sequential replay cannot add trades.** Policies with spare capacity are under-credited
  on real ledgers. The synthetic streams have no such censoring.
- **Per-hypothesis statistics are impossible at Prism's volume.** About 55 trades per
  hypothesis came from 120 synthetic days of a busy stream. Evidence-weighted or Kelly sizing
  per setup is years away at the current prospective rate.

## Verdict

**READY WITH SPECIFIC LIMITATIONS.**

**Ready.** The implementation is complete and tested; it is offline, versioned and
deterministic. The rules behave as designed:

- they normalise risk per trade;
- they bound losses through the drawdown halt;
- they never upsize after losses;
- they never rely on liquidation.

A conservative candidate (0.5% at invalidation, volatility floor, 2× cap, drawdown ladder)
is well defined. A paired prospective paper cohort is the only way to get the evidence this
phase lacks.

**Specific limitations to clear before any activation phase:**

1. Run `market sizing replay --reconcile` against an offline backup copy and confirm the
   control reproduces every recorded trade.
2. Extend the prospective entry record with `volatility_move`, regime ratio and the sizing
   explanation (§15).
3. Treat the post-hoc selection, the unmeasured impact model and the $100 minimum-order
   effects as open questions the cohort must answer, not as settled results.
4. Keep Kelly and evidence weighting research-only until ≥ 200 trades per cohort exist.
5. Obtain separate operator approval to register `dynamic_lab_risk_sizing_v1`.

Nothing was activated.
