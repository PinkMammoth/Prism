"""Fast daily screen: cheap, deterministic triage of compiled Lab signals.

Answers "how often, what happens next, versus random eligible entry on the same asset and
side, across assets and horizons". It does NOT validate, size, simulate portfolios,
correct for multiple testing or promote anything. ``INTERESTING`` means "candidate for
Prism's full research", nothing more.

Semantics (all frozen by a v2 ``ScreenPlan``):
- signal at bar T's close (Phase 3 compiler), restricted to the role period;
- entry at bar T+1's open, exit at bar T+h's close; both bars must exist inside the
  retained snapshot, which ends at the role period end, so outcomes never use data after
  the period and a signal on the last bars is not evaluable;
- perp returns: ``perps.backtest.side_forward_returns`` (notional, side-signed, minus
  2 x (fee + slippage), minus funding paid over bars T+1..T+h; missing funding makes the
  outcome missing). Spot returns: ``backtest.events.forward_returns`` on the total-return
  basis (entry x (1 + c), exit x (1 - c)). Gross = the same price move without costs or
  funding;
- baseline, independent events (greedy horizon-gap declustering) and the random-entry
  p-value are Prism's ``run_event_study`` on eligible in-period bars of the same asset and
  side.

``screen`` is a pure developer function. ``run_screen`` is the governed path: it requires
a preregistered attempt, records its start, and always attaches one terminal result.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from market_signal.backtest.events import AssetEvents, Costs, forward_returns, run_event_study
from market_signal.data.prices import PriceBasis, adjustment_factors, apply_basis
from market_signal.perps.backtest import PerpCosts, side_forward_returns
from market_signal.research.lab.compiler import (
    COMPILER_VERSION,
    CompileCache,
    CompiledStrategy,
    Snapshot,
    _calendar,
    _frame,
    compile_strategy,
)
from market_signal.research.lab.ledger import ErrorInfo, Ledger, LedgerError
from market_signal.research.lab.policy import DatasetRole, PValue, ScreenPlan
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lab.spec import StrategyDefinition
from market_signal.research.lab.vocabulary import VOCABULARY_VERSION

SCREEN_VERSION = "lab_fast_screen_v1"
TRIAGE_STATUS = {
    "NO_EVENTS": "insufficient_data",
    "INSUFFICIENT_EVENTS": "insufficient_data",
    "WEAK": "rejected",
    "INTERESTING": "succeeded",
    "ERROR": "errored",
}


class ScreenError(ValueError):
    pass


@dataclass
class ScreenWorkspace:
    """Reusable per-snapshot work for batches: compiled inputs/features and forward returns."""

    snapshot: Snapshot
    compile_cache: CompileCache = field(init=False)
    returns: dict = field(default_factory=dict)

    def __post_init__(self):
        self.compile_cache = CompileCache(self.snapshot)


@dataclass(frozen=True)
class ScreenResult:
    triage: str
    metrics: dict
    p_values: tuple[PValue, ...]
    events: pd.DataFrame  # every evaluable in-period event, with independence flags


# --------------------------------------------------------------------------- returns


def _spot_total_return(snapshot: Snapshot, symbol: str, plan: ScreenPlan) -> pd.DataFrame:
    """Total-return OHLC (split + dividends) for outcomes. Only ratios within a window are
    used, so the (backward) normalisation constant cannot affect a return."""
    found = snapshot.find("corporate_actions", symbol)
    acts = _frame(found[1], ["date", "split_factor", "dividend"])
    # compiled inputs are forward-split adjusted; outcomes use the retained raw rows
    rows = snapshot.find("bars", symbol)[1]
    raw = _frame(rows, ["ts", "close_time", "open", "high", "low", "close", "volume"])
    for col in ("open", "high", "low", "close", "volume"):
        raw[col] = pd.to_numeric(raw[col]).astype(float)
    factors = adjustment_factors(raw, acts, _calendar(plan.asset_class(symbol)))
    tr = apply_basis(raw, factors, PriceBasis.TOTAL_RETURN)
    return tr.rename(columns={c: f"tr_{c}" for c in ("open", "high", "low", "close")})


def _returns(
    ws: ScreenWorkspace, plan: ScreenPlan, symbol: str, side: str, compiled: CompiledStrategy
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(net forward-return frame, gross returns) per horizon, positional, cached."""
    horizons = {h.label: h.bars for h in plan.horizons}
    cost = next(c for c in plan.costs if c.symbol == symbol)
    key = (symbol, side, tuple(horizons.items()), cost.fee_bps, cost.slippage_bps)
    if key in ws.returns:
        return ws.returns[key]
    frame = compiled.inputs.reset_index(drop=True)
    gross = pd.DataFrame(index=frame.index)
    if plan.market == "perp":
        if "funding_day" not in frame:
            raise ScreenError(f"perp returns need the retained funding series for {symbol}")
        sign = 1 if side == "long" else -1
        net = side_forward_returns(
            frame, horizons, sign, PerpCosts(cost.fee_bps, cost.slippage_bps)
        )
        entry = frame["open"].shift(-1)
        for label, h in horizons.items():
            gross[label] = sign * (frame["close"].shift(-h) / entry - 1)
    else:
        tr = _spot_total_return(ws.snapshot, symbol, plan)
        if (
            len(tr) != len(frame)
            or not (tr["close_time"].values == frame["close_time"].values).all()
        ):
            raise ScreenError("total-return frame is misaligned with compiled inputs")
        net = forward_returns(tr, horizons, Costs(cost.fee_bps, cost.slippage_bps))
        entry = tr["tr_open"].shift(-1)
        for label, h in horizons.items():
            gross[label] = tr["tr_close"].shift(-h) / entry - 1
    ws.returns[key] = (net, gross)
    return net, gross


