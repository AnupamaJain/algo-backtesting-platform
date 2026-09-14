"""Relative strength. Implements docs/08 Part A.

RS here is a **descriptive ranking of what has already happened**, not a
forecast. "RS 95" means "stronger than 95% of the universe on this date" and
nothing more.

Cross-sectional by nature: a stock's RS depends on every other stock's return
on the same date, so this engine takes the whole universe at once rather than
one symbol at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from ..domain.types import RsResult, RsTrend
from ..versioning import RS_ENGINE_VERSION

DEFAULT_HORIZONS = ("ret_1m", "ret_3m", "ret_6m", "ret_12m")


@dataclass(frozen=True, slots=True)
class RsConfig:
    benchmark_index: str = "NIFTY500"
    #: Front-weighted: a 12-month return is dominated by last year's news,
    #: which is not what "currently strong" should mean.
    horizon_weights: tuple[tuple[str, float], ...] = (
        ("ret_1m", 0.40), ("ret_3m", 0.30), ("ret_6m", 0.20), ("ret_12m", 0.10),
    )
    trend_lookback_days: int = 21
    trend_improving_threshold: float = 5.0
    min_history_days: int = 21

    def __post_init__(self) -> None:
        total = sum(w for _, w in self.horizon_weights)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"RS horizon weights sum to {total}, expected 1.0")

    @classmethod
    def from_config(cls, cfg) -> RsConfig:
        rs = cfg.section("rs")
        return cls(
            benchmark_index=rs.get("benchmark_index", "NIFTY500"),
            horizon_weights=tuple(rs["horizon_weights"].items()),
            trend_lookback_days=int(rs.get("trend_lookback_days", 21)),
            trend_improving_threshold=float(rs.get("trend_improving_threshold", 5.0)),
            min_history_days=int(rs.get("min_history_days", 21)),
        )


def weighted_raw(returns: dict[str, float | None], cfg: RsConfig) -> tuple[float | None, tuple[str, ...]]:
    """Blend the available horizons, renormalising the weights.

    A stock listed two months ago has no 6m or 12m return. Treating those as
    zero would rank it as flat over a period it did not exist for; dropping it
    entirely would hide every recent listing. Renormalising over what exists
    is the honest option, and ``coverage`` records what that was.
    """
    numerator = 0.0
    weight_sum = 0.0
    coverage: list[str] = []

    for horizon, weight in cfg.horizon_weights:
        value = returns.get(horizon)
        if value is None or (isinstance(value, float) and np.isnan(value)):
            continue
        numerator += weight * float(value)
        weight_sum += weight
        coverage.append(horizon)

    if weight_sum == 0.0:
        return None, ()
    return numerator / weight_sum, tuple(coverage)


def percentile_rank(values: pd.Series) -> pd.Series:
    """Percentile 0..100 within the cross-section, ties sharing the lower rank.

    Percentile rather than a z-score because the cross-sectional return
    distribution on NSE is fat-tailed: three runaway small-caps would compress
    everything else into the middle of a z-score, whereas a percentile is
    robust to that and means what users expect it to mean.
    """
    if values.empty:
        return values
    if len(values) == 1:
        return pd.Series([100.0], index=values.index)
    return values.rank(method="min", pct=True) * 100.0


def rank_universe(
    frame: pd.DataFrame,
    as_of: date,
    cfg: RsConfig | None = None,
    *,
    sectors: dict[str, str] | None = None,
    previous_scores: dict[str, float] | None = None,
) -> dict[str, RsResult]:
    """Rank every symbol in ``frame`` (indexed by symbol, return columns).

    ``previous_scores`` are the RS scores from ``trend_lookback_days`` ago,
    used for the trend classification. Absent, everything is STABLE rather
    than guessed.
    """
    cfg = cfg or RsConfig()
    if frame.empty:
        return {}

    raws: dict[str, float] = {}
    coverages: dict[str, tuple[str, ...]] = {}
    for symbol, row in frame.iterrows():
        raw, coverage = weighted_raw(row.to_dict(), cfg)
        if raw is None:
            continue  # no usable history at all; excluded rather than ranked 0
        raws[symbol] = raw
        coverages[symbol] = coverage

    if not raws:
        return {}

    raw_series = pd.Series(raws)
    scores = percentile_rank(raw_series)
    # rank 1 = strongest
    ranks = raw_series.rank(method="min", ascending=False).astype(int)

    sector_scores: dict[str, float] = {}
    if sectors:
        by_sector: dict[str, list[str]] = {}
        for symbol in raw_series.index:
            code = sectors.get(symbol)
            if code:
                by_sector.setdefault(code, []).append(symbol)
        for members in by_sector.values():
            subset = raw_series.loc[members]
            for symbol, value in percentile_rank(subset).items():
                sector_scores[symbol] = float(value)

    results: dict[str, RsResult] = {}
    for symbol in raw_series.index:
        score = float(scores[symbol])

        trend = RsTrend.STABLE
        if previous_scores and symbol in previous_scores:
            delta = score - previous_scores[symbol]
            if delta >= cfg.trend_improving_threshold:
                trend = RsTrend.IMPROVING
            elif delta <= -cfg.trend_improving_threshold:
                trend = RsTrend.DETERIORATING

        row = frame.loc[symbol]
        results[symbol] = RsResult(
            symbol=symbol,
            as_of=as_of,
            rs_raw=float(raw_series[symbol]),
            rs_score=score,
            rs_rank=int(ranks[symbol]),
            rs_vs_sector=sector_scores.get(symbol),
            rs_trend=trend,
            returns={
                h: (None if pd.isna(row.get(h)) else float(row.get(h)))
                for h in DEFAULT_HORIZONS
                if h in frame.columns
            },
            coverage=coverages[symbol],
            benchmark_index=cfg.benchmark_index,
            engine_version=RS_ENGINE_VERSION,
        )
    return results


def rs_line(close: pd.Series, benchmark_close: pd.Series, *, normalise_to: float = 100.0) -> pd.Series:
    """Price relative to a benchmark, indexed to ``normalise_to`` at the start.

    For charting. "RS line at a new high while price is not" is a readable
    fact that needs no further scoring.
    """
    aligned = benchmark_close.reindex(close.index).ffill()
    ratio = close / aligned
    first = ratio.dropna()
    if first.empty:
        return ratio
    return ratio / first.iloc[0] * normalise_to
