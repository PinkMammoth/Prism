"""Daily strategy compiler: registered definition + registered snapshot -> masks.

The only market data read is the retained snapshot rows passed in (``Snapshot``); nothing
here touches the live market tables, a clock or an AI model. Output is a signal at bar T's
close. It is NOT a trade: entry timing (e.g. next bar open), costs, exits and statistics
belong to later stages, and nothing is shifted forward here.

Results are kept in memory and are not persisted: they are a pure function of
(strategy ID, dataset ID, symbol, asset class, compiler version), and ``digest`` lets a
later stage check that a recomputation is bit-identical.
"""

from __future__ import annotations

import hashlib
import operator
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from market_signal.data.prices import PriceBasis, adjustment_factors, apply_basis
from market_signal.models.domain import AssetClass, Calendar, Timeframe
from market_signal.research.lab import features as lab_features
from market_signal.research.lab.common import LabModel, canonical_json
from market_signal.research.lab.datasets import DatasetManifest, SeriesFingerprint
from market_signal.research.lab.spec import Condition, FeatureRef, StrategyDefinition
from market_signal.research.lab.vocabulary import VOCABULARY_VERSION, FeatureKey, parse_feature

COMPILER_VERSION = "lab_daily_compiler_v1"
EXIT_ATR = {"wilder_14": "atr_14", "perp_sma_14": "atr_sma_14"}
_COMPARE = {"gt": operator.gt, "ge": operator.ge, "lt": operator.lt, "le": operator.le}


class CompileError(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    """Decoded retained rows of one registered dataset, in manifest series order."""

    dataset_id: str
    manifest: DatasetManifest
    rows: tuple[list[dict], ...]

    def __post_init__(self):
        if self.manifest.dataset_id != self.dataset_id:
            raise CompileError("dataset ID does not match its manifest")
        if len(self.rows) != len(self.manifest.series):
            raise CompileError("snapshot rows do not match the manifest series")

    @classmethod
    def from_ledger(cls, ledger, dataset_id: str) -> Snapshot:
        # Ledger.read_dataset verifies content hashes, headers and row counts.
        return cls(dataset_id, ledger.get_dataset(dataset_id), ledger.read_dataset(dataset_id))

    def find(self, kind: str, symbol: str) -> tuple[SeriesFingerprint, list[dict]] | None:
        hits = [
            (s, r)
            for s, r in zip(self.manifest.series, self.rows, strict=True)
            if s.selection.kind == kind and s.selection.symbol == symbol
        ]
        if len(hits) > 1:
            raise CompileError(f"dataset has several {kind} series for {symbol}; ambiguous")
        return hits[0] if hits else None

    def symbols(self, market: str) -> list[str]:
        kind = "perp_bars" if market == "perp" else "bars"
        return sorted(
            {s.selection.symbol for s in self.manifest.series if s.selection.kind == kind}
        )


class FeatureInfo(LabModel):
    name: str
    warmup: int


class CompileMetadata(LabModel):
    compiler_version: str
    vocabulary_version: str
    strategy_id: str
    dataset_id: str
    symbol: str
    market: str
    side: str
    source: str
    asset_class: str
    price_basis: str
    features: tuple[FeatureInfo, ...]
    conditions: tuple[str, ...]
    cooldown_bars: int
    exit_atr: str
    warmup_bars: int
    rows: int
    first_close: str | None
    last_close: str | None
    max_interval_hours: float | None
    eligible_bars: int
    active_bars: int
    signals: int
    digest: str


@dataclass(frozen=True)
class CompiledStrategy:
    """All frames are indexed by bar ``close_time`` (UTC), the decision time of each row.

    ``features``: values (NaN where undefined). ``feature_valid``: finite values.
    ``conditions``: each condition's truth, False where undefined; ``condition_defined``
    says where it was evaluable. ``eligible``: warmup complete, every feature finite and
    every condition defined. ``active``: eligible and all conditions true. ``signal``:
    rising edges of ``active`` with cooldown (see ``edge_signal``). ``stop``: exit-intent
    stop level if entered on that bar's signal, in the frame's price basis.
    """

    metadata: CompileMetadata
    inputs: pd.DataFrame
    features: pd.DataFrame
    feature_valid: pd.DataFrame
    conditions: pd.DataFrame
    condition_defined: pd.DataFrame
    eligible: pd.Series
    active: pd.Series
    signal: pd.Series
    stop: pd.Series


# --------------------------------------------------------------------------- inputs


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=columns)
    for col in ("ts", "close_time", "time", "available_at"):
        if col in df:
            df[col] = pd.to_datetime(df[col], utc=True)
    return df


