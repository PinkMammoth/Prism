"""New definitions alongside, never in place of, frozen Phase 23 definitions."""

from market_signal.research.lab.common import content_id

PACKAGE_VERSION = "intelligence_context_read_v1"
PERSISTENCE_VERSION = "positioning_persistence_v1"

DEFINITION = {
    "version": PACKAGE_VERSION,
    "clock_bases": ["origin-observed", "origin-committed", "consumer-available"],
    "unknown_access": "withhold; processing/publication does not prove commit or receipt",
    "bar_versions": "latest accessible contiguous revision; closed bars; no forward fill",
    "event_versions": "accessible first report plus contiguous accessible updates and links",
    "source_semantics": "Phase23 taxonomy/crowding retained, separate package identity",
    "horizons": {"fast": 7, "daily": 30, "weekly": 90},
    "upcoming_days": 45,
    "breadth_hours": 24,
    "breadth_max_age_hours": 3,
    "breadth_min_assets": 3,
    "activity_lookback_days": 40,
    "positioning_lookback_days": 30,
    "funding_lookback_days": 120,
    "max_assets": 24,
    "max_rows": 50000,
    "max_package_bytes": 16 * 1024 * 1024,
    "persistence": {
        "version": PERSISTENCE_VERSION,
        "windows_hours": {"daily": 24, "weekly": 168},
        "oi_grid": "hourly as-of base OI; carry age at most 1h; require all endpoint/interior samples",
        "funding": "observed settlements in (cutoff-window, cutoff]; require cadence-complete path",
        "funding_cadence": "Hyperliquid 1h only; other venues require explicit cadence",
        "outputs": "coverage, OI trend, positive/negative/zero settled funding shares and sum",
        "interpretation": "descriptive only; no direction, liquidation, annualization or carry forecast",
    },
}
DEFINITION_ID = content_id("intelligencedef_", DEFINITION)
