"""Read-only causal adapters. The paper engine receives values, never a venue client."""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from market_signal.intraday.align import Availability
from market_signal.intraday.bars import load_bars
from market_signal.microstructure.query import load_microstructure
from market_signal.models.domain import Timeframe
from market_signal.research.discovery.catalogue import IntradayStrategy
from market_signal.research.discovery.primitives import Frame
from market_signal.research.discovery.signals import SignalContext, signal_mask
from market_signal.research.microdir.features import flags, member_mask, windows
from market_signal.research.microdir.spec import HYPOTHESES
from market_signal.research.structure.series import BarSeries

from .spec import COINS, Hypothesis


def ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def clean(x):
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [clean(v) for v in x]
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    if isinstance(x, np.generic):
        return clean(x.item())
    if isinstance(x, float) and not np.isfinite(x):
        return None
    if x is pd.NaT:
        return None
    return x


def technical_ready(f, strategy) -> bool:
    """Exactly the primitives read by the frozen rule pairs; gaps remain undefined."""
    if len(f) < 2:
        return False
    rule = strategy.rule
    if rule == "ema_cross_trend":
        values = (f.ema(20), f.ema_slope(20, 3))
    elif rule == "ema_stretch_fade":
        values = (f.ema_dist_atr(20),)
    elif rule == "compression_breakout":
        boundary = f.roll_high if strategy.side == "long" else f.roll_low
        values = (f.compression(strategy.p["n"], 720), boundary(strategy.p["n"]))
    elif rule in ("expansion_continuation", "expansion_fade"):
        values = (f.tr_atr,)
    elif rule == "exhaustion":
        values = (f.ret_z(strategy.p["n"], 48),)
    else:
        raise ValueError("unreleased technical rule")
    return all(np.isfinite(v[-2:]).all() for v in values)


@dataclass(frozen=True)
class Observation:
    hypothesis_id: str
    asset: str
    signal_at: str
    available_at: str
    fired: bool
    healthy: bool = True
    warm: bool = True
    causal: bool = True
    context_available: bool = True
    evidence: dict | None = None
    context: dict | None = None
    maturity: str = "WARMUP"
    scientific_verdict: str | None = None
    prospective_sample_count: int = 0
    intended_side: str | None = None


@dataclass(frozen=True)
class Quote:
    asset: str
    at: str
    available_at: str
    price: float
    high: float
    low: float
    kind: str = "minute_mid"
    complete: bool = True


@dataclass(frozen=True)
class Funding:
    asset: str
    at: str
    available_at: str
    rate: float


