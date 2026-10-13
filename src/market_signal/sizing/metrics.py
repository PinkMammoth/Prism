"""Policy metrics. Terminal balance alone never selects a policy."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import UTC, datetime

import numpy as np

from .replay import Outcome, SequentialResult

DAY = 86400.0


def _f(x):
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def trade_stats(outs: list[Outcome]) -> dict:
    taken = [o for o in outs if o.accepted]
    rs = np.array([o.r for o in taken if o.r is not None], dtype=float)
    pnl = np.array([o.net_pnl for o in taken], dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    rw = rs[rs > 0]
    rl = rs[rs <= 0]
    by_exit: dict[str, list[float]] = defaultdict(list)
    for o in taken:
        if o.r is not None:
            by_exit[o.exit_reason or "?"].append(o.r)
    return {
        "signals": len(outs),
        "taken": len(taken),
        "skipped": len(outs) - len(taken),
        "skip_reasons": dict(sorted(Counter(o.reason for o in outs if not o.accepted).items())),
        "reduced": sum(bool(o.bound_by) for o in taken),
        "bound_by": dict(sorted(Counter(b for o in taken for b in o.bound_by).items())),
        "win_rate": _f(len(wins) / len(pnl)) if len(pnl) else None,
        "avg_pnl": _f(pnl.mean()) if len(pnl) else None,
        "expectancy_r": _f(rs.mean()) if len(rs) else None,
        "median_r": _f(np.median(rs)) if len(rs) else None,
        "r_std": _f(rs.std(ddof=1)) if len(rs) > 1 else None,
        "r_quantiles": {
            str(q): _f(np.quantile(rs, q)) for q in (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)
        }
        if len(rs)
        else {},
        "avg_win_r": _f(rw.mean()) if len(rw) else None,
        "avg_loss_r": _f(rl.mean()) if len(rl) else None,
        "payoff_ratio": _f(wins.mean() / -losses.mean())
        if len(wins) and len(losses) and losses.mean() < 0
        else None,
        "profit_factor": _f(wins.sum() / -losses.sum())
        if len(losses) and losses.sum() < 0
        else None,
        "worst_trade": _f(pnl.min()) if len(pnl) else None,
        "worst_trade_r": _f(rs.min()) if len(rs) else None,
        "best_trade_r": _f(rs.max()) if len(rs) else None,
        "losses_beyond_1_2r": int((rs <= -1.2).sum()),
        "avg_risk_fraction": _f(np.mean([o.risk_fraction for o in taken])) if taken else None,
        "risk_fraction_quantiles": {
            str(q): _f(np.quantile([o.risk_fraction for o in taken], q)) for q in (0.05, 0.5, 0.95)
        }
        if taken
        else {},
        "risk_dispersion_p95_over_p5": _f(
            np.quantile([o.risk_fraction for o in taken], 0.95)
            / max(np.quantile([o.risk_fraction for o in taken], 0.05), 1e-12)
        )
        if taken
        else None,
        "worst_trade_pct_equity": _f(min(o.net_pnl / o.equity_before for o in taken))
        if taken
        else None,
        "max_risk_fraction": _f(max(o.risk_fraction for o in taken)) if taken else None,
        "avg_leverage": _f(np.mean([o.leverage for o in taken])) if taken else None,
        "max_leverage": _f(max(o.leverage for o in taken)) if taken else None,
        "avg_notional": _f(np.mean([o.notional for o in taken])) if taken else None,
        "fees": _f(sum(o.fees for o in taken)),
        "funding": _f(sum(o.funding for o in taken)),
        "liquidations": sum(o.liquidated for o in taken),
        "liquidation_near_misses": sum(o.near_miss for o in taken),
        "min_liquidation_distance": _f(
            min((o.liquidation_distance for o in taken if o.liquidation_distance), default=None)
        ),
        "r_by_exit_reason": {
            k: {"n": len(v), "mean_r": _f(np.mean(v))} for k, v in sorted(by_exit.items())
        },
    }


def independent_summary(outs: list[Outcome], equity: float) -> dict:
    out = trade_stats(outs)
    taken = [o for o in outs if o.accepted]
    pct = np.array([o.net_pnl / equity for o in taken], dtype=float)
    out.update(
        {
            "starting_equity": equity,
            "mean_pct_equity": _f(pct.mean()) if len(pct) else None,
            "sum_pct_equity_non_compounded": _f(pct.sum()) if len(pct) else None,
            "worst_pct_equity": _f(pct.min()) if len(pct) else None,
            "pct_equity_std": _f(pct.std(ddof=1)) if len(pct) > 1 else None,
        }
    )
    return out


def drawdowns(curve: list[tuple[float, float]]) -> dict:
    if not curve:
        return {}
    t = np.array([c[0] for c in curve], dtype=float)
    e = np.array([c[1] for c in curve], dtype=float)
    peak = np.maximum.accumulate(e)
    dd = 1 - e / peak
    episodes = []  # (depth, start_t, trough_t, recovered_t | None)
    start = trough = None
    depth = 0.0
    for i in range(len(e)):
        if dd[i] > 0:
            if start is None:
                start, depth, trough = t[i - 1] if i else t[i], 0.0, t[i]
            if dd[i] > depth:
                depth, trough = dd[i], t[i]
        elif start is not None:
            episodes.append((depth, start, trough, t[i]))
            start = None
    if start is not None:
        episodes.append((depth, start, trough, None))
    end = t[-1]
    worst = max(episodes, key=lambda x: x[0]) if episodes else None
    return {
        "max_drawdown": _f(dd.max()),
        "median_drawdown": _f(np.median([x[0] for x in episodes])) if episodes else 0.0,
        "drawdown_episodes": len(episodes),
        "longest_drawdown_days": _f(max(((x[3] or end) - x[1]) / DAY for x in episodes))
        if episodes
        else 0.0,
        "max_drawdown_recovery_days": None
        if not worst or worst[3] is None
        else _f((worst[3] - worst[2]) / DAY),
        "max_drawdown_recovered": bool(worst is None or worst[3] is not None),
        "min_equity": _f(e.min()),
    }


def _daily(curve, start_t, end_t):
    if not curve:
        return np.array([])
    days = math.floor((end_t - start_t) / DAY) + 1
    t = np.array([c[0] for c in curve])
    e = np.array([c[1] for c in curve])
    edges = start_t + DAY * np.arange(1, days + 1)
    idx = np.searchsorted(t, edges, side="right") - 1
    return e[np.clip(idx, 0, len(e) - 1)]


def sequential_summary(res: SequentialResult) -> dict:
    eq0 = res.starting_equity
    outs = res.outcomes
    out = trade_stats(outs)
    terminal = res.curve[-1][1] if res.curve else eq0
    n = out["taken"]
    out.update(
        {
            "policy": res.policy.name,
            "policy_id": res.policy.policy_id,
            "starting_equity": eq0,
            "terminal_equity": _f(terminal),
            "total_return": _f(terminal / eq0 - 1),
            "geometric_return_per_trade": _f((terminal / eq0) ** (1 / n) - 1)
            if n and terminal > 0
            else None,
            "halted_at": None
            if res.halted_at is None
            else datetime.fromtimestamp(res.halted_at, UTC).isoformat(),
        }
    )
    out.update(drawdowns(res.curve))
    if res.curve:
        start_t, end_t = res.curve[0][0], res.curve[-1][0]
        daily = _daily(res.curve, start_t, end_t)
        rets = np.diff(np.r_[eq0, daily]) / np.r_[eq0, daily][:-1] if len(daily) else np.array([])
        days = len(daily)
        meaningful = days >= 60 and n >= 30
        sd = rets.std(ddof=1) if len(rets) > 1 else 0.0
        downside = rets[rets < 0]
        dsd = math.sqrt((downside**2).mean()) if len(downside) else 0.0
        peak = np.maximum.accumulate(np.r_[eq0, daily])
        ddp = 100 * (1 - np.r_[eq0, daily] / peak)
        by_day: dict[int, float] = defaultdict(float)
        for o in outs:
            if o.accepted:
                from .replay import _ts

                by_day[int((_ts(o.exit_at) - start_t) // DAY)] += o.net_pnl
        worst_day = min(by_day.values(), default=0.0)
        out.update(
            {
                "days": days,
                "equity_daily_volatility": _f(sd),
                "sharpe_annualized": _f(rets.mean() / sd * math.sqrt(365))
                if meaningful and sd > 0
                else None,
                "sortino_annualized": _f(rets.mean() / dsd * math.sqrt(365))
                if meaningful and dsd > 0
                else None,
                "ratios_meaningful": meaningful,
                "ulcer_index": _f(math.sqrt((ddp**2).mean())),
                "worst_day_pnl": _f(worst_day),
                "worst_day_pct_start": _f(worst_day / eq0),
            }
        )
    ex = np.array(res.exposure) if res.exposure else np.zeros((0, 4))
    out.update(
        {
            "avg_gross_exposure": _f(ex[:, 1].mean()) if len(ex) else 0.0,
            "max_gross_exposure": _f(ex[:, 1].max()) if len(ex) else 0.0,
            "max_margin_utilization": _f(ex[:, 2].max()) if len(ex) else 0.0,
            "max_open_risk_fraction": _f(ex[:, 3].max()) if len(ex) else 0.0,
        }
    )
    return out
