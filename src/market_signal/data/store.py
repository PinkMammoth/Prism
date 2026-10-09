"""DuckDB storage.

Schema is created by ordered, idempotent migrations (``MIGRATIONS``). All writes are
upserts keyed on natural primary keys, so re-running an update is a no-op. When a
provider *changes* a previously stored bar, the old values are copied to
``bar_revisions`` before being replaced, so revisions are visible, never silent.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from market_signal.data.http import RawPayload
from market_signal.models.domain import Asset, Timeframe, utcnow

MIGRATIONS: list[str] = [
    # 1 — core market data + provenance
    """
    CREATE TABLE IF NOT EXISTS assets (
        symbol VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL,
        asset_class VARCHAR NOT NULL,
        currency VARCHAR NOT NULL,
        calendar VARCHAR NOT NULL,
        active BOOLEAN NOT NULL,
        provider_ids JSON,
        series JSON,
        updated_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bars (
        symbol VARCHAR NOT NULL,
        timeframe VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        ts TIMESTAMPTZ NOT NULL,
        close_time TIMESTAMPTZ NOT NULL,
        open DOUBLE NOT NULL,
        high DOUBLE NOT NULL,
        low DOUBLE NOT NULL,
        close DOUBLE NOT NULL,
        volume DOUBLE,
        ingest_run_id VARCHAR NOT NULL,
        ingested_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (symbol, timeframe, source, ts)
    );
    CREATE TABLE IF NOT EXISTS bar_revisions (
        symbol VARCHAR, timeframe VARCHAR, source VARCHAR, ts TIMESTAMPTZ,
        old_open DOUBLE, old_high DOUBLE, old_low DOUBLE, old_close DOUBLE, old_volume DOUBLE,
        new_open DOUBLE, new_high DOUBLE, new_low DOUBLE, new_close DOUBLE, new_volume DOUBLE,
        old_ingest_run_id VARCHAR, new_ingest_run_id VARCHAR, revised_at TIMESTAMPTZ
    );
    CREATE TABLE IF NOT EXISTS corporate_actions (
        symbol VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        date DATE NOT NULL,
        split_factor DOUBLE NOT NULL,
        dividend DOUBLE NOT NULL,
        ingest_run_id VARCHAR NOT NULL,
        PRIMARY KEY (symbol, source, date)
    );
    CREATE TABLE IF NOT EXISTS ingestion_runs (
        run_id VARCHAR PRIMARY KEY,
        provider VARCHAR NOT NULL,
        dataset VARCHAR NOT NULL,
        entity VARCHAR,
        params JSON,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        status VARCHAR NOT NULL,
        rows_received INTEGER,
        rows_written INTEGER,
        rows_rejected INTEGER,
        raw_paths JSON,
        raw_sha256 JSON,
        schema_fingerprint VARCHAR,
        error VARCHAR
    );
    CREATE TABLE IF NOT EXISTS series_changes (
        symbol VARCHAR, timeframe VARCHAR, old_source VARCHAR, new_source VARCHAR,
        detected_at TIMESTAMPTZ, note VARCHAR
    );
    CREATE TABLE IF NOT EXISTS data_quality_issues (
        run_id VARCHAR,
        symbol VARCHAR,
        timeframe VARCHAR,
        source VARCHAR,
        check_name VARCHAR,
        severity VARCHAR,
        ts TIMESTAMPTZ,
        detail VARCHAR,
        detected_at TIMESTAMPTZ
    );
    """,
    # 2 — point-in-time macro + fundamentals
    """
    CREATE TABLE IF NOT EXISTS macro_observations (
        series_id VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        obs_date DATE NOT NULL,
        realtime_start DATE NOT NULL,
        realtime_end DATE,
        value DOUBLE,
        available_at TIMESTAMPTZ NOT NULL,
        pit_method VARCHAR NOT NULL,
        ingest_run_id VARCHAR NOT NULL,
        PRIMARY KEY (series_id, source, obs_date, realtime_start)
    );
    CREATE TABLE IF NOT EXISTS fundamental_facts (
        fact_key VARCHAR PRIMARY KEY,
        symbol VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        concept VARCHAR NOT NULL,
        unit VARCHAR NOT NULL,
        period_start DATE,
        period_end DATE NOT NULL,
        value DOUBLE NOT NULL,
        form VARCHAR,
        fy INTEGER,
        fp VARCHAR,
        filed DATE NOT NULL,
        accn VARCHAR,
        available_at TIMESTAMPTZ NOT NULL,
        pit_method VARCHAR NOT NULL,
        ingest_run_id VARCHAR NOT NULL
    );
    CREATE TABLE IF NOT EXISTS crypto_metrics (
        symbol VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        metric VARCHAR NOT NULL,
        obs_date DATE NOT NULL,
        value DOUBLE,
        available_at TIMESTAMPTZ NOT NULL,
        pit_method VARCHAR NOT NULL,
        fetched_at TIMESTAMPTZ NOT NULL,
        ingest_run_id VARCHAR NOT NULL,
        PRIMARY KEY (symbol, source, metric, obs_date, pit_method)
    );
    """,
    # 3 — scans, research, portfolio, journal, alerts
    """
    CREATE TABLE IF NOT EXISTS scan_runs (
        scan_id VARCHAR PRIMARY KEY,
        as_of TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        config_hash VARCHAR,
        data_fingerprint VARCHAR,
        regime JSON,
        summary JSON
    );
    CREATE TABLE IF NOT EXISTS scan_results (
        scan_id VARCHAR NOT NULL,
        symbol VARCHAR NOT NULL,
        setup VARCHAR NOT NULL,
        score DOUBLE,
        coverage DOUBLE,
        status VARCHAR,
        price DOUBLE,
        payload JSON,
        PRIMARY KEY (scan_id, symbol, setup)
    );
    CREATE TABLE IF NOT EXISTS research_runs (
        run_id VARCHAR PRIMARY KEY,
        name VARCHAR,
        kind VARCHAR,
        created_at TIMESTAMPTZ,
        config JSON,
        config_hash VARCHAR,
        data_fingerprint JSON,
        code_version VARCHAR,
        git_commit VARCHAR,
        summary JSON,
        report_path VARCHAR
    );
    CREATE TABLE IF NOT EXISTS positions (
        position_id VARCHAR PRIMARY KEY,
        book VARCHAR NOT NULL,          -- 'paper' | 'real'
        kind VARCHAR NOT NULL,          -- TRADE | INVESTMENT
        symbol VARCHAR NOT NULL,
        setup VARCHAR,
        entry_date DATE NOT NULL,
        entry_price DOUBLE NOT NULL,
        quantity DOUBLE NOT NULL,
        fees DOUBLE DEFAULT 0,
        thesis VARCHAR,
        evidence VARCHAR,
        horizon VARCHAR,
        invalidation_price DOUBLE,
        invalidation_note VARCHAR,
        score_at_entry DOUBLE,
        regime_at_entry VARCHAR,
        risk_at_entry DOUBLE,           -- portfolio fraction at risk to invalidation
        followed_system BOOLEAN,
        exit_date DATE,
        exit_price DOUBLE,
        exit_reason VARCHAR,
        realised_return DOUBLE,
        mae DOUBLE,
        mfe DOUBLE,
        outcome VARCHAR,
        lessons VARCHAR,
        created_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ
    );
    CREATE TABLE IF NOT EXISTS journal_entries (
        entry_id VARCHAR PRIMARY KEY,
        position_id VARCHAR,
        created_at TIMESTAMPTZ NOT NULL,
        kind VARCHAR,                   -- entry | review | exit | note
        text VARCHAR,
        followed_system BOOLEAN,
        tags JSON
    );
    CREATE TABLE IF NOT EXISTS alert_rules (
        rule_id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL,
        symbol VARCHAR,
        params JSON,
        active BOOLEAN NOT NULL,
        created_at TIMESTAMPTZ
    );
    CREATE TABLE IF NOT EXISTS alert_events (
        event_id VARCHAR PRIMARY KEY,
        rule_id VARCHAR,
        fired_at TIMESTAMPTZ,
        symbol VARCHAR,
        message VARCHAR,
        payload JSON,
        acknowledged BOOLEAN DEFAULT FALSE
    );
    """,
    # 4 — alert delivery: events are delivered (e.g. in the Telegram daily brief) exactly once,
    # whoever evaluated them (CLI scan or dashboard)
    """
    ALTER TABLE alert_events ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
    """,
    # 5 — perpetual futures (kept apart from spot bars; never stitched)
    """
    CREATE TABLE IF NOT EXISTS perp_bars (
        coin VARCHAR NOT NULL,
        timeframe VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        ts TIMESTAMPTZ NOT NULL,
        open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE,
        close_time TIMESTAMPTZ NOT NULL,
        ingested_at TIMESTAMPTZ NOT NULL,
        ingest_run_id VARCHAR NOT NULL,
        PRIMARY KEY (coin, timeframe, source, ts)
    );
    CREATE TABLE IF NOT EXISTS perp_funding (
        coin VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        time TIMESTAMPTZ NOT NULL,       -- funding settlement time
        funding_rate DOUBLE NOT NULL,    -- per funding period (Hyperliquid: hourly)
        premium DOUBLE,
        available_at TIMESTAMPTZ NOT NULL,
        pit_method VARCHAR NOT NULL,
        ingest_run_id VARCHAR NOT NULL,
        PRIMARY KEY (coin, source, time)
    );
    CREATE TABLE IF NOT EXISTS perp_snapshots (
        coin VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        snapshot_at TIMESTAMPTZ NOT NULL,
        mark_px DOUBLE, oracle_px DOUBLE, mid_px DOUBLE, prev_day_px DOUBLE,
        funding_rate DOUBLE,             -- current (predicted) hourly rate
        premium DOUBLE,
        open_interest DOUBLE,            -- in coins
        oi_notional DOUBLE,              -- open_interest × mark_px (USD)
        day_ntl_vlm DOUBLE,
        max_leverage DOUBLE,
        ingest_run_id VARCHAR NOT NULL,
        PRIMARY KEY (coin, source, snapshot_at)
    );
    """,
    # 6 — perp strategy paper-tracking (forward test): recorded live, never backfilled
    """
    CREATE TABLE IF NOT EXISTS perp_paper_checks (
        strategy VARCHAR NOT NULL,
        coin VARCHAR NOT NULL,
        venue VARCHAR NOT NULL,
        bar_close TIMESTAMPTZ NOT NULL,   -- the closed bar that was checked
        checked_at TIMESTAMPTZ NOT NULL,  -- when (must be shortly after bar_close)
        side VARCHAR,                     -- 'long' | 'short' | NULL (no signal)
        close DOUBLE,
        stop DOUBLE,
        params_hash VARCHAR NOT NULL,
        PRIMARY KEY (strategy, coin, venue, bar_close)
    );
    """,
    # 7 — isolated Strategy Lab governance; append-only through the Lab API
    """
    CREATE TABLE IF NOT EXISTS lab_strategies (
        strategy_id VARCHAR PRIMARY KEY,
        definition JSON NOT NULL,
        family_id VARCHAR NOT NULL,
        parent_ids JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_hypotheses (
        hypothesis_id VARCHAR PRIMARY KEY,
        strategy_id VARCHAR NOT NULL REFERENCES lab_strategies(strategy_id),
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_submissions (
        submission_id VARCHAR PRIMARY KEY,
        received_at TIMESTAMPTZ NOT NULL,
        origin VARCHAR NOT NULL,
        family_id VARCHAR NOT NULL,
        raw_json VARCHAR NOT NULL,
        hypothesis_id VARCHAR REFERENCES lab_hypotheses(hypothesis_id),
        error VARCHAR,
        CHECK ((hypothesis_id IS NOT NULL AND error IS NULL) OR
               (hypothesis_id IS NULL AND error IS NOT NULL))
    );
    CREATE TABLE IF NOT EXISTS lab_plans (
        plan_id VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        UNIQUE (name, version)
    );
    CREATE TABLE IF NOT EXISTS lab_snapshot_blobs (
        sha256 VARCHAR PRIMARY KEY,
        payload BLOB NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_datasets (
        dataset_id VARCHAR PRIMARY KEY,
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_dataset_blobs (
        dataset_id VARCHAR NOT NULL REFERENCES lab_datasets(dataset_id),
        sha256 VARCHAR NOT NULL REFERENCES lab_snapshot_blobs(sha256),
        PRIMARY KEY (dataset_id, sha256)
    );
    CREATE TABLE IF NOT EXISTS lab_software (
        software_id VARCHAR PRIMARY KEY,
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_experiments (
        experiment_id VARCHAR PRIMARY KEY,
        logical_id VARCHAR NOT NULL,
        attempt INTEGER NOT NULL CHECK (attempt > 0),
        strategy_id VARCHAR NOT NULL REFERENCES lab_strategies(strategy_id),
        hypothesis_id VARCHAR NOT NULL REFERENCES lab_hypotheses(hypothesis_id),
        submission_id VARCHAR NOT NULL REFERENCES lab_submissions(submission_id),
        plan_id VARCHAR NOT NULL REFERENCES lab_plans(plan_id),
        dataset_id VARCHAR NOT NULL REFERENCES lab_datasets(dataset_id),
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        family_id VARCHAR NOT NULL,
        batch_id VARCHAR,
        role VARCHAR NOT NULL,
        period_start TIMESTAMPTZ NOT NULL,
        period_end TIMESTAMPTZ NOT NULL,
        stage VARCHAR NOT NULL,
        assets JSON NOT NULL,
        timeframes JSON NOT NULL,
        origin VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        rerun_of VARCHAR REFERENCES lab_experiments(experiment_id),
        rerun_reason VARCHAR,
        UNIQUE (logical_id, attempt)
    );
    CREATE TABLE IF NOT EXISTS lab_starts (
        experiment_id VARCHAR PRIMARY KEY REFERENCES lab_experiments(experiment_id),
        started_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_results (
        result_id VARCHAR PRIMARY KEY,
        experiment_id VARCHAR NOT NULL UNIQUE REFERENCES lab_starts(experiment_id),
        completed_at TIMESTAMPTZ NOT NULL,
        status VARCHAR NOT NULL CHECK
            (status IN ('succeeded','rejected','insufficient_data','failed','errored','cancelled')),
        verdict VARCHAR,
        metrics JSON NOT NULL,
        p_values JSON NOT NULL,
        error JSON
    );
    CREATE TABLE IF NOT EXISTS lab_inspections (
        inspection_id VARCHAR PRIMARY KEY,
        experiment_id VARCHAR NOT NULL REFERENCES lab_experiments(experiment_id),
        recorded_at TIMESTAMPTZ NOT NULL,
        kind VARCHAR NOT NULL CHECK (kind IN ('evaluation_started','manual_inspection')),
        reason VARCHAR NOT NULL
    );
    """,
    # 8 — Strategy Lab search batches (testing families); append-only through the Lab API
    """
    CREATE TABLE IF NOT EXISTS lab_batches (
        batch_id VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL UNIQUE,
        payload JSON NOT NULL,
        origin VARCHAR NOT NULL,
        frozen_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_batch_runs (
        run_id VARCHAR PRIMARY KEY,
        batch_id VARCHAR NOT NULL REFERENCES lab_batches(batch_id),
        attempt INTEGER NOT NULL CHECK (attempt > 0),
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        started_at TIMESTAMPTZ NOT NULL,
        rerun_of VARCHAR REFERENCES lab_batch_runs(run_id),
        rerun_reason VARCHAR,
        UNIQUE (batch_id, attempt)
    );
    CREATE TABLE IF NOT EXISTS lab_batch_members (
        run_id VARCHAR NOT NULL REFERENCES lab_batch_runs(run_id),
        strategy_id VARCHAR NOT NULL REFERENCES lab_strategies(strategy_id),
        experiment_id VARCHAR NOT NULL UNIQUE REFERENCES lab_experiments(experiment_id),
        PRIMARY KEY (run_id, strategy_id)
    );
    CREATE TABLE IF NOT EXISTS lab_batch_analyses (
        analysis_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL UNIQUE REFERENCES lab_batch_runs(run_id),
        recorded_at TIMESTAMPTZ NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('completed','failed')),
        payload JSON NOT NULL
    );
    """,
    # 9 — Strategy Lab evidence profiles: consumer-neutral, append-only via the Lab API
    """
    CREATE TABLE IF NOT EXISTS lab_evidence_policies (
        policy_id VARCHAR PRIMARY KEY,
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_evidence_profiles (
        profile_id VARCHAR PRIMARY KEY,
        policy_id VARCHAR NOT NULL REFERENCES lab_evidence_policies(policy_id),
        strategy_id VARCHAR NOT NULL REFERENCES lab_strategies(strategy_id),
        analysis_id VARCHAR REFERENCES lab_batch_analyses(analysis_id),
        tier VARCHAR NOT NULL CHECK (tier IN ('UNAVAILABLE','INSUFFICIENT','NEGATIVE',
            'INCONCLUSIVE','EXPLORATORY','RESEARCH_SUPPORTED','VALIDATED')),
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    """,
    # 10 — perp open interest. Interval-based provider history (Binance) is stored raw, per
    # venue; Hyperliquid OI stays in perp_snapshots (prospective, irregular capture times).
    # The view lists both side by side with the venue explicit; it never merges them.
    """
    CREATE TABLE IF NOT EXISTS perp_oi_history (
        source VARCHAR NOT NULL,           -- venue, e.g. 'binance'
        coin VARCHAR NOT NULL,             -- Prism perp coin, e.g. 'BTC'
        provider_symbol VARCHAR NOT NULL,  -- e.g. 'BTCUSDT'
        market_type VARCHAR NOT NULL,      -- e.g. 'usdm_perpetual'
        period VARCHAR NOT NULL,           -- provider statistics period, e.g. '1h'
        observed_at TIMESTAMPTZ NOT NULL,  -- provider timestamp, exact (never re-gridded)
        open_interest DOUBLE,              -- base units (coins), as returned
        oi_notional DOUBLE,                -- USD(T) value, as returned by the provider
        oi_notional_method VARCHAR NOT NULL,
        ingested_at TIMESTAMPTZ NOT NULL,  -- first stored
        updated_at TIMESTAMPTZ NOT NULL,   -- last time the values changed
        ingest_run_id VARCHAR NOT NULL,
        PRIMARY KEY (source, coin, period, observed_at)
    );
    CREATE OR REPLACE VIEW perp_oi_observations AS
        SELECT source AS venue, coin, coin AS provider_symbol, 'hyperliquid_perp' AS market_type,
               'snapshot' AS period, snapshot_at AS observed_at, open_interest, oi_notional,
               'open_interest*mark_px' AS oi_notional_method, ingest_run_id
        FROM perp_snapshots
        UNION ALL
        SELECT source, coin, provider_symbol, market_type, period, observed_at, open_interest,
               oi_notional, oi_notional_method, ingest_run_id
        FROM perp_oi_history;
    """,
    # 11 — Strategy Lab prospective forward tracking: append-only via the Lab API. A bar is
    # evaluated only within one bar interval of closing (CHECK below), so missed bars stay
    # missing; outcomes are written once, when final.
    """
    CREATE TABLE IF NOT EXISTS lab_forward_trackings (
        tracking_id VARCHAR PRIMARY KEY,
        strategy_id VARCHAR NOT NULL REFERENCES lab_strategies(strategy_id),
        profile_id VARCHAR NOT NULL REFERENCES lab_evidence_profiles(profile_id),
        profile_tier VARCHAR NOT NULL,
        enrolled_at TIMESTAMPTZ NOT NULL,
        reason VARCHAR NOT NULL,
        origin VARCHAR NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        definition JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_forward_status (
        event_id VARCHAR PRIMARY KEY,
        tracking_id VARCHAR NOT NULL REFERENCES lab_forward_trackings(tracking_id),
        status VARCHAR NOT NULL CHECK (status IN ('active','paused','stopped')),
        reason VARCHAR NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_forward_runs (
        run_id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL CHECK (kind IN ('check','resolve')),
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        summary JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_forward_inputs (
        input_id VARCHAR PRIMARY KEY,      -- content ID of the fingerprint manifest
        manifest JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_forward_evaluations (
        evaluation_id VARCHAR PRIMARY KEY,
        tracking_id VARCHAR NOT NULL REFERENCES lab_forward_trackings(tracking_id),
        symbol VARCHAR NOT NULL,
        bar_close TIMESTAMPTZ NOT NULL,    -- signal bar T (its close is the decision time)
        evaluated_at TIMESTAMPTZ NOT NULL,
        run_id VARCHAR NOT NULL REFERENCES lab_forward_runs(run_id),
        status VARCHAR NOT NULL CHECK (status IN ('signal','no_signal','ineligible')),
        eligible BOOLEAN NOT NULL,
        active BOOLEAN NOT NULL,
        fired BOOLEAN NOT NULL,
        close DOUBLE,
        stop DOUBLE,
        input_id VARCHAR NOT NULL REFERENCES lab_forward_inputs(input_id),
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        payload JSON NOT NULL,
        UNIQUE (tracking_id, symbol, bar_close),
        CHECK (evaluated_at >= bar_close AND evaluated_at < bar_close + INTERVAL 1 DAY),
        CHECK (fired = (status = 'signal')),
        CHECK (NOT fired OR (eligible AND active))
    );
    CREATE TABLE IF NOT EXISTS lab_forward_entries (
        evaluation_id VARCHAR PRIMARY KEY REFERENCES lab_forward_evaluations(evaluation_id),
        status VARCHAR NOT NULL CHECK (status IN ('entered','unavailable')),
        entry_bar_open TIMESTAMPTZ,
        entry_price DOUBLE,
        recorded_at TIMESTAMPTZ NOT NULL,
        payload JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_forward_outcomes (
        evaluation_id VARCHAR NOT NULL REFERENCES lab_forward_evaluations(evaluation_id),
        horizon VARCHAR NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('resolved','unavailable')),
        exit_bar_close TIMESTAMPTZ NOT NULL,
        gross DOUBLE,
        net DOUBLE,
        input_id VARCHAR REFERENCES lab_forward_inputs(input_id),
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        recorded_at TIMESTAMPTZ NOT NULL,
        payload JSON NOT NULL,
        PRIMARY KEY (evaluation_id, horizon),
        CHECK (recorded_at >= exit_bar_close),
        CHECK ((status = 'resolved') = (net IS NOT NULL AND gross IS NOT NULL))
    );
    CREATE TABLE IF NOT EXISTS lab_forward_summaries (
        summary_id VARCHAR PRIMARY KEY,
        tracking_id VARCHAR NOT NULL REFERENCES lab_forward_trackings(tracking_id),
        as_of TIMESTAMPTZ NOT NULL,
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    """,
    # 12 — Strategy Lab full research and independent validation: append-only via the Lab
    # API. A registration freezes the strategy, source profile and reserved validation
    # period; each run has at most one terminal result. Validation exposure itself is the
    # Phase 2 start/inspection of the linked experiment.
    """
    CREATE TABLE IF NOT EXISTS lab_research_policies (
        policy_id VARCHAR PRIMARY KEY,
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_research_registrations (
        registration_id VARCHAR PRIMARY KEY,
        strategy_id VARCHAR NOT NULL REFERENCES lab_strategies(strategy_id),
        source_profile_id VARCHAR NOT NULL REFERENCES lab_evidence_profiles(profile_id),
        historical_tier VARCHAR NOT NULL CHECK (historical_tier IN ('EXPLORATORY','RESEARCH_SUPPORTED')),
        validation_plan_id VARCHAR NOT NULL REFERENCES lab_plans(plan_id),
        registered_at TIMESTAMPTZ NOT NULL,
        reason VARCHAR NOT NULL,
        origin VARCHAR NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        definition JSON NOT NULL,
        independence JSON NOT NULL         -- recorded exposure state at registration time
    );
    CREATE TABLE IF NOT EXISTS lab_research_runs (
        run_id VARCHAR PRIMARY KEY,
        registration_id VARCHAR NOT NULL REFERENCES lab_research_registrations(registration_id),
        stage VARCHAR NOT NULL CHECK (stage IN ('full_research','validation')),
        attempt INTEGER NOT NULL CHECK (attempt > 0),
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        started_at TIMESTAMPTZ NOT NULL,
        experiment_id VARCHAR REFERENCES lab_experiments(experiment_id),
        rerun_of VARCHAR REFERENCES lab_research_runs(run_id),
        rerun_reason VARCHAR,
        UNIQUE (registration_id, stage, attempt),
        CHECK (stage = 'validation' OR experiment_id IS NULL)
    );
    CREATE TABLE IF NOT EXISTS lab_research_results (
        result_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL UNIQUE REFERENCES lab_research_runs(run_id),
        completed_at TIMESTAMPTZ NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN (
            'FULL_RESEARCH_CONSISTENT','FULL_RESEARCH_MIXED','FULL_RESEARCH_INCONSISTENT',
            'FULL_RESEARCH_INSUFFICIENT','FULL_RESEARCH_ERROR',
            'VALIDATION_SUPPORTIVE','VALIDATION_MIXED','VALIDATION_ADVERSE',
            'VALIDATION_INSUFFICIENT','VALIDATION_ERROR')),
        payload JSON NOT NULL
    );
    """,
    # 13 — perps co-pilot: a CONSUMER of Lab evidence (human alerts only). Reads lab_*
    # tables, never writes them (not even lab_software). Decisions are recorded once per
    # policy, strategy, symbol and signal bar, only inside the bar's live window (CHECK).
    # Deliveries are separate append-only attempts, so "policy said ALERT" and "message
    # delivered" stay distinct.
    """
    CREATE TABLE IF NOT EXISTS copilot_software (
        software_id VARCHAR PRIMARY KEY,
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS copilot_policies (
        policy_id VARCHAR PRIMARY KEY,
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS copilot_watchlist (
        watch_id VARCHAR PRIMARY KEY,
        strategy_id VARCHAR NOT NULL REFERENCES lab_strategies(strategy_id),
        baseline_profile_id VARCHAR NOT NULL REFERENCES lab_evidence_profiles(profile_id),
        policy_id VARCHAR NOT NULL REFERENCES copilot_policies(policy_id),
        registered_at TIMESTAMPTZ NOT NULL,
        reason VARCHAR NOT NULL,
        origin VARCHAR NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES copilot_software(software_id),
        definition JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS copilot_watch_status (
        event_id VARCHAR PRIMARY KEY,
        watch_id VARCHAR NOT NULL REFERENCES copilot_watchlist(watch_id),
        status VARCHAR NOT NULL CHECK (status IN ('active','paused','stopped')),
        reason VARCHAR NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS copilot_runs (
        run_id VARCHAR PRIMARY KEY,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES copilot_software(software_id),
        summary JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS copilot_decisions (
        decision_id VARCHAR PRIMARY KEY,
        watch_id VARCHAR NOT NULL REFERENCES copilot_watchlist(watch_id),
        policy_id VARCHAR NOT NULL REFERENCES copilot_policies(policy_id),
        strategy_id VARCHAR NOT NULL REFERENCES lab_strategies(strategy_id),
        symbol VARCHAR NOT NULL,
        bar_close TIMESTAMPTZ NOT NULL,
        evaluated_at TIMESTAMPTZ NOT NULL,
        run_id VARCHAR NOT NULL REFERENCES copilot_runs(run_id),
        decision VARCHAR NOT NULL CHECK (decision IN ('ALERT','SUPPRESS')),
        priority VARCHAR CHECK (priority IN ('WATCH','STRONG_WATCH')),
        profile_id VARCHAR REFERENCES lab_evidence_profiles(profile_id),
        software_id VARCHAR NOT NULL REFERENCES copilot_software(software_id),
        payload JSON NOT NULL,
        UNIQUE (policy_id, strategy_id, symbol, bar_close),
        CHECK (evaluated_at >= bar_close AND evaluated_at < bar_close + INTERVAL 1 DAY),
        CHECK ((decision = 'ALERT') = (priority IS NOT NULL))
    );
    CREATE TABLE IF NOT EXISTS copilot_deliveries (
        delivery_id VARCHAR PRIMARY KEY,
        decision_id VARCHAR NOT NULL REFERENCES copilot_decisions(decision_id),
        attempt INTEGER NOT NULL CHECK (attempt > 0),
        status VARCHAR NOT NULL CHECK (status IN ('attempted','sent','failed')),
        channel VARCHAR NOT NULL,
        message_kind VARCHAR NOT NULL CHECK (message_kind IN ('single','digest')),
        message_sha256 VARCHAR NOT NULL,
        error VARCHAR,
        recorded_at TIMESTAMPTZ NOT NULL,
        UNIQUE (decision_id, attempt, status)
    );
    """,
    # 14 — Strategy Lab cross-venue historical corroboration: append-only via the Lab API.
    # A registration freezes the strategy, the profile it extends, the venue plan/period and
    # the recorded historical exposure. It is never independent (CHECK). The look itself is
    # the Phase 2 start/inspection of the linked experiment (role 'corroboration').
    """
    CREATE TABLE IF NOT EXISTS lab_corroboration_registrations (
        registration_id VARCHAR PRIMARY KEY,
        strategy_id VARCHAR NOT NULL REFERENCES lab_strategies(strategy_id),
        base_profile_id VARCHAR NOT NULL REFERENCES lab_evidence_profiles(profile_id),
        venue VARCHAR NOT NULL,
        plan_id VARCHAR NOT NULL REFERENCES lab_plans(plan_id),
        registered_at TIMESTAMPTZ NOT NULL,
        reason VARCHAR NOT NULL,
        origin VARCHAR NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        independent BOOLEAN NOT NULL CHECK (NOT independent),
        definition JSON NOT NULL,
        exposure JSON NOT NULL             -- recorded prior exposure at registration time
    );
    CREATE TABLE IF NOT EXISTS lab_corroboration_runs (
        run_id VARCHAR PRIMARY KEY,
        registration_id VARCHAR NOT NULL REFERENCES lab_corroboration_registrations(registration_id),
        attempt INTEGER NOT NULL CHECK (attempt > 0),
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        started_at TIMESTAMPTZ NOT NULL,
        experiment_id VARCHAR NOT NULL REFERENCES lab_experiments(experiment_id),
        rerun_of VARCHAR REFERENCES lab_corroboration_runs(run_id),
        rerun_reason VARCHAR,
        UNIQUE (registration_id, attempt)
    );
    CREATE TABLE IF NOT EXISTS lab_corroboration_results (
        result_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL UNIQUE REFERENCES lab_corroboration_runs(run_id),
        completed_at TIMESTAMPTZ NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN (
            'CROSS_VENUE_CORROBORATIVE','CROSS_VENUE_MIXED','CROSS_VENUE_ADVERSE',
            'CROSS_VENUE_INSUFFICIENT','CROSS_VENUE_ERROR')),
        payload JSON NOT NULL
    );
    """,
    # 15 — paper auto-trader: a SIMULATED account (mode 'paper' only, CHECKed). A CONSUMER of
    # Lab evidence: reads lab_* tables, writes only paper_*. The account is the replay of the
    # append-only paper_events ledger (idempotent event keys; bar events must be strictly after
    # the run's creation). Notifications are separate attempts, never part of the ledger.
    """
    CREATE TABLE IF NOT EXISTS paper_software (
        software_id VARCHAR PRIMARY KEY,
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS paper_policies (
        policy_id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL CHECK (kind IN ('promotion','risk','execution','exit','maturity')),
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS paper_runs (
        run_id VARCHAR PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL,
        mode VARCHAR NOT NULL CHECK (mode = 'paper'),
        label VARCHAR,
        continues VARCHAR REFERENCES paper_runs(run_id),
        promotion_policy_id VARCHAR NOT NULL REFERENCES paper_policies(policy_id),
        risk_policy_id VARCHAR NOT NULL REFERENCES paper_policies(policy_id),
        execution_model_id VARCHAR NOT NULL REFERENCES paper_policies(policy_id),
        exit_policy_id VARCHAR NOT NULL REFERENCES paper_policies(policy_id),
        maturity_policy_id VARCHAR NOT NULL REFERENCES paper_policies(policy_id),
        engine_version VARCHAR NOT NULL,
        reason VARCHAR NOT NULL,
        origin VARCHAR NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES paper_software(software_id),
        definition JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS paper_cycles (
        cycle_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES paper_runs(run_id),
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('ok','error')),
        software_id VARCHAR NOT NULL REFERENCES paper_software(software_id),
        events_written INTEGER NOT NULL,
        summary JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS paper_events (
        event_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES paper_runs(run_id),
        seq INTEGER NOT NULL CHECK (seq > 0),
        event_key VARCHAR NOT NULL,
        event_type VARCHAR NOT NULL CHECK (event_type IN (
            'run_created','status_changed','signals_evaluated','signal_consumed',
            'intent_created','risk_decision','order_submitted','order_filled','order_rejected',
            'order_expired','order_cancelled','position_opened','funding_accrued','liquidation',
            'exit_intent','position_closed','account_mark','kill_switch','data_issue')),
        market_time TIMESTAMPTZ,
        run_created_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        cycle_id VARCHAR,
        payload JSON NOT NULL,
        UNIQUE (run_id, seq),
        UNIQUE (run_id, event_key),
        CHECK (event_type IN ('run_created','status_changed') OR market_time > run_created_at)
    );
    CREATE TABLE IF NOT EXISTS paper_notifications (
        notification_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES paper_runs(run_id),
        subject_id VARCHAR NOT NULL,
        attempt INTEGER NOT NULL CHECK (attempt > 0),
        status VARCHAR NOT NULL CHECK (status IN ('attempted','sent','failed')),
        channel VARCHAR NOT NULL,
        message_sha256 VARCHAR NOT NULL,
        error VARCHAR,
        recorded_at TIMESTAMPTZ NOT NULL,
        UNIQUE (subject_id, attempt, status)
    );
    CREATE TABLE IF NOT EXISTS paper_evidence (
        summary_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES paper_runs(run_id),
        stage VARCHAR NOT NULL CHECK (stage = 'paper_execution'),
        recorded_at TIMESTAMPTZ NOT NULL,
        payload JSON NOT NULL
    );
    """,  # 16 — paper observability (read-only w.r.t. trading): immutable snapshots of the
    # paper_execution evidence (one per run, ledger sequence and version) and one stored daily
    # brief per completed paper day. Delivery attempts reuse paper_notifications.
    """
    CREATE TABLE IF NOT EXISTS paper_snapshots (
        snapshot_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES paper_runs(run_id),
        snapshot_version VARCHAR NOT NULL,
        as_of_seq INTEGER NOT NULL CHECK (as_of_seq > 0),
        as_of_bar TIMESTAMPTZ,
        recorded_at TIMESTAMPTZ NOT NULL,
        payload JSON NOT NULL,
        UNIQUE (run_id, as_of_seq, snapshot_version)
    );
    CREATE TABLE IF NOT EXISTS paper_briefs (
        brief_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES paper_runs(run_id),
        bar_close TIMESTAMPTZ NOT NULL,
        brief_version VARCHAR NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        text_sha256 VARCHAR NOT NULL,
        text VARCHAR NOT NULL,
        payload JSON NOT NULL,
        UNIQUE (run_id, bar_close, brief_version)
    );
    """,  # 17 — always-on runtime (infrastructure only; never research or paper evidence):
    # who may write this database (latest ``authority_claimed`` wins), deployments, verified
    # backups, and one row per scheduled/manual pipeline cycle.
    """
    CREATE TABLE IF NOT EXISTS runtime_events (
        event_id VARCHAR PRIMARY KEY,
        event_type VARCHAR NOT NULL,
        runtime_id VARCHAR NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        git_commit VARCHAR,
        payload JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runtime_cycles (
        cycle_id VARCHAR PRIMARY KEY,
        job VARCHAR NOT NULL,
        runtime_id VARCHAR NOT NULL,
        trigger VARCHAR NOT NULL,
        git_commit VARCHAR,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        status VARCHAR NOT NULL,
        payload JSON
    );
    """,  # 18 — intraday perp bars (Phase 15; market data only, never a signal). One table for
    # every intraday timeframe, keyed by venue/coin/timeframe/open. Bars are UTC half-open
    # [open_time, close_time) and stored only once closed (CHECK). ``first_observed_at`` is when
    # Prism first held the closed bar (its availability); value changes after that bump
    # ``revision`` and keep the superseded values in ``perp_intraday_revisions`` so what Prism
    # knew at any instant is reconstructible. ``perp_intraday_coverage`` holds the merged
    # open-time ranges a provider was successfully asked for after they closed (gap triage).
    # ``intraday_execution_shadow`` is observational timing around paper orders: it is never
    # read by the paper engine and is not paper evidence.
    """
    CREATE TABLE IF NOT EXISTS perp_intraday_bars (
        source VARCHAR NOT NULL,
        coin VARCHAR NOT NULL,
        timeframe VARCHAR NOT NULL CHECK (timeframe IN ('15m', '1h', '4h')),
        open_time TIMESTAMPTZ NOT NULL,
        close_time TIMESTAMPTZ NOT NULL,
        open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, close DOUBLE NOT NULL,
        volume DOUBLE,
        trades BIGINT,
        derivation VARCHAR NOT NULL,
        first_observed_at TIMESTAMPTZ NOT NULL,
        first_run_id VARCHAR NOT NULL,
        observed_live BOOLEAN NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        updated_at TIMESTAMPTZ NOT NULL,
        ingest_run_id VARCHAR NOT NULL,
        -- unique (source, coin, timeframe, open_time) by the single writer's upsert (checked in
        -- its transaction); no PRIMARY KEY: its ART index would be ~73% of the table's bytes
        CHECK (close_time > open_time),
        CHECK (first_observed_at >= close_time),
        CHECK (updated_at >= first_observed_at)
    );
    CREATE TABLE IF NOT EXISTS perp_intraday_revisions (
        source VARCHAR NOT NULL,
        coin VARCHAR NOT NULL,
        timeframe VARCHAR NOT NULL,
        open_time TIMESTAMPTZ NOT NULL,
        close_time TIMESTAMPTZ NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        old_open DOUBLE, old_high DOUBLE, old_low DOUBLE, old_close DOUBLE, old_volume DOUBLE,
        old_trades BIGINT,
        new_open DOUBLE, new_high DOUBLE, new_low DOUBLE, new_close DOUBLE, new_volume DOUBLE,
        new_trades BIGINT,
        old_observed_at TIMESTAMPTZ NOT NULL,
        old_run_id VARCHAR NOT NULL,
        revised_at TIMESTAMPTZ NOT NULL,
        ingest_run_id VARCHAR NOT NULL,
        PRIMARY KEY (source, coin, timeframe, open_time, revision),
        CHECK (revised_at > old_observed_at)
    );
    CREATE TABLE IF NOT EXISTS perp_intraday_coverage (
        source VARCHAR NOT NULL,
        coin VARCHAR NOT NULL,
        timeframe VARCHAR NOT NULL,
        covered_from TIMESTAMPTZ NOT NULL,
        covered_to TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (source, coin, timeframe, covered_from),
        CHECK (covered_to > covered_from)
    );
    CREATE TABLE IF NOT EXISTS intraday_execution_shadow (
        shadow_id VARCHAR PRIMARY KEY,
        rule_version VARCHAR NOT NULL,
        paper_run_id VARCHAR NOT NULL,
        order_id VARCHAR NOT NULL,
        symbol VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        side INTEGER NOT NULL CHECK (side IN (1, -1)),
        intended_entry_at TIMESTAMPTZ NOT NULL,
        decision_at TIMESTAMPTZ NOT NULL,
        ref_open_time TIMESTAMPTZ NOT NULL,
        ref_price DOUBLE NOT NULL CHECK (ref_price > 0),
        ref_observed_at TIMESTAMPTZ NOT NULL,
        latency_seconds DOUBLE NOT NULL,
        timely BOOLEAN NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        payload JSON NOT NULL,
        UNIQUE (paper_run_id, order_id, rule_version),
        CHECK (ref_open_time >= intended_entry_at),
        CHECK (recorded_at >= ref_observed_at)
    );
    """,
    # 19 — Phase 17 structural falsification studies: append-only via the Lab API
    # (``research/lab/structure_study.py``). A study freezes its full definition (windows,
    # grids, horizons, families, gates, verdict policy, costs, retained Lab dataset IDs)
    # BEFORE any run; a run row is committed before evaluation starts (the exposure record);
    # each run has at most one terminal result. Studies are EXPLORATORY (CHECK): backfilled
    # intraday history with assumed-latency availability is never validation. No consumer
    # (forward, co-pilot, paper) reads these tables.
    """
    CREATE TABLE IF NOT EXISTS lab_structure_studies (
        study_id VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL UNIQUE,
        evidence_class VARCHAR NOT NULL CHECK (evidence_class = 'EXPLORATORY'),
        availability_mode VARCHAR NOT NULL CHECK (availability_mode = 'assumed'),
        assumed_latency_s DOUBLE NOT NULL,
        registered_at TIMESTAMPTZ NOT NULL,
        reason VARCHAR NOT NULL,
        origin VARCHAR NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        definition JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_structure_study_datasets (
        study_id VARCHAR NOT NULL REFERENCES lab_structure_studies(study_id),
        venue VARCHAR NOT NULL,
        coin VARCHAR NOT NULL,
        dataset_id VARCHAR NOT NULL REFERENCES lab_datasets(dataset_id),
        PRIMARY KEY (study_id, venue, coin)
    );
    CREATE TABLE IF NOT EXISTS lab_structure_runs (
        run_id VARCHAR PRIMARY KEY,
        study_id VARCHAR NOT NULL REFERENCES lab_structure_studies(study_id),
        attempt INTEGER NOT NULL CHECK (attempt > 0),
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        started_at TIMESTAMPTZ NOT NULL,
        rerun_of VARCHAR REFERENCES lab_structure_runs(run_id),
        rerun_reason VARCHAR,
        UNIQUE (study_id, attempt),
        CHECK ((rerun_of IS NULL) = (rerun_reason IS NULL))
    );
    CREATE TABLE IF NOT EXISTS lab_structure_results (
        result_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL UNIQUE REFERENCES lab_structure_runs(run_id),
        completed_at TIMESTAMPTZ NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('COMPLETED', 'FAILED')),
        evidence_class VARCHAR NOT NULL CHECK (evidence_class = 'EXPLORATORY'),
        result_digest VARCHAR,
        payload JSON NOT NULL,
        meta JSON NOT NULL,
        CHECK ((status = 'COMPLETED') = (result_digest IS NOT NULL))
    );
    """,
    # 20 — Phase 20 edge profiles: append-only via ``research/lifecycle/profile.py``. One row
    # per immutable, content-addressed profile (strategy, venue, evaluation time, data cutoff,
    # lifecycle policy and methodology version). A later evaluation is a new row; nothing is
    # updated or deleted. EXPLORATORY research records: no consumer (forward, co-pilot, paper)
    # reads this table, and no column grants any permission.
    """
    CREATE TABLE IF NOT EXISTS lab_edge_profiles (
        profile_id VARCHAR PRIMARY KEY,
        strategy_id VARCHAR NOT NULL,
        strategy_name VARCHAR NOT NULL,
        venue VARCHAR NOT NULL,
        as_of TIMESTAMPTZ NOT NULL,
        data_cutoff TIMESTAMPTZ NOT NULL,
        policy_id VARCHAR NOT NULL,
        methodology_version VARCHAR NOT NULL,
        edge_state VARCHAR NOT NULL CHECK (edge_state IN ('EMERGING', 'ACTIVE', 'STABLE',
            'DECAYING', 'DORMANT', 'DEAD', 'INSUFFICIENT')),
        source_id VARCHAR NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        payload JSON NOT NULL,
        CHECK (as_of <= data_cutoff)
    );
    """,
    # 21 — Phase 21 candidate incubation: append-only via ``research/incubation/prospective.py``.
    # A freeze fixes the candidate pool, the three policies, shadow execution, costs and
    # cadence BEFORE prospective collection; only bars closing after ``registered_at`` are
    # evaluated, each once (deterministic IDs + UNIQUE keys), and nothing is updated or
    # deleted. Shadow intents are analytical records: no order, no account, no venue. No
    # consumer (forward, co-pilot, paper) reads these tables and no column grants live use.
    """
    CREATE TABLE IF NOT EXISTS incubation_freezes (
        freeze_id VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL,
        registered_at TIMESTAMPTZ NOT NULL,
        reason VARCHAR NOT NULL,
        origin VARCHAR NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        grants_live BOOLEAN NOT NULL CHECK (NOT grants_live),
        definition JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS incubation_runs (
        run_id VARCHAR PRIMARY KEY,
        started_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        summary JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS incubation_evaluations (
        evaluation_id VARCHAR PRIMARY KEY,
        freeze_id VARCHAR NOT NULL REFERENCES incubation_freezes(freeze_id),
        strategy_id VARCHAR NOT NULL,
        strategy_name VARCHAR NOT NULL,
        side VARCHAR NOT NULL CHECK (side IN ('long', 'short')),
        venue VARCHAR NOT NULL,
        bar_close TIMESTAMPTZ NOT NULL,
        evaluated_at TIMESTAMPTZ NOT NULL,
        run_id VARCHAR NOT NULL REFERENCES incubation_runs(run_id),
        signals JSON NOT NULL,
        evidence JSON NOT NULL,
        UNIQUE (freeze_id, strategy_id, bar_close),
        CHECK (evaluated_at >= bar_close)
    );
    CREATE TABLE IF NOT EXISTS incubation_decisions (
        decision_id VARCHAR PRIMARY KEY,
        evaluation_id VARCHAR NOT NULL REFERENCES incubation_evaluations(evaluation_id),
        policy_id VARCHAR NOT NULL,
        profile VARCHAR NOT NULL CHECK (profile IN ('CONSERVATIVE', 'BALANCED', 'AGGRESSIVE')),
        level VARCHAR NOT NULL CHECK (level IN ('INSUFFICIENT', 'NEUTRAL', 'WATCH',
            'EXPLORATORY_PAPER', 'CONFIRMED_PAPER', 'DORMANT', 'RETIRED')),
        episode_id VARCHAR,
        payload JSON NOT NULL,
        UNIQUE (evaluation_id, policy_id),
        CHECK ((level IN ('EXPLORATORY_PAPER', 'CONFIRMED_PAPER')) = (episode_id IS NOT NULL))
    );
    CREATE TABLE IF NOT EXISTS incubation_transitions (
        transition_id VARCHAR PRIMARY KEY,
        decision_id VARCHAR NOT NULL UNIQUE REFERENCES incubation_decisions(decision_id),
        freeze_id VARCHAR NOT NULL REFERENCES incubation_freezes(freeze_id),
        profile VARCHAR NOT NULL,
        strategy_id VARCHAR NOT NULL,
        bar_close TIMESTAMPTZ NOT NULL,
        from_level VARCHAR NOT NULL,
        to_level VARCHAR NOT NULL,
        episode_id VARCHAR,
        payload JSON NOT NULL,
        CHECK (from_level <> to_level)
    );
    CREATE TABLE IF NOT EXISTS incubation_intents (
        intent_id VARCHAR PRIMARY KEY,
        decision_id VARCHAR NOT NULL REFERENCES incubation_decisions(decision_id),
        freeze_id VARCHAR NOT NULL REFERENCES incubation_freezes(freeze_id),
        profile VARCHAR NOT NULL,
        episode_id VARCHAR NOT NULL,
        strategy_id VARCHAR NOT NULL,
        asset VARCHAR NOT NULL,
        side VARCHAR NOT NULL CHECK (side IN ('long', 'short')),
        signal_bar_close TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('entered', 'missed_entry_window')),
        level VARCHAR NOT NULL CHECK (level IN ('EXPLORATORY_PAPER', 'CONFIRMED_PAPER')),
        payload JSON NOT NULL,
        UNIQUE (freeze_id, profile, strategy_id, asset, signal_bar_close),
        CHECK (recorded_at >= signal_bar_close)
    );
    CREATE TABLE IF NOT EXISTS incubation_outcomes (
        intent_id VARCHAR PRIMARY KEY REFERENCES incubation_intents(intent_id),
        status VARCHAR NOT NULL CHECK (status IN ('resolved', 'unavailable')),
        exit_close TIMESTAMPTZ NOT NULL,
        gross DOUBLE,
        net DOUBLE,
        pnl_usd DOUBLE,
        recorded_at TIMESTAMPTZ NOT NULL,
        payload JSON NOT NULL,
        CHECK ((status = 'resolved') = (net IS NOT NULL)),
        CHECK (recorded_at >= exit_close)
    );
    """,
    # 22 — Phase 23 context intelligence: append-only via ``context/ledger.py`` and
    # ``context/positioning.py``. An event row is its FIRST observation, never edited; later
    # reports are ``context_event_updates`` rows (observed_at = when Prism saw them). Research
    # may use an event only from ``first_seen_at`` (Prism's own clock). Positioning tables are
    # data only (no consumer: forward, co-pilot, paper and incubation never read context_*).
    """
    CREATE TABLE IF NOT EXISTS context_sources (
        source_id VARCHAR PRIMARY KEY,
        source_type VARCHAR NOT NULL,
        tier INTEGER NOT NULL CHECK (tier IN (1, 2, 3)),
        payload JSON NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS context_provider_runs (
        run_id VARCHAR PRIMARY KEY,
        provider VARCHAR NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('ok', 'partial', 'failed', 'skipped')),
        received INTEGER NOT NULL,
        new_events INTEGER NOT NULL,
        new_updates INTEGER NOT NULL,
        duplicates INTEGER NOT NULL,
        rejected INTEGER NOT NULL,
        filtered INTEGER NOT NULL,
        latency_ms DOUBLE,
        error VARCHAR,
        payload JSON NOT NULL,
        CHECK (finished_at >= started_at)
    );
    CREATE TABLE IF NOT EXISTS context_events (
        event_id VARCHAR PRIMARY KEY,
        dedup_key VARCHAR NOT NULL UNIQUE,
        schema_version VARCHAR NOT NULL,
        taxonomy_version VARCHAR NOT NULL,
        category VARCHAR NOT NULL,
        subcategory VARCHAR NOT NULL,
        title VARCHAR NOT NULL,
        summary VARCHAR NOT NULL,
        scheduled BOOLEAN NOT NULL,
        event_time TIMESTAMPTZ,
        published_at TIMESTAMPTZ,
        provider_time TIMESTAMPTZ,
        reported_first_seen_at TIMESTAMPTZ,
        first_seen_at TIMESTAMPTZ NOT NULL,
        processed_at TIMESTAMPTZ NOT NULL,
        source_id VARCHAR NOT NULL REFERENCES context_sources(source_id),
        source_ref VARCHAR,
        confidence VARCHAR NOT NULL,
        scope VARCHAR NOT NULL,
        country VARCHAR,
        region VARCHAR,
        relevance_end TIMESTAMPTZ,
        observation_mode VARCHAR NOT NULL CHECK (observation_mode IN ('live', 'historical')),
        attributes JSON NOT NULL,
        entities JSON NOT NULL,
        provenance_hash VARCHAR NOT NULL,
        raw_sha256 VARCHAR,
        observation JSON NOT NULL,
        run_id VARCHAR,
        CHECK (processed_at >= first_seen_at)
    );
    CREATE TABLE IF NOT EXISTS context_event_updates (
        update_id VARCHAR PRIMARY KEY,
        event_id VARCHAR NOT NULL REFERENCES context_events(event_id),
        seq INTEGER NOT NULL CHECK (seq >= 1),
        kind VARCHAR NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        processed_at TIMESTAMPTZ NOT NULL,
        published_at TIMESTAMPTZ,
        source_id VARCHAR NOT NULL REFERENCES context_sources(source_id),
        source_ref VARCHAR,
        confidence VARCHAR NOT NULL,
        changes JSON NOT NULL,
        observation_mode VARCHAR NOT NULL CHECK (observation_mode IN ('live', 'historical')),
        provenance_hash VARCHAR NOT NULL,
        observation JSON NOT NULL,
        run_id VARCHAR,
        UNIQUE (event_id, seq),
        UNIQUE (event_id, provenance_hash),
        CHECK (processed_at >= observed_at)
    );
    CREATE TABLE IF NOT EXISTS context_asset_links (
        event_id VARCHAR NOT NULL REFERENCES context_events(event_id),
        asset VARCHAR NOT NULL,
        link_type VARCHAR NOT NULL CHECK (link_type IN ('direct', 'ecosystem', 'market_wide')),
        entity VARCHAR NOT NULL,
        mapping_version VARCHAR NOT NULL,
        linked_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (event_id, asset, link_type)
    );
    CREATE TABLE IF NOT EXISTS context_snapshots (
        snapshot_id VARCHAR PRIMARY KEY,
        asset VARCHAR NOT NULL,
        as_of TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        version VARCHAR NOT NULL,
        payload JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS context_theses (
        thesis_id VARCHAR PRIMARY KEY,
        asset VARCHAR NOT NULL,
        as_of TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        status VARCHAR NOT NULL CHECK (status = 'research_only'),
        snapshot_id VARCHAR NOT NULL REFERENCES context_snapshots(snapshot_id),
        payload JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS context_hl_oi_hourly (
        coin VARCHAR NOT NULL,
        grid_hour TIMESTAMPTZ NOT NULL,
        captured_at TIMESTAMPTZ NOT NULL,
        open_interest DOUBLE,
        oi_notional DOUBLE,
        mark_px DOUBLE,
        oracle_px DOUBLE,
        mid_px DOUBLE,
        funding_rate DOUBLE,
        premium DOUBLE,
        impact_bid_px DOUBLE,
        impact_ask_px DOUBLE,
        day_ntl_vlm DOUBLE,
        cadence_version VARCHAR NOT NULL,
        run_id VARCHAR NOT NULL,
        PRIMARY KEY (coin, grid_hour),
        CHECK (captured_at >= grid_hour),
        CHECK (captured_at < grid_hour + INTERVAL 1 HOUR)
    );
    CREATE TABLE IF NOT EXISTS context_ls_ratios (
        source VARCHAR NOT NULL,
        coin VARCHAR NOT NULL,
        provider_symbol VARCHAR NOT NULL,
        metric VARCHAR NOT NULL CHECK (metric IN ('global_account', 'top_account',
            'top_position', 'taker_volume')),
        period VARCHAR NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        long_share DOUBLE,
        short_share DOUBLE,
        ratio DOUBLE,
        buy_volume DOUBLE,
        sell_volume DOUBLE,
        ingested_at TIMESTAMPTZ NOT NULL,
        run_id VARCHAR NOT NULL,
        PRIMARY KEY (source, coin, metric, period, observed_at)
    );
    """,
    # 23 — Phase 24A Hyperliquid microstructure (data only; docs/MICROSTRUCTURE_COLLECTION.md).
    # Written ONLY by ``microstructure/ingest.py`` (the runtime's scheduled ingest job) from the
    # collector's append-only spool: the persistent collector never opens this database. One
    # row per (feature_version, coin, UTC minute); every row is versioned. A late-trade revision
    # bumps ``revision`` and the superseded row is kept in ``microstructure_revisions``, so what
    # Prism knew at any instant is reconstructible. Availability = ``finalized_at`` (collector
    # clock, after the spool fsync), never the exchange time. ``microstructure_cutover`` holds
    # the immutable production start (first COMPLETE minute ingested from the authoritative
    # collector), one row per feature version, never updated. No consumer reads these tables.
    # ``microstructure_minutes`` deliberately has no PRIMARY KEY: its ART index measured ~120
    # bytes/row (+50%) on the volume and in every backup. (feature_version, coin, minute_open)
    # uniqueness is enforced by the single ingest writer (key lookup before insert, under the
    # runtime lock), tested, and checked by `market microstructure health`.
    """
    CREATE TABLE IF NOT EXISTS microstructure_minutes (
        feature_version VARCHAR NOT NULL,
        coin VARCHAR NOT NULL,
        minute_open TIMESTAMPTZ NOT NULL,
        revision SMALLINT NOT NULL CHECK (revision >= 0),
        status VARCHAR NOT NULL CHECK (status IN ('COMPLETE', 'PARTIAL', 'TRADE_ONLY',
            'BOOK_ONLY', 'GAP')),
        flags VARCHAR,
        trade_cov REAL NOT NULL,
        book_samples SMALLINT NOT NULL,
        depth20_samples SMALLINT NOT NULL,
        n_buy INTEGER, n_sell INTEGER, n_buy_prints INTEGER, n_sell_prints INTEGER,
        buy_vol DOUBLE, sell_vol DOUBLE, buy_ntl DOUBLE, sell_ntl DOUBLE,
        first_px DOUBLE, last_px DOUBLE, high_px DOUBLE, low_px DOUBLE,
        max_print_ntl DOUBLE, med_print_ntl DOUBLE,
        size_hist INTEGER[],
        lp_threshold DOUBLE, lp_n INTEGER, lp_buy_n INTEGER, lp_ntl DOUBLE, lp_buy_ntl DOUBLE,
        bid_end DOUBLE, ask_end DOUBLE,
        spread_mean REAL, spread_bps_mean REAL, spread_bps_med REAL, spread_bps_min REAL,
        spread_bps_max REAL,
        bid5_mean REAL, ask5_mean REAL, bid5_end REAL, ask5_end REAL,
        bid20_mean REAL, ask20_mean REAL, bid20_end REAL, ask20_end REAL,
        book_updates SMALLINT, bid_changes SMALLINT, ask_changes SMALLINT, mid_changes SMALLINT,
        bid_replenish REAL, ask_replenish REAL,
        oi_end DOUBLE, mark_end DOUBLE, oracle_end DOUBLE, funding_end DOUBLE,
        impact_bid_end DOUBLE, impact_ask_end DOUBLE,
        lat_p50_ms INTEGER, lat_max_ms INTEGER,
        n_dup INTEGER NOT NULL, n_late INTEGER NOT NULL,
        first_recv_at TIMESTAMPTZ, last_recv_at TIMESTAMPTZ,
        finalized_at TIMESTAMPTZ NOT NULL,
        ingested_at TIMESTAMPTZ NOT NULL,
        session_id VARCHAR NOT NULL,
        content_sha VARCHAR NOT NULL,
        CHECK (finalized_at >= minute_open + INTERVAL 1 MINUTE)
    );
    CREATE TABLE IF NOT EXISTS microstructure_revisions (
        feature_version VARCHAR NOT NULL,
        coin VARCHAR NOT NULL,
        minute_open TIMESTAMPTZ NOT NULL,
        revision SMALLINT NOT NULL,
        superseded_at TIMESTAMPTZ NOT NULL,
        superseded_by SMALLINT NOT NULL,
        row_json JSON NOT NULL,
        PRIMARY KEY (feature_version, coin, minute_open, revision)
    );
    CREATE TABLE IF NOT EXISTS microstructure_late_events (
        feature_version VARCHAR NOT NULL,
        coin VARCHAR NOT NULL,
        tid BIGINT NOT NULL,
        trade_time TIMESTAMPTZ NOT NULL,
        received_at TIMESTAMPTZ NOT NULL,
        minute_open TIMESTAMPTZ NOT NULL,
        px DOUBLE NOT NULL,
        sz DOUBLE NOT NULL,
        is_buy BOOLEAN NOT NULL,
        disposition VARCHAR NOT NULL CHECK (disposition IN ('revised', 'rejected')),
        PRIMARY KEY (feature_version, coin, tid)
    );
    CREATE TABLE IF NOT EXISTS microstructure_provider_runs (
        run_id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL CHECK (kind IN ('process', 'connection')),
        parent_run_id VARCHAR,
        runtime_id VARCHAR,
        role VARCHAR,
        git_commit VARCHAR,
        started_at TIMESTAMPTZ NOT NULL,
        ended_at TIMESTAMPTZ,
        end_reason VARCHAR,
        payload JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS microstructure_lp_thresholds (
        lp_version VARCHAR NOT NULL,
        coin VARCHAR NOT NULL,
        day DATE NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('ok', 'warmup')),
        threshold_ntl DOUBLE,
        n_prints BIGINT NOT NULL,
        days_used INTEGER NOT NULL,
        computed_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (lp_version, coin, day),
        CHECK ((status = 'ok') = (threshold_ntl IS NOT NULL))
    );
    CREATE TABLE IF NOT EXISTS microstructure_cutover (
        feature_version VARCHAR PRIMARY KEY,
        runtime_id VARCHAR NOT NULL,
        first_minute TIMESTAMPTZ NOT NULL,
        first_coin VARCHAR NOT NULL,
        first_finalized_at TIMESTAMPTZ NOT NULL,
        session_id VARCHAR NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS microstructure_ingest_offsets (
        path VARCHAR PRIMARY KEY,
        bytes BIGINT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    );
    """,
    # 24 — Phase 24B prospective, sequentially evaluated research studies
    # (docs/MICROSTRUCTURE_DIRECTION_STUDY.md). The Phase 17 ``lab_structure_*`` tables pin
    # availability_mode='assumed' and one evaluation per study; a prospective study on
    # observed receipt-time data is frozen once and then evaluated at fixed checkpoints
    # (as_of times on a calendar grid). Append-only: every checkpoint and its one terminal
    # result are kept, including those whose verdict later reversed (no optional stopping).
    # No consumer reads these tables, and no field can mark a study validated or live.
    """
    CREATE TABLE IF NOT EXISTS lab_prospective_studies (
        study_id VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL UNIQUE,
        study_version VARCHAR NOT NULL,
        evidence_class VARCHAR NOT NULL CHECK (evidence_class = 'PROSPECTIVE_EXPLORATORY'),
        availability_mode VARCHAR NOT NULL CHECK (availability_mode = 'observed'),
        registered_at TIMESTAMPTZ NOT NULL,
        reason VARCHAR NOT NULL,
        origin VARCHAR NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        definition JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lab_prospective_checkpoints (
        checkpoint_id VARCHAR PRIMARY KEY,
        study_id VARCHAR NOT NULL REFERENCES lab_prospective_studies(study_id),
        seq INTEGER NOT NULL CHECK (seq > 0),
        cadence VARCHAR NOT NULL CHECK (cadence IN ('daily', 'weekly')),
        as_of TIMESTAMPTZ NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        software_id VARCHAR NOT NULL REFERENCES lab_software(software_id),
        reproduces VARCHAR REFERENCES lab_prospective_checkpoints(checkpoint_id),
        reason VARCHAR,
        UNIQUE (study_id, seq),
        CHECK ((reproduces IS NULL) = (reason IS NULL)),
        CHECK (as_of <= started_at)
    );
    CREATE TABLE IF NOT EXISTS lab_prospective_results (
        result_id VARCHAR PRIMARY KEY,
        checkpoint_id VARCHAR NOT NULL UNIQUE REFERENCES lab_prospective_checkpoints(checkpoint_id),
        completed_at TIMESTAMPTZ NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('COMPLETED', 'FAILED')),
        result_digest VARCHAR,
        payload JSON NOT NULL,
        meta JSON NOT NULL,
        CHECK ((status = 'COMPLETED') = (result_digest IS NOT NULL))
    );
    """,
    # 25 — Phase 25A. Separate identities and append-only paper execution/opportunity evidence.
    """
    CREATE TABLE IF NOT EXISTS paper_retirements (
        run_id VARCHAR PRIMARY KEY REFERENCES paper_runs(run_id),
        retired_at TIMESTAMPTZ NOT NULL,
        reason VARCHAR NOT NULL,
        payload JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS paper_v2_universes (
        universe_id VARCHAR PRIMARY KEY,
        registered_at TIMESTAMPTZ NOT NULL,
        definition JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS paper_v2_policies (
        policy_id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL CHECK (kind IN ('admission','risk','execution')),
        definition JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS paper_v2_runs (
        run_id VARCHAR PRIMARY KEY,
        universe_id VARCHAR NOT NULL REFERENCES paper_v2_universes(universe_id),
        activated_at TIMESTAMPTZ NOT NULL,
        mode VARCHAR NOT NULL CHECK (mode = 'paper'),
        admission_policy_id VARCHAR NOT NULL REFERENCES paper_v2_policies(policy_id),
        risk_policy_id VARCHAR NOT NULL REFERENCES paper_v2_policies(policy_id),
        execution_policy_id VARCHAR NOT NULL REFERENCES paper_v2_policies(policy_id),
        definition JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS paper_v2_context_snapshots (
        snapshot_id VARCHAR PRIMARY KEY,
        payload JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS paper_v2_evaluations (
        evaluation_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES paper_v2_runs(run_id),
        evaluated_at TIMESTAMPTZ NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('ok','error')),
        payload JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS paper_v2_opportunities (
        opportunity_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES paper_v2_runs(run_id),
        hypothesis_id VARCHAR NOT NULL,
        asset VARCHAR NOT NULL,
        side VARCHAR NOT NULL CHECK (side IN ('long','short')),
        signal_at TIMESTAMPTZ NOT NULL,
        available_at TIMESTAMPTZ NOT NULL,
        evaluated_at TIMESTAMPTZ NOT NULL,
        activated_at TIMESTAMPTZ NOT NULL,
        fired BOOLEAN NOT NULL,
        admission VARCHAR NOT NULL CHECK (admission IN ('ADMITTED','SUPPORTED','REJECTED','NO_SIGNAL')),
        rejection_reason VARCHAR,
        trade_id VARCHAR,
        payload JSON NOT NULL,
        UNIQUE (run_id,hypothesis_id,asset,signal_at),
        CHECK (signal_at > activated_at),
        CHECK (available_at >= signal_at OR rejection_reason='INVALID_TIMING'),
        CHECK (evaluated_at >= activated_at),
        CHECK ((admission IN ('ADMITTED','SUPPORTED')) = (trade_id IS NOT NULL))
    );
    CREATE TABLE IF NOT EXISTS paper_v2_outcomes (
        opportunity_id VARCHAR PRIMARY KEY REFERENCES paper_v2_opportunities(opportunity_id),
        recorded_at TIMESTAMPTZ NOT NULL,
        payload JSON NOT NULL
    );
    CREATE TABLE IF NOT EXISTS paper_v2_events (
        event_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES paper_v2_runs(run_id),
        seq INTEGER NOT NULL CHECK (seq > 0),
        event_key VARCHAR NOT NULL,
        event_type VARCHAR NOT NULL CHECK (event_type IN ('pending','opened','closed','mark',
            'support','expired','run_state','hypothesis_state')),
        recorded_at TIMESTAMPTZ NOT NULL,
        payload JSON NOT NULL,
        UNIQUE (run_id,seq),
        UNIQUE (run_id,event_key)
    );
    CREATE TABLE IF NOT EXISTS paper_v2_reports (
        report_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL REFERENCES paper_v2_runs(run_id),
        day DATE NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        payload JSON NOT NULL,
        UNIQUE (run_id,day)
    );
    CREATE TABLE IF NOT EXISTS paper_v2_deliveries (
        report_id VARCHAR NOT NULL REFERENCES paper_v2_reports(report_id),
        state VARCHAR NOT NULL CHECK (state IN ('attempted','sent','failed')),
        recorded_at TIMESTAMPTZ NOT NULL,
        detail VARCHAR,
        PRIMARY KEY (report_id,state)
    );
    """,
    # 26 — Phase 26A transport receipt commit. Only the authoritative ingest writes here.
    """
    CREATE TABLE IF NOT EXISTS context_gateway_ingests (
        receipt_id VARCHAR PRIMARY KEY,
        recorded_at TIMESTAMPTZ NOT NULL,
        result VARCHAR NOT NULL
    );
    """,
]

ROLE_ENV, RUNTIME_ID_ENV = "PRISM_RUNTIME_ROLE", "PRISM_RUNTIME_ID"


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


@dataclass
class ArchivedPayload:
    path: str
    sha256: str


class DatabaseBusy(RuntimeError):
    """Another process holds the DuckDB file lock (DuckDB allows one writing process, and
    readers cannot open while it writes). Usually a running ``market update`` or scan."""

    def __init__(self, path: Path, message: str, waited: float):
        self.path, self.waited = path, waited
        m = re.search(r"held in (\S+) \(PID (\d+)\)", message)
        self.program = m.group(1) if m else None
        self.pid = int(m.group(2)) if m else None
        super().__init__(
            f"database busy: {self.holder} has {path.name} open (waited {waited:.0f}s)"
        )

    @property
    def holder(self) -> str:
        return f"another process (PID {self.pid})" if self.pid else "another process"


class NotAuthoritative(RuntimeError):
    """This process may not write this database: it carries an authority claim for another
    runtime (exactly one runtime writes the live prospective database), or this process
    claims to be authoritative for a database that was never claimed."""


def authority_claim(con: duckdb.DuckDBPyConnection) -> tuple[str, Any] | None:
    """(runtime_id, claimed_at) of the newest ``authority_claimed`` event, or None."""
    try:
        return con.execute(
            "SELECT runtime_id, recorded_at FROM runtime_events WHERE event_type="
            "'authority_claimed' ORDER BY recorded_at DESC, event_id DESC LIMIT 1"
        ).fetchone()
    except duckdb.CatalogException:  # before migration 17: never claimed
        return None


def check_authority(claim: tuple[str, Any] | None, path: Path) -> None:
    """Allow a writable open, or raise ``NotAuthoritative``.

    Unclaimed databases (development, tests, scratch) are unaffected unless this process says
    it is the authoritative runtime. A claimed database is writable only by the runtime named
    in the claim (``PRISM_RUNTIME_ROLE=authoritative`` + matching ``PRISM_RUNTIME_ID``) or by a
    process that explicitly declares it is working on a copy (``PRISM_RUNTIME_ROLE=scratch``).
    """
    role = os.environ.get(ROLE_ENV, "").strip().lower()
    rid = os.environ.get(RUNTIME_ID_ENV, "").strip()
    if claim is None:
        if role == "authoritative":
            raise NotAuthoritative(
                f"{path} has no authority claim; the authoritative runtime only writes a claimed "
                "live database (`market ops claim-authority`)"
            )
        return
    if role == "scratch" or (role == "authoritative" and rid == claim[0]):
        return
    raise NotAuthoritative(
        f"{path.name} is the live database of runtime {claim[0]!r} (claimed {str(claim[1])[:19]} UTC); "
        f"this process (role={role or 'unset'}, id={rid or 'unset'}) may only read it. "
        f"Set {ROLE_ENV}=scratch only if this file is a copy you are deliberately modifying."
    )


def _is_lock_error(exc: Exception) -> bool:
    msg = str(exc)
    return isinstance(exc, duckdb.IOException) and (
        "Could not set lock" in msg or "Conflicting lock" in msg
    )


class Store:
    """Thin repository over a DuckDB file. Not thread-safe; open one per process.

    ``lock_timeout``: seconds to keep retrying while another process holds the file lock,
    before raising ``DatabaseBusy`` (0 = fail immediately). ``on_wait`` is called once with a
    human-readable message when waiting starts.
    """

    def __init__(
        self,
        path: Path | str,
        raw_dir: Path | str | None = None,
        read_only: bool = False,
        lock_timeout: float = 0.0,
        on_wait: Callable[[str], None] | None = None,
        authority_check: bool = True,
    ):
        self.path = Path(path)
        self.raw_dir = Path(raw_dir) if raw_dir else self.path.parent / "raw"
        guarded = authority_check and not read_only and str(path) != ":memory:"
        if guarded and not self.path.exists():
            check_authority(None, self.path)  # never create a fresh "live" database
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = self._connect(read_only, lock_timeout, on_wait)
        self.con.execute("SET TimeZone='UTC'")
        if guarded:
            try:
                check_authority(authority_claim(self.con), self.path)
            except NotAuthoritative:
                self.con.close()
                raise
        if not read_only:
            self.migrate()

    def _connect(
        self, read_only: bool, lock_timeout: float, on_wait: Callable[[str], None] | None
    ) -> duckdb.DuckDBPyConnection:
        start = time.monotonic()
        delay, told = 0.25, False
        while True:
            try:
                return duckdb.connect(str(self.path), read_only=read_only)
            except duckdb.IOException as exc:
                if not _is_lock_error(exc):
                    raise
                waited = time.monotonic() - start
                if waited >= lock_timeout:
                    raise DatabaseBusy(self.path, str(exc), waited) from None
                if on_wait and not told:
                    busy = DatabaseBusy(self.path, str(exc), waited)
                    on_wait(
                        f"Database busy: {busy.holder} is using it. Waiting up to {lock_timeout:.0f}s…"
                    )
                    told = True
                time.sleep(min(delay, max(lock_timeout - waited, 0.01)))
                delay = min(delay * 2, 2.0)

    # ------------------------------------------------------------------ schema
    def migrate(self) -> None:
        self.con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
        row = self.con.execute("SELECT max(version) FROM schema_version").fetchone()
        current = row[0] or 0
        for i, ddl in enumerate(MIGRATIONS, start=1):
            if i > current:
                self.con.execute(ddl)
                self.con.execute("INSERT INTO schema_version VALUES (?)", [i])

    def close(self) -> None:
        self.con.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.con.execute("BEGIN TRANSACTION")
        try:
            yield
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        self.con.execute("COMMIT")

    def query(self, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
        return self.con.execute(sql, params or []).df()

    # ------------------------------------------------------------------ assets
    def upsert_assets(self, assets: Iterable[Asset]) -> None:
        now = utcnow()
        for a in assets:
            series = {
                tf.value: {"provider": s.provider, "symbol": s.symbol} for tf, s in a.series.items()
            }
            self.con.execute(
                "INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?,?,?,?,?)",
                [a.symbol, a.name, a.asset_class.value, a.currency, a.calendar.value, a.active,
                 json.dumps(a.provider_ids), json.dumps(series), now],
            )  # fmt: skip

    # ------------------------------------------------------------------ provenance
    def archive_raw(self, payloads: list[RawPayload], run_id: str) -> list[ArchivedPayload]:
        out = []
        for i, p in enumerate(payloads):
            digest = hashlib.sha256(p.body).hexdigest()
            day = p.fetched_at.strftime("%Y%m%d")
            target = self.raw_dir / p.provider / day / f"{run_id}_{i:04d}.json.gz"
            target.parent.mkdir(parents=True, exist_ok=True)
            envelope = {
                "provider": p.provider,
                "method": p.method,
                "url": p.url,
                "params": p.params,
                "status": p.status,
                "fetched_at": p.fetched_at.isoformat(),
                "sha256": digest,
            }
            with gzip.open(target, "wb") as fh:
                fh.write(json.dumps(envelope).encode() + b"\n")
                fh.write(p.body)
            out.append(ArchivedPayload(str(target), digest))
        return out

    def start_run(
        self, provider: str, dataset: str, entity: str | None, params: dict[str, Any]
    ) -> str:
        run_id = new_id("run_")
        self.con.execute(
            "INSERT INTO ingestion_runs (run_id, provider, dataset, entity, params, started_at, status) "
            "VALUES (?,?,?,?,?,?, 'running')",
            [run_id, provider, dataset, entity, json.dumps(params, default=str), utcnow()],
        )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        rows_received: int = 0,
        rows_written: int = 0,
        rows_rejected: int = 0,
        archived: list[ArchivedPayload] | None = None,
        schema_fingerprint: str = "",
        error: str | None = None,
    ) -> None:
        archived = archived or []
        self.con.execute(
            """UPDATE ingestion_runs SET finished_at=?, status=?, rows_received=?, rows_written=?,
               rows_rejected=?, raw_paths=?, raw_sha256=?, schema_fingerprint=?, error=?
               WHERE run_id=?""",
            [utcnow(), status, rows_received, rows_written, rows_rejected,
             json.dumps([a.path for a in archived]), json.dumps([a.sha256 for a in archived]),
             schema_fingerprint, error, run_id],
        )  # fmt: skip

    def last_schema_fingerprint(self, provider: str, dataset: str) -> str | None:
        row = self.con.execute(
            """SELECT schema_fingerprint FROM ingestion_runs
               WHERE provider=? AND dataset=? AND status='ok' AND schema_fingerprint <> ''
               ORDER BY finished_at DESC LIMIT 1""",
            [provider, dataset],
        ).fetchone()
        return row[0] if row else None

    def log_issues(self, issues: pd.DataFrame) -> None:
        if issues is None or issues.empty:
            return
        self.con.register("_issues", issues)
        self.con.execute(
            """INSERT INTO data_quality_issues
               SELECT run_id, symbol, timeframe, source, check_name, severity, ts, detail, detected_at
               FROM _issues"""
        )
        self.con.unregister("_issues")

    # ------------------------------------------------------------------ bars
    def upsert_bars(
        self, symbol: str, timeframe: Timeframe, source: str, bars: pd.DataFrame, run_id: str
    ) -> dict[str, int]:
        """Idempotent upsert. Returns counts of inserted / revised / unchanged rows."""
        if bars.empty:
            return {"inserted": 0, "revised": 0, "unchanged": 0}
        now = utcnow()
        stage = bars[["ts", "close_time", "open", "high", "low", "close", "volume"]].copy()
        stage["symbol"], stage["timeframe"], stage["source"] = symbol, timeframe.value, source
        self.con.register("_stage", stage)
        try:
            with self.transaction():
                key = "b.symbol=s.symbol AND b.timeframe=s.timeframe AND b.source=s.source AND b.ts=s.ts"
                changed_pred = (
                    "b.open IS DISTINCT FROM s.open OR b.high IS DISTINCT FROM s.high OR "
                    "b.low IS DISTINCT FROM s.low OR b.close IS DISTINCT FROM s.close OR "
                    "b.volume IS DISTINCT FROM s.volume"
                )
                revised = self.con.execute(
                    f"""INSERT INTO bar_revisions
                        SELECT b.symbol, b.timeframe, b.source, b.ts,
                               b.open, b.high, b.low, b.close, b.volume,
                               s.open, s.high, s.low, s.close, s.volume,
                               b.ingest_run_id, ?, ?
                        FROM bars b JOIN _stage s ON {key}
                        WHERE {changed_pred}""",
                    [run_id, now],
                ).fetchone()[0]
                self.con.execute(
                    f"""UPDATE bars b SET open=s.open, high=s.high, low=s.low, close=s.close,
                        volume=s.volume, close_time=s.close_time, ingest_run_id=?, ingested_at=?
                        FROM _stage s WHERE {key} AND ({changed_pred})""",
                    [run_id, now],
                )
                inserted = self.con.execute(
                    f"""INSERT INTO bars
                        SELECT s.symbol, s.timeframe, s.source, s.ts, s.close_time,
                               s.open, s.high, s.low, s.close, s.volume, ?, ?
                        FROM _stage s
                        WHERE NOT EXISTS (SELECT 1 FROM bars b WHERE {key})""",
                    [run_id, now],
                ).fetchone()[0]
        finally:
            self.con.unregister("_stage")
        return {
            "inserted": int(inserted),
            "revised": int(revised),
            "unchanged": len(stage) - int(inserted) - int(revised),
        }

    def upsert_actions(self, symbol: str, source: str, actions: pd.DataFrame, run_id: str) -> int:
        if actions is None or actions.empty:
            return 0
        stage = actions[["date", "split_factor", "dividend"]].copy()
        stage["symbol"], stage["source"], stage["run_id"] = symbol, source, run_id
        self.con.register("_act", stage)
        try:
            self.con.execute(
                """INSERT OR REPLACE INTO corporate_actions
                   SELECT symbol, source, date, split_factor, dividend, run_id FROM _act"""
            )
        finally:
            self.con.unregister("_act")
        return len(stage)

    def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        source: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """Bars for ONE series. If ``source`` is None and several sources exist, raise:
        callers must pick explicitly (no silent stitching)."""
        if source is None:
            sources = self.sources_for(symbol, timeframe)
            if len(sources) > 1:
                raise ValueError(
                    f"{symbol} {timeframe}: multiple sources {sources}; pass source= explicitly"
                )
            if not sources:
                return pd.DataFrame(
                    columns=["ts", "close_time", "open", "high", "low", "close", "volume"]
                )
            source = sources[0]
        sql = """SELECT ts, close_time, open, high, low, close, volume FROM bars
                 WHERE symbol=? AND timeframe=? AND source=?"""
        params: list[Any] = [symbol, timeframe.value, source]
        if start is not None:
            sql += " AND ts >= ?"
            params.append(start)
        if end is not None:
            sql += " AND ts <= ?"
            params.append(end)
        df = self.con.execute(sql + " ORDER BY ts", params).df()
        for c in ("ts", "close_time"):
            df[c] = pd.to_datetime(df[c], utc=True)
        return df

    def sources_for(self, symbol: str, timeframe: Timeframe) -> list[str]:
        rows = self.con.execute(
            "SELECT DISTINCT source FROM bars WHERE symbol=? AND timeframe=? ORDER BY source",
            [symbol, timeframe.value],
        ).fetchall()
        return [r[0] for r in rows]

    def get_actions(self, symbol: str, source: str) -> pd.DataFrame:
        df = self.con.execute(
            "SELECT date, split_factor, dividend FROM corporate_actions WHERE symbol=? AND source=? ORDER BY date",
            [symbol, source],
        ).df()
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def last_bar_ts(self, symbol: str, timeframe: Timeframe, source: str) -> pd.Timestamp | None:
        row = self.con.execute(
            "SELECT max(ts) FROM bars WHERE symbol=? AND timeframe=? AND source=?",
            [symbol, timeframe.value, source],
        ).fetchone()
        return pd.Timestamp(row[0]).tz_convert("UTC") if row and row[0] is not None else None

    def series_inventory(self) -> pd.DataFrame:
        return self.query(
            """SELECT symbol, timeframe, source, count(*) AS n_bars, min(ts) AS first_ts,
                      max(ts) AS last_ts, max(close_time) AS last_close, max(ingested_at) AS last_ingested
               FROM bars GROUP BY 1,2,3 ORDER BY 1,2,3"""
        )

    def record_series_change(
        self, symbol: str, timeframe: Timeframe, old: str, new: str, note: str
    ) -> None:
        exists = self.con.execute(
            "SELECT 1 FROM series_changes WHERE symbol=? AND timeframe=? AND old_source=? AND new_source=?",
            [symbol, timeframe.value, old, new],
        ).fetchone()
        if not exists:
            self.con.execute(
                "INSERT INTO series_changes VALUES (?,?,?,?,?,?)",
                [symbol, timeframe.value, old, new, utcnow(), note],
            )

    def series_fingerprint(self, symbol: str, timeframe: Timeframe, source: str) -> dict[str, Any]:
        """Content hash of a series (for research reproducibility)."""
        row = self.con.execute(
            """SELECT count(*), min(ts), max(ts),
                      sum(hash(ts, open, high, low, close, volume)) % 1000000007
               FROM bars WHERE symbol=? AND timeframe=? AND source=?""",
            [symbol, timeframe.value, source],
        ).fetchone()
        return {
            "symbol": symbol,
            "timeframe": timeframe.value,
            "source": source,
            "n": int(row[0]),
            "first": str(row[1]),
            "last": str(row[2]),
            "hash": str(row[3]),
        }
