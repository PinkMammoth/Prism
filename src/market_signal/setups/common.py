"""Helpers shared by setups."""

from __future__ import annotations

from typing import Any

import pandas as pd

from market_signal.config import get_settings


def load_setup_config(name: str) -> dict[str, Any]:
    return get_settings().yaml(f"setups/{name}.yaml")


def class_params(cfg: dict[str, Any], asset_class: str) -> dict[str, Any]:
    return {**(cfg.get("defaults") or {}), **((cfg.get("by_class") or {}).get(asset_class) or {})}


def between(x: pd.Series, lo: float | None, hi: float | None) -> pd.Series:
    out = x.notna()
    if lo is not None:
        out &= x >= lo
    if hi is not None:
        out &= x <= hi
    return out


def technical_stop(feat: pd.DataFrame, swing_buffer_atr: float, max_stop_atr: float) -> pd.Series:
    """Stop below the recent swing low, capped at max_stop_atr below the close."""
    swing = feat["swing_low_20"] - swing_buffer_atr * feat["atr_14"]
    floor = feat["close"] - max_stop_atr * feat["atr_14"]
    return pd.concat([swing, floor], axis=1).max(axis=1, skipna=False)
