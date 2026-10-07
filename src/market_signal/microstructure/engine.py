"""The collector state machine: normalized events in, finalized minute records out.

No network, no files, no clock of its own: every event carries its receipt time (Prism's
clock, epoch ms) and ``tick(now)`` drives finalization. ``feed`` ticks to the event's receipt
time *before* applying it, so the order "minute finalized, then this event arrived" depends
only on the event stream. The engine appends every applied event and every effective tick to
``journal``; replaying a journal through a fresh engine reproduces the same records exactly.

Normalized events (``recv`` = Prism receipt time, ``t`` = exchange time):

    ("O", recv, conn_id)                                connection opened
    ("A", recv, coin, feed)                             subscription acknowledged
    ("T", recv, coin, t, px, sz, is_buy, tid, hkey)     one fill (taker side from the venue)
    ("F", recv, coin, t, bid, ask, bid5_ntl, ask5_ntl)  fast 5-level book snapshot summary
    ("D", recv, coin, t, bid20_ntl, ask20_ntl)          20-level book snapshot summary
    ("C", recv, coin, oi, mark, oracle, funding, impact_bid, impact_ask)
    ("X", recv, reason)                                 connection closed / declared stale
    ("K", now)                                          tick (journal only)

Minute lifecycle per coin: open -> finalized at ``close + GRACE`` (record emitted, revision 0)
-> kept until ``close + GRACE + REVISION_WINDOW``: genuinely new late fills (never a duplicate
``tid``) are recorded and produce exactly one revision at that deadline -> evicted; later
fills for it are rejected and recorded. Nothing finalized is ever silently changed.
"""

from __future__ import annotations

import time
from bisect import bisect_right, insort
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from market_signal.microstructure import aggregate as agg
from market_signal.microstructure import definitions as d

DAY_MS = 86_400_000
TID_KEEP_MS = 20 * d.MINUTE_MS
SNAP_KEEP_MS = 3 * d.MINUTE_MS


def minute_of(t: int) -> int:
    return t - t % d.MINUTE_MS


def day_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, UTC).isoformat()


# --------------------------------------------------------------------------- large prints


class LargePrints:
    """Causal large-print thresholds (``LP_VERSION``) from per-day fine print histograms.

    ``days[day][coin] = {"bins": {bin: count}, "minutes": observed_minutes}``. The threshold
    for day D uses only days D-7 .. D-1, so it is fixed for the whole UTC day and never sees
    the day it classifies.
    """

    def __init__(self, days: dict[str, dict[str, dict]] | None = None):
        self.days: dict[str, dict[str, dict]] = days or {}
        self._cache: dict[tuple[str, str], dict] = {}

    def add(self, coin: str, day: str, fine: dict, observed: bool) -> None:
        c = self.days.setdefault(day, {}).setdefault(coin, {"bins": {}, "minutes": 0})
        c["minutes"] += int(observed)
        for k, n in fine.items():
            c["bins"][str(k)] = c["bins"].get(str(k), 0) + n

    def threshold(self, coin: str, day: str) -> dict:
        key = (coin, day)
        if key not in self._cache:
            self._cache[key] = self._compute(coin, day)
        return self._cache[key]

    def _compute(self, coin: str, day: str) -> dict:
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000
        prior = [day_str(int(d0 - i * DAY_MS)) for i in range(1, d.LP_DAYS + 1)]
        bins: Counter = Counter()
        used = 0
        for p in prior:
            c = self.days.get(p, {}).get(coin)
            if not c:
                continue
            bins.update({int(k): v for k, v in c["bins"].items()})
            used += c["minutes"] >= d.LP_MIN_DAY_MINUTES
        n = sum(bins.values())
        out = {"lp_version": d.LP_VERSION, "coin": coin, "day": day, "n_prints": n,
               "days_used": used, "threshold": None, "status": "warmup"}  # fmt: skip
        if used < d.LP_MIN_DAYS or n < d.LP_MIN_PRINTS:
            return out
        need, cum = d.LP_QUANTILE * n, 0
        for b in sorted(bins):
            cum += bins[b]
            if cum >= need:
                out.update(threshold=d.fine_upper_edge(b), status="ok")
                break
        return out


