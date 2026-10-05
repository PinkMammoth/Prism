"""Event layer: breach, breakout outcome, failed breakout ("sweep"), rejection, structure
shift and retest. Each detector is written once for the HIGH side and runs on the low side
through ``BarSeries.oriented`` (a negated frame), so bullish and bearish cannot diverge.

Vocabulary is observational. Prism uses the term "sweep" only as shorthand for an
objectively defined failed-breakout event. It does not imply knowledge of stop locations,
manipulation or institutional intent.

Every event row carries:

- ``event_id``: deterministic content ID (series identity, primitive version, parameters,
  reference, event bar open time). Re-running reproduces it; nothing random or clock-based;
- ``bar_ns`` / ``close_ns``: open and close of the bar the event is determined on;
- ``available_ns``: when it could be known: ``ready_at`` of that bar, never before any
  input it references (level, predecessor event) was known;
- ``parent_id``: its causal predecessor (breach -> failed breakout -> rejection / shift ->
  retest), so a chain is a join, not a bespoke strategy;
- provenance: venue, coin, timeframes, availability mode and assumed latency, and whether
  every bar it read was observed live (``inputs_live``).

Unresolved outcomes (the data ends inside a window) are reported as UNRESOLVED and never
emitted as events: appending bars can resolve them later, but cannot change an event that
was already available.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from market_signal.research.lab.common import canonical_json
from market_signal.research.structure.levels import (
    SIDES,
    eligible_range,
    sign,
    swings,
    tolerance_price,
)
from market_signal.research.structure.registry import (
    BreachParams,
    BreakoutParams,
    RejectionParams,
    RetestParams,
    StructureShiftParams,
    params_key,
)
from market_signal.research.structure.series import NAT, BarSeries

OPPOSITE = {"high": "low", "low": "high"}


def provenance(series: BarSeries, **roles: BarSeries) -> dict:
    p = series.prov
    tfs = {k: s.prov.timeframe for k, s in roles.items()}
    for s in roles.values():
        if (s.prov.venue, s.prov.coin, s.prov.availability_mode, s.prov.assumed_latency_s) != (
            p.venue, p.coin, p.availability_mode, p.assumed_latency_s,
        ):  # fmt: skip
            raise ValueError("all series of one detection must share venue, coin and availability")
    return {"venue": p.venue, "coin": p.coin, "dataset_key": p.dataset_key,
            "availability_mode": p.availability_mode, "assumed_latency_s": p.assumed_latency_s,
            **{f"{k}_tf": v for k, v in tfs.items()}}  # fmt: skip


def ids(prov: dict, version: str, params):
    """Event-ID maker for one detector run. The ID is the SHA-256 of the canonical JSON of the
    static definition (provenance, primitive version, parameters), the reference it is
    about (a level or predecessor event ID) and the event bar's open time (ns)."""
    head = canonical_json({"provenance": prov, "primitive": version, "params": params_key(params)})

    def make(ref: str, bar_ns: int) -> str:
        return "sev_" + hashlib.sha256(f"{head}\0{ref}\0{int(bar_ns)}".encode()).hexdigest()

    return make


def event_id(prov: dict, version: str, params, ref: str, bar_ns: int) -> str:
    return ids(prov, version, params)(ref, bar_ns)


# --------------------------------------------------------------------------- continuous