def _bars(fingerprint: SeriesFingerprint, rows: list[dict]) -> pd.DataFrame:
    if fingerprint.selection.timeframe != Timeframe.D1:
        raise CompileError("the Phase 3 compiler supports daily bars only")
    df = _frame(rows, ["ts", "close_time", "open", "high", "low", "close", "volume"])
    if df.empty:
        raise CompileError(
            f"no {fingerprint.selection.kind} rows for {fingerprint.selection.symbol}"
        )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="raise").astype(float)
    if not (df["ts"].is_monotonic_increasing and df["ts"].is_unique):
        raise CompileError("bars must have strictly increasing open timestamps")
    if not (df["close_time"].is_monotonic_increasing and df["close_time"].is_unique):
        raise CompileError("bars must have strictly increasing close times")
    if (df["close_time"] <= df["ts"]).any():
        raise CompileError("every bar must close after it opens")
    return df.reset_index(drop=True)


def _calendar(asset_class: AssetClass) -> Calendar:
    return Calendar.CRYPTO_24_7 if asset_class == AssetClass.CRYPTO else Calendar.NYSE


def build_inputs(
    snapshot: Snapshot,
    symbol: str,
    market: str,
    asset_class: AssetClass,
    need_funding: bool,
) -> tuple[pd.DataFrame, str, str]:
    """Native daily input frame, its source label and price-basis label."""
    kind = "perp_bars" if market == "perp" else "bars"
    found = snapshot.find(kind, symbol)
    if found is None:
        raise CompileError(f"dataset {snapshot.dataset_id} has no {kind} series for {symbol}")
    fingerprint, rows = found
    source = fingerprint.selection.source
    bars = _bars(fingerprint, rows)
    if market == "spot":
        actions = snapshot.find("corporate_actions", symbol)
        if actions is None:
            raise CompileError(f"spot compilation needs the corporate_actions series for {symbol}")
        if actions[0].selection.source != source:
            raise CompileError("corporate actions must come from the bars' source")
        acts = _frame(actions[1], ["date", "split_factor", "dividend"])
        factors = adjustment_factors(bars, acts, _calendar(asset_class))
        # FORWARD split adjustment: express prices in the share terms of the first bar.
        # Backward adjustment (Prism's display/research basis) rescales bar T by splits
        # after T; this is the same series up to one constant, so ratios are identical,
        # but a value at T now depends only on splits effective on or before T.
        factors["split_mult"] = factors["split_mult"] / factors["split_mult"].iloc[0]
        return apply_basis(bars, factors, PriceBasis.SPLIT), source, "split_forward"
    found = snapshot.find("perp_funding", symbol)
    if found is None and need_funding:
        raise CompileError(f"funding features need the perp_funding series for {symbol}")
    if found is not None:  # always derived when retained: screening returns need it too
        if found[0].selection.source != source:
            raise CompileError("funding must come from the bars' venue")
        funding = _frame(found[1], ["time", "funding_rate", "available_at"])
        bars["funding_day"] = lab_features.lab_funding_day(bars, funding)
    return bars, source, "perp_last"


@dataclass
class CompileCache:
    """In-process reuse for many definitions on ONE snapshot (e.g. a screening batch).

    Inputs are keyed by (symbol, market, asset class) and feature series additionally by
    canonical feature name, so a shared ``ema_20`` is computed once. Cached frames are
    never mutated. Results are identical with or without a cache (tested).
    """

    snapshot: Snapshot
    inputs: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- conditions


def _label(condition: Condition) -> str:
    right = condition.right
    if isinstance(right, FeatureRef):
        rhs = right.name
    else:
        rhs = repr(float(right) + 0.0)  # one spelling for -0.0
    return f"{condition.left.name} {condition.op} {rhs}"


