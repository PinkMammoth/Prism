"""Pre-trade risk engine: every candidate intent of one bar against ONE account snapshot.

``allocate`` is pure. All intents that became known at the same bar close are decided
together, against the same pre-trade snapshot (the account marked at that close), so no
intent sees capital or slots consumed by another in iteration order.

Order of rules (``RiskPolicy.allocation_rule = same_snapshot_hash_lottery_proportional_v1``):

1. Account gates: run ACTIVE; no daily-loss halt on this bar (the drawdown kill makes the
   run KILLED, so it is caught by the first gate).
2. Per-intent gates: signal still inside the execution window; promotion PAPER_ELIGIBLE;
   signal data contiguous; no open position / unfilled order on the asset (same side:
   duplicate, never added to; other side: conflict, never hedged).
3. Same-bar asset conflicts: a long and a short intent on one asset reject each other; several
   same-side intents on one asset keep one (the lottery winner).
4. Capacity, in lottery order: free position slots, then per-strategy and per-asset
   notional headroom. The lottery key is SHA-256(run, bar, strategy, symbol): deterministic,
   reproducible, unrelated to names' alphabetical order, input order or past profitability.
5. Notional: every selected intent requests the same fraction of snapshot equity. If the
   gross-notional headroom or the free cash (after the reserve) cannot fund them all, all are
   scaled by the SAME factor. If that leaves a position below the minimum size, the last
   intent in lottery order is dropped and the scaling is recomputed.
"""

from __future__ import annotations

import hashlib

from market_signal.paper.policy import ExecutionModel, RiskPolicy


def lottery_key(run_id: str, bar_close: str, strategy_id: str, symbol: str) -> str:
    return hashlib.sha256(f"{run_id}|{bar_close}|{strategy_id}|{symbol}".encode()).hexdigest()


def allocate(
    policy: RiskPolicy,
    execution: ExecutionModel,
    snapshot: dict,
    candidates: list[dict],
    *,
    run_id: str,
    bar_close: str,
) -> dict[str, dict]:
    """Decide every candidate. Returns ``{intent_id: decision}``.

    ``snapshot``: ``status``, ``halted`` (daily loss on this bar), ``equity``, ``cash``,
    ``positions`` ({symbol: {side, strategy_id, notional}}) and ``orders`` (unfilled entry
    orders, same shape plus ``reserved_cash``).
    ``candidates``: ``intent_id``, ``strategy_id``, ``symbol``, ``side`` (+1/-1), ``live``,
    ``promotion`` (decision dict) and ``data_ok``.

    Each decision: ``decision`` (ACCEPTED/REJECTED), ``reasons``, ``notional``, ``scale``,
    ``lottery_key`` and ``rank``.
    """
    eq = float(snapshot["equity"])
    out: dict[str, dict] = {}
    keyed = sorted(
        candidates,
        key=lambda c: lottery_key(run_id, bar_close, c["strategy_id"], c["symbol"]),
    )
    for rank, c in enumerate(keyed):
        out[c["intent_id"]] = {"decision": "REJECTED", "reasons": [], "notional": 0.0, "scale": None,
                               "rank": rank,
                               "lottery_key": lottery_key(run_id, bar_close, c["strategy_id"], c["symbol"])}  # fmt: skip

    def reject(c: dict, reason: str) -> None:
        out[c["intent_id"]]["reasons"].append(reason)

    # 1-2: account and per-intent gates (all reasons are recorded, not just the first)
    held = {**{s: p for s, p in snapshot["orders"].items()}, **snapshot["positions"]}
    alive = []
    for c in keyed:
        if snapshot["status"] != "ACTIVE":
            reject(c, f"account_{str(snapshot['status']).lower()}")
        if snapshot.get("halted"):
            reject(c, "daily_loss_limit")
        if not c["live"]:
            reject(c, "missed_execution_window")
        if c["promotion"]["decision"] != "PAPER_ELIGIBLE":
            reject(c, "promotion_ineligible:" + ",".join(c["promotion"]["blocked_by"]))
        if not c["data_ok"]:
            reject(c, "stale_or_gapped_data")
        h = held.get(c["symbol"])
        if h is not None:
            reject(
                c, "duplicate_position_no_add" if h["side"] == c["side"] else "conflicting_position"
            )
        if not out[c["intent_id"]]["reasons"]:
            alive.append(c)

    # 3: same-bar conflicts per asset
    by_asset: dict[str, list[dict]] = {}
    for c in alive:
        by_asset.setdefault(c["symbol"], []).append(c)
    survivors = []
    for group in by_asset.values():
        if len({c["side"] for c in group}) > 1:
            for c in group:
                reject(c, "conflicting_signals_same_bar")
            continue
        survivors.append(group[0])  # groups keep lottery order: the first is the winner
        for c in group[1:]:
            reject(c, "duplicate_signal_same_bar")
    survivors.sort(key=lambda c: out[c["intent_id"]]["rank"])

    # 4: capacity in lottery order
    request = policy.position_notional_fraction * eq
    slots = policy.max_open_positions - len(snapshot["positions"]) - len(snapshot["orders"])
    strategy_used: dict[str, float] = {}
    asset_used: dict[str, float] = {}
    for h in held.values():
        strategy_used[h["strategy_id"]] = strategy_used.get(h["strategy_id"], 0.0) + h["notional"]
    for s, h in held.items():
        asset_used[s] = asset_used.get(s, 0.0) + h["notional"]
    selected = []
    for c in survivors:
        if slots <= 0:
            reject(c, "max_open_positions")
            continue
        if (
            strategy_used.get(c["strategy_id"], 0.0) + request
            > policy.max_strategy_notional_fraction * eq + 1e-9
        ):
            reject(c, "max_strategy_allocation")
            continue
        if (
            asset_used.get(c["symbol"], 0.0) + request
            > policy.max_asset_notional_fraction * eq + 1e-9
        ):
            reject(c, "max_asset_exposure")
            continue
        selected.append(c)
        slots -= 1
        strategy_used[c["strategy_id"]] = strategy_used.get(c["strategy_id"], 0.0) + request
        asset_used[c["symbol"]] = asset_used.get(c["symbol"], 0.0) + request

    # 5: one proportional scale factor for everyone selected
    gross_now = sum(h["notional"] for h in held.values())
    gross_room = max(policy.max_gross_notional_fraction * eq - gross_now, 0.0)
    reserved = sum(o.get("reserved_cash", 0.0) for o in snapshot["orders"].values())
    cash_room = max(snapshot["cash"] - reserved - policy.min_free_cash_fraction * eq, 0.0)
    minimum = policy.min_position_notional_fraction * eq
    while selected:
        per_notional = [
            1 / policy.leverage + execution.cost(c["symbol"]).fee_bps / 1e4 for c in selected
        ]
        want = request * len(selected)
        need_cash = sum(request * k for k in per_notional)
        scale = min(
            1.0, gross_room / want if want else 1.0, cash_room / need_cash if need_cash else 1.0
        )
        if request * scale >= minimum - 1e-9:
            for c in selected:
                d = out[c["intent_id"]]
                d.update(decision="ACCEPTED", notional=request * scale, scale=scale)
            break
        reject(selected.pop(), "insufficient_headroom")
    return out
