"""Is the data (and the last stored scan) current enough to act on?

A scheduled update can fail silently. Without this check, Today and the daily brief would
present an old answer as if it were today's. Rules (deliberately simple):

- crypto (24/7, daily bar closes 00:00 UTC): stale if the newest daily bar closed more
  than ``crypto_max_age`` ago (default 36h: one missed daily update).
- NYSE assets: stale if the newest daily bar is older than the most recent session close
  that should already be available (session close + ``nyse_grace`` for EOD providers).
- scan: stale if the newest stored scan is older than ``scan_max_age`` (default 30h).

Each asset is checked individually; a calendar group is *stale* when its newest asset is
behind (the whole update failed), and *partial* when only some assets are behind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from market_signal.config import Settings
from market_signal.data.calendars import is_nyse_session, nyse_close_utc
from market_signal.data.store import Store
from market_signal.models.domain import Calendar, utcnow


@dataclass
class GroupFreshness:
    name: str  # "crypto" | "equities & ETFs"
    newest: pd.Timestamp | None
    expected: pd.Timestamp  # the newest close that should already be stored
    behind: list[str] = field(default_factory=list)  # symbols missing the expected bar
    total: int = 0

    @property
    def stale(self) -> bool:
        return self.newest is None or self.newest < self.expected

    @property
    def partial(self) -> bool:
        return not self.stale and bool(self.behind)


@dataclass
class Freshness:
    now: pd.Timestamp
    groups: list[GroupFreshness]
    last_scan: pd.Timestamp | None
    scan_max_age: pd.Timedelta

    @property
    def data_stale(self) -> bool:
        return any(g.stale for g in self.groups)

    @property
    def scan_stale(self) -> bool:
        return self.last_scan is None or self.now - self.last_scan > self.scan_max_age

    def problems(self, include_scan: bool = True) -> list[str]:
        out = []
        for g in self.groups:
            if g.stale:
                age = (
                    "no data" if g.newest is None else f"newest bar {_age(self.now - g.newest)} old"
                )
                out.append(f"{g.name} prices are out of date ({age}).")
            elif g.partial:
                out.append(
                    f"{g.name}: {len(g.behind)} of {g.total} assets behind ({', '.join(g.behind[:6])})."
                )
        if include_scan and self.scan_stale:
            out.append("No scan stored in the last day." if self.last_scan is not None
                       else "No scan stored yet.")  # fmt: skip
        return out


def _age(d: pd.Timedelta) -> str:
    h = d.total_seconds() / 3600
    return f"{h:.0f}h" if h < 48 else f"{h / 24:.1f} days"


def latest_nyse_close(now: pd.Timestamp, grace: timedelta) -> pd.Timestamp:
    """Newest NYSE session close that an EOD provider should have published by ``now``."""
    d = now.tz_convert("America/New_York").date()
    for _ in range(15):
        if is_nyse_session(d) and pd.Timestamp(nyse_close_utc(d)) + grace <= now:
            return pd.Timestamp(nyse_close_utc(d))
        d -= timedelta(days=1)
    return pd.Timestamp(nyse_close_utc(d))


def check_freshness(
    store: Store,
    settings: Settings,
    now: pd.Timestamp | None = None,
    crypto_max_age: timedelta = timedelta(hours=36),
    nyse_grace: timedelta = timedelta(hours=8),
    scan_max_age: timedelta = timedelta(hours=30),
) -> Freshness:
    now = pd.Timestamp(now or utcnow())
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    try:
        inv = store.series_inventory()
        inv = inv[inv["timeframe"] == "1d"].groupby("symbol")["last_close"].max()
    except Exception:
        inv = pd.Series(dtype="datetime64[ns, UTC]")
    try:
        last_scan = store.query("SELECT max(created_at) t FROM scan_runs").iloc[0]["t"]
        last_scan = None if pd.isna(last_scan) else pd.Timestamp(last_scan).tz_convert("UTC")
    except Exception:
        last_scan = None
    crypto = GroupFreshness("Crypto", None, now - pd.Timedelta(crypto_max_age))
    nyse = GroupFreshness("Equities & ETFs", None, latest_nyse_close(now, nyse_grace))
    for a in settings.active_assets():
        g = crypto if a.calendar == Calendar.CRYPTO_24_7 else nyse
        g.total += 1
        last = inv.get(a.symbol)
        last = None if last is None or pd.isna(last) else pd.Timestamp(last).tz_convert("UTC")
        if last is None or last < g.expected:
            g.behind.append(a.symbol)
        if last is not None and (g.newest is None or last > g.newest):
            g.newest = last
    groups = [g for g in (crypto, nyse) if g.total]
    return Freshness(now, groups, last_scan, pd.Timedelta(scan_max_age))