def evaluate_condition(condition: Condition, features: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """(truth, defined). Truth is False wherever the condition is undefined.

    A comparison at T needs both sides finite at T. A crossover at T needs both sides
    finite at T AND at T-1 (the previous native bar); it is true when the strict relation
    holds at T and did not hold at T-1. Only values at T and T-1 are read.
    """
    left = features[condition.left.name]
    if isinstance(condition.right, FeatureRef):
        right = features[condition.right.name]
        right_ok = np.isfinite(right)
    else:
        right = float(condition.right)
        right_ok = pd.Series(True, index=features.index)
    defined = np.isfinite(left) & right_ok
    if condition.op in _COMPARE:
        truth = _COMPARE[condition.op](left, right)
    else:
        prev_left = left.shift(1)
        prev_right = right.shift(1) if isinstance(right, pd.Series) else right
        if condition.op == "crosses_above":
            truth = (left > right) & (prev_left <= prev_right)
        else:
            truth = (left < right) & (prev_left >= prev_right)
        defined = defined & defined.shift(1, fill_value=False)
    return (truth & defined).astype(bool), defined.astype(bool)


def edge_signal(active: pd.Series, eligible: pd.Series, cooldown: int) -> pd.Series:
    """``setups.base.edge_trigger`` semantics with explicit missing-state handling.

    Fires at T when ``active[T]`` and the previous bar was eligible and inactive, and more
    than ``cooldown`` bars have passed since the last signal. If T-1 was ineligible the
    previous state is unknown, so a condition already true when data becomes valid does
    not fire; it must turn off and on again. With all bars eligible this equals
    ``edge_trigger(active, cooldown)`` (tested).
    """
    a = active.to_numpy(bool)
    e = eligible.to_numpy(bool)
    out = np.zeros(len(a), dtype=bool)
    last = -(10**9)
    for i in range(1, len(a)):
        if a[i] and e[i - 1] and not a[i - 1] and i - last > cooldown:
            out[i] = True
            last = i
    return pd.Series(out, index=active.index)


def _digest(index: pd.Index, frames: list[pd.DataFrame | pd.Series]) -> str:
    h = hashlib.sha256()
    h.update(np.asarray(index.as_unit("ns").asi8, dtype="<i8").tobytes())
    for obj in frames:
        df = obj.to_frame() if isinstance(obj, pd.Series) else obj
        for col in df.columns:
            h.update(str(col).encode() + b"\0")
            values = df[col].to_numpy()
            if values.dtype == bool:
                h.update(values.astype("u1").tobytes())
            else:
                v = values.astype("<f8")
                h.update(np.where(np.isnan(v), np.nan, v).tobytes())  # one NaN bit pattern
    return h.hexdigest()


# --------------------------------------------------------------------------- compile


def required_features(definition: StrategyDefinition) -> list[FeatureKey]:
    names = {ref.name for c in definition.conditions for ref in c.references()}
    names.add(EXIT_ATR[definition.exit.atr])
    return sorted(parse_feature(n) for n in names)


def compile_strategy(
    definition: StrategyDefinition,
    snapshot: Snapshot,
    symbol: str,
    *,
    asset_class: AssetClass | None = None,
    cache: CompileCache | None = None,
) -> CompiledStrategy:
    """Compile one DAILY definition for one symbol of a registered snapshot.

    Perps are crypto by construction; spot requires the asset class explicitly (it fixes
    the trading calendar for split dates and the bar count of ``roc_1m``). Missing series,
    columns or unsupported timeframes raise ``CompileError``; nothing falls back.
    """
    definition = StrategyDefinition.model_validate(definition.model_dump(mode="python"))
    refs = [ref for c in definition.conditions for ref in c.references()]
    if definition.trigger_timeframe != Timeframe.D1 or any(
        ref.timeframe != Timeframe.D1 for ref in refs
    ):
        raise CompileError("the Phase 3 compiler supports daily definitions only")
    if definition.market == "perp":
        if asset_class not in (None, AssetClass.CRYPTO):
            raise CompileError("perp compilation is defined for crypto only")
        asset_class = AssetClass.CRYPTO
    elif asset_class is None:
        raise CompileError("spot compilation requires an explicit asset class")
    keys = required_features(definition)
    need_funding = any("funding_day" in k.spec.inputs for k in keys)
    if cache is not None and cache.snapshot is not snapshot:
        raise CompileError("compile cache belongs to a different snapshot")
    key = (symbol, definition.market, asset_class)
    if cache is not None and key in cache.inputs:
        inputs, source, basis = cache.inputs[key]
        if need_funding and "funding_day" not in inputs:
            raise CompileError(f"funding features need the perp_funding series for {symbol}")
    else:
        inputs, source, basis = build_inputs(
            snapshot, symbol, definition.market, asset_class, need_funding
        )
        if cache is not None:
            cache.inputs[key] = (inputs, source, basis)
    index = pd.DatetimeIndex(inputs["close_time"], name="close_time")

    values = {}
    for k in keys:
        if cache is not None and (*key, k.name) in cache.features:
            values[k.name] = cache.features[(*key, k.name)]
            continue
        try:
            values[k.name] = lab_features.compute(k, inputs, asset_class)
        except ValueError as exc:
            raise CompileError(str(exc)) from exc
        if cache is not None:
            cache.features[(*key, k.name)] = values[k.name]
    feats = pd.DataFrame(values, index=inputs.index)
    feature_valid = pd.DataFrame(
        {name: np.isfinite(col) for name, col in feats.items()}, index=inputs.index
    )

    ordered = sorted(definition.conditions, key=lambda c: canonical_json(c.model_dump(mode="json")))
    labels = [_label(c) for c in ordered]
    truths, defined = {}, {}
    for label, condition in zip(labels, ordered, strict=True):
        truths[label], defined[label] = evaluate_condition(condition, feats)
    conditions = pd.DataFrame(truths, index=inputs.index)
    condition_defined = pd.DataFrame(defined, index=inputs.index)

    warmups = [FeatureInfo(name=k.name, warmup=lab_features.warmup(k, asset_class)) for k in keys]
    crossover = any(c.op.startswith("crosses") for c in definition.conditions)
    warmup_bars = max(w.warmup for w in warmups) + (1 if crossover else 0)
    eligible = (
        pd.Series(np.arange(len(inputs)) >= warmup_bars - 1, index=inputs.index)
        & feature_valid.all(axis=1)
        & condition_defined.all(axis=1)
    )
    active = conditions.all(axis=1) & eligible
    signal = edge_signal(active, eligible, definition.cooldown_bars)
    atr = feats[EXIT_ATR[definition.exit.atr]]
    k = definition.exit.stop_atr * (1 if definition.side == "long" else -1)
    stop = inputs["close"] - k * atr

    frames = {
        "features": feats,
        "feature_valid": feature_valid,
        "conditions": conditions,
        "condition_defined": condition_defined,
        "eligible": eligible.rename("eligible"),
        "active": active.rename("active"),
        "signal": signal.rename("signal"),
        "stop": stop.rename("stop"),
    }
    for obj in frames.values():
        obj.index = index
    inputs = inputs.set_index(index)
    gaps = index.to_series().diff().dropna()
    metadata = CompileMetadata(
        compiler_version=COMPILER_VERSION,
        vocabulary_version=VOCABULARY_VERSION,
        strategy_id=definition.strategy_id,
        dataset_id=snapshot.dataset_id,
        symbol=symbol,
        market=definition.market,
        side=definition.side,
        source=source,
        asset_class=asset_class.value,
        price_basis=basis,
        features=tuple(warmups),
        conditions=tuple(labels),
        cooldown_bars=definition.cooldown_bars,
        exit_atr=EXIT_ATR[definition.exit.atr],
        warmup_bars=warmup_bars,
        rows=len(index),
        first_close=index[0].isoformat(),
        last_close=index[-1].isoformat(),
        max_interval_hours=gaps.max().total_seconds() / 3600 if len(gaps) else None,
        eligible_bars=int(eligible.sum()),
        active_bars=int(active.sum()),
        signals=int(signal.sum()),
        digest=_digest(index, list(frames.values())),
    )
    return CompiledStrategy(metadata=metadata, inputs=inputs, **frames)


def compile_registered(
    ledger,
    strategy_id: str,
    dataset_id: str,
    *,
    symbols: tuple[str, ...] = (),
    asset_class: AssetClass | None = None,
) -> list[CompiledStrategy]:
    """Compile a ledger-registered strategy on a ledger-registered snapshot (read-only)."""
    definition = ledger.get_strategy(strategy_id)
    snapshot = Snapshot.from_ledger(ledger, dataset_id)
    chosen = symbols or tuple(snapshot.symbols(definition.market))
    if not chosen:
        raise CompileError(f"dataset {dataset_id} has no {definition.market} bar series")
    return [compile_strategy(definition, snapshot, s, asset_class=asset_class) for s in chosen]
