"""``microdir_features_v1``: 15-minute signal windows built from ``microstructure_1m_v1``.

Input: one coin's 1-minute frame from ``microstructure.query.load_microstructure`` (gap-filled
grid, ``fill_missing=True``; MISSING/GAP/PARTIAL minutes are kept as such, never zero-filled).
Output: one row per UTC quarter hour with raw window aggregates, causal normalizations and the
frozen condition flags of ``spec``.

Causality:

* a window's own values use only its 15 minutes (and the previous window's last state as the
  start reference); its **availability** is the latest finalization among those minutes;
* every percentile / scale uses PRIOR COMPLETE windows only (the last ``NORM_DAYS``), never
  the window itself or anything later, and is undefined (NaN) until ``NORM_MIN_WINDOWS``
  prior windows exist;
* a window is **complete** only if all 15 minutes are COMPLETE; incomplete windows never
  generate events and never enter a normalization.

Sign conventions: ``s`` = +1 for one-sided buy flow, -1 for sell flow. Flags are read
relative to ``s`` ("opposing book" = asks for buy flow, bids for sell flow). Everything is
mirror-symmetric: negating flow and mirroring prices/book sides maps buy events to sell
events with identical flags (tested).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.research.microdir import spec as sp

W = sp.SIGNAL_MINUTES
NEEDED = ("status", "buy_ntl", "sell_ntl", "buy_vol", "sell_vol", "first_px", "last_px",
          "high_px", "low_px", "bid_end", "ask_end", "bid5_end", "ask5_end", "bid20_end",
          "ask20_end", "bid_replenish", "ask_replenish", "oi_end", "funding_end", "lp_ntl",
          "lp_buy_ntl", "lp_threshold", "spread_bps_mean", "available_at")  # fmt: skip


def grid(df1: pd.DataFrame) -> pd.DataFrame:
    """The 1-minute frame on a full grid aligned to quarter hours (missing minutes MISSING)."""
    df = df1.copy()
    df["minute_open"] = pd.to_datetime(df["minute_open"], utc=True)
    if df.empty:
        return df
    start = df["minute_open"].min().floor(f"{W}min")
    end = df["minute_open"].max().floor(f"{W}min") + pd.Timedelta(minutes=W)
    idx = pd.date_range(start, end, freq="1min", inclusive="left", tz="UTC")
    df = df.drop_duplicates("minute_open", keep="last").set_index("minute_open").reindex(idx)
    df.index.name = "minute_open"
    df["status"] = df["status"].fillna("MISSING")
    for c in NEEDED:
        if c not in df:
            df[c] = np.nan
    df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
    return df.reset_index()


def _num(df: pd.DataFrame, c: str) -> np.ndarray:
    return pd.to_numeric(df[c], errors="coerce").to_numpy(float)


def _prior_pct(x: np.ndarray, ok: np.ndarray) -> np.ndarray:
    """Share of the prior ``NORM_DAYS`` of complete windows with value <= x (prior only;
    NaN until ``NORM_MIN_WINDOWS`` prior values exist)."""
    n = sp.NORM_DAYS * 1440 // W
    hist = np.concatenate([np.full(n, np.nan), np.where(ok, x, np.nan)])
    out = np.full(len(x), np.nan)
    view = np.lib.stride_tricks.sliding_window_view(hist, n)  # row i: windows i-n .. i-1
    for a in range(0, len(x), 2048):
        b = min(a + 2048, len(x))
        win = view[a:b]
        cnt = np.isfinite(win).sum(1)
        with np.errstate(invalid="ignore"):
            le = (win <= x[a:b, None]).sum(1)
        good = (cnt >= sp.NORM_MIN_WINDOWS) & np.isfinite(x[a:b])
        out[a:b] = np.where(good, le / np.maximum(cnt, 1), np.nan)
    return out


def _prior_scale(x: np.ndarray, ok: np.ndarray, *, center: bool) -> np.ndarray:
    """Trailing std (center=True) or RMS (center=False) of prior complete windows."""
    n = sp.NORM_DAYS * 1440 // W
    s = pd.Series(np.where(ok, x, np.nan)).shift(1)
    if center:
        out = s.rolling(n, min_periods=sp.NORM_MIN_WINDOWS).std()
    else:
        out = np.sqrt((s * s).rolling(n, min_periods=sp.NORM_MIN_WINDOWS).mean())
    out = out.to_numpy(float)
    return np.where(out > 0, out, np.nan)


def _sign(x: np.ndarray) -> np.ndarray:
    return np.sign(np.nan_to_num(x))


def windows(df1: pd.DataFrame, coin: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(15m feature frame, 1m grid). Row ``w`` of the feature frame covers grid minutes
    ``w*15 .. w*15+14``."""
    g = grid(df1)
    if g.empty:
        return pd.DataFrame(), g
    nw = len(g) // W
    status = g["status"].to_numpy().reshape(nw, W)
    complete = (status == "COMPLETE").all(axis=1)

    def m(c: str) -> np.ndarray:
        return _num(g, c).reshape(nw, W)

    buy, sell = m("buy_ntl"), m("sell_ntl")
    bvol, svol = m("buy_vol"), m("sell_vol")
    d_min = buy - sell
    buy_w, sell_w = np.nansum(buy, 1), np.nansum(sell, 1)
    delta = buy_w - sell_w
    ntl = buy_w + sell_w
    vol = np.nansum(bvol, 1) + np.nansum(svol, 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        buy_frac = np.where(vol > 0, np.nansum(bvol, 1) / vol, np.nan)
    mid = (m("bid_end") + m("ask_end")) / 2
    mid_last = mid[:, -1]
    mid_ref = np.concatenate([[np.nan], mid_last[:-1]])  # previous window's closing mid
    r = np.log(mid_last / mid_ref)
    # trade-price candle (what an OHLCV bar would show)
    fp, lp_, hp, lo = m("first_px"), m("last_px"), m("high_px"), m("low_px")
    c_open = pd.DataFrame(fp).bfill(axis=1).to_numpy()[:, 0]
    c_close = pd.DataFrame(lp_).ffill(axis=1).to_numpy()[:, -1]
    c_high, c_low = np.nanmax(np.where(np.isfinite(hp), hp, -np.inf), 1), np.nanmin(
        np.where(np.isfinite(lo), lo, np.inf), 1)  # fmt: skip
    c_high = np.where(np.isfinite(c_high), c_high, np.nan)
    c_low = np.where(np.isfinite(c_low), c_low, np.nan)
    rc = np.log(c_close / np.concatenate([[np.nan], c_close[:-1]]))
    rng = c_high - c_low
    with np.errstate(invalid="ignore", divide="ignore"):
        clv = np.where(rng > 0, (c_close - c_low) / rng, 0.5)
    # persistence
    s = _sign(delta)
    sub = d_min.reshape(nw, W // sp.SUB_MINUTES, sp.SUB_MINUTES)
    sub_sum = np.nansum(sub, 2)
    n_sub_same = (np.sign(sub_sum) == s[:, None]).sum(1)
    n_min_same = (np.sign(np.nan_to_num(d_min)) == s[:, None]).sum(1)
    absd = np.abs(np.nan_to_num(d_min))
    with np.errstate(invalid="ignore", divide="ignore"):
        burst_share = np.where(absd.sum(1) > 0, absd.max(1) / absd.sum(1), np.nan)
    # book: replenishment relative to the notional that hit that side, depth persistence
    ask_rep, bid_rep = np.nansum(m("ask_replenish"), 1), np.nansum(m("bid_replenish"), 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        rep_ask = np.where(buy_w > 0, ask_rep / buy_w, np.nan)
        rep_bid = np.where(sell_w > 0, bid_rep / sell_w, np.nan)
    a5, b5 = m("ask5_end"), m("bid5_end")
    a5_end, b5_end = a5[:, -1], b5[:, -1]
    a5_ref = np.concatenate([[np.nan], a5_end[:-1]])
    b5_ref = np.concatenate([[np.nan], b5_end[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        ask_depth_ratio = a5_end / a5_ref
        bid_depth_ratio = b5_end / b5_ref
        imb5 = (b5_end - a5_end) / (b5_end + a5_end)
        b20, a20 = m("bid20_end")[:, -1], m("ask20_end")[:, -1]
        imb20 = (b20 - a20) / (b20 + a20)
    imb5_chg = imb5 - np.concatenate([[np.nan], imb5[:-1]])
    sp_bps = m("spread_bps_mean")
    n_sp = np.isfinite(sp_bps).sum(1)
    spread = np.where(n_sp > 0, np.nansum(sp_bps, 1) / np.maximum(n_sp, 1), np.nan)
    # positioning
    oi = m("oi_end")[:, -1]
    oi_chg = np.log(oi / np.concatenate([[np.nan], oi[:-1]]))
    funding = m("funding_end")[:, -1]
    # large prints (NULL until the 24A warmup): available only if every minute has a threshold
    lpt = m("lp_threshold")
    lp_ok = np.isfinite(lpt).all(1)
    lp_net = np.where(lp_ok, 2 * np.nansum(m("lp_buy_ntl"), 1) - np.nansum(m("lp_ntl"), 1), np.nan)
    avail = g["available_at"].to_numpy().reshape(nw, W)
    avail_max = pd.to_datetime(pd.DataFrame(avail).max(axis=1), utc=True)
    w_open = g["minute_open"].to_numpy()[::W][:nw]

    with np.errstate(invalid="ignore", divide="ignore"):
        ok = complete & np.isfinite(r) & np.isfinite(delta)
        f = pd.DataFrame({
            "coin": coin, "window_open": pd.to_datetime(w_open, utc=True), "complete": complete,
            "n_complete_minutes": (status == "COMPLETE").sum(1),
            "buy_ntl": buy_w, "sell_ntl": sell_w, "delta_ntl": delta, "ntl": ntl,
            "delta_vol": np.nansum(bvol, 1) - np.nansum(svol, 1), "buy_frac": buy_frac,
            "mid_ref": mid_ref, "mid_close": mid_last, "ret_mid": r,
            "c_open": c_open, "c_high": c_high, "c_low": c_low, "c_close": c_close,
            "ret_candle": rc, "clv": clv,
            "n_sub_same": n_sub_same, "n_min_same": n_min_same, "burst_share": burst_share,
            "rep_ask": rep_ask, "rep_bid": rep_bid, "ask_depth_ratio": ask_depth_ratio,
            "bid_depth_ratio": bid_depth_ratio, "imb5_end": imb5, "imb20_end": imb20,
            "imb5_chg": imb5_chg, "spread_bps": spread, "oi_end": oi, "oi_chg": oi_chg,
            "funding": funding, "lp_available": lp_ok, "lp_net_ntl": lp_net,
            "available_at": avail_max,
        })  # fmt: skip
    f["window_close"] = f["window_open"] + pd.Timedelta(minutes=W)
    # ------------------------------------------------------------------ normalization
    okc = ok
    f["flow_pct"] = _prior_pct(delta, okc)
    f["flow_z"] = delta / _prior_scale(delta, okc, center=True)
    f["sigma15"] = _prior_scale(r, okc, center=False)
    f["sigma15_candle"] = _prior_scale(rc, okc & np.isfinite(rc), center=False)
    f["c_ret_z"] = rc / f["sigma15_candle"].to_numpy()
    f["c_ret_pct"] = _prior_pct(rc, okc & np.isfinite(rc))
    f["vol_pct"] = _prior_pct(ntl, okc)
    f["rep_ask_pct"] = _prior_pct(rep_ask, okc & np.isfinite(rep_ask))
    f["rep_bid_pct"] = _prior_pct(rep_bid, okc & np.isfinite(rep_bid))
    f["oi_z"] = oi_chg / _prior_scale(oi_chg, okc & np.isfinite(oi_chg), center=True)
    f["funding_pct"] = _prior_pct(funding, okc & np.isfinite(funding))
    rv = np.sqrt(pd.Series(np.where(okc, r * r, np.nan)).rolling(sp.RV_WINDOWS,
                                                                 min_periods=sp.RV_WINDOWS // 2).mean())  # fmt: skip
    f["rv4h"] = rv.to_numpy()
    f["rv_pct"] = _prior_pct(f["rv4h"].to_numpy(), okc & np.isfinite(f["rv4h"].to_numpy()))
    return classify(f), g


def classify(f: pd.DataFrame) -> pd.DataFrame:
    """Frozen buckets and condition flags (relative to the flow side)."""
    f = f.copy()
    pct = f["flow_pct"].to_numpy(float)
    s = np.where(pct >= sp.FLOW_STRONG, 1, np.where(pct <= 1 - sp.FLOW_STRONG, -1, 0))
    s = np.where(np.isfinite(pct), s, 0)
    f["flow_side"] = s
    f["flow_bucket"] = np.select(
        [pct >= sp.FLOW_EXTREME, pct >= sp.FLOW_STRONG, pct <= 1 - sp.FLOW_EXTREME,
         pct <= 1 - sp.FLOW_STRONG, np.isfinite(pct)],
        ["extreme_buy", "strong_buy", "extreme_sell", "strong_sell", "neutral"], "undefined")  # fmt: skip
    resp = s * f["ret_mid"].to_numpy(float) / f["sigma15"].to_numpy(float)
    f["resp_z"] = np.where(s != 0, resp, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        f["response_efficiency"] = f["resp_z"] / np.abs(f["flow_z"])
    f["response"] = np.select(
        [f["resp_z"] >= sp.RESP_EFFICIENT, f["resp_z"] >= sp.RESP_WEAK,
         f["resp_z"] > sp.RESP_OPPOSITE, f["resp_z"] <= sp.RESP_OPPOSITE],
        ["efficient", "moderate", "weak", "opposite"], "none")  # fmt: skip
    rep_pct = np.where(s > 0, f["rep_ask_pct"], np.where(s < 0, f["rep_bid_pct"], np.nan))
    depth = np.where(s > 0, f["ask_depth_ratio"], np.where(s < 0, f["bid_depth_ratio"], np.nan))
    f["opp_rep_pct"], f["opp_depth_ratio"] = rep_pct, depth
    book_ok = np.isfinite(rep_pct) & np.isfinite(depth)
    res = book_ok & (rep_pct >= sp.REPLENISH_PCT) & (depth >= sp.DEPTH_PERSIST)
    con = book_ok & (rep_pct < sp.REPLENISH_PCT) & (depth < sp.DEPTH_PERSIST)
    f["book_state"] = np.select([res, con, book_ok], ["resilient", "consumed", "mixed"], "unknown")
    oz = f["oi_z"].to_numpy(float)
    oi_ok = np.isfinite(oz)
    f["oi_state"] = np.select([oz >= sp.OI_Z, oz <= -sp.OI_Z, oi_ok],
                              ["rising", "falling", "flat"], "unknown")  # fmt: skip
    fp = f["funding_pct"].to_numpy(float)
    f["funding_aligned"] = np.isfinite(fp) & (
        ((s > 0) & (fp >= sp.FUNDING_CROWD)) | ((s < 0) & (fp <= 1 - sp.FUNDING_CROWD)))  # fmt: skip
    pers = (f["n_sub_same"] >= sp.PERSIST_SUBWINDOWS) & (f["n_min_same"] >= sp.PERSIST_MINUTES)
    burst = ~pers & (f["burst_share"] >= sp.BURST_SHARE)
    f["persistence"] = np.select([s == 0, pers, burst], ["none", "persistent", "burst"], "mixed")
    lp_al = f["lp_available"] & (s * f["lp_net_ntl"].fillna(0) > 0)
    f["lp_state"] = np.select(
        [~f["lp_available"], lp_al], ["unavailable", "aligned"], "not_aligned"
    )
    rvp = f["rv_pct"].to_numpy(float)
    f["vol_state"] = np.select([rvp < 1 / 3, rvp < 2 / 3, rvp <= 1], ["low", "normal", "high"],
                               "unknown")  # fmt: skip
    hours = f["window_open"].dt.hour.to_numpy()
    f["session"] = np.select([(hours >= a) & (hours < b) for _, a, b in sp.SESSIONS],
                             [n for n, _, _ in sp.SESSIONS], "other")  # fmt: skip
    # eligibility: complete, normalized, and a valid response
    f["eligible"] = (f["complete"] & np.isfinite(pct) & np.isfinite(f["sigma15"])
                     & np.isfinite(f["ret_mid"]))  # fmt: skip
    # candle (OHLCV-only) twins: their own side
    cz = f["c_ret_z"].to_numpy(float)
    vp = f["vol_pct"].to_numpy(float)
    hv = np.isfinite(vp) & (vp >= sp.VOLUME_HIGH)
    cp = f["c_ret_pct"].to_numpy(float)
    f["twin_candle_big_move"] = np.where(cp >= sp.FLOW_STRONG, 1,
                                         np.where(cp <= 1 - sp.FLOW_STRONG, -1, 0))  # fmt: skip
    f["twin_candle_momentum"] = np.where(hv & (np.abs(cz) >= sp.CANDLE_BIG), np.sign(cz), 0)
    clv = f["clv"].to_numpy(float)
    small = hv & (np.abs(cz) < sp.CANDLE_SMALL)
    f["twin_candle_absorption"] = np.where(small & (clv <= sp.CLV_LOW), -1,
                                           np.where(small & (clv >= sp.CLV_HIGH), 1, 0))  # fmt: skip
    for c in ("twin_candle_big_move", "twin_candle_momentum", "twin_candle_absorption"):
        f[c] = np.where(f["eligible"] & np.isfinite(cz), f[c], 0).astype(int)
    f["hv"] = hv
    return f


def flags(f: pd.DataFrame) -> dict[str, np.ndarray]:
    """Boolean condition arrays (relative to the flow side) used by the hypotheses."""
    s = f["flow_side"].to_numpy()
    one = (s != 0) & f["eligible"].to_numpy()
    resp = f["response"].to_numpy()
    book = f["book_state"].to_numpy()
    oi = f["oi_state"].to_numpy()
    pers = f["persistence"].to_numpy()
    lp = f["lp_state"].to_numpy()
    crowd = f["crowd_aligned"].to_numpy() if "crowd_aligned" in f else np.zeros(len(f), bool)
    crowd_known = (f["crowd_known"].to_numpy() if "crowd_known" in f
                   else np.zeros(len(f), bool))  # fmt: skip
    return {
        "one_sided": one,
        "resp_efficient": resp == "efficient",
        "resp_weak": np.isin(resp, ["weak", "opposite"]),
        "opp_resilient": book == "resilient",
        "opp_not_resilient": np.isin(book, ["consumed", "mixed"]),
        "opp_consumed": book == "consumed",
        "opp_not_consumed": np.isin(book, ["resilient", "mixed"]),
        "oi_rising": oi == "rising",
        "oi_falling": oi == "falling",
        "oi_not_rising": np.isin(oi, ["flat", "falling"]),
        "persistent": pers == "persistent",
        "burst": pers == "burst",
        "lp_aligned": lp == "aligned",
        "lp_not_aligned": lp == "not_aligned",
        "crowd_aligned": crowd & crowd_known,
        "crowd_not": ~crowd & crowd_known,
    }


def member_mask(f: pd.DataFrame, fl: dict, h: sp.Hypothesis, side: str) -> np.ndarray:
    """Windows where hypothesis ``h`` fires on ``side``."""
    s = f["flow_side"].to_numpy()
    want = (1 if side == "long" else -1) * (1 if h.action == "follow" else -1)
    m = fl["one_sided"] & (s == want)
    for c in h.requires:
        m = m & fl[c]
    return m
