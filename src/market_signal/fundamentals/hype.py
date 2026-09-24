"""HYPE valuation: structural buyback yield.

    structural_bid  = normalised_core_protocol_revenue × af_share + AQAv2_revenue
    AQAv2_revenue   = USDC_on_Hyperliquid × eligible × yield_share × max(reserve_yield − cost, 0)
    buyback_yield   = structural_bid / (price × circulating_supply)

Every number is a ``Value`` tagged OBSERVED (from a provider, with source and as-of),
ASSUMED (from config/hype.yaml) or DERIVED (computed here). Missing observed inputs stay
missing and propagate: the model reports what it cannot compute instead of guessing.

This is a CURRENT/PROSPECTIVE model. Revenue history from DefiLlama is a third-party
reconstruction and is never used for historical backtests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd

Kind = Literal["observed", "assumed", "derived"]


@dataclass(frozen=True)
class Value:
    name: str
    value: float | None
    kind: Kind
    unit: str
    source: str
    as_of: str | None = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None and np.isfinite(self.value)


def _v(
    name: str,
    value: float | None,
    kind: Kind,
    unit: str,
    source: str,
    as_of: Any = None,
    note: str = "",
) -> Value:
    if value is not None and not np.isfinite(value):
        value = None
    return Value(
        name,
        None if value is None else float(value),
        kind,
        unit,
        source,
        None if as_of is None else str(as_of),
        note,
    )


@dataclass
class HypeInputs:
    price: Value
    circulating_supply: Value
    usdc_on_hyperliquid: Value
    reserve_yield: Value  # observed (T-bill) or assumed override
    daily_revenue: pd.Series  # observed (reconstructed by DefiLlama), indexed by date
    revenue_as_of: str | None = None
    extra_observed: list[Value] = field(default_factory=list)


def run_rates(
    daily: pd.Series, as_of: date, windows: dict[str, int], min_coverage: float
) -> dict[str, float | None]:
    """Annualised run-rates over trailing windows ending at ``as_of`` (inclusive).

    A window with fewer than ``min_coverage`` of its days present is None (not scaled up).
    """
    out: dict[str, float | None] = {}
    if daily is None or daily.empty:
        return dict.fromkeys(windows)
    s = daily.copy()
    s.index = pd.to_datetime(s.index).date
    for name, days in windows.items():
        start = as_of - pd.Timedelta(days=days - 1)
        w = s[(s.index >= start) & (s.index <= as_of)].dropna()
        out[name] = float(w.sum() * 365 / days) if len(w) >= min_coverage * days else None
    return out


def normalised_revenue(
    rates: dict[str, float | None], cfg: dict[str, Any]
) -> tuple[float | None, str]:
    method = cfg.get("normalisation", "blend")
    haircut = float(cfg.get("haircut", 0.0))
    if method in rates:
        v = rates[method]
        desc = f"{method} run-rate"
    elif method == "min":
        vals = [x for x in rates.values() if x is not None]
        v = min(vals) if len(vals) == len(rates) else None
        desc = "min of run-rates"
    else:
        w = cfg.get("blend_weights") or {}
        if any(rates.get(k) is None for k in w):
            return None, "blend (a component window is missing)"
        v = sum(rates[k] * float(wt) for k, wt in w.items()) / sum(float(x) for x in w.values())
        desc = "blend " + ", ".join(f"{k}×{wt}" for k, wt in w.items())
    return (None if v is None else v * (1 - haircut)), desc + (
        f", haircut {haircut:.0%}" if haircut else ""
    )


def aqa_revenue(
    usdc: float | None, reserve_yield: float | None, cfg: dict[str, Any]
) -> float | None:
    if usdc is None or reserve_yield is None:
        return None
    net = max(reserve_yield - float(cfg["cost_adjustment"]), 0.0)
    return usdc * float(cfg["eligible_fraction"]) * float(cfg["yield_share"]) * net


def band(y: float | None, bands: list[dict[str, Any]]) -> str:
    if y is None or not np.isfinite(y):
        return "UNKNOWN"
    for b in sorted(bands, key=lambda b: -float(b["min"])):
        if y >= float(b["min"]):
            return str(b["label"])
    return "UNKNOWN"


@dataclass
class HypeValuation:
    observed: list[Value]
    assumed: list[Value]
    derived: dict[str, Value]
    signal: str
    warnings: list[str]
    cfg: dict[str, Any]

    def get(self, name: str) -> float | None:
        v = self.derived.get(name)
        return v.value if v else None

    # --- inverse: what is required to justify a price? -------------------------------
    def required_for_price(
        self, price: float, target_yield: float | None = None
    ) -> dict[str, float | None]:
        cfg = self.cfg
        y = float(target_yield or cfg["inverse_table"]["target_yield"])
        circ = self.get("circulating_supply")
        core = self.get("core_bid")
        aqa = self.get("aqa_revenue")
        a = cfg["aqa_v2"]
        ry = self.get("reserve_yield")
        if circ is None:
            return {
                "price": price,
                "required_bid": None,
                "required_core_revenue": None,
                "required_usdc": None,
                "yield_at_price": None,
            }
        req_bid = y * price * circ
        af = float(cfg["revenue"]["af_share_of_revenue"])
        req_core = (req_bid - (aqa or 0.0)) / af if aqa is not None else None
        per_usdc = (
            (
                float(a["eligible_fraction"])
                * float(a["yield_share"])
                * max(ry - float(a["cost_adjustment"]), 0.0)
            )
            if ry is not None
            else None
        )
        req_usdc = (req_bid - core) / per_usdc if core is not None and per_usdc else None
        bid = self.get("structural_bid")
        return {
            "price": price,
            "required_bid": req_bid,
            "required_core_revenue": None if req_core is None else max(req_core, 0.0),
            "required_usdc": None if req_usdc is None else max(req_usdc, 0.0),
            "yield_at_price": (bid / (price * circ)) if bid is not None else None,
        }

    def inverse_table(self) -> pd.DataFrame:
        price = self.get("price")
        if price is None:
            return pd.DataFrame()
        rows = []
        for pct in self.cfg["inverse_table"]["price_grid_pct"]:
            p = price * (1 + pct / 100)
            r = self.required_for_price(p)
            r["signal"] = band(r["yield_at_price"], self.cfg["bands"])
            rows.append(r)
        return pd.DataFrame(rows)

    def price_for_yield(self, y: float) -> float | None:
        bid, circ = self.get("structural_bid"), self.get("circulating_supply")
        if bid is None or circ is None or y <= 0:
            return None
        return bid / (y * circ)

    def zones(self) -> dict[str, tuple[float | None, float | None]]:
        """Price zones implied by the yield bands (higher yield = lower price)."""
        b = {x["label"]: float(x["min"]) for x in self.cfg["bands"]}
        return {
            "strong_buy": (None, self.price_for_yield(b["STRONGLY_UNDERVALUED"])),
            "accumulate": (
                self.price_for_yield(b["STRONGLY_UNDERVALUED"]),
                self.price_for_yield(b["UNDERVALUED"]),
            ),
            "fair": (self.price_for_yield(b["UNDERVALUED"]), self.price_for_yield(b["FAIR"])),
            "overvalued": (self.price_for_yield(b["FAIR"]), self.price_for_yield(b["OVERVALUED"])),
        }

    def sensitivity(self) -> pd.DataFrame:
        s = self.cfg["sensitivity"]
        core, usdc, ry = (
            self.get("normalised_revenue"),
            self.get("usdc_on_hyperliquid"),
            self.get("reserve_yield"),
        )
        price, circ = self.get("price"), self.get("circulating_supply")
        if None in (core, usdc, ry, price, circ):
            return pd.DataFrame()
        a = self.cfg["aqa_v2"]
        af = float(self.cfg["revenue"]["af_share_of_revenue"])
        rows = []
        for rp in s["revenue_pct"]:
            for up in s["usdc_pct"]:
                for yp in s["reserve_yield_pp"]:
                    bid = core * (1 + rp / 100) * af + aqa_revenue(
                        usdc * (1 + up / 100), ry + yp / 100, a
                    )
                    y = bid / (price * circ)
                    rows.append({"revenue_chg": rp, "usdc_chg": up, "reserve_yield_chg_pp": yp, "buyback_yield": y,
                                 "signal": band(y, self.cfg["bands"])})  # fmt: skip
        return pd.DataFrame(rows)


def value_hype(inp: HypeInputs, cfg: dict[str, Any], as_of: date) -> HypeValuation:
    rcfg, acfg, dcfg = cfg["revenue"], cfg["aqa_v2"], cfg["dilution"]
    warnings: list[str] = []
    rates = run_rates(
        inp.daily_revenue,
        as_of,
        {"30d": 30, "90d": 90, "365d": 365},
        float(rcfg["min_window_coverage"]),
    )
    norm, method = normalised_revenue(rates, rcfg)
    for k, v in rates.items():
        if v is None:
            warnings.append(f"revenue {k} run-rate unavailable (insufficient days)")
    active = as_of >= pd.Timestamp(acfg["active_since"]).date()
    aqa = (
        aqa_revenue(inp.usdc_on_hyperliquid.value, inp.reserve_yield.value, acfg) if active else 0.0
    )
    if active and aqa is None:
        warnings.append("AQAv2 revenue unavailable (USDC supply or reserve yield missing)")
    af = float(rcfg["af_share_of_revenue"])
    core_bid = norm * af if norm is not None else None
    bid = (core_bid + aqa) if core_bid is not None and aqa is not None else None
    circ, price = inp.circulating_supply.value, inp.price.value
    mcap = price * circ if price is not None and circ is not None else None
    y = bid / mcap if bid is not None and mcap else None
    y_core = core_bid / mcap if core_bid is not None and mcap else None

    # dilution (display): contributor unlocks sold × price + staking emissions (if known)
    unlock_until = pd.Timestamp(dcfg["contributor_unlock_until"]).date()
    unlocks = float(dcfg["contributor_unlock_per_month"]) * 12 if as_of <= unlock_until else 0.0
    sell = unlocks * float(dcfg["contributor_sell_fraction"]) * price if price is not None else None
    staking = dcfg.get("staking_emissions_per_year")
    staking_val = float(staking) * price if staking is not None and price is not None else None
    if staking is None:
        warnings.append("staking emissions unknown: net yield excludes them (shown as partial)")
    net_bid = bid - sell - (staking_val or 0.0) if bid is not None and sell is not None else None
    net_y = net_bid / mcap if net_bid is not None and mcap else None

    D = "derived"
    derived = {
        "price": _v("price", price, D, "USD", "observed price", inp.price.as_of),
        "circulating_supply": _v("circulating_supply", circ, D, "HYPE", "observed supply", inp.circulating_supply.as_of),
        "usdc_on_hyperliquid": _v("usdc_on_hyperliquid", inp.usdc_on_hyperliquid.value, D, "USD", "observed"),
        "reserve_yield": _v("reserve_yield", inp.reserve_yield.value, D, "p.a.", inp.reserve_yield.source),
        "market_cap": _v("market_cap", mcap, D, "USD", "price × circulating supply"),
        "revenue_30d_annualised": _v("revenue_30d_annualised", rates.get("30d"), D, "USD/yr", "sum(30d) × 365/30"),
        "revenue_90d_annualised": _v("revenue_90d_annualised", rates.get("90d"), D, "USD/yr", "sum(90d) × 365/90"),
        "revenue_365d": _v("revenue_365d", rates.get("365d"), D, "USD/yr", "sum(365d)"),
        "normalised_revenue": _v("normalised_revenue", norm, D, "USD/yr", method),
        "core_bid": _v("core_bid", core_bid, D, "USD/yr", f"normalised revenue × af_share ({af:.0%})"),
        "aqa_revenue": _v("aqa_revenue", aqa, D, "USD/yr",
                          "USDC × eligible × share × max(reserve yield − cost, 0)" if active else "AQAv2 not active at as-of date"),
        "structural_bid": _v("structural_bid", bid, D, "USD/yr", "core bid + AQAv2 revenue"),
        "buyback_yield": _v("buyback_yield", y, D, "p.a.", "structural bid / market cap"),
        "core_only_yield": _v("core_only_yield", y_core, D, "p.a.", "core bid / market cap"),
        "contributor_sell_pressure": _v("contributor_sell_pressure", sell, D, "USD/yr", "unlocks/yr × sell fraction × price"),
        "net_structural_yield": _v("net_structural_yield", net_y, D, "p.a.",
                                   "(bid − contributor sells − staking emissions) / market cap",
                                   note="partial: staking emissions unknown" if staking is None else ""),
    }  # fmt: skip
    assumed = [
        _v(
            "revenue_normalisation",
            None,
            "assumed",
            "",
            f"hype.yaml revenue.normalisation = {method}",
        ),
        _v(
            "af_share_of_revenue",
            af,
            "assumed",
            "fraction",
            "hype.yaml revenue.af_share_of_revenue",
        ),
        _v(
            "aqa_yield_share",
            float(acfg["yield_share"]),
            "assumed",
            "fraction",
            "hype.yaml aqa_v2.yield_share",
        ),
        _v(
            "aqa_cost_adjustment",
            float(acfg["cost_adjustment"]),
            "assumed",
            "p.a.",
            "hype.yaml aqa_v2.cost_adjustment",
        ),
        _v(
            "aqa_eligible_fraction",
            float(acfg["eligible_fraction"]),
            "assumed",
            "fraction",
            "hype.yaml aqa_v2.eligible_fraction",
        ),
        _v(
            "contributor_unlock_per_month",
            float(dcfg["contributor_unlock_per_month"]),
            "assumed",
            "HYPE",
            "hype.yaml dilution",
        ),
        _v(
            "contributor_sell_fraction",
            float(dcfg["contributor_sell_fraction"]),
            "assumed",
            "fraction",
            "hype.yaml dilution",
        ),
    ]
    if inp.reserve_yield.kind == "assumed":
        assumed.append(inp.reserve_yield)
    observed = [inp.price, inp.circulating_supply, inp.usdc_on_hyperliquid, *( [inp.reserve_yield] if inp.reserve_yield.kind == "observed" else []),
                _v("daily_revenue_history", float(len(inp.daily_revenue)), "observed", "days",
                   "DefiLlama summary/fees/hyperliquid (reconstructed by third party)", inp.revenue_as_of), *inp.extra_observed]  # fmt: skip
    for v in (inp.price, inp.circulating_supply):
        if not v.ok:
            warnings.append(f"{v.name} missing: valuation cannot be computed")
    return HypeValuation(observed, assumed, derived, band(y, cfg["bands"]), warnings, cfg)


# --------------------------------------------------------------------------- store glue


def load_hype_inputs(store, settings, as_of: date | None = None) -> HypeInputs:
    """Assemble inputs from the local database (never from the network)."""
    from market_signal.data.pit import load_macro
    from market_signal.data.prices import PriceBasis, load_bars
    from market_signal.models.domain import Timeframe

    cfg = settings.yaml("hype.yaml")
    asset = settings.asset("HYPE")
    bars = load_bars(store, asset, Timeframe.D1, PriceBasis.RAW)
    if not bars.empty:
        last = bars.iloc[-1]
        price = _v(
            "price",
            last["close"],
            "observed",
            "USD",
            f"{asset.series[Timeframe.D1].provider} HYPE/USDC daily close",
            last["close_time"],
        )
    else:
        price = _v("price", None, "observed", "USD", "no HYPE bars stored")

    def metric(name: str) -> tuple[float | None, str | None, str]:
        df = store.query(
            """SELECT value, fetched_at, source FROM crypto_metrics WHERE symbol='HYPE' AND metric=? AND pit_method='snapshot'
               ORDER BY fetched_at DESC LIMIT 1""",
            [name],
        )
        if df.empty:
            return None, None, "not fetched"
        return float(df.iloc[0]["value"]), str(df.iloc[0]["fetched_at"]), str(df.iloc[0]["source"])

    circ, circ_t, circ_src = metric("circulating_supply")
    usdc, usdc_t, usdc_src = metric("usdc_on_hyperliquid")
    af_bal, af_t, af_src = metric("af_hype_balance")
    override = cfg["aqa_v2"].get("reserve_yield_override")
    if override is not None:
        ry = _v(
            "reserve_yield",
            float(override),
            "assumed",
            "p.a.",
            "hype.yaml aqa_v2.reserve_yield_override",
        )
    else:
        rows = load_macro(store, cfg["aqa_v2"]["reserve_yield_series"], research=False)
        rows = rows.dropna(subset=["value"]) if not rows.empty else rows
        if rows.empty:
            ry = _v("reserve_yield", None, "observed", "p.a.", "FRED DTB3 (not fetched)")
        else:
            r = rows.sort_values("obs_date").iloc[-1]
            ry = _v(
                "reserve_yield",
                r["value"] / 100,
                "observed",
                "p.a.",
                "FRED DTB3 (3M T-bill)",
                r["obs_date"],
            )
    rev = store.query(
        """SELECT obs_date, value FROM crypto_metrics WHERE symbol='HYPE' AND metric='daily_revenue'
           AND pit_method='reconstructed' ORDER BY obs_date"""
    )
    daily = (
        pd.Series(rev["value"].to_numpy(), index=pd.to_datetime(rev["obs_date"]).dt.date)
        if not rev.empty
        else pd.Series(dtype=float)
    )
    return HypeInputs(
        price=price,
        circulating_supply=_v(
            "circulating_supply", circ, "observed", "HYPE", f"{circ_src} tokenDetails", circ_t
        ),
        usdc_on_hyperliquid=_v(
            "usdc_on_hyperliquid", usdc, "observed", "USD", f"{usdc_src} tokenDetails(USDC)", usdc_t
        ),
        reserve_yield=ry,
        daily_revenue=daily,
        revenue_as_of=str(daily.index.max()) if len(daily) else None,
        extra_observed=[
            _v(
                "af_hype_balance",
                af_bal,
                "observed",
                "HYPE",
                f"{af_src} spotClearinghouseState(0xfefe…)",
                af_t,
            )
        ],
    )


def hype_valuation_from_store(store, settings, as_of: date | None = None) -> HypeValuation:
    cfg = settings.yaml("hype.yaml")
    inp = load_hype_inputs(store, settings, as_of)
    if as_of is None:
        as_of = (
            inp.daily_revenue.index.max()
            if len(inp.daily_revenue)
            else pd.Timestamp.now(tz="UTC").date()
        )
    return value_hype(inp, cfg, as_of)