def rejection_metrics(series: BarSeries) -> pd.DataFrame:
    """``rejection_metrics_v1``: continuous per-bar measurements (no threshold).

    For side s in (high, low): ``wick_s`` (upper wick for high, lower wick for low),
    ``wick_body_s`` = wick / |close - open| (NaN for a zero body), ``wick_range_s`` =
    wick / range, ``reversal_atr_s`` = distance from the s extreme back to the close in
    prior-ATR. Side-free: ``clv`` = ((c - l) - (h - c)) / range in [-1, 1] (+1 = close at
    the high), ``range_atr``, ``range_expansion`` = range / mean range of the 20 bars before,
    ``rel_volume`` and ``ret`` (close-to-close). NaN wherever a denominator is zero/unknown.
    """
    h, lo, c = series.h, series.l, series.c
    rng = h - lo
    body = np.abs(c - series.o)
    safe = np.where(rng > 0, rng, np.nan)
    prior_rng = pd.Series(rng).shift(1).rolling(20, min_periods=20).mean().to_numpy()
    out = {
        "clv": ((c - lo) - (h - c)) / safe,
        "range_atr": rng / series.atr_prior,
        "range_expansion": rng / np.where(prior_rng > 0, prior_rng, np.nan),
        "rel_volume": series.rel_volume,
        "ret": np.concatenate([[np.nan], c[1:] / c[:-1] - 1]) if len(c) else c,
    }
    for side in SIDES:
        xo, xh, _, xc = series.oriented(side)
        wick = xh - np.maximum(xo, xc)
        out[f"wick_{side}"] = wick
        out[f"wick_body_{side}"] = wick / np.where(body > 0, body, np.nan)
        out[f"wick_range_{side}"] = wick / safe
        out[f"reversal_atr_{side}"] = (xh - xc) / series.atr_prior
    return pd.DataFrame(out, index=pd.to_datetime(series.close_time, utc=True))


# --------------------------------------------------------------------------- breach / breakout

BREACH_COLUMNS = [
    "event_id", "primitive", "side", "level_id", "level_kind", "level_price", "bar_idx",
    "bar_ns", "close_ns", "available_ns", "breach_kind", "overshoot", "overshoot_bps",
    "overshoot_atr", "close_vs_level_bps", "atr", "rel_volume", "outcome", "resolve_idx",
    "resolve_ns", "resolve_close_ns", "resolve_available_ns", "failure_delay_bars",
    "bars_beyond", "max_excursion", "max_excursion_bps", "max_excursion_atr", "extreme",
    "close_back", "close_back_bps", "close_back_atr", "ret_breach_bar", "ret_to_resolve",
    "close_price", "resolve_close_price", "inputs_live",
]  # fmt: skip


_FLOAT_BREACH = [
    "level_price", "overshoot", "overshoot_bps", "overshoot_atr", "close_vs_level_bps", "atr",
    "rel_volume", "failure_delay_bars", "bars_beyond", "max_excursion", "max_excursion_bps",
    "max_excursion_atr", "extreme", "close_back", "close_back_bps", "close_back_atr",
    "ret_breach_bar", "ret_to_resolve", "close_price", "resolve_close_price",
]  # fmt: skip