# --------------------------------------------------------------------------- metrics


def _num(x) -> float | None:
    x = float(x)
    return x if np.isfinite(x) else None


def _stats(net: pd.Series, gross: pd.Series, excess: pd.Series) -> dict:
    n = len(net)
    std = float(net.std(ddof=1)) if n > 1 else np.nan
    return {
        "independent_events": n,
        "gross_mean": _num(gross.mean()) if n else None,
        "gross_median": _num(gross.median()) if n else None,
        "net_mean": _num(net.mean()) if n else None,
        "net_median": _num(net.median()) if n else None,
        "hit_rate": _num((net > 0).mean()) if n else None,
        "net_std": _num(std),
        # descriptive per-event ratio on overlapping-free events; NOT an annualised Sharpe
        "net_mean_over_std": _num(net.mean() / std) if n > 1 and std > 0 else None,
        "excess_mean": _num(excess.mean()) if n else None,
        "excess_median": _num(excess.median()) if n else None,
        "worst_net": _num(net.min()) if n else None,
        "best_net": _num(net.max()) if n else None,
    }


def triage(primary: dict, plan: ScreenPlan) -> tuple[str, dict]:
    """Deterministic triage from the pooled primary-horizon row and the plan's gates."""
    gates = plan.gates
    n = primary["independent_events"]
    checks = {
        "min_independent_events": n >= plan.statistics.min_independent_events,
        "min_assets_with_events": primary["assets_with_events"] >= gates.min_assets_with_events,
        "min_pooled_excess": primary["excess_mean"] is not None
        and primary["excess_mean"] > gates.min_pooled_excess,
        "min_positive_asset_share": primary["positive_asset_share"] is not None
        and primary["positive_asset_share"] >= gates.min_positive_asset_share,
        "max_p_value": gates.max_p_value is None
        or (primary["p_value_random_entry"] is not None
            and primary["p_value_random_entry"] <= gates.max_p_value),
    }  # fmt: skip
    if n == 0:
        return "NO_EVENTS", checks
    if not (checks["min_independent_events"] and checks["min_assets_with_events"]):
        return "INSUFFICIENT_EVENTS", checks
    if checks["min_pooled_excess"] and checks["min_positive_asset_share"] and checks["max_p_value"]:
        return "INTERESTING", checks
    return "WEAK", checks


# --------------------------------------------------------------------------- screen


