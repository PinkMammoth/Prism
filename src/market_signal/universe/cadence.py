"""UTC scheduling slots are orchestration metadata, never evidence timestamps."""

import fcntl
import math
from contextlib import contextmanager

from market_signal.universe.policy import POLICY

VERSION = "utc_600s_slots_v1"


def slot(at):
    return int(at // POLICY.cadence_seconds) * POLICY.cadence_seconds


def first_deadline(now):
    """Start/restart at the next boundary; never manufacture a missed observation."""
    return math.ceil(now / POLICY.cadence_seconds) * POLICY.cadence_seconds


def next_deadline(previous, now):
    """Skip expired deadlines explicitly, without overlapping or catch-up bursts."""
    target = previous + POLICY.cadence_seconds
    skipped = max(0, math.ceil((now - target) / POLICY.cadence_seconds))
    return target + skipped * POLICY.cadence_seconds, skipped


def due(previous, refresh_started):
    if not previous:
        return True
    prior = previous.get("schedule", {}).get("slot", slot(previous["at"]))
    return slot(refresh_started) > prior


@contextmanager
def refresh_lock(db):
    """One radar fetch through commit, independent of the short DB writer lock.

    Applies to supervised and manual refreshes. Kernel releases the lock on exit;
    restart cannot orphan an owner. No runtime DB lock is held during HTTP.
    """
    path = db.with_name(db.name + ".universe-refresh.lock")
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another universe refresh is already running") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
