"""VCP engine. Phase 2 gate.

Bases are built from explicit waypoints so the expected answer can be
verified by reading the test, rather than by trusting a recorded snapshot of
whatever the code happened to do first.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from vriddhix.domain.types import PatternStatus
from vriddhix.engines.vcp import VcpConfig, detect, triangular
from conftest import build_path, frame_from_closes, make_ohlcv, make_vcp

import numpy as np


@pytest.fixture
def vcp_config(cfg):
    return VcpConfig.from_config(cfg)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_detects_a_textbook_base(vcp_config):
    result = detect(make_vcp(), vcp_config)

    assert result is not None
    assert len(result.base.contractions) >= 2
    assert result.base.depth_pct < vcp_config.max_base_depth_pct
    assert result.score.total > 0


def test_contractions_tighten(vcp_config):
    result = detect(make_vcp(), vcp_config)
    depths = [c.depth_pct for c in result.base.contractions]
    assert depths == sorted(depths, reverse=True), f"expected tightening, got {depths}"


def test_score_breaks_down_into_its_components(vcp_config):
    """A score the reader cannot decompose is a number taken on faith."""
    result = detect(make_vcp(), vcp_config)
    names = {c.name for c in result.score.components}
    assert names == {
        "prior_trend", "base_quality", "contraction_quality", "volume_dryup",
        "relative_strength", "sector_strength", "pivot_readiness",
    }
    assert result.score.total == pytest.approx(
        sum(c.contribution for c in result.score.components), abs=1e-9
    )


def test_pivot_sits_at_or_above_the_last_contraction_high(vcp_config):
    result = detect(make_vcp(), vcp_config)
    last_high = result.base.contractions[-1].high
    assert result.base.pivot_price >= min(last_high, result.base.base_high)
    assert result.base.pivot_type in ("LAST_CONTRACTION_HIGH", "BASE_HIGH")


# ---------------------------------------------------------------------------
# Rejection -- structural failure returns None, not a low score
# ---------------------------------------------------------------------------


def test_rejects_a_base_with_no_prior_trend(vcp_config):
    """A base is only meaningful as a pause in something."""
    closes = build_path([(200.0, 0), (100.0, 260), (96.0, 12), (99.0, 10),
                         (97.0, 8), (98.5, 8)])
    assert detect(frame_from_closes(closes), vcp_config) is None


def test_rejects_a_base_that_is_too_deep(vcp_config):
    deep = make_vcp(base_waypoints=[
        (200.0, 0), (120.0, 12), (190.0, 12), (130.0, 10),
        (185.0, 10), (140.0, 8), (180.0, 8),
    ])
    result = detect(deep, vcp_config)
    assert result is None, "a 40%-deep base is not a VCP"


def test_rejects_a_frame_that_is_too_short(vcp_config):
    assert detect(make_ohlcv(50), vcp_config) is None


def test_rejects_a_downtrend(vcp_config):
    assert detect(make_ohlcv(400, pattern="downtrend", seed=3), vcp_config) is None


def test_returns_none_rather_than_a_zero_score(vcp_config):
    """None means 'no structural candidate', which is a different statement
    from 'a poor base' -- the latter returns a result with a low score."""
    assert detect(make_ohlcv(400, pattern="chop", seed=9), vcp_config) is None


# ---------------------------------------------------------------------------
# Tolerance -- the property that separates this from a naive implementation
# ---------------------------------------------------------------------------


def test_an_imperfect_sequence_still_scores_well(vcp_config):
    """18 -> 11 -> 12 -> 6 has one out-of-order step and is a real VCP.

    A zero-tolerance monotonicity test would reject it, which would throw away
    a large share of genuine bases.
    """
    from vriddhix.engines.vcp import _score_contractions
    from vriddhix.domain.types import Contraction

    def contraction(seq: int, depth: float) -> Contraction:
        return Contraction(
            sequence=seq, start=date(2024, 1, 1), end=date(2024, 1, 10),
            high=100.0, low=100.0 - depth, depth_pct=depth,
            duration_days=8, avg_volume=1e6, atr_avg=1.0,
        )

    imperfect = [contraction(i, d) for i, d in enumerate([18.0, 11.0, 12.0, 6.0], 1)]
    score, diagnostics = _score_contractions(imperfect, vcp_config)

    assert score > 80, f"imperfect-but-real VCP scored {score}"
    assert diagnostics["tightening_pairs"] == "3/3"  # 12 <= 11 * 1.15


def test_a_widening_sequence_scores_badly(vcp_config):
    from vriddhix.engines.vcp import _score_contractions
    from vriddhix.domain.types import Contraction

    widening = [
        Contraction(sequence=i, start=date(2024, 1, 1), end=date(2024, 1, 10),
                    high=100.0, low=100.0 - d, depth_pct=d,
                    duration_days=8, avg_volume=1e6, atr_avg=1.0)
        for i, d in enumerate([6.0, 11.0, 18.0], 1)
    ]
    score, _ = _score_contractions(widening, vcp_config)
    assert score < 30, f"a widening sequence scored {score}"


# ---------------------------------------------------------------------------
# Stage classification
# ---------------------------------------------------------------------------


def test_price_far_below_the_pivot_is_forming(vcp_config):
    df = make_vcp(base_waypoints=[
        (200.0, 0), (186.0, 10), (198.0, 10), (180.0, 8),
        (196.0, 8), (188.0, 6), (193.0, 6), (176.0, 8),
    ])
    result = detect(df, vcp_config)
    assert result is not None
    assert result.stage is PatternStatus.FORMING
    assert result.distance_from_pivot_pct > vcp_config.near_pivot_pct


def test_price_close_to_the_pivot_is_near_pivot(vcp_config):
    result = detect(make_vcp(), vcp_config)
    assert result is not None
    if result.distance_from_pivot_pct <= vcp_config.near_pivot_pct:
        assert result.stage is PatternStatus.NEAR_PIVOT


def test_a_close_above_the_pivot_is_a_breakout(vcp_config):
    df = make_vcp(base_waypoints=[
        (200.0, 0), (186.0, 10), (198.0, 10), (180.0, 8),
        (196.0, 8), (188.0, 6), (193.0, 6), (188.0, 5), (206.0, 5),
    ])
    result = detect(df, vcp_config)
    assert result is not None
    assert result.stage is PatternStatus.BREAKOUT


def test_breakout_without_volume_is_still_detected_but_flagged(vcp_config):
    """Detection and confirmation are separate steps: the lifecycle service
    decides CONFIRMED vs FAILED from what happens next."""
    df = make_vcp(
        base_waypoints=[
            (200.0, 0), (186.0, 10), (198.0, 10), (180.0, 8),
            (196.0, 8), (188.0, 6), (193.0, 6), (188.0, 5), (206.0, 5),
        ],
        base_volume=200_000.0,   # thin: no volume confirmation
        prior_volume=2_000_000.0,
    )
    result = detect(df, vcp_config)
    assert result.stage is PatternStatus.BREAKOUT
    assert result.diagnostics["volume_confirmed"] is False


# ---------------------------------------------------------------------------
# Market context
# ---------------------------------------------------------------------------


def test_absent_rs_scores_zero_and_is_flagged(vcp_config):
    """Never redistributed: a score computed without market context must not
    be able to impersonate one that had it."""
    result = detect(make_vcp(), vcp_config)
    rs = result.score.component("relative_strength")
    assert rs.raw == 0.0
    assert result.diagnostics["rs_available"] is False


def test_supplying_rs_and_sector_raises_the_total(vcp_config):
    without = detect(make_vcp(), vcp_config)
    with_context = detect(make_vcp(), vcp_config, rs_score=95.0, sector_score=85.0)

    assert with_context.score.total > without.score.total
    assert with_context.score.component("relative_strength").raw == 95.0
    assert with_context.diagnostics["rs_available"] is True


# ---------------------------------------------------------------------------
# Causality
# ---------------------------------------------------------------------------


def test_detection_is_invariant_to_future_bars(vcp_config):
    """The Phase 2 acceptance gate: a base identified as of T must not change
    when later bars arrive."""
    df = make_vcp()
    cutoff = df.index[-20]

    truncated = detect(df.loc[:cutoff], vcp_config)
    with_future = detect(df, vcp_config, as_of=cutoff.date())

    assert (truncated is None) == (with_future is None)
    if truncated is not None:
        assert truncated.base.pivot_price == with_future.base.pivot_price
        assert truncated.base.start == with_future.base.start
        assert truncated.score.total == pytest.approx(with_future.score.total, abs=1e-9)


@pytest.mark.parametrize("back", [0, 5, 10])
def test_pivot_does_not_move_retroactively(vcp_config, back):
    """The pivot recorded for a given date must not depend on later bars.

    Note what this does NOT claim. The pivot legitimately *evolves* as the
    base develops: when a new, lower contraction forms, the last-contraction
    high moves and so does the pivot. That is the pattern changing, not the
    past being rewritten.

    What must never happen is the pivot for date T differing according to
    whether bars after T happen to be in the frame -- that would make every
    recorded breakout unfalsifiable after the fact.
    """
    df = make_vcp()
    cutoff = df.index[len(df) - 1 - back]

    truncated = detect(df.loc[:cutoff], vcp_config)
    with_future = detect(df, vcp_config, as_of=cutoff.date())

    assert truncated is not None, f"no base detected at cutoff {cutoff.date()}"
    assert truncated.base.pivot_price == with_future.base.pivot_price
    assert truncated.base.start == with_future.base.start
    assert truncated.stage is with_future.stage


def test_the_pivot_follows_the_newest_contraction(vcp_config):
    """As a new contraction forms, the pivot tracks its high downward. This is
    the designed behaviour (docs/05 §6) and is what makes 'distance from
    pivot' meaningful while a base is still tightening."""
    df = make_vcp()

    earlier = detect(df.iloc[:-10], vcp_config)
    later = detect(df, vcp_config)

    assert earlier.base.start == later.base.start, "same base"
    assert len(later.base.contractions) > len(earlier.base.contractions)
    assert later.base.pivot_price < earlier.base.pivot_price


