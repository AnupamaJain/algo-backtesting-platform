"""The shared vocabulary. Stdlib only — no pandas, no ORM, no I/O.

Engines speak in these types rather than in ORM rows for a reason that is
easy to lose sight of: an ORM row can lazily load a relationship mid
calculation and reach data outside the evaluation window, which is
look-ahead by another name. A frozen dataclass cannot.

See docs/04-data-model.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"


class Timeframe(str, Enum):
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


class PatternType(str, Enum):
    VCP = "VCP"
    BASE = "BASE"
    FLAT_BASE = "FLAT_BASE"
    CUP_HANDLE = "CUP_HANDLE"
    DOUBLE_BOTTOM = "DOUBLE_BOTTOM"


class PatternStatus(str, Enum):
    """Lifecycle states. Transitions are constrained -- see ALLOWED_TRANSITIONS."""

    FORMING = "FORMING"
    NEAR_PIVOT = "NEAR_PIVOT"
    BREAKOUT = "BREAKOUT"
    CONFIRMED = "CONFIRMED"
    EXTENDED = "EXTENDED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


#: The state machine from docs/04-data-model.md §6. A pattern may not skip a
#: state: FORMING -> CONFIRMED would mean a breakout was never recorded, and
#: the breakout row is what research is actually built on.
ALLOWED_TRANSITIONS: dict[PatternStatus, frozenset[PatternStatus]] = {
    PatternStatus.FORMING: frozenset(
        {PatternStatus.NEAR_PIVOT, PatternStatus.BREAKOUT, PatternStatus.FAILED}
    ),
    PatternStatus.NEAR_PIVOT: frozenset(
        {PatternStatus.BREAKOUT, PatternStatus.FORMING, PatternStatus.FAILED}
    ),
    PatternStatus.BREAKOUT: frozenset(
        {PatternStatus.CONFIRMED, PatternStatus.FAILED}
    ),
    PatternStatus.CONFIRMED: frozenset(
        {PatternStatus.EXTENDED, PatternStatus.COMPLETED, PatternStatus.FAILED}
    ),
    PatternStatus.EXTENDED: frozenset(
        {PatternStatus.COMPLETED, PatternStatus.FAILED}
    ),
    # Terminal. A symbol that sets up again gets a new pattern row rather than
    # a resurrected one, so that each attempt is independently countable.
    PatternStatus.FAILED: frozenset(),
    PatternStatus.COMPLETED: frozenset(),
}


def can_transition(src: PatternStatus, dst: PatternStatus) -> bool:
    return dst in ALLOWED_TRANSITIONS[src]


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class SwingType(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


class StructureEvent(str, Enum):
    BOS = "BOS"
    CHOCH = "CHOCH"


class FvgStatus(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    MITIGATED = "MITIGATED"
    INVALIDATED = "INVALIDATED"


class MarketRegime(str, Enum):
    STRONG_BULL = "STRONG_BULL"
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"
    STRONG_BEAR = "STRONG_BEAR"


class SectorQuadrant(str, Enum):
    LEADING = "LEADING"
    IMPROVING = "IMPROVING"
    WEAKENING = "WEAKENING"
    LAGGING = "LAGGING"


class RsTrend(str, Enum):
    IMPROVING = "IMPROVING"
    STABLE = "STABLE"
    DETERIORATING = "DETERIORATING"


class SetupGrade(str, Enum):
    """A bucketing of the composite score. A research classification, not a
    recommendation -- the UI is required to label it as such."""

    A_PLUS = "A_PLUS"
    A = "A"
    B_PLUS = "B_PLUS"
    B = "B"
    C = "C"


class DataIssue(str, Enum):
    MISSING_BAR = "MISSING_BAR"
    DUPLICATE = "DUPLICATE"
    INVALID_OHLC = "INVALID_OHLC"
    ZERO_VOLUME = "ZERO_VOLUME"
    NEGATIVE_PRICE = "NEGATIVE_PRICE"
    SUSPECTED_SPLIT = "SUSPECTED_SPLIT"
    STALE = "STALE"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class SurvivorshipMode(str, Enum):
    """How a universe was resolved.

    POINT_IN_TIME means real membership history was available. CURRENT_UNIVERSE
    means it was not, and today's list was substituted -- which biases results
    upward, because today's index members are the companies that survived.
    Callers must propagate this into anything they store.
    """

    POINT_IN_TIME = "POINT_IN_TIME"
    CURRENT_UNIVERSE = "CURRENT_UNIVERSE"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError(
                f"impossible bar on {self.date}: "
                f"O={self.open} H={self.high} L={self.low} C={self.close}"
            )


@dataclass(frozen=True, slots=True)
class SwingPoint:
    date: date
    price: float
    type: SwingType
    strength: int


@dataclass(frozen=True, slots=True)
class Contraction:
    sequence: int
    start: date
    end: date
    high: float
    low: float
    depth_pct: float
    duration_days: int
    avg_volume: float
    atr_avg: float


@dataclass(frozen=True, slots=True)
class PriorTrend:
    start: date
    end: date
    advance_pct: float
    duration_days: int
    ema_aligned: bool


@dataclass(frozen=True, slots=True)
class VcpBase:
    start: date
    end: date
    base_high: float
    base_low: float
    depth_pct: float
    duration_days: int
    contractions: tuple[Contraction, ...]
    pivot_price: float
    pivot_date: date
    pivot_type: str


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    """One weighted term of a composite score.

    Carried through to the API so the UI can show the breakdown. A score the
    reader cannot decompose is a number they are asked to take on faith.
    """

    name: str
    raw: float  # 0..100 before weighting
    weight: float

    @property
    def contribution(self) -> float:
        return self.raw * self.weight


@dataclass(frozen=True, slots=True)
class CompositeScore:
    total: float
    components: tuple[ScoreComponent, ...]
    rule_version: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.total <= 100.0:
            raise ValueError(f"composite score out of range: {self.total}")

    def component(self, name: str) -> ScoreComponent | None:
        return next((c for c in self.components if c.name == name), None)


@dataclass(frozen=True, slots=True)
class UniverseResolution:
    """The answer to 'who was in this index on this date', plus how confident
    we are that it is the real answer."""

    index_code: str
    as_of: date
    symbols: tuple[str, ...]
    mode: SurvivorshipMode
    snapshot_version: str | None = None
    warning: str | None = None

    @property
    def is_biased(self) -> bool:
        return self.mode is SurvivorshipMode.CURRENT_UNIVERSE


@dataclass(frozen=True, slots=True)
class QualityFinding:
    """One thing wrong with incoming data.

    Findings are recorded rather than raised wherever the bar is still usable,
    because a market-wide provider hiccup should degrade one symbol, not abort
    a nightly scan.
    """

    issue: DataIssue
    severity: Severity
    symbol: str
    bar_date: date | None = None
    detail: dict = field(default_factory=dict)

    @property
    def is_fatal(self) -> bool:
        return self.severity is Severity.ERROR


class InvalidPriceFrame(ValueError):
    """Raised when a frame violates the price-frame contract.

    Deliberately an exception rather than a repair: a silently corrected frame
    produces plausible numbers that are wrong, which is the failure mode this
    whole system is built to avoid.
    """


# ---------------------------------------------------------------------------
# Market context (Phase 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RsResult:
    symbol: str
    as_of: date
    rs_raw: float
    rs_score: float          # percentile 0..100
    rs_rank: int             # 1 = strongest
    rs_vs_sector: float | None
    rs_trend: RsTrend
    returns: dict            # horizon -> return, None where unavailable
    #: Which horizons had enough history. A two-month-old listing ranking on
    #: ret_1m alone must be identifiable as such.
    coverage: tuple[str, ...]
    benchmark_index: str
    engine_version: str


@dataclass(frozen=True, slots=True)
class SectorResult:
    sector_code: str
    as_of: date
    returns: dict
    #: None when the sector was not ranked (too few constituents). Explicitly
    #: not 0.0 -- "unranked" and "ranked worst" are different statements, and
    #: a 0.0 here would read as the latter.
    rs_score: float | None
    rs_rank: int | None
    momentum: float
    #: None when the sector was not ranked. A quadrant is a position among
    #: ranked peers, so an unranked sector does not occupy one -- reporting
    #: LAGGING would be an opinion the ranker declined to give.
    quadrant: SectorQuadrant | None
    pct_above_ema_20: float
    pct_above_ema_50: float
    pct_above_ema_200: float
    new_highs: int
    new_lows: int
    breakouts: int
    failed_breakouts: int
    avg_pattern_score: float | None
    constituent_count: int
    #: Too few constituents for a rank to mean anything -- a "sector" of two
    #: stocks produces a single-stock opinion wearing a sector's name.
    low_sample: bool
    engine_version: str


@dataclass(frozen=True, slots=True)
class BreadthSnapshot:
    as_of: date
    advancers: int
    decliners: int
    unchanged: int
    ad_ratio: float
    #: None where the input carried no such column at all -- "not measured"
    #: rather than "nobody is above it". A stored 0.0 for an EMA nobody
    #: computed is a number a reader cannot tell apart from a real reading.
    pct_above_ema_20: float | None
    pct_above_ema_50: float | None
    pct_above_ema_100: float | None
    pct_above_ema_200: float | None
    new_highs_52w: int
    new_lows_52w: int
    universe_size: int
    #: Symbols with no bar for this date. Excluded from both numerator and
    #: denominator -- counting a missing bar as "not above its EMA" would
    #: manufacture bearishness on a data-outage day.
    excluded: int = 0

    @property
    def net_new_highs(self) -> int:
        return self.new_highs_52w - self.new_lows_52w


@dataclass(frozen=True, slots=True)
class RegimeResult:
    as_of: date
    regime: MarketRegime
    regime_score: float
    components: tuple[ScoreComponent, ...]
    confidence: float
    #: Set when a threshold has been crossed but hysteresis has not yet
    #: allowed the change. Prevents a regime filter flickering daily.
    pending_regime: MarketRegime | None
    breadth: BreadthSnapshot
    engine_version: str
