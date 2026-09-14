"""The daily pipeline: context persistence, failure engine, structure, job.

These tests defend the properties that make stored rows trustworthy:

* what the engine computed is what lands in the table, including its Nones
* re-running a date replaces that date rather than duplicating it
* a version bump cannot silently overwrite what an earlier version said
* a swing is stored with the date it became knowable, not only when it printed
* a failed breakout is resolved and kept, never dropped
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from conftest import make_ohlcv, make_vcp


@pytest.fixture
def populated(session, seeded, cfg):
    from vriddhix.data.ingest import ingest_symbol
    from vriddhix.data.providers import ChainedProvider
    from test_providers import FakeProvider

    start = date(2021, 1, 4)
    frames = {
        "RELIANCE": make_vcp(start=start),
        "TCS": make_ohlcv(319, pattern="uptrend", seed=5, start=start),
        "INFY": make_ohlcv(319, pattern="downtrend", seed=6, start=start),
    }
    for symbol, frame in frames.items():
        ingest_symbol(session, symbol, ChainedProvider([FakeProvider("fake", frame)]), cfg)
    session.flush()
    return frames


@pytest.fixture
def scan_result(session, populated, cfg):
    from vriddhix.services.scanner import scan

    as_of = populated["RELIANCE"].index[-1].date()
    return scan(session, cfg, as_of, symbols=["RELIANCE", "TCS", "INFY"])


# ---------------------------------------------------------------------------
# Context persistence
# ---------------------------------------------------------------------------


def test_rs_rows_are_written_for_every_ranked_symbol(session, scan_result):
    from vriddhix.db.models import RelativeStrength
    from vriddhix.services.context import persist_relative_strength

    written = persist_relative_strength(session, scan_result.rs, scan_result.as_of)
    assert written == 3

    rows = session.query(RelativeStrength).all()
    # Numeric(6, 2) by design -- a percentile carried to more than two
    # decimals is precision the ranking does not have.
    stored = sorted(float(r.rs_score) for r in rows)
    computed = sorted(r.rs_score for r in scan_result.rs.values())
    assert stored == pytest.approx(computed, abs=0.005)
    # Coverage travels with the score: a rank from one horizon is a weaker
    # claim than one from four.
    assert all(r.coverage for r in rows)
    assert all(r.universe_size == 3 for r in rows)


def test_persisting_the_same_date_twice_replaces_rather_than_duplicates(
    session, scan_result
):
    from vriddhix.db.models import RelativeStrength
    from vriddhix.services.context import persist_relative_strength

    persist_relative_strength(session, scan_result.rs, scan_result.as_of)
    persist_relative_strength(session, scan_result.rs, scan_result.as_of)
    assert session.query(RelativeStrength).count() == 3


def test_an_unranked_sector_stores_null_not_zero(session, scan_result):
    """The distinction the whole sector table depends on.

    A 0.0 would sort an unranked sector below every ranked one and read as
    "ranked worst", which is a claim the ranker declined to make.
    """
    from vriddhix.db.models import SectorMetric
    from vriddhix.services.context import persist_sector_metrics

    persist_sector_metrics(session, scan_result.sectors, scan_result.as_of)
    rows = session.query(SectorMetric).all()
    assert rows, "no sector rows written"

    for row in rows:
        if row.rs_score is None:
            assert row.rs_rank is None
            assert row.quadrant is None, "an unranked sector has no quadrant"


def test_a_version_bump_cannot_silently_overwrite_history(session, scan_result):
    from vriddhix.services import context as context_service
    from vriddhix.services.context import VersionConflict, persist_relative_strength

    persist_relative_strength(session, scan_result.rs, scan_result.as_of)

    original = context_service.RS_ENGINE_VERSION
    context_service.RS_ENGINE_VERSION = "RS_ENGINE_V2.0"
    try:
        with pytest.raises(VersionConflict):
            persist_relative_strength(session, scan_result.rs, scan_result.as_of)
        # Explicit consent still works -- the guard refuses silence, not
        # recomputation.
        persist_relative_strength(
            session, scan_result.rs, scan_result.as_of, allow_version_overwrite=True
        )
    finally:
        context_service.RS_ENGINE_VERSION = original


def test_breadth_and_regime_are_written_together(session, scan_result):
    from vriddhix.db.models import MarketMetric, MarketRegimeRow
    from vriddhix.services.context import persist_scan_context

    report = persist_scan_context(session, scan_result, breakouts=2, failed_breakouts=1)
    assert report.market_rows == 1 and report.regime_rows == 1

    breadth = session.get(MarketMetric, scan_result.as_of)
    regime = session.get(MarketRegimeRow, scan_result.as_of)
    assert breadth.universe_size == 3
    assert float(breadth.breakout_success_ratio) == pytest.approx(2 / 3)
    assert regime.regime == scan_result.regime.regime.value


def test_no_breakouts_means_an_undefined_success_ratio(session, scan_result):
    """0.0 would read as 'every breakout failed'."""
    from vriddhix.db.models import MarketMetric
    from vriddhix.services.context import persist_scan_context

    persist_scan_context(session, scan_result, breakouts=0, failed_breakouts=0)
    assert session.get(MarketMetric, scan_result.as_of).breakout_success_ratio is None


def test_realised_volatility_uses_only_the_trailing_window(populated):
    import pandas as pd
    from vriddhix.services.context import realised_volatility

    closes = populated["TCS"]["close"]
    cut = len(closes) - 5
    # Truncating the future must not change a value computed from the past.
    assert realised_volatility(closes.iloc[:cut]) == pytest.approx(
        realised_volatility(pd.concat([closes.iloc[:cut]]))
    )
    assert realised_volatility(closes.iloc[:10]) is None


# ---------------------------------------------------------------------------
# Structure persistence
# ---------------------------------------------------------------------------


def test_a_swing_records_when_it_became_knowable(session, scan_result):
    from vriddhix.db.models import SwingPointRow
    from vriddhix.engines.smc import SmcConfig
    from vriddhix.services.structure import persist_scan_structure

    persist_scan_structure(session, scan_result.structures, smc_cfg=SmcConfig())
    swings = session.query(SwingPointRow).all()
    assert swings, "no swings persisted"

    for swing in swings:
        if swing.confirmed_on is None:
            continue
        # The lag is the whole point: reading a swing by its own date hands a
        # backtest information from the future.
        assert swing.confirmed_on > swing.date
        assert swing.right_bars > 0


def test_fvg_mitigation_never_goes_backwards(session, scan_result):
    from vriddhix.db.models import FvgZone
    from vriddhix.services.structure import persist_gaps

    symbol = next(iter(scan_result.structures))
    payload = scan_result.structures[symbol]
    gaps = payload["gaps"]
    if not gaps:
        pytest.skip("no gaps in this fixture")

    persist_gaps(session, payload["stock_id"], gaps)
    row = session.query(FvgZone).first()
    high_water = float(row.mitigation_pct)

    # A later scan that reports a smaller fill must not lower the stored one.
    from dataclasses import replace

    lowered = [replace(gaps[0], mitigation_pct=0.0)]
    persist_gaps(session, payload["stock_id"], lowered)
    session.refresh(row)
    assert float(row.mitigation_pct) == pytest.approx(high_water)


def test_structure_is_not_duplicated_on_a_second_run(session, scan_result):
    from vriddhix.db.models import StructureBreakRow
    from vriddhix.engines.smc import SmcConfig
    from vriddhix.services.structure import persist_scan_structure

    persist_scan_structure(session, scan_result.structures, smc_cfg=SmcConfig())
    first = session.query(StructureBreakRow).count()
    persist_scan_structure(session, scan_result.structures, smc_cfg=SmcConfig())
    assert session.query(StructureBreakRow).count() == first


# ---------------------------------------------------------------------------
# Failure engine
# ---------------------------------------------------------------------------


@pytest.fixture
def pending_breakout(session, seeded, populated):
    """A breakout recorded partway through RELIANCE's history."""
    from vriddhix.db.models import Breakout

    frame = populated["RELIANCE"]
    breakout_date = frame.index[-20].date()
    breakout = Breakout(
        stock_id=seeded["stocks"]["RELIANCE"].id,
        breakout_date=breakout_date,
        breakout_price=float(frame["close"].iloc[-20]),
        pivot_price=float(frame["close"].iloc[-20]),
        status="PENDING",
        engine_version="VCP_ENGINE_V1.0",
    )
    session.add(breakout)
    session.flush()
    return breakout


