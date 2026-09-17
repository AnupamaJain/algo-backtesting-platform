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
    check_reverting_spikes,
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


# ---------------------------------------------------------------------------
# Reverting spikes -- bad data that a split check cannot see
# ---------------------------------------------------------------------------


def _decimal_shift(df: pd.DataFrame, at: int, span: int, factor: float) -> pd.DataFrame:
    """Displace `span` bars by `factor`, the way a dropped decimal point does."""
    df = df.copy()
    cols = [df.columns.get_loc(c) for c in ("open", "high", "low", "close")]
    df.iloc[at:at + span, cols] /= factor
    return df


def test_a_flat_displaced_span_is_flagged():
    """The NIFTYBEES case: two sessions printed at a tenth of their
    neighbours, with the bar in between carrying no large move of its own."""
    df = _decimal_shift(make_ohlcv(60, seed=11), at=30, span=2, factor=10.0)

    findings = check_reverting_spikes(df, "ETF", threshold_pct=25.0)

    assert DataIssue.BAD_PRICE_SPAN in issues(findings)
    flagged = {f.bar_date for f in findings}
    # Both bars, not just the two ends where the big moves are. The middle
    # bar is the one a split check cannot reach.
    assert flagged == {df.index[30].date(), df.index[31].date()}
    assert all(f.severity is Severity.ERROR for f in findings)


def test_the_flagged_span_records_the_ratio_it_was_displaced_by():
    df = _decimal_shift(make_ohlcv(60, seed=12), at=30, span=2, factor=100.0)
    findings = check_reverting_spikes(df, "ETF", threshold_pct=25.0)
    assert findings
    assert findings[0].detail["ratio"] == pytest.approx(100.0, rel=0.05)


def test_a_real_crash_that_recovers_is_not_flagged():
    """March 2020, and the reason flatness is required at all.

    YESBANK fell 56% on the RBI moratorium and was back near its prior level
    three sessions later -- which satisfies reversion on its own. The bars in
    between moved +32% and +36%. Flagging that span would delete a real
    market event from the record, which is a worse failure than missing a
    corrupted one.
    """
    base = make_ohlcv(60, seed=13)
    close = base["close"].copy()
    prior = float(close.iloc[29])
    path = [prior * 0.44, prior * 0.58, prior * 0.79, prior * 0.99]
    for offset, price in enumerate(path):
        for col in ("open", "high", "low", "close"):
            base.iloc[30 + offset, base.columns.get_loc(col)] = price

    findings = check_reverting_spikes(base, "YESBANK", threshold_pct=25.0)

    assert findings == [], "a real crash was flagged as corrupt data"


def test_a_genuine_split_is_not_flagged_as_a_bad_span():
    """A split does not revert -- that is the whole distinction."""
    df = make_ohlcv(60, seed=14)
    cols = [df.columns.get_loc(c) for c in ("open", "high", "low", "close")]
    df.iloc[30:, cols] /= 5.0

    assert check_reverting_spikes(df, "SPLIT", threshold_pct=25.0) == []
    # ...and the split rule still sees it, so nothing is lost.
    assert DataIssue.SUSPECTED_SPLIT in issues(
        check_suspected_splits(df, "SPLIT", threshold_pct=25.0)
    )


def test_ordinary_volatility_produces_no_bad_spans():
    assert check_reverting_spikes(make_ohlcv(250, seed=15), "NORMAL") == []


def test_a_bad_span_is_flagged_not_repaired():
    """Same commitment as the split rule. A guessed correction factor is a
    silent rewrite of the record."""
    df = _decimal_shift(make_ohlcv(60, seed=16), at=30, span=2, factor=10.0)
    before = df["close"].copy()

    check_reverting_spikes(df, "ETF", threshold_pct=25.0)

    pd.testing.assert_series_equal(df["close"], before)
