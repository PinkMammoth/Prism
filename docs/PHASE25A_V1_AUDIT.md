# Phase 25A pre-change production audit

Read directly from Railway on 2026-10-09 ~06:24 UTC, deployed revision
`2a58e751682d` (latest main). Raw definitions/counts/runtime durations:
`docs/evidence/phase25a/v1-production-audit.json`.

Run: `paperrun_27b0a336e707c389a3fb574cd9d09a7800b563f0691612bf4fb4f1fd97029279`.
Created 2026-10-04T14:59:39.411736Z; age 4.64 days at audit. ACTIVE and healthy;
40 successful cycles, 5 processed daily bars, 5 signal evaluations, **0 fired signals,
0 intents, 0 orders/fills/trades**, no positions or pending orders. Cash, equity and
peak equity 10,000 simulated USDC; all PnL, fees, exposure and drawdown zero.

Frozen universe: `ma_trend_10_50_long`, `ma_trend_20_100_short`, each on AAVE, BTC,
ETH, HYPE, LINK, SOL on Hyperliquid daily bars with 400-day lookbacks. Both have
10-day primary fixed exits; no target, discretionary exit or optimized stop.
Sizing 20% current equity notional, isolated 2x margin, max 3 positions, gross cap
60%, per-asset cap 20%, free-cash reserve 25%; 3% daily entry halt, 15% drawdown kill.

Admission: enrolled and compiler-compatible, active forward tracking, eligible evidence
(EXPLORATORY/RESEARCH_SUPPORTED), >=30 independent events on >=3 assets, positive net
excess, breadth/not-isolated-spike guards, full research CONSISTENT/MIXED, validation
registered and not adverse/error, cross-venue not adverse, DEVELOPING/MATURE forward
not adverse. These constraints do not explain the observed zero intents: neither
frozen daily crossover fired. They would further constrain exploratory throughput.
Daily crossovers and 10-day holds do not respond to most intraday market moves.
No retrospective missed-trade claims can be made from absent v1 signal events alone.

Policy IDs (all preserved):
- promotion `appolicy_1f0b69cff1c4c5d0584a62f124f0a5902d37a7b9f0a3d05f388db80e27ce0277`
- risk `riskpolicy_e755fb81dd3154b593f75385a9916177bee00e6240f68f1cfd8799750db67181`
- execution `execmodel_3a94bdf5f64156550972b53c1067c48e05a4baede915d6b075cdcfde34108f39`
- exit `exitpolicy_4ef7a1b26f9d6d4c24d32e942ce5257372827464e956490bcc23dcc2c8103812`
- maturity `papermaturity_0b19dee3e95d87645f9eb62d9362600a3fd24764a303583224e144d27dbde620`

Runtime: one shared Railway service, one 5 GB volume (~2.11 GB used at audit).
`prospective` runs 00:10, 00:45, 03:00, 06:00, 11:00, 17:00 UTC, including boot catchup.
Only `paper` (`lab paper run`) and `brief` (`lab paper brief --send`) are solely v1.
The intraday `shadow` step only observes v1 execution orders and can also be removed
once v1 is flat/retired. Forward, copilot, incubation, data, OI, context, microstructure,
checkpoints and backups have independent purposes and must stay.

Recent measured paper+brief steps: ~3–4 seconds per invocation, ~18–24 seconds/day
at six scheduled runs. This is wall-clock/process-start saving, not measured CPU.
Paper evaluation itself makes zero market API calls; retirement eliminates at most one
Telegram brief/day (plus hypothetical trade notifications), six paper cycle inserts/day,
~2 paper events/day, one snapshot/day, one brief/day and its delivery-attempt rows.
Shadow is normally idle because there are no orders. No whole service is removed;
Railway dollar savings are not measurable from these observations and likely small.