def screen(
    definition: StrategyDefinition,
    plan: ScreenPlan,
    snapshot: Snapshot,
    role: DatasetRole,
    assets: tuple[str, ...],
    *,
    workspace: ScreenWorkspace | None = None,
) -> ScreenResult:
    """Pure screen of one definition. Use ``run_screen`` for a Lab result."""
    if not isinstance(plan, ScreenPlan):
        raise ScreenError("screening needs a schema v2 ScreenPlan")
    if definition.market != plan.market:
        raise ScreenError("strategy market does not match the plan")
    ws = workspace or ScreenWorkspace(snapshot)
    if ws.snapshot is not snapshot:
        raise ScreenError("workspace belongs to a different snapshot")
    period = plan.period(role)
    side = definition.side
    horizons = {h.label: h.bars for h in plan.horizons}
    per_asset_meta, asset_events, gross_by = {}, [], {}
    for symbol in sorted(assets):
        compiled = compile_strategy(
            definition,
            snapshot,
            symbol,
            asset_class=plan.asset_class(symbol),
            cache=ws.compile_cache,
        )
        close = compiled.inputs["close_time"].reset_index(drop=True)
        window = ((close >= period.start) & (close < period.end)).to_numpy()
        eligible = compiled.eligible.to_numpy() & window
        if plan.market == "perp":  # perp_asset_events convention: funding known at T
            eligible &= compiled.inputs["funding_day"].notna().to_numpy()
        signal = compiled.signal.to_numpy() & window
        net, gross = _returns(ws, plan, symbol, side, compiled)
        name = f"{symbol}:{side}"
        gross_by[name] = gross
        feat = compiled.inputs.reset_index(drop=True)
        asset_events.append(
            AssetEvents(name, f"{plan.market}_{side}", feat, net,
                        pd.Series(signal), pd.Series(eligible), dict(horizons))
        )  # fmt: skip
        prewindow = int((close < period.start).sum())
        first_eligible = np.flatnonzero(eligible)
        per_asset_meta[symbol] = {
            "rows": len(close),
            "prewindow_bars": prewindow,
            "window_bars": int(window.sum()),
            "warmup_bars": compiled.metadata.warmup_bars,
            "warmup_shortfall_bars": max(compiled.metadata.warmup_bars - 1 - prewindow, 0),
            "first_eligible_close": close.iloc[first_eligible[0]] if len(first_eligible) else None,
            "eligible_bars": int(eligible.sum()),
            "raw_signals": int(signal.sum()),
            "compile_digest": compiled.metadata.digest,
        }

    stats = plan.statistics
    study = run_event_study(
        asset_events,
        plan.primary_horizon,
        n_boot=stats.random_entry_samples,
        seed=stats.seed,
        min_events=stats.min_independent_events,
    )
    events = study.events
    if not events.empty:
        events = events.copy()
        events["gross"] = [
            float(gross_by[s][h].iloc[b])
            for s, h, b in zip(events["symbol"], events["horizon"], events["bar"], strict=True)
        ]
    base = study.baseline.set_index(["symbol", "horizon"]) if not study.baseline.empty else None

    per_asset, aggregate = [], []
    for label in horizons:
        sub = events[events["horizon"] == label] if not events.empty else events
        asset_rows = []
        for symbol in sorted(assets):
            name = f"{symbol}:{side}"
            mine = sub[sub["symbol"] == name] if not sub.empty else sub
            ind = mine[mine["independent"]] if not mine.empty else mine
            empty = pd.Series(dtype=float)
            row = {
                "symbol": symbol,
                "horizon": label,
                "eligible_bars": per_asset_meta[symbol]["eligible_bars"],
                "raw_signals": per_asset_meta[symbol]["raw_signals"],
                "evaluable_events": len(mine),
                "baseline_n": int(base.loc[(name, label), "base_n"]) if base is not None else 0,
                "baseline_mean": _num(base.loc[(name, label), "base_mean"])
                if base is not None
                else None,
                **_stats(
                    ind["ret"] if len(ind) else empty,
                    ind["gross"] if len(ind) else empty,
                    ind["excess"] if len(ind) else empty,
                ),
            }
            asset_rows.append(row)
        per_asset.extend(asset_rows)
        ind = sub[sub["independent"]] if not sub.empty else sub
        empty = pd.Series(dtype=float)
        pooled = _stats(
            ind["ret"] if len(ind) else empty,
            ind["gross"] if len(ind) else empty,
            ind["excess"] if len(ind) else empty,
        )
        with_events = [r for r in asset_rows if r["independent_events"] > 0]
        excesses = np.array([r["excess_mean"] for r in with_events], dtype=float)
        counts = np.array([r["independent_events"] for r in with_events], dtype=float)
        summary = study.summary.set_index("horizon").loc[label]
        aggregate.append(
            {
                "horizon": label,
                **pooled,
                "raw_signals": sum(r["raw_signals"] for r in asset_rows),
                "evaluable_events": sum(r["evaluable_events"] for r in asset_rows),
                "assets": len(asset_rows),
                "assets_with_events": len(with_events),
                "assets_positive_excess": int((excesses > 0).sum()),
                "assets_negative_excess": int((excesses < 0).sum()),
                "positive_asset_share": _num((excesses > 0).mean()) if len(excesses) else None,
                "median_asset_excess": _num(np.median(excesses)) if len(excesses) else None,
                "min_asset_excess": _num(excesses.min()) if len(excesses) else None,
                "max_asset_excess": _num(excesses.max()) if len(excesses) else None,
                "asset_excess_std": _num(excesses.std(ddof=1)) if len(excesses) > 1 else None,
                "max_asset_event_share": _num(counts.max() / counts.sum()) if len(counts) else None,
                "excess_t": _num(summary["excess_t_indep"]),
                "p_value_random_entry": _num(summary["p_value_random_entry"])
                if label == plan.primary_horizon
                else None,
            }
        )
    primary = next(a for a in aggregate if a["horizon"] == plan.primary_horizon)
    label, checks = triage(primary, plan)
    p_values = ()
    if primary["p_value_random_entry"] is not None:
        p_values = (
            PValue(
                test="random_entry_mean_excess_v1",
                endpoint=f"pooled:{side}:{plan.primary_horizon}",
                value=primary["p_value_random_entry"],
                n_observations=primary["independent_events"],
            ),
        )
    metrics = {
        "triage": label,
        "gate_checks": checks,
        "provenance": {
            "screen_version": SCREEN_VERSION,
            "compiler_version": COMPILER_VERSION,
            "vocabulary_version": VOCABULARY_VERSION,
            "strategy_id": definition.strategy_id,
            "dataset_id": snapshot.dataset_id,
            "plan_id": plan.plan_id,
            "role": role,
            "window_start": period.start,
            "window_end": period.end,
            "data_start": plan.data_start(role),
            "warmup_days": plan.warmup_days,
            "side": side,
            "horizons": horizons,
            "primary_horizon": plan.primary_horizon,
            "entry": plan.entry,
            "horizon_exit": plan.horizon_exit,
            "outcome_boundary": plan.outcome_boundary,
            "return_model": plan.return_model,
            "costs": [c.model_dump(mode="python") for c in plan.costs if c.symbol in assets],
            "funding": plan.funding.model_dump(mode="python") if plan.funding else None,
            "baseline": plan.baseline,
            "statistics": stats.model_dump(mode="python"),
            "gates": plan.gates.model_dump(mode="python"),
            "independent_events": "backtest.events.decluster: keep first, then gap >= horizon bars",
        },
        "assets": per_asset_meta,
        "aggregate": aggregate,
        "per_asset": per_asset,
    }
    return ScreenResult(label, metrics, p_values, events)


