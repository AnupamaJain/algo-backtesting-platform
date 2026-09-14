"""Domain vocabulary: enums, state machine, value objects."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from vriddhix.domain.types import (
    ALLOWED_TRANSITIONS,
    Bar,
    CompositeScore,
    PatternStatus,
    ScoreComponent,
    Severity,
    SurvivorshipMode,
    UniverseResolution,
    can_transition,
)


# ---------------------------------------------------------------------------
# Lifecycle state machine
# ---------------------------------------------------------------------------


def test_every_status_has_declared_transitions():
    for status in PatternStatus:
        assert status in ALLOWED_TRANSITIONS


def test_terminal_states_are_terminal():
    """A symbol that sets up again gets a new pattern row. Resurrecting a
    failed one would make each attempt uncountable."""
    assert ALLOWED_TRANSITIONS[PatternStatus.FAILED] == frozenset()
    assert ALLOWED_TRANSITIONS[PatternStatus.COMPLETED] == frozenset()


def test_states_cannot_be_skipped():
    """FORMING -> CONFIRMED would mean a breakout was never recorded, and the
    breakout row is what the research database is built on."""
    assert not can_transition(PatternStatus.FORMING, PatternStatus.CONFIRMED)
    assert not can_transition(PatternStatus.FORMING, PatternStatus.EXTENDED)
    assert not can_transition(PatternStatus.NEAR_PIVOT, PatternStatus.CONFIRMED)


def test_the_happy_path_is_walkable():
    path = [
        PatternStatus.FORMING,
        PatternStatus.NEAR_PIVOT,
        PatternStatus.BREAKOUT,
        PatternStatus.CONFIRMED,
        PatternStatus.EXTENDED,
        PatternStatus.COMPLETED,
    ]
    for src, dst in zip(path, path[1:]):
        assert can_transition(src, dst), f"{src} -> {dst} should be allowed"


def test_failure_is_reachable_from_every_non_terminal_state():
    for status in PatternStatus:
        if status in (PatternStatus.FAILED, PatternStatus.COMPLETED):
            continue
        assert can_transition(status, PatternStatus.FAILED), f"{status} must be able to fail"


def test_near_pivot_can_relax_back_to_forming():
    """Price drifting away from the pivot is not a failure -- it is the base
    still forming."""
    assert can_transition(PatternStatus.NEAR_PIVOT, PatternStatus.FORMING)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


def test_bar_rejects_impossible_ohlc():
    with pytest.raises(ValueError, match="impossible bar"):
        Bar(date=date(2024, 1, 1), open=10, high=9, low=11, close=10, volume=100)


def test_bar_accepts_a_valid_bar():
    bar = Bar(date=date(2024, 1, 1), open=10, high=12, low=9, close=11, volume=100)
    assert bar.high >= bar.close >= bar.low


def test_value_objects_are_frozen():
    """Immutability stops a scoring step quietly mutating what a detection
    step returned."""
    bar = Bar(date=date(2024, 1, 1), open=10, high=12, low=9, close=11, volume=100)
    with pytest.raises(dataclasses.FrozenInstanceError):
        bar.close = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------


def test_score_component_contribution():
    component = ScoreComponent(name="prior_trend", raw=94.0, weight=0.15)
    assert component.contribution == pytest.approx(14.1)


def test_composite_score_rejects_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        CompositeScore(total=120.0, components=(), rule_version="SCORING_V1.0")


def test_composite_score_exposes_its_breakdown():
    """A score the reader cannot decompose is a number they are asked to take
    on faith."""
    components = (
        ScoreComponent("prior_trend", 94.0, 0.15),
        ScoreComponent("contraction_quality", 96.0, 0.20),
    )
    score = CompositeScore(total=33.3, components=components, rule_version="SCORING_V1.0")
    assert score.component("prior_trend").raw == 94.0
    assert score.component("absent") is None


# ---------------------------------------------------------------------------
# Universe resolution
# ---------------------------------------------------------------------------


def test_current_universe_resolution_is_marked_biased():
    resolution = UniverseResolution(
        index_code="NIFTY500",
        as_of=date(2020, 1, 1),
        symbols=("A", "B"),
        mode=SurvivorshipMode.CURRENT_UNIVERSE,
        warning="no history",
    )
    assert resolution.is_biased is True


def test_point_in_time_resolution_is_not_biased():
    resolution = UniverseResolution(
        index_code="NIFTY500",
        as_of=date(2020, 1, 1),
        symbols=("A",),
        mode=SurvivorshipMode.POINT_IN_TIME,
    )
    assert resolution.is_biased is False
    assert resolution.warning is None


def test_severity_ordering_is_explicit():
    assert Severity.ERROR.value == "ERROR"
    assert {s.value for s in Severity} == {"INFO", "WARN", "ERROR"}
