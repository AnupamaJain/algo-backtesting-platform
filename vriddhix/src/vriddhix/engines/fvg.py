"""Fair Value Gaps and the confluence score. Implements docs/06 §7-8.

An FVG is a three-candle imbalance: the middle candle moved far enough that
candle 1 and candle 3 do not overlap, leaving a price band that traded through
without two-sided participation.

The arithmetic is trivial. The care goes into mitigation tracking, where
``mitigation_pct`` is a running MAXIMUM: a gap tagged 60% and left does not
revert to 0% when price walks away. The deepest touch is the fact worth
keeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from ..domain.types import Direction, FvgStatus, ScoreComponent
from ..versioning import CONFLUENCE_RULE_VERSION, FVG_ENGINE_VERSION


@dataclass(frozen=True, slots=True)
class FvgConfig:
    #: Below this, a "gap" is tick noise -- on an illiquid NSE small cap they
    #: occur constantly and mean nothing.
    min_gap_pct: float = 0.1
    mitigation_threshold_pct: float = 50.0
    invalidation_buffer_pct: float = 0.5

    @classmethod
    def from_config(cls, cfg) -> FvgConfig:
        fvg = cfg.section("fvg")
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in fvg.items() if k in known})


@dataclass(frozen=True, slots=True)
class FairValueGap:
    created_at: date
    direction: Direction
    upper: float
    lower: float
    size_pct: float
    status: FvgStatus
    mitigation_pct: float
    mitigated_at: date | None
    timeframe: str = "1d"
    engine_version: str = FVG_ENGINE_VERSION

    @property
    def midpoint(self) -> float:
        return (self.upper + self.lower) / 2.0

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper


def detect_gaps(
    df: pd.DataFrame,
    cfg: FvgConfig | None = None,
    *,
    as_of: date | None = None,
    timeframe: str = "1d",
) -> list[FairValueGap]:
    """Find every FVG and track it forward to its current state."""
    cfg = cfg or FvgConfig()
    if as_of is not None:
        df = df.loc[: pd.Timestamp(as_of)]
    if len(df) < 3:
        return []

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    index = df.index

    gaps: list[FairValueGap] = []
    for i in range(2, len(df)):
        if lows[i] > highs[i - 2]:
            direction, upper, lower = Direction.BULLISH, lows[i], highs[i - 2]
        elif highs[i] < lows[i - 2]:
            direction, upper, lower = Direction.BEARISH, lows[i - 2], highs[i]
        else:
            continue

        if lower <= 0:
            continue
        size_pct = (upper - lower) / lower * 100.0
        if size_pct < cfg.min_gap_pct:
            continue

        status, mitigation_pct, mitigated_at = _track(
            highs, lows, closes, index, i, upper, lower, direction, cfg
        )
        gaps.append(
            FairValueGap(
                created_at=index[i].date(),
                direction=direction,
                upper=float(upper),
                lower=float(lower),
                size_pct=float(size_pct),
                status=status,
                mitigation_pct=float(mitigation_pct),
                mitigated_at=mitigated_at,
                timeframe=timeframe,
            )
        )
    return gaps


def _track(highs, lows, closes, index, created, upper, lower, direction, cfg):
    """Follow a gap forward from creation and classify its current state.

    Vectorised over the bars after creation. Every gap is tracked to the end
    of the series, so a bar-by-bar loop makes detection quadratic in the
    length of the history -- on a decade of daily bars that is the single
    largest cost in a scan. The semantics are unchanged: mitigation is still
    a RUNNING MAXIMUM (a gap tagged 60% and left does not revert), and
    invalidation still stops the walk at the first decisive close through the
    far side.
    """
    span = upper - lower
    start = created + 1
    if start >= len(closes):
        return FvgStatus.OPEN, 0.0, None

    future_closes = closes[start:]
    future_highs = highs[start:]
    future_lows = lows[start:]

    buffer = cfg.invalidation_buffer_pct / 100.0
    if direction is Direction.BULLISH:
        breached = future_closes < lower * (1 - buffer)
    else:
        breached = future_closes > upper * (1 + buffer)

    hits = np.flatnonzero(breached)
    # Only bars BEFORE the invalidating close count toward mitigation: the
    # loop returned at that bar without measuring its overlap.
    stop = int(hits[0]) if hits.size else len(future_closes)

    if span > 0 and stop > 0:
        overlap = np.minimum(future_highs[:stop], upper) - np.maximum(
            future_lows[:stop], lower
        )
        np.maximum(overlap, 0.0, out=overlap)
        pct = overlap / span * 100.0
        running = np.maximum.accumulate(pct)
        deepest = float(running[-1]) if running.size else 0.0
    else:
        running = np.empty(0)
        deepest = 0.0

    if hits.size:
        return FvgStatus.INVALIDATED, max(deepest, 0.0), index[start + stop].date()

    mitigated_at = None
    if deepest >= cfg.mitigation_threshold_pct:
        # The first bar at which the running maximum crossed the threshold.
        first = int(np.argmax(running >= cfg.mitigation_threshold_pct))
        mitigated_at = index[start + first].date()
        return FvgStatus.MITIGATED, deepest, mitigated_at
    if deepest >= 10.0:
        return FvgStatus.PARTIALLY_FILLED, deepest, None
    return FvgStatus.OPEN, deepest, None


def open_gaps(gaps: list[FairValueGap], direction: Direction | None = None) -> list[FairValueGap]:
    return [
        g for g in gaps
        if g.status in (FvgStatus.OPEN, FvgStatus.PARTIALLY_FILLED)
        and (direction is None or g.direction is direction)
    ]


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------


DEFAULT_CONFLUENCE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("bos_aligned", 0.20), ("choch_recent", 0.10), ("fvg_present", 0.15),
    ("order_block", 0.15), ("liquidity_sweep", 0.10), ("vwap_alignment", 0.05),
    ("ema_alignment", 0.05), ("volume", 0.05), ("rs_supportive", 0.10),
    ("regime_supportive", 0.05),
)


@dataclass(frozen=True, slots=True)
class ConfluenceResult:
    score: float
    components: tuple[ScoreComponent, ...]
    conditions_met: int
    conditions_total: int
    counter_trend: bool
    rule_version: str = CONFLUENCE_RULE_VERSION

    def describe(self) -> str:
        """Rendered as a count of conditions, never as a likelihood."""
        return f"{self.score:.0f} ({self.conditions_met} of {self.conditions_total} conditions)"


def _proximity(price: float, lower: float, upper: float, tolerance_pct: float = 1.0) -> float:
    """1.0 inside the zone, tapering to 0 at ``tolerance_pct`` away.

    Graded rather than binary so that "price sitting in the gap" outranks
    "a gap exists 6% away", which a 0/1 test would score identically.
    """
    if lower <= price <= upper:
        return 1.0
    distance = (lower - price) if price < lower else (price - upper)
    band = price * tolerance_pct / 100.0
    return max(0.0, 1.0 - distance / band) if band > 0 else 0.0


def score_confluence(
    *,
    direction: Direction,
    price: float,
    smc_state=None,
    gaps: list[FairValueGap] | None = None,
    rs_score: float | None = None,
    regime_supportive: bool | None = None,
    rel_volume: float | None = None,
    ema_aligned: bool | None = None,
    vwap_aligned: bool | None = None,
    weights: tuple[tuple[str, float], ...] = DEFAULT_CONFLUENCE_WEIGHTS,
    bos_lookback_bars: int = 20,
    choch_lookback_bars: int = 40,
    sweep_lookback_bars: int = 10,
    as_of: date | None = None,
) -> ConfluenceResult:
    """Weighted count of the conditions that are met.

    This is NOT a probability. The UI must render it as
    "Confluence: 87 (8 of 10 conditions)" and never as a likelihood that the
    trade works -- nothing here has been calibrated against outcomes.
    """
    from ..domain.types import StructureEvent

    raw: dict[str, float] = {name: 0.0 for name, _ in weights}
    counter_trend = False

    if smc_state is not None and as_of is not None:
        def bars_since(when: date) -> int:
            return (as_of - when).days

        aligned = smc_state.latest_break(direction)
        if aligned is not None and bars_since(aligned.date) <= bos_lookback_bars * 1.5:
            raw["bos_aligned"] = 100.0
        if aligned is not None and aligned.kind is StructureEvent.CHOCH:
            if bars_since(aligned.date) <= choch_lookback_bars * 1.5:
                raw["choch_recent"] = 100.0

        for block in smc_state.order_blocks:
            if block.direction is direction and block.status == "OPEN":
                raw["order_block"] = max(
                    raw["order_block"], 100.0 * _proximity(price, block.lower, block.upper)
                )

        for event in smc_state.liquidity:
            if (
                event.kind == "SWEEP"
                and event.direction is direction
                and bars_since(event.date) <= sweep_lookback_bars * 1.5
            ):
                raw["liquidity_sweep"] = 100.0

        expected = "BULLISH" if direction is Direction.BULLISH else "BEARISH"
        if smc_state.structure not in (expected, "RANGING"):
            counter_trend = True

    for gap in open_gaps(gaps or [], direction):
        raw["fvg_present"] = max(
            raw["fvg_present"], 100.0 * _proximity(price, gap.lower, gap.upper)
        )

    if rs_score is not None and rs_score >= 70.0:
        raw["rs_supportive"] = 100.0
    if regime_supportive:
        raw["regime_supportive"] = 100.0
    if rel_volume is not None and rel_volume >= 1.2:
        raw["volume"] = 100.0
    if ema_aligned:
        raw["ema_alignment"] = 100.0
    if vwap_aligned:
        raw["vwap_alignment"] = 100.0

    if counter_trend:
        # Recorded, not hidden: counter-trend setups are exactly the
        # population worth studying separately.
        raw["bos_aligned"] = 0.0
        raw["regime_supportive"] = 0.0

    components = tuple(
        ScoreComponent(name=name, raw=raw[name], weight=weight) for name, weight in weights
    )
    return ConfluenceResult(
        score=sum(c.contribution for c in components),
        components=components,
        conditions_met=sum(1 for c in components if c.raw > 0),
        conditions_total=len(components),
        counter_trend=counter_trend,
    )
