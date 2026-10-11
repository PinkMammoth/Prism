"""Targeted adapters preserve the exact 28 frozen predicates and knowledge-time rules."""

from __future__ import annotations

import json
import math
from dataclasses import replace

import pandas as pd

from market_signal.data.store import authority_claim
from market_signal.intraday.bars import load_bars
from market_signal.microstructure.query import load_microstructure
from market_signal.microstructure.spool import default_root
from market_signal.models.domain import Timeframe
from market_signal.paper.v2.data import Sources as BaselineSources
from market_signal.paper.v2.data import clean, ts

from . import spec
from .model import ExecutableQuote, StateUpdate


class Sources(BaselineSources):
    def observations(self, hypotheses, activated_at, *, assets):
        observations = super().observations(
            hypotheses, activated_at, assets=assets, ledger="paper_nimble_opportunities"
        )
        positioning_ids = {h.hypothesis_id for h in hypotheses if h.source_phase == 23}
        result = []
        for o in observations:
            if o.hypothesis_id in positioning_ids:
                captured = self.store.con.execute(
                    "SELECT max(captured_at) FROM context_hl_oi_hourly "
                    "WHERE coin=? AND captured_at<=?",
                    [o.asset, self.now.to_pydatetime()],
                ).fetchone()[0]
                if captured:
                    o = replace(o, available_at=max(ts(o.signal_at), ts(captured)).isoformat())
            result.append(o)
        return result

    def geometry(self, asset, observation):
        df = load_microstructure(
            self.store,
            asset,
            self.now - pd.Timedelta(minutes=16),
            self.now,
            known_at=self.now,
            availability="ingested",
            production_only=True,
        )
        df = df.tail(15)
        if (
            len(df) < 15
            or not df["complete"].all()
            or not pd.to_datetime(df["minute_close"], utc=True)
            .diff()
            .dropna()
            .eq(pd.Timedelta(minutes=1))
            .all()
        ):
            h = next(
                h for h in spec.baseline.bootstrap() if h.hypothesis_id == observation.hypothesis_id
            )
            if h.source_phase == 24:
                raise ValueError("GEOMETRY_UNAVAILABLE")
            timeframe = Timeframe.H1 if h.source_phase == 22 else Timeframe.M15
            bars = load_bars(
                self.store,
                "hyperliquid",
                asset,
                timeframe,
                start=self.now - pd.Timedelta(days=2),
                end=self.now,
                known_at=self.now,
            )
            if bars.empty:
                raise ValueError("GEOMETRY_UNAVAILABLE")
            last = bars.iloc[-1]
            if self.now - ts(last["close_time"]) > pd.Timedelta(
                minutes=80 if h.source_phase == 22 else 20
            ):
                raise ValueError("GEOMETRY_UNAVAILABLE")
            atr = (observation.evidence or {}).get("atr")
            volatility = (
                math.expm1(atr)
                if atr is not None and math.isfinite(atr) and atr > 0
                else (last["high"] - last["low"]) / last["close"]
            )
            if not math.isfinite(volatility) or volatility <= 0:
                raise ValueError("GEOMETRY_UNAVAILABLE")
            return clean(
                {
                    "local_low": last["low"],
                    "local_high": last["high"],
                    "volatility_move": volatility,
                    "available_at": max(ts(last["close_time"]), ts(last["first_observed_at"])),
                    "geometry_source": f"causal_{timeframe.value}_bar_fallback",
                    "microstructure": {},
                }
            )
        hi, lo = df["high_px"].max(), df["low_px"].min()
        mid = df["mid_end"].iloc[-1]
        prev = df["mid_end"].shift(1)
        tr = (
            pd.concat(
                [
                    df["high_px"] - df["low_px"],
                    (df["high_px"] - prev).abs(),
                    (df["low_px"] - prev).abs(),
                ],
                axis=1,
            )
            .max(axis=1)
            .mean()
        )
        volatility = 15**0.5 * tr / mid
        atr = (observation.evidence or {}).get("atr")
        if atr is not None and math.isfinite(atr) and atr > 0:
            volatility = math.expm1(atr)  # Phase 22 ATR is in log-price units.
        if not all(math.isfinite(x) and x > 0 for x in (hi, lo, mid, volatility)):
            raise ValueError("GEOMETRY_UNAVAILABLE")
        r = df.iloc[-1]
        return clean(
            {
                "local_low": lo,
                "local_high": hi,
                "volatility_move": volatility,
                "available_at": df["available_at"].max(),
                "microstructure": {
                    "bid5": r["bid5_end"],
                    "ask5": r["ask5_end"],
                    "delta": r["delta_ntl"],
                    "funding_rate": r["funding_end"],
                    "oi": r["oi_end"],
                    "feature_version": r["feature_version"],
                },
            }
        )

    def updates(self, assets):
        out = []
        for asset in assets:
            df = load_microstructure(
                self.store,
                asset,
                self.now - pd.Timedelta(minutes=4),
                self.now,
                known_at=self.now,
                availability="ingested",
                production_only=True,
            )
            df = df.tail(2)
            if (
                len(df) == 2
                and df["complete"].all()
                and ts(df["minute_close"].iloc[-1]) - ts(df["minute_close"].iloc[0])
                == pd.Timedelta(minutes=1)
            ):
                ntl = df["buy_ntl"] + df["sell_ntl"]
                fractions = ((df["buy_ntl"] - df["sell_ntl"]) / ntl.where(ntl > 0)).tolist()
                out.append(
                    StateUpdate(
                        asset,
                        ts(df["minute_close"].iloc[-1]).isoformat(),
                        ts(df["available_at"].max()).isoformat(),
                        clean(
                            {
                                "flow_fractions": fractions,
                                "mid": df["mid_end"].iloc[-1],
                                "bid5": df["bid5_end"].iloc[-1],
                                "ask5": df["ask5_end"].iloc[-1],
                                "funding_rate": df["funding_end"].iloc[-1],
                            }
                        ),
                    )
                )
            # Positioning is sampled hourly: do not fabricate minute-resolution crowding.
            ctx = self.context(asset)
            p = ((ctx.get("positioning") or {}).get("venues") or {}).get("hyperliquid", {})
            r = self.store.con.execute(
                "SELECT max(captured_at) FROM context_hl_oi_hourly WHERE coin=? AND captured_at<=?",
                [asset, self.now.to_pydatetime()],
            ).fetchone()[0]
            if r and self.now - ts(r) <= pd.Timedelta(minutes=80):
                out.append(
                    StateUpdate(
                        asset,
                        ts(r).isoformat(),
                        ts(r).isoformat(),
                        clean(
                            {
                                "crowding": (p.get("crowding") or {}).get("skew"),
                                "oi_change_1h_pct": p.get("oi_change_1h_pct"),
                                "mark": p.get("mark_px"),
                                "funding_rate": p.get("predicted_funding_hourly"),
                            }
                        ),
                    )
                )
        return out

    def research_quotes(self, assets, since):
        out = []
        for asset in assets:
            df = load_microstructure(
                self.store,
                asset,
                since,
                self.now,
                known_at=self.now,
                availability="ingested",
                production_only=True,
                complete_only=True,
            )
            for r in df.to_dict("records"):
                q = ExecutableQuote(
                    asset,
                    ts(r["minute_close"]).isoformat(),
                    ts(r["available_at"]).isoformat(),
                    r["bid_end"],
                    r["ask_end"],
                    kind="minute_book",
                    high=clean(r["high_px"]),
                    low=clean(r["low_px"]),
                    interval_start=ts(r["minute_open"]).isoformat(),
                )
                if q.valid(self.now, fresh=False):
                    out.append(q)
        return out


