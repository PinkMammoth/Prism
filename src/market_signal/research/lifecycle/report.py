"""Plain-text renderings of a lifecycle study payload and of stored edge profiles."""

from __future__ import annotations


def _f(x, pct: bool = True, digits: int = 2) -> str:
    if x is None:
        return "-"
    return f"{100 * x:+.{digits}f}%" if pct else f"{x:.{digits}f}"


def study_report(payload: dict) -> str:
    out = [f"Phase 20 edge-lifecycle study {payload['study_id']}",
           payload["statement"], f"policy {payload['policy_id']}; horizon "
           f"{payload['horizon_bars']} bars; cutoff {payload['cutoff']}; economic floor "
           f"{_f(payload['economic_floor'])}/event", ""]  # fmt: skip
    for venue, v in payload["venues"].items():
        a = v["aggregate"]
        out.append(f"== {venue} ({v['role']}): {a['strategies_with_outcomes']} strategies, "
                   f"{v['evaluations']} weekly evaluations, current regime {v['current_regime']}")  # fmt: skip
        out.append(f"edge states at cutoff: {a['edge_states_at_cutoff']}")
        out.append(f"divergence patterns:   {a['divergence_patterns_at_cutoff']}")
        out.append(
            f"recent-mode lifecycle states at cutoff: {a['recent_lifecycle_states_at_cutoff']}"
        )
        out.append("mode      net_sum   per_event  partic.  active  acts  deacts  react  bursts  "
                   "spell_d  mdd_mean  better_than_static")  # fmt: skip
        for m, s in a["modes"].items():
            out.append(f"{m:8s} {s['net_sum_total']:+8.2f}  {_f(s['net_mean_per_event']):>9s}  "
                       f"{s['events_participating']:6d}  {_f(s['mean_active_time_share'], False):>6s}"
                       f"  {s['activations']:4d}  {s['deactivations']:6d}  {s['reactivations']:5d}"
                       f"  {s['short_bursts']:6d}  {_f(s['mean_spell_days'], False, 0):>7s}"
                       f"  {_f(s['mean_max_drawdown'], False):>8s}  "
                       f"{s['strategies_better_than_static'] if s['strategies_better_than_static'] is not None else '-'}")  # fmt: skip
        out.append(f"poor lifetime, strong recent: {len(a['poor_lifetime_strong_recent'])}; "
                   f"strong lifetime, recent gone: {len(a['strong_lifetime_recent_gone'])}; "
                   f"ever EMERGING during walk-forward: {len(a['ever_emerging_during_walk_forward'])}")  # fmt: skip
        for r in a["poor_lifetime_strong_recent"][:8]:
            out.append(f"  + {r['strategy']}: lifetime {_f(r['lifetime_mean'])} (t {_f(r['lifetime_t'], False)})"
                       f" vs recent[{r['recent_window'] or 'insufficient, n=' + str(r['recent_n'])}] {_f(r['recent_mean'])} (t {_f(r['recent_t'], False)})"
                       f" -> {r['edge_state']}/{r['pattern']}")  # fmt: skip
        for r in a["strong_lifetime_recent_gone"][:8]:
            out.append(f"  - {r['strategy']}: lifetime {_f(r['lifetime_mean'])} (t {_f(r['lifetime_t'], False)})"
                       f" vs recent[{r['recent_window'] or 'insufficient, n=' + str(r['recent_n'])}] {_f(r['recent_mean'])} (t {_f(r['recent_t'], False)})"
                       f" -> {r['edge_state']}/{r['pattern']}")  # fmt: skip
        st = a["stress"]
        out.append(f"stress: {len(v['stress_episodes'])} episodes, {v['stress_days']} stress days; "
                   f"net all {st['all_net_sum']:+.2f} = normal {st['normal_net_sum']:+.2f} + "
                   f"stress {st['stress_net_sum']:+.2f}")  # fmt: skip
        out.append(f"lifetime class all->normal: {st['lifetime_class_all_vs_normal']}")
        for m in ("recent", "regime"):
            s = a["modes"][m]
            out.append(f"mortality [{m}]: per event while participating {_f(s['net_mean_per_event'])}"
                       f", while not {_f(s['net_mean_not_participating_per_event'])}; avoided "
                       f"losses {s['avoided_losses_total']:.2f}, missed upside "
                       f"{s['missed_upside_total']:.2f}; reacquisition success "
                       f"{_f(s['mean_reacquisition_success_share'], False)}")  # fmt: skip
        out.append("family:side          n  static_net  recent_net  regime_net  active  states")
        for k, f in sorted(a["families"].items()):
            out.append(f"{k:28s} {f['variants']:2d}  {f['static_net']:+9.2f}  {f['recent_net']:+9.2f}"
                       f"  {f['regime_net']:+9.2f}  {_f(f['recent_active_share'], False)}  "
                       f"{f['states']}")  # fmt: skip
        out.append("eras (calendar years): " + "; ".join(
            f"{e['era']} {e['positive_share']:.0%} positive" for e in a["eras"]))  # fmt: skip
        mv = a["multi_edge_view"]["strategies"]
        out.append(f"multi-edge research view at cutoff (simulated participation): {len(mv)}")
        for r in mv:
            out.append(f"  {r['strategy']}: HL-90 edge {_f(r['expected_current_edge'])} +/- "
                       f"{_f(r['uncertainty'])}, 365d drawdown {_f(r['recent_drawdown_365d'], False)}"
                       f", {r['edge_state']}, regime tracks {r['regime_tracks']}")  # fmt: skip
        out.append("")
    return "\n".join(out)


