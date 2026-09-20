"""The regime chain, and what an out-of-order session does to it.

Regime is not a per-day fact. Hysteresis makes each label depend on the one
before it, so the stored sequence is a chain and an insertion in the middle
invalidates everything after it. These tests pin that the damage is
detectable and that the repair is faithful to the engine.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from vriddhix.config import get_config
from vriddhix.db.models import MarketRegimeRow
from vriddhix.engines.regime import RegimeConfig, apply_hysteresis, classify
from vriddhix.services.regime_chain import flickers, replay


@pytest.fixture
def cfg():
    return RegimeConfig.from_config(get_config())


def _store(session, scores: list[tuple[date, float, str]]) -> None:
    for when, score, label in scores:
        session.add(MarketRegimeRow(
            date=when, regime=label, regime_score=score,
            engine_version="REGIME_ENGINE_V1.0",
        ))
    session.flush()


def _chain(cfg, scores: list[float]) -> list[str]:
    """What the engine says the labels should be, computed in order."""
    current, candidates, out = None, [], []
    for score in scores:
        raw = classify(score, cfg)
        effective, _ = apply_hysteresis(raw, score, current, candidates, cfg)
        out.append(effective.value)
        current = effective
        candidates.append(raw)
    return out


def test_a_consistent_chain_is_left_alone(session, cfg):
    """The repair must be idempotent, or running it becomes its own risk."""
    start = date(2024, 1, 1)
    scores = [80.0, 79.0, 78.0, 77.0, 76.0, 75.0]
    labels = _chain(cfg, scores)
    _store(session, [(start + timedelta(days=i), s, l)
                     for i, (s, l) in enumerate(zip(scores, labels))])

    report = replay(session, cfg, commit=True)

    assert report.is_clean
    assert report.changed == 0
    assert report.examined == len(scores)


def test_a_session_inserted_late_breaks_the_labels_after_it(session, cfg):
    """The real defect. A session arrives out of order -- NSE holds weekend
    sessions for Budget Day, Muhurat and DR drills -- and every hysteresis
    decision after it was taken against the wrong predecessor."""
    start = date(2024, 1, 1)
    without = [80.0, 60.0, 61.0, 62.0]
    labels = _chain(cfg, without)

    # Stored as if the middle session had never existed.
    _store(session, [
        (start, without[0], labels[0]),
        (start + timedelta(days=2), without[1], labels[1]),
        (start + timedelta(days=3), without[2], labels[2]),
        (start + timedelta(days=4), without[3], labels[3]),
    ])
    # ...then it turns up, labelled against whatever was stored last.
    _store(session, [(start + timedelta(days=1), 61.0, labels[-1])])

    report = replay(session, cfg, commit=False)

    assert not report.is_clean, "an inserted session left the chain consistent"
    assert report.first_change is not None


def test_the_repair_agrees_with_the_engine(session, cfg):
    """The replay must produce exactly what classify + apply_hysteresis do.

    Restating the hysteresis rules inside the repair would let the two drift,
    and they would then disagree about the same day -- which is the specific
    failure this platform is built to refuse.
    """
    start = date(2024, 1, 1)
    scores = [82.0, 74.0, 73.0, 71.0, 78.0, 78.0, 79.0, 60.0, 59.0, 58.0]
    _store(session, [(start + timedelta(days=i), s, "NEUTRAL")
                     for i, s in enumerate(scores)])

    replay(session, cfg, commit=True)

    stored = [r.regime for r in session.scalars(
        __import__("sqlalchemy").select(MarketRegimeRow)
        .order_by(MarketRegimeRow.date)).all()]
    assert stored == _chain(cfg, scores)


def test_scores_are_never_rewritten(session, cfg):
    """A score is a per-day measurement that no neighbour can change. Only
    the label depends on the chain."""
    start = date(2024, 1, 1)
    scores = [82.0, 74.0, 60.0, 59.0]
    _store(session, [(start + timedelta(days=i), s, "NEUTRAL")
                     for i, s in enumerate(scores)])

    replay(session, cfg, commit=True)

    after = [float(r.regime_score) for r in session.scalars(
        __import__("sqlalchemy").select(MarketRegimeRow)
        .order_by(MarketRegimeRow.date)).all()]
    assert after == scores


def test_a_dry_run_writes_nothing(session, cfg):
    start = date(2024, 1, 1)
    _store(session, [(start + timedelta(days=i), s, "NEUTRAL")
                     for i, s in enumerate([82.0, 74.0, 60.0, 59.0])])

    report = replay(session, cfg, commit=False)
    assert report.changed > 0

    stored = [r.regime for r in session.scalars(
        __import__("sqlalchemy").select(MarketRegimeRow)
        .order_by(MarketRegimeRow.date)).all()]
    assert stored == ["NEUTRAL"] * 4, "a dry run wrote to the database"


def test_flickers_finds_a_single_session_state_change(session):
    start = date(2024, 1, 1)
    _store(session, [
        (start, 70.0, "BULL"),
        (start + timedelta(days=1), 69.0, "NEUTRAL"),
        (start + timedelta(days=2), 71.0, "BULL"),
    ])
    assert flickers(session) == [start + timedelta(days=1)]


def test_flickers_is_quiet_on_a_sustained_change(session):
    start = date(2024, 1, 1)
    _store(session, [
        (start, 70.0, "BULL"),
        (start + timedelta(days=1), 69.0, "NEUTRAL"),
        (start + timedelta(days=2), 68.0, "NEUTRAL"),
    ])
    assert flickers(session) == []
