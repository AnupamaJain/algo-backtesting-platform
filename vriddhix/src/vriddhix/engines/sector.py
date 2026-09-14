"""Sector aggregation and rotation quadrants. Implements docs/08 Part B.

The quadrant states where a sector sits today on two measured axes. It does
**not** predict rotation into the next quadrant -- the tempting clockwise
arrow is not drawn, because the data does not support it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from ..domain.types import SectorQuadrant, SectorResult
from ..versioning import SECTOR_ENGINE_VERSION
from .rs import RsConfig, percentile_rank, weighted_raw


@dataclass(frozen=True, slots=True)
class SectorConfig:
    #: Equal weight, not cap weight. Cap weighting makes every sector a proxy
    #: for its two largest names, which is the opposite of the participation
    #: question sectors are being asked here.
    aggregation: str = "equal_weight"
    min_constituents: int = 5
    quadrant_rs_midpoint: float = 50.0

    @classmethod
    def from_config(cls, cfg) -> SectorConfig:
        sector = cfg.section("sector")
        return cls(
            aggregation=sector.get("aggregation", "equal_weight"),
            min_constituents=int(sector.get("min_constituents", 5)),
            quadrant_rs_midpoint=float(sector.get("quadrant_rs_midpoint", 50.0)),
        )


def classify_quadrant(rs_score: float, momentum: float, midpoint: float = 50.0) -> SectorQuadrant:
    """Two axes: where the sector is (RS), and where it is going (momentum).

    The midpoint is the median sector rather than an absolute return, so the
    four quadrants are always populated and describe *relative* standing.
    """
    strong = rs_score >= midpoint
    rising = momentum >= 0.0

    if strong and rising:
        return SectorQuadrant.LEADING
    if strong and not rising:
        return SectorQuadrant.WEAKENING
    if not strong and rising:
        return SectorQuadrant.IMPROVING
    return SectorQuadrant.LAGGING


def aggregate_sectors(
    constituents: pd.DataFrame,
    as_of: date,
    cfg: SectorConfig | None = None,
    rs_cfg: RsConfig | None = None,
    *,
    previous_scores: dict[str, float] | None = None,
    pattern_stats: dict[str, dict] | None = None,
) -> dict[str, SectorResult]:
    """Aggregate per-stock rows into per-sector metrics.

    ``constituents`` is indexed by symbol and must carry ``sector`` plus the
    return columns and boolean ``above_ema_*`` / ``is_new_high`` /
    ``is_new_low`` flags produced by the feature layer.
    """
    cfg = cfg or SectorConfig()
    rs_cfg = rs_cfg or RsConfig()
    if constituents.empty or "sector" not in constituents.columns:
        return {}

    pattern_stats = pattern_stats or {}
    rows: dict[str, dict] = {}

    for code, group in constituents.groupby("sector"):
        returns = {
            horizon: (float(group[horizon].mean()) if horizon in group.columns else None)
            for horizon in ("ret_1m", "ret_3m", "ret_6m", "ret_12m")
        }
        returns = {k: (None if v is None or np.isnan(v) else v) for k, v in returns.items()}
        raw, _ = weighted_raw(returns, rs_cfg)

        def pct_above(column: str) -> float:
            if column not in group.columns:
                return 0.0
            values = group[column].dropna()
            return float(values.mean() * 100.0) if len(values) else 0.0

        stats = pattern_stats.get(code, {})
        rows[code] = {
            "returns": returns,
            "raw": raw,
            "count": len(group),
            "pct_above_ema_20": pct_above("above_ema_20"),
            "pct_above_ema_50": pct_above("above_ema_50"),
            "pct_above_ema_200": pct_above("above_ema_200"),
            "new_highs": int(group.get("is_new_high", pd.Series(dtype=bool)).sum()),
            "new_lows": int(group.get("is_new_low", pd.Series(dtype=bool)).sum()),
            "breakouts": int(stats.get("breakouts", 0)),
            "failed_breakouts": int(stats.get("failed_breakouts", 0)),
            "avg_pattern_score": stats.get("avg_pattern_score"),
        }

    # Sectors below the minimum are computed but excluded from ranking: a rank
    # derived from two stocks is a single-stock opinion wearing a sector name.
    rankable = {
        code: row["raw"]
        for code, row in rows.items()
        if row["raw"] is not None and row["count"] >= cfg.min_constituents
    }

    scores: dict[str, float] = {}
    ranks: dict[str, int] = {}
    if rankable:
        series = pd.Series(rankable)
        scores = percentile_rank(series).to_dict()
        ranks = series.rank(method="min", ascending=False).astype(int).to_dict()

    results: dict[str, SectorResult] = {}
    for code, row in rows.items():
        low_sample = row["count"] < cfg.min_constituents
        # None rather than 0.0: an unranked sector has no standing, which is
        # not the same as standing last.
        score = float(scores[code]) if code in scores else None
        momentum = (
            score - previous_scores[code]
            if score is not None and previous_scores and code in previous_scores
            else 0.0
        )

        results[code] = SectorResult(
            sector_code=code,
            as_of=as_of,
            returns=row["returns"],
            rs_score=score,
            rs_rank=int(ranks[code]) if code in ranks else None,
            momentum=float(momentum),
            # None, not LAGGING: a quadrant is a position among ranked
            # peers, and an unranked sector does not occupy one.
            quadrant=(
                classify_quadrant(score, momentum, cfg.quadrant_rs_midpoint)
                if score is not None
                else None
            ),
            pct_above_ema_20=row["pct_above_ema_20"],
            pct_above_ema_50=row["pct_above_ema_50"],
            pct_above_ema_200=row["pct_above_ema_200"],
            new_highs=row["new_highs"],
            new_lows=row["new_lows"],
            breakouts=row["breakouts"],
            failed_breakouts=row["failed_breakouts"],
            avg_pattern_score=row["avg_pattern_score"],
            constituent_count=row["count"],
            low_sample=low_sample,
            engine_version=SECTOR_ENGINE_VERSION,
        )
    return results