# --------------------------------------------------------------------------- governed path


def run_screen(
    ledger: Ledger,
    experiment_id: str,
    *,
    software: SoftwareIdentity,
    workspaces: dict[str, ScreenWorkspace] | None = None,
) -> dict:
    """Start a preregistered screen attempt and attach exactly one terminal result.

    Preconditions (stage, plan version, software, not already started) raise before any
    start is recorded. After the start, every failure becomes an ``errored`` result with
    the exception type, message and a short traceback; the exception is not re-raised.
    ``workspaces`` (dataset ID -> workspace) lets a batch reuse decoded snapshots.
    """
    experiment = ledger.get_experiment(experiment_id)
    plan = ledger.get_plan(experiment.plan_id)
    if experiment.stage != "screen" or not isinstance(plan, ScreenPlan):
        raise LedgerError("attempt was not preregistered under a v2 screen plan")
    ledger.start(experiment_id, software=software)
    try:
        definition = ledger.get_strategy(experiment.strategy_id)
        if workspaces is not None and experiment.dataset_id in workspaces:
            ws = workspaces[experiment.dataset_id]
        else:
            ws = ScreenWorkspace(Snapshot.from_ledger(ledger, experiment.dataset_id))
            if workspaces is not None:
                workspaces[experiment.dataset_id] = ws
        result = screen(
            definition, plan, ws.snapshot, experiment.role, experiment.assets, workspace=ws
        )
    except Exception as exc:  # every post-start failure must be recorded
        tb = "".join(traceback.format_exception(exc, limit=-3)).strip()
        message = f"{exc}\n{tb}"[:9000] or type(exc).__name__
        metrics = {"triage": "ERROR", "provenance": _identity(experiment, plan)}
        result_id = ledger.record_result(
            experiment_id,
            status="errored",
            verdict="ERROR",
            metrics=metrics,
            error=ErrorInfo(kind=type(exc).__name__, message=message),
        )
        return {"result_id": result_id, "triage": "ERROR", "error": str(exc)}
    result.metrics["provenance"].update(_identity(experiment, plan))
    result_id = ledger.record_result(
        experiment_id,
        status=TRIAGE_STATUS[result.triage],
        verdict=result.triage,
        metrics=result.metrics,
        p_values=result.p_values,
    )
    return {"result_id": result_id, "triage": result.triage}


def _identity(experiment, plan: ScreenPlan) -> dict:
    return {
        "screen_version": SCREEN_VERSION,
        "experiment_id": experiment.experiment_id,
        "attempt": experiment.attempt,
        "logical_id": experiment.logical_id,
        "software_id": experiment.software_id,
        "strategy_id": experiment.strategy_id,
        "dataset_id": experiment.dataset_id,
        "plan_id": plan.plan_id,
    }
