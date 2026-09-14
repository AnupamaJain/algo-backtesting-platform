"""Data validation.

The governing rule: **nothing is silently discarded, and nothing is silently
repaired.** Every problem produces a ``QualityFinding``. Fatal problems
(ERROR) remove the offending bar; everything else keeps the bar and records
the concern, because most "bad" bars are real market events.

The clearest example is zero volume. It looks like corruption and is usually a
circuit halt -- a genuine session in which nothing traded. Deleting it would
fabricate a gap in the series that never existed, which is a worse lie than
the anomaly.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from ..domain.types import DataIssue, QualityFinding, Severity


def check_duplicates(df: pd.DataFrame, symbol: str) -> tuple[pd.DataFrame, list[QualityFinding]]:
    """Drop duplicate timestamps, keeping the last.

    Providers re-send a partial bar before the final print; the later row is
    the settled one. Keeping both would double-count volume and corrupt every
    rolling window downstream.

    "Last" means last in the frame's current row order, so callers must not
    have reordered it with an unstable sort beforehand. ``normalise_price_frame``
    is careful about this: it de-duplicates before sorting, not after.
    """
    if not df.index.has_duplicates:
        return df, []

    dupes = df.index[df.index.duplicated()].unique()
    findings = [
        QualityFinding(
            issue=DataIssue.DUPLICATE,
            severity=Severity.WARN,
            symbol=symbol,
            bar_date=ts.date(),
            detail={"kept": "last"},
        )
        for ts in dupes
    ]
    return df[~df.index.duplicated(keep="last")], findings


def check_ohlc_sanity(df: pd.DataFrame, symbol: str) -> tuple[pd.DataFrame, list[QualityFinding]]:
    """Reject structurally impossible bars.

    A bar where ``high < low`` or the close sits outside the range is not a
    market event -- it is corruption, and it produces a negative true range
    that poisons ATR for the next fourteen bars.
    """
    findings: list[QualityFinding] = []

    impossible = (
        (df["high"] < df["low"])
        | (df["high"] < df[["open", "close"]].max(axis=1))
        | (df["low"] > df[["open", "close"]].min(axis=1))
    )
    negative = (df[["open", "high", "low", "close"]] <= 0).any(axis=1)

    for ts in df.index[impossible]:
        row = df.loc[ts]
        findings.append(
            QualityFinding(
                issue=DataIssue.INVALID_OHLC,
                severity=Severity.ERROR,
                symbol=symbol,
                bar_date=ts.date(),
                detail={
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                },
            )
        )
    for ts in df.index[negative & ~impossible]:
        findings.append(
            QualityFinding(
                issue=DataIssue.NEGATIVE_PRICE,
                severity=Severity.ERROR,
                symbol=symbol,
                bar_date=ts.date(),
            )
        )

    return df[~(impossible | negative)], findings


def check_zero_volume(df: pd.DataFrame, symbol: str, severity: Severity = Severity.WARN) -> list[QualityFinding]:
    """Flag zero-volume bars. Kept, not dropped -- see module docstring."""
    return [
        QualityFinding(
            issue=DataIssue.ZERO_VOLUME,
            severity=severity,
            symbol=symbol,
            bar_date=ts.date(),
        )
        for ts in df.index[df["volume"].fillna(0) <= 0]
    ]


def check_suspected_splits(
    df: pd.DataFrame, symbol: str, threshold_pct: float = 25.0
) -> list[QualityFinding]:
    """Flag overnight gaps large enough to suggest an unadjusted corporate action.

    A 1:5 split shows up as an apparent -80% move. Left unflagged it becomes a
    catastrophic 'drawdown' in a backtest and a spurious 52-week low in a
    screen. This does not attempt to *correct* it -- a guessed adjustment
    factor is its own silent corruption. It raises the flag so a human or a
    corporate-actions feed can resolve it.
    """
    if len(df) < 2:
        return []

    change = (df["close"] / df["close"].shift(1) - 1.0) * 100.0
    suspects = change.abs() >= threshold_pct

    return [
        QualityFinding(
            issue=DataIssue.SUSPECTED_SPLIT,
            severity=Severity.WARN,
            symbol=symbol,
            bar_date=ts.date(),
            detail={
                "change_pct": round(float(change.loc[ts]), 2),
                "prev_close": float(df["close"].shift(1).loc[ts]),
                "close": float(df["close"].loc[ts]),
            },
        )
        for ts in df.index[suspects.fillna(False)]
    ]


def check_staleness(
    df: pd.DataFrame, symbol: str, as_of: date, stale_after_days: int = 5
) -> list[QualityFinding]:
    """Flag a series whose most recent bar is too old to trust as current."""
    if df.empty:
        return []
    last = df.index[-1].date()
    age = (as_of - last).days
    if age <= stale_after_days:
        return []
    return [
        QualityFinding(
            issue=DataIssue.STALE,
            severity=Severity.WARN,
            symbol=symbol,
            bar_date=last,
            detail={"age_days": age, "as_of": as_of.isoformat()},
        )
    ]


def check_missing_bars(
    df: pd.DataFrame, symbol: str, max_streak: int = 5
) -> list[QualityFinding]:
    """Flag runs of consecutive absent weekdays.

    Deliberately weekday-based rather than holiday-calendar-based: an NSE
    holiday calendar that is itself wrong would generate false findings
    forever. A run longer than ``max_streak`` weekdays exceeds any Indian
    market holiday cluster and is worth a human look.
    """
    if len(df) < 2:
        return []

    findings: list[QualityFinding] = []
    dates = df.index.date
    for prev, curr in zip(dates[:-1], dates[1:]):
        gap_weekdays = sum(
            1
            for offset in range(1, (curr - prev).days)
            if (prev + timedelta(days=offset)).weekday() < 5
        )
        if gap_weekdays > max_streak:
            findings.append(
                QualityFinding(
                    issue=DataIssue.MISSING_BAR,
                    severity=Severity.WARN,
                    symbol=symbol,
                    bar_date=curr,
                    detail={"missing_weekdays": gap_weekdays, "previous_bar": prev.isoformat()},
                )
            )
    return findings


def validate(
    df: pd.DataFrame,
    symbol: str,
    *,
    as_of: date | None = None,
    suspected_split_pct: float = 25.0,
    zero_volume_severity: Severity = Severity.WARN,
    stale_after_days: int = 5,
    max_missing_bar_streak: int = 5,
) -> tuple[pd.DataFrame, list[QualityFinding]]:
    """Run every rule. Returns the cleaned frame and everything found.

    Order matters: structural repairs (duplicates, impossible bars) come
    first, so that the statistical checks downstream are not computed against
    rows that are about to be removed.
    """
    findings: list[QualityFinding] = []

    df, dupe_findings = check_duplicates(df, symbol)
    findings.extend(dupe_findings)

    df, ohlc_findings = check_ohlc_sanity(df, symbol)
    findings.extend(ohlc_findings)

    if df.empty:
        return df, findings

    findings.extend(check_zero_volume(df, symbol, zero_volume_severity))
    findings.extend(check_suspected_splits(df, symbol, suspected_split_pct))
    findings.extend(check_missing_bars(df, symbol, max_missing_bar_streak))
    if as_of is not None:
        findings.extend(check_staleness(df, symbol, as_of, stale_after_days))

    return df, findings


def validate_from_config(
    df: pd.DataFrame, symbol: str, cfg, *, as_of: date | None = None
) -> tuple[pd.DataFrame, list[QualityFinding]]:
    quality = cfg.section("quality")
    return validate(
        df,
        symbol,
        as_of=as_of,
        suspected_split_pct=float(quality["suspected_split_pct"]),
        zero_volume_severity=Severity(quality["zero_volume_severity"]),
        stale_after_days=int(quality["stale_after_days"]),
        max_missing_bar_streak=int(quality["max_missing_bar_streak"]),
    )
