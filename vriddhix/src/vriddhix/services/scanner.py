"""The scanner: runs the engines over a universe and assembles setup rows.

L4. This is where state lives -- the database, the universe, the calendar --
so that L3 engines can stay pure. The scanner's job is to load the right data,
truncate it at the evaluation date, and hand it to engines that cannot see
anything else.

The truncation happens HERE, not in the engines. An engine physically cannot
look ahead because the future rows are not in the frame it receives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.types import (
    Direction,
    PatternStatus,
    SetupGrade,
    UniverseResolution,
)
from ..db.models import OhlcvDaily, Stock, TechnicalFeature
from ..engines import fvg as fvg_engine
from ..engines import regime as regime_engine
from ..engines import rs as rs_engine
from ..engines import sector as sector_engine
from ..engines import smc as smc_engine
from ..engines import vcp as vcp_engine
from ..universe.service import UniverseService
from ..versioning import VCP_ENGINE_VERSION
from .conditions import evaluate

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SetupRow:
    """One scanner result. Flat by design -- this is what a condition tree is
    evaluated against, what the API serialises, and what the backtester reads.
    Three consumers, one shape."""

    symbol: str
    as_of: date
    close: float
    sector: str | None = None

    vcp_score: float | None = None
    vcp_stage: str | None = None
    grade: str | None = None
    pivot_price: float | None = None
    distance_from_pivot_pct: float | None = None
    base_depth_pct: float | None = None
    base_start: date | None = None
    base_end: date | None = None
    contractions: int | None = None
    score_components: tuple = ()

    rs_score: float | None = None
    rs_rank: int | None = None
    rs_trend: str | None = None
    sector_rs: float | None = None
    sector_rank: int | None = None
    sector_quadrant: str | None = None

    structure: str | None = None
    bos_direction: str | None = None
    confluence_score: float | None = None
    open_fvgs: int = 0

    volume: float | None = None
    rel_volume: float | None = None
    above_ema_50: bool | None = None
    above_ema_200: bool | None = None
    market_regime: str | None = None

    engine_version: str = VCP_ENGINE_VERSION

    def as_fields(self) -> dict:
        """Flat mapping for condition evaluation.

        Built from the dataclass fields rather than ``__dict__`` -- this class
        uses ``slots=True``, so there is no instance dict to read.
        """
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "score_components"
        }


@dataclass
class ScanResult:
    as_of: date
    rows: list[SetupRow] = field(default_factory=list)
    universe: UniverseResolution | None = None
    regime: object | None = None
    sectors: dict = field(default_factory=dict)
    #: Full RsResult per symbol. SetupRow flattens RS to three fields; the
    #: relative_strength table stores returns, coverage and rs_raw as well,
    #: and recomputing those at write time would be a second implementation.
    rs: dict = field(default_factory=dict)
    #: The series the regime was actually computed against, kept so a stored
    #: regime row can be audited against its own input.
    benchmark: object | None = None
    #: symbol -> {stock_id, state, gaps, calendar}. The SMC/FVG engines ran
    #: during the scan; keeping their output lets it be persisted instead of
    #: recomputed on every read.
    structures: dict = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def survivorship_warning(self) -> str | None:
        return self.universe.warning if self.universe else None

    def top(self, n: int = 20, by: str = "vcp_score") -> list[SetupRow]:
        scored = [r for r in self.rows if getattr(r, by) is not None]
        return sorted(scored, key=lambda r: getattr(r, by), reverse=True)[:n]


def grade_for(score: float, thresholds: dict) -> SetupGrade:
    """Bucket a score. A research classification, never a recommendation."""
    if score >= thresholds.get("A_PLUS", 85):
        return SetupGrade.A_PLUS
    if score >= thresholds.get("A", 75):
        return SetupGrade.A
    if score >= thresholds.get("B_PLUS", 65):
        return SetupGrade.B_PLUS
    if score >= thresholds.get("B", 55):
        return SetupGrade.B
    return SetupGrade.C


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_prices(session: Session, symbol: str, *, as_of: date | None = None) -> pd.DataFrame:
    """Bars for one symbol, truncated at ``as_of``.

    The truncation is a SQL WHERE clause rather than a post-filter, so a bar
    after the evaluation date never enters the process at all.
    """
    stmt = (
        select(OhlcvDaily.date, OhlcvDaily.open, OhlcvDaily.high, OhlcvDaily.low,
               OhlcvDaily.close, OhlcvDaily.volume)
        .join(Stock, Stock.id == OhlcvDaily.stock_id)
        .where(Stock.symbol == symbol)
        .order_by(OhlcvDaily.date)
    )
    if as_of is not None:
        stmt = stmt.where(OhlcvDaily.date <= as_of)

    rows = session.execute(stmt).all()
    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date").astype(float)


def load_features(session: Session, symbol: str, *, as_of: date | None = None) -> pd.DataFrame:
    stmt = (
        select(TechnicalFeature)
        .join(Stock, Stock.id == TechnicalFeature.stock_id)
        .where(Stock.symbol == symbol)
        .order_by(TechnicalFeature.date)
    )
    if as_of is not None:
        stmt = stmt.where(TechnicalFeature.date <= as_of)

    records = session.scalars(stmt).all()
    if not records:
        return pd.DataFrame()

    columns = [
        "ema_20", "ema_50", "ema_100", "ema_200", "atr_14", "atr_pct",
        "avg_volume_20", "avg_volume_50", "rel_volume",
        "ret_1m", "ret_3m", "ret_6m", "ret_12m",
        "high_52w", "low_52w", "pct_from_52w_high", "pct_from_52w_low",
        "above_ema_20", "above_ema_50", "above_ema_100", "above_ema_200",
    ]
    data = {
        column: [
            None if getattr(r, column) is None else
            (bool(getattr(r, column)) if column.startswith("above_") else float(getattr(r, column)))
            for r in records
        ]
        for column in columns
    }
    frame = pd.DataFrame(data, index=pd.to_datetime([r.date for r in records]))
    return frame.rename_axis("date")


def sector_map(session: Session) -> dict[str, str]:
    from ..db.models import Industry, Sector

    rows = session.execute(
        select(Stock.symbol, Sector.code)
        .join(Industry, Industry.id == Stock.industry_id)
        .join(Sector, Sector.id == Industry.sector_id)
    ).all()
    return {symbol: code for symbol, code in rows}


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def scan(
    session: Session,
    cfg,
    as_of: date,
    *,
    index_code: str | None = None,
    symbols: list[str] | None = None,
    with_structure: bool = True,
) -> ScanResult:
    """Run every engine across the universe as of ``as_of``."""
    index_code = index_code or cfg.get("universe.default_index")
    vcp_cfg = vcp_engine.VcpConfig.from_config(cfg)
    rs_cfg = rs_engine.RsConfig.from_config(cfg)
    sector_cfg = sector_engine.SectorConfig.from_config(cfg)
    regime_cfg = regime_engine.RegimeConfig.from_config(cfg)
    smc_cfg = smc_engine.SmcConfig.from_config(cfg)
    fvg_cfg = fvg_engine.FvgConfig.from_config(cfg)
    grades = cfg.get("scoring.grades", {})

    universe: UniverseResolution | None = None
    if symbols is None:
        universe = UniverseService(
            session,
            allow_current_fallback=bool(cfg.get("universe.allow_current_universe_fallback", True)),
        ).members_as_of(index_code, as_of)
        symbols = list(universe.symbols)
        if universe.is_biased:
            logger.warning("scan running on a biased universe: %s", universe.warning)

    sectors = sector_map(session)
    stock_ids = dict(
        session.execute(select(Stock.symbol, Stock.id).where(Stock.symbol.in_(symbols))).all()
    )
    result = ScanResult(as_of=as_of, universe=universe)

    # -- pass 1: per-symbol engines --------------------------------------
    per_symbol: dict[str, dict] = {}
    for symbol in symbols:
        prices = load_prices(session, symbol, as_of=as_of)
        min_bars = int(cfg.get("data.min_history_bars", 60))
        if len(prices) < min_bars:
            result.skipped[symbol] = f"insufficient history ({len(prices)} bars)"
            continue

        features = load_features(session, symbol, as_of=as_of)
        if features.empty:
            features = None
        else:
            features = features.reindex(prices.index)

        latest = features.iloc[-1] if features is not None else pd.Series(dtype=float)
        per_symbol[symbol] = {
            "prices": prices,
            "features": features,
            "close": float(prices["close"].iloc[-1]),
            "volume": _as_float(prices["volume"].iloc[-1]) if "volume" in prices else None,
            "latest": latest,
        }

    if not per_symbol:
        return result

    # -- pass 2: cross-sectional RS (needs the whole universe) ------------
    returns = pd.DataFrame(
        {
            symbol: {
                horizon: data["latest"].get(horizon)
                for horizon in ("ret_1m", "ret_3m", "ret_6m", "ret_12m")
            }
            for symbol, data in per_symbol.items()
        }
    ).T
    rs_results = rs_engine.rank_universe(returns, as_of, rs_cfg, sectors=sectors)
    result.rs = rs_results

    # -- pass 3: sectors --------------------------------------------------
    constituent_rows = []
    for symbol, data in per_symbol.items():
        latest = data["latest"]
        constituent_rows.append({
            "symbol": symbol,
            "sector": sectors.get(symbol, "UNCLASSIFIED"),
            "ret_1m": latest.get("ret_1m"), "ret_3m": latest.get("ret_3m"),
            "ret_6m": latest.get("ret_6m"), "ret_12m": latest.get("ret_12m"),
            "above_ema_20": latest.get("above_ema_20"),
            "above_ema_50": latest.get("above_ema_50"),
            "above_ema_100": latest.get("above_ema_100"),
            "above_ema_200": latest.get("above_ema_200"),
            "is_new_high": _is_extreme(data, "high"),
            "is_new_low": _is_extreme(data, "low"),
        })
    constituents = pd.DataFrame(constituent_rows).set_index("symbol")
    sector_results = sector_engine.aggregate_sectors(constituents, as_of, sector_cfg, rs_cfg)
    result.sectors = sector_results

    # -- pass 4: regime ---------------------------------------------------
    breadth_rows = constituents.copy()
    breadth_rows["change"] = [
        _daily_change(per_symbol[s]["prices"]) for s in constituents.index
    ]
    breadth = regime_engine.compute_breadth(
        breadth_rows, as_of, universe_size=len(symbols)
    )
    benchmark = _benchmark_series(per_symbol, sectors)
    result.benchmark = benchmark
    result.regime = regime_engine.compute_regime(
        breadth, benchmark, as_of, regime_cfg,
        universe_size=len(symbols),
        sector_scores={c: s.rs_score for c, s in sector_results.items()},
        universe_returns=constituents["ret_1m"],
    )
    regime_name = result.regime.regime.value if result.regime else None

    # -- pass 5: patterns and structure -----------------------------------
    for symbol, data in per_symbol.items():
        rs = rs_results.get(symbol)
        sector_code = sectors.get(symbol)
        sector_result = sector_results.get(sector_code) if sector_code else None

        vcp_result = vcp_engine.detect(
            data["prices"], vcp_cfg,
            features=data["features"],
            rs_score=rs.rs_score if rs else None,
            sector_score=sector_result.rs_score if sector_result else None,  # None when unranked
        )

        structure = confluence = None
        gaps: list = []
        if with_structure:
            smc_state = smc_engine.analyse(data["prices"], smc_cfg)
            gaps = fvg_engine.detect_gaps(data["prices"], fvg_cfg)
            structure = smc_state
            confluence = fvg_engine.score_confluence(
                direction=Direction.BULLISH,
                price=data["close"],
                smc_state=smc_state,
                gaps=gaps,
                rs_score=rs.rs_score if rs else None,
                regime_supportive=regime_name in ("BULL", "STRONG_BULL"),
                rel_volume=data["latest"].get("rel_volume"),
                ema_aligned=bool(data["latest"].get("above_ema_50")),
                as_of=as_of,
            )

        if with_structure:
            result.structures[symbol] = {
                "stock_id": stock_ids.get(symbol),
                "state": structure,
                "gaps": gaps,
                "calendar": data["prices"].index,
            }

        latest_break = structure.latest_break() if structure else None
        result.rows.append(
            SetupRow(
                symbol=symbol,
                as_of=as_of,
                close=data["close"],
                sector=sector_code,
                vcp_score=vcp_result.score.total if vcp_result else None,
                vcp_stage=vcp_result.stage.value if vcp_result else None,
                grade=grade_for(vcp_result.score.total, grades).value if vcp_result else None,
                pivot_price=vcp_result.base.pivot_price if vcp_result else None,
                distance_from_pivot_pct=(
                    vcp_result.distance_from_pivot_pct if vcp_result else None
                ),
                base_depth_pct=vcp_result.base.depth_pct if vcp_result else None,
                base_start=vcp_result.base.start if vcp_result else None,
                base_end=vcp_result.base.end if vcp_result else None,
                contractions=len(vcp_result.base.contractions) if vcp_result else None,
                score_components=vcp_result.score.components if vcp_result else (),
                rs_score=rs.rs_score if rs else None,
                rs_rank=rs.rs_rank if rs else None,
                rs_trend=rs.rs_trend.value if rs else None,
                sector_rs=sector_result.rs_score if sector_result else None,
                sector_rank=sector_result.rs_rank if sector_result else None,
                sector_quadrant=(
                    sector_result.quadrant.value
                    if sector_result and sector_result.quadrant else None
                ),
                structure=structure.structure if structure else None,
                bos_direction=latest_break.direction.value if latest_break else None,
                confluence_score=confluence.score if confluence else None,
                open_fvgs=len(fvg_engine.open_gaps(gaps, Direction.BULLISH)),
                volume=data.get("volume"),
                rel_volume=_as_float(data["latest"].get("rel_volume")),
                above_ema_50=_as_bool(data["latest"].get("above_ema_50")),
                above_ema_200=_as_bool(data["latest"].get("above_ema_200")),
                market_regime=regime_name,
            )
        )

    # -- pass 6: sector pattern statistics --------------------------------
    # Deliberately after pass 5 rather than folded into pass 3: breakout
    # counts and average pattern score are downstream of detection, and
    # aggregating them earlier would have persisted a structural zero.
    result.sectors = _with_pattern_stats(sector_results, result.rows)

    return result


def _with_pattern_stats(sectors: dict, rows: list[SetupRow]) -> dict:
    """Add per-sector pattern counts to already-ranked sector results.

    RS, quadrant and breadth are untouched -- those came from
    ``aggregate_sectors`` and this must not become a second place that
    computes them.
    """
    counts: dict[str, dict] = {}
    for row in rows:
        if row.sector is None:
            continue
        bucket = counts.setdefault(row.sector, {"breakouts": 0, "scores": []})
        if row.vcp_stage == PatternStatus.BREAKOUT.value:
            bucket["breakouts"] += 1
        if row.vcp_score is not None:
            bucket["scores"].append(row.vcp_score)

    enriched = {}
    for code, result in sectors.items():
        bucket = counts.get(code)
        if bucket is None:
            enriched[code] = result
            continue
        scores = bucket["scores"]
        enriched[code] = replace(
            result,
            breakouts=bucket["breakouts"],
            avg_pattern_score=float(sum(scores) / len(scores)) if scores else None,
        )
    return enriched


def run_screen(result: ScanResult, tree: dict) -> list[SetupRow]:
    """Filter scan rows through a condition tree -- the same evaluator the
    backtester uses for entry rules."""
    return [row for row in result.rows if evaluate(tree, row.as_fields()).passed]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_float(value) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def _as_bool(value) -> bool | None:
    return None if value is None or pd.isna(value) else bool(value)


def _daily_change(prices: pd.DataFrame) -> float:
    if len(prices) < 2:
        return 0.0
    return float(prices["close"].iloc[-1] - prices["close"].iloc[-2])


def _is_extreme(data: dict, kind: str) -> bool:
    latest = data["latest"]
    column = "high_52w" if kind == "high" else "low_52w"
    reference = latest.get(column)
    if reference is None or pd.isna(reference):
        return False
    price = data["prices"][kind].iloc[-1]
    return bool(price >= reference) if kind == "high" else bool(price <= reference)


def _benchmark_series(per_symbol: dict, sectors: dict) -> pd.Series:
    """An equal-weighted stand-in for the index.

    A real deployment feeds the actual index series here. Averaging the
    universe is a documented approximation, not a silent one: it is what
    'benchmark' means when no index bars have been ingested.
    """
    frames = [d["features"] for d in per_symbol.values() if d["features"] is not None]
    if not frames:
        return pd.Series(dtype=float)

    latest = [f.iloc[-1] for f in frames]
    combined = pd.DataFrame(latest)
    closes = [d["close"] for d in per_symbol.values()]

    return pd.Series({
        "close": float(pd.Series(closes).mean()),
        "ema_20": float(combined["ema_20"].mean(skipna=True)),
        "ema_50": float(combined["ema_50"].mean(skipna=True)),
        "ema_200": float(combined["ema_200"].mean(skipna=True)),
        "ema_50_slope_20": 0.0,
        "ret_3m": float(combined["ret_3m"].mean(skipna=True)),
    })