def breaches(levels: pd.DataFrame, series: BarSeries, params: BreakoutParams) -> pd.DataFrame:
    """``level_breach_v1`` + ``breakout_outcome_v1``: one row per level that was breached on
    ``series`` by a qualifying overshoot.

    Breach (high side): the FIRST eligible bar b with ``high[b] > level``. The level is
    retired at that bar whatever happens next (one breach per level). It is an event only
    if ``high[b] - level >= max(min_excursion_bps x |level|, min_excursion_atr x prior
    ATR)``; a smaller poke retires the level silently (``sub_threshold`` in diagnostics).
    ``breach_kind``: CLOSE_BEYOND if ``close[b] > level`` else WICK_ONLY.

    Outcome within ``failure_window`` w (inside = close <= level):
    - FAILED at b+k (k = 0..w): the first close back inside; WICK_ONLY is FAILED at k = 0;
    - HELD at b+w: every close b..b+w beyond (a sustained breakout);
    - GAP: a data gap inside the window before resolution; UNRESOLVED: the data ends first.
    The outcome is known at ``ready_at`` of its resolving bar.
    """
    bp: BreachParams = params.breach
    w = params.failure_window
    prov = provenance(series, event=series)
    rows: list[dict] = []
    n = len(series)
    for side in SIDES:
        part = levels[levels["side"] == side]
        if part.empty:
            continue
        _xo, xh, _xl, xc = series.oriented(side)
        s = sign(side)
        a, b = eligible_range(part, series)
        cols = zip(part["oprice"].to_numpy(float), part["level_id"].to_numpy(),
                   part["kind"].to_numpy(), part["price"].to_numpy(float),
                   part["available_ns"].to_numpy(np.int64), part["inputs_live"].to_numpy(bool),
                   a, b, strict=True)  # fmt: skip
        for L, lid, kind, price, lavail, llive, a_, b_ in cols:
            hit = np.flatnonzero(xh[a_:b_] > L)
            if not len(hit):
                continue
            i = a_ + int(hit[0])
            over = xh[i] - L
            need_atr = bp.min_excursion_atr * series.atr_prior[i]
            if bp.min_excursion_atr and not np.isfinite(need_atr):
                continue  # not evaluable: no ATR yet
            need = max(
                bp.min_excursion_bps * abs(L) / 1e4, need_atr if bp.min_excursion_atr else 0.0
            )
            if over < need or over <= 0:
                continue
            known = max(int(series.ready_at[i]), int(lavail))
            outcome, res = "UNRESOLVED", -1
            for k in range(w + 1):
                j = i + k
                if j >= n:
                    break
                if series.segment[j] != series.segment[i]:
                    outcome = "GAP"
                    break
                if xc[j] <= L:
                    outcome, res = "FAILED", j
                    break
                if k == w:
                    outcome, res = "HELD", j
            atr = series.atr_prior[i]
            row = {
                "side": side,
                "level_id": lid,
                "level_kind": kind,
                "level_price": float(price),
                "bar_idx": i,
                "bar_ns": int(series.open_time[i]),
                "close_ns": int(series.close_time[i]),
                "available_ns": known,
                "breach_kind": "CLOSE_BEYOND" if xc[i] > L else "WICK_ONLY",
                "overshoot": over,
                "overshoot_bps": over / abs(L) * 1e4,
                "overshoot_atr": over / atr,
                "close_vs_level_bps": (xc[i] - L) / abs(L) * 1e4,
                "atr": atr,
                "rel_volume": series.rel_volume[i],
                "outcome": outcome,
                "resolve_idx": res,
                "inputs_live": bool(llive)
                and bool(series.inputs_live(np.array([a_]), np.array([max(i, res)]))[0]),
                "ret_breach_bar": series.c[i] / series.c[i - 1] - 1 if i > 0 else np.nan,
                "close_price": series.c[i],
            }
            if res >= 0:
                ext = float(xh[i : res + 1].max())
                row.update(
                    resolve_ns=int(series.open_time[res]),
                    resolve_close_ns=int(series.close_time[res]),
                    resolve_available_ns=max(int(series.ready_at[res]), known),
                    failure_delay_bars=res - i if outcome == "FAILED" else np.nan,
                    bars_beyond=res - i if outcome == "FAILED" else res - i + 1,
                    max_excursion=ext - L,
                    max_excursion_bps=(ext - L) / abs(L) * 1e4,
                    max_excursion_atr=(ext - L) / atr,
                    extreme=s * ext,
                    close_back=(L - xc[res]) if outcome == "FAILED" else np.nan,
                    close_back_bps=(L - xc[res]) / abs(L) * 1e4 if outcome == "FAILED" else np.nan,
                    close_back_atr=(L - xc[res]) / atr if outcome == "FAILED" else np.nan,
                    ret_to_resolve=series.c[res] / series.c[i] - 1,
                    resolve_close_price=series.c[res],
                )
            rows.append(row)
    df = pd.DataFrame(rows)
    for col in BREACH_COLUMNS:
        if col not in df:
            df[col] = np.nan
    if df.empty:
        return df[BREACH_COLUMNS]
    df["primitive"] = "level_breach_v1"
    make = ids(prov, "level_breach_v1", params.breach)
    df["event_id"] = [make(lid, t) for lid, t in zip(df["level_id"], df["bar_ns"], strict=True)]
    for c in ("resolve_ns", "resolve_close_ns", "resolve_available_ns"):
        df[c] = df[c].fillna(NAT).astype(np.int64)
    for c in _FLOAT_BREACH:
        df[c] = df[c].astype(float)
    df = df[BREACH_COLUMNS].sort_values(["bar_ns", "side", "level_id"], kind="stable")
    for k, v in prov.items():
        df[k] = v
    return df.reset_index(drop=True)


