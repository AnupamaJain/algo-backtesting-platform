"""Swing detection -- the primitive both VCP and SMC rest on.

There is one implementation because there must be one. VCP decomposes a base
into contractions using swings; SMC builds BOS and CHoCH from swings. If each
had its own, the two would drift and a "BOS confirming a VCP pivot" would be
comparing structures found by different rules.

CONFIRMATION LAG is the subtle part. A swing high at bar ``i`` is not *known*
until ``i + right`` bars have printed -- until then, a higher high could still
arrive and cancel it. Every consumer must use ``confirmed_swings``. Drawing a
swing using bars that had not yet printed is the most common way an SMC
backtest becomes fiction.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ..domain.types import SwingPoint, SwingType


def find_swings(
    df: pd.DataFrame,
    left: int = 3,
    right: int = 3,
    min_move_pct: float = 0.0,
) -> list[SwingPoint]:
    """All swing points in the frame, in chronological order.

    Asymmetric strictness -- ``>`` on the left, ``>=`` on the right -- is
    deliberate. On a plateau of equal highs it marks the FIRST one, so the
    swing does not drift forward as more equal bars arrive. Symmetric strict
    comparison would find no swing at all on a plateau; symmetric loose
    comparison would find several.
    """
    if left < 1 or right < 1:
        raise ValueError("left and right must be >= 1")
    if len(df) < left + right + 1:
        return []

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    dates = df.index

    swings: list[SwingPoint] = []
    for i in range(left, len(df) - right):
        window_left = slice(i - left, i)
        window_right = slice(i + 1, i + 1 + right)

        if (highs[i] > highs[window_left]).all() and (highs[i] >= highs[window_right]).all():
            swings.append(
                SwingPoint(
                    date=dates[i].date(),
                    price=float(highs[i]),
                    type=SwingType.HIGH,
                    strength=_strength(highs, i, left, right, kind="high"),
                )
            )
        elif (lows[i] < lows[window_left]).all() and (lows[i] <= lows[window_right]).all():
            swings.append(
                SwingPoint(
                    date=dates[i].date(),
                    price=float(lows[i]),
                    type=SwingType.LOW,
                    strength=_strength(lows, i, left, right, kind="low"),
                )
            )

    if min_move_pct > 0:
        swings = _filter_by_move(swings, min_move_pct)
    return swings


def _strength(values: np.ndarray, i: int, left: int, right: int, *, kind: str) -> int:
    """How many bars beyond the required window still confirm the swing.

    A high that stands out over 12 bars is a more meaningful level than one
    that clears its neighbours by a whisker, and scoring wants to know.
    """
    limit = max(left, right)
    count = 0
    for offset in range(1, limit * 4 + 1):
        lo, hi = i - offset, i + offset
        if lo < 0 or hi >= len(values):
            break
        if kind == "high":
            if values[i] >= values[lo] and values[i] >= values[hi]:
                count += 1
            else:
                break
        else:
            if values[i] <= values[lo] and values[i] <= values[hi]:
                count += 1
            else:
                break
    return count


def _filter_by_move(swings: list[SwingPoint], min_move_pct: float) -> list[SwingPoint]:
    """Drop swings whose move from the previous opposite swing is trivial.

    Suppresses noise on low-volatility names, where every third bar otherwise
    qualifies as structure.
    """
    if not swings:
        return []

    kept = [swings[0]]
    for swing in swings[1:]:
        previous = kept[-1]
        if swing.type is previous.type:
            # Same type in a row: keep the more extreme one.
            more_extreme = (
                swing.price > previous.price
                if swing.type is SwingType.HIGH
                else swing.price < previous.price
            )
            if more_extreme:
                kept[-1] = swing
            continue

        move = abs(swing.price - previous.price) / previous.price * 100.0
        if move >= min_move_pct:
            kept.append(swing)
    return kept


def confirmed_swings(
    df: pd.DataFrame,
    as_of: date | None = None,
    left: int = 3,
    right: int = 3,
    min_move_pct: float = 0.0,
) -> list[SwingPoint]:
    """Only swings that were already knowable at ``as_of``.

    This is the function callers should use. ``find_swings`` on a frame
    truncated at T still reports a swing in the final ``right`` bars, which
    could not have been identified at T without seeing what came next.
    """
    frame = df if as_of is None else df.loc[: pd.Timestamp(as_of)]
    if frame.empty:
        return []

    swings = find_swings(frame, left, right, min_move_pct)
    if not swings:
        return []

    cutoff_position = len(frame) - 1 - right
    if cutoff_position < 0:
        return []
    cutoff_date = frame.index[cutoff_position].date()
    return [s for s in swings if s.date <= cutoff_date]


def alternating(swings: list[SwingPoint]) -> list[SwingPoint]:
    """Collapse consecutive same-type swings, keeping the extreme of each run.

    Contraction analysis needs a clean high-low-high-low sequence; three highs
    in a row would otherwise produce two zero-length "contractions".
    """
    if not swings:
        return []

    out = [swings[0]]
    for swing in swings[1:]:
        if swing.type is out[-1].type:
            more_extreme = (
                swing.price > out[-1].price
                if swing.type is SwingType.HIGH
                else swing.price < out[-1].price
            )
            if more_extreme:
                out[-1] = swing
        else:
            out.append(swing)
    return out


def last_swing(swings: list[SwingPoint], kind: SwingType) -> SwingPoint | None:
    for swing in reversed(swings):
        if swing.type is kind:
            return swing
    return None
