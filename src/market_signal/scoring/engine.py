"""Opportunity scoring (0–100), price zones, status and sizing for the current bar.

Everything here is deterministic and explains itself: each component carries the reasons
for its points, and each input is tagged observed / assumed / derived. Valuation and entry
quality are separate components, so "cheap but technically extended" is expressible.

Score = 100 × Σ points / Σ max(points) over AVAILABLE components; coverage = Σ available
weight / total weight. Missing components never count as zero.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from market_signal.config import Settings, config_hash
from market_signal.data.prices import weekly_from_daily
from market_signal.data.store import Store, new_id
from market_signal.features import FeatureStore
from market_signal.fundamentals.modules import (
    ModuleResult,
    commodity_module,
    crypto_revenue_module,
    equity_module,
    hype_module,
)
from market_signal.models.domain import Asset, Regime, Timeframe, utcnow
from market_signal.regimes.engine import RegimeBundle, build_regimes
from market_signal.scoring.risk import INVESTMENT_EXITS, suggest_size
from market_signal.setups.base import SetupContext, get_setup

SETUP_NAMES = ("quality_pullback", "breakout_retest", "rerating")


@dataclass
class Component:
    name: str
    points: float | None
    max_points: float
    reasons: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.points is not None

    def label(self) -> str:
        return "N/A" if self.points is None else f"{self.points:.0f}/{self.max_points:.0f}"


@dataclass
class SetupState:
    setup: str
    title: str
    kind: str
    state: str  # ACTIVE | WAIT_RETEST | NEAR | NONE | N/A
    detail: str
    conditions: dict[str, bool | None] = field(default_factory=dict)
    entry_zone: tuple[float, float] | None = None
    ideal_entry: float | None = None
    stop: float | None = None
    research_verdict: str | None = None


@dataclass
class Assessment:
    symbol: str
    name: str
    asset_class: str
    as_of: str
    price: float
    regime: str
    setup: SetupState
    setups: list[SetupState]
    components: list[Component]
    score: float | None
    coverage: float
    band: str
    status: str
    status_text: str
    zones: dict[str, Any]
    sizing: dict[str, Any] | None
    module: str
    module_notes: list[str]
    factors: list[dict[str, Any]]
    warnings: list[str]

    def component(self, name: str) -> Component:
        return next(c for c in self.components if c.name == name)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return json.loads(json.dumps(d, default=_json_default))


def _json_default(o: Any) -> Any:
    if isinstance(o, float | np.floating) and not np.isfinite(o):
        return None
    if isinstance(o, np.generic):
        return o.item()
    return str(o)


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


# --------------------------------------------------------------------------- setups (current)


def _latest_research_verdicts(store: Store) -> dict[str, str]:
    try:
        df = store.query(
            """SELECT name, summary FROM research_runs QUALIFY row_number() OVER (PARTITION BY name ORDER BY created_at DESC) = 1"""
        )
    except Exception:
        return {}
    out = {}
    for _, r in df.iterrows():
        try:
            out[r["name"]] = json.loads(r["summary"])["verdict"]["verdict"]
        except (TypeError, KeyError, ValueError):
            continue
    return out


def setup_states(
    feat: pd.DataFrame, asset: Asset, ctx: SetupContext, verdicts: dict[str, str]
) -> list[SetupState]:
    out = []
    last = feat.iloc[-1]
    atr = _f(last["atr_14"])
    close = float(last["close"])
    for name in SETUP_NAMES:
        s = get_setup(name)
        p = s.default_params(asset.asset_class.value)
        res = s.evaluate(feat, p, ctx)
        verdict = verdicts.get(name)
        if name == "rerating" and asset.asset_class.value in ("etf", "commodity"):
            out.append(
                SetupState(
                    name,
                    s.title,
                    s.kind,
                    "N/A",
                    "no fundamentals for this asset class",
                    research_verdict=verdict,
                )
            )
            continue
        diag = res.diagnostics
        if name == "breakout_retest":
            out.append(_breakout_state(feat, res, p, s, verdict))
            continue
        if diag is None or diag.empty or not bool(res.eligible.iloc[-1]):
            reason = "insufficient data / not eligible (missing inputs stay missing)"
            if name == "rerating" and asset.is_crypto and not ctx.extra.get("current_fundamentals"):
                reason = "crypto fundamentals are current-only; none available"
            out.append(SetupState(name, s.title, s.kind, "N/A", reason, research_verdict=verdict))
            continue
        row = diag.iloc[-1]
        conds = {k: (None if pd.isna(v) else bool(v)) for k, v in row.items()}
        n_true = sum(1 for v in conds.values() if v)
        failing = [k for k, v in conds.items() if not v]
        recent = res.signal.iloc[-5:]
        recent_stop = (
            _f(res.stop[recent.index[recent.to_numpy()]].iloc[-1]) if recent.any() else None
        )
        still_valid = recent_stop is not None and close > recent_stop and n_true >= len(conds) - 2
        if all(conds.values()):
            state, detail = "ACTIVE", "all conditions met"
        elif still_valid:
            state, detail = (
                "ACTIVE",
                f"triggered in the last 5 bars; above invalidation {recent_stop:,.4g}",
            )
        elif n_true >= len(conds) - 2:
            state, detail = "NEAR", "missing: " + ", ".join(failing)
        else:
            state, detail = "NONE", f"{n_true}/{len(conds)} conditions met"
        zone = ideal = stop = None
        if name == "quality_pullback" and atr:
            sma50 = _f(last["sma_50"])
            if sma50:
                zone, ideal = (sma50 - 0.5 * atr, sma50 + 0.5 * atr), sma50
            stop = _f(res.stop.iloc[-1])
        out.append(
            SetupState(name, s.title, s.kind, state, detail, conds, zone, ideal, stop, verdict)
        )
    return out


def _breakout_state(feat, res, p, s, verdict) -> SetupState:
    last = feat.iloc[-1]
    atr = _f(last["atr_14"])
    sig_recent = res.signal.iloc[-3:]
    if sig_recent.any():
        i = sig_recent[sig_recent].index[-1]
        level = _f(res.diagnostics.loc[i, "level"])
        stop = _f(res.stop.loc[i])
        if level and atr and stop and float(last["close"]) > stop:
            return SetupState(
                s.name,
                s.title,
                s.kind,
                "ACTIVE",
                f"retest of {level:,.4g} held",
                {},
                (level - 0.25 * atr, level + 0.5 * atr),
                level,
                stop,
                verdict,
            )
    # a breakout still awaiting its retest?
    bo = res.diagnostics["breakout_bar"]
    window = int(p["retest_window"])
    recent_bo = bo.iloc[-window:]
    if recent_bo.any() and atr:
        b = recent_bo[recent_bo].index[-1]
        pos = feat.index.get_loc(b)
        level = float(feat["high"].iloc[max(0, pos - int(p["base_len"])) : pos].max())
        closes_after = feat["close"].iloc[pos + 1 :]
        failed = (closes_after < level - p["fail_atr"] * atr).any()
        if not failed and not res.signal.loc[b:].any():
            stop = level - p["stop_atr"] * atr
            return SetupState(s.name, s.title, s.kind, "WAIT_RETEST", f"breakout above {level:,.4g}; wait for a retest",
                              {}, (level - 0.25 * atr, level + 0.5 * atr), level, stop, verdict)  # fmt: skip
    if not bool(res.eligible.iloc[-1]):
        return SetupState(
            s.name, s.title, s.kind, "N/A", "insufficient history", research_verdict=verdict
        )
    return SetupState(
        s.name,
        s.title,
        s.kind,
        "NONE",
        "no recent breakout from a tight base",
        research_verdict=verdict,
    )


# --------------------------------------------------------------------------- components


def structure_component(feat: pd.DataFrame, weekly: pd.DataFrame, maxp: float) -> Component:
    last = feat.iloc[-1]
    items: list[tuple[str, float, bool | None]] = []

    def add(label: str, pts: float, cond: Any) -> None:
        items.append((label, pts, None if cond is None or pd.isna(cond) else bool(cond)))

    add(
        "close above 200DMA",
        3,
        None if pd.isna(last["sma_200"]) else last["close"] > last["sma_200"],
    )
    add(
        "50DMA above 200DMA",
        3,
        None if pd.isna(last["sma_200"]) else last["sma_50"] > last["sma_200"],
    )
    add("200DMA rising", 2, None if pd.isna(last["sma_200_slope"]) else last["sma_200_slope"] > 0)
    add("6-month momentum positive", 2, None if pd.isna(last["roc_6m"]) else last["roc_6m"] > 0)
    wk = weekly[weekly["complete"]] if not weekly.empty else weekly
    if len(wk) >= 40:
        w40 = wk["close"].rolling(40).mean().iloc[-1]
        w10 = wk["close"].rolling(10).mean().iloc[-1]
        add("weekly close above 40-week MA", 3, wk["close"].iloc[-1] > w40)
        add("10-week MA above 40-week MA", 2, w10 > w40)
    else:
        add("weekly close above 40-week MA", 3, None)
        add("10-week MA above 40-week MA", 2, None)
    avail = [(lab, p, c) for lab, p, c in items if c is not None]
    total = sum(p for _, p, _ in items)
    if sum(p for _, p, _ in avail) < 0.5 * total:
        return Component("structure", None, maxp, ["insufficient history for trend structure"])
    earned = sum(p for _, p, c in avail if c)
    reasons = [f"{'✓' if c else '✗'} {lab} ({p:g})" for lab, p, c in avail]
    reasons += [f"– {lab}: n/a" for lab, _, c in items if c is None]
    return Component("structure", maxp * earned / sum(p for _, p, _ in avail), maxp, reasons)


def entry_component(
    feat: pd.DataFrame, h4: pd.DataFrame | None, st: SetupState, cfg: dict, maxp: float
) -> tuple[Component, bool]:
    last = feat.iloc[-1]
    close, atr = float(last["close"]), _f(last["atr_14"])
    pts, reasons, in_zone = 0.0, [], False
    if st.state == "ACTIVE":
        pts += cfg["setup_active_points"]
        reasons.append(f"+{cfg['setup_active_points']} {st.title} active")
    elif st.state in ("NEAR", "WAIT_RETEST"):
        pts += 2
        reasons.append(f"+2 {st.title} {st.state.lower().replace('_', ' ')}")
    if st.entry_zone and atr:
        lo, hi = st.entry_zone
        if lo <= close <= hi:
            pts += cfg["in_zone_points"]
            in_zone = True
            reasons.append(f"+{cfg['in_zone_points']} price inside entry zone {lo:,.4g}–{hi:,.4g}")
        elif hi < close <= hi + cfg["near_zone_atr"] * atr:
            pts += cfg["near_zone_points"]
            reasons.append(
                f"+{cfg['near_zone_points']} price within {cfg['near_zone_atr']} ATR above the zone"
            )
        elif close > hi:
            reasons.append(f"+0 price {(close / hi - 1):.1%} above the entry zone (wait)")
        else:
            reasons.append("+0 price below the entry zone (support broken?)")
    else:
        reasons.append("+0 no defined entry zone")
    ext = _f(last["dist_sma_50_atr"])
    if ext is not None and ext <= cfg["not_extended_max_sma50_atr"]:
        pts += cfg["not_extended_points"]
        reasons.append(f"+{cfg['not_extended_points']} not extended ({ext:+.1f} ATR from 50DMA)")
    elif ext is not None:
        reasons.append(f"+0 extended ({ext:+.1f} ATR above 50DMA)")
    rsi = _f(last["rsi_14"])
    if rsi is not None and rsi < cfg["rsi_overbought"]:
        pts += cfg["rsi_points"]
        reasons.append(f"+{cfg['rsi_points']} RSI {rsi:.0f} not overbought")
    elif rsi is not None:
        reasons.append(f"+0 RSI {rsi:.0f} overbought")
    if h4 is not None and len(h4) > 30:
        l4 = h4.iloc[-1]
        ema, a4 = _f(l4["ema_21"]), _f(l4["atr_14"])
        if ema and a4 and l4["close"] > ema + cfg["h4_overextension_atr"] * a4:
            pts -= cfg["h4_penalty"]
            reasons.append(
                f"−{cfg['h4_penalty']} 4h short-term extended (close > 4h EMA21 + {cfg['h4_overextension_atr']} ATR)"
            )
        elif ema and a4:
            reasons.append("4h: not short-term extended")
    else:
        reasons.append("4h refinement unavailable (optional)")
    return Component("entry", float(np.clip(pts, 0, maxp)), maxp, reasons), in_zone


def macro_component(regime: str, maxp: float, which: str) -> Component:
    pts = {Regime.RISK_ON.value: 1.0, Regime.NEUTRAL.value: 0.6, Regime.RISK_OFF.value: 0.2}.get(
        regime
    )
    if pts is None:
        return Component("macro", None, maxp, [f"{which} regime UNKNOWN (insufficient data)"])
    return Component("macro", pts * maxp, maxp, [f"{which} regime {regime}"])


def catalyst_component(
    symbol: str, catalysts: list[dict], as_of: pd.Timestamp, maxp: float
) -> Component:
    active = []
    for c in catalysts:
        if str(c.get("symbol", "")).upper() != symbol:
            continue
        start, end = pd.Timestamp(c["date"], tz="UTC"), pd.Timestamp(c["expires"], tz="UTC")
        if start <= as_of <= end + pd.Timedelta(days=1):
            active.append(c)
    if not active:
        return Component("catalyst", None, maxp, ["no catalysts recorded (config/catalysts.yaml)"])
    net = sum(float(c["strength"]) * (1 if c["impact"] == "positive" else -1) for c in active)
    reasons = [
        f"{'+' if c['impact'] == 'positive' else '−'}{c['strength']} {c['description'][:110]}"
        for c in active
    ]
    return Component("catalyst", float(np.clip(net, 0, maxp)), maxp, reasons)


def liquidity_component(feat: pd.DataFrame, min_dv: float, maxp: float) -> Component:
    last = feat.iloc[-1]
    dv, vp = _f(last["dollar_vol_20"]), _f(last["rvol_pct"])
    reasons, pts, avail = [], 0.0, 0.0
    if dv is not None:
        avail += 3
        s = min(dv / min_dv, 1.0) * 3
        pts += s
        reasons.append(f"20d avg traded value ${dv / 1e6:,.0f}M (threshold ${min_dv / 1e6:,.0f}M)")
    if vp is not None:
        avail += 2
        s = 2.0 if vp < 0.8 else max(0.0, 2 * (1 - (vp - 0.8) / 0.2))
        pts += s
        reasons.append(f"realised-vol percentile {vp:.0%}")
    if avail == 0:
        return Component("liquidity", None, maxp, ["no volume/volatility data"])
    return Component("liquidity", maxp * pts / avail, maxp, reasons)


def module_components(mod: ModuleResult, w: dict) -> list[Component]:
    fr = [
        f.explanation
        for f in mod.factors
        if f.role == "quality" and (f.score is not None or "unavailable" in f.explanation)
    ]
    q = Component("fundamental", None if mod.quality is None else mod.quality * w["fundamental"], w["fundamental"],
                  (fr or ["no fundamental inputs"]) + [f"note: {n}" for n in mod.notes])  # fmt: skip
    val_reasons = [f.explanation for f in mod.factors if f.role == "valuation"]
    v = Component("valuation", None if mod.valuation is None else mod.valuation * w["valuation"], w["valuation"],
                  val_reasons or [f"no valuation model for this asset ({mod.module})"])  # fmt: skip
    return [q, v]


# --------------------------------------------------------------------------- zones


def price_zones(
    feat: pd.DataFrame, st: SetupState, mod: ModuleResult, fund: pd.DataFrame | None
) -> dict[str, Any]:
    last = feat.iloc[-1]
    close, atr = float(last["close"]), _f(last["atr_14"])
    supports = {}
    for k, lab in (
        ("sma_50", "50DMA"),
        ("sma_100", "100DMA"),
        ("sma_200", "200DMA"),
        ("swing_low_20", "20-bar swing low"),
    ):
        v = _f(last[k])
        if v is not None and v < close:
            supports[lab] = v
    if st.ideal_entry and st.setup == "breakout_retest":
        supports["breakout level"] = st.ideal_entry
    z: dict[str, Any] = {"current": close, "supports": dict(sorted(supports.items(), key=lambda kv: -kv[1])),
                         "fair": None, "accumulate": None, "strong_buy": None, "zone_basis": None}  # fmt: skip
    if "valuation" in mod.extra:  # HYPE yield bands
        zs = mod.extra["valuation"].zones()
        z.update(
            fair=zs["fair"],
            accumulate=zs["accumulate"],
            strong_buy=zs["strong_buy"],
            zone_basis="HYPE buyback-yield bands",
        )
    elif fund is not None and "pe" in fund and fund["pe"].notna().sum() > 252:
        pe_hist = fund["pe"].dropna().iloc[-5 * 252 :]
        pe_now = _f(fund["pe"].iloc[-1])
        if pe_now:
            eps_px = close / pe_now  # price per unit of P/E at today's earnings
            q = pe_hist.quantile([0.2, 0.4, 0.6]).to_numpy() * eps_px
            z.update(
                strong_buy=(None, q[0]),
                accumulate=(q[0], q[1]),
                fair=(q[1], q[2]),
                zone_basis="own 5y P/E percentiles × current TTM earnings",
            )
    if st.entry_zone:
        z["entry_zone"] = st.entry_zone
        z["ideal_entry"] = st.ideal_entry
    elif atr and _f(last["sma_50"]):
        z["entry_zone"] = (float(last["sma_50"]) - atr, float(last["sma_50"]))
        z["ideal_entry"] = float(last["sma_50"])
    ideal = z.get("ideal_entry")
    z["distance_to_ideal"] = (close / ideal - 1) if ideal else None
    z["invalidation"] = (
        st.stop
        if st.stop
        else (float(last["sma_200"]) - (atr or 0) if _f(last["sma_200"]) else None)
    )
    z["invalidation_basis"] = (
        "setup stop" if st.stop else "thesis review below 200DMA − 1 ATR (investment)"
    )
    return z


# --------------------------------------------------------------------------- assessment


class Scanner:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self.fs = FeatureStore(store, settings)
        self.cfg = settings.yaml("scoring.yaml")
        self.catalysts = (
            (settings.yaml("catalysts.yaml").get("catalysts") or [])
            if (settings.paths.config / "catalysts.yaml").exists()
            else []
        )
        self.regimes: RegimeBundle = build_regimes(store, settings, self.fs)
        self.verdicts = _latest_research_verdicts(store)

    def regime_for(self, asset: Asset, t: pd.Timestamp) -> tuple[str, str]:
        which = self.regimes.policy["governing"].get(asset.asset_class.value, "macro")
        reg = self.regimes.for_class(asset.asset_class.value, pd.DatetimeIndex([t])).iloc[0]
        return str(reg), which

    def module_for(
        self, asset: Asset, feat: pd.DataFrame, fund: pd.DataFrame | None, t: pd.Timestamp
    ) -> ModuleResult:
        cls = asset.asset_class.value
        if cls == "equity":
            return equity_module(fund.iloc[-1] if fund is not None else None, "bank" in asset.tags)
        if cls == "commodity":
            return commodity_module(self.store, asset.tags, t)
        if cls == "crypto":
            return (
                hype_module(self.store, self.settings)
                if asset.symbol == "HYPE"
                else crypto_revenue_module(self.store, asset.symbol)
            )
        return ModuleResult(
            "etf", None, None, notes=["no free point-in-time ETF fundamentals: N/A"]
        )

    def assess(self, asset: Asset) -> Assessment | None:
        feat = self.fs(asset.symbol)
        if feat is None or len(feat) < 60:
            return None
        w = self.cfg["weights"]
        last = feat.iloc[-1]
        t = pd.Timestamp(last["close_time"])
        warnings: list[str] = []
        fund = None
        if asset.asset_class.value == "equity":
            from market_signal.fundamentals.equity import pit_fundamental_features

            fund = pit_fundamental_features(self.store, self.settings, asset, feat)
        mod = self.module_for(asset, feat, fund, t)
        regime, which = self.regime_for(asset, t)
        ctx = SetupContext(asset.symbol, asset.asset_class.value, fundamentals=fund)
        if asset.symbol == "HYPE" and "valuation" in mod.extra:
            ctx.extra["current_fundamentals"] = {
                "buyback_yield": mod.extra["valuation"].get("buyback_yield")
            }
        states = setup_states(feat, asset, ctx, self.verdicts)
        # investments (Setup C) enter on valuation, not a technical level
        val_zones = price_zones(feat, SetupState("", "", "", "NONE", ""), mod, fund)
        for st in states:
            if st.setup == "rerating" and st.entry_zone is None:
                # valuation entry: anywhere at or below the accumulate ceiling (cheaper is not worse)
                acc, strong = val_zones.get("accumulate"), val_zones.get("strong_buy")
                ceiling = (acc[1] if acc and acc[1] else None) or (
                    strong[1] if strong and strong[1] else None
                )
                if ceiling:
                    st.entry_zone = (0.0, ceiling)
                    st.ideal_entry = ceiling
        order = {"ACTIVE": 0, "WAIT_RETEST": 1, "NEAR": 2, "NONE": 3, "N/A": 4}
        best = sorted(states, key=lambda s: order[s.state])[0]
        h4 = self.fs(asset.symbol, Timeframe.H4) if asset.series_for(Timeframe.H4) else None
        weekly = weekly_from_daily(
            feat[["ts", "close_time", "open", "high", "low", "close", "volume"]], asset.calendar
        )
        comps = module_components(mod, w)
        comps.append(structure_component(feat, weekly, w["structure"]))
        entry, in_zone = entry_component(feat, h4, best, self.cfg["entry"], w["entry"])
        comps.append(entry)
        comps.append(macro_component(regime, w["macro"], which))
        comps.append(catalyst_component(asset.symbol, self.catalysts, t, w["catalyst"]))
        comps.append(
            liquidity_component(
                feat,
                float(self.cfg["liquidity"]["min_dollar_volume"][asset.asset_class.value]),
                w["liquidity"],
            )
        )
        avail = [c for c in comps if c.available]
        max_avail = sum(c.max_points for c in avail)
        score = 100 * sum(c.points for c in avail) / max_avail if max_avail else None
        coverage = max_avail / sum(w.values())
        adj = float(self.regimes.policy["min_score_adjustment"].get(regime, 5))
        eff = None if score is None else score - adj
        bands = self.cfg["bands"]
        band = "IGNORE"
        for name in ("watch", "actionable", "strong", "exceptional"):
            if eff is not None and eff >= bands[name]:
                band = name.upper()
        status, text = self._status(band, coverage, best, in_zone, feat, warnings)
        zones = price_zones(feat, best, mod, fund)
        sizing = None
        if status in ("ACTIONABLE", "STRONG", "EXCEPTIONAL") or (
            status == "WAIT" and band != "WATCH"
        ):
            tier = (
                band.lower()
                if band.lower() in ("actionable", "strong", "exceptional")
                else "actionable"
            )
            if best.research_verdict not in ("PROMISING", "WEAK_POSITIVE") and tier != "actionable":
                warnings.append(
                    f"sizing capped at standard tier: {best.title} has no demonstrated edge yet"
                )
                tier = "actionable"
            mult = float(self.regimes.policy["risk_multiplier"].get(regime, 0.5))
            entry_px = (
                min(float(last["close"]), zones["entry_zone"][1])
                if zones.get("entry_zone")
                else float(last["close"])
            )
            s = suggest_size(best.kind, tier, mult, entry_px, best.stop, self.cfg["risk"])
            if s is not None:
                sizing = asdict(s) | {"entry_price": entry_px}
                if best.kind == "INVESTMENT":
                    sizing["exit_rules"] = INVESTMENT_EXITS
        if best.research_verdict in (None, "REJECT", "INSUFFICIENT_DATA", "INCONCLUSIVE"):
            warnings.append(
                f"{best.title}: research verdict {best.research_verdict or 'not run on real data'} — treat signals as unproven"
            )
        if coverage < self.cfg["min_coverage_for_actionable"]:
            warnings.append(f"low data coverage ({coverage:.0%} of score weight)")
        factors = [asdict(f) for f in mod.factors]
        return Assessment(
            asset.symbol, asset.name, asset.asset_class.value, str(t), float(last["close"]), regime, best, states, comps,
            score, coverage, band, status, text, zones, sizing, mod.module, mod.notes, factors, warnings,
        )  # fmt: skip

    def _status(
        self, band: str, coverage: float, best: SetupState, in_zone: bool, feat, warnings
    ) -> tuple[str, str]:
        if band == "IGNORE":
            return "IGNORE", "score below watch threshold"
        if band == "WATCH":
            return "WATCH", "watchlist"
        min_cov = float(self.cfg["min_coverage_for_actionable"])
        if best.research_verdict in ("PROMISING", "WEAK_POSITIVE"):
            min_cov = float(self.cfg.get("technical_only_min_coverage", min_cov))
        if coverage < min_cov:
            return "WATCH", f"capped at WATCH: data coverage {coverage:.0%} < {min_cov:.0%}"
        if self.cfg["setup_required_for_actionable"] and best.state in ("NONE", "N/A"):
            return "WATCH", "capped at WATCH: no active setup"
        if best.state == "ACTIVE" and in_zone:
            return band, f"{best.title}: in entry zone"
        if best.entry_zone:
            return (
                "WAIT",
                f"wait ≤ {best.entry_zone[1]:,.4g} ({best.title}, {best.state.lower().replace('_', ' ')})",
            )
        return "WATCH", f"{best.title} {best.state.lower()} but no entry zone"


@dataclass
class ScanResult:
    scan_id: str
    as_of: str
    regimes: dict[str, Any]
    assessments: list[Assessment]
    headline: str


def regime_summary(b: RegimeBundle) -> dict[str, Any]:
    out = {}
    for name, frame in (("crypto", b.crypto), ("macro", b.macro)):
        if frame is None or frame.empty:
            out[name] = {"regime": Regime.UNKNOWN.value, "detail": "no data"}
            continue
        r = frame.iloc[-1]
        votes = {
            k.removeprefix("vote_"): (None if pd.isna(v) else float(v))
            for k, v in r.items()
            if str(k).startswith("vote_")
        }
        out[name] = {
            "regime": r["regime"],
            "score": _f(r["score"]),
            "coverage": _f(r["coverage"]),
            "as_of": str(frame.index[-1]),
            "votes": votes,
        }
    return out


def run_scan(store: Store, settings: Settings, persist: bool = True) -> ScanResult:
    sc = Scanner(store, settings)
    assessments = [a for a in (sc.assess(x) for x in settings.active_assets()) if a is not None]
    rank = {"EXCEPTIONAL": 0, "STRONG": 1, "ACTIONABLE": 2, "WAIT": 3, "WATCH": 4, "IGNORE": 5}
    assessments.sort(key=lambda a: (rank[a.status], -(a.score or 0)))
    actionable = [a for a in assessments if a.status in ("ACTIONABLE", "STRONG", "EXCEPTIONAL")]
    if actionable:
        headline = f"{len(actionable)} actionable: " + ", ".join(
            f"{a.symbol} {a.score:.0f}" for a in actionable[:5]
        )
    elif assessments:
        b = max(assessments, key=lambda a: a.score or 0)
        headline = f"No actionable opportunities. Best candidate: {b.symbol} {b.score:.0f}/100 — {b.status.lower()} ({b.status_text})."
    else:
        headline = "No data: run `market update` first."
    res = ScanResult(
        new_id("scan_"),
        utcnow().isoformat(timespec="seconds"),
        regime_summary(sc.regimes),
        assessments,
        headline,
    )
    if persist:
        persist_scan(store, settings, res)
    return res


def persist_scan(store: Store, settings: Settings, res: ScanResult) -> None:
    cfg_hash = config_hash(
        {k: settings.yaml(k) for k in ("scoring.yaml", "regimes.yaml", "hype.yaml")}
    )
    store.con.execute(
        "INSERT INTO scan_runs VALUES (?,?,?,?,?,?,?)",
        [res.scan_id, pd.Timestamp(res.as_of), utcnow(), cfg_hash, None, json.dumps(res.regimes, default=_json_default),
         json.dumps({"headline": res.headline}, default=_json_default)],
    )  # fmt: skip
    for a in res.assessments:
        store.con.execute(
            "INSERT INTO scan_results VALUES (?,?,?,?,?,?,?,?)",
            [
                res.scan_id,
                a.symbol,
                a.setup.setup,
                a.score,
                a.coverage,
                a.status,
                a.price,
                json.dumps(a.to_json()),
            ],
        )