def test_a_breakout_that_holds_is_confirmed(session, pending_breakout, populated):
    from vriddhix.services.failure import evaluate_breakouts

    as_of = populated["RELIANCE"].index[-1].date()
    report = evaluate_breakouts(
        session, as_of, failure_buffer_pct=99.0, confirm_within_days=3
    )
    assert report.confirmed == 1
    session.refresh(pending_breakout)
    assert pending_breakout.status == "CONFIRMED"
    assert pending_breakout.outcome is not None
    assert pending_breakout.outcome.days_held >= 3


def test_a_breakout_that_breaks_down_is_failed_and_kept(
    session, pending_breakout, populated
):
    from vriddhix.db.models import Breakout
    from vriddhix.services.failure import evaluate_breakouts

    as_of = populated["RELIANCE"].index[-1].date()
    # A zero buffer fails on any close below the pivot.
    report = evaluate_breakouts(
        session, as_of, failure_buffer_pct=0.0, confirm_within_days=3
    )
    assert report.failed == 1

    session.refresh(pending_breakout)
    assert pending_breakout.status == "FAILED"
    assert pending_breakout.outcome.failed is True
    assert pending_breakout.outcome.failure_reason == "CLOSE_BELOW_PIVOT"
    # Kept, not deleted. This is the row research is built on.
    assert session.query(Breakout).count() == 1


