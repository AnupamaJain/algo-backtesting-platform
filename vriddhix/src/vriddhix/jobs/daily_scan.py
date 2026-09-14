"""The daily scan. Phase 8.

One run, in this order, because each step depends on the one before it:

1. open a ``scan_runs`` row so a crashed run is visible as RUNNING rather than
   as an absence
2. run every engine across the universe (``services.scanner.scan``)
3. persist patterns and advance the lifecycle
4. resolve open breakouts -- confirm or fail them
5. persist market context (RS, sector, breadth, regime)
6. evaluate alerts
7. close the run with counts

Step 4 comes before step 5 because breadth records the day's breakout and
failure counts, and those are only known once breakouts have been resolved.

The run is transactional at the step level: a failure after step 2 leaves the
scan row marked FAILED with the error text, and the partial writes are rolled
back. A half-written scan that looks complete is worse than an obvious
failure, because every number downstream of it inherits the gap silently.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ScanRun, UniverseSnapshot
from ..services import context as context_service
from ..services import failure as failure_service
from ..services import structure as structure_service
from ..services import lifecycle, scanner
from ..services.alerts import evaluate_alerts
from ..versioning import VCP_ENGINE_VERSION

logger = logging.getLogger(__name__)


@dataclass
class DailyScanReport:
    scan_run_id: int | None = None
    as_of: date | None = None
    status: str = "RUNNING"
    symbols_processed: int = 0
    symbols_skipped: int = 0
    patterns_detected: int = 0
    structure_rows: int = 0
    breakouts_detected: int = 0
    breakouts_failed: int = 0
    breakouts_confirmed: int = 0
    alerts_triggered: int = 0
    context: context_service.ContextReport | None = None
    survivorship_warning: str | None = None
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "COMPLETED"


def _snapshot_id(session: Session, resolution) -> int | None:
    """The universe snapshot this scan ran against, if one is recorded."""
    if resolution is None or resolution.snapshot_version is None:
        return None
    # version is unique across the table, so it identifies the snapshot on
    # its own -- no join through indices needed.
    return session.scalar(
        select(UniverseSnapshot.id).where(
            UniverseSnapshot.version == resolution.snapshot_version
        )
    )


def run_daily_scan(
    session: Session,
    cfg,
    as_of: date,
    *,
    index_code: str | None = None,
    symbols: list[str] | None = None,
    with_structure: bool = True,
    evaluate_alerts_too: bool = True,
) -> DailyScanReport:
    """Run the full pipeline for one date."""
    started = time.perf_counter()
    report = DailyScanReport(as_of=as_of)

    run = ScanRun(
        scan_date=as_of,
        status="RUNNING",
        engine_version=VCP_ENGINE_VERSION,
    )
    session.add(run)
    session.flush()
    report.scan_run_id = run.id

    try:
        result = scanner.scan(
            session, cfg, as_of,
            index_code=index_code, symbols=symbols, with_structure=with_structure,
        )
        report.symbols_processed = len(result.rows)
        report.symbols_skipped = len(result.skipped)
        report.survivorship_warning = result.survivorship_warning
        if result.survivorship_warning:
            # Recorded on the run, not only logged: a scan whose universe was
            # approximated must say so where the results are read.
            run.notes = result.survivorship_warning

        if result.structures:
            from ..engines.smc import SmcConfig

            structure_report = structure_service.persist_scan_structure(
                session, result.structures, smc_cfg=SmcConfig.from_config(cfg)
            )
            report.structure_rows = (
                structure_report.swings + structure_report.breaks
                + structure_report.order_blocks + structure_report.liquidity
                + structure_report.gaps
            )

        lifecycle_report = lifecycle.record_scan(session, result.rows, as_of)
        report.patterns_detected = lifecycle_report.created + lifecycle_report.advanced
        report.breakouts_detected = lifecycle_report.breakouts

        vcp_cfg = cfg.section("vcp")
        failure_report = failure_service.evaluate_breakouts(
            session, as_of,
            failure_buffer_pct=float(vcp_cfg.get("failure_buffer_pct", 2)),
            confirm_within_days=int(vcp_cfg.get("confirm_within_days", 3)),
        )
        report.breakouts_failed = failure_report.failed
        report.breakouts_confirmed = failure_report.confirmed

        report.context = context_service.persist_scan_context(
            session, result,
            benchmark_index=index_code or cfg.get("universe.default_index"),
            breakouts=report.breakouts_detected,
            failed_breakouts=failure_report.failed,
        )

        if evaluate_alerts_too:
            alert_report = evaluate_alerts(session, result.rows, as_of)
            report.alerts_triggered = alert_report.triggered
            report.errors.extend(alert_report.errors)

        run.symbols_processed = report.symbols_processed
        run.symbols_failed = report.symbols_skipped
        run.patterns_detected = report.patterns_detected
        run.breakouts_detected = report.breakouts_detected
        run.universe_snapshot_id = _snapshot_id(session, result.universe)
        run.finished_at = datetime.now(timezone.utc)
        run.status = "COMPLETED"
        report.status = "COMPLETED"
        session.flush()

    except Exception as exc:  # noqa: BLE001
        session.rollback()
        # Re-attach a FAILED row after the rollback so the failure is a
        # visible record rather than a missing scan.
        failed = ScanRun(
            scan_date=as_of,
            status="FAILED",
            engine_version=VCP_ENGINE_VERSION,
            finished_at=datetime.now(timezone.utc),
            notes=f"{type(exc).__name__}: {exc}"[:2000],
        )
        session.add(failed)
        session.flush()
        report.scan_run_id = failed.id
        report.status = "FAILED"
        report.errors.append(f"{type(exc).__name__}: {exc}")
        logger.exception("daily scan for %s failed", as_of)

    report.duration_seconds = time.perf_counter() - started
    return report


def last_successful_scan(session: Session) -> ScanRun | None:
    """The most recent completed scan, which is what staleness is measured
    against. A RUNNING or FAILED row is not evidence of fresh data."""
    return session.scalar(
        select(ScanRun)
        .where(ScanRun.status == "COMPLETED")
        .order_by(ScanRun.scan_date.desc(), ScanRun.id.desc())
        .limit(1)
    )


def backfill(
    session: Session, cfg, dates: list[date], **kwargs
) -> list[DailyScanReport]:
    """Replay the pipeline across a list of dates, oldest first.

    Order matters: RS trend and regime hysteresis both read the previous
    stored value, so running dates out of order would compute today's regime
    against a future baseline.
    """
    reports = []
    for as_of in sorted(dates):
        reports.append(run_daily_scan(session, cfg, as_of, **kwargs))
    return reports


def summarise(report: DailyScanReport) -> str:
    """One line for a log or a CLI."""
    bits = [
        f"scan {report.as_of} {report.status}",
        f"{report.symbols_processed} scanned",
        f"{report.patterns_detected} patterns",
        f"{report.breakouts_detected} breakouts",
    ]
    if report.structure_rows:
        bits.append(f"{report.structure_rows} structure rows")
    if report.breakouts_failed or report.breakouts_confirmed:
        bits.append(
            f"{report.breakouts_confirmed} confirmed / {report.breakouts_failed} failed"
        )
    if report.alerts_triggered:
        bits.append(f"{report.alerts_triggered} alerts")
    if report.survivorship_warning:
        bits.append("BIASED UNIVERSE")
    bits.append(f"{report.duration_seconds:.1f}s")
    return " · ".join(bits)


__all__ = [
    "DailyScanReport",
    "backfill",
    "last_successful_scan",
    "run_daily_scan",
    "summarise",
]
