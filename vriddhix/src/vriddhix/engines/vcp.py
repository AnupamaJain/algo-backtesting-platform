"""Volatility Contraction Pattern engine. Implements docs/05-vcp-specification.md.

Pure: DataFrame in, typed result out. No I/O, no database, no clock. That is
what lets the live scanner and the backtester call this same function and get
the same answer.

Two things this engine deliberately does NOT do:

* It does not treat falling ATR as sufficient. Volatility contraction is one
  of seven scored components, and a base can contract while failing every
  other test.
* It does not demand a perfect sequence. Real bases are untidy: 18 -> 11 ->
  12 -> 6 has one out-of-order step and is a good VCP. Quality is measured on
  a continuum; rejection is reserved for structural failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from ..domain.types import (
    CompositeScore,
    Contraction,
    PatternStatus,
    PriorTrend,
    ScoreComponent,
    SwingType,
    VcpBase,
)
from ..features.technical import compute_features
from ..versioning import SCORING_RULE_VERSION, VCP_ENGINE_VERSION
from .swings import alternating, confirmed_swings

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VcpConfig:
    prior_trend_lookback_days: int = 250
    min_prior_trend_pct: float = 30.0
    min_prior_trend_days: int = 40
    ideal_prior_trend_pct: float = 100.0

    min_base_days: int = 30
    max_base_days: int = 250
    min_base_depth_pct: float = 5.0
    max_base_depth_pct: float = 35.0
    ideal_base_depth_pct: float = 18.0
    ideal_base_duration_days: int = 55
    base_pivot_strength: int = 5
    base_high_lookback: int = 30
    max_base_cv: float = 0.12

    contraction_swing_strength: int = 3
    min_contractions: int = 2
    max_contractions: int = 6
    min_contraction_depth_pct: float = 2.0
    contraction_tolerance: float = 0.15
    ideal_compression: float = 0.6
    max_final_contraction_pct: float = 8.0

    ideal_dryup_ratio: float = 0.6
    ideal_base_vol_ratio: float = 0.75

    pivot_buffer_pct: float = 0.1
    pivot_max_distance_from_base_high_pct: float = 8.0
    max_pivot_distance_pct: float = 10.0
    near_pivot_pct: float = 3.0

    breakout_volume_multiplier: float = 1.5
    failure_buffer_pct: float = 2.0
    confirm_within_days: int = 3

    weights: tuple[tuple[str, float], ...] = (
        ("prior_trend", 0.15),
        ("base_quality", 0.15),
        ("contraction_quality", 0.20),
        ("volume_dryup", 0.15),
        ("relative_strength", 0.15),
        ("sector_strength", 0.10),
        ("pivot_readiness", 0.10),
    )

    def __post_init__(self) -> None:
        total = sum(w for _, w in self.weights)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"VCP score weights sum to {total}, expected 1.0")

    @classmethod
    def from_config(cls, cfg) -> VcpConfig:
        vcp = cfg.section("vcp")
        weights = tuple(cfg.get("scoring.vcp").items())
        known = {f for f in cls.__dataclass_fields__ if f != "weights"}
        return cls(**{k: v for k, v in vcp.items() if k in known}, weights=weights)


@dataclass(frozen=True, slots=True)
class VcpResult:
    base: VcpBase
    score: CompositeScore
    stage: PatternStatus
    prior_trend: PriorTrend
    distance_from_pivot_pct: float
    diagnostics: dict
    engine_version: str = VCP_ENGINE_VERSION


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def triangular(x: float, ideal: float, lo: float, hi: float) -> float:
    """1.0 at ``ideal``, tapering linearly to 0 at ``lo`` and ``hi``.

    Encodes "there is a sweet spot", which a monotonic function cannot: a
    60%-deep base is not better than a 35%-deep one just because deeper bases
    score higher up to a point.
    """
    if x <= lo or x >= hi:
        return 0.0
    if x == ideal:
        return 1.0
    if x < ideal:
        return (x - lo) / (ideal - lo) if ideal > lo else 1.0
    return (hi - x) / (hi - ideal) if hi > ideal else 1.0


# ---------------------------------------------------------------------------
# Stage 1 -- prior trend
# ---------------------------------------------------------------------------


def _analyse_prior_trend(
    df: pd.DataFrame, features: pd.DataFrame, base_start_pos: int, cfg: VcpConfig
) -> tuple[PriorTrend | None, float, dict]:
    lookback_start = max(0, base_start_pos - cfg.prior_trend_lookback_days)
    window = df.iloc[lookback_start : base_start_pos + 1]
    if len(window) < cfg.min_prior_trend_days:
        return None, 0.0, {"reason": "insufficient history before base"}

    closes = window["close"]
    trough_pos = int(closes.to_numpy().argmin())
    trough_close = float(closes.iloc[trough_pos])
    start_close = float(closes.iloc[-1])

    advance_pct = (start_close / trough_close - 1.0) * 100.0
    duration = len(closes) - 1 - trough_pos

    row = features.iloc[base_start_pos]
    ema_50, ema_200 = row.get("ema_50"), row.get("ema_200")
    ema_aligned = bool(
        pd.notna(ema_50) and pd.notna(ema_200) and start_close > ema_200 and ema_50 > ema_200
    )

    diagnostics = {
        "advance_pct": round(advance_pct, 2),
        "duration_days": duration,
        "ema_aligned": ema_aligned,
    }

    if advance_pct < cfg.min_prior_trend_pct:
        return None, 0.0, {**diagnostics, "reason": "prior advance too small"}
    if duration < cfg.min_prior_trend_days:
        return None, 0.0, {**diagnostics, "reason": "prior advance too brief"}
    if not ema_aligned:
        return None, 0.0, {**diagnostics, "reason": "not in an established uptrend"}

    trend = PriorTrend(
        start=window.index[trough_pos].date(),
        end=window.index[-1].date(),
        advance_pct=advance_pct,
        duration_days=duration,
        ema_aligned=ema_aligned,
    )
    return trend, advance_pct, diagnostics


def _score_prior_trend(
    df: pd.DataFrame,
    features: pd.DataFrame,
    base_start_pos: int,
    trend: PriorTrend,
    cfg: VcpConfig,
    rs_score: float | None,
) -> float:
    magnitude = clamp(trend.advance_pct / cfg.ideal_prior_trend_pct) * 40.0

    row = features.iloc[base_start_pos]
    close = float(df["close"].iloc[base_start_pos])
    ema_20, ema_50, ema_200 = row.get("ema_20"), row.get("ema_50"), row.get("ema_200")
    if all(pd.notna(v) for v in (ema_20, ema_50, ema_200)) and close > ema_20 > ema_50 > ema_200:
        alignment = 25.0
    elif all(pd.notna(v) for v in (ema_50, ema_200)) and close > ema_50 > ema_200:
        alignment = 15.0
    elif pd.notna(ema_200) and close > ema_200:
        alignment = 8.0
    else:
        alignment = 0.0

    lookback_start = max(0, base_start_pos - cfg.prior_trend_lookback_days)
    window_close = df["close"].iloc[lookback_start : base_start_pos + 1]
    window_ema50 = features["ema_50"].iloc[lookback_start : base_start_pos + 1]
    valid = window_ema50.notna()
    persistence = (
        float((window_close[valid] > window_ema50[valid]).mean()) * 20.0 if valid.any() else 0.0
    )

    # Absent RS scores zero and is flagged rather than redistributed: a score
    # computed without market context must not impersonate one that had it.
    rs_bonus = clamp((rs_score - 50.0) / 50.0) * 15.0 if rs_score is not None else 0.0

    return magnitude + alignment + persistence + rs_bonus


# ---------------------------------------------------------------------------
# Stage 2 -- base boundaries
# ---------------------------------------------------------------------------


def _find_base_start(df: pd.DataFrame, cfg: VcpConfig) -> int | None:
    """Most recent bar that tops out the preceding stretch.

    Identified from confirmed swings only, so the base's left edge is a level
    that was knowable at the time rather than one revealed by later bars.
    """
    swings = confirmed_swings(df, left=cfg.base_pivot_strength, right=cfg.base_pivot_strength)
    highs = [s for s in swings if s.type is SwingType.HIGH]
    if not highs:
        return None

    positions = {d.date(): i for i, d in enumerate(df.index)}
    frame_high = df["high"].to_numpy(dtype=float)

    for swing in reversed(highs):
        pos = positions.get(swing.date)
        if pos is None:
            continue

        bars_since = len(df) - 1 - pos
        if bars_since < cfg.min_base_days:
            continue  # too recent to have formed a base yet
        if bars_since > cfg.max_base_days:
            break  # everything older is further away still

        window_start = max(0, pos - cfg.base_high_lookback)
        if frame_high[pos] >= frame_high[window_start : pos + 1].max():
            return pos
    return None


# ---------------------------------------------------------------------------
# Stage 3 -- contractions
# ---------------------------------------------------------------------------


def _find_contractions(
    df: pd.DataFrame, features: pd.DataFrame, base_slice: slice, cfg: VcpConfig
) -> list[Contraction]:
    base_df = df.iloc[base_slice]
    swings = alternating(
        confirmed_swings(
            base_df,
            left=cfg.contraction_swing_strength,
            right=cfg.contraction_swing_strength,
        )
    )
    if len(swings) < 2:
        return []

    positions = {d.date(): i for i, d in enumerate(base_df.index)}
    atr_col = next((c for c in features.columns if c.startswith("atr_")), None)

    contractions: list[Contraction] = []
    sequence = 0
    for first, second in zip(swings, swings[1:]):
        if not (first.type is SwingType.HIGH and second.type is SwingType.LOW):
            continue

        depth_pct = (first.price - second.price) / first.price * 100.0
        if depth_pct < cfg.min_contraction_depth_pct:
            continue  # noise, not a pullback

        start_pos = positions.get(first.date)
        end_pos = positions.get(second.date)
        if start_pos is None or end_pos is None or end_pos <= start_pos:
            continue

        leg = base_df.iloc[start_pos : end_pos + 1]
        leg_features = features.iloc[base_slice].iloc[start_pos : end_pos + 1]

        sequence += 1
        contractions.append(
            Contraction(
                sequence=sequence,
                start=first.date,
                end=second.date,
                high=first.price,
                low=second.price,
                depth_pct=depth_pct,
                duration_days=end_pos - start_pos,
                avg_volume=float(leg["volume"].mean()),
                atr_avg=float(leg_features[atr_col].mean()) if atr_col else float("nan"),
            )
        )

    return contractions[-cfg.max_contractions :]


def _score_contractions(contractions: list[Contraction], cfg: VcpConfig) -> tuple[float, dict]:
    depths = [c.depth_pct for c in contractions]
    pairs = list(zip(depths, depths[1:]))

    tightening = sum(1 for a, b in pairs if b <= a * (1 + cfg.contraction_tolerance))
    monotonic = 50.0 * (tightening / len(pairs)) if pairs else 0.0

    compression = clamp(1.0 - depths[-1] / depths[0]) if depths[0] > 0 else 0.0
    compression_score = 30.0 * clamp(compression / cfg.ideal_compression)

    final_score = 20.0 * clamp(1.0 - depths[-1] / cfg.max_final_contraction_pct)

    return monotonic + compression_score + final_score, {
        "depths_pct": [round(d, 2) for d in depths],
        "tightening_pairs": f"{tightening}/{len(pairs)}",
        "compression": round(compression, 3),
    }


# ---------------------------------------------------------------------------
# Stage 4 -- volume dry-up
# ---------------------------------------------------------------------------


def _score_volume(
    df: pd.DataFrame,
    base_slice: slice,
    contractions: list[Contraction],
    cfg: VcpConfig,
) -> tuple[float, dict]:
    volumes = [c.avg_volume for c in contractions]
    half = max(1, len(volumes) // 2)
    early = float(np.mean(volumes[:half]))
    late = float(np.mean(volumes[-half:]))
    dryup_ratio = late / early if early > 0 else 1.0

    base_avg = float(df["volume"].iloc[base_slice].mean())
    prior_slice = slice(max(0, base_slice.start - 50), base_slice.start)
    prior = df["volume"].iloc[prior_slice]
    prior_avg = float(prior.mean()) if len(prior) else base_avg
    base_ratio = base_avg / prior_avg if prior_avg > 0 else 1.0

    dryup_score = 60.0 * clamp((1.0 - dryup_ratio) / (1.0 - cfg.ideal_dryup_ratio))
    base_score = 40.0 * clamp((1.0 - base_ratio) / (1.0 - cfg.ideal_base_vol_ratio))

    return dryup_score + base_score, {
        "dryup_ratio": round(dryup_ratio, 3),
        "base_vs_prior_ratio": round(base_ratio, 3),
        # Expansion into the pivot is a negative, not a neutral.
        "volume_expanding": dryup_ratio > 1.0,
    }


# ---------------------------------------------------------------------------
# Stage 5 -- pivot
# ---------------------------------------------------------------------------


def _find_pivot(
    base_high: float, contractions: list[Contraction], cfg: VcpConfig
) -> tuple[float, date | None, str]:
    last = contractions[-1]
    within_reach = (base_high - last.high) / base_high * 100.0 <= (
        cfg.pivot_max_distance_from_base_high_pct
    )

    if within_reach:
        candidate, pivot_date, kind = last.high, last.start, "LAST_CONTRACTION_HIGH"
    else:
        candidate, pivot_date, kind = base_high, None, "BASE_HIGH"

    # Buffer stops a one-tick poke through the exact high registering as a
    # breakout.
    return candidate * (1.0 + cfg.pivot_buffer_pct / 100.0), pivot_date, kind


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def detect(
    df: pd.DataFrame,
    cfg: VcpConfig | None = None,
    *,
    as_of: date | None = None,
    features: pd.DataFrame | None = None,
    rs_score: float | None = None,
    sector_score: float | None = None,
) -> VcpResult | None:
    """Detect a VCP base as it stands at ``as_of``.

    Returns ``None`` when no candidate meets the structural minimums -- which
    is different from a poor base, which returns a result with a low score.
    ``diagnostics`` always names the gate that rejected a candidate.
    """
    cfg = cfg or VcpConfig()

    if as_of is not None:
        df = df.loc[: pd.Timestamp(as_of)]
    if len(df) < cfg.min_base_days + cfg.min_prior_trend_days:
        return None

    if features is None:
        features = compute_features(df, validate=False)
    else:
        features = features.loc[df.index]

    base_start_pos = _find_base_start(df, cfg)
    if base_start_pos is None:
        return None

    base_slice = slice(base_start_pos, len(df))
    base_df = df.iloc[base_slice]

    base_high = float(base_df["high"].max())
    base_low = float(base_df["low"].min())
    depth_pct = (base_high - base_low) / base_high * 100.0
    duration_days = len(base_df) - 1

    if not (cfg.min_base_days <= duration_days <= cfg.max_base_days):
        return None
    # A 2%-deep "base" is drift, not supply being absorbed.
    if not (cfg.min_base_depth_pct <= depth_pct <= cfg.max_base_depth_pct):
        return None

    trend, _, trend_diag = _analyse_prior_trend(df, features, base_start_pos, cfg)
    if trend is None:
        return None

    contractions = _find_contractions(df, features, base_slice, cfg)
    if len(contractions) < cfg.min_contractions:
        return None

    last_close = float(df["close"].iloc[-1])

    # -- components ------------------------------------------------------
    prior_trend_score = _score_prior_trend(df, features, base_start_pos, trend, cfg, rs_score)

    depth_quality = 40.0 * triangular(
        depth_pct, cfg.ideal_base_depth_pct, cfg.min_base_depth_pct, cfg.max_base_depth_pct
    )
    duration_quality = 25.0 * triangular(
        duration_days, cfg.ideal_base_duration_days, cfg.min_base_days, cfg.max_base_days
    )
    closes = base_df["close"]
    cv = float(closes.std() / closes.mean()) if closes.mean() else 1.0
    tightness = 20.0 * (1.0 - clamp(cv / cfg.max_base_cv))
    span = base_high - base_low
    position = 15.0 * (clamp(1.0 - (base_high - last_close) / span) if span > 0 else 0.0)
    base_quality_score = depth_quality + duration_quality + tightness + position

    contraction_score, contraction_diag = _score_contractions(contractions, cfg)
    volume_score, volume_diag = _score_volume(df, base_slice, contractions, cfg)

    pivot_price, pivot_date, pivot_type = _find_pivot(base_high, contractions, cfg)
    distance_pct = (pivot_price - last_close) / last_close * 100.0
    pivot_readiness = 100.0 * clamp(1.0 - max(distance_pct, 0.0) / cfg.max_pivot_distance_pct)

    raw = {
        "prior_trend": prior_trend_score,
        "base_quality": base_quality_score,
        "contraction_quality": contraction_score,
        "volume_dryup": volume_score,
        "relative_strength": rs_score if rs_score is not None else 0.0,
        "sector_strength": sector_score if sector_score is not None else 0.0,
        "pivot_readiness": pivot_readiness,
    }
    components = tuple(
        ScoreComponent(name=name, raw=clamp(raw[name], 0.0, 100.0), weight=weight)
        for name, weight in cfg.weights
    )
    total = sum(c.contribution for c in components)

    # -- stage -----------------------------------------------------------
    avg_volume_50 = features.get("avg_volume_50")
    latest_volume = float(df["volume"].iloc[-1])
    reference_volume = (
        float(avg_volume_50.iloc[-1])
        if avg_volume_50 is not None and pd.notna(avg_volume_50.iloc[-1])
        else float("nan")
    )
    volume_confirmed = (
        bool(latest_volume >= reference_volume * cfg.breakout_volume_multiplier)
        if not np.isnan(reference_volume)
        else False
    )

    if last_close > pivot_price:
        # Detection, not confirmation: a close above the pivot without volume
        # is still a breakout. services/lifecycle.py decides CONFIRMED vs
        # FAILED from what happens next.
        stage = PatternStatus.BREAKOUT
    elif distance_pct <= cfg.near_pivot_pct:
        stage = PatternStatus.NEAR_PIVOT
    else:
        stage = PatternStatus.FORMING

    base = VcpBase(
        start=base_df.index[0].date(),
        end=base_df.index[-1].date(),
        base_high=base_high,
        base_low=base_low,
        depth_pct=depth_pct,
        duration_days=duration_days,
        contractions=tuple(contractions),
        pivot_price=pivot_price,
        pivot_date=pivot_date or base_df.index[0].date(),
        pivot_type=pivot_type,
    )

    return VcpResult(
        base=base,
        score=CompositeScore(
            total=clamp(total, 0.0, 100.0),
            components=components,
            rule_version=SCORING_RULE_VERSION,
        ),
        stage=stage,
        prior_trend=trend,
        distance_from_pivot_pct=distance_pct,
        diagnostics={
            "prior_trend": trend_diag,
            "contractions": contraction_diag,
            "volume": volume_diag,
            "pivot_type": pivot_type,
            "volume_confirmed": volume_confirmed,
            "rs_available": rs_score is not None,
            "sector_available": sector_score is not None,
            "base_cv": round(cv, 4),
        },
    )
