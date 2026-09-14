"""Ingestion: providers -> validation -> database.

Two properties this pipeline guarantees:

**Idempotence.** Re-running for the same symbol and window converges to the
same rows. A nightly job that has to be re-run after a partial failure must
not double-count volume or leave a mixture of two vintages.

**Partial failure is normal.** One symbol failing is one symbol failing, not a
scan aborting. Failures land in ``data_quality_events`` with the provider and
the reason, so the morning question "what didn't we get?" has an answer in
SQL rather than in a log file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..domain.types import DataIssue, QualityFinding, Severity
from ..db.models import DataQualityEvent, OhlcvDaily, Stock, TechnicalFeature
from ..features.technical import compute_features_from_config
from ..versioning import FEATURES_VERSION
from .providers import ChainedProvider, ProviderError
from .quality import validate_from_config

logger = logging.getLogger(__name__)


@dataclass
class SymbolResult:
    symbol: str
    ok: bool
    bars_written: int = 0
    features_written: int = 0
    findings: list[QualityFinding] = field(default_factory=list)
    error: str | None = None


@dataclass
class IngestReport:
    started_at: datetime
    finished_at: datetime | None = None
    results: list[SymbolResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[SymbolResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[SymbolResult]:
        return [r for r in self.results if not r.ok]

    @property
    def bars_written(self) -> int:
        return sum(r.bars_written for r in self.results)

    @property
    def features_written(self) -> int:
        return sum(r.features_written for r in self.results)

    def summary(self) -> str:
        return (
            f"{len(self.succeeded)}/{len(self.results)} symbols, "
            f"{self.bars_written} bars, {self.features_written} feature rows, "
            f"{sum(len(r.findings) for r in self.results)} quality findings"
        )


def _record_findings(session: Session, stock_id: int | None, findings: list[QualityFinding],
                     provider: str | None = None) -> None:
    for finding in findings:
        session.add(
            DataQualityEvent(
                stock_id=stock_id,
                date=finding.bar_date,
                severity=finding.severity.value,
                category=finding.issue.value,
                detail=json.dumps(finding.detail) if finding.detail else None,
                provider=provider or finding.detail.get("provider"),
            )
        )


def _write_bars(session: Session, stock: Stock, df: pd.DataFrame, provider: str) -> int:
    """Replace the affected window rather than upserting row by row.

    Delete-then-insert over the exact date range is both faster and safer than
    per-row merges: it cannot leave a mixture of an old vintage and a new one
    in the same window, which is the failure that makes a re-run produce
    different numbers from a first run.
    """
    if df.empty:
        return 0

    first, last = df.index[0].date(), df.index[-1].date()
    session.execute(
        delete(OhlcvDaily).where(
            OhlcvDaily.stock_id == stock.id,
            OhlcvDaily.date >= first,
            OhlcvDaily.date <= last,
        )
    )

    has_adj = "adj_close" in df.columns
    session.bulk_save_objects(
        [
            OhlcvDaily(
                stock_id=stock.id,
                date=ts.date(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume) if pd.notna(row.volume) else 0.0,
                adj_close=float(getattr(row, "adj_close")) if has_adj and pd.notna(getattr(row, "adj_close")) else None,
                provider=provider,
            )
            for ts, row in zip(df.index, df.itertuples(index=False))
        ]
    )
    return len(df)


def _write_features(session: Session, stock: Stock, features: pd.DataFrame) -> int:
    if features.empty:
        return 0

    first, last = features.index[0].date(), features.index[-1].date()
    session.execute(
        delete(TechnicalFeature).where(
            TechnicalFeature.stock_id == stock.id,
            TechnicalFeature.date >= first,
            TechnicalFeature.date <= last,
        )
    )

    def val(row, name):
        if name not in features.columns:
            return None
        value = row[name]
        return None if pd.isna(value) else float(value)

    def flag(row, name):
        if name not in features.columns:
            return None
        value = row[name]
        return None if pd.isna(value) else bool(value)

    rows = []
    for ts, row in features.iterrows():
        rows.append(
            TechnicalFeature(
                stock_id=stock.id,
                date=ts.date(),
                ema_20=val(row, "ema_20"), ema_50=val(row, "ema_50"),
                ema_100=val(row, "ema_100"), ema_200=val(row, "ema_200"),
                atr_14=val(row, "atr_14"), atr_pct=val(row, "atr_pct"),
                avg_volume_20=val(row, "avg_volume_20"),
                avg_volume_50=val(row, "avg_volume_50"),
                rel_volume=val(row, "rel_volume"),
                ret_1m=val(row, "ret_1m"), ret_3m=val(row, "ret_3m"),
                ret_6m=val(row, "ret_6m"), ret_12m=val(row, "ret_12m"),
                high_52w=val(row, "high_52w"), low_52w=val(row, "low_52w"),
                pct_from_52w_high=val(row, "pct_from_52w_high"),
                pct_from_52w_low=val(row, "pct_from_52w_low"),
                above_ema_20=flag(row, "above_ema_20"),
                above_ema_50=flag(row, "above_ema_50"),
                above_ema_100=flag(row, "above_ema_100"),
                above_ema_200=flag(row, "above_ema_200"),
                feature_version=FEATURES_VERSION,
            )
        )
    session.bulk_save_objects(rows)
    return len(rows)


def ingest_symbol(
    session: Session,
    symbol: str,
    provider: ChainedProvider,
    cfg,
    *,
    start: date | None = None,
    end: date | None = None,
    as_of: date | None = None,
    compute_features: bool = True,
) -> SymbolResult:
    """Fetch, validate, persist bars and features for one symbol."""
    stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
    if stock is None:
        return SymbolResult(symbol=symbol, ok=False, error="symbol not in stocks table")

    try:
        raw = provider.fetch(symbol, start, end)
    except ProviderError as exc:
        _record_findings(
            session, stock.id,
            [QualityFinding(
                issue=DataIssue.PROVIDER_ERROR, severity=Severity.ERROR,
                symbol=symbol, detail={"error": str(exc)},
            )],
        )
        return SymbolResult(symbol=symbol, ok=False, error=str(exc))

    source = provider.last_source or "unknown"
    cleaned, findings = validate_from_config(raw, symbol, cfg, as_of=as_of)

    # Provider-level findings accumulated during failover belong to this
    # symbol too -- they explain why the answer came from the fallback.
    chain_findings = [f for f in provider.findings if f.symbol == symbol]
    provider.findings = [f for f in provider.findings if f.symbol != symbol]
    findings = chain_findings + findings

    _record_findings(session, stock.id, findings, provider=source)

    min_bars = int(cfg.get("data.min_history_bars", 60))
    if len(cleaned) < min_bars:
        # Refuse rather than compute from a stub: features over 12 bars are
        # arithmetically valid and analytically meaningless, and nothing
        # downstream would be able to tell the difference.
        message = f"only {len(cleaned)} usable bars, minimum is {min_bars}"
        _record_findings(
            session, stock.id,
            [QualityFinding(
                issue=DataIssue.MISSING_BAR, severity=Severity.ERROR,
                symbol=symbol, detail={"usable_bars": len(cleaned), "minimum": min_bars},
            )],
            provider=source,
        )
        return SymbolResult(symbol=symbol, ok=False, findings=findings, error=message)

    bars_written = _write_bars(session, stock, cleaned, source)

    features_written = 0
    if compute_features:
        frame = compute_features_from_config(cleaned, cfg, symbol=symbol)
        features_written = _write_features(session, stock, frame)

    return SymbolResult(
        symbol=symbol,
        ok=True,
        bars_written=bars_written,
        features_written=features_written,
        findings=findings,
    )


def ingest_universe(
    session: Session,
    symbols: list[str],
    provider: ChainedProvider,
    cfg,
    *,
    start: date | None = None,
    end: date | None = None,
    as_of: date | None = None,
    compute_features: bool = True,
) -> IngestReport:
    """Ingest many symbols. One failure does not stop the rest."""
    report = IngestReport(started_at=datetime.now())

    for symbol in symbols:
        try:
            result = ingest_symbol(
                session, symbol, provider, cfg,
                start=start, end=end, as_of=as_of, compute_features=compute_features,
            )
        except Exception as exc:  # noqa: BLE001 - one symbol must not stop the run
            logger.exception("unhandled error ingesting %s", symbol)
            result = SymbolResult(symbol=symbol, ok=False, error=f"{type(exc).__name__}: {exc}")
        report.results.append(result)

    report.finished_at = datetime.now()
    logger.info("ingest complete: %s", report.summary())
    return report
