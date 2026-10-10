"""Read-only paper trader performance, with explicit snapshot provenance."""

from __future__ import annotations

import math
from datetime import date
from typing import cast

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import common
import paper_data
from market_signal.data.store import DatabaseBusy
from market_signal.paper.dashboard.report import (
    COHORTS,
    SORT_FIELDS,
    comparison,
    required_timestamp,
)

METRICS = {
    "starting_equity": ("Starting equity", "$"),
    "current_equity": ("Current equity · last observed MTM", "$"),
    "realized_equity": ("Realized cash · after open entry fees", "$"),
    "net_pnl": ("Realized net PnL · closed trades", "$"),
    "price_pnl": ("Reference price PnL", "$"),
    "gross_pnl": ("Gross PnL · fills", "$"),
    "fees": ("Fees", "$"),
    "slippage": ("Spread / slippage", "$"),
    "funding": ("Funding · signed cost", "$"),
    "total_trades": ("Trades entered", "n"),
    "open_positions": ("Open positions · current", "n"),
    "closed_trades": ("Closed trades", "n"),
    "trades_per_day": ("Trades / elapsed day", "f"),
    "win_rate": ("Win rate", "%"),
    "average_winner": ("Average winner", "$"),
    "average_loser": ("Average loser", "$"),
    "expectancy_per_trade": ("Expectancy / closed trade", "$"),
    "profit_factor": ("Profit factor", "f"),
    "max_drawdown": ("Max observed MTM drawdown", "%"),
    "average_hold_minutes": ("Average hold · minutes", "f"),
    "capital_utilization": ("Capital utilization · notional / initial capital", "%"),
    "mfe_capture_ratio_mean": ("MFE captured · complete research path", "%"),
}
TRADE_FIELDS = [
    "run",
    "run_id",
    "trade_id",
    "asset",
    "side",
    "hypothesis_id",
    "hypothesis_name",
    "execution_component",
    "event_id",
    "event_class",
    "entry_variant",
    "source_provider",
    "event_age_at_entry_seconds",
    "confirmation_delay_seconds",
    "move_missed_before_fill",
    "confluence",
    "family",
    "entry_at",
    "exit_at",
    "hold_minutes",
    "entry_fill",
    "exit_fill",
    "entry_ref",
    "exit_ref",
    "gross_return",
    "fees",
    "slippage_cost",
    "funding",
    "funding_reserve",
    "liquidation_adjustment",
    "trade_return",
    "net_pnl",
    "exit_reason",
    "horizon_minutes",
    "mfe",
    "mae",
    "mfe_capture_ratio",
    "mae_avoidance",
    "accounting_final",
]
GROUP_FIELDS = [
    "signals",
    "admissions",
    "total_trades",
    "closed_trades",
    "win_rate",
    "expectancy_per_trade",
    "net_pnl",
    "average_hold_minutes",
    "mfe",
    "mae",
    "mfe_capture_ratio_mean",
    "mae_avoidance_mean",
]
OPEN_FIELDS = [
    "run",
    "trade_id",
    "asset",
    "side",
    "entry_at",
    "entry_fill",
    "entry_ref",
    "mark_ref",
    "mark_at",
    "mark_stale",
    "unrealised",
    "funding_accrued",
    "funding_reserve",
    "hypothesis_id",
    "hypothesis_name",
    "execution_component",
    "event_id",
    "event_class",
    "entry_variant",
    "age_minutes",
    "target",
    "invalidation",
    "invalidation_rule",
    "maximum_hold_minutes",
    "exit_due_at",
    "exit_state",
]


def fmt(value: float | int | None, kind="f") -> str:
    if value is None or pd.isna(value) or not math.isfinite(float(value)):
        return "N/A"
    return (
        f"${value:,.4f}"
        if kind == "$"
        else f"{value:.2%}"
        if kind == "%"
        else f"{value:,}"
        if kind == "n"
        else f"{value:,.3f}"
    )


def table(data, fields=None):
    if not data:
        st.info("No observations in this window. Metrics requiring trades are N/A.")
        return
    df = pd.DataFrame(data)
    if fields:
        df = df.reindex(columns=fields)
    # Display missing numeric and text fields explicitly; keep numeric sorting server-side.
    for column in df.columns:
        if bool(df[column].isna().any()):
            df[column] = df[column].map(lambda value: "N/A" if bool(pd.isna(value)) else str(value))
    st.dataframe(df, hide_index=True, width="stretch")


