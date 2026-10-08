"""``market lab microstructure``: Phase 24B prospective microstructure direction study
(``microstructure_absorption_v1``; research only, never a live signal).

READ-ONLY:
  status                production evidence (24A cutover, coverage, warmups, OI/context
                        freshness), study registration, latest maturity and checkpoint
  definition            the frozen definition and its study ID (from this repository + config)
  calibrate             null / planted calibration on synthetic markets (no data)
  study                 registration + every checkpoint
  results [CHECKPOINT]  a stored checkpoint result (default: the latest)
  history               every checkpoint and each member's verdict path (no best-date view)
  events                events of one member, recomputed on the data held at --as-of
  explain EVENT_ID      why one event qualified (machine-readable)
  state ASSET           the asset's latest closed 15m window, the hypotheses firing now and
                        the study's current evidence (for agents; no raw-table scraping)
  describe              a NON-GOVERNED descriptive evaluation (smoke; nothing recorded)

WRITES (append-only, migration 24):
  register --reason     freeze the study (evaluates nothing)
  checkpoint            evaluate the next owed grid instant(s) (--due), or --reproduce one

No command accepts a threshold, horizon or cost, and governed checkpoints never accept a
date: the fixed UTC grid decides. Nothing here places an order, touches the paper account,
the co-pilot, risk policy or the Phase 21 incubation freeze, and nothing activates a strategy.
"""

from __future__ import annotations

import json

import pandas as pd
import typer

from market_signal.cli.common import console, open_store

microdir = typer.Typer(
    no_args_is_help=True,
    help="Phase 24B microstructure direction study: prospective research, never a live signal.",
)
JSON = typer.Option(False, "--json", help="Machine-readable output")
AS_OF = typer.Option(None, "--as-of", help="Inspection instant (default: latest checkpoint, "
                                           "else now - 1 h); never recorded")  # fmt: skip


def _fail(exc: Exception) -> None:
    console.print(f"Microstructure study: {exc}", style="red", markup=False)
    raise typer.Exit(1) from None


def _out(obj, as_json: bool) -> None:
    from market_signal.research.microdir.analysis import clean

    if as_json:
        print(json.dumps(clean(obj), indent=2, sort_keys=True, default=str))
    else:
        console.print_json(json.dumps(clean(obj), default=str))


def _defn(settings):
    from market_signal.perps.data import perp_config
    from market_signal.research.microdir import spec as sp

    return sp.build_definition(perp_config(settings))


def _study(store):
    from market_signal.research.microdir import governance as gv

    try:
        return gv.get_study(store)
    except gv.MicroStudyError:
        return None, None


def _as_of(store, as_of: str | None) -> pd.Timestamp:
    from market_signal.research.microdir import governance as gv

    if as_of:
        return pd.Timestamp(as_of).tz_convert("UTC") if pd.Timestamp(as_of).tzinfo else \
            pd.Timestamp(as_of).tz_localize("UTC")  # fmt: skip
    _, row = _study(store)
    if row is not None:
        last = gv.latest(store, row["study_id"])
        if last:
            return pd.Timestamp(last["checkpoint"]["as_of"])
    return (pd.Timestamp.now(tz="UTC") - gv.SETTLE).floor("min")


def _panel(store, as_of, coins=None, since=None):
    from market_signal.research.microdir import analysis as an
    from market_signal.research.microdir import data
    from market_signal.research.microdir import spec as sp

    frames, meta = data.load_frames(store, as_of, coins or sp.COINS, since=since)
    P = an.panel(frames, crowding=data.crowding_provider(store),
                 context=data.context_provider(store))  # fmt: skip
    return P, meta


