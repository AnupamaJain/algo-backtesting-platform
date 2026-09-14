"""Causality: no feature may be influenced by a bar dated after it.

This is the most important test file in Phase 1. A violation here does not
crash anything -- it produces a backtest that looks excellent and means
nothing, and it would survive every other test in the suite.

The method: compute on a frame truncated at T, compute on the full frame, and
require the rows up to T to be byte-identical. If any feature peeks forward,
the two disagree.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from vriddhix.features.technical import compute_features
from conftest import make_ohlcv

FEATURES_DIR = Path(__file__).resolve().parent.parent / "src" / "vriddhix" / "features"


@pytest.mark.parametrize("cutoff_index", [100, 150, 200, 250])
def test_all_features_are_causal(cutoff_index):
    df = make_ohlcv(300, seed=99)
    cutoff = df.index[cutoff_index]

    truncated = compute_features(df.loc[:cutoff])
    full = compute_features(df).loc[:cutoff]

    # check_exact=True is the point. A 1e-9 tolerance would hide a one-bar
    # shift in a 200-period EMA, which is precisely the bug being hunted.
    pd.testing.assert_frame_equal(truncated, full, check_exact=True)


@pytest.mark.parametrize(
    "pattern", ["uptrend", "downtrend", "chop", "flat"]
)
def test_causality_holds_for_every_market_shape(pattern):
    df = make_ohlcv(300, pattern=pattern, seed=17)
    cutoff = df.index[180]
    truncated = compute_features(df.loc[:cutoff])
    full = compute_features(df).loc[:cutoff]
    pd.testing.assert_frame_equal(truncated, full, check_exact=True)


def test_appending_bars_never_changes_history():
    """The live-scan version of the same property: yesterday's printed values
    must not move when today's bar arrives."""
    df = make_ohlcv(400, seed=23)
    first_half = df.iloc[:300]

    before = compute_features(first_half)
    after = compute_features(df).loc[: first_half.index[-1]]

    pd.testing.assert_frame_equal(before, after, check_exact=True)


def test_single_bar_append_is_stable():
    df = make_ohlcv(260, seed=31)
    before = compute_features(df.iloc[:-1])
    after = compute_features(df).iloc[:-1]
    pd.testing.assert_frame_equal(before, after, check_exact=True)


# ---------------------------------------------------------------------------
# Static check: the forbidden constructs must not appear at all
# ---------------------------------------------------------------------------


FORBIDDEN_CALLS = {"bfill", "backfill"}
FORBIDDEN_KWARGS = {"center"}


def _feature_sources() -> list[Path]:
    return [p for p in FEATURES_DIR.rglob("*.py") if p.name != "__init__.py"]


def test_no_forward_shift_in_feature_code():
    """``shift(-n)`` pulls a future bar backwards. It has no legitimate use in
    a causal feature and is easy to introduce by accident."""
    for path in _feature_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "shift":
                continue
            for arg in node.args:
                if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
                    pytest.fail(f"{path.name}: negative shift() is look-ahead")


def test_no_centred_windows_or_backfill_in_feature_code():
    for path in _feature_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_CALLS:
                    pytest.fail(f"{path.name}: {node.func.attr}() fills from the future")
                for kw in node.keywords:
                    if kw.arg in FORBIDDEN_KWARGS:
                        value = getattr(kw.value, "value", None)
                        if value is True:
                            pytest.fail(f"{path.name}: center=True straddles the current bar")


def test_feature_code_does_not_read_the_clock():
    """A feature that knows the wall clock behaves differently in a backtest
    than it did live, which breaks the equivalence the whole design rests on."""
    banned = {"now", "today", "utcnow"}
    for path in _feature_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in banned
            ):
                pytest.fail(f"{path.name}: {node.func.attr}() -- engines must not read the clock")


# ---------------------------------------------------------------------------
# The same property, on real NSE history
# ---------------------------------------------------------------------------


def test_causality_holds_on_real_nse_data(cfg):
    """Synthetic frames are smooth. Real ones have gaps, halts, circuit
    moves and unadjusted corporate actions -- the situations where an
    accidental look-ahead is most likely to hide.
    """
    from vriddhix.data.providers import CsvCacheProvider

    if not cfg.cache_dir.is_dir():
        pytest.skip("NSE cache not present")

    provider = CsvCacheProvider(cfg.cache_dir)
    for symbol in ("RELIANCE", "TCS", "HDFCBANK"):
        try:
            df = provider.fetch(symbol)
        except Exception:
            pytest.skip(f"{symbol} not cached")

        for fraction in (0.5, 0.75, 0.9):
            cutoff = df.index[int(len(df) * fraction)]
            truncated = compute_features(df.loc[:cutoff], symbol=symbol)
            full = compute_features(df, symbol=symbol).loc[:cutoff]
            pd.testing.assert_frame_equal(
                truncated, full, check_exact=True,
                obj=f"{symbol} truncated at {cutoff.date()}",
            )


def test_causality_survives_an_unadjusted_corporate_action(cfg):
    """RELIANCE's 1:1 bonus shows up as a ~50% overnight drop in the raw
    series. A feature that peeked across it would be spectacularly wrong."""
    from vriddhix.data.providers import CsvCacheProvider

    if not cfg.cache_dir.is_dir():
        pytest.skip("NSE cache not present")

    df = CsvCacheProvider(cfg.cache_dir).fetch("RELIANCE")
    split_date = pd.Timestamp("2024-10-28")
    if split_date not in df.index:
        pytest.skip("bonus-issue bar not in cache")

    # Cut off the day before the gap: the features must be identical whether
    # or not the engine can see what happens next.
    cutoff = df.index[df.index.get_loc(split_date) - 1]
    truncated = compute_features(df.loc[:cutoff], symbol="RELIANCE")
    full = compute_features(df, symbol="RELIANCE").loc[:cutoff]
    pd.testing.assert_frame_equal(truncated, full, check_exact=True)
