"""Declarative research experiments.

An experiment YAML (config/experiments/*.yaml) names a setup (or a raw ``conditions``
block), a universe, parameters, and robustness settings. ``run_experiment`` executes:

  signals → event study (+ baselines, random-entry null) → trade simulation →
  regime / trend / volatility / rate-cycle splits → walk-forward → sensitivity →
  automatic verdict → report files + ``research_runs`` provenance row.

Nothing here tunes parameters to maximise returns: the walk-forward calibrates at most
two parameters on training data only, and the default parameters are always reported
alongside.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from market_signal.backtest.engine import AssetInput, SimResult, Sizing, simulate
from market_signal.backtest.events import (
    AssetEvents,
    EventStudyResult,
    baseline_bars,
    costs_for,
    forward_returns,
    run_event_study,
)
from market_signal.backtest.robustness import (
    label_asof,
    make_folds,
    plateau_verdict,
    sensitivity_grid,
    split_by_label,
    walk_forward,
)
from market_signal.config import Settings, config_hash, load_yaml
from market_signal.data.pit import load_macro
from market_signal.data.registry import source_label
from market_signal.data.store import Store, new_id
from market_signal.features import FeatureStore
from market_signal.models.domain import Asset, Calendar, Timeframe, utcnow
from market_signal.regimes.engine import RegimeBundle, build_regimes, derived_macro_rows
from market_signal.setups.base import Setup, SetupContext, SetupOutput, get_setup

GROUPS = {
    "crypto": lambda a: a.asset_class.value == "crypto",
    "equities": lambda a: a.asset_class.value == "equity",
    "etfs": lambda a: a.asset_class.value == "etf",
    "commodities": lambda a: a.asset_class.value == "commodity",
    "all": lambda a: True,
}


@dataclass
class Experiment:
    name: str
    setup: str
    description: str = ""
    universe: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    params_by_class: dict[str, dict[str, Any]] = field(default_factory=dict)
    start: str | None = None
    end: str | None = None
    primary_horizon: str | None = None
    trade: dict[str, Any] = field(default_factory=dict)
    walk_forward: dict[str, Any] = field(default_factory=dict)
    sensitivity: dict[str, Any] = field(default_factory=dict)
    regime_filter: list[str] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def load_experiment(settings: Settings, ref: str) -> Experiment:
    path = Path(ref)
    if not path.exists():
        path = settings.paths.config / "experiments" / f"{ref.replace('-', '_')}.yaml"
    raw = load_yaml(path)
    universe: list[str] = []
    for item in raw.get("universe") or ["all"]:
        if item in GROUPS:
            universe += [a.symbol for a in settings.active_assets() if GROUPS[item](a)]
        else:
            settings.asset(item)
            universe.append(item.upper())
    params = dict(raw.get("params") or {})
    if raw.get("conditions"):
        params["conditions"] = raw["conditions"]
    return Experiment(
        name=raw.get("name", path.stem),
        setup=raw.get("setup", "conditions" if raw.get("conditions") else path.stem),
        description=raw.get("description", ""),
        universe=list(dict.fromkeys(universe)),
        params=params,
        params_by_class=raw.get("params_by_class") or {},
        start=str(raw["period"]["start"]) if raw.get("period", {}).get("start") else None,
        end=str(raw["period"]["end"]) if raw.get("period", {}).get("end") else None,
        primary_horizon=raw.get("primary_horizon"),
        trade=raw.get("trade") or {},
        walk_forward=raw.get("walk_forward") or {},
        sensitivity=raw.get("sensitivity") or {},
        regime_filter=(raw.get("regime_filter") or {}).get("allow"),
        raw=raw,
    )


class ResearchContext:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.fs = FeatureStore(store, settings)
        self.bt = settings.yaml("backtest.yaml")
        self._regimes: RegimeBundle | None = None
        self._labels: dict[str, pd.Series] = {}
        self._fund_cache: dict[str, pd.DataFrame | None] = {}

    @property
    def regimes(self) -> RegimeBundle:
        if self._regimes is None:
            self._regimes = build_regimes(self.store, self.settings, self.fs)
        return self._regimes

    def horizons(self, asset: Asset) -> dict[str, int]:
        key = "crypto" if asset.calendar == Calendar.CRYPTO_24_7 else "nyse"
        return {k: int(v) for k, v in self.bt["horizons"][key].items()}

    def fundamentals(self, asset: Asset, feat: pd.DataFrame) -> pd.DataFrame | None:
        if asset.symbol not in self._fund_cache:
            try:
                from market_signal.fundamentals.equity import pit_fundamental_features
            except ModuleNotFoundError:
                self._fund_cache[asset.symbol] = None
            else:
                self._fund_cache[asset.symbol] = pit_fundamental_features(
                    self.store, self.settings, asset, feat
                )
        return self._fund_cache[asset.symbol]

    def label_series(self, name: str) -> pd.Series:
        """Market-state labels indexed by UTC time (for splits)."""
        if name in self._labels:
            return self._labels[name]
        out = pd.Series(dtype=object)
        if name in ("trend_crypto", "trend_equity"):
            feat = self.fs("BTC" if name == "trend_crypto" else "SPY")
            if feat is not None:
                lab = np.where(
                    feat["sma_200"].isna(),
                    "UNKNOWN",
                    np.where(feat["close"] > feat["sma_200"], "bull", "bear"),
                )
                out = pd.Series(lab, index=pd.DatetimeIndex(feat["close_time"]))
        elif name == "rates":
            rows = load_macro(self.store, "DFF", research=True)
            if not rows.empty:
                d = derived_macro_rows(rows, "diff", 126)
                lab = np.where(d["value"].isna(), "UNKNOWN", np.where(d["value"] > 0.25, "hiking",
                               np.where(d["value"] < -0.25, "cutting", "on_hold")))  # fmt: skip
                out = pd.Series(lab, index=pd.DatetimeIndex(d["available_at"])).sort_index()
        self._labels[name] = out
        return out


def _params(
    setup: Setup, exp: Experiment, asset: Asset, overrides: dict[str, Any]
) -> dict[str, Any]:
    cls = asset.asset_class.value
    return {
        **setup.default_params(cls),
        **exp.params,
        **exp.params_by_class.get(cls, {}),
        **overrides,
    }


@dataclass
class AssetRun:
    asset: Asset
    feat: pd.DataFrame
    out: SetupOutput
    params: dict[str, Any]
    in_period: pd.Series


def evaluate_universe(
    ctx: ResearchContext, exp: Experiment, overrides: dict[str, Any] | None = None
) -> list[AssetRun]:
    setup = get_setup(exp.setup)
    runs = []
    start = pd.Timestamp(exp.start, tz="UTC") if exp.start else None
    end = pd.Timestamp(exp.end, tz="UTC") if exp.end else None
    for sym in exp.universe:
        asset = ctx.settings.asset(sym)
        feat = ctx.fs(sym)
        if feat is None or len(feat) < 50:
            continue
        params = _params(setup, exp, asset, overrides or {})
        sctx = SetupContext(sym, asset.asset_class.value)
        if (
            getattr(setup, "needs_fundamentals", False)
            or params.get("min_fundamental_score") is not None
        ):
            sctx.fundamentals = ctx.fundamentals(asset, feat)
        regime = ctx.regimes.for_class(asset.asset_class.value, feat["close_time"])
        sctx.regime = pd.Series(regime.to_numpy(), index=feat.index)
        out = setup.evaluate(feat, params, sctx)
        in_period = pd.Series(True, index=feat.index)
        if start is not None:
            in_period &= feat["close_time"] >= start
        if end is not None:
            in_period &= feat["close_time"] <= end
        sig = out.signal & in_period
        if exp.regime_filter:
            sig &= sctx.regime.isin(exp.regime_filter)
        out = SetupOutput(sig, out.stop, out.eligible & in_period, out.diagnostics)
        runs.append(AssetRun(asset, feat, out, params, in_period))
    return runs


def _asset_events(ctx: ResearchContext, runs: list[AssetRun]) -> list[AssetEvents]:
    evs = []
    for r in runs:
        costs = costs_for(ctx.bt, r.asset.symbol, r.asset.asset_class.value)
        hz = ctx.horizons(r.asset)
        fwd = forward_returns(r.feat, hz, costs)
        evs.append(
            AssetEvents(
                r.asset.symbol,
                r.asset.asset_class.value,
                r.feat,
                fwd,
                r.out.signal,
                r.out.eligible,
                hz,
            )
        )
    return evs


def _label_events(ctx: ResearchContext, events: pd.DataFrame, runs: list[AssetRun]) -> pd.DataFrame:
    if events.empty:
        return events
    ev = events.copy()
    by_sym = {r.asset.symbol: r for r in runs}
    ev["regime"] = "UNKNOWN"
    ev["trend"] = "UNKNOWN"
    ev["vol"] = "UNKNOWN"
    for sym, idx in ev.groupby("symbol").groups.items():
        r = by_sym[sym]
        bars = ev.loc[idx, "bar"].to_numpy()
        reg = ctx.regimes.for_class(r.asset.asset_class.value, r.feat["close_time"])
        ev.loc[idx, "regime"] = reg.to_numpy()[bars]
        trend_name = "trend_crypto" if r.asset.is_crypto else "trend_equity"
        ev.loc[idx, "trend"] = label_asof(
            ev.loc[idx, "signal_time"], ctx.label_series(trend_name)
        ).to_numpy()
        rv = r.feat["rvol_pct"].to_numpy()[bars]
        ev.loc[idx, "vol"] = np.where(
            np.isnan(rv), "UNKNOWN", np.where(rv >= 0.5, "high_vol", "low_vol")
        )
    ev["rates"] = label_asof(ev["signal_time"], ctx.label_series("rates")).to_numpy()
    return ev


def run_simulation(ctx: ResearchContext, exp: Experiment, runs: list[AssetRun]) -> SimResult:
    setup = get_setup(exp.setup)
    pcfg = ctx.bt["portfolio"]
    t = exp.trade
    sizing = Sizing(
        method=t.get("sizing", "risk" if setup.kind == "TRADE" else "fixed"),
        risk_per_trade=float(t.get("risk_per_trade", pcfg["risk_per_trade"])),
        fixed_fraction=float(t.get("fixed_fraction", 0.10)),
        max_position_fraction=float(t.get("max_position_fraction", pcfg["max_position_fraction"])),
        max_gross_exposure=float(pcfg["max_gross_exposure"]),
    )
    policy = ctx.regimes.policy["risk_multiplier"]
    inputs = []
    rules = None
    for r in runs:
        rules = setup.exit_rules(r.params)
        for k in ("max_hold_bars", "target_r", "trend_exit_sma"):
            if k in t:
                setattr(rules, k, t[k])
        regime = ctx.regimes.for_class(r.asset.asset_class.value, r.feat["close_time"])
        rmult = (
            pd.Series(regime.map(policy).fillna(0.5).to_numpy(), index=r.feat.index)
            if t.get("regime_sizing", True)
            else None
        )
        inputs.append(AssetInput(
            r.asset.symbol, r.asset.asset_class.value, r.feat, r.out.signal, r.out.stop,
            costs_for(ctx.bt, r.asset.symbol, r.asset.asset_class.value), rmult, setup.name, setup.kind,
        ))  # fmt: skip
    if not inputs:
        return SimResult(
            pd.DataFrame(),
            pd.Series(dtype=float),
            pd.Series(dtype=float),
            pd.DataFrame(),
            {"trades": 0},
        )
    # per-asset rules are identical in V1 (params drive holding periods by class); use the last
    return simulate(inputs, rules, sizing, float(pcfg["initial_equity"]))


@dataclass
class ResearchReport:
    experiment: Experiment
    run_id: str
    created_at: str
    study: EventStudyResult
    events: pd.DataFrame
    sim: SimResult
    splits: dict[str, pd.DataFrame]
    wf_table: pd.DataFrame | None
    wf_summary: dict | None
    sens_table: pd.DataFrame | None
    sens_verdict: dict | None
    verdict: dict
    provenance: dict
    period: tuple[str, str]


def _gap_by_symbol(ctx: ResearchContext, runs: list[AssetRun], horizon: str) -> dict[str, int]:
    return {r.asset.symbol: ctx.horizons(r.asset)[horizon] for r in runs}


def _purge(ctx: ResearchContext, runs: list[AssetRun], horizon: str) -> pd.Timedelta:
    days = 0.0
    for r in runs:
        bars = ctx.horizons(r.asset)[horizon]
        days = max(days, bars * (1.0 if r.asset.is_crypto else 7 / 5) + 5)
    return pd.Timedelta(days=days)


def automatic_verdict(
    summary_row: pd.Series, wf: dict | None, sens: dict | None, asset_share: float | None, cfg: dict
) -> dict:
    """Explicit, documented criteria (docs/BACKTESTING.md). No discretion."""
    min_n = int(cfg["statistics"]["min_events_for_conclusion"])
    reasons = []
    n = int(summary_row.get("n_independent", 0))
    ex = summary_row.get("excess_mean_indep", np.nan)
    p = summary_row.get("p_value_random_entry", np.nan)
    if n < min_n:
        return {
            "verdict": "INSUFFICIENT_DATA",
            "reasons": [f"only {n} independent events (< {min_n})"],
        }
    if not np.isfinite(ex) or ex <= 0:
        reasons.append(f"non-positive excess vs baseline ({ex:+.2%})")
    if not np.isfinite(p) or p >= 0.10:
        reasons.append(f"not distinguishable from random entry (p={p:.2f})")
    if wf:
        if wf.get("oos_default_n", 0) >= min_n and not (
            wf.get("oos_default_excess_mean", np.nan) > 0
        ):
            reasons.append("default parameters show no out-of-sample excess in walk-forward")
        if (
            wf.get("folds_with_events")
            and wf.get("folds_positive", 0) / wf["folds_with_events"] < 0.5
        ):
            reasons.append(
                f"positive in only {wf['folds_positive']}/{wf['folds_with_events']} walk-forward test folds"
            )
    if sens and sens.get("verdict") == "FRAGILE":
        reasons.append("parameter-fragile (neighbouring parameters fail)")
    if asset_share is not None and asset_share < 0.5:
        reasons.append(f"positive on only {asset_share:.0%} of assets")
    if reasons:
        rejected = any(
            r.startswith(("non-positive", "not distinguishable", "default parameters"))
            for r in reasons
        )
        return {"verdict": "REJECT" if rejected else "INCONCLUSIVE", "reasons": reasons}
    strong = p < 0.05 and (sens or {}).get("verdict") == "PLATEAU"
    return {"verdict": "PROMISING" if strong else "WEAK_POSITIVE",
            "reasons": ["passes all criteria" if strong else "positive but p>=0.05 or no clear plateau"]}  # fmt: skip


def _git_commit(root: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def run_experiment(
    ctx: ResearchContext,
    exp: Experiment,
    *,
    walk_forward_on: bool = True,
    sensitivity_on: bool = True,
) -> ResearchReport:
    cfg = ctx.bt
    primary = exp.primary_horizon or cfg["primary_horizon"]
    stats_cfg = cfg["statistics"]
    runs = evaluate_universe(ctx, exp)
    if not runs:
        raise RuntimeError(f"{exp.name}: no assets with data in universe {exp.universe}")
    aevs = _asset_events(ctx, runs)
    study = run_event_study(
        aevs,
        primary,
        int(stats_cfg["bootstrap_samples"]),
        int(stats_cfg["seed"]),
        int(stats_cfg["min_events_for_conclusion"]),
    )
    events = _label_events(ctx, study.events, runs)
    splits = (
        {
            k: split_by_label(events, k, primary)
            for k in ("regime", "trend", "vol", "rates", "asset_class")
        }
        if not events.empty
        else {}
    )
    sim = run_simulation(ctx, exp, runs)
    base = baseline_bars(aevs)
    gap = _gap_by_symbol(ctx, runs, primary)

    def events_for(overrides: dict[str, Any]) -> pd.DataFrame:
        rr = evaluate_universe(ctx, exp, overrides)
        return run_event_study(_asset_events(ctx, rr), primary, n_boot=0).events

    setup = get_setup(exp.setup)
    default_params = {
        k: v for k, v in setup.default_params(runs[0].asset.asset_class.value).items()
    }
    wf_table = wf_summary = sens_table = sens_verdict = None
    period_start = base["signal_time"].min() if not base.empty else None
    period_end = base["signal_time"].max() if not base.empty else None
    if walk_forward_on and period_start is not None:
        wcfg = {**cfg["walk_forward"], **exp.walk_forward}
        grid_axes = wcfg.get("grid") or {}
        names = list(grid_axes)
        grid = (
            [
                dict(zip(names, combo, strict=True))
                for combo in pd.MultiIndex.from_product(list(grid_axes.values())).tolist()
            ]
            if names
            else [{}]
        )
        folds = make_folds(
            period_start.normalize(),
            period_end,
            float(wcfg["train_years"]),
            float(wcfg["test_years"]),
            bool(wcfg["anchored"]),
        )
        default_sel = {k: exp.params.get(k, default_params.get(k)) for k in names}
        wf_table, wf_summary = walk_forward(
            events_for, base, [{**default_sel, **g} for g in grid] if names else [{}], default_sel if names else {},
            folds, primary, gap, _purge(ctx, runs, primary), int(wcfg["min_train_events"]), wcfg["objective"],
        )  # fmt: skip
    if sensitivity_on and exp.sensitivity.get("axes"):
        axes = exp.sensitivity["axes"]
        centre = {k: exp.params.get(k, default_params.get(k)) for k in axes}
        sens_table = sensitivity_grid(events_for, centre, axes, primary, base, gap)
        sc = cfg["sensitivity"]
        sens_verdict = plateau_verdict(sens_table, centre, axes, int(stats_cfg["min_events_for_conclusion"]),
                                       float(sc["plateau_min_neighbour_ratio"]), float(sc["fragile_if_neighbours_fail"]))  # fmt: skip
    prim_row = (
        study.summary.set_index("horizon").loc[primary]
        if not study.summary.empty
        else pd.Series(dtype=float)
    )
    asset_share = None
    if not study.by_asset.empty:
        pa = study.by_asset[
            (study.by_asset["horizon"] == primary) & (study.by_asset["n_independent"] >= 5)
        ]
        asset_share = float((pa["excess_mean_indep"] > 0).mean()) if len(pa) else None
    verdict = automatic_verdict(prim_row, wf_summary, sens_verdict, asset_share, cfg)
    provenance = {
        "config_hash": config_hash({"experiment": exp.raw, "backtest": cfg}),
        "git_commit": _git_commit(ctx.settings.paths.root),
        "data": [
            ctx.store.series_fingerprint(r.asset.symbol, Timeframe.D1, _source(r.asset))
            for r in runs
        ],
        "code_version": _version(),
    }
    return ResearchReport(
        exp, new_id("rr_"), utcnow().isoformat(timespec="seconds"), study, events, sim, splits,
        wf_table, wf_summary, sens_table, sens_verdict, verdict, provenance,
        (str(period_start)[:10], str(period_end)[:10]),
    )  # fmt: skip


def _source(asset: Asset) -> str:
    import os

    return os.environ.get("PRISM_SOURCE_OVERRIDE") or source_label(
        asset.series[Timeframe.D1].provider, Timeframe.D1
    )


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("prism-market-signal")
    except PackageNotFoundError:
        return "dev"


def save_report(ctx: ResearchContext, rep: ResearchReport) -> Path:
    from market_signal.research.report import render_markdown

    out = (
        ctx.settings.paths.results
        / rep.experiment.name
        / rep.created_at.replace(":", "").replace("-", "")[:15]
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(render_markdown(rep))
    rep.events.to_csv(out / "events.csv", index=False)
    if not rep.sim.trades.empty:
        rep.sim.trades.to_csv(out / "trades.csv", index=False)
    rep.sim.equity.rename("equity").to_csv(out / "equity.csv")
    summary = {
        "name": rep.experiment.name, "setup": rep.experiment.setup, "verdict": rep.verdict,
        "period": rep.period, "summary": rep.study.summary.replace({np.nan: None}).to_dict("records"),
        "simulation": {k: v for k, v in rep.sim.metrics.items()},
        "walk_forward": rep.wf_summary, "sensitivity": rep.sens_verdict, "provenance": rep.provenance,
    }  # fmt: skip
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    ctx.store.con.execute(
        "INSERT INTO research_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [rep.run_id, rep.experiment.name, "experiment", utcnow(), json.dumps(rep.experiment.raw, default=str),
         rep.provenance["config_hash"], json.dumps(rep.provenance["data"], default=str), rep.provenance["code_version"],
         rep.provenance["git_commit"], json.dumps({"verdict": rep.verdict}, default=str), str(out)],
    )  # fmt: skip
    return out
