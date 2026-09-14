"""Data quality rules.

Each rule gets a test in both directions: it fires when it should, and stays
quiet when it should not. A validator that never fires is indistinguishable
from one that is not wired in.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from vriddhix.data.quality import (
    check_duplicates,
    check_missing_bars,
    check_ohlc_sanity,
    check_staleness,
    check_suspected_splits,
    check_zero_volume,
    validate,
)
from vriddhix.domain.types import DataIssue, Severity
from conftest import make_ohlcv


def issues(findings) -> set[DataIssue]:
    return {f.issue for f in findings}


# ---------------------------------------------------------------------------
# Clean data stays quiet
# ---------------------------------------------------------------------------


def test_clean_frame_produces_no_findings():
    df = make_ohlcv(200, seed=1)
    cleaned, findings = validate(df, "CLEAN")
    assert findings == []
    assert len(cleaned) == len(df)


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_duplicate_bars_are_collapsed_keeping_the_later():
    df = make_ohlcv(50)
    dupe = df.iloc[[10]].copy()
    dupe["close"] = 999.0
    dupe["high"] = 1000.0
    # kind="stable" matters: the default sort is not stable, so "keep the
    # last row" would otherwise pick an arbitrary one of the two.
    df = pd.concat([df, dupe]).sort_index(kind="stable")

    cleaned, findings = check_duplicates(df, "DUP")
    assert DataIssue.DUPLICATE in issues(findings)
    assert not cleaned.index.has_duplicates
    assert cleaned.loc[dupe.index[0], "close"] == 999.0


# ---------------------------------------------------------------------------
# OHLC sanity
# ---------------------------------------------------------------------------


def test_impossible_bar_is_removed_and_flagged_error():
    """high < low is corruption, not a market event: it produces a negative
    true range that poisons ATR for the next fourteen bars."""
    df = make_ohlcv(50)
    df.iloc[20, df.columns.get_loc("high")] = df.iloc[20]["low"] - 5

    cleaned, findings = check_ohlc_sanity(df, "BAD")
    assert DataIssue.INVALID_OHLC in issues(findings)
    assert all(f.severity is Severity.ERROR for f in findings)
    assert len(cleaned) == len(df) - 1


def test_close_outside_range_is_removed():
    df = make_ohlcv(50)
    df.iloc[7, df.columns.get_loc("close")] = df.iloc[7]["high"] + 10

    cleaned, findings = check_ohlc_sanity(df, "BAD")
    assert DataIssue.INVALID_OHLC in issues(findings)
    assert len(cleaned) == len(df) - 1


def test_negative_price_is_removed():
    df = make_ohlcv(50)
    for col in ("open", "high", "low", "close"):
        df.iloc[12, df.columns.get_loc(col)] = -1.0

    cleaned, findings = check_ohlc_sanity(df, "NEG")
    assert issues(findings) & {DataIssue.NEGATIVE_PRICE, DataIssue.INVALID_OHLC}
    assert len(cleaned) == len(df) - 1


# ---------------------------------------------------------------------------
# Zero volume -- kept, not dropped
# ---------------------------------------------------------------------------


def test_zero_volume_is_flagged_but_the_bar_survives():
    """NSE circuit halts produce genuine zero-volume sessions. Deleting them
    would fabricate a gap in the series that never existed."""
    df = make_ohlcv(50)
    df.iloc[30, df.columns.get_loc("volume")] = 0.0

    findings = check_zero_volume(df, "HALT")
    assert DataIssue.ZERO_VOLUME in issues(findings)
    assert all(f.severity is Severity.WARN for f in findings)

    cleaned, _ = validate(df, "HALT")
    assert len(cleaned) == len(df)  # survived


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------


def test_large_overnight_gap_flagged_as_suspected_split():
    df = make_ohlcv(50, seed=4)
    # A 1:5 split looks like an -80% move if the series is unadjusted.
    df.iloc[25:, [df.columns.get_loc(c) for c in ("open", "high", "low", "close")]] /= 5.0

    findings = check_suspected_splits(df, "SPLIT", threshold_pct=25.0)
    assert DataIssue.SUSPECTED_SPLIT in issues(findings)
    assert findings[0].bar_date == df.index[25].date()


def test_ordinary_volatility_is_not_flagged_as_a_split():
    findings = check_suspected_splits(make_ohlcv(200, seed=8), "NORMAL", threshold_pct=25.0)
    assert findings == []


def test_split_is_flagged_not_corrected():
    """A guessed adjustment factor is its own silent corruption. The rule
    raises a flag for a human or a corporate-actions feed to resolve."""
    df = make_ohlcv(50, seed=4)
    original = df.copy()
    df.iloc[25:, [df.columns.get_loc(c) for c in ("open", "high", "low", "close")]] /= 5.0
    cleaned, _ = validate(df, "SPLIT")
    pd.testing.assert_frame_equal(cleaned, df)  # untouched
    assert not cleaned.equals(original)


# ---------------------------------------------------------------------------
# Staleness and gaps
# ---------------------------------------------------------------------------


def test_stale_series_is_flagged():
    df = make_ohlcv(50, start=date(2024, 1, 1))
    last = df.index[-1].date()
    findings = check_staleness(df, "OLD", as_of=last + timedelta(days=30), stale_after_days=5)
    assert DataIssue.STALE in issues(findings)


def test_fresh_series_is_not_flagged():
    df = make_ohlcv(50, start=date(2024, 1, 1))
    last = df.index[-1].date()
    assert check_staleness(df, "FRESH", as_of=last + timedelta(days=1)) == []


def test_long_gap_is_flagged():
    df = make_ohlcv(60)
    # Remove three weeks of bars in the middle.
    df = pd.concat([df.iloc[:20], df.iloc[35:]])
    findings = check_missing_bars(df, "GAPPY", max_streak=5)
    assert DataIssue.MISSING_BAR in issues(findings)


def test_normal_holiday_cluster_is_not_flagged():
    """Weekday-based rather than holiday-calendar-based: a wrong holiday
    calendar would generate false findings forever."""
    df = make_ohlcv(60)
    df = pd.concat([df.iloc[:20], df.iloc[23:]])  # 3 missing weekdays
    assert check_missing_bars(df, "HOLIDAY", max_streak=5) == []


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def test_validate_applies_structural_repairs_before_statistics():
    """An impossible bar must be removed before the split check runs, or its
    garbage price generates a phantom gap finding as well."""
    df = make_ohlcv(60, seed=6)
    df.iloc[30, df.columns.get_loc("high")] = df.iloc[30]["low"] - 100

    cleaned, findings = validate(df, "MIXED")
    assert len(cleaned) == len(df) - 1
    assert DataIssue.INVALID_OHLC in issues(findings)


def test_validate_from_config_uses_configured_thresholds(cfg):
    from vriddhix.data.quality import validate_from_config

    df = make_ohlcv(60, seed=9)
    cleaned, findings = validate_from_config(df, "CFG", cfg)
    assert len(cleaned) == len(df)
    assert findings == []