def draw_curves(reports, detail, individual=False):
    colors = ["#4a6fa5", "#b7791f", "#36816b", "#ad597b"]
    figures = []
    combined = go.Figure()
    for i, r in enumerate(reports):
        fig = go.Figure() if individual else combined
        curve = detail[r.run["run_id"]][0]
        for series in ("Realized cash (after entry fees)", "Observed mark-to-market (provisional)"):
            values = [p for p in curve if p["series"] == series]
            realized = series.startswith("Realized")
            sampled = any(p.get("observed_samples", 0) > 1000 for p in values)
            fig.add_trace(
                go.Scatter(
                    x=[p["timestamp"] for p in values],
                    y=[p["value"] for p in values],
                    name=f"{r.run['label']} · {series}",
                    mode="lines+markers" if realized and not sampled else "markers",
                    line={"shape": "hv", "color": colors[i]},
                    marker={
                        "size": 4,
                        "color": colors[i],
                        "symbol": "circle" if realized else "diamond-open",
                    },
                    connectgaps=False,
                    hovertemplate="%{x|%Y-%m-%d %H:%M:%S UTC}<br>$%{y:.4f}<extra>%{fullData.name}</extra>",
                )
            )
        activation = required_timestamp(r.run["activated_at"])
        assert activation is not None
        if r.start <= activation <= r.end:
            fig.add_vline(x=activation.timestamp() * 1000, line_dash="dot", line_color=colors[i])
        fig.update_layout(
            xaxis_title="Observed timestamp · UTC",
            yaxis_title="Simulated USD / USDC",
            height=430,
            legend={"orientation": "h", "y": -0.25},
        )
        if individual:
            figures.append(fig)
    for fig in figures if individual else [combined]:
        st.plotly_chart(fig, width="stretch")
    st.caption(
        "Cash follows accounting events as steps. Diamonds are observed MTM, including open positions and funding reserves; no interpolated returns. Dotted lines mark activation. Long histories retain actual bucket extrema and endpoints; sampled cash is shown as points so omitted accounting events do not become invented steps."
    )


