"""The nightly backfill. Phase 8.

One job, run after the close: fetch whatever bars are new, then scan every
trading date that has bars but no completed scan.

**It catches up rather than doing "today".** If the machine was asleep for a
week, this scans all five missing sessions, oldest first. A scheduler that
only ever processes the current date leaves a silent hole in the research
database for every day it did not run -- and a hole in regime history is
invisible until someone charts it months later and finds the line stops.

Order is not negotiable. RS trend and regime hysteresis both read the
previously stored value, so replaying dates out of order measures each day
against a future baseline. ``pending_scan_dates`` returns them sorted and
``run_backfill`` consumes them in that order.

The job is safe to run twice. Ingestion is delete-then-insert per symbol and
context persistence replaces a date's rows rather than appending, so a second
run over the same window changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..data.ingest import ingest_universe
from ..data.providers import build_provider
from ..db.base import session_scope
from ..db.models import OhlcvDaily, ScanRun, Stock
from .daily_scan import run_daily_scan, summarise

logger = logging.getLogger(__name__)

#: Never replay more than this many sessions in one run without being asked.
#: A machine off for a month would otherwise start a multi-hour job at 19:00
#: with nobody watching; the cap makes that a visible decision instead.
DEFAULT_MAX_CATCHUP_DAYS = 15


@dataclass
class BackfillReport:
    ingested_symbols: int = 0
    ingested_bars: int = 0
    scanned: list[date] = field(default_factory=list)
    failed: list[date] = field(default_factory=list)
    skipped_beyond_cap: list[date] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed and not self.errors


def scanned_dates(session: Session) -> set[date]:
    """Dates with a COMPLETED scan.

    A RUNNING or FAILED row is not evidence that a date was processed, so
    those dates are returned as pending and get another attempt.
    """
    return {
        d for (d,) in session.execute(
            select(ScanRun.scan_date).where(ScanRun.status == "COMPLETED").distinct()
        ).all()
    }


def trading_dates(session: Session, *, since: date | None = None) -> list[date]:
    """Every date the database holds bars for, oldest first."""
    stmt = select(OhlcvDaily.date).distinct().order_by(OhlcvDaily.date)
    if since is not None:
        stmt = stmt.where(OhlcvDaily.date >= since)
    return [d for (d,) in session.execute(stmt).all()]


def pending_scan_dates(
    session: Session, *, since: date | None = None, min_history_bars: int = 60
) -> list[date]:
    """Trading dates with bars but no completed scan, oldest first.

    Dates before the universe has ``min_history_bars`` of history are skipped:
    scanning them would produce a run that found nothing, for the uninteresting
    reason that no engine had enough data, and that empty run would then look
    like a real answer to every reader afterwards.
    """
    # The floor is computed from the WHOLE calendar, never from the slice
    # `since` selects. Deriving it from the slice would let `--since` scan
    # dates before the universe has enough history, and those runs record a
    # COMPLETED row that found nothing -- indistinguishable afterwards from a
    # date the engines genuinely had nothing to say about.
    calendar = trading_dates(session)
    if len(calendar) <= min_history_bars:
        return []

    floor = calendar[min_history_bars - 1]
    if since is not None:
        floor = max(floor, since)

    done = scanned_dates(session)
    return [d for d in calendar if d >= floor and d not in done]


def run_backfill(
    session: Session,
    cfg=None,
    *,
    since: date | None = None,
    ingest: bool = True,
    max_catchup_days: int = DEFAULT_MAX_CATCHUP_DAYS,
    index_code: str | None = None,
    with_structure: bool = True,
) -> BackfillReport:
    """Fetch new bars, then scan every date still missing a completed run."""
    cfg = cfg or get_config()
    report = BackfillReport(started_at=datetime.now())
    started = datetime.now()

    if ingest:
        _ingest_into(session, cfg, report)

    pending = pending_scan_dates(
        session, since=since,
        min_history_bars=int(cfg.get("data.min_history_bars", 60)),
    )
    pending, report.skipped_beyond_cap = _apply_cap(pending, max_catchup_days)

    for as_of in pending:
        scan_report = run_daily_scan(
            session, cfg, as_of,
            index_code=index_code, with_structure=with_structure,
        )
        logger.info("%s", summarise(scan_report))
        if scan_report.ok:
            report.scanned.append(as_of)
        else:
            report.failed.append(as_of)
            report.errors.extend(scan_report.errors)

    report.duration_seconds = (datetime.now() - started).total_seconds()
    return report


def run_once(
    cfg=None,
    *,
    since: date | None = None,
    ingest: bool = True,
    max_catchup_days: int = DEFAULT_MAX_CATCHUP_DAYS,
    index_code: str | None = None,
    with_structure: bool = True,
    progress_every: int = 25,
) -> BackfillReport:
    """Run the backfill, committing each session's work as it completes.

    **One transaction per scan date, not one for the whole run.** A decade of
    history is thousands of dates and hours of work; holding that in a single
    transaction means a failure at hour six discards hour one, and the
    session's identity map grows without bound for the whole time. Each date
    is already idempotent, so a per-date commit is the natural unit of
    durability -- an interrupted backfill resumes from where it stopped
    rather than from the beginning.
    """
    cfg = cfg or get_config()
    report = BackfillReport(started_at=datetime.now())
    started = datetime.now()

    if ingest:
        with session_scope() as session:
            _ingest_into(session, cfg, report)

    with session_scope() as session:
        pending = pending_scan_dates(
            session, since=since,
            min_history_bars=int(cfg.get("data.min_history_bars", 60)),
        )
    pending, report.skipped_beyond_cap = _apply_cap(pending, max_catchup_days)

    total = len(pending)
    for position, as_of in enumerate(pending, start=1):
        try:
            with session_scope() as session:
                scan_report = run_daily_scan(
                    session, cfg, as_of,
                    index_code=index_code, with_structure=with_structure,
                )
            if scan_report.ok:
                report.scanned.append(as_of)
            else:
                report.failed.append(as_of)
                report.errors.extend(scan_report.errors)
        except Exception as exc:  # noqa: BLE001
            # One bad date must not end the run: the remaining dates are
            # independent, and stopping would leave a hole that no later
            # run distinguishes from a date that was never due.
            report.failed.append(as_of)
            report.errors.append(f"{as_of}: {type(exc).__name__}: {exc}")
            logger.exception("scan for %s raised", as_of)

        if progress_every and position % progress_every == 0:
            elapsed = (datetime.now() - started).total_seconds()
            rate = elapsed / position
            logger.info(
                "backfill %d/%d (%s) · %.1fs/session · ~%.0f min remaining",
                position, total, as_of, rate, rate * (total - position) / 60,
            )

    report.duration_seconds = (datetime.now() - started).total_seconds()
    return report


def _apply_cap(
    pending: list[date], max_catchup_days: int
) -> tuple[list[date], list[date]]:
    """Bound one run's work, dropping the OLDEST pending dates.

    The recent sessions are what anybody is looking at tonight, so the cap
    trims the far past; each run then scans today first and works backwards,
    and the archive fills in on its own.
    """
    if not max_catchup_days or len(pending) <= max_catchup_days:
        return pending, []
    skipped = pending[:-max_catchup_days]
    logger.warning(
        "%d dates pending, capped to %d. Run `vriddhix backfill --since %s "
        "--max-catchup 0` to replay the rest.",
        len(pending), max_catchup_days, skipped[0],
    )
    return pending[-max_catchup_days:], skipped


def _ingest_into(session: Session, cfg, report: BackfillReport) -> None:
    """Fetch bars for every known symbol, recording failures without raising."""
    try:
        symbols = [s for (s,) in session.execute(select(Stock.symbol)).all()]
        if not symbols:
            return
        years = int(cfg.get("data.lookback_years", 10))
        start = date.today() - timedelta(days=int(years * 365.25))
        result = ingest_universe(
            session, symbols, build_provider(cfg), cfg,
            start=start, as_of=date.today(),
        )
        report.ingested_symbols = len(result.succeeded)
        report.ingested_bars = result.bars_written
        for failure in result.failed:
            # Recorded, not fatal: one dead symbol must not stop the other
            # 499 from being scanned.
            report.errors.append(f"ingest {failure.symbol}: {failure.error}")
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"ingest failed: {type(exc).__name__}: {exc}")
        logger.exception("nightly ingest failed")


def describe(report: BackfillReport) -> str:
    bits = []
    if report.ingested_symbols:
        bits.append(f"ingested {report.ingested_symbols} symbols / {report.ingested_bars:,} bars")
    if report.scanned:
        bits.append(f"scanned {len(report.scanned)} sessions ({report.scanned[0]} .. {report.scanned[-1]})")
    else:
        bits.append("nothing to scan")
    if report.failed:
        bits.append(f"{len(report.failed)} FAILED")
    if report.skipped_beyond_cap:
        bits.append(f"{len(report.skipped_beyond_cap)} skipped beyond cap")
    bits.append(f"{report.duration_seconds:.0f}s")
    return " · ".join(bits)


# ---------------------------------------------------------------------------
# Resident scheduler
# ---------------------------------------------------------------------------


def serve_scheduler(
    hour: int = 19, minute: int = 0, *, timezone: str = "Asia/Kolkata", **kwargs
) -> None:
    """Run the backfill on a cron schedule until interrupted.

    Weekdays only, after the NSE close plus settling time. This is the
    long-running alternative to launchd; both call the same ``run_once``, so
    they cannot diverge in what a "nightly run" means.
    """
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone=timezone)

    def job() -> None:
        try:
            report = run_once(**kwargs)
            logger.info("nightly backfill: %s", describe(report))
        except Exception:  # noqa: BLE001
            # Never let one failure kill the scheduler: tomorrow's run is
            # what fills tonight's hole.
            logger.exception("nightly backfill raised")

    scheduler.add_job(
        job, "cron", day_of_week="mon-fri", hour=hour, minute=minute,
        id="vriddhix-nightly-backfill", misfire_grace_time=3600,
        coalesce=True,  # one run after a sleep, not one per missed trigger
    )
    logger.info(
        "scheduler up: weekdays %02d:%02d %s (Ctrl-C to stop)", hour, minute, timezone
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler stopped")


__all__ = [
    "BackfillReport",
    "describe",
    "pending_scan_dates",
    "run_backfill",
    "run_once",
    "serve_scheduler",
    "trading_dates",
]
