"""Per-venue inputs shared by the retrospective replay and the prospective runner.

Everything is Phase 20 code, unchanged: the causal outcome ledger (``lifecycle.outcomes``,
Phase 3 compiler + Prism perp returns + causal declustering), causal market-state and
stress labels (``lifecycle.market``) and the ``EventSet`` the estimators read.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from market_signal.models.domain import AssetClass
from market_signal.research.lab.compiler import CompileCache, Snapshot, build_inputs
from market_signal.research.lab.spec import StrategyDefinition
from market_signal.research.lifecycle import estimators as es
from market_signal.research.lifecycle import market as mk
from market_signal.research.lifecycle.outcomes import strategy_outcomes
from market_signal.research.lifecycle.policy import LifecyclePolicy


@dataclass
class VenueInputs:
    snaps: dict[str, Snapshot]
    caches: dict[str, CompileCache]
    state: pd.DataFrame  # market_state_v1 labels by reference close
    stress: pd.DataFrame


def venue_inputs(snaps: dict[str, Snapshot], reference: str, lp: LifecyclePolicy) -> VenueInputs:
    frames = {c: build_inputs(snaps[c], c, "perp", AssetClass.CRYPTO, True)[0] for c in snaps}
    state = mk.market_state(frames, reference, lp.market_state)
    return VenueInputs(snaps=snaps, caches={c: CompileCache(snaps[c]) for c in snaps},
                       state=state, stress=mk.stress_days(frames[reference], lp.stress))  # fmt: skip


def strategy_ledger(defn: StrategyDefinition, vi: VenueInputs, costs: dict[str, tuple[float, float]],
                    horizon: int, lp: LifecyclePolicy) -> tuple[pd.DataFrame, es.EventSet | None]:  # fmt: skip
    """(every signal row incl. unresolved ones, the independent evaluable EventSet or None)."""
    df, hs = strategy_outcomes(defn, vi.snaps, costs, horizon_bars=horizon, baseline=lp.baseline,
                               caches=vi.caches)  # fmt: skip
    if df.empty or hs is None:
        return df, None
    df = mk.label_events(df, vi.state, vi.stress)
    return df, es.EventSet.from_frame(df, history_start=hs, block_days=lp.inference.block_days)


def regime_codes(state: pd.DataFrame, times: np.ndarray, key: str = "trend") -> np.ndarray:
    labels = es.REGIME_LABELS[key]
    idx = state.index
    pos = (
        idx.searchsorted(pd.to_datetime([es.to_time(t) for t in times], utc=True), side="right") - 1
    )
    vals = state[key].to_numpy()
    return np.array([labels.index(vals[p]) if p >= 0 and vals[p] in labels else -1 for p in pos],
                    dtype=np.int64)  # fmt: skip


def current_regime(state: pd.DataFrame, at) -> dict[str, str]:
    pos = state.index.searchsorted(pd.Timestamp(at), side="right") - 1
    if pos < 0:
        return {d: "unknown" for d in es.REGIME_DIMS}
    row = state.iloc[pos]
    return {d: row[d] for d in es.REGIME_DIMS}


def daily_times(first: float, last: float) -> np.ndarray:
    """Every daily close from ``first`` through ``last`` inclusive (trigger timeframe 1d)."""
    n = int(np.floor(last - first + 1e-9)) + 1 if last >= first else 0
    return first + np.arange(max(n, 0), dtype=float)
