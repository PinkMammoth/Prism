"""Read-only Railway audit payload; send with SSH, never install on production.

Prints only allowlisted runtime flags, labelled processes and bounded SQL evidence.
Does not disable jobs, checkpoint, migrate, create a backup or delete files.
"""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb


def resource_sample():
    result = {"at": datetime.now(UTC).isoformat(), "monotonic": time.monotonic(), "processes": []}
    labels = {
        "market microstructure collect": "microstructure_collector",
        "market_signal.context.gateway.launch": "context_gateway",
        "market_signal.context.gateway.worker": "context_ingest",
        "market_signal.context.free_sources.collector": "free_source_collector",
        "market_signal.context.free_sources.worker": "free_source_ingest",
        "market_signal.paper.nimble.worker": "nimble_worker",
        "supercronic": "scheduler",
        "market_signal.universe.worker": "universe_radar",
    }
    ticks = os.sysconf("SC_CLK_TCK")
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            rss = int(stat[21]) * os.sysconf("SC_PAGE_SIZE")
            name = (
                "audit_probe"
                if "python -c" in cmd
                else next((v for k, v in labels.items() if k in cmd), None)
            )
            # Do not emit arbitrary command lines, which may contain credentials.
            if name is None:
                name = "audit_probe" if "python -c" in cmd else (proc / "comm").read_text().strip()
            result["processes"].append(
                {
                    "pid": int(proc.name),
                    "name": name,
                    "state": stat[0],
                    "rss_bytes": rss,
                    "cpu_seconds": (int(stat[11]) + int(stat[12])) / ticks,
                    "start_ticks": int(stat[19]),
                    "write_bytes": next(
                        (
                            int(x.split()[1])
                            for x in (proc / "io").read_text().splitlines()
                            if x.startswith("write_bytes:")
                        ),
                        None,
                    ),
                }
            )
        except (OSError, ValueError, IndexError):
            continue
    for name in (
        "memory.current",
        "memory.peak",
        "memory.stat",
        "cpu.stat",
        "cpu.max",
        "memory.max",
    ):
        p = Path("/sys/fs/cgroup") / name
        if p.exists():
            result[name] = p.read_text().strip()
    return result


def main():
    from market_signal.ops import backups
    from market_signal.ops import runtime as rt

    db = Path("/data/prism.duckdb")
    out = {
        "revision": Path("/app/REVISION").read_text().strip(),
        "runtime_flags": {
            k: os.environ.get(k)
            for k in (
                "PRISM_PROFILE",
                "PRISM_AUTHORITY",
                "PRISM_DEPLOYMENT_TARGET",
                "PRISM_RUNTIME_ROLE",
                "PRISM_RUNTIME_ID",
                "PRISM_NIMBLE_RUNTIME",
                "PRISM_UNIVERSE_STAGE",
                "PRISM_MICROSTRUCTURE",
                "PRISM_CONTEXT_GATEWAY",
                "PRISM_FREE_SOURCES",
            )
        },
        "crontab": Path("/tmp/prism.crontab").read_text(),
        "backups": backups.inventory(Path("/data/backups"), db),
    }
    out["resource_start"] = resource_sample()
    with rt.runtime_lock(db, "research-cutover-read-only-audit", 45):  # noqa: SIM117
        with duckdb.connect(str(db), read_only=True) as con:

            def query(sql, args=()):
                cur = con.execute(sql, args)
                return [
                    dict(zip([d[0] for d in cur.description], row, strict=True))
                    for row in cur.fetchall()
                ]

            tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
            out["schema"] = query("SELECT max(version) AS version FROM schema_version")
            out["authority"] = list(rt.authority_claim(con))
            out["lifecycle"] = {}
            for table in sorted(tables):
                if table in (
                    "paper_runs",
                    "paper_v2_runs",
                    "paper_nimble_runs",
                    "paper_nimble_accounts",
                    "paper_nimble_research_pending",
                    "paper_execution_lifecycle",
                    "paper_v2_lifecycle",
                ):
                    out["lifecycle"][table] = query('SELECT * FROM "' + table + '"')
                if "lifecycle" in table or table in ("runtime_cycles", "runtime_steps"):
                    out.setdefault("columns", {})[table] = query(
                        'PRAGMA table_info("' + table + '")'
                    )
            out["nimble_positions"] = query(
                "SELECT run_id,state,count(*) AS n FROM paper_nimble_positions GROUP BY ALL"
            )
            out["unsettled_nimble"] = query(
                "SELECT run_id,trade_id,payload FROM paper_nimble_positions WHERE state='CLOSED' AND json_extract(payload,'$.accounting_final')=false"
            )
            out["pending_research_counts"] = query(
                "SELECT run_id,count(*) AS n FROM paper_nimble_research_pending GROUP BY ALL"
            )
            out["nimble_events"] = query(
                "SELECT run_id,event_type,count(*) AS n,max(recorded_at) AS latest FROM paper_nimble_events GROUP BY ALL"
            )
            out["cycles_48h"] = query(
                "SELECT job,status,count(*) AS n,avg(epoch(finished_at-started_at)) AS mean_seconds,max(epoch(finished_at-started_at)) AS max_seconds,max(started_at) AS latest FROM runtime_cycles WHERE started_at>=now()-INTERVAL '48 hours' GROUP BY ALL"
            )
            out["backup_cycles"] = query(
                "SELECT started_at,status,payload FROM runtime_cycles WHERE job='backup' ORDER BY started_at DESC LIMIT 3"
            )
            from types import SimpleNamespace

            from market_signal.paper import retirement
            from market_signal.paper.v2 import engine as v2

            store = SimpleNamespace(con=con)
            out["v1_work_needed"] = retirement.v1_work_needed(store)
            out["v2_work_needed"] = v2.work_needed(store)
            out["v2_run_states"] = query(
                "SELECT run_id,payload FROM paper_v2_events WHERE event_type='run_state' QUALIFY row_number() OVER(PARTITION BY run_id ORDER BY seq DESC)=1"
            )
            out["nimble_briefs"] = (
                query(
                    "SELECT run_id,count(*) AS n,max(created_at) AS latest FROM paper_nimble_briefs GROUP BY ALL"
                )
                if "paper_nimble_briefs" in tables
                else []
            )
            out["table_names"] = sorted(tables)
    time.sleep(30)
    out["resource_end"] = resource_sample()
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
