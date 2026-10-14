"""Read-only Hyperliquid collector quality over an exact window, from a microstructure spool.

  python spool_compare.py SPOOL_DIR FROM_ISO TO_ISO   -> JSON on stdout

Minute statuses (revision 0, first record per coin/minute) and missing minutes per core coin,
flags, late prints, run starts, and every connection close with its reason, lifetime and the
disconnected interval until the next conn_open. Only reads files; never writes.
"""

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

CORE = ("AAVE", "BTC", "ETH", "HYPE", "LINK", "SOL")
root, t0, t1 = Path(sys.argv[1]), datetime.fromisoformat(sys.argv[2]), datetime.fromisoformat(sys.argv[3])
a, b = int(t0.timestamp() * 1000), int(t1.timestamp() * 1000)
seen, status, flags, late = {}, {c: Counter() for c in CORE}, Counter(), 0
conns, runs = [], 0
for f in sorted(root.glob("*/*.jsonl")):
    with f.open("rb") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            k = r.get("kind")
            if k == "minute":
                m = r.get("minute_open")
                if r.get("revision") != 0 or r.get("coin") not in status or not (a <= m < b):
                    continue
                if (r["coin"], m) in seen:
                    continue
                seen[(r["coin"], m)] = 1
                status[r["coin"]][r.get("status")] += 1
                for x in (r.get("flags") or "").split(","):
                    if x:
                        flags[x] += 1
            elif k in ("conn_open", "conn_close", "run_start", "late") and a <= r.get("at", 0) < b:
                if k == "late":
                    late += 1
                elif k == "run_start":
                    runs += 1
                else:
                    conns.append(r)
conns.sort(key=lambda r: r["at"])
closes = []
for i, r in enumerate(conns):
    if r["kind"] != "conn_close":
        continue
    nxt = next((x for x in conns[i + 1:] if x["kind"] == "conn_open"), None)
    closes.append({"at": datetime.fromtimestamp(r["at"] / 1000).isoformat(), "reason": r.get("reason"),
                   "lived_s": r.get("seconds"),
                   "down_s": round((nxt["at"] - r["at"]) / 1000, 1) if nxt else None})
expected = (b - a) // 60000
tot = Counter()
for c in CORE:
    tot.update(status[c])
present = sum(tot.values())
down = [c["down_s"] for c in closes if c["down_s"] is not None]
print(json.dumps({
    "window": [t0.isoformat(), t1.isoformat()], "expected_minutes_per_coin": expected,
    "status_total": dict(tot), "complete_fraction_of_expected": tot["COMPLETE"] / (expected * len(CORE)),
    "missing_minutes": expected * len(CORE) - present,
    "per_coin": {c: dict(status[c]) for c in CORE}, "flags": dict(flags), "late_prints": late,
    "run_starts": runs, "disconnects": len(closes),
    "disconnect_total_s": round(sum(down), 1), "disconnect_max_s": max(down, default=0),
    "closes": closes,
}, indent=1, default=str))
