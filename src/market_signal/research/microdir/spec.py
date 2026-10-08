"""The frozen Phase 24B definition: everything decided BEFORE any prospective outcome.

``microstructure_absorption_v1`` asks whether one-sided aggressive (taker) flow, read together
with the price response it bought, the opposing book, open interest and positioning, predicts
the next hour's direction beyond what an OHLCV candle already says. Every constant below
(features, buckets, horizons, costs, comparisons, tests, maturity and verdict rules) is part of
the definition and therefore of the content-addressed ``study_id``. Changing any of them is a
new study (a new name and ID), never a retune of this one.

Research only: nothing here places an order, touches paper trading, the co-pilot, risk
policy or the Phase 21 incubation freeze, and no consumer reads the result.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import model_validator

from market_signal.microstructure import definitions as md
from market_signal.research.lab.common import LabModel, canonical_json, content_id
from market_signal.research.structure.study.spec import CostRef

STUDY_NAME = "microstructure_absorption_v1"
STUDY_VERSION = "microstructure_direction_v1"
FEATURE_VERSION = "microdir_features_v1"
INFERENCE_VERSION = "microdir_inference_v1"
VERDICT_VERSION = "microdir_verdicts_v1"
MATURITY_VERSION = "microdir_maturity_v1"
COST_VERSION = "microdir_costs_v1"
EVIDENCE_CLASS = "PROSPECTIVE_EXPLORATORY"
AVAILABILITY_MODE = "observed"
STATEMENT = (
    "Phase 24B tests whether microstructure provides incremental directional information "
    "beyond OHLCV. A microstructure signal is not useful merely because it describes market "
    "activity. Aggressive flow does not automatically imply continuation. Failure of price to "
    "respond to aggressive flow is itself a testable market state."
)
VERDICTS = ("IMMATURE", "REJECTED", "NO_EVIDENCE", "INTERESTING", "INCUBATION_CANDIDATE",
            "STRONG_INCUBATION_CANDIDATE")  # fmt: skip
CANDIDATE_VERDICTS = ("INCUBATION_CANDIDATE", "STRONG_INCUBATION_CANDIDATE")
MATURITY_LEVELS = ("WARMUP", "EARLY", "DEVELOPING", "ADEQUATE")

VENUE = "hyperliquid"
COINS = md.COINS
SOURCE_FEATURE_VERSION = md.FEATURE_VERSION  # microstructure_1m_v1, read via the causal loader
AVAILABILITY = "finalized"  # collector finalization time (close + 5 s grace), receipt clock

# --------------------------------------------------------------------------- time
SIGNAL_MINUTES = 15  # primary signal window: UTC quarter hours [:00, :15, :30, :45)
SUB_MINUTES = 5  # three 5-minute sub-windows (persistence)
HORIZONS_MIN = (15, 30, 60, 120, 240)
PRIMARY_HORIZON = 60
ENTRY_RULE = ("first minute whose close is at or after the signal's availability (the latest "
              "finalization of its 15 contributing minutes); reference price = that minute's "
              "end-of-minute mid; exit = end-of-minute mid of the minute closing `horizon` later")  # fmt: skip
SESSIONS = (("asia", 0, 8), ("europe", 8, 13), ("us", 13, 21), ("other", 21, 24))  # UTC hours

# --------------------------------------------------------------------------- normalization
NORM_DAYS = 7  # trailing window (prior COMPLETE 15m windows only) for every percentile / scale
NORM_MIN_WINDOWS = 288  # 3 days of prior windows before a normalized feature exists
RV_WINDOWS = 16  # realised volatility state: last 4 h of 15m mid returns

# --------------------------------------------------------------------------- buckets
FLOW_STRONG = 0.90  # signed-notional-delta percentile >= this: strong buy; <= 1 - this: sell
FLOW_EXTREME = 0.975  # descriptive sub-bucket
RESP_EFFICIENT = 0.75  # resp_z >= this: efficient (accepted) response
RESP_WEAK = 0.25  # resp_z < this: weak or opposite (unaccepted) response -> absorption core
RESP_OPPOSITE = -0.25  # descriptive split of the weak bucket
REPLENISH_PCT = 0.50  # opposing replenishment / traded notional, trailing percentile
DEPTH_PERSIST = 1.0  # opposing top-5 depth end / start >= this: persisted or grew
OI_Z = 0.5  # 15m OI change z (trailing scale): rising >= +0.5, falling <= -0.5
FUNDING_CROWD = 0.80  # funding percentile >= this: long-crowding-like (<= 0.20: short)
VOLUME_HIGH = 0.90  # candle volume percentile (the OHLCV twin's "large volume")
BURST_SHARE = 0.50  # one minute holds >= 50% of the window's |delta|: a burst
PERSIST_SUBWINDOWS = 2  # of 3 five-minute sub-windows with the window's flow sign ...
PERSIST_MINUTES = 9  # ... and at least 9 of 15 minutes with it: persistent pressure
CLV_LOW, CLV_HIGH = 0.35, 0.65  # candle close location (OHLCV twin of absorption direction)
CANDLE_SMALL = 0.25  # |candle return z| < this: small body
CANDLE_BIG = 0.75  # |candle return z| >= this: big body
Z_CLIP = 4.0

# --------------------------------------------------------------------------- inference
BLOCK_HOURS = 4  # UTC 4 h blocks shared by all coins, adjacent-block covariance
DEDUP_MINUTES = PRIMARY_HORIZON  # per (coin, hypothesis, side): refractory after a kept event
FLOOR_BPS = 6.0  # economic floor at 1 h, net per event (Phase 22's 1 h floor)
BH_Q = 0.10
RECENT_DAYS = 30
MATURITY = {  # level: (min coverage days, min independent episodes, min assets, eps/asset)
    "EARLY": (7, 30, 1, 0),
    "DEVELOPING": (30, 100, 3, 10),
    "ADEQUATE": (90, 300, 4, 20),
}
COVERAGE_DAY_MIN = 0.50  # a coverage day: >= 50% of the day's 15m windows COMPLETE (pooled)
RULES = {
    "order": "IMMATURE (WARMUP) > candidate route with all guards > route failing a guard "
             "(INTERESTING) > REJECTED > INTERESTING > NO_EVIDENCE (first match)",
    "rejected": "net t <= -1, or gross t >= 2 while net mean <= 0 (edge only before costs)",
    "route_full": "maturity >= DEVELOPING, net mean >= floor and net t >= 2.0",
    "route_recent": "maturity >= DEVELOPING, last 30 days: >= 30 episodes, mean >= floor, t >= 2.5 "
                    "(a recent edge is not vetoed by an earlier sample)",
    "guards": ">= 3 assets; leave-best-asset-out mean > 0; top-5 episodes < 50% of net; >= half "
              "of assets positive; gross > 0; excess vs matched random t >= 1; candle-residual "
              "t >= 1; >= 0.25 independent episodes/day (on the route's sample)",
    "strong": "full-sample route candidate + maturity ADEQUATE + BH q <= 0.10 in its family + net t >= 2.5 + "
              "candle-residual t >= 2 + last-30-day mean > 0",
    "interesting": "net mean > 0 and net t >= 1 (or a route that failed a guard)",
    "immature": "maturity WARMUP: no verdict; EARLY caps the verdict at INTERESTING",
}  # fmt: skip

# Passive-execution SENSITIVITY (never the verdict): Prism has no measured maker fill model.
# Hyperliquid's published base-tier maker fee (an assumption exactly like the taker fee in
# config/perps.yaml): resting entry at the touch, no entry slippage, taker exit at horizon.
# No fill probability or adverse selection is modelled, so it is an optimistic bound.
MAKER_FEE_BPS = 1.5

# --------------------------------------------------------------------------- hypotheses
FAMILIES = ("flow_continuation", "absorption", "absorption_oi", "absorption_crowding",
            "persistence", "large_print", "context")  # fmt: skip
VERDICT_FAMILIES = FAMILIES[:5]  # large_print and context are descriptive (immature inputs)


@dataclass(frozen=True)
class Hypothesis:
    key: str
    family: str
    action: Literal["follow", "fade"]  # trade with or against the aggressive flow
    requires: tuple[str, ...]  # conditions (feature flags, read relative to the flow side)
    candle_twin: str  # OHLCV-only analogue evaluated on the same data
    statement: str

    def thesis(self, side: str) -> dict:
        """Structural candidate composition (no opaque score)."""
        flow = ("buy" if side == "long" else "sell") if self.action == "follow" else (
            "sell" if side == "long" else "buy")  # fmt: skip
        return {"hypothesis": self.key, "family": self.family, "flow": f"one_sided_{flow}",
                "conditions": list(self.requires), "action": self.action, "direction": side,
                "signal": f"{SIGNAL_MINUTES}m", "horizon": f"{PRIMARY_HORIZON // 60}h"}  # fmt: skip


H = Hypothesis
HYPOTHESES: tuple[Hypothesis, ...] = (
    H("flow_follow", "flow_continuation", "follow", (), "candle_big_move",
      "strong one-sided taker flow continues in its direction"),
    H("continuation_core", "flow_continuation", "follow", ("resp_efficient",), "candle_momentum",
      "one-sided flow that moved price efficiently is accepted and continues"),
    H("continuation_book", "flow_continuation", "follow", ("resp_efficient", "opp_consumed"),
      "candle_momentum", "accepted flow that consumed the opposing book continues"),
    H("absorption_core", "absorption", "fade", ("resp_weak",), "candle_absorption",
      "one-sided flow that failed to move price was absorbed; price reverses"),
    H("absorption_book", "absorption", "fade", ("resp_weak", "opp_resilient"),
      "candle_absorption", "absorbed flow met a replenishing, persistent opposing book"),
    H("absorption_oi_rising", "absorption_oi", "fade", ("resp_weak", "oi_rising"),
      "candle_absorption", "absorbed aggression that added open interest leaves trapped positions"),
    H("absorption_oi_not_rising", "absorption_oi", "fade", ("resp_weak", "oi_not_rising"),
      "candle_absorption", "absorption without new leverage (the OI contrast's comparison arm)"),
    H("absorption_crowded", "absorption_crowding", "fade", ("resp_weak", "crowd_aligned"),
      "candle_absorption", "absorption on the already-crowded side (funding/positioning skew)"),
    H("continuation_persistent", "persistence", "follow", ("resp_efficient", "persistent"),
      "candle_momentum", "accepted flow sustained across the window continues"),
    H("absorption_persistent", "persistence", "fade", ("resp_weak", "persistent"),
      "candle_absorption", "persistent flow that is still absorbed reverses"),
    H("continuation_lp_aligned", "large_print", "follow", ("resp_efficient", "lp_aligned"),
      "candle_momentum", "accepted flow reinforced by same-side large prints (descriptive)"),
    H("absorption_lp_aligned", "large_print", "fade", ("resp_weak", "lp_aligned"),
      "candle_absorption", "absorbed flow that included same-side large prints (descriptive)"),
)  # fmt: skip
SIDES = ("long", "short")

# Candle twins: the same idea from OHLCV only (trade-price candle + volume, no aggressor side,
# no book, no OI). Direction comes from the candle itself.
CANDLE_TWINS = {
    "candle_big_move": "signed candle return percentile >= 0.90 (<= 0.10): follow",
    "candle_momentum": "volume pct >= 0.90 and |return z| >= 0.75: follow the candle",
    "candle_absorption": "volume pct >= 0.90, |return z| < 0.25, close location <= 0.35 -> short, "
                         ">= 0.65 -> long (rejected push)",
}  # fmt: skip

# Contrasts (difference of means, shared blocks): (name, family, base hypothesis, flag, other)
CONTRASTS = (
    ("oi_rising_vs_not", "absorption_oi", "absorption_core", "oi_rising", "oi_not_rising"),
    ("crowded_vs_not", "absorption_crowding", "absorption_core", "crowd_aligned", "crowd_not"),
    ("resilient_vs_not", "absorption", "absorption_core", "opp_resilient", "opp_not_resilient"),
    ("consumed_vs_not", "flow_continuation", "continuation_core", "opp_consumed", "opp_not_consumed"),
    ("cont_oi_rising_vs_falling", "flow_continuation", "continuation_core", "oi_rising", "oi_falling"),
    ("abs_persistent_vs_burst", "persistence", "absorption_core", "persistent", "burst"),
    ("cont_persistent_vs_burst", "persistence", "continuation_core", "persistent", "burst"),
    ("abs_lp_aligned_vs_not", "large_print", "absorption_core", "lp_aligned", "lp_not_aligned"),
    ("cont_lp_aligned_vs_not", "large_print", "continuation_core", "lp_aligned", "lp_not_aligned"),
)  # fmt: skip

# Candle-only baseline ladder (block-clustered OLS on every eligible window; y = next-1h long
# return in units of the coin's trailing 15m sigma). Each layer adds a few interpretable terms.
LADDER = (
    ("candle", ("c_ret", "c_clv", "c_mom", "c_absorb")),
    ("flow", ("f_flow", "f_cont", "f_abs")),
    ("book", ("b_imb", "b_res")),
    ("oi", ("o_flow", "o_abs")),
    ("positioning", ("p_fund", "p_abs_crowd")),
)

CONTEXT_CATEGORIES = ("pre_tier1_60m", "post_tier1_0_15m", "post_tier1_15_60m",
                      "crypto_event_active", "none")  # fmt: skip


def hypotheses() -> tuple[Hypothesis, ...]:
    return HYPOTHESES


def hypothesis(key: str) -> Hypothesis:
    return next(h for h in HYPOTHESES if h.key == key)


def member_key(h: str, side: str) -> str:
    return f"{h}:{side}"


def frozen() -> dict:
    """Everything that defines the study, as data (hashed into the study ID)."""
    g = globals()
    consts = {k: g[k] for k in (
        "FEATURE_VERSION", "INFERENCE_VERSION", "VERDICT_VERSION", "MATURITY_VERSION",
        "COST_VERSION", "SOURCE_FEATURE_VERSION", "AVAILABILITY", "VENUE", "COINS",
        "SIGNAL_MINUTES", "SUB_MINUTES", "HORIZONS_MIN", "PRIMARY_HORIZON", "ENTRY_RULE",
        "SESSIONS", "NORM_DAYS", "NORM_MIN_WINDOWS", "RV_WINDOWS", "FLOW_STRONG", "FLOW_EXTREME",
        "RESP_EFFICIENT", "RESP_WEAK", "RESP_OPPOSITE", "REPLENISH_PCT", "DEPTH_PERSIST", "OI_Z",
        "FUNDING_CROWD", "VOLUME_HIGH", "BURST_SHARE", "PERSIST_SUBWINDOWS", "PERSIST_MINUTES",
        "CLV_LOW", "CLV_HIGH", "CANDLE_SMALL", "CANDLE_BIG", "Z_CLIP", "BLOCK_HOURS",
        "DEDUP_MINUTES", "FLOOR_BPS", "BH_Q", "RECENT_DAYS", "MATURITY", "COVERAGE_DAY_MIN",
        "RULES", "MAKER_FEE_BPS", "FAMILIES", "VERDICT_FAMILIES", "SIDES", "CANDLE_TWINS",
        "CONTRASTS", "LADDER", "CONTEXT_CATEGORIES", "STATEMENT")}  # fmt: skip
    consts["HYPOTHESES"] = [asdict(h) for h in HYPOTHESES]
    consts["SOURCE_DEFINITION_DIGEST"] = md.definition_digest()
    return json.loads(json.dumps(consts, default=list))


def definition_digest() -> str:
    return content_id("microdirdef_", frozen())


class MicroStudyDefinition(LabModel):
    """The statistical identity of the Phase 24B study. ``study_id`` hashes all of it."""

    schema_version: Literal["1"] = "1"
    name: Literal["microstructure_absorption_v1"] = STUDY_NAME
    study_version: Literal["microstructure_direction_v1"] = STUDY_VERSION
    evidence_class: Literal["PROSPECTIVE_EXPLORATORY"] = EVIDENCE_CLASS
    availability_mode: Literal["observed"] = AVAILABILITY_MODE
    production_only: Literal[True] = True  # confirmatory rows start at the 24A cutover
    validated_reachable: Literal[False] = False
    live_reachable: Literal[False] = False
    consumers: Literal["none"] = "none"
    definition: dict
    costs: tuple[CostRef, ...]

    @model_validator(mode="after")
    def complete(self) -> MicroStudyDefinition:
        if self.definition != frozen():
            raise ValueError("definition differs from the frozen microstructure_absorption_v1")
        keys = [(c.venue, c.coin) for c in self.costs]
        if sorted(keys) != sorted((VENUE, c) for c in COINS):
            raise ValueError("exactly one cost entry per study coin")
        return self

    def canonical_json(self) -> str:
        data = self.model_dump(mode="python")
        data["costs"] = sorted(data["costs"], key=canonical_json)
        return canonical_json(data)

    @property
    def study_id(self) -> str:
        return content_id("sstudy_", json.loads(self.canonical_json()))

    def cost(self, coin: str) -> CostRef:
        return next(c for c in self.costs if c.coin == coin)


def build_definition(perps_cfg: dict) -> MicroStudyDefinition:
    """Freeze Prism's realistic taker cost model (fee + per-coin slippage) per coin."""
    from market_signal.perps.backtest import perp_costs

    costs = []
    for coin in COINS:
        c = perp_costs(perps_cfg, coin, VENUE)
        costs.append(CostRef(venue=VENUE, coin=coin, fee_bps=c.fee_bps,
                             slippage_bps=c.slippage_bps))  # fmt: skip
    return MicroStudyDefinition(definition=frozen(), costs=tuple(costs))