@microdir.command("status")
def status_cmd(as_json: bool = JSON) -> None:
    """READ-ONLY: production evidence, registration, latest checkpoint."""
    from market_signal.research.microdir import data
    from market_signal.research.microdir import governance as gv

    with open_store(read_only=True) as (settings, store):
        ev = data.production_evidence(store)
        defn = _defn(settings)
        _, row = _study(store)
        out = {"production": ev, "study": {"name": defn.name, "study_id_from_code": defn.study_id,
                                           "registered": row is not None}}  # fmt: skip
        if row is not None:
            out["study"].update(study_id=row["study_id"], registered_at=row["registered_at"],
                                matches_code=row["study_id"] == defn.study_id)  # fmt: skip
            last = gv.latest(store, row["study_id"])
            if last:
                p = last["payload"]
                out["latest_checkpoint"] = {"as_of": last["checkpoint"]["as_of"],
                                            "cadence": last["checkpoint"]["cadence"],
                                            "maturity": p.get("maturity"),
                                            "verdicts": (p.get("summary") or {}).get("verdicts")}  # fmt: skip
            out["due"] = {c: [t.isoformat() for t in gv.due(store, row["study_id"], c)]
                          for c in gv.CADENCES}  # fmt: skip
    _out(out, as_json)


@microdir.command("definition")
def definition_cmd(as_json: bool = JSON) -> None:
    """READ-ONLY: the frozen definition (hypotheses, families, rules) and its study ID."""
    from market_signal.cli.common import get_settings
    from market_signal.research.microdir import spec as sp

    defn = _defn(get_settings())
    _out({"study_id": defn.study_id, "definition_digest": sp.definition_digest(),
          "costs": [c.model_dump() for c in defn.costs], "definition": sp.frozen()}, as_json)  # fmt: skip


@microdir.command("calibrate")
def calibrate_cmd(workers: int = typer.Option(1, help="Processes (results do not depend on it)"),
                  only: str = typer.Option(None, help="Scenario name prefix"),
                  out: str = typer.Option(None, help="Write the full JSON here")) -> None:  # fmt: skip
    """READ-ONLY: the frozen harness on synthetic null and planted markets."""
    from market_signal.cli.common import get_settings
    from market_signal.research.microdir.calibration import calibrate

    res = calibrate(_defn(get_settings()), workers=workers, only=only)
    if out:
        with open(out, "w") as fh:
            json.dump(res, fh, indent=1, default=str)
    _out({k: v for k, v in res.items() if k != "scenarios"} | {"scenarios": [
        {k: s[k] for k in ("scenario", "verdict_counts", "detected", "non_target_candidates",
                           "raw_p05_share", "ladder_p")} for s in res["scenarios"]]}, False)  # fmt: skip


@microdir.command("register")
def register_cmd(reason: str = typer.Option(..., help="Why the study is being frozen")) -> None:
    """WRITE: freeze the study. Evaluates nothing."""
    from market_signal.cli.lab_cmds import _software
    from market_signal.research.microdir import governance as gv

    with open_store() as (settings, store):
        try:
            r = gv.register(store, _defn(settings), software=_software(),
                            origin="cli:lab microstructure register", reason=reason)  # fmt: skip
        except gv.MicroStudyError as exc:
            _fail(exc)
    console.print(f"Frozen [bold]{r['study_id']}[/] ({r['name']}) at {r['registered_at']}; "
                  "PROSPECTIVE_EXPLORATORY, nothing evaluated")  # fmt: skip