def failed_breakouts(br: pd.DataFrame, params: BreakoutParams) -> pd.DataFrame:
    """``failed_breakout_v1`` ("sweep"): the FAILED breach outcomes as events of their own,
    timed at the failure bar (the close back inside), parent = the breach."""
    return _derived(br, br["outcome"] == "FAILED", "failed_breakout_v1", params)


def held_breakouts(br: pd.DataFrame, params: BreakoutParams) -> pd.DataFrame:
    """Sustained breakouts: HELD outcomes, timed at the last bar of the window."""
    return _derived(br, br["outcome"] == "HELD", "held_breakout_v1", params)


def _derived(br: pd.DataFrame, mask, version: str, params) -> pd.DataFrame:
    d = br[mask].copy()
    d["parent_id"] = d["event_id"]
    d["primitive"] = version
    d["breach_bar_ns"] = d["bar_ns"]
    d["bar_idx"] = d["resolve_idx"].astype(int)
    d["bar_ns"] = d["resolve_ns"]
    d["close_ns"] = d["resolve_close_ns"]
    d["available_ns"] = d["resolve_available_ns"]
    d["breach_close_price"] = d["close_price"]
    d["close_price"] = d["resolve_close_price"]
    if d.empty:
        d["event_id"] = pd.Series(dtype=object)
        return d.reset_index(drop=True)
    prov_cols = [c for c in ("venue", "coin", "dataset_key", "availability_mode",
                             "assumed_latency_s", "event_tf") if c in d]  # fmt: skip
    first = d.iloc[0]
    make = ids({c: first[c] for c in prov_cols}, version, params)  # constant per table
    d["event_id"] = [make(p, t) for p, t in zip(d["parent_id"], d["bar_ns"], strict=True)]
    return d.reset_index(drop=True)


# --------------------------------------------------------------------------- rejection


def rejections(sweeps: pd.DataFrame, series: BarSeries, params: RejectionParams) -> pd.DataFrame:
    """``rejection_v1``: for each failed breakout, the first bar from the breach bar to
    ``within_bars`` after the failure bar whose wick on the swept side is at least
    ``min_wick_range`` of its range. Known no earlier than the failed breakout itself.
    Status per sweep: REJECTION, NONE, UNRESOLVED (data ends inside the window)."""
    rows = []
    n = len(series)
    m = {k: v.to_numpy() for k, v in rejection_metrics(series).items()}
    cols = zip(sweeps["event_id"].to_numpy(), sweeps["side"].to_numpy(),
               sweeps["breach_bar_ns"].to_numpy(np.int64), sweeps["bar_idx"].to_numpy(np.int64),
               sweeps["available_ns"].to_numpy(np.int64), strict=True)  # fmt: skip
    for sid, side, breach_ns, f, savail in cols:
        wr = m[f"wick_range_{side}"]
        lo = int(np.searchsorted(series.open_time, breach_ns))
        f = int(f)
        hi = f + params.within_bars
        found = np.flatnonzero(wr[lo : min(hi, n - 1) + 1] >= params.min_wick_range)
        if len(found):
            k = lo + int(found[0])
            status = "REJECTION"
        else:
            k = -1
            status = "UNRESOLVED" if hi > n - 1 else "NONE"
        rows.append({"parent_id": sid, "side": side, "status": status, "bar_idx": k,
                     "bar_ns": int(series.open_time[k]) if k >= 0 else NAT,
                     "close_ns": int(series.close_time[k]) if k >= 0 else NAT,
                     "available_ns": max(int(series.ready_at[k]), int(savail)) if k >= 0 else NAT,
                     "wick_range": wr[k] if k >= 0 else np.nan,
                     "wick_body": m[f"wick_body_{side}"][k] if k >= 0 else np.nan,
                     "clv": m["clv"][k] if k >= 0 else np.nan,
                     "reversal_atr": m[f"reversal_atr_{side}"][k] if k >= 0 else np.nan,
                     "range_atr": m["range_atr"][k] if k >= 0 else np.nan,
                     "rel_volume": series.rel_volume[k] if k >= 0 else np.nan,
                     "close_price": series.c[k] if k >= 0 else np.nan})  # fmt: skip
    df = pd.DataFrame(rows, columns=["parent_id", "side", "status", "bar_idx", "bar_ns", "close_ns",
                                     "available_ns", "wick_range", "wick_body", "clv", "reversal_atr",
                                     "range_atr", "rel_volume", "close_price"])  # fmt: skip
    prov = provenance(series, event=series)
    make = ids(prov, "rejection_v1", params)
    df["primitive"] = "rejection_v1"
    df["event_id"] = [
        make(p, t) if s == "REJECTION" else None
        for p, t, s in zip(df["parent_id"], df["bar_ns"], df["status"], strict=True)
    ]
    for k, v in prov.items():
        df[k] = v
    return df


