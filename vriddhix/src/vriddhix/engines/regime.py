"""Market breadth and regime. Implements docs/07.

Regime describes what the market **is doing now**, measured from breadth and
trend. It is not a forecast and must never be rendered as one.

Two refusals are built in, and both matter more than any scoring detail:

* Below ``min_universe_coverage_pct`` the engine returns ``None``. A regime
  computed from 60% of the universe is a regime for a biased sample, and a
  confident wrong number is worse than an honest gap.
* Regime changes require hysteresis. Without it the state flips on noise, and
  a backtest filtering on regime produces entry churn that has nothing to do
  with the market.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from ..domain.types import BreadthSnapshot, MarketRegime, RegimeResult, ScoreComponent
from ..versioning import REGIME_ENGINE_VERSION


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    benchmark_index: str = "NIFTY500"
    weights: tuple[tuple[str, float], ...] = (
        ("trend", 0.30), ("breadth", 0.25), ("momentum", 0.20),
        ("participation", 0.15), ("volatility", 0.10),
    )
    strong_bull: float = 75.0
    bull: float = 60.0
    neutral: float = 40.0
    bear: float = 25.0
    hysteresis_band: float = 3.0
    hysteresis_days: int = 2
    ideal_ema50_slope: float = 0.03
    low_vol: float = 0.10
    high_vol: float = 0.30
    min_universe_coverage_pct: float = 80.0

    def __post_init__(self) -> None:
        total = sum(w for _, w in self.weights)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"regime weights sum to {total}, expected 1.0")

    @classmethod
    def from_config(cls, cfg) -> RegimeConfig:
        regime = cfg.section("regime")
        thresholds = regime.get("thresholds", {})
        return cls(
            benchmark_index=regime.get("benchmark_index", "NIFTY500"),
            weights=tuple(regime["weights"].items()),
            strong_bull=float(thresholds.get("strong_bull", 75)),
            bull=float(thresholds.get("bull", 60)),
            neutral=float(thresholds.get("neutral", 40)),
            bear=float(thresholds.get("bear", 25)),
            hysteresis_band=float(regime.get("hysteresis_band", 3)),
            hysteresis_days=int(regime.get("hysteresis_days", 2)),
            ideal_ema50_slope=float(regime.get("ideal_ema50_slope", 0.03)),
            low_vol=float(regime.get("low_vol", 0.10)),
            high_vol=float(regime.get("high_vol", 0.30)),
            min_universe_coverage_pct=float(regime.get("min_universe_coverage_pct", 80)),
        )


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Breadth
# ---------------------------------------------------------------------------


def compute_breadth(
    snapshot: pd.DataFrame, as_of: date, *, universe_size: int | None = None
) -> BreadthSnapshot:
    """Breadth across the universe on one date.

    ``snapshot`` is indexed by symbol, one row per symbol that HAS a bar for
    this date. Symbols without one are excluded from both numerator and
    denominator -- counting a missing bar as "not above its EMA" would
    manufacture bearishness on a data-outage day.
    """
    present = len(snapshot)
    total = universe_size if universe_size is not None else present

    if present == 0:
        return BreadthSnapshot(
            as_of=as_of, advancers=0, decliners=0, unchanged=0, ad_ratio=0.0,
            pct_above_ema_20=None, pct_above_ema_50=None, pct_above_ema_100=None,
            pct_above_ema_200=None, new_highs_52w=0, new_lows_52w=0,
            universe_size=0, excluded=total,
        )

    change = snapshot.get("change", pd.Series(0.0, index=snapshot.index)).fillna(0.0)
    advancers = int((change > 0).sum())
    decliners = int((change < 0).sum())
    unchanged = present - advancers - decliners

    def pct(column: str) -> float | None:
        """Percentage above, or None when the reading does not exist.

        A missing column means the caller never computed that EMA; a column
        of all-NaN means no symbol had enough history. Both are "unknown",
        and both used to return 0.0 -- indistinguishable from a genuine
        reading of zero, which is a very different statement about the tape.
        """
        if column not in snapshot.columns:
            return None
        values = snapshot[column].dropna()
        return float(values.mean() * 100.0) if len(values) else None

    return BreadthSnapshot(
        as_of=as_of,
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
        ad_ratio=advancers / max(decliners, 1),
        pct_above_ema_20=pct("above_ema_20"),
        pct_above_ema_50=pct("above_ema_50"),
        pct_above_ema_100=pct("above_ema_100"),
        pct_above_ema_200=pct("above_ema_200"),
        new_highs_52w=int(snapshot.get("is_new_high", pd.Series(dtype=bool)).sum()),
        new_lows_52w=int(snapshot.get("is_new_low", pd.Series(dtype=bool)).sum()),
        universe_size=present,
        excluded=max(0, total - present),
    )


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


def _trend_score(benchmark: pd.Series, cfg: RegimeConfig) -> float:
    close = benchmark.get("close")
    ema_20, ema_50, ema_200 = (benchmark.get(k) for k in ("ema_20", "ema_50", "ema_200"))
    slope = benchmark.get("ema_50_slope_20")

    score = 0.0
    if pd.notna(close) and pd.notna(ema_200) and close > ema_200:
        score += 30.0
    if pd.notna(close) and pd.notna(ema_50) and close > ema_50:
        score += 25.0
    if all(pd.notna(v) for v in (ema_20, ema_50, ema_200)) and ema_20 > ema_50 > ema_200:
        score += 25.0
    elif pd.notna(ema_50) and pd.notna(ema_200) and ema_50 > ema_200:
        score += 12.0
    if pd.notna(slope):
        score += 20.0 * _clamp(float(slope) / cfg.ideal_ema50_slope)
    return score


def _breadth_score(breadth: BreadthSnapshot) -> float:
    # Weighted toward the slower averages: pct_above_ema_20 whipsaws on a
    # three-day bounce and would drag the regime with it.
    #
    # Weights renormalise over the readings that exist, so a universe too
    # young for a 200-day EMA scores on what it has rather than being
    # penalised for the absence. Treating a missing reading as zero would
    # report every newly-listed universe as maximally bearish.
    parts = [
        (0.30, breadth.pct_above_ema_20),
        (0.35, breadth.pct_above_ema_50),
        (0.35, breadth.pct_above_ema_200),
    ]
    present = [(w, v) for w, v in parts if v is not None]
    if not present:
        return 50.0  # no breadth reading at all: neutral, not bearish
    total_weight = sum(w for w, _ in present)
    return sum(w * v for w, v in present) / total_weight


def _momentum_score(breadth: BreadthSnapshot, benchmark: pd.Series) -> float:
    size = max(breadth.universe_size, 1)
    # Offsets centre "flat" mid-range: equal new highs and lows is neutral,
    # not maximally weak.
    nh_nl = 40.0 * _clamp((breadth.net_new_highs / size + 0.02) / 0.06)

    ad = breadth.ad_ratio
    ad_trend = 30.0 * _clamp((ad - 0.7) / 0.8)

    ret_3m = benchmark.get("ret_3m")
    idx_mom = 30.0 * _clamp(((float(ret_3m) if pd.notna(ret_3m) else 0.0) + 0.05) / 0.20)

    return nh_nl + ad_trend + idx_mom


def _participation_score(
    sector_scores: dict[str, float | None] | None, returns: pd.Series | None
) -> float:
    """Is strength broad, or carried by a handful of names?

    A tape led by five stocks scores poorly here even at index highs. That
    divergence is the whole reason participation is measured separately from
    breadth.
    """
    # An unranked sector reports None rather than 0.0, and None is not a
    # score of zero -- it is the absence of one. Dropping those sectors from
    # both numerator and denominator is the only reading that does not
    # invent an opinion the ranker declined to give.
    ranked = [v for v in (sector_scores or {}).values() if v is not None]
    positive = sum(1 for v in ranked if v > 50.0) / len(ranked) if ranked else 0.5

    concentration = 0.5
    if returns is not None and len(returns.dropna()) >= 10:
        values = returns.dropna().sort_values(ascending=False)
        total = values.clip(lower=0).sum()
        if total > 0:
            top_decile = values.head(max(1, len(values) // 10)).clip(lower=0).sum()
            share = top_decile / total
            concentration = 1.0 - _clamp((share - 0.3) / 0.5)

    return 60.0 * positive + 40.0 * concentration


def _volatility_score(realised_vol: float | None, cfg: RegimeConfig) -> float:
    """Higher score = calmer. Absent data scores neutral rather than extreme."""
    if realised_vol is None or (isinstance(realised_vol, float) and np.isnan(realised_vol)):
        return 50.0
    return 100.0 * _clamp(
        1.0 - (realised_vol - cfg.low_vol) / max(cfg.high_vol - cfg.low_vol, 1e-9)
    )


def classify(score: float, cfg: RegimeConfig) -> MarketRegime:
    if score >= cfg.strong_bull:
        return MarketRegime.STRONG_BULL
    if score >= cfg.bull:
        return MarketRegime.BULL
    if score >= cfg.neutral:
        return MarketRegime.NEUTRAL
    if score >= cfg.bear:
        return MarketRegime.BEAR
    return MarketRegime.STRONG_BEAR


def _threshold_for(regime: MarketRegime, cfg: RegimeConfig) -> float:
    return {
        MarketRegime.STRONG_BULL: cfg.strong_bull,
        MarketRegime.BULL: cfg.bull,
        MarketRegime.NEUTRAL: cfg.neutral,
        MarketRegime.BEAR: cfg.bear,
        MarketRegime.STRONG_BEAR: 0.0,
    }[regime]


def apply_hysteresis(
    raw_regime: MarketRegime,
    score: float,
    current: MarketRegime | None,
    recent_candidates: list[MarketRegime],
    cfg: RegimeConfig,
) -> tuple[MarketRegime, MarketRegime | None]:
    """Require a decisive, sustained move before changing state.

    Returns ``(effective_regime, pending_regime)``.
    """
    if current is None or raw_regime is current:
        return raw_regime, None

    # Must clear the boundary by the band, not merely touch it.
    boundary = _threshold_for(raw_regime, cfg)
    moving_up = score > _threshold_for(current, cfg)
    decisive = (
        score >= boundary + cfg.hysteresis_band
        if moving_up
        else score <= _threshold_for(current, cfg) - cfg.hysteresis_band
    )
    if not decisive:
        return current, raw_regime

    # ...and must have held for the required number of sessions.
    held = recent_candidates[-(cfg.hysteresis_days - 1) :] if cfg.hysteresis_days > 1 else []
    if len(held) < cfg.hysteresis_days - 1 or any(r is not raw_regime for r in held):
        return current, raw_regime

    return raw_regime, None


def compute_regime(
    breadth: BreadthSnapshot,
    benchmark: pd.Series,
    as_of: date,
    cfg: RegimeConfig | None = None,
    *,
    universe_size: int | None = None,
    sector_scores: dict[str, float | None] | None = None,
    universe_returns: pd.Series | None = None,
    realised_vol: float | None = None,
    current_regime: MarketRegime | None = None,
    recent_candidates: list[MarketRegime] | None = None,
) -> RegimeResult | None:
    """Compose the five components into a regime, or refuse.

    Returns ``None`` when universe coverage is too thin to describe the market
    -- the dashboard then says "regime unavailable" rather than showing a
    number derived from a biased sample.
    """
    cfg = cfg or RegimeConfig()

    total_universe = universe_size or breadth.universe_size + breadth.excluded
    if total_universe > 0:
        coverage = breadth.universe_size / total_universe * 100.0
        if coverage < cfg.min_universe_coverage_pct:
            return None

    raw = {
        "trend": _trend_score(benchmark, cfg),
        "breadth": _breadth_score(breadth),
        "momentum": _momentum_score(breadth, benchmark),
        "participation": _participation_score(sector_scores, universe_returns),
        "volatility": _volatility_score(realised_vol, cfg),
    }
    components = tuple(
        ScoreComponent(name=name, raw=_clamp(raw[name], 0.0, 100.0), weight=weight)
        for name, weight in cfg.weights
    )
    score = sum(c.contribution for c in components)

    raw_regime = classify(score, cfg)
    effective, pending = apply_hysteresis(
        raw_regime, score, current_regime, recent_candidates or [], cfg
    )

    # Five components agreeing at ~80 is a confident bull. The same average
    # from 95/30/80/20/60 is not, and the UI should be able to say so.
    spread = statistics.pstdev([c.raw for c in components]) if len(components) > 1 else 0.0
    confidence = _clamp(100.0 - spread * 2.0, 0.0, 100.0)

    return RegimeResult(
        as_of=as_of,
        regime=effective,
        regime_score=float(score),
        components=components,
        confidence=float(confidence),
        pending_regime=pending,
        breadth=breadth,
        engine_version=REGIME_ENGINE_VERSION,
    )