@microdir.command("checkpoint")
def checkpoint_cmd(
    cadence: str = typer.Option("weekly", help="daily (descriptive) | weekly (governed)"),
    due_only: bool = typer.Option(True, "--due/--no-due", help="Run every owed grid instant"),
    reproduce: str = typer.Option(None, help="Re-evaluate this earlier checkpoint"),
    reason: str = typer.Option(None, help="Reproduction reason"),
) -> None:
    """WRITE: evaluate the owed checkpoint(s) in order, or reproduce one."""
    from market_signal.cli.lab_cmds import _software
    from market_signal.research.microdir import data
    from market_signal.research.microdir import governance as gv

    with open_store() as (_settings, store):
        try:
            _, row = gv.get_study(store)
            sid = row["study_id"]
            ev = data.evaluator(store, registered_at=row["registered_at"])
            if reproduce:
                src = next(c for c in gv.checkpoints(store, sid) if c["checkpoint_id"] == reproduce)
                todo = [(src["cadence"], src["as_of"])]
            else:
                todo = [(cadence, t) for t in gv.due(store, sid, cadence)]
            if not todo:
                console.print(f"No {cadence} checkpoint is owed yet.")
            for cad, t in todo:
                r = gv.checkpoint(store, sid, cad, t, software=_software(), evaluate=ev,
                                  reproduces=reproduce, reason=reason)  # fmt: skip
                p = r["payload"]
                console.print(f"{r['checkpoint_id']} {cad} as_of {r['as_of']}: {r['status']} "
                              f"maturity={(p.get('maturity') or {}).get('study_level')} "
                              f"verdicts={(p.get('summary') or {}).get('verdicts')} "
                              f"digest={r['result_digest']}")  # fmt: skip
        except (gv.MicroStudyError, StopIteration) as exc:
            _fail(exc)


@microdir.command("study")
def study_cmd(as_json: bool = JSON) -> None:
    """READ-ONLY: registration and every checkpoint."""
    from market_signal.research.microdir import governance as gv

    with open_store(read_only=True) as (_s, store):
        try:
            _, row = gv.get_study(store)
        except gv.MicroStudyError as exc:
            _fail(exc)
        cps = gv.checkpoints(store, row["study_id"])
    _out({"study_id": row["study_id"], "name": row["name"], "registered_at": row["registered_at"],
          "reason": row["reason"], "evidence_class": row["evidence_class"],
          "checkpoints": cps}, as_json)  # fmt: skip


@microdir.command("results")
def results_cmd(checkpoint: str = typer.Argument(None), as_json: bool = JSON) -> None:
    """READ-ONLY: a stored checkpoint result (default: the latest completed one)."""
    from market_signal.research.microdir import governance as gv

    with open_store(read_only=True) as (_s, store):
        try:
            _, row = gv.get_study(store)
            r = gv.result(store, checkpoint) if checkpoint else gv.latest(store, row["study_id"])
        except gv.MicroStudyError as exc:
            _fail(exc)
    if r is None:
        _fail(gv.MicroStudyError("no completed checkpoint yet"))
    _out(r, as_json)


@microdir.command("history")
def history_cmd(as_json: bool = JSON) -> None:
    """READ-ONLY: every checkpoint and each member's verdict path (crossings and fall-backs)."""
    from market_signal.research.microdir import governance as gv

    with open_store(read_only=True) as (_s, store):
        try:
            _, row = gv.get_study(store)
            h = gv.history(store, row["study_id"])
        except gv.MicroStudyError as exc:
            _fail(exc)
    _out(h, as_json)


@microdir.command("events")
def events_cmd(member: str = typer.Option("absorption_core:short", help="hypothesis:side"),
               as_of: str = AS_OF, limit: int = typer.Option(50),
               independent: bool = typer.Option(True, "--independent/--raw"),
               as_json: bool = JSON) -> None:  # fmt: skip
    """READ-ONLY: one member's events on the data held at --as-of (explained, newest first)."""
    from market_signal.research.microdir import analysis as an
    from market_signal.research.microdir import spec as sp

    key, side = member.split(":")
    with open_store(read_only=True) as (settings, store):
        t = _as_of(store, as_of)
        P, meta = _panel(store, t)
        E = an.events(P, _defn(settings), sp.hypothesis(key), side) if len(P) else pd.DataFrame()
    if len(E) and independent:
        E = E[E["independent"]]
    rows = [an.explain_row(r, member) for _, r in E.sort_values("window_open").tail(limit).iterrows()] \
        if len(E) else []  # fmt: skip
    _out({"as_of": t, "member": member, "governed": False, "cutover": meta.get("cutover"),
          "count": len(E), "events": rows[::-1]}, as_json)  # fmt: skip


