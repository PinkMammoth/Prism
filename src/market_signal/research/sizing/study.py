"""Full counterfactual sizing study -> machine-readable evidence (no database writes).

The selection rule is declared here, before results exist, and is applied mechanically:
see ``select``. Scenario streams are SYNTHETIC (scenario.py); real-ledger replay runs only
against an operator-supplied offline backup via ``market sizing replay --db``.
"""

from __future__ import annotations

import json
import math
import subprocess
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from . import audit, bootstrap, metrics, policy, prospective, replay, scenario, stress

PRIMARY_EQUITY = (100.0, 200.0)
SENSITIVITY_EQUITY = (500.0, 1000.0)


def candidates() -> list[policy.SizingPolicy]:
    return [
        policy.baseline(),
        policy.fixed_fractional(0.005),
        policy.invalidation_vol(0.005),
        policy.drawdown_throttled(0.0025),
        policy.drawdown_throttled(0.005),
        policy.drawdown_throttled(0.005, leverage_cap=2.0),
        policy.drawdown_throttled(0.0075),
        policy.drawdown_throttled(0.01),
        policy.drawdown_throttled(0.005, regime=True),
        policy.fractional_kelly(0.25, scope="pooled"),
    ]


SELECTION_RULE = {
    "eligible": "status == CANDIDATE (never CONTROL, RESEARCH_ONLY or STRESS_ONLY)",
    "constraints": [
        "every scenario, $100 and $200: block-bootstrap P(path loss >= 50%) <= the control's",
        "WEAK_EDGE, MODERATE_EDGE, REGIME_FLIP, $100 and $200: P(path loss >= 30%) <= 5%",
        "WEAK_EDGE, MODERATE_EDGE, REGIME_FLIP, $100 and $200: median max drawdown <= 20%",
        "WEAK_EDGE $200 stresses: terminal >= 80% of start under every stress",
        "zero liquidations in every replay",
    ],
    "ranking": "WEAK_EDGE $200 bootstrap median terminal equity; then prefer the LOWEST "
    "risk policy whose median lies within the best survivor's interquartile range "
    "(plateau preference, not the single best parameter)",
}


def _revision() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # pragma: no cover - outside a checkout
        return "unknown"


def describe(trades) -> dict:
    """Signal-stream geometry (policy-independent) for the later exit-geometry phase."""
    a = np.array

    def q(x):
        x = a(x, dtype=float)
        x = x[np.isfinite(x)]
        return (
            {str(p): float(np.quantile(x, p)) for p in (0.05, 0.25, 0.5, 0.75, 0.95)}
            if len(x)
            else {}
        )

    mid = [(t.entry_bid + t.entry_ask) / 2 for t in trades]
    inv = [t.sign * (m - t.invalidation_price) / m for t, m in zip(trades, mid, strict=True)]
    tgt = [t.sign * (t.target_price - m) / m for t, m in zip(trades, mid, strict=True)]
    vol = [t.causal_volatility or np.nan for t in trades]
    from collections import Counter

    return {
        "trades": len(trades),
        "days": round(
            (replay._ts(trades[-1].decision_at) - replay._ts(trades[0].decision_at)) / 86400, 1
        )
        if trades
        else 0,
        "exit_reasons": dict(sorted(Counter(t.exit_reason for t in trades).items())),
        "cohorts": dict(sorted(Counter(t.cohort for t in trades).items())),
        "families": dict(sorted(Counter(t.family.split(":")[0] for t in trades).items())),
        "invalidation_classes": dict(sorted(Counter(t.invalidation_class for t in trades).items())),
        "trades_per_hypothesis_median": float(
            np.median(list(Counter(t.hypothesis_id for t in trades).values()))
        )
        if trades
        else 0,
        "invalidation_distance": q(inv),
        "target_distance": q(tgt),
        "nominal_reward_risk": q([g / i for g, i in zip(tgt, inv, strict=True) if i > 0]),
        "causal_volatility": q(vol),
        "stop_tighter_than_half_volatility_share": float(
            np.mean([i < 0.5 * v for i, v in zip(inv, vol, strict=True) if math.isfinite(v)])
        ),
        "mfe": q([t.mfe for t in trades]),
        "mae": q([t.mae for t in trades]),
        "hold_minutes": q([t.hold_minutes for t in trades]),
    }


def _replays(args):
    name, trades, equities = args
    rows = []
    for p in policy.catalogue():
        for eq in equities:
            ind = metrics.independent_summary(replay.independent(trades, p, eq), eq)
            seq = metrics.sequential_summary(replay.sequential(trades, p, eq))
            rows.append(
                {
                    "scenario": name,
                    "policy": p.name,
                    "equity": eq,
                    "independent": ind,
                    "sequential": seq,
                }
            )
    return rows


def _boot(args):
    name, trades, p, eq, n, seed, block = args
    out = bootstrap.run(trades, p, eq, n=n, seed=seed, mean_block_days=block)
    return {"scenario": name, "policy": p.name, "equity": eq, **out}