class Sources:
    def __init__(self, store, now):
        self.store, self.now = store, ts(now)
        self._micro: dict = {}
        self.dependencies: dict = {}

    def micro(self, coin: str) -> pd.DataFrame:
        if coin not in self._micro:
            # Database consumer: a finalized spool minute cannot be acted on before ingest.
            self._micro[coin] = load_microstructure(
                self.store,
                coin,
                self.now - pd.Timedelta(days=8),
                self.now,
                known_at=self.now,
                availability="ingested",
                production_only=True,
            )
        return self._micro[coin]

    def quotes(self, since) -> list[Quote]:
        result = []
        for coin in COINS:
            df = self.micro(coin)
            m = (
                df[(df["status"] == "COMPLETE") & (df["minute_close"] >= ts(since))]
                if len(df)
                else df
            )
            if len(m):
                for r in m.to_dict("records"):
                    p = r["mid_end"]
                    if np.isfinite(p) and p > 0:
                        result.append(
                            Quote(
                                coin,
                                ts(r["minute_close"]).isoformat(),
                                ts(r["available_at"]).isoformat(),
                                p,
                                max(p, r["high_px"]) if np.isfinite(r["high_px"]) else p,
                                min(p, r["low_px"]) if np.isfinite(r["low_px"]) else p,
                            )
                        )
            # Fallback OHLCV is a distinct coarser execution path (15m horizons still exact).
            bars = load_bars(
                self.store,
                "hyperliquid",
                coin,
                Timeframe.M15,
                start=ts(since) - pd.Timedelta(minutes=15),
                end=self.now,
                known_at=self.now,
            )
            for r in bars.to_dict("records"):
                avail = max(ts(r["close_time"]), ts(r["first_observed_at"]))
                result.append(
                    Quote(
                        coin,
                        ts(r["open_time"]).isoformat(),
                        avail.isoformat(),
                        r["open"],
                        r["open"],
                        r["open"],
                        "bar_open",
                    )
                )
                result.append(
                    Quote(
                        coin,
                        ts(r["close_time"]).isoformat(),
                        avail.isoformat(),
                        r["close"],
                        r["high"],
                        r["low"],
                        "bar_close",
                    )
                )
        return sorted(result, key=lambda q: (ts(q.at), q.kind != "minute_mid"))

    def funding(self, since) -> list[Funding]:
        rows = self.store.con.execute(
            "SELECT coin,time,available_at,funding_rate FROM perp_funding WHERE source='hyperliquid' "
            "AND time >= ? AND time <= ? AND available_at <= ? ORDER BY time",
            [ts(since).to_pydatetime(), self.now.to_pydatetime(), self.now.to_pydatetime()],
        ).fetchall()
        return [Funding(a, ts(t).isoformat(), ts(av).isoformat(), r) for a, t, av, r in rows]

    def context(self, coin) -> dict:
        from market_signal.context.snapshot import context_snapshot

        return context_snapshot(self.store, coin, self.now.to_pydatetime(), record=False)

    def science(self, h: Hypothesis) -> dict:
        if h.source_phase != 24:
            return {"scientific_verdict": h.scientific_verdict}
        row = self.store.con.execute(
            "SELECT r.payload,c.checkpoint_id FROM lab_prospective_results r JOIN "
            "lab_prospective_checkpoints c USING(checkpoint_id) JOIN lab_prospective_studies s "
            "USING(study_id) WHERE s.name='microstructure_absorption_v1' AND r.status='COMPLETED' "
            "AND c.as_of <= ? AND r.completed_at <= ? ORDER BY c.as_of DESC LIMIT 1",
            [self.now.to_pydatetime(), self.now.to_pydatetime()],
        ).fetchone()
        if not row:
            return {}
        m = json.loads(row[0]).get("members", {}).get(f"{h.source_key}:{h.side}", {})
        return {
            "maturity": m.get("maturity", "WARMUP"),
            "scientific_verdict": (m.get("verdict") or {}).get("verdict")
            if isinstance(m.get("verdict"), dict)
            else m.get("verdict"),
            "prospective_sample_count": m.get(
                "episodes", (m.get("stats") or {}).get("episodes", 0)
            ),
            "checkpoint_id": row[1],
        }

    def observations(
        self, hypotheses, activated_at, *, assets=None, ledger="paper_v2_opportunities"
    ) -> list[Observation]:
        result = []
        self.dependencies = {"sources": {}, "context": {}}
        if ledger not in ("paper_v2_opportunities", "paper_nimble_opportunities"):
            raise ValueError("unsupported opportunity ledger")
        for coin in COINS if assets is None else assets:
            contexts = None
            tech = None
            micro_features = None
            for h in hypotheses:
                # One decision per source instant. No full feature rebuild between instants.
                at = self.now.floor(f"{h.cadence_minutes}min")
                if at <= ts(activated_at):
                    continue
                exists = self.store.con.execute(
                    f"SELECT 1 FROM {ledger} WHERE hypothesis_id=? AND asset=? "
                    "AND signal_at=? AND run_id=?",
                    [h.hypothesis_id, coin, at.to_pydatetime(), self.run_id],
                ).fetchone()
                if exists:
                    continue
                if contexts is None:
                    contexts = self.context(coin)
                ev, fired, warm, healthy = {}, False, False, False
                available = self.now
                if h.source_phase == 22:
                    if tech is None:
                        b = load_bars(
                            self.store,
                            "hyperliquid",
                            coin,
                            Timeframe.H1,
                            start=self.now - pd.Timedelta(days=45),
                            end=self.now,
                            known_at=self.now,
                        )
                        tech = (
                            BarSeries.from_frame(
                                b,
                                venue="hyperliquid",
                                coin=coin,
                                timeframe="1h",
                                availability=Availability.observed(),
                            )
                            if len(b)
                            else []
                        )
                    s = IntradayStrategy.model_validate(h.parameters["strategy"])
                    if len(tech):
                        i = len(tech) - 1
                        f = Frame.from_series(tech)
                        actual = ts(tech.close_time[i])
                        # A stale candle cannot masquerade as the current decision grid.
                        healthy = at == actual and self.now - actual <= pd.Timedelta(minutes=20)
                        warm = technical_ready(f, s)
                        fired = (
                            bool(
                                signal_mask(SignalContext(tf="1h", f=f, ready_at=tech.ready_at), s)[
                                    i
                                ]
                            )
                            if warm and healthy
                            else False
                        )
                        available = ts(tech.ready_at[i])
                        ev = {
                            "strategy_id": s.strategy_id,
                            "strategy_key": s.key,
                            "source_bar_close": actual.isoformat(),
                            "close": tech.c[i],
                            "atr": clean(f.atr[i]),
                            "ret_1h": clean(f.ret(1)[i]),
                        }
                    if "scientific_verdict_reasons" in h.parameters:
                        ev["scientific_verdict_reasons"] = h.parameters[
                            "scientific_verdict_reasons"
                        ]
                elif h.source_phase == 23:
                    p = (contexts.get("positioning") or {}).get("venues", {}).get("hyperliquid", {})
                    rows = self.store.con.execute(
                        "SELECT captured_at,mark_px FROM context_hl_oi_hourly WHERE coin=? "
                        "AND captured_at <= ? ORDER BY captured_at DESC LIMIT 2",
                        [coin, self.now.to_pydatetime()],
                    ).fetchall()
                    healthy = bool(rows) and self.now - ts(rows[0][0]) <= pd.Timedelta(minutes=80)
                    warm = len(rows) == 2 and ts(rows[0][0]) - ts(rows[1][0]) <= pd.Timedelta(
                        minutes=80
                    )
                    progress = (rows[0][1] / rows[1][1] - 1) * 100 if warm and rows[1][1] else None
                    oi = p.get("oi_change_1h_pct")
                    funding = p.get("predicted_funding_hourly")
                    skew = (p.get("crowding") or {}).get("skew")
                    orientation = 1 if h.side == "long" else -1
                    ev = {"positioning": p, "price_progress_1h_pct": progress}
                    if oi is None or progress is None:
                        warm = False
                    elif oi >= h.parameters["oi_change_1h_pct_min"]:
                        if h.source_key == "oi_directional_pressure":
                            fired = orientation * progress >= h.parameters["price_progress_1h_pct"]
                        elif h.source_key == "oi_price_stall":
                            fired = (
                                funding is not None
                                and orientation * funding < 0
                                and abs(progress) <= 0.1
                            )
                        else:
                            fired = (
                                skew == h.parameters["skew"]
                                and funding is not None
                                and -orientation * funding >= h.parameters["funding_hourly_extreme"]
                                and orientation * progress >= -0.1
                            )
                else:
                    if micro_features is None:
                        raw = self.micro(coin)
                        micro_features = windows(raw, coin)[0] if len(raw) else pd.DataFrame()
                    f = micro_features
                    if len(f):
                        ix = f.index[f["window_close"] == at]
                        if len(ix):
                            i = ix[-1]
                            row = f.loc[i]
                            healthy = bool(row["complete"])
                            warm = bool(row["eligible"])
                            src = next(x for x in HYPOTHESES if x.key == h.source_key)
                            fired = bool(member_mask(f, flags(f), src, h.side)[i])
                            available = max(ts(row["available_at"]), at)
                            ev = clean(row.to_dict())
                self.dependencies["sources"][f"{coin}:phase{h.source_phase}"] = {
                    "healthy": healthy,
                    "warm": warm,
                    "available_at": available.isoformat(),
                }
                science = self.science(h)
                ev["checkpoint_id"] = science.pop("checkpoint_id", None)
                result.append(
                    Observation(
                        h.hypothesis_id,
                        coin,
                        at.isoformat(),
                        max(at, available).isoformat(),
                        fired,
                        healthy,
                        warm,
                        intended_side=h.side,
                        evidence=ev,
                        context=contexts,
                        **science,
                    )
                )
        return result