def live_cache(root, now, *, runtime_id=None):
    """Cheap filesystem-only quote monitor input; receipt/freshness checks in model.valid."""
    try:
        data = json.loads((root / "latest_books.json").read_text())
    except (OSError, ValueError):
        return [], {}
    if runtime_id and (data.get("role") != "authoritative" or data.get("runtime_id") != runtime_id):
        return [], {}
    if ts(data["published_at"]) > ts(now):
        return [], {}
    out = []
    for r in data.get("books", {}).values():
        q = ExecutableQuote(**r, available_at=data["published_at"])
        if q.valid(now):
            out.append(q)
    contexts = {
        a: v
        for a, v in data.get("contexts", {}).items()
        if 0 <= (ts(now) - ts(v["observed_at"])).total_seconds() <= 10
    }
    return out, contexts


def quotes_for_store(store, now):
    claim = authority_claim(store.con)
    qs, ctx = live_cache(default_root(store.path), now, runtime_id=claim[0] if claim else None)
    # Provenance is checked against collector process rows, not a gateway/user payload.
    sessions = {
        r[0]
        for r in store.con.execute(
            "SELECT run_id FROM microstructure_provider_runs "
            "WHERE kind='process' AND ended_at IS NULL"
        ).fetchall()
    }
    return [q for q in qs if q.session_id in sessions], ctx
