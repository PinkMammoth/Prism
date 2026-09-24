"""Plotly figures using the validated reference palette (dataviz skill).

- categorical slots in FIXED order (never cycled); one y-axis per panel
- up/down and positive/negative use the blue↔red diverging pair, grey midpoint
- thin marks, recessive grid, legend for >=2 series
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
UP, DOWN, MID = "#2a78d6", "#e34948", "#f0efec"
GRID, MUTED = "rgba(137,135,129,0.25)", "#898781"
ZONE_FILL = {"entry": "rgba(42,120,214,0.10)", "fair": "rgba(137,135,129,0.12)", "accumulate": "rgba(27,175,122,0.12)",
             "strong_buy": "rgba(0,131,0,0.12)"}  # fmt: skip


def _style(fig: go.Figure, height: int = 520) -> go.Figure:
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified",
                      legend=dict(orientation="h", y=1.04, x=0), xaxis_rangeslider_visible=False)  # fmt: skip
    fig.update_xaxes(showgrid=False, linecolor=GRID)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


def price_chart(
    bars: pd.DataFrame,
    feat: pd.DataFrame | None,
    zones: dict | None,
    title: str = "",
    mas=(20, 50, 200),
) -> go.Figure:
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.8, 0.2], vertical_spacing=0.03
    )
    x = bars["ts"]
    fig.add_trace(go.Candlestick(x=x, open=bars["open"], high=bars["high"], low=bars["low"], close=bars["close"], name="price",
                                 increasing=dict(line=dict(color=UP, width=1), fillcolor=UP),
                                 decreasing=dict(line=dict(color=DOWN, width=1), fillcolor=DOWN)), row=1, col=1)  # fmt: skip
    if feat is not None:
        for slot, n in enumerate(mas, start=1):  # slot 0 reserved (price = blue)
            col = f"sma_{n}"
            if col in feat:
                fig.add_trace(go.Scatter(x=feat["ts"], y=feat[col], name=f"SMA{n}", mode="lines",
                                         line=dict(color=SERIES[slot], width=2)), row=1, col=1)  # fmt: skip
    if zones:

        def band(rng, key, label, position):
            if not rng:
                return
            lo, hi = rng
            if (
                not lo or hi is None or hi <= 0
            ):  # open-ended zones ("≤ X") are shown by their edge only
                return
            fig.add_hrect(y0=lo, y1=hi, fillcolor=ZONE_FILL[key], line_width=0, row=1, col=1,
                          annotation_text=label, annotation_position=position, annotation_font_color=MUTED)  # fmt: skip

        band(zones.get("fair"), "fair", "fair value", "top left")
        band(zones.get("accumulate"), "accumulate", "accumulate", "bottom left")
        band(zones.get("entry_zone"), "entry", "entry zone", "top right")
        inv = zones.get("invalidation")
        if inv:
            fig.add_hline(y=inv, line=dict(color=DOWN, width=1, dash="dot"), row=1, col=1,
                          annotation_text="invalidation", annotation_font_color=MUTED)  # fmt: skip
    vol_colors = [UP if c >= o else DOWN for o, c in zip(bars["open"], bars["close"], strict=True)]
    fig.add_trace(
        go.Bar(
            x=x,
            y=bars["volume"],
            marker_color=vol_colors,
            name="volume",
            showlegend=False,
            opacity=0.6,
        ),
        row=2,
        col=1,
    )
    lo, hi = bars["low"].min(), bars["high"].max()
    fig.update_yaxes(range=[lo * 0.95, hi * 1.05], row=1, col=1)
    fig.update_layout(title=title)
    return _style(fig)


def equity_chart(equity: pd.Series) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.04,
                        subplot_titles=("Equity (net of costs)", "Drawdown"))  # fmt: skip
    fig.add_trace(
        go.Scatter(
            x=equity.index,
            y=equity.values,
            mode="lines",
            line=dict(color=SERIES[0], width=2),
            name="equity",
        ),
        row=1,
        col=1,
    )
    dd = equity / equity.cummax() - 1
    fig.add_trace(go.Scatter(x=dd.index, y=dd.values, mode="lines", fill="tozeroy", line=dict(color=DOWN, width=1),
                             fillcolor="rgba(227,73,72,0.15)", name="drawdown"), row=2, col=1)  # fmt: skip
    fig.update_yaxes(tickformat=".0%", row=2, col=1)
    fig.update_layout(showlegend=False)
    return _style(fig, 460)


def diverging_bars(df: pd.DataFrame, cat: str, val: str, title: str) -> go.Figure:
    d = df.sort_values(val)
    colors = [UP if v > 0 else DOWN for v in d[val]]
    fig = go.Figure(go.Bar(x=d[val], y=d[cat], orientation="h", marker_color=colors,
                           text=[f"{v:+.1%}" for v in d[val]], textposition="outside",
                           hovertemplate="%{y}: %{x:+.2%}<extra></extra>"))  # fmt: skip
    fig.add_vline(x=0, line=dict(color=MUTED, width=1))
    fig.update_xaxes(tickformat=".0%")
    fig.update_layout(title=title, hovermode="closest")
    return _style(fig, max(260, 28 * len(d) + 80))


def sensitivity_heatmap(
    df: pd.DataFrame, x: str, y: str | None, val: str = "excess_mean"
) -> go.Figure:
    if y is None:
        return diverging_bars(df.assign(**{x: df[x].astype(str)}), x, val, f"Excess vs {x}")
    piv = df.pivot(index=y, columns=x, values=val)
    m = float(piv.abs().max().max() or 0.01)
    fig = go.Figure(go.Heatmap(z=piv.values, x=[str(c) for c in piv.columns], y=[str(i) for i in piv.index],
                               colorscale=[[0, DOWN], [0.5, MID], [1, UP]], zmin=-m, zmax=m,
                               text=[[f"{v:+.1%}" if v == v else "" for v in row] for row in piv.values],
                               texttemplate="%{text}", colorbar=dict(tickformat=".0%", title="excess"),
                               hovertemplate=f"{x}=%{{x}}<br>{y}=%{{y}}<br>excess=%{{z:+.2%}}<extra></extra>"))  # fmt: skip
    fig.update_layout(xaxis_title=x, yaxis_title=y, hovermode="closest")
    return _style(fig, 420)


def line_chart(s: pd.Series, name: str, fmt: str = "", title: str = "") -> go.Figure:
    fig = go.Figure(
        go.Scatter(
            x=s.index, y=s.values, mode="lines", line=dict(color=SERIES[0], width=2), name=name
        )
    )
    if fmt:
        fig.update_yaxes(tickformat=fmt)
    fig.update_layout(title=title, showlegend=False)
    return _style(fig, 300)
