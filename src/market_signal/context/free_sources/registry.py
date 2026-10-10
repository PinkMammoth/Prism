"""Reviewed, zero-subscription source registry. Runtime health lives in the spool."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml

VERSION = "free_event_sources_v1"
PARSERS = {"statuspage", "rss", "github_releases", "github_advisories", "gdelt"}


def load(path: Path | None = None) -> dict:
    if path is None:
        from market_signal.config import find_project_root

        path = find_project_root() / "config/context/free_sources.v1.yaml"
    cfg = yaml.safe_load(path.read_text())
    if cfg["version"] != VERSION or len(cfg["sources"]) > 20:
        raise ValueError("unreviewed source registry")
    seen = set()
    for s in cfg["sources"]:
        if s["subscription_gbp_month"] != 0 or s["api_gbp_month"] != 0:
            raise ValueError("DATA_COST must equal £0")
        if s["id"] in seen or s["parser"] not in PARSERS:
            raise ValueError("duplicate provider or unsupported parser")
        seen.add(s["id"])
        u = urlsplit(s["url"])
        if u.scheme != "https" or not u.netloc or u.username or u.password:
            raise ValueError("public HTTPS sources only")
        if not 60 <= s["cadence_seconds"] <= 3600 or not 1 <= s["freshness_seconds"] <= 86400:
            raise ValueError("unsafe polling cadence or freshness")
        if s["enabled"] and s["access_review"] != "public_automated":
            raise ValueError("source access not verified")
        if u.hostname == "api.github.com" and s["cadence_seconds"] < 300:
            raise ValueError("GitHub free shared-IP budget exceeded")
    return cfg


def active(cfg, stage=None):
    if stage is None:
        import json

        from market_signal.context.free_sources.spool import default_root

        marker = default_root() / "rollout.json"
        stage = (
            json.loads(marker.read_text())["stage"]
            if marker.exists()
            else os.environ.get("PRISM_FREE_SOURCES_STAGE", "1")
        )
    stage = int(stage)
    if stage not in range(1, 6):
        raise ValueError("rollout stage must be 1..5")
    return [s for s in cfg["sources"] if s["enabled"] and s["stage"] <= stage]


def verify_cost_report(cfg, path=None):
    """Production cannot activate a changed source set with an old/paid cost report."""
    import json

    from market_signal.research.lab.common import content_id

    if path is None:
        from market_signal.config import find_project_root

        path = find_project_root() / "docs/evidence/phase29/free-source-cost-report.json"
    report = json.loads(path.read_text())
    if report["report"] != "FREE_SOURCE_COST_REPORT" or report[
        "registry_sources_sha256"
    ] != content_id("", cfg["sources"]):
        raise ValueError("FREE_SOURCE_COST_REPORT is missing or stale")
    audited = {s["provider"]: s for s in report["sources"]}
    for s in cfg["sources"]:
        if s["enabled"] and (
            s["id"] not in audited
            or audited[s["id"]]["DATA_COST"] != "£0"
            or audited[s["id"]]["subscription_gbp_month"] != 0
            or audited[s["id"]]["api_gbp_month"] != 0
        ):
            raise ValueError("source failed zero-data-cost gate")
    if report["total_incremental_subscription_gbp_month"] != 0:
        raise ValueError("nonzero incremental data subscription")
    return report