def profile_status(p: dict) -> dict:
    """The compact answer to "does this strategy appear to have a credible edge now?"."""
    r = p["research_modes"]["recent"]
    life = p["lifetime"]
    return {
        "profile_id": p.get("profile_id"),
        "strategy": p["strategy_name"],
        "venue": p["venue"],
        "as_of": p["as_of"],
        "data_cutoff": p["data_cutoff"],
        "policy_id": p["policy_id"],
        "edge_state": p["edge_state"]["state"],
        "edge_state_reason": p["edge_state"]["reason"],
        "divergence": p["divergence"]["pattern"],
        "lifetime": {"mean": life.get("mean"), "t": life.get("t"), "n": life.get("n")},
        "recent": {"window": r.get("selected") or r.get("window"), "mean": r.get("mean"),
                   "t": r.get("t"), "n": r.get("n")},
        "regime_hierarchy": p["regime"].get("hierarchy"),
        "decay": {k: p["decay"].get(k) for k in ("days_since_peak", "rolling_slope_per_year",
                                                 "consecutive_windows_below_floor")},
        "forward": p["forward"].get("maturity") or p["forward"].get("status"),
        "simulated_lifecycle_state": p["lifecycle"]["recent"]["final_states"].get("all"),
        "diagnostics": p["diagnostics"],
        "evidence_class": p["evidence_class"],
    }  # fmt: skip


def profile_compare(p: dict) -> dict:
    """Lifetime vs recent vs regime vs forward, side by side."""
    keep = ("n", "assets", "mean", "se", "t", "hit_rate", "excess_mean", "adequate")
    w = {k: {x: v.get(x) for x in keep} for k, v in p["windows"].items()}
    wt = {k: {x: v.get(x) for x in (*keep, "ess")} for k, v in p["weighted"].items()}
    return {"strategy": p["strategy_name"], "venue": p["venue"], "as_of": p["as_of"],
            "lifetime": {x: p["lifetime"].get(x) for x in keep}, "windows": w, "weighted": wt,
            "regime": {k: p["regime"].get(k) for k in ("current", "current_regime_historical",
                                                       "recent_current_regime", "hierarchy")},
            "stress": {k: p["stress"][k]["lifetime"] for k in ("all", "normal", "stress")},
            "forward": p["forward"], "divergence": p["divergence"]}  # fmt: skip
