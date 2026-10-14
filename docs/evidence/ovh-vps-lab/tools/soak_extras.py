"""Extra statistics for a truncated soak.jsonl (complements `vps_soak.py report`).

  python soak_extras.py SOAK.jsonl CLEANUP_ISO   -> JSON on stdout

Per-interval host CPU busy/steal (max/p95), load average max, RAM available min, per-unit RSS
peak, sampler cadence/gaps, and disk usage with the validation-cleanup step removed: usage
growth is reported before and after the cleanup instant separately, and Prism data growth is
taken from the per-category /data byte counters (which never included the test artefacts).
"""

import json
import sys
from datetime import datetime

rows = [json.loads(x) for x in open(sys.argv[1]) if x.strip()]
cleanup = datetime.fromisoformat(sys.argv[2])
at = [datetime.fromisoformat(r["at"]) for r in rows]
gaps = [(at[i + 1] - at[i]).total_seconds() for i in range(len(at) - 1)]
busy, steal = [], []
for a, b in zip(rows, rows[1:], strict=False):
    d = [y - x for x, y in zip(a["host"]["cpu_jiffies"], b["host"]["cpu_jiffies"], strict=False)]
    t = sum(d) or 1
    busy.append(1 - (d[3] + d[4]) / t)
    steal.append(d[7] / t)


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))]


used = [r["disk"]["total"] - r["disk"]["free"] for r in rows]
pre = [u for u, t in zip(used, at, strict=False) if t < cleanup]
post = [u for u, t in zip(used, at, strict=False) if t >= cleanup]
hours = (at[-1] - at[0]).total_seconds() / 3600
cats = {k: rows[-1]["bytes"].get(k, 0) - rows[0]["bytes"].get(k, 0) for k in rows[-1]["bytes"]}
rss = {}
for r in rows:
    for u, v in r["units"].items():
        m = v.get("MemoryCurrent")
        if isinstance(m, int) and m < 2**60:
            rss[u] = max(rss.get(u, 0), m)
print(json.dumps({
    "samples": len(rows), "from": rows[0]["at"], "to": rows[-1]["at"], "hours": round(hours, 3),
    "interval_s": {"min": min(gaps), "max": max(gaps), "expected": 300,
                   "gaps_over_360s": sum(g > 360 for g in gaps)},
    "cpu_busy": {"mean": sum(busy) / len(busy), "p95": pct(busy, 0.95), "max": max(busy)},
    "steal": {"mean": sum(steal) / len(steal), "p95": pct(steal, 0.95), "max": max(steal)},
    "loadavg1_max": max(r["host"]["loadavg"][0] for r in rows),
    "loadavg15_max": max(r["host"]["loadavg"][2] for r in rows),
    "mem_total": rows[0]["host"]["mem_total"],
    "mem_available_min": min(r["host"]["mem_available"] for r in rows),
    "mem_available_min_fraction": min(r["host"]["mem_available"] for r in rows) / rows[0]["host"]["mem_total"],
    "swap_used_max": max(r["host"]["swap_used"] for r in rows),
    "unit_memory_peak_bytes": rss,
    "disk": {
        "cleanup_at": cleanup.isoformat(),
        "used_first": used[0], "used_last_before_cleanup": pre[-1] if pre else None,
        "used_first_after_cleanup": post[0] if post else None, "used_last": used[-1],
        "validation_cleanup_step_bytes": (pre[-1] - post[0]) if pre and post else None,
        "free_min": min(r["disk"]["free"] for r in rows), "free_last": rows[-1]["disk"]["free"],
        "free_fraction_last": rows[-1]["disk"]["free"] / rows[-1]["disk"]["total"],
    },
    "prism_data_growth_bytes": cats,
    "prism_data_growth_bytes_per_day": {k: v / hours * 24 for k, v in cats.items()},
}, indent=1))
