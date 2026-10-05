"""The intraday strategy catalogue (``intraday_catalogue_v1``): frozen, versioned, simple.

The daily Phase 6 catalogue (``config/lab/families``, ``vocabulary_v1``) is NOT touched: this
is a second, explicitly versioned catalogue whose identities live in their own namespace
(``istrat_…``). Every strategy encodes its timeframe, side, family, rule, parameters, entry
timing, horizons (primary + at most two secondaries), feature version, universe and its
simpler baseline; ``strategy_id`` is the content hash of all of it.

Design rules (frozen before any real outcome was computed):

- **Simple.** Each rule is one or two sentences and at most four conditions. Grids are one or
  two small axes (two or three values). No giant rule stacks: filters live in separate,
  named variants with a declared simpler ``baseline`` so their value can be measured.
- **Timeframes.** 1H is primary. 15m is used for fast triggers (plain, or with 1H context).
  4H is context only (``context_timeframe = 4h``). No full timeframe matrix.
- **Horizons.** 1H strategies choose from {1, 2, 4, 8, 12, 24} hours; 15m strategies from
  {15m, 30m, 1h, 2h, 4h}. Each rule preregisters ONE primary horizon and two secondaries.
  The best exit is never selected afterwards.
- **Symmetry.** Long and short are the same code (``signals.Oriented``): a short rule is the
  long rule applied to the price-mirrored market. Three short-specific rules with their own
  economic logic are marked ``symmetric = False`` and documented; none is invented to raise
  the count.
- **Correction families** (``bh_family``) are fixed here, coherent by economic mechanism,
  never tiny and never one mega-family.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Literal

from market_signal.research.discovery.primitives import FEATURES_VERSION
from market_signal.research.lab.common import LabModel, Name, PositiveInt, Text, content_id

CATALOGUE_VERSION = "intraday_catalogue_v1"
ENTRY_RULE = "next_15m_open_after_availability_v1"
EXIT_RULE = "fixed_horizon_close_v1"
HORIZONS_1H = (1, 2, 4, 8, 12, 24)  # hours
HORIZONS_15M = (1, 2, 4, 8, 16)  # 15m bars: 15m, 30m, 1h, 2h, 4h
PCT_WINDOW = {"1h": 720, "15m": 2880}  # 30 days of bars for trailing percentiles
TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240}

BH_FAMILIES = ("momentum", "pullback", "breakout", "compression", "mean_reversion", "volume",
               "expansion_exhaustion", "funding", "btc_context")  # fmt: skip
VOL_FAMILY = "volatility_forecast"
CONTRAST_FAMILY = "context_contrasts"

ParamValue = float | int | str


@dataclass(frozen=True)
class RuleMeta:
    """A signal rule: plain-language hypothesis and the complexity bookkeeping."""

    hypothesis: str  # for the long side; the short side is its mirror
    conditions: int  # independent conditions that must hold at the signal bar
    feature_families: tuple[str, ...]
    short_hypothesis: str | None = None  # asymmetric (short-only) rules


RULES: dict[str, RuleMeta] = {
    "momentum_z": RuleMeta("A strong n-bar move (z >= k in pre-move volatility units) continues.",
                           1, ("momentum",)),
    "ema_cross": RuleMeta("Price crossing above its n-bar EMA starts a short-term up-move.",
                          1, ("trend",)),
    "ema_cross_trend": RuleMeta("A cross above the n-bar EMA while that EMA is rising continues.",
                                2, ("trend",)),
    "efficiency_trend": RuleMeta("An efficient (low-noise) 12-bar up-move continues.",
                                 1, ("efficiency",)),
    "momentum_htf": RuleMeta("1H momentum aligned with the 4H trend continues.",
                             2, ("momentum", "trend")),
    "momentum_ltf_ctx": RuleMeta("15m momentum aligned with a rising 1H trend continues.",
                                 3, ("momentum", "trend")),
    "pullback": RuleMeta("In an uptrend, a dip to the fast EMA (or the recross after it) resumes "
                         "the trend.", 3, ("trend",)),
    "breakout": RuleMeta("A close above the prior n-bar high continues upward.",
                         1, ("range",)),
    "range_reversion": RuleMeta("A close near the bottom of a recent range (not breaking it) "
                                "reverts upward.", 2, ("range",)),
    "ema_stretch_fade": RuleMeta("Price stretched k ATR below its 20-bar EMA snaps back.",
                                 1, ("trend", "volatility")),
    "compression_breakout": RuleMeta("A breakout from a compressed range continues.",
                                     2, ("range", "compression")),
    "compression_volume_breakout": RuleMeta("A breakout from a compressed range while volume "
                                            "was rising continues.", 3,
                                            ("range", "compression", "volume")),
    "compression_clue": RuleMeta("When a range compresses, the side the price leans to is the "
                                 "side of the next move.", 2, ("compression", "direction_clue")),
    "volume_continuation": RuleMeta("A high relative-volume up-bar continues.",
                                    2, ("volume", "candle")),
    "volume_fade": RuleMeta("A high relative-volume down-bar reverses upward (capitulation).",
                            2, ("volume", "candle")),
    "expansion_continuation": RuleMeta("An abnormally large up-bar closing near its high "
                                       "continues.", 3, ("candle", "volatility")),
    "expansion_fade": RuleMeta("An abnormally large down-bar closing near its low reverts "
                               "upward.", 3, ("candle", "volatility")),
    "exhaustion": RuleMeta("After an extreme n-bar decline (optionally with a strong close) "
                           "price rebounds.", 2, ("momentum", "candle")),
    "funding_trend": RuleMeta("Upward momentum while funding is extremely positive (crowded, "
                              "still trending) continues.", 2, ("funding", "momentum")),
    "funding_reversal": RuleMeta("Recovery (cross above EMA20) while funding is extremely "
                                 "negative (crowded shorts) continues.", 2, ("funding", "trend")),
    "btc_aligned_momentum": RuleMeta("Alt momentum with BTC moving the same way (or against it) "
                                     "continues.", 2, ("momentum", "market")),
    "btc_flat_breakout": RuleMeta("An alt breakout while BTC is flat is idiosyncratic and "
                                  "continues.", 2, ("range", "market")),
    "btc_strength_breakout": RuleMeta("An alt breakout while BTC is strong continues "
                                      "(mirror: breakdown while BTC is weak).", 3,
                                      ("range", "market")),
    # short-specific (symmetric = False): their own downside logic, not mirrors
    "failed_bounce_breakdown": RuleMeta(
        "", 4, ("trend", "range"),
        short_hypothesis="In a downtrend, a bounce rejected at the 20-bar EMA followed by a "
        "close below the prior 12-bar low continues down (trapped bounce buyers)."),
    "volume_downside_expansion": RuleMeta(
        "", 3, ("volume", "candle"),
        short_hypothesis="A large high-volume down-bar closing near its low continues down "
        "(forced selling / liquidations cascade)."),
    "blowoff_exhaustion": RuleMeta(
        "", 3, ("momentum", "volume", "candle"),
        short_hypothesis="After a parabolic 24-bar rise, a high-volume bar closing weak marks a "
        "blow-off top; price falls."),
}  # fmt: skip

SHORT_ONLY = ("failed_bounce_breakdown", "volume_downside_expansion", "blowoff_exhaustion")

# Non-directional volatility hypotheses (family ``volatility_forecast``): does the state
# predict a larger absolute move? Tested before any direction is asked of it.
VOL_RULES: dict[str, RuleMeta] = {
    "compression_state": RuleMeta("A compressed range is followed by a larger absolute move.",
                                  1, ("compression",)),
    "compression_volume_state": RuleMeta("A compressed range with rising volume is followed by "
                                         "a larger absolute move.", 2, ("compression", "volume")),
    "volume_spike_state": RuleMeta("A relative-volume spike is followed by a larger absolute move.",
                                   1, ("volume",)),
    "expansion_state": RuleMeta("An abnormally large bar is followed by a larger absolute move.",
                                1, ("volatility",)),
}  # fmt: skip


class Complexity(LabModel):
    """Descriptive only: never a rejection rule. ``score`` = conditions + thresholds +
    extra timeframes + feature families."""

    conditions: PositiveInt
    thresholds: int
    timeframes: PositiveInt
    feature_families: PositiveInt
    score: PositiveInt


class IntradayStrategy(LabModel):
    catalogue: Literal["intraday_catalogue_v1"] = CATALOGUE_VERSION
    family: Name
    bh_family: Name
    rule: Name
    side: Literal["long", "short"]
    timeframe: Literal["1h", "15m"]
    context_timeframe: Literal["none", "4h", "1h"] = "none"
    params: tuple[tuple[str, ParamValue], ...]
    horizons: tuple[PositiveInt, ...]  # bars of ``timeframe``
    primary_horizon: PositiveInt
    entry: Literal["next_15m_open_after_availability_v1"] = ENTRY_RULE
    exit: Literal["fixed_horizon_close_v1"] = EXIT_RULE
    feature_version: Literal["intraday_features_v1"] = FEATURES_VERSION
    universe: Literal["all", "alts"] = "all"
    symmetric: bool = True
    hypothesis: Text
    baseline: str | None = None  # key of the simpler baseline variant
    complexity: Complexity

    @property
    def p(self) -> dict:
        return dict(self.params)

    @property
    def key(self) -> str:
        """Human-readable unique name (the ID is the hash)."""
        ps = ",".join(f"{k}={v}" for k, v in self.params)
        ctx = f"@{self.context_timeframe}" if self.context_timeframe != "none" else ""
        return f"{self.rule}[{ps}]{ctx}:{self.timeframe}:{self.side}"

    @property
    def strategy_id(self) -> str:
        return content_id("istrat_", self.model_dump(mode="json"))

    @property
    def primary_minutes(self) -> int:
        return self.primary_horizon * TF_MINUTES[self.timeframe]

    def horizon_minutes(self, h: int) -> int:
        return h * TF_MINUTES[self.timeframe]


class VolTest(LabModel):
    """A non-directional volatility-forecast hypothesis (no side, never a trade)."""

    catalogue: Literal["intraday_catalogue_v1"] = CATALOGUE_VERSION
    family: Literal["volatility_forecast"] = VOL_FAMILY
    rule: Name
    timeframe: Literal["1h", "15m"]
    params: tuple[tuple[str, ParamValue], ...]
    horizons: tuple[PositiveInt, ...]
    primary_horizon: PositiveInt
    feature_version: Literal["intraday_features_v1"] = FEATURES_VERSION
    hypothesis: Text

    @property
    def p(self) -> dict:
        return dict(self.params)

    @property
    def key(self) -> str:
        ps = ",".join(f"{k}={v}" for k, v in self.params)
        return f"{self.rule}[{ps}]:{self.timeframe}"

    @property
    def test_id(self) -> str:
        return content_id("ivol_", self.model_dump(mode="json"))


# --------------------------------------------------------------------------- the grid

H1 = {1: (1, 2, 4), 2: (1, 2, 4), 4: (1, 4, 12), 8: (4, 8, 24)}  # primary -> horizons
M15 = (2, 4, 16)  # 30m, 1h (primary), 4h


@dataclass(frozen=True)
class Spec:
    family: str  # economic family label (reporting)
    bh_family: str
    rule: str
    timeframe: str
    grid: tuple[tuple[str, tuple], ...]  # axis -> values (ordered: neighbours are adjacent)
    primary: int
    context: str = "none"
    universe: str = "all"
    sides: tuple[str, ...] = ("long", "short")
    baseline: tuple[str, dict] | None = None  # (rule, params) at the same tf/context-free


SPECS: tuple[Spec, ...] = (
    # A — short-term momentum / trend
    Spec("a_momentum", "momentum", "momentum_z", "1h", (("n", (4, 12)), ("k", (1.5, 2.0))), 4),
    Spec("a_momentum", "momentum", "ema_cross", "1h", (("n", (20, 50)),), 8),
    Spec("a_momentum", "momentum", "ema_cross_trend", "1h", (("n", (20, 50)),), 8,
         baseline=("ema_cross", {"n": "n"})),
    Spec("a_momentum", "momentum", "efficiency_trend", "1h", (("n", (12,)), ("er", (0.4, 0.6))), 4),
    Spec("a_momentum", "momentum", "momentum_htf", "1h", (("n", (4,)), ("k", (1.5,))), 4,
         context="4h", baseline=("momentum_z", {"n": 4, "k": 1.5})),
    Spec("a_momentum", "momentum", "momentum_z", "15m", (("n", (4,)), ("k", (1.5, 2.0))), 4),
    Spec("a_momentum", "momentum", "momentum_ltf_ctx", "15m", (("n", (4,)), ("k", (1.5,))), 4,
         context="1h", baseline=("momentum_z", {"n": 4, "k": 1.5})),
    # B — trend pullback (immediate vs confirmed; 1H trend vs 4H trend)
    Spec("b_pullback", "pullback", "pullback", "1h",
         (("entry", ("immediate", "confirm")), ("trend", ("1h", "4h"))), 8),
    # C — range breakout
    Spec("c_breakout", "breakout", "breakout", "1h", (("n", (12, 24, 48)),), 4),
    Spec("c_breakout", "breakout", "breakout", "15m", (("n", (16, 48)),), 4),
    # D — range mean reversion (+ EMA stretch)
    Spec("d_range_reversion", "mean_reversion", "range_reversion", "1h",
         (("n", (24, 48)), ("max_width_atr", (8.0, 1000.0))), 4),
    Spec("d_range_reversion", "mean_reversion", "range_reversion", "15m",
         (("n", (32,)), ("max_width_atr", (1000.0,))), 4),
    Spec("i_exhaustion", "mean_reversion", "ema_stretch_fade", "1h", (("k", (3.0, 4.0)),), 4),
    # E/G/16 — compression -> expansion: direction
    Spec("e_compression", "compression", "compression_breakout", "1h",
         (("n", (12, 24)), ("pct", (0.2, 0.35))), 4, baseline=("breakout", {"n": "n"})),
    Spec("e_compression", "compression", "compression_breakout", "15m",
         (("n", (16,)), ("pct", (0.25,))), 4, baseline=("breakout", {"n": 16})),
    Spec("g_volume_compression", "compression", "compression_volume_breakout", "1h",
         (("n", (24,)), ("pct", (0.35,)), ("vslope_pct", (0.75,))), 4,
         baseline=("compression_breakout", {"n": 24, "pct": 0.35})),
    Spec("e_compression", "compression", "compression_clue", "1h",
         (("n", (24,)), ("pct", (0.2,)),
          ("clue", ("range_pos", "signed_return", "ema_slope", "pressure"))), 8),
    # F — volume expansion
    Spec("f_volume", "volume", "volume_continuation", "1h", (("r", (2.0, 3.0)),), 2),
    Spec("f_volume", "volume", "volume_fade", "1h", (("r", (2.0, 3.0)),), 2),
    Spec("f_volume", "volume", "volume_continuation", "15m", (("r", (3.0,)),), 4),
    Spec("f_volume", "volume", "volume_downside_expansion", "1h",
         (("r", (2.5,)), ("body", (1.5,))), 4, sides=("short",)),
    # H — abnormal range expansion
    Spec("h_expansion", "expansion_exhaustion", "expansion_continuation", "1h", (("k", (2.0, 3.0)),), 4),
    Spec("h_expansion", "expansion_exhaustion", "expansion_fade", "1h", (("k", (2.0, 3.0)),), 4),
    Spec("h_expansion", "expansion_exhaustion", "expansion_continuation", "15m", (("k", (2.5,)),), 4),
    Spec("h_expansion", "expansion_exhaustion", "expansion_fade", "15m", (("k", (2.5,)),), 4),
    # I — short-term exhaustion
    Spec("i_exhaustion", "expansion_exhaustion", "exhaustion", "1h",
         (("n", (24,)), ("z", (2.0, 3.0)), ("strong_close", ("no", "yes"))), 4),
    Spec("i_exhaustion", "expansion_exhaustion", "blowoff_exhaustion", "1h",
         (("n", (24,)), ("z", (3.0,)), ("r", (2.0,))), 4, sides=("short",),
         baseline=("exhaustion", {"n": 24, "z": 3.0, "strong_close": "yes"})),
    Spec("c_breakout", "breakout", "failed_bounce_breakdown", "1h", (("n", (12,)),), 4,
         sides=("short",), baseline=("breakout", {"n": 12})),
    # K — funding context (vs the equivalent price-only signal)
    Spec("k_funding", "funding", "funding_trend", "1h", (("extreme", (0.9,)),), 8,
         baseline=("momentum_z", {"n": 4, "k": 1.5})),
    Spec("k_funding", "funding", "funding_reversal", "1h", (("extreme", (0.9,)),), 8,
         baseline=("ema_cross", {"n": 20})),
    # L — minimal BTC context (alts only)
    Spec("l_btc_context", "btc_context", "btc_aligned_momentum", "1h",
         (("btc", ("aligned", "against")),), 4, universe="alts",
         baseline=("momentum_z", {"n": 4, "k": 1.5})),
    Spec("l_btc_context", "btc_context", "btc_flat_breakout", "1h", (("n", (24,)),), 4,
         universe="alts", baseline=("breakout", {"n": 24})),
    Spec("l_btc_context", "btc_context", "btc_strength_breakout", "1h", (("n", (24,)),), 4,
         universe="alts", baseline=("breakout", {"n": 24})),
)  # fmt: skip

VOL_SPECS: tuple[tuple[str, str, tuple[tuple[str, tuple], ...], int], ...] = (
    ("compression_state", "1h", (("n", (24,)), ("pct", (0.2,))), 8),
    ("compression_state", "15m", (("n", (32,)), ("pct", (0.2,))), 8),
    ("compression_volume_state", "1h", (("n", (24,)), ("pct", (0.25,)), ("vslope_pct", (0.75,))), 8),
    ("volume_spike_state", "1h", (("r", (3.0,)),), 4),
    ("expansion_state", "1h", (("k", (2.5,)),), 4),
)  # fmt: skip


def _horizons(tf: str, primary: int) -> tuple[int, ...]:
    return H1[primary] if tf == "1h" else M15


def _complexity(rule: str, params: dict, tf: str, ctx: str, universe: str) -> Complexity:
    meta = RULES[rule]
    thresholds = sum(1 for v in params.values() if isinstance(v, int | float) and v < 1000)
    timeframes = 1 + (ctx != "none")
    fams = len(meta.feature_families)
    return Complexity(conditions=meta.conditions, thresholds=thresholds, timeframes=timeframes,
                      feature_families=fams,
                      score=meta.conditions + thresholds + (timeframes - 1) + fams)  # fmt: skip


def _ptuple(params: dict) -> tuple:
    return tuple(params.items())


def _baseline_key(spec: Spec, params: dict, side: str) -> str | None:
    if spec.baseline is None:
        return None
    rule, bp = spec.baseline
    base = next(s for s in SPECS if s.rule == rule and s.timeframe == spec.timeframe)
    resolved = {}
    for axis, values in base.grid:
        v = bp.get(axis, values[0])
        if v == axis or (isinstance(v, str) and v in params):  # "inherit this axis"
            v = params[v]
        resolved[axis] = v
    ps = ",".join(f"{k}={v}" for k, v in resolved.items())
    return f"{rule}[{ps}]:{spec.timeframe}:{side}"


def strategies() -> tuple[IntradayStrategy, ...]:
    """Every catalogue variant, in a fixed order."""
    out = []
    for spec in SPECS:
        axes = [a for a, _ in spec.grid]
        for values in product(*(v for _, v in spec.grid)):
            params = dict(zip(axes, values, strict=True))
            for side in spec.sides:
                meta = RULES[spec.rule]
                symmetric = spec.rule not in SHORT_ONLY
                hyp = meta.hypothesis if symmetric else meta.short_hypothesis
                if symmetric and side == "short":
                    hyp = f"[mirror] {hyp}"
                out.append(IntradayStrategy(
                    family=spec.family, bh_family=spec.bh_family, rule=spec.rule, side=side,
                    timeframe=spec.timeframe, context_timeframe=spec.context,
                    params=_ptuple(params), horizons=_horizons(spec.timeframe, spec.primary),
                    primary_horizon=spec.primary if spec.timeframe == "1h" else 4,
                    universe=spec.universe, symmetric=symmetric, hypothesis=hyp,
                    baseline=_baseline_key(spec, params, side),
                    complexity=_complexity(spec.rule, params, spec.timeframe, spec.context,
                                           spec.universe)))  # fmt: skip
    keys = [s.key for s in out]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate strategy key in the catalogue")
    known = set(keys)
    for s in out:
        if s.baseline is not None and s.baseline not in known:
            raise ValueError(f"{s.key}: baseline {s.baseline} is not a catalogue variant")
    return tuple(out)


def vol_tests() -> tuple[VolTest, ...]:
    out = []
    for rule, tf, grid, primary in VOL_SPECS:
        axes = [a for a, _ in grid]
        for values in product(*(v for _, v in grid)):
            out.append(VolTest(rule=rule, timeframe=tf, params=_ptuple(dict(zip(axes, values, strict=True))),
                               horizons=_horizons(tf, primary) if tf == "1h" else M15,
                               primary_horizon=primary if tf == "1h" else 4,
                               hypothesis=VOL_RULES[rule].hypothesis))  # fmt: skip
    return tuple(out)


def neighbours(s: IntradayStrategy, pool: tuple[IntradayStrategy, ...]) -> list[IntradayStrategy]:
    """Variants of the same rule/side/timeframe/context differing in exactly ONE grid axis by
    one step (the adjacent value). Used for nearby-parameter support."""
    spec = next(x for x in SPECS if x.rule == s.rule and x.timeframe == s.timeframe
                and x.context == s.context_timeframe)  # fmt: skip
    grid = dict(spec.grid)
    mine = s.p
    out = []
    for o in pool:
        if (o.rule, o.side, o.timeframe, o.context_timeframe) != (
            s.rule,
            s.side,
            s.timeframe,
            s.context_timeframe,
        ) or o is s:
            continue
        diff = [a for a in mine if o.p[a] != mine[a]]
        if len(diff) == 1:
            vals = list(grid[diff[0]])
            if abs(vals.index(o.p[diff[0]]) - vals.index(mine[diff[0]])) == 1:
                out.append(o)
    return out


def catalogue_id() -> str:
    return content_id("icat_", {"version": CATALOGUE_VERSION,
                                "strategies": [s.strategy_id for s in strategies()],
                                "vol_tests": [v.test_id for v in vol_tests()]})  # fmt: skip


def summary() -> dict:
    ss = strategies()
    by_fam: dict = {}
    for s in ss:
        d = by_fam.setdefault(s.bh_family, {"long": 0, "short": 0, "rules": set()})
        d[s.side] += 1
        d["rules"].add(s.rule)
    return {
        "catalogue": CATALOGUE_VERSION, "catalogue_id": catalogue_id(), "variants": len(ss),
        "long": sum(s.side == "long" for s in ss), "short": sum(s.side == "short" for s in ss),
        "short_specific": sum(not s.symmetric for s in ss),
        "by_timeframe": {tf: sum(s.timeframe == tf for s in ss) for tf in ("1h", "15m")},
        "with_htf_context": sum(s.context_timeframe != "none" for s in ss),
        "bh_families": {f: {"long": d["long"], "short": d["short"], "rules": sorted(d["rules"])}
                        for f, d in by_fam.items()},
        "vol_tests": len(vol_tests()),
        "complexity_median": sorted(s.complexity.score for s in ss)[len(ss) // 2],
    }  # fmt: skip