def test_detect_truncates_at_as_of(vcp_config):
    df = make_vcp()
    cutoff = df.index[-30].date()
    result = detect(df, vcp_config, as_of=cutoff)
    if result is not None:
        assert result.base.end <= cutoff


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="weights sum to"):
        VcpConfig(weights=(("prior_trend", 0.5), ("base_quality", 0.4)))


def test_config_is_loaded_from_yaml(cfg, vcp_config):
    assert vcp_config.min_base_days == cfg.get("vcp.min_base_days")
    assert vcp_config.contraction_tolerance == cfg.get("vcp.contraction_tolerance")


def test_triangular_peaks_at_the_ideal():
    assert triangular(18, ideal=18, lo=5, hi=35) == 1.0
    assert triangular(5, ideal=18, lo=5, hi=35) == 0.0
    assert triangular(35, ideal=18, lo=5, hi=35) == 0.0
    assert 0 < triangular(12, ideal=18, lo=5, hi=35) < 1
    # A deeper-than-ideal base scores lower, not higher: the sweet spot is a
    # peak, not a threshold.
    assert triangular(30, ideal=18, lo=5, hi=35) < triangular(18, ideal=18, lo=5, hi=35)


def test_a_tighter_config_rejects_a_marginal_base(vcp_config):
    df = make_vcp()
    assert detect(df, vcp_config) is not None

    strict = VcpConfig.from_config
    tighter = VcpConfig(
        **{
            **{f: getattr(vcp_config, f) for f in VcpConfig.__dataclass_fields__},
            "min_contractions": 6,
        }
    )
    assert detect(df, tighter) is None, "config must actually gate detection"
