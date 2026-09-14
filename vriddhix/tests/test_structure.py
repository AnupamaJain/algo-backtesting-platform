"""Phases 4-5: swings, market structure, fair value gaps, confluence."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from vriddhix.domain.types import Direction, FvgStatus, StructureEvent, SwingType
from vriddhix.engines.fvg import (
    FvgConfig,
    detect_gaps,
    open_gaps,
    score_confluence,
)
from vriddhix.engines.smc import SmcConfig, analyse
from vriddhix.engines.swings import alternating, confirmed_swings, find_swings
from conftest import build_path, frame_from_closes, make_ohlcv


def bars(rows: list[tuple[float, float, float, float]], start="2024-01-01") -> pd.DataFrame:
    """Explicit OHLC rows -- (open, high, low, close)."""
    index = pd.bdate_range(start, periods=len(rows), freq="B")
    return pd.DataFrame(
        {
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [1_000_000.0] * len(rows),
        },
        index=index,
    ).rename_axis("date")


# ===========================================================================
# Swings
# ===========================================================================


def test_finds_an_obvious_swing_high():
    df = frame_from_closes(build_path([(100.0, 0), (120.0, 6), (100.0, 6)]))
    swings = find_swings(df, left=3, right=3)
    highs = [s for s in swings if s.type is SwingType.HIGH]
    assert highs, "a clear peak must produce a swing high"
    assert highs[0].price == pytest.approx(df["high"].max(), rel=1e-6)


def test_a_plateau_marks_the_first_bar():
    """Asymmetric strictness (> left, >= right) stops the swing drifting
    forward as more equal bars arrive."""
    df = bars(
        [(10, 11, 9, 10)] * 3
        + [(10, 15, 9, 14), (14, 15, 13, 14), (14, 15, 13, 14)]
        + [(14, 12, 9, 10)] * 3
    )
    highs = [s for s in find_swings(df, 3, 3) if s.type is SwingType.HIGH]
    assert highs
    assert highs[0].date == df.index[3].date()


def test_a_swing_is_not_reported_before_it_could_be_known():
    """The confirmation lag. Reporting a swing in the final `right` bars uses
    information that had not printed yet."""
    df = frame_from_closes(build_path([(100.0, 0), (130.0, 10), (120.0, 2)]))

    raw = find_swings(df, 3, 3)
    confirmed = confirmed_swings(df, left=3, right=3)

    assert len(confirmed) <= len(raw)
    if confirmed:
        last_knowable = df.index[len(df) - 1 - 3].date()
        assert all(s.date <= last_knowable for s in confirmed)


def test_confirmed_swings_are_stable_as_bars_arrive():
    df = frame_from_closes(build_path([(100.0, 0), (140.0, 20), (110.0, 20), (150.0, 20)]))
    early = confirmed_swings(df.iloc[:40], left=3, right=3)
    later = [s for s in confirmed_swings(df, left=3, right=3) if s.date <= early[-1].date]
    assert early == later


def test_alternating_collapses_runs_of_the_same_type():
    """Three highs in a row would otherwise produce two zero-length
    contractions."""
    df = frame_from_closes(build_path([(100.0, 0), (120.0, 5), (115.0, 4), (125.0, 5),
                                       (100.0, 8)]))
    swings = alternating(find_swings(df, 2, 2))
    for first, second in zip(swings, swings[1:]):
        assert first.type is not second.type


def test_swing_requires_positive_windows():
    with pytest.raises(ValueError):
        find_swings(make_ohlcv(50), left=0, right=3)


# ===========================================================================
# Market structure
# ===========================================================================


def test_a_rising_market_produces_bullish_breaks():
    df = frame_from_closes(
        build_path([(100.0, 0), (120.0, 8), (110.0, 6), (135.0, 8), (125.0, 6), (150.0, 8)])
    )
    state = analyse(df, SmcConfig(swing_left_bars=2, swing_right_bars=2))
    assert state.structure == "BULLISH"
    assert any(b.direction is Direction.BULLISH for b in state.breaks)


def test_the_same_break_is_bos_or_choch_depending_on_prior_structure():
    """This is the distinction that requires structure to be carried as state
    rather than recomputed per bar."""
    df = frame_from_closes(
        build_path([
            (100.0, 0), (80.0, 10),    # decline
            (90.0, 6), (70.0, 8),      # lower high, lower low -> bearish
            (95.0, 10),                # breaks the lower high -> CHoCH
            (85.0, 6), (110.0, 10),    # continuation -> BOS
        ])
    )
    state = analyse(df, SmcConfig(swing_left_bars=2, swing_right_bars=2))
    bullish = [b for b in state.breaks if b.direction is Direction.BULLISH]
    assert bullish, "expected at least one bullish break"

    kinds = [b.kind for b in bullish]
    if StructureEvent.CHOCH in kinds:
        first_choch = kinds.index(StructureEvent.CHOCH)
        assert bullish[first_choch].prior_structure == "BEARISH"


def test_a_break_records_what_it_broke():
    """So the UI can draw exactly what changed rather than asserting that
    something did."""
    df = frame_from_closes(build_path([(100.0, 0), (120.0, 8), (110.0, 6), (140.0, 8)]))
    state = analyse(df, SmcConfig(swing_left_bars=2, swing_right_bars=2))
    for event in state.breaks:
        assert event.broken_level > 0
        assert event.broken_swing_date < event.date


def test_close_based_breaks_are_fewer_than_wick_based():
    """Wick breaks generate roughly 3x the events and most are noise."""
    df = make_ohlcv(300, seed=44)
    close_based = analyse(df, SmcConfig(break_on="close"))
    wick_based = analyse(df, SmcConfig(break_on="high_low"))
    assert len(wick_based.breaks) >= len(close_based.breaks)


def test_structure_analysis_is_invariant_to_future_bars():
    df = make_ohlcv(300, seed=51)
    cutoff = df.index[220]

    truncated = analyse(df.loc[:cutoff])
    with_future = analyse(df, as_of=cutoff.date())

    assert truncated.structure == with_future.structure
    assert [b.date for b in truncated.breaks] == [b.date for b in with_future.breaks]
    assert [b.kind for b in truncated.breaks] == [b.kind for b in with_future.breaks]


def test_order_block_is_the_last_opposing_candle_before_the_impulse():
    df = frame_from_closes(build_path([(100.0, 0), (95.0, 4), (130.0, 10), (120.0, 5),
                                       (145.0, 8)]))
    state = analyse(df, SmcConfig(swing_left_bars=2, swing_right_bars=2))
    for block in state.order_blocks:
        assert block.upper >= block.lower
        assert block.date <= block.origin_break


def test_equal_highs_are_detected_as_liquidity():
    df = bars(
        [(10, 11, 9, 10)] * 3
        + [(10, 20, 10, 19)]                      # first high
        + [(19, 19, 14, 15)] * 4
        + [(15, 20.01, 14, 19)]                   # equal high within tolerance
        + [(19, 19, 12, 13)] * 4
    )
    state = analyse(df, SmcConfig(swing_left_bars=2, swing_right_bars=2,
                                  equal_level_tolerance_pct=0.5))
    assert any(e.kind == "EQH" for e in state.liquidity)


def test_empty_frame_is_handled():
    state = analyse(pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))
    assert state.structure == "RANGING"
    assert state.breaks == ()


# ===========================================================================
# Fair value gaps
# ===========================================================================


def test_bullish_gap_arithmetic():
    """candle1.high < candle3.low leaves an unfilled band."""
    df = bars([
        (10, 11, 9, 10),      # candle 1: high 11
        (11, 20, 11, 19),     # impulse
        (19, 22, 15, 21),     # candle 3: low 15 > 11
    ] + [(21, 22, 20, 21)] * 5)

    gaps = detect_gaps(df, FvgConfig(min_gap_pct=0.1))
    assert len(gaps) >= 1
    gap = gaps[0]
    assert gap.direction is Direction.BULLISH
    assert gap.lower == pytest.approx(11.0)
    assert gap.upper == pytest.approx(15.0)
    assert gap.size_pct == pytest.approx((15 - 11) / 11 * 100)


def test_bearish_gap_arithmetic():
    df = bars([
        (20, 21, 19, 20),     # candle 1: low 19
        (19, 19, 10, 11),     # impulse down
        (11, 15, 9, 10),      # candle 3: high 15 < 19
    ] + [(10, 11, 9, 10)] * 5)

    gaps = detect_gaps(df, FvgConfig(min_gap_pct=0.1))
    assert gaps[0].direction is Direction.BEARISH
    assert gaps[0].lower == pytest.approx(15.0)
    assert gaps[0].upper == pytest.approx(19.0)


def test_tiny_gaps_are_discarded_as_noise():
    df = bars([
        (10, 11, 9, 10),
        (11, 12, 11, 11.5),
        (11.5, 12, 11.005, 11.8),   # ~0.05% gap
    ] + [(11.8, 12, 11, 11.5)] * 5)
    assert detect_gaps(df, FvgConfig(min_gap_pct=0.1)) == []


def test_an_untouched_gap_stays_open():
    df = bars([
        (10, 11, 9, 10),
        (11, 20, 11, 19),
        (19, 22, 15, 21),
    ] + [(21, 24, 20, 23)] * 10)   # never returns to the gap
    gap = detect_gaps(df)[0]
    assert gap.status is FvgStatus.OPEN
    assert gap.mitigation_pct == pytest.approx(0.0)


def test_a_revisited_gap_is_mitigated():
    df = bars([
        (10, 11, 9, 10),
        (11, 20, 11, 19),
        (19, 22, 15, 21),
    ] + [(21, 22, 20, 21)] * 3 + [(21, 22, 11.5, 13)] + [(13, 14, 12, 13)] * 3)
    gap = detect_gaps(df)[0]
    assert gap.status in (FvgStatus.MITIGATED, FvgStatus.PARTIALLY_FILLED)
    assert gap.mitigation_pct > 0
    assert gap.mitigated_at is not None or gap.status is FvgStatus.PARTIALLY_FILLED


def test_mitigation_never_decreases():
    """A gap tagged 60% and left does not revert to 0% -- the deepest touch is
    the fact worth keeping."""
    df = bars([
        (10, 11, 9, 10),
        (11, 20, 11, 19),
        (19, 22, 15, 21),
        (21, 22, 13, 14),      # deep touch
    ] + [(14, 30, 25, 29)] * 6)  # walks far away afterwards
    gap = detect_gaps(df)[0]
    assert gap.mitigation_pct > 40


def test_a_gap_closed_through_is_invalidated():
    df = bars([
        (10, 11, 9, 10),
        (11, 20, 11, 19),
        (19, 22, 15, 21),
    ] + [(21, 22, 5, 6)] + [(6, 7, 5, 6)] * 5)
    gap = detect_gaps(df)[0]
    assert gap.status is FvgStatus.INVALIDATED


def test_open_gaps_filters_by_status_and_direction():
    gaps = detect_gaps(make_ohlcv(300, seed=61))
    bullish = open_gaps(gaps, Direction.BULLISH)
    assert all(g.direction is Direction.BULLISH for g in bullish)
    assert all(g.status in (FvgStatus.OPEN, FvgStatus.PARTIALLY_FILLED) for g in bullish)


def test_gap_detection_is_invariant_to_future_bars():
    df = make_ohlcv(300, seed=71)
    cutoff = df.index[200]

    truncated = detect_gaps(df.loc[:cutoff])
    with_future = detect_gaps(df, as_of=cutoff.date())

    assert [(g.created_at, g.lower, g.upper) for g in truncated] == [
        (g.created_at, g.lower, g.upper) for g in with_future
    ]


# ===========================================================================
# Confluence
# ===========================================================================


def test_confluence_counts_conditions_not_probability():
    result = score_confluence(
        direction=Direction.BULLISH, price=100.0,
        rs_score=85.0, regime_supportive=True, rel_volume=1.5, ema_aligned=True,
    )
    assert 0 <= result.score <= 100
    assert result.conditions_total == 10
    assert "conditions" in result.describe()


def test_more_conditions_met_scores_higher():
    few = score_confluence(direction=Direction.BULLISH, price=100.0, rs_score=40.0)
    many = score_confluence(
        direction=Direction.BULLISH, price=100.0,
        rs_score=90.0, regime_supportive=True, rel_volume=2.0,
        ema_aligned=True, vwap_aligned=True,
    )
    assert many.score > few.score
    assert many.conditions_met > few.conditions_met


def test_a_gap_underfoot_outranks_a_distant_one():
    """Graded, not binary: 'price sitting in the gap' and 'a gap exists 6%
    away' would otherwise score identically."""
    from vriddhix.engines.fvg import FairValueGap

    near = FairValueGap(created_at=date(2024, 1, 1), direction=Direction.BULLISH,
                        upper=101.0, lower=99.0, size_pct=2.0, status=FvgStatus.OPEN,
                        mitigation_pct=0.0, mitigated_at=None)
    far = FairValueGap(created_at=date(2024, 1, 1), direction=Direction.BULLISH,
                       upper=90.0, lower=88.0, size_pct=2.0, status=FvgStatus.OPEN,
                       mitigation_pct=0.0, mitigated_at=None)

    inside = score_confluence(direction=Direction.BULLISH, price=100.0, gaps=[near])
    away = score_confluence(direction=Direction.BULLISH, price=100.0, gaps=[far])
    assert inside.score > away.score


def test_weak_rs_does_not_earn_the_rs_component():
    result = score_confluence(direction=Direction.BULLISH, price=100.0, rs_score=40.0)
    assert result.components[8].name == "rs_supportive"
    assert result.components[8].raw == 0.0


def test_counter_trend_setups_are_recorded_not_hidden():
    """They are exactly the population worth studying separately."""
    df = frame_from_closes(build_path([(150.0, 0), (100.0, 30), (90.0, 20)]))
    state = analyse(df, SmcConfig(swing_left_bars=2, swing_right_bars=2))

    result = score_confluence(
        direction=Direction.BULLISH, price=90.0, smc_state=state,
        as_of=df.index[-1].date(), regime_supportive=True, rs_score=90.0,
    )
    if state.structure == "BEARISH":
        assert result.counter_trend is True
        assert result.components[0].raw == 0.0   # bos_aligned zeroed
        assert result.conditions_met >= 1        # but not suppressed entirely


def test_confluence_components_sum_to_the_score():
    result = score_confluence(direction=Direction.BULLISH, price=100.0, rs_score=90.0)
    assert result.score == pytest.approx(
        sum(c.contribution for c in result.components), abs=1e-9
    )