def decision_views(path, revision, reports, start, end):
    st.caption(
        "Inspect recorded decisions for each independent account. Signal evaluations, event classifications, confirmation waits, and execution outcomes are separate stages; their counts must not be added as trades. Only fired signal evaluations are listed."
    )
    if not reports:
        return
    r = st.selectbox("Inspect decisions for", reports, format_func=lambda item: item.run["label"])
    args = (str(path), revision, r.run["run_id"], start, end)
    overview, reasons = paper_data.decision_overview(*args)
    if overview is not None:
        if not overview["available"]:
            st.warning(
                "Event decision evidence is absent from this snapshot. Cohort metadata and trade performance are still available. Retrieve a newer snapshot with the updated pull_paper_snapshot.py; reloading this file cannot add missing tables."
            )
        else:
            st.markdown("**Event cohort decisions**")
            table([overview["counts"]])
            st.caption(
                "Logical events are distinct within this cohort and window. Rejections and waits count evidence observations, not unique rejected events. Pending is the current snapshot state. Classification rows without an assigned hypothesis appear in the decision ledger rather than hypothesis counts."
            )
            table(overview["hypotheses"])
            st.markdown("**Original vs event execution contribution**")
            table(overview["components"], ["execution_component", *GROUP_FIELDS])
            st.caption(
                "These components belong to this account; the immediate and confirmed accounts remain separate. All metrics are descriptive."
            )
    with st.expander("Reasons by decision stage"):
        table(reasons)
        st.caption(
            "Top 30 recorded reason/stage combinations. Confirmation waits and observed source gates are not terminal rejections."
        )
    scope, asset_control, reason_control = st.columns(3)
    sources = [None, "opportunity", "execution"]
    if r.run.get("event_variant"):
        sources.append("event evidence")
    source = scope.selectbox("Decision stage", sources, format_func=lambda v: v or "All stages")
    asset = asset_control.text_input("Decision asset · exact", key="decision_asset")
    reason = reason_control.text_input("Decision reason · exact", key="decision_reason")
    event_control, trade_control = st.columns(2)
    event_id = event_control.text_input("Event ID · exact", key="decision_event")
    trade_id = trade_control.text_input("Trade ID · exact", key="decision_trade")
    filters = (source, reason, asset, event_id, trade_id)
    page_rows, total = paper_data.decision_page(*args, *filters)
    pages = max(1, math.ceil(total / 50))
    page_key = "decision_page"
    if st.session_state.get(page_key, 1) > pages:
        st.session_state[page_key] = 1
    page = int(st.number_input("Decision page · 50 rows", 1, pages, 1, key=page_key)) - 1
    if page:
        page_rows, _ = paper_data.decision_page(*args, *filters, page=page)
    st.caption(
        f"{total:,} matching records · page {page + 1}/{pages}. Timestamps and reasons are persisted production observations."
    )
    decision_fields = [
        "timestamp",
        "source",
        "kind",
        "outcome",
        "reason",
        "asset",
        "side",
        "hypothesis_name",
        "hypothesis_id",
        "event_id",
        "trade_id",
        "id",
    ]
    table(page_rows, decision_fields)
    if not page_rows:
        return
    choice = st.selectbox(
        "Inspect a recorded decision",
        [None, *page_rows],
        format_func=lambda v: (
            "Select a record"
            if v is None
            else f"{v['timestamp']} · {v['kind']} · {v['reason'] or v['outcome']} · {v['id']}"
        ),
    )
    if choice is None:
        return
    evidence = paper_data.decision_detail(*args, choice["source"], choice["id"])
    st.caption(
        "Recorded payload for this timestamp: admission gates, causal source clocks, context, geometry, confirmation, and reasons are shown where persisted. Later execution state is displayed separately below."
    )
    table(evidence["facts"])
    if evidence["source_timings"]:
        table(
            evidence["source_timings"],
            [
                "provider",
                "source_publication_at",
                "collector_observed_at",
                "gateway_received_at",
                "context_available_at",
                "first_evaluator_notice_at",
            ],
        )
    with st.expander("Complete recorded decision evidence", expanded=True):
        st.json(evidence["recorded_payload"])
    if evidence.get("frozen_entry_thesis"):
        with st.expander("Frozen entry thesis"):
            st.json(evidence["frozen_entry_thesis"])
    if evidence.get("latest_trade_state"):
        with st.expander("Latest recorded trade state · may postdate this decision"):
            st.json(evidence["latest_trade_state"])
        timeline, count = paper_data.decision_page(
            *args, source="execution", trade_id=choice["trade_id"]
        )
        st.caption(f"Related execution events in this window · latest {len(timeline)} of {count}.")
        table(timeline, decision_fields)
    if choice.get("event_id"):
        timeline, count = paper_data.decision_page(*args, event_id=choice["event_id"])
        st.caption(
            f"Related event evidence and execution in this window · latest {len(timeline)} of {count}. Filter by this Event ID above to paginate the full timeline."
        )
        table(timeline, decision_fields)