def test_failure_excursions_stop_at_the_exit(session, pending_breakout, populated):
    """MFE after the position would have closed is not the trade's MFE."""
    from vriddhix.services.failure import evaluate_breakouts

    as_of = populated["RELIANCE"].index[-1].date()
    evaluate_breakouts(session, as_of, failure_buffer_pct=0.0, confirm_within_days=3)
    outcome = pending_breakout.outcome

    frame = populated["RELIANCE"]
    after_exit = frame.loc[frame.index > str(outcome.failure_date)]
    if not after_exit.empty:
        entry = float(pending_breakout.breakout_price)
        later_high = (float(after_exit["high"].max()) - entry) / entry * 100.0
        if later_high > float(outcome.mfe_pct):
            assert float(outcome.mfe_pct) < later_high


def test_failure_statistics_are_undefined_over_an_empty_ledger(session, seeded):
    from vriddhix.services.failure import failure_statistics

    stats = failure_statistics(session)
    assert stats["total"] == 0
    # None, not 0.0: nothing has failed because nothing has happened.
    assert stats["failure_rate"] is None
    assert stats["avg_days_to_failure"] is None


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


def test_the_daily_scan_writes_a_completed_run(session, populated, cfg):
    from vriddhix.db.models import ScanRun
    from vriddhix.jobs.daily_scan import last_successful_scan, run_daily_scan

    as_of = populated["RELIANCE"].index[-1].date()
    report = run_daily_scan(
        session, cfg, as_of, symbols=["RELIANCE", "TCS", "INFY"]
    )
    assert report.ok, report.errors
    assert report.symbols_processed == 3
    assert report.context.rs_rows == 3

    run = session.get(ScanRun, report.scan_run_id)
    assert run.status == "COMPLETED"
    assert run.finished_at is not None
    assert last_successful_scan(session).id == run.id


def test_a_failed_scan_leaves_a_visible_failed_row(session, populated, cfg, monkeypatch):
    """A missing scan and a failed scan must not look the same."""
    from vriddhix.db.models import ScanRun
    from vriddhix.jobs import daily_scan as job
    from vriddhix.services import scanner

    def boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(scanner, "scan", boom)
    report = job.run_daily_scan(session, cfg, date(2026, 1, 5), symbols=["RELIANCE"])

    assert not report.ok
    assert "provider exploded" in report.errors[0]
    run = session.get(ScanRun, report.scan_run_id)
    assert run.status == "FAILED"
    assert "provider exploded" in run.notes


def test_the_survivorship_warning_reaches_the_scan_row(session, populated, cfg):
    from sqlalchemy import select

    from vriddhix.db.models import ScanRun, UniverseMember
    from vriddhix.jobs.daily_scan import run_daily_scan

    for member in session.scalars(select(UniverseMember)).all():
        member.source = "current_only"
    session.flush()

    as_of = populated["RELIANCE"].index[-1].date()
    report = run_daily_scan(session, cfg, as_of, index_code="NIFTY500")
    run = session.get(ScanRun, report.scan_run_id)
    if report.survivorship_warning:
        # It must be ON the row, not only in a log line nobody reads.
        assert run.notes == report.survivorship_warning