# --------------------------------------------------------------------------- engine


@dataclass
class _Final:
    rec: dict
    inputs: tuple  # (trades, b5, b20, ctx, trade_cov, lp, flags, first/last recv)
    deadline: int
    late: list = field(default_factory=list)


@dataclass
class CoinState:
    next_final: int  # open time of the next minute to finalize
    first_minute: int  # first minute this process observed
    open: dict[int, list] = field(default_factory=dict)  # minute -> fills
    dups: Counter = field(default_factory=Counter)  # minute -> duplicate fills dropped
    finals: dict[int, _Final] = field(default_factory=dict)
    seen: dict[int, int] = field(default_factory=dict)  # tid -> exchange time
    b5: list = field(default_factory=list)
    b20: list = field(default_factory=list)
    ctx: list = field(default_factory=list)
    live: list = field(default_factory=list)  # trades-feed live intervals [start, end|None]
    acked: set = field(default_factory=set)
    last: dict = field(default_factory=dict)  # feed -> last receipt time


class Engine:
    def __init__(self, coins: tuple[str, ...] | list[str], emit: Callable[[dict], None], *,
                 run_id: str, lp: LargePrints | None = None, journal: bool = True):  # fmt: skip
        self.coins = tuple(coins)
        self.emit = emit
        self.run_id = run_id
        self.lp = lp or LargePrints()
        self.keep_journal = journal
        self.journal: list[tuple] = []
        self.coin: dict[str, CoinState] = {}
        self.now = 0
        self.connected = False
        self.conn_id: str | None = None
        self.last_msg = 0
        self.conn_events: deque = deque(maxlen=200)  # (recv, "open"|"close")
        self.counters: Counter = Counter()
        self.latency: deque = deque(maxlen=4000)
        self.proc_us: deque = deque(maxlen=4000)
        self.last_final: dict[str, tuple[int, str]] = {}
        self.lp_emitted: set[tuple[str, str]] = set()
        self.started = False

    # ------------------------------------------------------------------ lifecycle

    def start(self, now: int, last_final: dict[str, int | None] | None = None) -> None:
        """Begin observing at ``now``. Minutes between the last finalized minute of a previous
        run and this start are recorded as explicit GAP rows (collector offline), at most
        ``GAP_FILL_MAX_MINUTES`` back; nothing is synthesized for them."""
        self.now = now
        m0 = minute_of(now)
        for c in self.coins:
            prev = (last_final or {}).get(c)
            first = m0 if prev is None else max(m0, prev + d.MINUTE_MS)
            self.coin[c] = CoinState(next_final=first, first_minute=first)
            if prev is not None:
                gap_from = max(prev + d.MINUTE_MS, m0 - d.GAP_FILL_MAX_MINUTES * d.MINUTE_MS)
                for m in range(gap_from, m0, d.MINUTE_MS):
                    self._emit_gap(c, m, now)
                self.last_final[c] = (prev, "previous run")
        self.started = True
        if self.keep_journal:
            self.journal.append(("S", now, {c: (last_final or {}).get(c) for c in self.coins}))

    def _emit_gap(self, coin: str, m: int, now: int) -> None:
        rec = self._record(coin, m, 0, {"trade_cov": 0.0, "book_samples": 0, "depth20_samples": 0,
                                        "status": "GAP"}, "collector_down", None, None, now, 0, 0)  # fmt: skip
        self.emit(rec)
        self.counters["gap_minutes"] += 1

    # ------------------------------------------------------------------ events

    def feed(self, ev: tuple) -> None:
        t0 = time.perf_counter()
        recv = ev[1]
        self.tick(recv)
        if self.keep_journal:
            self.journal.append(ev)
        kind = ev[0]
        self.counters[f"ev_{kind}"] += 1
        if kind not in ("X", "K"):
            self.last_msg = max(self.last_msg, recv)
        if kind == "T":
            self._trade(ev)
        elif kind == "F":
            self._book(ev, "book5")
        elif kind == "D":
            self._book(ev, "book20")
        elif kind == "C":
            st = self.coin.get(ev[2])
            if st is not None:
                st.ctx.append((recv, *ev[3:]))
                st.last["ctx"] = recv
        elif kind == "A":
            self._ack(ev)
        elif kind == "O":
            self.connected, self.conn_id = True, ev[2]
            self.conn_events.append((recv, "open"))
            self.counters["connections"] += 1
            for st in self.coin.values():
                st.acked.clear()
        elif kind == "X":
            self._closed(recv)
        self.proc_us.append((time.perf_counter() - t0) * 1e6)

    def _ack(self, ev: tuple) -> None:
        _, recv, coin, feed = ev
        st = self.coin.get(coin)
        if st is None or feed in st.acked:
            return
        st.acked.add(feed)
        if feed == "trades" and self.connected:
            st.live.append([recv, None])

    def _closed(self, recv: int) -> None:
        if not self.connected:
            return
        self.connected = False
        self.conn_events.append((recv, "close"))
        end = min(self.last_msg, recv) if self.last_msg else recv
        for st in self.coin.values():
            if st.live and st.live[-1][1] is None:
                st.live[-1][1] = max(st.live[-1][0], end)
            st.acked.clear()

    def _trade(self, ev: tuple) -> None:
        _, recv, coin, t, px, sz, is_buy, tid, hkey = ev
        st = self.coin.get(coin)
        if st is None:
            self.counters["invalid"] += 1
            return
        st.last["trades"] = recv
        m = minute_of(t)
        if tid in st.seen:
            self.counters["dup_trades"] += 1
            if m in st.open:
                st.dups[m] += 1
            return
        if t > self.now + 2 * d.MINUTE_MS or not (px > 0 and sz > 0):
            self.counters["invalid"] += 1
            return
        fill = (t, recv, px, sz, is_buy, tid, hkey)
        if m >= st.next_final:
            st.seen[tid] = t
            st.open.setdefault(m, []).append(fill)
            self.latency.append(recv - t)
            return
        f = st.finals.get(m)
        if f is not None and self.now < f.deadline:
            st.seen[tid] = t
            f.late.append(fill)
            self._emit_late(coin, m, fill, "revised")
            self.counters["late_revised"] += 1
        elif m >= st.first_minute:
            st.seen[tid] = t
            self._emit_late(coin, m, fill, "rejected")
            self.counters["late_rejected"] += 1
        else:  # before this run observed anything (e.g. the snapshot sent on subscribe)
            self.counters["replay_unobserved"] += 1

    def _book(self, ev: tuple, feed: str) -> None:
        coin, t = ev[2], ev[3]
        st = self.coin.get(coin)
        if st is None:
            self.counters["invalid"] += 1
            return
        st.last[feed] = ev[1]
        snap = (t, ev[1], *ev[4:])
        snaps = st.b5 if feed == "book5" else st.b20
        if minute_of(t) < st.next_final:
            self.counters[f"late_{feed}"] += 1  # the minute's book state is already final
            return
        if snaps and snaps[-1][0] == t and snaps[-1][2:] == snap[2:]:
            self.counters[f"dup_{feed}"] += 1
            return
        if snaps and t < snaps[-1][0]:
            self.counters[f"reordered_{feed}"] += 1
            insort(snaps, snap, key=lambda x: x[0])
        else:
            snaps.append(snap)
        if feed == "book5":
            self.latency.append(ev[1] - t)

    # ------------------------------------------------------------------ finalization

    def tick(self, now: int) -> bool:
        """Finalize every minute whose close + GRACE has passed and issue due revisions."""
        if not self.started:
            return False
        self.now = max(self.now, now)
        did = False
        for c, st in self.coin.items():
            while st.next_final + d.MINUTE_MS + d.GRACE_MS <= self.now:
                self._finalize(c, st, st.next_final)
                st.next_final += d.MINUTE_MS
                did = True
            for m in [m for m, f in st.finals.items() if f.deadline <= self.now]:
                f = st.finals.pop(m)
                if f.late:
                    self._revise(c, f)
                    did = True
            if did:
                self._prune(st)
        if did and self.keep_journal:
            self.journal.append(("K", self.now))
        return did

    def _coverage(self, st: CoinState, o: int, c: int) -> float:
        cov = 0
        for s, e in st.live:
            e = e if e is not None else (self.last_msg if self.connected else s)
            cov += max(0, min(e, c) - max(s, o))
        return min(cov / (c - o), 1.0)

    def _flags(self, st: CoinState, coin: str, m: int) -> str | None:
        fl = []
        if m == st.first_minute:
            fl.append("collector_start")
        lo, hi = m, m + d.MINUTE_MS + d.GRACE_MS
        if any(lo <= t < hi and k == "close" for t, k in self.conn_events):
            fl.append("disconnect")
        if any(lo <= t < hi and k == "open" for t, k in self.conn_events):
            fl.append("connect")
        return ",".join(fl) or None

    def _finalize(self, coin: str, st: CoinState, m: int) -> None:
        close = m + d.MINUTE_MS
        trades = st.open.pop(m, [])
        n_dup = st.dups.pop(m, 0)
        b5 = self._window(st.b5, m)
        b20 = self._window(st.b20, m)
        recvs = [x[1] for x in st.ctx]
        i = bisect_right(recvs, close - 1) - 1
        ctx = st.ctx[i] if i >= 0 else None
        cov = self._coverage(st, m, close)
        day = day_str(m)
        lp = self.lp.threshold(coin, day)
        if (coin, day) not in self.lp_emitted:
            self.lp_emitted.add((coin, day))
            self.emit(
                {"kind": "lp_threshold", **lp, "computed_ms": self.now, "run_id": self.run_id}
            )
        flags = self._flags(st, coin, m)
        fields, fine = agg.aggregate_minute(m, trades, b5, b20, ctx, cov, lp["threshold"])
        rv = [x[1] for x in trades] + [x[1] for x in b5 + b20 if m <= x[0] < close]
        span = (min(rv), max(rv)) if rv else (None, None)
        rec = self._record(coin, m, 0, fields, flags, *span, self.now, n_dup, 0, fine)
        self.emit(rec)
        self.lp.add(coin, day, fine, fields["status"] != "GAP")
        st.finals[m] = _Final(rec, (trades, b5, b20, ctx, cov, lp["threshold"], flags, span),
                              close + d.GRACE_MS + d.REVISION_WINDOW_MS)  # fmt: skip
        self.last_final[coin] = (m, fields["status"])
        self.counters[f"status_{fields['status']}"] += 1
        self.counters["finalized"] += 1

    def _revise(self, coin: str, f: _Final) -> None:
        trades, b5, b20, ctx, cov, lp, flags, span = f.inputs
        m = f.rec["minute_open"]
        fields, fine = agg.aggregate_minute(m, trades + f.late, b5, b20, ctx, cov, lp)
        rv = [x[1] for x in f.late] + [s for s in span if s is not None]
        fl = ",".join(x for x in [flags, "late_revision"] if x)
        rec = self._record(coin, m, 1, fields, fl, min(rv), max(rv), self.now,
                           f.rec["n_dup"], len(f.late), fine)  # fmt: skip
        self.emit(rec)
        self.counters["revisions"] += 1

    @staticmethod
    def _window(snaps: list, m: int) -> list:
        """Snapshots inside the minute, the last one before it (carry-in) and the first one
        after it (continuity proof for the final samples)."""
        times = [x[0] for x in snaps]
        lo = max(bisect_right(times, m - 1) - 1, 0)
        hi = bisect_right(times, m + d.MINUTE_MS - 1) + 1
        return snaps[lo:hi]

    def _prune(self, st: CoinState) -> None:
        horizon = st.next_final - SNAP_KEEP_MS
        for name in ("b5", "b20"):
            snaps = getattr(st, name)
            times = [x[0] for x in snaps]
            cut = max(bisect_right(times, horizon) - 1, 0)
            if cut:
                setattr(st, name, snaps[cut:])
        st.ctx = [x for x in st.ctx if x[0] >= horizon] or st.ctx[-1:]
        old = self.now - TID_KEEP_MS
        if len(st.seen) > 2000 or (st.seen and min(st.seen.values()) < old):
            st.seen = {k: v for k, v in st.seen.items() if v >= old}
        keep = []
        for s, e in st.live:
            if e is None or e >= horizon:
                keep.append([s, e])
        st.live = keep
        while self.conn_events and self.conn_events[0][0] < horizon:
            self.conn_events.popleft()

    def _record(self, coin: str, m: int, revision: int, fields: dict, flags: str | None,
                first_recv: int | None, last_recv: int | None, now: int, n_dup: int,
                n_late: int, fine: dict | None = None) -> dict:  # fmt: skip
        rec = {"kind": "minute", "feature_version": d.FEATURE_VERSION, "coin": coin,
               "minute_open": m, "revision": revision, "flags": flags}  # fmt: skip
        for col in d.MINUTE_COLUMNS:
            if col not in rec and col not in ("first_recv_at", "last_recv_at", "finalized_at",
                                              "ingested_at", "session_id", "content_sha"):  # fmt: skip
                rec[col] = fields.get(col)
        rec["n_dup"], rec["n_late"] = n_dup, n_late
        rec["first_recv_ms"], rec["last_recv_ms"], rec["finalized_ms"] = first_recv, last_recv, now
        rec["session_id"] = self.run_id
        rec["fh"] = fine or {}
        rec["content_sha"] = d.content_sha(rec)
        return rec

    def _emit_late(self, coin: str, m: int, fill: tuple, disposition: str) -> None:
        t, recv, px, sz, is_buy, tid, _h = fill
        self.emit({"kind": "late", "run_id": self.run_id, "feature_version": d.FEATURE_VERSION,
                   "coin": coin, "tid": tid,
                   "t": t, "recv": recv, "minute_open": m, "px": px, "sz": sz,
                   "is_buy": is_buy, "disposition": disposition})  # fmt: skip

    # ------------------------------------------------------------------ health

    def health(self, now: int) -> dict:
        lat = sorted(self.latency)
        pu = sorted(self.proc_us)

        def pct(a: list, q: float):
            return None if not a else round(a[min(int(q * len(a)), len(a) - 1)], 1)

        per = {}
        for c, st in self.coin.items():
            lf = self.last_final.get(c)
            per[c] = {"subscribed": sorted(st.acked),
                      "last_trade_age_s": None if "trades" not in st.last else round((now - st.last["trades"]) / 1000, 1),
                      "last_book5_age_s": None if "book5" not in st.last else round((now - st.last["book5"]) / 1000, 1),
                      "last_book20_age_s": None if "book20" not in st.last else round((now - st.last["book20"]) / 1000, 1),
                      "last_ctx_age_s": None if "ctx" not in st.last else round((now - st.last["ctx"]) / 1000, 1),
                      "last_finalized_minute": iso(lf[0]) if lf else None,
                      "last_finalized_status": lf[1] if lf else None}  # fmt: skip
        return {"connected": self.connected, "conn_id": self.conn_id,
                "last_message_age_s": None if not self.last_msg else round((now - self.last_msg) / 1000, 1),
                "coins": per, "counters": dict(self.counters),
                "latency_ms": {"p50": pct(lat, 0.5), "p90": pct(lat, 0.9), "p99": pct(lat, 0.99),
                               "n": len(lat)},
                "processing_us": {"p50": pct(pu, 0.5), "p99": pct(pu, 0.99)}}  # fmt: skip


def replay(journal: list[tuple], coins: tuple[str, ...], run_id: str,
           lp: LargePrints | None = None) -> list[dict]:  # fmt: skip
    """Re-run a journal through a fresh engine; returns the records it emits."""
    out: list[dict] = []
    eng = Engine(coins, out.append, run_id=run_id, lp=lp, journal=False)
    for ev in journal:
        if ev[0] == "S":
            eng.start(ev[1], ev[2])
        elif ev[0] == "K":
            eng.tick(ev[1])
        else:
            eng.feed(ev)
    return out