# --------------------------------------------------------------------------- structure shift

SHIFT_COLUMNS = [
    "event_id", "primitive", "parent_id", "status", "direction", "side", "sweep_bar_ns",
    "sweep_close_ns", "sweep_available_ns", "sweep_extreme", "structure_level_id",
    "structure_price", "structure_pivot_ns", "structure_available_ns", "bar_idx", "bar_ns",
    "close_ns", "available_ns", "delay_bars", "delay_hours", "move", "move_bps", "move_atr",
    "atr", "close_price",
]  # fmt: skip


def structure_shifts(
    sweeps: pd.DataFrame, event: BarSeries, confirm: BarSeries, params: StructureShiftParams
) -> pd.DataFrame:
    """``structure_shift_v1`` after each failed breakout.

    For a failed LOW breakout (bullish; the bearish case is the mirror): among swing HIGHS
    of ``confirm`` (``params.swing``) that were already known when the failed breakout was
    known and formed by its failure-bar close, take the latest one that is still intact (no
    close, or wick if ``break_on='wick'``, above it since it formed), searching back over at
    most 10 swings. It must be at least ``min_excursion_atr`` x ATR above the swept extreme
    (the lowest low from breach to failure); otherwise NO_REFERENCE.

    Then scan ``confirm`` bars opening at or after the failure-bar close, at most ``window``
    bars: the first bar closing (or trading) above the reference is the SHIFT, known at its
    ``ready_at``. A bar closing below the swept extreme first is INVALIDATED (checked before
    the break on the same bar: with OHLC we cannot know the wick came first). Otherwise
    EXPIRED, GAP or UNRESOLVED; NO_DATA if the confirmation series does not cover the
    failed breakout (e.g. 15m history shorter than 1h). The shift is never timed before its break bar.
    """
    prov = provenance(event, event=event, confirm=confirm)
    rows = []
    sw_cache = {}
    need = [
        "event_id",
        "side",
        "extreme",
        "close_ns",
        "available_ns",
        "bar_ns",
        "breach_bar_ns",
        "close_price",
    ]
    for r in sweeps[need].to_dict("records"):
        side = r["side"]
        opp = OPPOSITE[side]
        if opp not in sw_cache:
            t = swings(confirm, params.swing, opp)
            sw_cache[opp] = {c: t[c].to_numpy() for c in t.columns}
        sw = sw_cache[opp]
        _xo, xh, _xl, xc = confirm.oriented(opp)
        so = sign(opp)
        ext_o = so * float(r["extreme"])  # the swept extreme in the structure orientation
        fclose = int(r["close_ns"])
        known = int(r["available_ns"])
        base = {"parent_id": r["event_id"], "side": side,
                "direction": "bullish" if side == "low" else "bearish",
                "sweep_bar_ns": int(r["bar_ns"]), "sweep_close_ns": fclose,
                "sweep_available_ns": known, "sweep_extreme": float(r["extreme"])}  # fmt: skip
        # confirm bars fully closed by the failure close
        last = int(np.searchsorted(confirm.close_time, fclose, side="right")) - 1
        atr = confirm.atr[last] if last >= 0 else np.nan
        if last < 0 or confirm.open_time[0] > int(r["breach_bar_ns"]):
            rows.append({**base, "status": "NO_DATA"})  # the confirmation series starts later
            continue
        ref = None
        if len(sw["level_id"]):
            av = sw["available_ns"].astype(np.int64)
            fm = sw["formed_ns"].astype(np.int64)
            top = min(int(np.searchsorted(av, known, side="right")),
                      int(np.searchsorted(fm, fclose, side="right")))  # fmt: skip
            probe = xc if params.break_on == "close" else xh
            for q in range(top - 1, max(top - 11, -1), -1):
                L = float(sw["oprice"][q])
                a_ = int(sw["confirm_idx"][q]) + 1
                if a_ <= last and probe[a_ : last + 1].max() > L:
                    continue  # already broken before the sweep: not intact
                ref = q
                break
        if ref is None:
            rows.append({**base, "status": "NO_REFERENCE"})
            continue
        L = float(sw["oprice"][ref])
        lvl = {"structure_level_id": sw["level_id"][ref], "structure_price": float(sw["price"][ref]),
               "structure_pivot_ns": int(sw["origin_ns"][ref]),
               "structure_available_ns": int(sw["available_ns"][ref]), "atr": atr}  # fmt: skip
        if not np.isfinite(atr) and params.min_excursion_atr:
            rows.append({**base, **lvl, "status": "NO_REFERENCE"})
            continue
        if L - ext_o < params.min_excursion_atr * (atr if np.isfinite(atr) else 0.0):
            rows.append({**base, **lvl, "status": "NO_REFERENCE"})
            continue
        start = int(np.searchsorted(confirm.open_time, fclose, side="left"))
        status, hit = "UNRESOLVED", -1
        for k in range(params.window):
            i = start + k
            if i >= len(confirm):
                break
            # a missing confirmation bar (right after the failure or inside the window)
            if (k == 0 and confirm.open_time[i] - fclose >= confirm.step) or (
                k and confirm.segment[i] != confirm.segment[i - 1]
            ):
                status = "GAP"
                break
            if xc[i] < ext_o:
                status, hit = "INVALIDATED", i
                break
            if (xc[i] if params.break_on == "close" else xh[i]) > L:
                status, hit = "SHIFT", i
                break
            if k == params.window - 1:
                status, hit = "EXPIRED", i
        row = {**base, **lvl, "status": status}
        if status == "SHIFT":
            move = confirm.c[hit] - float(r["close_price"])
            row.update(bar_idx=hit, bar_ns=int(confirm.open_time[hit]), close_ns=int(confirm.close_time[hit]),
                       available_ns=max(int(confirm.ready_at[hit]), known),
                       delay_bars=hit - start + 1, close_price=confirm.c[hit],
                       delay_hours=(int(confirm.close_time[hit]) - fclose) / 3.6e12,
                       move=move, move_bps=move / float(r["close_price"]) * 1e4,
                       move_atr=move / atr if np.isfinite(atr) else np.nan)  # fmt: skip
        rows.append(row)
    df = pd.DataFrame(rows)
    for col in SHIFT_COLUMNS:
        if col not in df:
            df[col] = np.nan
    df["primitive"] = "structure_shift_v1"
    make = ids(prov, "structure_shift_v1", params)
    df["event_id"] = [
        make(p, t) if s == "SHIFT" else None
        for p, t, s in zip(df["parent_id"], df["bar_ns"], df["status"], strict=True)
    ]
    for c in ("bar_ns", "close_ns", "available_ns", "structure_pivot_ns", "structure_available_ns"):
        df[c] = df[c].fillna(NAT).astype(np.int64)
    df["bar_idx"] = df["bar_idx"].fillna(-1).astype(np.int64)
    df["delay_bars"] = df["delay_bars"].astype(float)
    df = df[SHIFT_COLUMNS]
    for k, v in prov.items():
        df[k] = v
    return df


