"""Read-only health summary of the lab database (rehearsal). Opens read-only with a lock wait.

  python lab_db_health.py [/data/prism.duckdb]
"""

import json
import sys

from market_signal.data.store import Store

s = Store(sys.argv[1] if len(sys.argv) > 1 else "/data/prism.duckdb", read_only=True, lock_timeout=120)
c = s.con
tables = [t for (t,) in c.execute("SHOW TABLES").fetchall()]


def cols(t):
    return [x[0] for x in c.execute(f"DESCRIBE {t}").fetchall()]


out = {"tables": len(tables)}
for t in tables:
    if t.startswith("runtime_"):
        out.setdefault("runtime_tables", {})[t] = cols(t)
for t in ("runtime_cycles", "runtime_events"):
    if t in tables:
        out[t + "_count"] = c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        out[t + "_tail"] = [list(map(str, r)) for r in c.execute(f"SELECT * FROM {t}").fetchall()[-12:]]
if "microstructure_minutes" in tables:
    out["micro_minutes"] = list(map(str, c.execute(
        "SELECT count(*), min(minute_open), max(minute_open) FROM microstructure_minutes").fetchone()))
for t in ("perp_intraday_bars", "perp_funding", "perp_oi_history", "context_hl_oi_hourly", "context_ls_ratios"):
    if t in tables:
        tc = [x for x in cols(t) if x in ("ts", "at", "time", "open_time", "hour", "observed_at", "bar_open")]
        out[t] = {"rows": c.execute(f"SELECT count(*) FROM {t}").fetchone()[0],
                  "max_" + (tc[0] if tc else "?"): str(c.execute(f"SELECT max({tc[0]}) FROM {t}").fetchone()[0]) if tc else None}
out["paper_rows_excluding_policies"] = {
    t: n for t in tables if t.startswith("paper_") and t != "paper_nimble_policies"
    for n in [c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]] if n}
s.close()
print(json.dumps(out, indent=1, default=str))