def _stress(args):
    name, trades, p, eq = args
    rows = []
    for sname, st, realized in stress.scenarios(trades):
        s = metrics.sequential_summary(replay.sequential(st, p, eq, realized=realized))
        keep = (
            "terminal_equity",
            "total_return",
            "max_drawdown",
            "expectancy_r",
            "taken",
            "liquidations",
            "liquidation_near_misses",
            "worst_trade_r",
            "worst_trade_pct_equity",
            "halted_at",
            "min_equity",
        )
        rows.append(
            {
                "scenario": name,
                "policy": p.name,
                "equity": eq,
                "stress": sname,
                **{k: s.get(k) for k in keep},
            }
        )
    return rows


def _apply(boot_rows, stress_rows, replay_rows, *, skip_stress=()) -> dict:
    by = {
        (r["scenario"], r["policy"], r["equity"]): r for r in boot_rows if r["mean_block_days"] == 5
    }
    ctrl = policy.baseline().name
    positive = ("WEAK_EDGE", "MODERATE_EDGE", "REGIME_FLIP")
    verdicts = {}
    for p in candidates():
        if p.status != "CANDIDATE":
            continue
        fails = []
        for (sc, name, eq), r in sorted(by.items()):
            if name != p.name or eq not in PRIMARY_EQUITY:
                continue
            c = by.get((sc, ctrl, eq))
            if c and r["p_ruin"] > c["p_ruin"]:
                fails.append(f"{sc} ${eq:g}: P(ruin) {r['p_ruin']:.2f} > control {c['p_ruin']:.2f}")
            if sc in positive and r["p_path_loss_30"] > 0.05:
                fails.append(f"{sc} ${eq:g}: P(loss>=30%) {r['p_path_loss_30']:.2f}")
            if sc in positive and r["max_drawdown"]["0.5"] > 0.20:
                fails.append(f"{sc} ${eq:g}: median MDD {r['max_drawdown']['0.5']:.2f}")
        for st in stress_rows:
            if (
                st["policy"] == p.name
                and st["scenario"] == "WEAK_EDGE"
                and st["equity"] == 200.0
                and st["stress"] not in skip_stress
                and st["terminal_equity"] < 0.8 * 200.0
            ):
                fails.append(f"stress {st['stress']}: terminal {st['terminal_equity']:.1f}")
        for r in replay_rows:
            if r["policy"] == p.name and r["sequential"]["liquidations"]:
                fails.append(f"{r['scenario']} ${r['equity']:g}: liquidation")
        verdicts[p.name] = fails
    survivors = [n for n, f in verdicts.items() if not f]
    out = {"failures": verdicts, "survivors": survivors, "selected": None}
    if survivors:
        med = {n: by[("WEAK_EDGE", n, 200.0)]["terminal_equity"]["0.5"] for n in survivors}
        best = max(med, key=med.get)
        lo = by[("WEAK_EDGE", best, 200.0)]["terminal_equity"]["0.25"]
        plateau = [n for n in survivors if med[n] >= lo]
        risk = {p.name: (p.risk_fraction or 0, p.leverage_cap) for p in candidates()}
        out.update(
            weak_edge_median_terminal=med,
            best=best,
            plateau=plateau,
            selected=min(plateau, key=lambda n: risk[n]),
        )
    return out


def select(boot_rows, stress_rows, replay_rows) -> dict:
    """Apply SELECTION_RULE mechanically; also report a labelled post-hoc variant."""
    strict = _apply(boot_rows, stress_rows, replay_rows)
    composite = tuple(
        sorted({s["stress"] for s in stress_rows if s["stress"].startswith("combined")})
    )
    single = _apply(boot_rows, stress_rows, replay_rows, skip_stress=composite)
    return {
        "rule": SELECTION_RULE,
        "strict": strict,
        "post_hoc_single_factor_stresses": {
            **single,
            "label": "POST-HOC: excludes the composite stress "
            + ", ".join(composite)
            + " after the strict rule admitted no survivor; the composite turns the WEAK_EDGE "
            "stream negative for every policy, the control included",
        },
        "selected": strict["selected"] or single["selected"],
        "selected_under": "strict"
        if strict["selected"]
        else "post_hoc_single_factor_stresses"
        if single["selected"]
        else None,
    }


def control_impact_parity(streams) -> list[dict]:
    """The control keeps today's constant slippage in every main replay. At $500+ its $100+
    notionals would also pay impact, so compare it on the candidates' cost model here."""
    rows = []
    sized = policy.baseline()
    parity = sized.model_copy(update={"cost_model": policy.CostModel()})
    for n in ("WEAK_EDGE", "NULL_EDGE", "MODERATE_EDGE"):
        for p, label in ((sized, "control_constant_slippage"), (parity, "control_sqrt_impact")):
            for eq in PRIMARY_EQUITY + SENSITIVITY_EQUITY:
                s = metrics.sequential_summary(replay.sequential(streams[n], p, eq))
                rows.append(
                    {
                        "scenario": n,
                        "policy": label,
                        "equity": eq,
                        "terminal_equity": s["terminal_equity"],
                        "max_drawdown": s["max_drawdown"],
                        "expectancy_r": s["expectancy_r"],
                    }
                )
    return rows


