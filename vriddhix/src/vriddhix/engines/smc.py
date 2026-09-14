"""Smart Money Concepts. Implements docs/06 §1-6.

SMC is normally taught as a set of visual judgements, which is useless for a
system that has to backtest. Every structure here has an arithmetic definition
that produces the same answer every time it runs.

The distinction that requires real care is BOS versus CHoCH: the *same* price
break is one or the other depending entirely on the structure that preceded
it. That means structure has to be carried as state through the walk, not
recomputed per bar from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from ..domain.types import Direction, StructureEvent, SwingPoint, SwingType
from ..versioning import SMC_ENGINE_VERSION
from .swings import find_swings

RANGING = "RANGING"


@dataclass(frozen=True, slots=True)
class SmcConfig:
    swing_left_bars: int = 3
    swing_right_bars: int = 3
    min_swing_move_pct: float = 0.0
    #: Close-based by default. Wick breaks generate roughly 3x the events and
    #: most are noise.
    break_on: str = "close"
    ob_lookback_bars: int = 10
    ob_use_body: bool = False
    equal_level_tolerance_pct: float = 0.1
    sweep_bos_window: int = 5

    @classmethod
    def from_config(cls, cfg) -> SmcConfig:
        smc = cfg.section("smc")
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in smc.items() if k in known})


@dataclass(frozen=True, slots=True)
class StructureBreak:
    date: date
    kind: StructureEvent          # BOS | CHOCH
    direction: Direction
    broken_level: float
    broken_swing_date: date
    close_price: float
    volume: float
    prior_structure: str


@dataclass(frozen=True, slots=True)
class OrderBlock:
    date: date
    direction: Direction
    upper: float
    lower: float
    origin_break: date
    status: str = "OPEN"          # OPEN | MITIGATED | INVALIDATED
    mitigated_at: date | None = None

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper


@dataclass(frozen=True, slots=True)
class LiquidityEvent:
    date: date
    kind: str                     # SWEEP | EQH | EQL
    level: float
    direction: Direction
    reclaimed: bool = False
    linked_break: date | None = None


@dataclass(frozen=True, slots=True)
class SmcState:
    as_of: date
    structure: str
    breaks: tuple[StructureBreak, ...]
    order_blocks: tuple[OrderBlock, ...]
    liquidity: tuple[LiquidityEvent, ...]
    last_confirmed_high: SwingPoint | None
    last_confirmed_low: SwingPoint | None
    engine_version: str = SMC_ENGINE_VERSION

    def latest_break(self, direction: Direction | None = None) -> StructureBreak | None:
        for event in reversed(self.breaks):
            if direction is None or event.direction is direction:
                return event
        return None


def _break_level(row: pd.Series, cfg: SmcConfig, direction: Direction) -> float:
    if cfg.break_on == "close":
        return float(row["close"])
    return float(row["high"] if direction is Direction.BULLISH else row["low"])


def _break_level_at(
    i: int, closes, highs, lows, cfg: SmcConfig, direction: Direction
) -> float:
    """``_break_level`` against columns pulled once, for the main loop.

    Identical arithmetic; it exists only so the per-bar walk does not build a
    pandas Series for every one of several thousand bars.
    """
    if cfg.break_on == "close":
        return float(closes[i])
    return float(highs[i] if direction is Direction.BULLISH else lows[i])


def _find_order_block(
    df: pd.DataFrame, break_pos: int, direction: Direction, cfg: SmcConfig,
    *, columns: dict | None = None,
) -> OrderBlock | None:
    """The last opposing candle before the impulse that caused the break.

    ``columns`` lets a caller pass arrays it already holds. Without it this
    converted every column of the whole frame on each call, making order
    block discovery quadratic in the number of breaks.
    """
    start = max(0, break_pos - cfg.ob_lookback_bars)
    if columns is not None:
        opens, closes = columns["open"], columns["close"]
    else:
        opens = df["open"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)

    for k in range(break_pos - 1, start - 1, -1):
        if direction is Direction.BULLISH:
            if closes[k] >= opens[k]:
                continue  # not a down candle
            # The move from k to the break must not have closed back through it.
            if (closes[k + 1 : break_pos + 1] < lows[k]).any():
                continue
        else:
            if closes[k] <= opens[k]:
                continue
            if (closes[k + 1 : break_pos + 1] > highs[k]).any():
                continue

        if cfg.ob_use_body:
            upper = max(opens[k], closes[k])
            lower = min(opens[k], closes[k])
        else:
            upper, lower = highs[k], lows[k]

        return OrderBlock(
            date=df.index[k].date(),
            direction=direction,
            upper=float(upper),
            lower=float(lower),
            origin_break=df.index[break_pos].date(),
        )
    return None


def _update_order_blocks(df: pd.DataFrame, blocks: list[OrderBlock]) -> list[OrderBlock]:
    """Mark blocks mitigated once price trades back into them.

    Vectorised over numpy views taken once, rather than slicing the frame per
    block: every block is tested against the whole remaining series, so the
    per-block DataFrame slice made this quadratic in the length of history.
    """
    positions = {d.date(): i for i, d in enumerate(df.index)}
    lows = df["low"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    index = df.index
    updated: list[OrderBlock] = []

    for block in blocks:
        start = positions.get(block.origin_break)
        if start is None:
            updated.append(block)
            continue

        begin = start + 1
        touched = np.flatnonzero(
            (lows[begin:] <= block.upper) & (highs[begin:] >= block.lower)
        )
        if touched.size:
            updated.append(
                OrderBlock(
                    date=block.date, direction=block.direction,
                    upper=block.upper, lower=block.lower,
                    origin_break=block.origin_break,
                    status="MITIGATED",
                    mitigated_at=index[begin + int(touched[0])].date(),
                )
            )
        else:
            updated.append(block)
    return updated


def _equal_levels(swings: list[SwingPoint], cfg: SmcConfig) -> list[LiquidityEvent]:
    """Two or more swings of the same type at effectively the same price.

    Resting stops cluster above equal highs and below equal lows, which is
    what makes them worth marking at all.
    """
    events: list[LiquidityEvent] = []
    for kind, swing_type, direction in (
        ("EQH", SwingType.HIGH, Direction.BEARISH),
        ("EQL", SwingType.LOW, Direction.BULLISH),
    ):
        same = [s for s in swings if s.type is swing_type]
        for first, second in zip(same, same[1:]):
            if first.price == 0:
                continue
            gap = abs(second.price - first.price) / first.price * 100.0
            if gap <= cfg.equal_level_tolerance_pct:
                events.append(
                    LiquidityEvent(
                        date=second.date,
                        kind=kind,
                        level=(first.price + second.price) / 2.0,
                        direction=direction,
                    )
                )
    return events


def _sweeps(
    df: pd.DataFrame, swings: list[SwingPoint], breaks: list[StructureBreak], cfg: SmcConfig
) -> list[LiquidityEvent]:
    """A wick through a swing that the close does not hold.

    The pairing that matters is a sweep followed by a structure break within
    ``sweep_bos_window`` -- that is the sequence the confluence engine scores,
    rather than a sweep in isolation.
    """
    positions = {d.date(): i for i, d in enumerate(df.index)}
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)

    events: list[LiquidityEvent] = []
    for swing in swings:
        origin = positions.get(swing.date)
        if origin is None:
            continue
        # Only bars after the swing was confirmed can sweep it.
        for i in range(origin + cfg.swing_right_bars + 1, len(df)):
            if swing.type is SwingType.LOW:
                swept = lows[i] < swing.price and closes[i] > swing.price
                direction = Direction.BULLISH
            else:
                swept = highs[i] > swing.price and closes[i] < swing.price
                direction = Direction.BEARISH
            if not swept:
                continue
            if i > 0 and swing.type is SwingType.LOW and closes[i] <= closes[i - 1]:
                continue
            if i > 0 and swing.type is SwingType.HIGH and closes[i] >= closes[i - 1]:
                continue

            sweep_date = df.index[i].date()
            linked = next(
                (
                    b.date for b in breaks
                    if b.direction is direction
                    and 0 <= (positions.get(b.date, -999) - i) <= cfg.sweep_bos_window
                ),
                None,
            )
            events.append(
                LiquidityEvent(
                    date=sweep_date, kind="SWEEP", level=swing.price,
                    direction=direction, reclaimed=True, linked_break=linked,
                )
            )
            break  # a level is swept once
    return events


def analyse(
    df: pd.DataFrame, cfg: SmcConfig | None = None, *, as_of: date | None = None
) -> SmcState:
    """Walk the frame and produce the full structural picture as of ``as_of``."""
    cfg = cfg or SmcConfig()
    if as_of is not None:
        df = df.loc[: pd.Timestamp(as_of)]
    if df.empty:
        return SmcState(
            as_of=as_of or date.min, structure=RANGING, breaks=(), order_blocks=(),
            liquidity=(), last_confirmed_high=None, last_confirmed_low=None,
        )

    swings = find_swings(df, cfg.swing_left_bars, cfg.swing_right_bars, cfg.min_swing_move_pct)
    positions = {d.date(): i for i, d in enumerate(df.index)}

    # A swing at bar i is only KNOWN at bar i + right. Using it before then is
    # the most common way an SMC backtest becomes fiction.
    pending: list[tuple[int, SwingPoint]] = sorted(
        (positions[s.date] + cfg.swing_right_bars, s) for s in swings if s.date in positions
    )

    structure = RANGING
    active_high: SwingPoint | None = None
    active_low: SwingPoint | None = None
    breaks: list[StructureBreak] = []
    order_blocks: list[OrderBlock] = []

    # Pulled once. `df.iloc[i]` builds a Series per bar, and a decade of
    # daily data across a universe makes that the dominant cost of a scan.
    col_close = df["close"].to_numpy(dtype=float)
    col_high = df["high"].to_numpy(dtype=float)
    col_low = df["low"].to_numpy(dtype=float)
    col_volume = df["volume"].to_numpy(dtype=float)
    col_open = df["open"].to_numpy(dtype=float)
    bar_dates = [d.date() for d in df.index]
    columns = {"open": col_open, "close": col_close}

    cursor = 0
    for i in range(len(df)):
        while cursor < len(pending) and pending[cursor][0] <= i:
            _, swing = pending[cursor]
            if swing.type is SwingType.HIGH:
                active_high = swing
            else:
                active_low = swing
            cursor += 1

        today = bar_dates[i]

        if active_high is not None and active_high.date < today:
            level = _break_level_at(i, col_close, col_high, col_low, cfg, Direction.BULLISH)
            if level > active_high.price:
                # Same break, two meanings: continuation if already bullish,
                # a change of character if the prior structure was bearish.
                kind = (
                    StructureEvent.CHOCH
                    if structure == "BEARISH"
                    else StructureEvent.BOS
                )
                breaks.append(
                    StructureBreak(
                        date=today, kind=kind, direction=Direction.BULLISH,
                        broken_level=active_high.price, broken_swing_date=active_high.date,
                        close_price=float(col_close[i]), volume=float(col_volume[i]),
                        prior_structure=structure,
                    )
                )
                block = _find_order_block(df, i, Direction.BULLISH, cfg, columns=columns)
                if block is not None:
                    order_blocks.append(block)
                structure = "BULLISH"
                active_high = None  # retired; a fresh swing must confirm
                continue

        if active_low is not None and active_low.date < today:
            level = _break_level_at(i, col_close, col_high, col_low, cfg, Direction.BEARISH)
            if level < active_low.price:
                kind = (
                    StructureEvent.CHOCH
                    if structure == "BULLISH"
                    else StructureEvent.BOS
                )
                breaks.append(
                    StructureBreak(
                        date=today, kind=kind, direction=Direction.BEARISH,
                        broken_level=active_low.price, broken_swing_date=active_low.date,
                        close_price=float(col_close[i]), volume=float(col_volume[i]),
                        prior_structure=structure,
                    )
                )
                block = _find_order_block(df, i, Direction.BEARISH, cfg, columns=columns)
                if block is not None:
                    order_blocks.append(block)
                structure = "BEARISH"
                active_low = None

    confirmed = [s for s in swings if positions[s.date] + cfg.swing_right_bars < len(df)]
    liquidity = _equal_levels(confirmed, cfg) + _sweeps(df, confirmed, breaks, cfg)

    return SmcState(
        as_of=df.index[-1].date(),
        structure=structure,
        breaks=tuple(breaks),
        order_blocks=tuple(_update_order_blocks(df, order_blocks)),
        liquidity=tuple(sorted(liquidity, key=lambda e: e.date)),
        last_confirmed_high=active_high,
        last_confirmed_low=active_low,
    )