def main():
    common.banner()
    st.title("Paper trader performance")
    st.caption(
        "Descriptive paper-execution performance · read-only · scientific validity remains separate"
    )
    path = paper_data.source_path()
    if not path.is_file():
        st.info(
            f"No paper snapshot found at {path}. Download a production backup using the workflow below, then select its path with PRISM_PAPER_DB_PATH."
        )
        workflow()
        return
    revision = paper_data.version(path)
    metadata = paper_data.metadata(str(path), revision)
    fresh = paper_data.freshness_state(path, metadata)
    with st.container(border=True):
        st.markdown(f"**{fresh['source']} {'· STALE' if fresh['stale'] else '· recent'}**")
        st.caption(f"Data source: {path}")
        st.caption(
            f"Snapshot age: {fmt(fresh['age_seconds'] / 3600 if fresh['age_seconds'] is not None else None)} hours · production backup: {fresh['production_path'] or 'N/A'}"
        )
        st.write(
            f"Snapshot timestamp: {fresh['snapshot_at'] or 'N/A'} · Last production sync: {fresh['last_production_sync'] or 'N/A'}"
        )
        st.caption(
            f"Timestamp basis: {fresh['snapshot_basis']}. Last paper update: {fresh['last_update'] or 'N/A'}. Freshness threshold: 30 minutes; each run's update is also shown below."
        )
        label = (
            "Reread production database"
            if fresh["source"] == "Live / production-backed"
            else "Reload local snapshot"
        )
        if st.button(label):
            paper_data.reload_local()
            st.rerun()
        st.caption(
            "This control rereads the configured file. It does not download a newer Railway backup."
        )
    if fresh["stale"]:
        st.warning(
            "Paper data is stale or freshness is unknown. A recent download can still contain an old snapshot."
        )
    with st.expander("Production snapshot workflow & read-only safety"):
        workflow()
    if not metadata:
        st.info(
            "No Paper v2 or nimble runs exist in this snapshot. Run selection and trade metrics will appear after a production snapshot includes them."
        )
        return
    selected = []
    for column, cohort in zip(st.columns(4), COHORTS, strict=True):
        with column:
            options = [r for r in metadata if r["cohort"] == cohort]
            if not options:
                st.info(f"No {COHORTS[cohort]} run in this snapshot.")
                continue
            # Select the active production run, never an embedded/hardcoded run identity.
            index = next((i for i, r in enumerate(options) if r["status"] == "ACTIVE"), 0)
            run = st.selectbox(
                COHORTS[cohort],
                options,
                index=index,
                format_func=lambda r: f"{r['name']} · {r['status']} · {r['run_id']}",
                key=f"paper_run_{cohort}",
            )
            if run is None:
                continue
            selected.append(run)
            st.code(run["run_id"], language=None)
            st.write(f"{run['status']} · activated {run['activated_at']}")
            st.metric("Current observed equity", fmt(run["equity"], "$"))
            st.caption(f"Last evaluation / update: {run['last_update'] or 'N/A'}")
            if cohort == "v2":
                st.caption(
                    "Retired strategy · historical comparator. Stored run status is shown above."
                )
            with st.expander("Run metadata"):
                st.json(
                    {
                        k: run.get(k)
                        for k in (
                            "execution_version",
                            "event_variant",
                            "policy_id",
                            "risk_policy_id",
                            "universe_id",
                            "baseline_run_id",
                        )
                    }
                    | {
                        "entry_universe_id": run["definition"].get("entry_universe_id"),
                        "hypothesis_count": len(run["hypotheses"]),
                        "event_hypothesis_count": sum(
                            h.get("source_phase") == 30 for h in run["hypotheses"].values()
                        ),
                        "baseline_nimble_runs": run["definition"].get("baseline_nimble_runs", []),
                        "thesis_policy_version": run["definition"].get("thesis_policy_version"),
                        "exit_policy_version": run["definition"].get("exit_policy_version"),
                        "comparison_review_at": run["definition"].get("comparison_review_at"),
                    }
                )
            if run["last_update"] is None or required_timestamp(
                pd.Timestamp.now(tz="UTC")
            ) - required_timestamp(run["last_update"]) > pd.Timedelta(minutes=30):
                st.warning("This run's update is stale or unavailable.")
    window = st.radio(
        "Performance window", ["All time", "24h", "7d", "30d", "Custom"], horizontal=True
    )
    end = required_timestamp(fresh["effective_at"])
    assert end is not None
    start = (
        None
        if window == "All time"
        else end - pd.Timedelta({"24h": "1D", "7d": "7D", "30d": "30D"}.get(window, "1D"))
    )
    if window == "Custom":
        a, b = st.columns(2)
        lo = a.date_input("From · UTC", value=cast(date, (end - pd.Timedelta(days=7)).date()))
        hi = b.date_input("Through · UTC", value=end.date())
        start, end = (
            required_timestamp(lo),
            min(end, required_timestamp(hi) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)),
        )
        if start > end:
            st.error("From must precede the end of the snapshot/window.")
            return
    st.caption(
        f"Window ends at {end} (snapshot observation time for local data). Current equity/open positions remain the latest recorded state; trade outcomes use exit time, trade frequency uses entry time."
    )
    with st.spinner("Reading paper execution reports…"):
        reports = [
            paper_data.report(str(path), revision, r["run_id"], start, end) for r in selected
        ]
        detail = {
            r.run["run_id"]: paper_data.details(str(path), revision, r.run["run_id"], start, end)
            for r in reports
        }
    if len(reports) >= 2:
        st.subheader("Run comparison")
        left, right = st.columns(2)
        comparator = left.selectbox("Comparator", reports, format_func=lambda r: r.run["label"])
        compared = right.selectbox(
            "Compared run",
            [r for r in reports if r is not comparator],
            format_func=lambda r: r.run["label"],
        )
        with paper_data.connection(path) as con:
            c = comparison(comparator, compared, con)
        st.info(c["conclusion"])
        expected_baseline = compared.run.get("baseline_run_id")
        if (
            comparator.run["system"] == "v2"
            and expected_baseline
            and expected_baseline != comparator.run["run_id"]
        ):
            st.warning(
                f"Selected baseline differs from nimble’s registered comparison baseline: {expected_baseline}. This is an alternate descriptive comparison."
            )
        st.caption(
            f"Common prospective entry interval: {c['start']} → {c['end']}. Closed samples: comparator {c['samples'][0]}, compared run {c['samples'][1]}. Differences are compared run minus comparator; independent accounts remain separate. V2 is retained for historical analysis."
        )
        diff_labels = {
            "net_pnl": "$",
            "average_hold_minutes": "f",
            "capital_utilization": "%",
            "trades_per_day": "f",
            "cost_drag": "$",
            "max_drawdown": "%",
            "mfe_capture_ratio_mean": "%",
        }
        table(
            [
                {
                    "Difference": k.replace("_", " "),
                    "Compared run − comparator": fmt(c["differences"][k], kind),
                }
                for k, kind in diff_labels.items()
            ]
        )
        st.caption(
            "Drawdown compares observed MTM over the common interval, against each run’s historical peak. Capture differences are N/A unless both runs have complete primary research paths. A count of 30 per run only removes the small-sample notice; it is not a significance test."
        )
    st.subheader("Comparison overview")
    table(
        [
            {"Metric": label, **{r.run["label"]: fmt(r.stats.get(k), kind) for r in reports}}
            for k, (label, kind) in METRICS.items()
        ]
    )
    st.subheader("Equity")
    draw_curves(reports, detail, st.toggle("Individual equity charts"))
    (
        pnl_tab,
        open_tab,
        closed_tab,
        side_tab,
        hypothesis_tab,
        exit_tab,
        activity_tab,
        decisions_tab,
    ) = st.tabs(
        [
            "PnL attribution",
            "Open positions",
            "Closed trades",
            "Long / short",
            "Hypotheses",
            "Nimble exits",
            "Activity",
            "Decisions / event cohorts",
        ]
    )
    with pnl_tab:
        st.caption(
            "Closed-trade attribution. Positive funding is a cost; negative funding is income. Price PnL − spread/slippage + liquidation adjustment − fees − funding − unsettled adverse reserve = net PnL. Open entry fees are included in realized cash, not closed attribution."
        )
        attribution = []
        for r in reports:
            for period, stat in (("Selected window", r.stats), ("Cumulative", r.cumulative)):
                attribution.append(
                    {
                        "Run": r.run["label"],
                        "Period": period,
                        **{
                            k: fmt(stat[k], "$")
                            for k in (
                                "price_pnl",
                                "slippage",
                                "liquidation_adjustment",
                                "fees",
                                "funding",
                                "unsettled_adverse_reserve",
                                "net_pnl",
                            )
                        },
                        "Provisional closed trades": stat["provisional_trades"],
                    }
                )
        table(attribution)
    with open_tab:
        st.caption(
            "Current positions across both sides. Dynamic levels come from the frozen nimble thesis. V2 has a fixed horizon; funding accrued is N/A when its mark payload does not record it."
        )
        table(
            [p for r in reports for p in r.open_positions(as_of=fresh["effective_at"])], OPEN_FIELDS
        )
    with closed_tab:
        for r in reports:
            st.markdown(f"**{r.run['label']}**")
            controls = st.columns(4)
            candidates = [t for t in r.trades if t["state"] == "CLOSED"]

            def choose(
                label,
                field,
                column,
                candidates=candidates,
                run_id=r.run["run_id"],
                hypotheses=r.run["hypotheses"],
            ):
                return column.selectbox(
                    label,
                    [None, *sorted({t[field] for t in candidates if t.get(field)})],
                    format_func=lambda v: (
                        f"{hypotheses.get(v, {}).get('name', v)} · {v}"
                        if v and field == "hypothesis_id"
                        else v or "All"
                    ),
                    key=f"{run_id}_{field}",
                )

            asset, side, hypothesis, reason = [
                choose(label, field, col)
                for label, field, col in zip(
                    ("Asset", "Side", "Hypothesis", "Exit reason"),
                    ("asset", "side", "hypothesis_id", "exit_reason"),
                    controls,
                    strict=True,
                )
            ]
            sorting, direction, page_control = st.columns(3)
            sort = sorting.selectbox(
                "Sort",
                sorted(SORT_FIELDS),
                index=sorted(SORT_FIELDS).index("exit_at"),
                key=f"sort_{r.run['run_id']}",
            )
            descending = direction.checkbox("Descending", value=True, key=f"desc_{r.run['run_id']}")
            filtered, total = r.closed_page(
                asset=asset,
                side=side,
                hypothesis=hypothesis,
                reason=reason,
                sort=sort,
                descending=descending,
            )
            pages = max(1, math.ceil(total / 50))
            page_key = f"page_{r.run['run_id']}"
            if st.session_state.get(page_key, 1) > pages:
                st.session_state[page_key] = 1
            page = (
                int(
                    page_control.number_input(
                        "Page", min_value=1, max_value=pages, value=1, step=1, key=page_key
                    )
                )
                - 1
            )
            filtered, _ = r.closed_page(
                asset=asset,
                side=side,
                hypothesis=hypothesis,
                reason=reason,
                sort=sort,
                descending=descending,
                page=page,
            )
            st.caption(
                f"{total:,} filtered closed trades · page {page + 1}/{pages} · up to 50 rows. Returns are fractions of entry notional. MFE/MAE use observed reference prices; capture requires a complete original primary research path."
            )
            table(filtered, TRADE_FIELDS)
    with side_tab:
        for r in reports:
            st.markdown(f"**{r.run['label']}**")
            table(r.groups(("side",)), ["side", *GROUP_FIELDS])
    with hypothesis_tab:
        st.caption(
            "Descriptive paper-execution performance. Primary hypothesis attribution only; supporting signals do not duplicate trade PnL. No scientific validity or graduation is inferred."
        )
        dims = st.multiselect(
            "Group by",
            ["hypothesis_id", "family", "asset", "side"],
            default=["hypothesis_id", "family", "asset", "side"],
        )
        for r in reports:
            if not dims:
                continue
            st.markdown(f"**{r.run['label']}**")
            groups = r.groups(tuple(dims))
            group_key = f"groups_{r.run['run_id']}"
            if st.session_state.get(group_key, 1) > max(1, math.ceil(len(groups) / 100)):
                st.session_state[group_key] = 1
            group_page = (
                int(
                    st.number_input(
                        "Group page · 100 rows",
                        1,
                        max(1, math.ceil(len(groups) / 100)),
                        value=1,
                        key=group_key,
                    )
                )
                - 1
            )
            table(groups[group_page * 100 : (group_page + 1) * 100], [*dims, *GROUP_FIELDS])
    with exit_tab:
        for r in reports:
            if r.run["system"] == "nimble":
                st.markdown(f"**{r.run['label']}**")
                table(r.exits())
                st.caption(
                    "Exact mechanical ledger reasons are retained, including additional reasons. Empty categories have count 0 and outcome N/A."
                )
    with activity_tab:
        st.caption(
            "Recent 50 fired opportunities / admissions / rejections / entries / exit intents / exits per run, with UTC timestamps. Full signal histories stay on the server."
        )
        for r in reports:
            with st.expander(r.run["label"]):
                table(detail[r.run["run_id"]][1])
                st.markdown("Top rejection reasons · selected window")
                table(r.rejections)
    with decisions_tab:
        decision_views(path, revision, reports, start, end)