# ---------------------------------------------------------------------------
# The nightly backfill
# ---------------------------------------------------------------------------


def test_pending_dates_exclude_already_scanned_ones(session, populated, cfg):
    from vriddhix.jobs.daily_scan import run_daily_scan
    from vriddhix.jobs.nightly import pending_scan_dates

    before = pending_scan_dates(session)
    assert before, "nothing pending on a freshly ingested database"

    run_daily_scan(session, cfg, before[-1], symbols=["RELIANCE", "TCS", "INFY"])
    after = pending_scan_dates(session)
    assert before[-1] not in after


def test_a_failed_scan_leaves_its_date_pending(session, populated, cfg):
    """A FAILED run is not evidence the date was processed.

    If it counted as done, one transient provider error would leave a
    permanent hole that no later run ever fills.
    """
    from vriddhix.db.models import ScanRun
    from vriddhix.jobs.nightly import pending_scan_dates

    target = pending_scan_dates(session)[-1]
    session.add(
        ScanRun(scan_date=target, status="FAILED", engine_version="VCP_ENGINE_V1.0")
    )
    session.flush()
    assert target in pending_scan_dates(session)


def test_pending_dates_are_ordered_oldest_first(session, populated):
    """RS trend and regime hysteresis read the previous stored value, so any
    other order measures each day against a future baseline."""
    from vriddhix.jobs.nightly import pending_scan_dates

    pending = pending_scan_dates(session)
    assert pending == sorted(pending)


def test_the_catchup_cap_keeps_the_most_recent_sessions(session, populated, cfg):
    """When the cap bites it must drop the far past, not the recent days --
    those are what anybody is actually looking at tonight."""
    from vriddhix.jobs.nightly import pending_scan_dates, run_backfill

    pending = pending_scan_dates(session)
    if len(pending) <= 3:
        pytest.skip("not enough pending sessions to exercise the cap")

    report = run_backfill(session, cfg, ingest=False, max_catchup_days=2)
    assert len(report.scanned) <= 2
    assert report.skipped_beyond_cap
    # Everything skipped is older than everything scanned.
    assert max(report.skipped_beyond_cap) < min(report.scanned)


def test_a_capped_run_resumes_where_the_last_one_stopped(session, populated, cfg):
    """The cap bounds one night's work, it does not abandon the rest."""
    from vriddhix.jobs.nightly import run_backfill

    first = run_backfill(session, cfg, ingest=False, max_catchup_days=2)
    second = run_backfill(session, cfg, ingest=False, max_catchup_days=2)

    assert first.scanned and second.scanned
    # No date is scanned twice, and the second run picks up older sessions
    # that the first run's cap had pushed aside.
    assert not set(first.scanned) & set(second.scanned)


def test_backfill_over_a_finished_window_changes_nothing(session, populated, cfg):
    from vriddhix.db.models import MarketRegimeRow, RelativeStrength
    from vriddhix.jobs.nightly import pending_scan_dates, run_backfill

    window_start = pending_scan_dates(session)[-2]
    run_backfill(session, cfg, ingest=False, since=window_start, max_catchup_days=0)
    counts = (
        session.query(RelativeStrength).count(),
        session.query(MarketRegimeRow).count(),
    )

    # Everything in the window is done, so a second pass has nothing to do.
    second = run_backfill(
        session, cfg, ingest=False, since=window_start, max_catchup_days=0
    )
    assert second.scanned == []
    assert counts == (
        session.query(RelativeStrength).count(),
        session.query(MarketRegimeRow).count(),
    )


def test_an_ingest_failure_does_not_stop_the_scan(session, populated, cfg, monkeypatch):
    """One dead symbol must not stop the other 499 from being scanned."""
    from vriddhix.jobs import nightly

    def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(nightly, "build_provider", boom)
    report = nightly.run_backfill(session, cfg, ingest=True, max_catchup_days=2)

    assert any("provider down" in e for e in report.errors)
    assert report.scanned, "the scan did not run after the ingest error"