# --------------------------------------------------------------------------- retest


def retests(breaks: pd.DataFrame, series: BarSeries, params: RetestParams) -> pd.DataFrame:
    """``retest_v1``: after a level is broken, the first return to within tolerance of it.

    ``breaks``: one row per break with ``event_id``, ``break_side`` ('high' = broke upward
    through a level that is now below price; 'low' mirrors), ``level_price``, ``close_ns``
    (close of the break bar) and ``available_ns``. Scanning ``series`` bars that open at or
    after the break close, at most ``max_delay`` bars: the first bar whose low (oriented)
    comes within ``tolerance`` above the level, or below it, is the RETEST.

    Recorded: bars/hours to retest; the closest approach before or at it (signed distance,
    positive = still beyond the level); penetration through the level on the retest bar;
    the first price inside the tolerance zone (the open if the bar gapped into it, else the
    zone edge); ``held`` = the retest bar closed back beyond the level (``>= level``).
    NO_RETEST after ``max_delay`` bars; UNRESOLVED/GAP as elsewhere. Known at the retest
    bar's ``ready_at``, never earlier than the break.
    """
    prov = provenance(series, event=series)
    rows = []
    for r in breaks.to_dict("records"):
        side = r["break_side"]
        s = sign(side)
        xo, _xh, xl, xc = series.oriented(side)
        L = s * float(r["level_price"])
        start = int(np.searchsorted(series.open_time, int(r["close_ns"]), side="left"))
        status, hit = "UNRESOLVED", -1
        closest = np.inf
        for k in range(params.max_delay):
            i = start + k
            if i >= len(series):
                break
            if (k == 0 and series.open_time[i] - int(r["close_ns"]) >= series.step) or (
                k and series.segment[i] != series.segment[i - 1]
            ):
                status = "GAP"
                break
            tol = float(
                tolerance_price(params.tolerance, np.array([L]), series.atr_prior[i : i + 1])[0]
            )
            if not np.isfinite(tol):
                status = "UNDEFINED"
                break
            closest = min(closest, xl[i] - L)
            if xl[i] <= L + tol:
                status, hit = "RETEST", i
                break
            if k == params.max_delay - 1:
                status = "NO_RETEST"
        row = {"parent_id": r["event_id"], "break_side": side, "level_price": float(r["level_price"]),
               "status": status,
               "closest_distance": closest if np.isfinite(closest) else np.nan,
               "closest_distance_bps": closest / abs(L) * 1e4 if np.isfinite(closest) else np.nan}  # fmt: skip
        if hit >= 0:
            atr = series.atr_prior[hit]
            touch = min(xo[hit], L + tol)
            row.update(
                bar_idx=hit,
                bar_ns=int(series.open_time[hit]),
                close_ns=int(series.close_time[hit]),
                available_ns=max(int(series.ready_at[hit]), int(r["available_ns"])),
                delay_bars=hit - start + 1,
                delay_hours=(int(series.close_time[hit]) - int(r["close_ns"])) / 3.6e12,
                closest_distance_atr=closest / atr,
                penetration=max(0.0, L - xl[hit]),
                penetration_bps=max(0.0, L - xl[hit]) / abs(L) * 1e4,
                penetration_atr=max(0.0, L - xl[hit]) / atr,
                first_retest_price=s * touch,
                held=bool(xc[hit] >= L),
                close_price=series.c[hit],
            )
        rows.append(row)
    cols = ["parent_id", "break_side", "level_price", "status", "bar_idx", "bar_ns", "close_ns",
            "available_ns", "delay_bars", "delay_hours", "closest_distance", "closest_distance_bps",
            "closest_distance_atr", "penetration", "penetration_bps", "penetration_atr",
            "first_retest_price", "held", "close_price"]  # fmt: skip
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df:
            df[c] = np.nan
    df = df[cols]
    for c in ("bar_ns", "close_ns", "available_ns"):
        df[c] = df[c].fillna(NAT).astype(np.int64)
    df["bar_idx"] = df["bar_idx"].fillna(-1).astype(np.int64)
    df["delay_bars"] = df["delay_bars"].astype(float)
    df["held"] = df["held"].astype("boolean")
    df["primitive"] = "retest_v1"
    make = ids(prov, "retest_v1", params)
    df["event_id"] = [
        make(p, t) if s == "RETEST" else None
        for p, t, s in zip(df["parent_id"], df["bar_ns"], df["status"], strict=True)
    ]
    for k, v in prov.items():
        df[k] = v
    return df
