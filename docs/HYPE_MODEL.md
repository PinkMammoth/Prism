# HYPE valuation model

**Status: current and prospective only.** The inputs have no defensible point-in-time
history, so this model is never used in historical backtests.

## Mechanism being modelled

1. **Trading fees → Assistance Fund (AF).** Hyperliquid routes the large majority of
   protocol fees (reported at 97–99%) to the AF at `0xfefe…fefe`. The AF buys HYPE on the
   open market and burns it.
2. **AQAv2** (live for USDC since 2026-08-26). Stablecoin deployers share about 90% of the
   *cost-adjusted* reserve yield earned on USDC held on Hyperliquid. The yield is paid to
   the AF in 30-day cycles, with an 8-day delay; the first payout was reported for
   2026-10-03. It funds further buyback-and-burn.

## Formula

```text
normalised_core_revenue = blend(30d, 90d, 365d annualised run-rates)        [derived]
core_bid                = normalised_core_revenue × af_share_of_revenue      [derived; af_share assumed, default 1.0]
AQAv2_revenue           = USDC_on_Hyperliquid × eligible_fraction × yield_share
                          × max(reserve_yield − cost_adjustment, 0)          [derived]
structural_bid          = core_bid + AQAv2_revenue                           [derived]
buyback_yield           = structural_bid / (price × circulating_supply)      [derived]
net_structural_yield    = (structural_bid − contributor_unlocks × sell_fraction × price
                           − staking_emissions × price) / market_cap         [derived, partial]
```

## Inputs, by kind

| Input | Kind | Source |
|---|---|---|
| HYPE price | observed | Hyperliquid spot HYPE/USDC daily close (USDC treated as USD) |
| Circulating supply | observed | Hyperliquid `tokenDetails(HYPE).circulatingSupply`, snapshot on each update |
| USDC on Hyperliquid | observed | Hyperliquid `tokenDetails(USDC).circulatingSupply`, snapshot |
| Daily protocol revenue | observed (third-party reconstruction) | DefiLlama `summary/fees/hyperliquid?dataType=dailyRevenue` |
| Reserve yield | observed | FRED `DTB3` (3M T-bill), or an **assumed** override |
| AF HYPE balance | observed (display) | Hyperliquid `spotClearinghouseState(0xfefe…)` |
| Normalisation method and weights | assumed | `config/hype.yaml` |
| af_share_of_revenue (1.0) | assumed | `config/hype.yaml` |
| AQAv2 yield share (0.90) | assumed (reported figure) | `config/hype.yaml` |
| AQAv2 cost adjustment (35 bp) | assumed | `config/hype.yaml`. Real issuer costs are not published. |
| Eligible USDC fraction (1.0) | assumed | `config/hype.yaml` |
| Contributor unlocks (~9.92M/month to Nov 2027) and sell fraction (0.5) | assumed | `config/hype.yaml` |
| Staking emissions | **unknown → N/A** | Not zero-filled, so the net yield is shown as *partial* |

Every output line in the CLI (`market hype`) and on the dashboard page is labelled
*observed*, *assumed* or *derived*, with its source and as-of time.

## Missing-data behaviour

- A run-rate window needs at least 95% of its days. Otherwise it is N/A, and short windows
  are not scaled up.
- The blend needs every window. With less than a year of revenue history it is N/A, not a
  re-weighted guess. Choose `normalisation: 90d` explicitly if you accept that.
- If supply or price is missing, the yield and signal are `UNKNOWN`, with a warning.
- If USDC supply or the reserve yield is missing, AQAv2 revenue is N/A, so the structural
  bid is N/A. The core-only yield is still shown.

## Signal bands (configurable, not gospel)

| Buyback yield | Signal |
|---|---|
| ≥ 6% | STRONGLY_UNDERVALUED |
| 5–6% | UNDERVALUED |
| 4–5% | FAIR |
| 3–4% | OVERVALUED |
| < 3% | STRONGLY_OVERVALUED |

## The inverse question

*For any HYPE price, what protocol revenue and/or USDC supply would justify it?*

At a target yield *y* (default 4.5%, the fair-band midpoint), holding everything else
fixed:

```text
required_bid          = y × price × circulating_supply
required_core_revenue = (required_bid − AQAv2_revenue) / af_share           (USDC held fixed)
required_USDC         = (required_bid − core_bid)
                        / (eligible × share × (reserve_yield − cost))        (revenue held fixed)
```

A round-trip test guarantees that plugging the required value back in yields exactly *y*.
The dashboard and `market hype` show the table over a price grid (−40% … +50%).

**Sensitivity.** A grid of revenue ±15/30%, USDC ±30% and reserve yield ±1pp shows how
fragile the signal is. If a plausible ±15% revenue move flips UNDERVALUED to FAIR, treat
the band as low-confidence.

## Known weaknesses

- **DefiLlama's revenue definition** ("fees retained by protocol") can change with adapter
  updates. The model inherits any methodology change.
- **The AQAv2 cost adjustment is a guess**, because issuers do not publish it. At a 4%
  T-bill yield, moving it by ±25 bp changes AQAv2 revenue by about ±7%.
- **Buybacks are not a claim on cash flows.** The yield framing is an analytical lens, not a
  guarantee of price support. The AF's behaviour can change by governance or validator
  decision.
- **Circulating supply definitions differ** across sources (Hyperliquid, CoinGecko,
  tokenomist). Prism uses Hyperliquid's own figure.
- **No history.** The model cannot be backtested honestly. Snapshots stored from first use
  onwards will allow a genuinely point-in-time track record over time.
