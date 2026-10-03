"""Feature vocabulary v1: the names a strategy definition may reference (no calculations).

A feature reference is ONE canonical token: a family name followed by its integer
parameters in a fixed order, e.g. ``ema_50``, ``ret_5``, ``donchian_high_20``,
``funding_pct_7_365``. Parameters are part of the name, so the existing ``FeatureRef``
shape (and every strategy ID registered before this vocabulary) is unchanged, and one
logical feature has exactly one spelling: no leading zeros, exact parameter count, bounded
values, and a family's implicit default (``rel_volume`` = 20 bars) cannot be spelled out.

This module is deliberately free of pandas so that ``spec.py`` can validate names without
importing calculation code. ``features.py`` maps every family here to its implementation;
a test enforces that the two stay in one-to-one correspondence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

VOCABULARY_VERSION = "lab_features_v1"

Input = Literal["open", "high", "low", "close", "volume", "funding_day"]


@dataclass(frozen=True)
class Param:
    name: str
    lo: int
    hi: int


@dataclass(frozen=True)
class Family:
    name: str
    params: tuple[Param, ...]
    inputs: tuple[Input, ...]
    market: Literal["general", "perp"]
    daily_only: bool
    unit: str
    meaning: str
    implicit: tuple[int, ...] | None = None  # bare-name parameters; cannot be spelled out


_N = (Param("n", 1, 1000),)
_N2 = (Param("n", 2, 1000),)  # a dispersion or recursion needs at least two bars
_PX = ("close",)
_HLC = ("high", "low", "close")

FAMILIES: dict[str, Family] = {
    f.name: f
    for f in (
        # Raw native-bar columns, observable at the bar's close.
        Family("open", (), ("open",), "general", False, "price", "bar open"),
        Family("high", (), ("high",), "general", False, "price", "bar high"),
        Family("low", (), ("low",), "general", False, "price", "bar low"),
        Family("close", (), _PX, "general", False, "price", "bar close"),
        Family("volume", (), ("volume",), "general", False, "volume", "bar volume"),
        # Price / return
        Family("ret", _N, _PX, "general", False, "fraction", "close / close n bars ago - 1"),
        Family(
            "roc_1m",
            (),
            _PX,
            "general",
            True,
            "fraction",
            "legacy: ret over one class month (30 crypto / 21 NYSE daily bars)",
        ),
        Family(
            "range_pct",
            (),
            ("high", "low", "close"),
            "general",
            False,
            "fraction",
            "(high - low) / close of the same bar",
        ),
        # Trend
        Family("sma", _N, _PX, "general", False, "price", "simple mean of the last n closes"),
        Family("ema", _N2, _PX, "general", False, "price", "recursive EMA, adjust=False"),
        Family("dist_sma", _N, _PX, "general", False, "fraction", "close / sma_n - 1"),
        Family("dist_ema", _N2, _PX, "general", False, "fraction", "close / ema_n - 1"),
        # Momentum
        Family("rsi", _N2, _PX, "general", False, "0-100", "Wilder RSI"),
        # Volatility
        Family("atr", _N, _HLC, "general", False, "price", "Wilder ATR (spot convention)"),
        Family(
            "atr_sma",
            _N,
            _HLC,
            "general",
            False,
            "price",
            "simple-mean ATR including the first bar's range (perp convention)",
        ),
        Family("atr_pct", _N, _HLC, "general", False, "fraction", "Wilder atr_n / close"),
        Family(
            "rvol",
            _N2,
            _PX,
            "general",
            False,
            "fraction per bar",
            "sample std of the last n log returns, NOT annualised",
        ),
        # Breakout: thresholds use the PRIOR n completed bars, never the current bar.
        Family(
            "donchian_high",
            _N,
            ("high",),
            "general",
            False,
            "price",
            "max high of the n bars before the current bar",
        ),
        Family(
            "donchian_low",
            _N,
            ("low",),
            "general",
            False,
            "price",
            "min low of the n bars before the current bar",
        ),
        Family(
            "close_high",
            _N,
            _PX,
            "general",
            False,
            "price",
            "max close of the n bars before the current bar",
        ),
        Family(
            "close_low",
            _N,
            _PX,
            "general",
            False,
            "price",
            "min close of the n bars before the current bar",
        ),
        Family(
            "dist_donchian_high",
            _N,
            ("high", "close"),
            "general",
            False,
            "fraction",
            "close / donchian_high_n - 1",
        ),
        Family(
            "dist_donchian_low",
            _N,
            ("low", "close"),
            "general",
            False,
            "fraction",
            "close / donchian_low_n - 1",
        ),
        # Volume
        Family("vol_sma", _N, ("volume",), "general", False, "volume", "mean of last n volumes"),
        Family(
            "rel_volume",
            _N,
            ("volume",),
            "general",
            False,
            "ratio",
            "volume / vol_sma_n (window includes the current bar); bare name = 20",
            implicit=(20,),
        ),
        Family(
            "vol_z",
            _N2,
            ("volume",),
            "general",
            False,
            "z",
            "(volume - mean) / sample std of the n bars before the current bar",
        ),
        # Perp-native funding (daily only: the daily aggregation is the only one defined)
        Family(
            "funding_day",
            (),
            ("funding_day",),
            "perp",
            True,
            "rate per day",
            "settled funding in (bar open, bar close], causal cadence, coverage-checked",
        ),
        Family(
            "funding_sum",
            _N,
            ("funding_day",),
            "perp",
            True,
            "rate",
            "sum of the last n funding_day values",
        ),
        Family(
            "funding_mean",
            _N,
            ("funding_day",),
            "perp",
            True,
            "rate per day",
            "mean of the last n funding_day values",
        ),
        Family(
            "funding_pct",
            (Param("avg", 1, 365), Param("lookback", 2, 2000)),
            ("funding_day",),
            "perp",
            True,
            "fraction (0, 1]",
            "pct rank (average ties) of funding_mean_avg within its trailing lookback values, "
            "including the current bar; full lookback required",
        ),
    )
}

_TOKEN = re.compile(r"^(?P<family>[a-z]+(?:_[a-z]+)*)(?P<params>(?:_[0-9]+)*)$")


@dataclass(frozen=True, order=True)
class FeatureKey:
    family: str
    params: tuple[int, ...]

    @property
    def spec(self) -> Family:
        return FAMILIES[self.family]

    @property
    def name(self) -> str:
        """The unique canonical spelling."""
        if self.spec.implicit == self.params:
            return self.family
        return "_".join([self.family, *map(str, self.params)])


def parse_feature(name: str) -> FeatureKey:
    """Parse and validate a canonical feature token; raise ValueError otherwise."""
    if not isinstance(name, str):
        raise ValueError("feature name must be a string")
    if name in FAMILIES:  # exact names first: covers roc_1m and parameterless families
        family = FAMILIES[name]
        if family.params and family.implicit is None:
            raise ValueError(f"feature {name!r} requires parameters: {_signature(family)}")
        return FeatureKey(name, family.implicit or ())
    match = _TOKEN.fullmatch(name)
    if match is None or match["family"] not in FAMILIES:
        raise ValueError(f"unknown feature {name!r}; vocabulary {VOCABULARY_VERSION}")
    family = FAMILIES[match["family"]]
    raw = match["params"].split("_")[1:]
    if len(raw) != len(family.params):
        raise ValueError(f"feature {name!r} expects {_signature(family)}")
    values = []
    for token, param in zip(raw, family.params, strict=True):
        if token != str(int(token)):
            raise ValueError(f"feature {name!r}: parameters must not have leading zeros")
        value = int(token)
        if not param.lo <= value <= param.hi:
            raise ValueError(f"feature {name!r}: {param.name} must be in [{param.lo}, {param.hi}]")
        values.append(value)
    key = FeatureKey(family.name, tuple(values))
    if key.name != name:
        raise ValueError(f"feature {name!r} is not canonical; write {key.name!r}")
    return key


def _signature(family: Family) -> str:
    return family.name + "".join(f"_<{p.name}>" for p in family.params)


def catalog() -> list[dict]:
    """Data-only description of the vocabulary, e.g. for a later proposal client."""
    return [
        {
            "family": f.name,
            "spelling": _signature(f),
            "params": [{"name": p.name, "min": p.lo, "max": p.hi} for p in f.params],
            "implicit": list(f.implicit) if f.implicit else None,
            "inputs": list(f.inputs),
            "market": f.market,
            "daily_only": f.daily_only,
            "unit": f.unit,
            "meaning": f.meaning,
        }
        for f in FAMILIES.values()
    ]
