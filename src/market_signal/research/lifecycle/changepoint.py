"""Change detection: online (causal) versus retrospective (research only).

**Online** (``lifecycle_cusum_v1``) — usable for lifecycle decisions. Outcomes are grouped
into resolution-week blocks (``floor(t_res / 7 days)``); co-timed outcomes on different
assets share market shocks, so per-event increments would not be independent and one bad
week would look like many bad events. A block is processed only once it has fully elapsed.
Block b's mean m_b is standardised with the mean and standard deviation of the means of
blocks 0..b-1 only, and a one-sided Page CUSUM runs on the result:

    z_b = (m_b - ref_b) / scale_b
    S+_b = max(0, S+_{b-1} + z_b - k)       (upward shift)
    S-_b = max(0, S-_{b-1} - z_b - k)       (downward shift)

An alarm is raised when a statistic reaches h; that side then restarts from 0. The value at
time T depends only on outcomes resolved by T, so appending later outcomes can never change
an earlier alarm (tested).

- ``online_cusum``: diagnostic for edge profiles; ref = mean of all earlier block means
  ("has the edge moved away from its own history?").
- ``ActiveCusum``: used by the lifecycle machine while a strategy participates; ref = the
  one-standard-error lower bound of the recent mean that justified activation, floored at
  the economic floor ("has realised behaviour fallen below what activation expected?"). An
  alarm is a continuation failure (DEGRADED); together with a negative recent mean it is a
  hard failure.

**Retrospective** (``retrospective_binseg_v1``) — binary segmentation of the whole series
for a mean shift, accepted while the SSE reduction exceeds a BIC-style penalty
``2 * var * log(n)``. Every split uses outcomes on both sides, i.e. the future relative to
any interior point. It is reported for research, labelled ``retrospective``, and the
lifecycle simulation never calls it (tested).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import numpy as np

from market_signal.research.lifecycle.estimators import EventSet, iso
from market_signal.research.lifecycle.policy import Cusum


@dataclass(frozen=True)
class Blocks:
    """Resolution-week block means of an EventSet (contiguous because events are sorted by
    resolution time), with prefix sums for the causal moments of earlier blocks."""

    end: np.ndarray  # block end (days): the block is complete once T >= end
    mean: np.ndarray
    n: np.ndarray
    cum: np.ndarray
    cum2: np.ndarray

    @classmethod
    def of(cls, ev: EventSet, block_days: int) -> Blocks:
        if len(ev) == 0:
            z = np.array([])
            return cls(z, z, z, np.zeros(1), np.zeros(1))
        bid = np.floor(ev.t_res / block_days).astype(np.int64)
        starts = np.flatnonzero(np.r_[True, bid[1:] != bid[:-1]])
        sums = np.add.reduceat(ev.net, starts)
        n = np.diff(np.r_[starts, len(bid)])
        mean = sums / n
        end = (bid[starts] + 1).astype(float) * block_days
        return cls(end, mean, n, np.r_[0.0, np.cumsum(mean)], np.r_[0.0, np.cumsum(mean**2)])

    def complete(self, as_of: float) -> int:
        return int(np.searchsorted(self.end, as_of, side="right"))

    def prior(self, b: int) -> tuple[float, float]:
        mean = self.cum[b] / b
        var = max((self.cum2[b] - b * mean**2) / (b - 1), 0.0)
        return float(mean), float(np.sqrt(var))


def online_cusum(ev: EventSet, as_of: float, cfg: Cusum) -> dict:
    bl = Blocks.of(ev, cfg.block_days)
    nb = bl.complete(as_of)
    up = down = 0.0
    alarms = []
    for b in range(cfg.min_scale_blocks, nb):
        ref, scale = bl.prior(b)
        if scale <= 0:
            continue
        z = (bl.mean[b] - ref) / scale
        up = max(0.0, up + z - cfg.k)
        down = max(0.0, down - z - cfg.k)
        if up >= cfg.h:
            alarms.append({"block_end": iso(bl.end[b]), "direction": "up", "at": bl.end[b]})
            up = 0.0
        if down >= cfg.h:
            alarms.append({"block_end": iso(bl.end[b]), "direction": "down", "at": bl.end[b]})
            down = 0.0
    recent = as_of - cfg.alarm_memory_days

    def recent_alarm(direction: str) -> bool:
        return any(a["direction"] == direction and a["at"] > recent for a in alarms)

    return {"method": "lifecycle_cusum_v1", "unit": cfg.unit, "causal": True,
            "reference": "mean of all earlier block means", "k": cfg.k, "h": cfg.h,
            "blocks_processed": nb, "s_up": up, "s_down": down,
            "alarms": [{k: v for k, v in a.items() if k != "at"} for a in alarms],
            "recent_up_alarm": recent_alarm("up"),
            "recent_down_alarm": recent_alarm("down")}  # fmt: skip


class ActiveCusum:
    """Downward CUSUM against the activation reference (see module docstring)."""

    def __init__(self, blocks: Blocks, cfg: Cusum, reference: float, start: int):
        self.bl, self.cfg, self.reference = blocks, cfg, reference
        self.next = start  # first block not yet processed
        self.s = 0.0
        self.last_alarm: float | None = None

    def advance(self, as_of: float) -> None:
        """Process every block completed by ``as_of``."""
        nb = self.bl.complete(as_of)
        for b in range(self.next, nb):
            if b < self.cfg.min_scale_blocks:
                continue
            _, scale = self.bl.prior(b)
            if scale <= 0:
                continue
            z = (self.bl.mean[b] - self.reference) / scale
            self.s = max(0.0, self.s - z - self.cfg.k)
            if self.s >= self.cfg.h:
                self.s = 0.0
                self.last_alarm = float(self.bl.end[b])
        self.next = max(self.next, nb)

    def alarmed(self, as_of: float) -> bool:
        return self.last_alarm is not None and as_of - self.last_alarm <= self.cfg.alarm_memory_days


def retrospective_segments(ev: EventSet, as_of: float, max_changes: int = 3,
                           min_segment: int = 30) -> dict:  # fmt: skip
    """Offline mean-shift segmentation of outcomes resolved by ``as_of`` (uses the future of
    every interior point; research description only)."""
    k = ev.resolved(as_of)
    x = ev.net[:k]
    if k < 2 * min_segment:
        return {"method": "retrospective_binseg_v1", "retrospective": True,
                "segments": [], "note": "too few outcomes to segment"}  # fmt: skip
    penalty = 2 * float(np.var(x)) * np.log(k)
    cuts = [0, k]
    for _ in range(max_changes):
        best = None
        for a, b in pairwise(cuts):
            seg = x[a:b]
            if len(seg) < 2 * min_segment:
                continue
            cs = np.cumsum(seg)
            tot = cs[-1]
            m = np.arange(min_segment, len(seg) - min_segment + 1)
            left = cs[m - 1]
            gain = left**2 / m + (tot - left) ** 2 / (len(seg) - m) - tot**2 / len(seg)
            j = int(np.argmax(gain))
            if best is None or gain[j] > best[0]:
                best = (float(gain[j]), a + int(m[j]))
        if best is None or best[0] <= penalty:
            break
        cuts = sorted([*cuts, best[1]])
    segs = []
    for a, b in pairwise(cuts):
        segs.append({"first_resolved": iso(ev.t_res[a]), "last_resolved": iso(ev.t_res[b - 1]),
                     "n": int(b - a), "mean": float(x[a:b].mean())})  # fmt: skip
    return {"method": "retrospective_binseg_v1", "retrospective": True,
            "penalty": "2 * var * log(n)", "min_segment": min_segment, "segments": segs,
            "note": "uses outcomes after each split point; never an input to lifecycle "
                    "decisions"}  # fmt: skip