@microdir.command("explain")
def explain_cmd(event_id: str, as_of: str = AS_OF, as_json: bool = JSON) -> None:
    """READ-ONLY: why EVENT_ID qualified (flow, response, book, OI, crowding, context)."""
    from market_signal.research.microdir import analysis as an
    from market_signal.research.microdir import spec as sp

    with open_store(read_only=True) as (settings, store):
        t = _as_of(store, as_of)
        P, _ = _panel(store, t)
        defn = _defn(settings)
        for h in sp.HYPOTHESES:
            for side in sp.SIDES:
                E = an.events(P, defn, h, side) if len(P) else pd.DataFrame()
                if len(E) and (E["event_id"] == event_id).any():
                    r = E[E["event_id"] == event_id].iloc[0]
                    out = an.explain_row(r, sp.member_key(h.key, side))
                    out["independent"] = bool(r["independent"])
                    out["thesis"] = h.thesis(side)
                    _out(out, as_json)
                    return
    _fail(LookupError(f"{event_id} is not an event in the data held at {t.isoformat()}"))


@microdir.command("state")
def state_cmd(asset: str, as_json: bool = JSON) -> None:
    """READ-ONLY: the asset's latest closed 15m window, hypotheses firing now, study evidence.
    Combine with `market context snapshot ASSET` and `market microstructure inspect ASSET`."""
    from market_signal.research.microdir import analysis as an
    from market_signal.research.microdir import features as ft
    from market_signal.research.microdir import governance as gv
    from market_signal.research.microdir import spec as sp

    coin = asset.upper()
    with open_store(read_only=True) as (_s, store):
        t = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=1)).floor("min")
        P, meta = _panel(store, t, coins=(coin,), since=t - pd.Timedelta(days=sp.NORM_DAYS + 1))
        _, row = _study(store)
        last = gv.latest(store, row["study_id"]) if row is not None else None
    out: dict = {"asset": coin, "as_of": t, "cutover": meta.get("cutover"), "window": None,
                 "firing": [], "study": None, "governed": False}  # fmt: skip
    if len(P):
        done = P[P["complete"] & P["available_at"].notna()]
        if len(done):
            r = done.iloc[-1]
            out["window"] = an.explain_row(r)
            fl = ft.flags(P.loc[[r.name]])
            out["firing"] = [sp.member_key(h.key, s) for h in sp.HYPOTHESES for s in sp.SIDES
                             if ft.member_mask(P.loc[[r.name]], fl, h, s)[0]]  # fmt: skip
    if last:
        mem = last["payload"].get("members") or {}
        out["study"] = {"checkpoint_as_of": last["checkpoint"]["as_of"],
                        "maturity": (last["payload"].get("maturity") or {}).get("study_level"),
                        "firing_members": {k: {"verdict": (v.get("verdict") or {}).get("verdict")
                                               if isinstance(v.get("verdict"), dict) else v.get("verdict"),
                                               "maturity": v.get("maturity")}
                                           for k, v in mem.items() if k in out["firing"]}}  # fmt: skip
    out["note"] = "research context only: not a trade, not a signal, no execution path"
    _out(out, as_json)


@microdir.command("describe")
def describe_cmd(as_of: str = AS_OF, out: str = typer.Option(None, help="Write the full JSON"),
                 as_json: bool = JSON) -> None:  # fmt: skip
    """READ-ONLY, NON-GOVERNED: the full evaluation on the data held now (smoke: features,
    frequency, coverage). Nothing is recorded; it never substitutes for a checkpoint."""
    from market_signal.research.microdir import data

    with open_store(read_only=True) as (settings, store):
        t = _as_of(store, as_of)
        payload, _ = data.evaluator(store)(_defn(settings), t)
    payload["governed"] = False
    if out:
        with open(out, "w") as fh:
            json.dump(payload, fh, indent=1, default=str)
    _out({k: payload.get(k) for k in ("as_of", "maturity", "data", "summary",
                                      "failure_analysis")} if not as_json else payload, as_json)  # fmt: skip