def run(out_dir: Path, *, bootstrap_n: int = 200, processes: int = 4) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC).isoformat()
    streams = {name: scenario.generate(sc) for name, sc in scenario.SCENARIOS.items()}
    equities = PRIMARY_EQUITY + SENSITIVITY_EQUITY
    with Pool(processes) as pool:
        replay_rows = [
            r
            for rows in pool.map(_replays, [(n, t, equities) for n, t in streams.items()])
            for r in rows
        ]
        jobs = [
            (n, t, p, eq, bootstrap_n, 7, 5.0)
            for n, t in streams.items()
            for p in candidates()
            for eq in PRIMARY_EQUITY
        ] + [
            (n, t, p, 200.0, bootstrap_n, 7, 1.0) for n, t in streams.items() for p in candidates()
        ]
        boot_rows = pool.map(_boot, jobs)
        stress_rows = [
            r
            for rows in pool.map(
                _stress,
                [
                    (n, streams[n], p, eq)
                    for n in ("WEAK_EDGE", "NULL_EDGE", "REGIME_FLIP")
                    for p in candidates()
                    for eq in (100.0, 200.0)
                ],
            )
            for r in rows
        ]
    # Cost-model sensitivity for the size-dependent impact assumption.
    cost_rows = []
    for n in ("WEAK_EDGE", "NULL_EDGE"):
        for p in candidates():
            for label, cm in (
                ("sqrt_impact_ref_100", p.cost_model),
                ("no_impact", p.cost_model.model_copy(update={"impact": "none"})),
                (
                    "sqrt_impact_ref_50",
                    p.cost_model.model_copy(
                        update={
                            "impact": "sqrt_reference_notional",
                            "impact_reference_notional": 50.0,
                        }
                    ),
                ),
            ):
                q = p.model_copy(update={"cost_model": cm})
                for eq in equities:
                    s = metrics.sequential_summary(replay.sequential(streams[n], q, eq))
                    cost_rows.append(
                        {
                            "scenario": n,
                            "policy": p.name,
                            "cost_model": label,
                            "equity": eq,
                            "terminal_equity": s["terminal_equity"],
                            "max_drawdown": s["max_drawdown"],
                            "expectancy_r": s["expectancy_r"],
                            "fees": s["fees"],
                        }
                    )
    selection = select(boot_rows, stress_rows, replay_rows)
    meta = {
        "version": policy.VERSION,
        "kind": "COUNTERFACTUAL_SIZING_REPLAY on SYNTHETIC scenario streams (not Prism evidence)",
        "revision": _revision(),
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "scenarios": {
            n: {**sc.__dict__, "scenario_id": sc.scenario_id}
            for n, sc in scenario.SCENARIOS.items()
        },
        "equities": equities,
        "bootstrap": {"n": bootstrap_n, "seed": 7, "block_days": [5.0, 1.0]},
        "assumptions": [
            "signals, timestamps, prices, exit decisions and funding frozen per stream",
            "15 s executable ticks; stop and target fills at the next tick after the decision",
            "costs: 4.5 bps taker each side, frozen per-asset slippage, sqrt impact above $100",
            "isolated margin; liquidation at 1/L - 1/(2*venue max leverage) adverse",
            "sequential replay uses realized (closed-trade) equity for sizing and drawdown",
            "state-rule invalidations, cost-decay and data-failure exits not simulated",
        ],
    }
    files = {
        "meta.json": meta,
        "audit.json": audit.audit(),
        "policies.json": [p.identity() for p in policy.catalogue()],
        "prospective-design.json": {**prospective.design(), "design_id": prospective.design_id()},
        "streams.json": {n: describe(t) for n, t in streams.items()},
        "replays.json": replay_rows,
        "bootstrap.json": boot_rows,
        "stress.json": stress_rows,
        "cost-sensitivity.json": cost_rows,
        "control-impact-parity.json": control_impact_parity(streams),
        "selection.json": selection,
    }
    import gzip

    for name, value in files.items():
        text = json.dumps(value, indent=1, sort_keys=True, default=str) + "\n"
        if name == "replays.json":
            # Deterministic gzip (mtime=0) so regenerated evidence is byte-stable.
            with (
                open(out_dir / (name + ".gz"), "wb") as f,
                gzip.GzipFile(fileobj=f, mode="wb", mtime=0, filename="") as g,
            ):
                g.write(text.encode())
            continue
        (out_dir / name).write_text(text)
    return {"out_dir": str(out_dir), "selection": selection["selected"], "files": sorted(files)}
