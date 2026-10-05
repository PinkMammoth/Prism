"""Declarative event chains: breach -> failed breakout -> rejection -> structure shift ->
retest, built from the shared detectors (no bespoke strategy code), plus entry-delay
analytics: what each confirmation stage cost in time and price relative to the raw
failed breakout.

A ``ChainSpec`` names the timeframes and the parameters of each stage. ``run_chain``
returns every stage's table (each stage keeps its own event IDs and timestamps; later
stages reference their predecessor by ``parent_id``) and ``chain``: one row per failed
breakout with each stage's status, times, prices and delays. Phase 17 ablations are
selections of that table (or truncated ChainSpecs), never new detector code.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from market_signal.research.structure import events as ev
from market_signal.research.structure.levels import levels as build_levels
from market_signal.research.structure.registry import ChainSpec
from market_signal.research.structure.series import NAT, BarSeries


@dataclass(frozen=True)
class ChainResult:
    spec: ChainSpec
    levels: pd.DataFrame
    breaches: pd.DataFrame
    failed: pd.DataFrame
    held: pd.DataFrame
    rejections: pd.DataFrame | None
    shifts: pd.DataFrame | None
    retests: pd.DataFrame | None
    chain: pd.DataFrame


def _check(spec: ChainSpec, structure: BarSeries, event: BarSeries, confirm: BarSeries) -> None:
    for role, s, tf in (("structure", structure, spec.structure_tf), ("event", event, spec.event_tf),
                        ("confirm", confirm, spec.confirm_tf)):  # fmt: skip
        if s.prov.timeframe != tf:
            raise ValueError(f"{role} series is {s.prov.timeframe}, the spec says {tf}")
        if s.prov.venue != spec.venue:
            raise ValueError(f"{role} series is from {s.prov.venue}, the spec says {spec.venue}")
    ev.provenance(event, structure=structure, confirm=confirm)  # same venue/coin/availability


def run_chain(
    spec: ChainSpec, structure: BarSeries, event: BarSeries, confirm: BarSeries | None = None
) -> ChainResult:
    """Run one chain on one venue/coin. Pass the same series for equal timeframes."""
    confirm = confirm if confirm is not None else event
    _check(spec, structure, event, confirm)
    lv = pd.concat(
        [build_levels(structure, spec.breakout.breach.level, side) for side in ("high", "low")],
        ignore_index=True,
    )
    br = ev.breaches(lv, event, spec.breakout)
    for k, v in {"structure_tf": spec.structure_tf, "event_tf": spec.event_tf}.items():
        br[k] = v
    failed = ev.failed_breakouts(br, spec.breakout)
    held = ev.held_breakouts(br, spec.breakout)
    rej = ev.rejections(failed, event, spec.rejection) if spec.rejection else None
    sh = (
        ev.structure_shifts(failed, event, confirm, spec.structure_shift)
        if spec.structure_shift
        else None
    )
    rt = None
    if spec.retest is not None and sh is not None:
        shifts = sh[sh["status"] == "SHIFT"]
        breaks = pd.DataFrame({
            "event_id": shifts["event_id"],
            "break_side": np.where(shifts["direction"] == "bullish", "high", "low"),
            "level_price": shifts["structure_price"],
            "close_ns": shifts["close_ns"], "available_ns": shifts["available_ns"],
        })  # fmt: skip
        rt = ev.retests(breaks, confirm, spec.retest)
    return ChainResult(
        spec, lv, br, failed, held, rej, sh, rt, chain_table(spec, br, failed, rej, sh, rt)
    )


STAGES = ("breach", "failed", "rejection", "shift", "retest")


def chain_table(spec, br, failed, rej, sh, rt) -> pd.DataFrame:
    """One row per failed breakout (or per breach when ``failed_only`` is False).

    For each stage: ``<stage>_id``, ``<stage>_status``, ``<stage>_bar_ns`` (bar open),
    ``<stage>_available_ns``, ``<stage>_price`` (close of the stage bar). Delays are
    measured from the failed breakout (the "raw sweep"): ``<stage>_delay_hours`` (from the
    failed breakout's availability to the stage's), ``<stage>_delay_bars`` (in event-
    timeframe bars), ``<stage>_price_diff``, ``_bps`` and ``_atr`` (sweep's prior ATR), and
    ``<stage>_entry_cost_bps``: the same difference signed in the REVERSAL direction
    (long after a failed low breakout, short after a failed high one). Positive means
    waiting for the stage meant a worse reversal entry price. ``chain_available_ns`` is
    the availability of the deepest stage the spec includes; it is NaT when that stage
    did not occur. ``ablation`` names the deepest stage.
    """
    if not spec.failed_only:
        base = br.rename(columns={"event_id": "breach_id"})[
            [
                "breach_id",
                "side",
                "level_id",
                "level_kind",
                "level_price",
                "bar_ns",
                "available_ns",
                "close_price",
                "outcome",
                "atr",
            ]
        ].rename(
            columns={
                "bar_ns": "breach_bar_ns",
                "available_ns": "breach_available_ns",
                "close_price": "breach_price",
            }
        )
        base["ablation"] = spec.ablation
        base["chain_available_ns"] = base["breach_available_ns"]
        return base.reset_index(drop=True)
    step = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[spec.event_tf] * 1e9
    out = pd.DataFrame({
        "breach_id": failed["parent_id"], "failed_id": failed["event_id"], "side": failed["side"],
        "level_id": failed["level_id"], "level_kind": failed["level_kind"],
        "level_price": failed["level_price"], "extreme": failed["extreme"], "atr": failed["atr"],
        "breach_bar_ns": failed["breach_bar_ns"], "breach_price": failed["breach_close_price"],
        "failed_status": "FAILED", "failed_bar_ns": failed["bar_ns"],
        "failed_available_ns": failed["available_ns"], "failed_price": failed["close_price"],
        "failure_delay_bars": failed["failure_delay_bars"],
    })  # fmt: skip
    reversal = np.where(out["side"] == "low", 1.0, -1.0)

    def attach(stage: str, table: pd.DataFrame | None, status_ok: str, price_col: str | None):
        if table is None:
            return
        t = table.set_index("parent_id")
        key = out["failed_id"] if stage != "retest" else out.get("shift_id")
        sub = t.reindex(key)
        out[f"{stage}_id"] = sub["event_id"].to_numpy()
        out[f"{stage}_status"] = sub["status"].to_numpy()
        ok = sub["status"].to_numpy() == status_ok
        out[f"{stage}_bar_ns"] = np.where(ok, sub["bar_ns"].fillna(NAT).to_numpy(np.int64), NAT)
        out[f"{stage}_available_ns"] = np.where(
            ok, sub["available_ns"].fillna(NAT).to_numpy(np.int64), NAT
        )
        out[f"{stage}_price"] = np.where(ok, sub[price_col].to_numpy(float), np.nan)

    attach("rejection", rej, "REJECTION", "close_price")
    attach("shift", sh, "SHIFT", "close_price")
    if rt is not None and "shift_id" in out:
        attach("retest", rt, "RETEST", "close_price")
    for stage in ("rejection", "shift", "retest"):
        if f"{stage}_price" not in out:
            continue
        av = out[f"{stage}_available_ns"].to_numpy(np.int64)
        f0 = out["failed_available_ns"].to_numpy(np.int64)
        has = av != NAT
        out[f"{stage}_delay_hours"] = np.where(has, (av - f0) / 3.6e12, np.nan)
        bar = out[f"{stage}_bar_ns"].to_numpy(np.int64)
        out[f"{stage}_delay_bars"] = np.where(
            has, (bar - out["failed_bar_ns"].to_numpy(np.int64)) / step, np.nan
        )
        diff = out[f"{stage}_price"] - out["failed_price"]
        out[f"{stage}_price_diff"] = diff
        out[f"{stage}_price_diff_bps"] = diff / out["failed_price"] * 1e4
        out[f"{stage}_price_diff_atr"] = diff / out["atr"]
        out[f"{stage}_entry_cost_bps"] = reversal * out[f"{stage}_price_diff_bps"]
    deepest = {"B_failed_breakout": "failed", "C_rejection": "rejection",
               "D_structure_shift": "shift", "D_shift_no_rejection": "shift",
               "E_retest": "retest", "E_retest_no_rejection": "retest"}[spec.ablation]  # fmt: skip
    cols = [
        f"{s}_available_ns"
        for s in ("failed", "rejection", "shift", "retest")
        if f"{s}_available_ns" in out
    ]
    stage_cols = cols[: cols.index(f"{deepest}_available_ns") + 1]
    avs = out[stage_cols].to_numpy(np.int64)
    out["chain_available_ns"] = np.where((avs != NAT).all(axis=1), avs.max(axis=1), NAT)
    out["ablation"] = spec.ablation
    return out.reset_index(drop=True)
