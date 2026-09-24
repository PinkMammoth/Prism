"""Setup interface, registry, and the declarative conditions DSL.

A setup turns a *causal* feature frame into:
  signal  bool   — conditions met at bar t's close (edge-triggered + cooldown)
  stop    float  — technical invalidation (split-adjusted price), NaN for INVESTMENT setups
  eligible bool  — bars where the setup *could* be evaluated (the baseline universe)
Setups must only use columns of the feature frame at rows <= t (tests enforce this by
truncation invariance) plus explicitly point-in-time context (fundamentals, regimes).
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import numpy as np
import pandas as pd

from market_signal.backtest.engine import ExitRules


@dataclass
class SetupContext:
    """Point-in-time extras a setup may use. Every field is aligned to the feature frame
    index and must already be as-of each bar's close_time."""

    symbol: str
    asset_class: str
    fundamentals: pd.DataFrame | None = None  # PIT fundamental features (Setup C)
    regime: pd.Series | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SetupOutput:
    signal: pd.Series
    stop: pd.Series
    eligible: pd.Series
    diagnostics: pd.DataFrame | None = None  # per-condition booleans for explanations


class Setup(Protocol):
    name: ClassVar[str]
    title: ClassVar[str]
    kind: ClassVar[str]  # TRADE | INVESTMENT

    def default_params(self, asset_class: str) -> dict[str, Any]: ...

    def evaluate(
        self, feat: pd.DataFrame, params: dict[str, Any], ctx: SetupContext
    ) -> SetupOutput: ...

    def exit_rules(self, params: dict[str, Any]) -> ExitRules: ...


REGISTRY: dict[str, Setup] = {}


def register(setup_cls: type) -> type:
    inst = setup_cls()
    REGISTRY[inst.name] = inst
    return setup_cls


def get_setup(name: str) -> Setup:
    import market_signal.setups  # noqa: F401  (populates the registry)

    key = name.replace("-", "_")
    if key not in REGISTRY:
        raise KeyError(f"unknown setup {name!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[key]


def edge_trigger(cond: pd.Series, cooldown: int) -> pd.Series:
    """True on the first bar a condition becomes true, then silent for ``cooldown`` bars.

    Uses only past values (the previous bar's condition and the last trigger), so it is
    causal. NaN conditions count as False.
    """
    c = cond.fillna(False).to_numpy(bool)
    out = np.zeros(len(c), dtype=bool)
    last = -(10**9)
    for i in range(len(c)):
        rising = c[i] and (i == 0 or not c[i - 1])
        if rising and i - last > cooldown:
            out[i] = True
            last = i
    return pd.Series(out, index=cond.index)


# ----------------------------------------------------------------------------- DSL
# Named boolean conditions usable in experiment YAML (``price_above_sma_200: true``).
NAMED_CONDITIONS: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "price_above_sma_200": lambda f: f["close"] > f["sma_200"],
    "price_above_sma_50": lambda f: f["close"] > f["sma_50"],
    "sma_50_above_sma_200": lambda f: f["sma_50"] > f["sma_200"],
    "sma_200_rising": lambda f: f["sma_200_slope"] > 0,
}
_OPS = {"min": operator.ge, "max": operator.le, "gt": operator.gt, "lt": operator.lt}


def conditions_mask(
    feat: pd.DataFrame, conditions: dict[str, Any]
) -> tuple[pd.Series, pd.DataFrame, pd.Series]:
    """Evaluate a conditions mapping. Returns (all_true, per-condition frame, all_defined).

    Keys are named conditions (bool value) or feature columns with {min,max,gt,lt}.
    A bar where any referenced input is NaN is *undefined* (not eligible), never False-by-
    default: missing stays missing.
    """
    parts, defined = {}, pd.Series(True, index=feat.index)
    for key, spec in conditions.items():
        if key in NAMED_CONDITIONS:
            raw = NAMED_CONDITIONS[key](feat)
            cols = _named_inputs(key)
            ok = feat[cols].notna().all(axis=1)
            val = raw if bool(spec) else ~raw
        elif key in feat.columns:
            if not isinstance(spec, dict):
                raise ValueError(f"condition {key}: expected mapping of min/max/gt/lt")
            val = pd.Series(True, index=feat.index)
            for op, bound in spec.items():
                if op not in _OPS:
                    raise ValueError(f"condition {key}: unknown operator {op}")
                val &= _OPS[op](feat[key], float(bound))
            ok = feat[key].notna()
        else:
            raise ValueError(f"unknown condition or feature {key!r}")
        parts[key] = val & ok
        defined &= ok
    frame = pd.DataFrame(parts, index=feat.index)
    return frame.all(axis=1) & defined, frame, defined


def _named_inputs(key: str) -> list[str]:
    return {
        "price_above_sma_200": ["close", "sma_200"],
        "price_above_sma_50": ["close", "sma_50"],
        "sma_50_above_sma_200": ["sma_50", "sma_200"],
        "sma_200_rising": ["sma_200_slope"],
    }[key]


class ConditionsSetup:
    """A setup defined entirely by an experiment's ``conditions`` block."""

    name = "conditions"
    title = "Declarative conditions"
    kind = "TRADE"

    def default_params(self, asset_class: str) -> dict[str, Any]:
        return {"conditions": {}, "cooldown": 10, "stop_atr": 2.0, "max_hold_bars": 21}

    def evaluate(
        self, feat: pd.DataFrame, params: dict[str, Any], ctx: SetupContext
    ) -> SetupOutput:
        cond, frame, defined = conditions_mask(feat, params["conditions"])
        signal = edge_trigger(cond, int(params["cooldown"]))
        stop = feat["close"] - float(params["stop_atr"]) * feat["atr_14"]
        return SetupOutput(signal, stop, defined & feat["atr_14"].notna(), frame)

    def exit_rules(self, params: dict[str, Any]) -> ExitRules:
        return ExitRules(max_hold_bars=int(params["max_hold_bars"]))


REGISTRY["conditions"] = ConditionsSetup()
