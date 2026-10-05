"""The input contract for structural primitives: ONE venue's bars of ONE coin and timeframe.

Times are int64 nanoseconds UTC internally (fast, unambiguous) and Timestamps in outputs.

Availability (Phase 15 semantics, never re-derived here):

- ``available_at[i]``: when bar ``i`` itself was available, from ``intraday.align.Availability``
  (observed: ``max(close_time, first_observed_at)``; assumed: ``close_time + latency``);
- ``ready_at[i] = max(available_at[0..i])``: when EVERYTHING up to and including bar ``i``
  was available. Any measurement made at bar ``i`` (it may read earlier bars) is known at
  ``ready_at[i]``, never earlier. This is the availability of every state value and event.

Provenance: each series records its availability mode. ``assumed`` means every timestamp
derived from it rests on an assumed publication latency (backfilled history); ``observed``
means Prism's own first observation. ``live[i]`` says whether bar ``i`` was observed live.

Gaps: a missing grid bar starts a new ``segment``. Windows (swings, prior extremes, paths)
never span segments: a pattern across a hole in the data is undefined, not guessed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from market_signal.indicators import technical as ta
from market_signal.intraday.align import Availability
from market_signal.research.structure.registry import ATR_N, REL_VOLUME_N

TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
NS = 1_000_000_000


@dataclass(frozen=True)
class Provenance:
    venue: str
    coin: str
    timeframe: str
    availability_mode: str  # "observed" | "assumed"
    assumed_latency_s: float | None
    # Stable identity of WHAT is measured (not of the bytes): a registered Lab dataset ID,
    # or "store:<venue>" for direct store reads. Content hashes go in the build manifest,
    # so appending newer bars never changes the identity of an earlier event.
    dataset_key: str

    def as_dict(self) -> dict:
        return {"venue": self.venue, "coin": self.coin, "timeframe": self.timeframe,
                "availability_mode": self.availability_mode,
                "assumed_latency_s": self.assumed_latency_s, "dataset_key": self.dataset_key}  # fmt: skip


@dataclass(frozen=True, eq=False)
class BarSeries:
    prov: Provenance
    open_time: np.ndarray  # int64 ns
    close_time: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray
    available_at: np.ndarray  # int64 ns
    live: np.ndarray  # bool
    ready_at: np.ndarray = field(init=False)
    segment: np.ndarray = field(init=False)
    atr: np.ndarray = field(init=False)  # Wilder ATR(14) including bar i (known at ready_at[i])
    atr_prior: np.ndarray = field(init=False)  # the normaliser: ATR of bar i - 1
    rel_volume: np.ndarray = field(init=False)  # v[i] / mean(v[i-20..i-1])
    _low: tuple | None = field(init=False, default=None, repr=False)
    _dead: np.ndarray | None = field(init=False, default=None, repr=False)

    def __post_init__(self):
        n = len(self.open_time)
        for name in ("close_time", "o", "h", "l", "c", "v", "available_at", "live"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"column {name} has the wrong length")
        if n and not (np.all(np.diff(self.open_time) > 0)):
            raise ValueError("bars must have strictly increasing open times")
        if n and np.any(self.close_time <= self.open_time):
            raise ValueError("every bar must close after it opens")
        if n and np.any(self.available_at < self.close_time):
            raise ValueError("a bar cannot be available before it closes")
        bad = (self.h < np.maximum(self.o, self.c)) | (self.l > np.minimum(self.o, self.c))
        if np.any(bad):
            raise ValueError(f"{int(bad.sum())} bar(s) with high/low not bracketing open/close")
        step = TF_SECONDS[self.prov.timeframe] * NS
        seg = np.zeros(n, dtype=np.int64)
        if n:
            seg[1:] = np.cumsum(np.diff(self.open_time) != step)
        hi, lo, cl = (pd.Series(x) for x in (self.h, self.l, self.c))
        atr = ta.atr(hi, lo, cl, ATR_N).to_numpy(float)
        vol = pd.Series(self.v)
        rel = (vol / vol.shift(1).rolling(REL_VOLUME_N, min_periods=REL_VOLUME_N).mean()).to_numpy()
        for name, value in (
            ("ready_at", np.maximum.accumulate(self.available_at) if n else self.available_at),
            ("segment", seg),
            ("atr", atr),
            ("atr_prior", np.concatenate([[np.nan], atr[:-1]]) if n else atr),
            ("rel_volume", np.where(np.isfinite(rel), rel, np.nan)),
        ):
            object.__setattr__(self, name, value)

    def __len__(self) -> int:
        return len(self.open_time)

    @property
    def step(self) -> int:
        return TF_SECONDS[self.prov.timeframe] * NS

    # ------------------------------------------------------------------ constructors

    @classmethod
    def from_frame(
        cls,
        bars: pd.DataFrame,
        *,
        venue: str,
        coin: str,
        timeframe: str,
        availability: Availability,
        dataset_key: str | None = None,
    ) -> BarSeries:
        """From ``load_bars``-shaped rows: open_time, close_time, open, high, low, close,
        volume and (observed mode) first_observed_at / observed_live."""
        if timeframe not in TF_SECONDS:
            raise ValueError(f"unsupported timeframe {timeframe!r}")
        if "source" in bars and bars["source"].nunique() > 1:
            raise ValueError("one series is one venue: never mix venues")
        b = bars.sort_values("open_time").reset_index(drop=True)
        if availability.mode == "observed" and "first_observed_at" not in b:
            raise ValueError("observed availability needs first_observed_at")
        if b.empty:
            avail = pd.Series([], dtype="datetime64[ns, UTC]")
        else:
            avail = availability.available_at(b)
        live = (
            b["observed_live"].astype(bool)
            if "observed_live" in b
            else pd.Series(False, index=b.index)
        )
        prov = Provenance(
            venue=venue,
            coin=coin,
            timeframe=timeframe,
            availability_mode=availability.mode,
            assumed_latency_s=(
                availability.latency.total_seconds() if availability.mode == "assumed" else None
            ),
            dataset_key=dataset_key or f"store:{venue}",
        )
        return cls(
            prov=prov,
            open_time=_ns(b["open_time"]),
            close_time=_ns(b["close_time"]),
            o=b["open"].to_numpy(float),
            h=b["high"].to_numpy(float),
            l=b["low"].to_numpy(float),
            c=b["close"].to_numpy(float),
            v=b["volume"].to_numpy(float) if "volume" in b else np.full(len(b), np.nan),
            available_at=_ns(avail),
            live=live.to_numpy(bool),
        )

    @classmethod
    def from_store(
        cls, store, venue: str, coin: str, timeframe: str, start=None, end=None, *,
        availability: Availability, known_at=None,
    ) -> BarSeries:  # fmt: skip
        """Intraday bars of one venue/coin/timeframe from ``perp_intraday_bars``.

        ``known_at`` reconstructs the values Prism held at that instant (Phase 15 revisions)."""
        from market_signal.intraday.bars import load_bars

        bars = load_bars(store, venue, coin, timeframe, start, end, known_at=known_at)
        return cls.from_frame(bars, venue=venue, coin=coin, timeframe=timeframe,
                              availability=availability)  # fmt: skip

    # ------------------------------------------------------------------ views

    def head(self, n: int) -> BarSeries:
        """The first ``n`` bars (a strictly earlier dataset; for future-immunity checks)."""
        return BarSeries(
            prov=self.prov, open_time=self.open_time[:n], close_time=self.close_time[:n],
            o=self.o[:n], h=self.h[:n], l=self.l[:n], c=self.c[:n], v=self.v[:n],
            available_at=self.available_at[:n], live=self.live[:n],
        )  # fmt: skip

    def known_by(self, t) -> BarSeries:
        """Only the bars whose ``ready_at`` is at or before instant ``t``."""
        return self.head(int(np.searchsorted(self.ready_at, _ns1(t), side="right")))

    def oriented(self, side: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """(o, h, l, c) such that the ``side`` extreme is the HIGH.

        Every detector is written once, for the high side. The low side is the same code on
        the negated frame (o, h, l, c) -> (-o, -l, -h, -c): a low becomes a high, a close
        below becomes a close above. Bullish and bearish versions cannot diverge."""
        if side == "high":
            return self.o, self.h, self.l, self.c
        if side != "low":
            raise ValueError(f"side must be 'high' or 'low', not {side!r}")
        if self._low is None:  # computed once: detectors call this per event
            low = tuple(np.ascontiguousarray(-x) for x in (self.o, self.l, self.h, self.c))
            for x in low:
                x.setflags(write=False)
            object.__setattr__(self, "_low", low)
        return self._low

    def fingerprint(self) -> str:
        """SHA-256 of exactly the values a build read (for the manifest, not identities)."""
        hsh = hashlib.sha256()
        hsh.update(repr(self.prov.as_dict()).encode())
        for arr in (self.open_time, self.close_time, self.available_at):
            hsh.update(np.ascontiguousarray(arr, dtype="<i8").tobytes())
        for arr in (self.o, self.h, self.l, self.c, self.v):
            hsh.update(np.ascontiguousarray(arr, dtype="<f8").tobytes())
        hsh.update(self.live.astype("u1").tobytes())
        return hsh.hexdigest()

    def inputs_live(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """Whether every bar in each inclusive index range [a, b] was observed live."""
        if self._dead is None:  # prefix count of backfilled bars, built once
            object.__setattr__(self, "_dead", np.concatenate([[0], np.cumsum(~self.live)]))
        return (self._dead[np.asarray(b) + 1] - self._dead[np.asarray(a)]) == 0


def _ns(values) -> np.ndarray:
    idx = pd.DatetimeIndex(pd.to_datetime(values, utc=True))
    return idx.as_unit("ns").asi8.astype(np.int64)


def _ns1(t) -> int:
    ts = pd.Timestamp(t)
    if ts.tzinfo is None:
        raise ValueError(f"naive timestamp {t!r}: structural primitives need UTC instants")
    return int(ts.tz_convert("UTC").as_unit("ns").value)


NAT = np.iinfo(np.int64).min  # int64 ns "no time" (pandas' NaT)


def to_ts(ns) -> pd.Series | pd.Timestamp:
    """int64 ns (scalar or array; ``NAT`` becomes NaT) to UTC Timestamps."""
    if np.isscalar(ns):
        return pd.Timestamp(int(ns), tz="UTC")
    return pd.Series(pd.to_datetime(np.asarray(ns, dtype=np.int64), utc=True))


def assumed(latency_seconds: float = 0.0) -> Availability:
    return Availability.assumed(timedelta(seconds=latency_seconds))