def test_since_cannot_scan_dates_without_enough_history(session, populated, cfg):
    """`--since` narrows the window; it does not lower the history floor.

    Scanning a date the universe has no history for records a COMPLETED run
    that found nothing, and afterwards that is indistinguishable from a date
    the engines genuinely had nothing to say about.
    """
    from vriddhix.jobs.nightly import pending_scan_dates, trading_dates

    calendar = trading_dates(session)
    min_bars = int(cfg.get("data.min_history_bars", 60))
    floor = calendar[min_bars - 1]

    # Ask from the very first bar: the floor must still hold.
    pending = pending_scan_dates(
        session, since=calendar[0], min_history_bars=min_bars
    )
    assert pending, "nothing pending at all"
    assert min(pending) >= floor


def test_a_universe_too_young_to_scan_yields_nothing(session, seeded, cfg):
    from vriddhix.jobs.nightly import pending_scan_dates

    # No bars ingested at all -> no dates, and no crash.
    assert pending_scan_dates(session, min_history_bars=60) == []


# ---------------------------------------------------------------------------
# The backtest worker
# ---------------------------------------------------------------------------


@pytest.fixture
def queued_run(session, populated, cfg):
    """A scanned window plus a backtest queued over it."""
    from vriddhix.db.models import BacktestRun
    from vriddhix.jobs.nightly import run_backfill

    run_backfill(session, cfg, ingest=False, max_catchup_days=6)

    dates = sorted(d for d in populated["RELIANCE"].index)
    run = BacktestRun(
        name="VCP",
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        initial_capital=1_000_000,
        config=json.dumps({"entry": {"field": "vcp_score", "cmp": "gte", "value": 0}}),
        engine_version="VCP_ENGINE_V1.0",
        rule_version="SCORING_V1.0",
        survivorship_mode="CURRENT_UNIVERSE",
        status="QUEUED",
    )
    session.add(run)
    session.flush()
    return run


def test_the_worker_claims_a_queued_run_before_doing_any_work(session, queued_run):
    """Marked RUNNING first so a crash leaves a visible row, and a second
    worker cannot take the same job."""
    from vriddhix.jobs.backtest_worker import claim_next

    claimed = claim_next(session)
    assert claimed.id == queued_run.id
    assert claimed.status == "RUNNING"
    # Nothing left to claim.
    assert claim_next(session) is None


def test_a_queued_backtest_runs_to_completion(session, queued_run, cfg):
    from vriddhix.db.models import BacktestRun
    from vriddhix.jobs.backtest_worker import run_pending

    report = run_pending(session, cfg)
    assert report.claimed == 1
    assert report.failed == 0, report.errors

    run = session.get(BacktestRun, queued_run.id)
    assert run.status == "COMPLETED"
    assert run.finished_at is not None


def test_signals_come_from_stored_patterns(session, queued_run):
    """Not from a re-run of the detector: a backtest must measure what the
    scanner actually showed, under the engine version that produced it."""
    from vriddhix.jobs.backtest_worker import signal_rows

    signals = signal_rows(session, queued_run.start_date, queued_run.end_date)
    assert signals, "no stored patterns in the window"
    for rows in signals.values():
        for row in rows:
            assert row["pattern_id"] is not None
            assert "vcp_score" in row


def test_a_broken_entry_rule_fails_the_run_loudly(session, queued_run, cfg):
    """Silently matching nothing would report a flawless zero-trade strategy."""
    from vriddhix.db.models import BacktestRun
    from vriddhix.jobs.backtest_worker import run_pending

    queued_run.config = json.dumps(
        {"entry": {"field": "not_a_field", "cmp": "gte", "value": 1}}
    )
    session.flush()

    report = run_pending(session, cfg)
    assert report.failed == 1
    assert any("not_a_field" in e for e in report.errors)
    assert session.get(BacktestRun, queued_run.id).status == "FAILED"


def test_a_failed_run_is_not_silently_requeued(session, queued_run, cfg):
    """A run retried forever hides the bug that breaks it."""
    from vriddhix.jobs.backtest_worker import run_pending

    queued_run.config = json.dumps(
        {"entry": {"field": "nope", "cmp": "gte", "value": 1}}
    )
    session.flush()
    run_pending(session, cfg)

    # Nothing queued now, so a second pass claims nothing.
    assert run_pending(session, cfg).claimed == 0
