from __future__ import annotations

import os

import pytest
from scripts.storage_growth import attribution, category, inventory


def sample(at, files):
    return {"measured_at": at, "files": files, "free_bytes": 10000}


def file(path, size):
    return {"path": path, "allocated": size, "bytes": size}


def test_attribution_preserves_additions_removals_and_sampled_peak():
    old = "/data/raw/source/old.json.gz"
    new = "/data/raw/source/new.json.gz"
    result = attribution(
        [
            sample("2026-10-12T00:00:00+00:00", [file(old, 100)]),
            sample("2026-10-12T01:00:00+00:00", [file(old, 100), file(new, 50)]),
            sample("2026-10-12T02:00:00+00:00", [file(new, 70)]),
        ]
    )
    row = result["categories"]["provider_raw/source"]
    assert row["start_size"] == 100 and row["end_size"] == 70
    assert row["bytes_added"] == 70 and row["bytes_removed"] == 100
    assert row["net_growth"] == -30 and row["peak_size"] == 150
    assert row["file_count_change"] == 0 and row["net_bytes_per_day"] == -360


def test_inventory_skips_symlinks_and_counts_hardlink_allocation_once(tmp_path):
    p = tmp_path / "source"
    p.write_bytes(b"preserved")
    os.link(p, tmp_path / "second")
    (tmp_path / "symlink").symlink_to(p)
    (tmp_path / "directory-link").symlink_to(tmp_path, target_is_directory=True)
    result = inventory(tmp_path)
    assert len(result["files"]) == 2
    assert sum(f["allocated"] for f in result["files"]) == p.stat().st_blocks * 512
    assert result["errors"][0]["reason"] == "non-regular file"


def test_category_protects_active_parts_and_distinguishes_wal():
    assert category("/data/raw/provider/run.json.gz.part") == "temporary_files"
    assert category("/data/prism.duckdb.wal") == "duckdb_wal"
    assert category("/data/microstructure/lp/2026-10-12.json") == "large_print_calibration"
    with pytest.raises(ValueError, match="at least two"):
        attribution([])


def test_same_path_replacement_and_historical_numeric_timestamps():
    path = "/data/logs/runtime.log"
    first = sample(1791763200, [{**file(path, 100), "inode": 1}])
    second = sample(1791766800, [{**file(path, 100), "inode": 2}])
    row = attribution([first, second])["categories"]["logs"]
    assert row["net_growth"] == 0
    assert row["bytes_added"] == row["bytes_removed"] == 100


def test_read_only_duckdb_storage_queries_are_supported():
    import duckdb

    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE sample AS SELECT 1 AS value")
        assert con.execute(
            "SELECT block_id,compression FROM pragma_storage_info(?)", ["sample"]
        ).fetchall()
        assert con.execute("PRAGMA database_size").fetchone()
        con.execute("CREATE VIEW derived AS SELECT * FROM sample")
        assert con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog=current_database() AND table_schema='main' AND table_type='BASE TABLE'"
        ).fetchall() == [("sample",)]


def test_provider_captured_day_report_excludes_partial_day_and_preserves_compression():
    from scripts.raw_growth_report import report

    def row(day, size):
        return {
            "source_path": f"/data/raw/fred/{day}/run_example.json.gz",
            "source_provider": "fred",
            "first_timestamp": f"{day[:4]}-{day[4:6]}-{day[6:]}T01:00:00+00:00",
            "source_size": size,
            "read_test": {"logical_size": size * 10},
            "eligibility": {"export_eligible": True},
        }

    rows = [row("20261011", 100), row("20261012", 900)]
    result = report(
        {"rows": rows, "errors": []},
        {"measured_at": "2026-10-12T12:00:00+00:00", "files": []},
    )
    assert result["sources"][0]["last_complete_day"]["bytes"] == 100
    assert result["sources"][0]["compression_ratio"] == 10
    assert result["sources"][0]["seven_day_max_bytes"] == 100
    assert result["sources"][0]["eligible_bytes"] == 1000