def workflow():
    st.markdown(
        "Download an **existing** production backup from Railway with the read-only retrieval script. It verifies the checksum and writes a local provenance manifest. A backup may predate recent trades; download time is not snapshot time. The frontend has no production command or trading controls."
    )
    st.code(
        "uv run python scripts/pull_paper_snapshot.py\nPRISM_PAPER_DB_PATH=data/remote/paper-dashboard.duckdb uv run streamlit run dashboard/paper_app.py",
        language="bash",
    )
    st.caption(
        "Railway project must already be linked; PRISM_RAILWAY_SERVICE / PRISM_RAILWAY_VOLUME select the existing runtime service and volume. The page defaults to local snapshot mode. PRISM_PAPER_DATA_MODE=production is only for a frontend reading the actual production database, never a copied authority claim. See docs/PAPER_TRADER_DASHBOARD.md."
    )


try:
    main()
except DatabaseBusy as exc:
    st.warning(
        f"Paper source is busy: {exc}. The source is opened read-only; retry after the writer finishes."
    )
    if st.button("Retry read-only source"):
        paper_data.reload_local()
        st.rerun()
except (OSError, ValueError, duckdb.Error) as exc:
    st.error(
        f"Paper data could not be read: {exc}. Reload the local snapshot or check the configured source."
    )
