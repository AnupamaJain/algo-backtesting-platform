from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.backtest import (
    MaxDrawdownFilter,
    MinTradesFilter,
    MultipleComparisonFilter,
    OOSSharpeFilter,
    OverfittingFilter,
    ProfitFactorFilter,
    ValidationFunnel,
)


def _config_frame(**overrides) -> pd.DataFrame:
    """A small frame of configurations with sensible passing defaults."""
    base = {
        "symbol": ["SPY"] * 4,
        "strategy": ["MACrossover"] * 4,
        "param_key": [f"p{i}" for i in range(4)],
        "is_sharpe": [1.0, 1.0, 1.0, 1.0],
        "oos_sharpe": [1.0, 1.0, 1.0, 1.0],
        "oos_max_drawdown": [0.10] * 4,
        "oos_profit_factor": [1.5] * 4,
        "oos_num_trades": [50] * 4,
        "oos_num_periods": [1000] * 4,
    }
    base.update(overrides)
    return pd.DataFrame(base)


# -- individual filters -------------------------------------------------


def test_min_trades_filter():
    df = _config_frame(oos_num_trades=[10, 29, 30, 100])
    mask = MinTradesFilter(30).apply(df, {})
    assert list(mask) == [False, False, True, True]


def test_oos_sharpe_filter_is_strictly_greater():
    df = _config_frame(oos_sharpe=[0.4, 0.5, 0.51, 2.0])
    mask = OOSSharpeFilter(0.5).apply(df, {})
    assert list(mask) == [False, False, True, True]


def test_max_drawdown_filter():
    df = _config_frame(oos_max_drawdown=[0.10, 0.35, 0.36, 0.90])
    mask = MaxDrawdownFilter(0.35).apply(df, {})
    assert list(mask) == [True, True, False, False]


def test_overfitting_filter_rejects_is_far_above_oos():
    df = _config_frame(is_sharpe=[1.0, 2.0, 2.1, 4.0], oos_sharpe=[1.0, 1.0, 1.0, 1.0])
    mask = OverfittingFilter(2.0).apply(df, {})
    assert list(mask) == [True, True, False, False]


def test_overfitting_filter_passes_when_oos_beats_is():
    df = _config_frame(is_sharpe=[0.5], oos_sharpe=[2.0], **{
        k: [v[0]] for k, v in _config_frame().items()
        if k not in ("is_sharpe", "oos_sharpe")
    })
    mask = OverfittingFilter(2.0).apply(df, {})
    assert bool(mask.iloc[0]) is True


def test_overfitting_filter_rejects_positive_is_with_nonpositive_oos():
    """The worst overfit case: looked great in-sample, died out-of-sample.
    The ratio is undefined, so it must be rejected explicitly."""
    df = _config_frame(is_sharpe=[2.0, 2.0], oos_sharpe=[0.0, -1.0], **{
        k: v[:2] for k, v in _config_frame().items()
        if k not in ("is_sharpe", "oos_sharpe")
    })
    mask = OverfittingFilter(2.0).apply(df, {})
    assert list(mask) == [False, False]


def test_profit_factor_filter():
    df = _config_frame(oos_profit_factor=[0.9, 1.09, 1.1, 3.0])
    mask = ProfitFactorFilter(1.1).apply(df, {})
    assert list(mask) == [False, False, True, True]


# -- multiple comparison correction -------------------------------------


def test_bonferroni_is_stricter_than_uncorrected():
    df = _config_frame(oos_sharpe=[0.6, 0.7, 0.8, 0.9], oos_num_periods=[300] * 4)
    context = {"num_configurations_tested": 5000, "periods_per_year": 252, "annotations": {}}
    mask = MultipleComparisonFilter("bonferroni", 0.05).apply(df, context)
    # With 5,000 hypotheses, a marginal Sharpe over ~1 year cannot survive.
    assert mask.sum() == 0


def test_bonferroni_admits_overwhelming_evidence():
    df = _config_frame(oos_sharpe=[4.0], oos_num_periods=[3000], **{
        k: [v[0]] for k, v in _config_frame().items()
        if k not in ("oos_sharpe", "oos_num_periods")
    })
    context = {"num_configurations_tested": 100, "periods_per_year": 252, "annotations": {}}
    mask = MultipleComparisonFilter("bonferroni", 0.05).apply(df, context)
    assert bool(mask.iloc[0]) is True


def test_fdr_is_at_least_as_permissive_as_bonferroni():
    df = _config_frame(oos_sharpe=[1.5, 1.6, 1.7, 1.8], oos_num_periods=[2000] * 4)
    context = lambda: {  # noqa: E731 - fresh context per call
        "num_configurations_tested": 200,
        "periods_per_year": 252,
        "annotations": {},
    }
    bonf = MultipleComparisonFilter("bonferroni", 0.05).apply(df, context()).sum()
    fdr = MultipleComparisonFilter("fdr", 0.05).apply(df, context()).sum()
    assert fdr >= bonf


def test_mcp_corrects_by_total_tested_not_survivors():
    """Correcting by the survivor count would understate the search; the
    filter must use the original number of configurations tested."""
    df = _config_frame(oos_sharpe=[1.2] * 4, oos_num_periods=[500] * 4)
    small = MultipleComparisonFilter("bonferroni", 0.05).apply(
        df, {"num_configurations_tested": 4, "periods_per_year": 252, "annotations": {}}
    )
    large = MultipleComparisonFilter("bonferroni", 0.05).apply(
        df, {"num_configurations_tested": 9000, "periods_per_year": 252, "annotations": {}}
    )
    assert small.sum() >= large.sum()


def test_mcp_rejects_invalid_method():
    with pytest.raises(ValueError):
        MultipleComparisonFilter("magic")


def test_mcp_on_empty_frame():
    mask = MultipleComparisonFilter().apply(pd.DataFrame(), {})
    assert len(mask) == 0


# -- the full funnel ----------------------------------------------------


def _funnel() -> ValidationFunnel:
    return ValidationFunnel.from_config(
        {
            "filter_1_min_trades": 30,
            "filter_2_min_oos_sharpe": 0.5,
            "filter_3_max_drawdown": 0.35,
            "filter_4_max_is_oos_sharpe_ratio": 2.0,
            "filter_5_min_profit_factor": 1.1,
            "filter_6": {"method": "fdr", "alpha": 0.05},
        }
    )


def test_funnel_survivors_decrease_monotonically():
    rng = np.random.default_rng(3)
    n = 400
    df = pd.DataFrame(
        {
            "symbol": ["SPY"] * n,
            "strategy": ["S"] * n,
            "param_key": [f"p{i}" for i in range(n)],
            "is_sharpe": rng.normal(1.0, 0.6, n),
            "oos_sharpe": rng.normal(0.3, 0.6, n),
            "oos_max_drawdown": rng.uniform(0.05, 0.6, n),
            "oos_profit_factor": rng.uniform(0.8, 1.6, n),
            "oos_num_trades": rng.integers(5, 200, n),
            "oos_num_periods": [1500] * n,
        }
    )
    result = _funnel().run(df)
    counts = [r.survived for r in result.stage_reports]
    assert counts == sorted(counts, reverse=True), "survivor count must never increase"
    assert len(result.stage_reports) == 6
    assert len(result.survivors) == counts[-1]


def test_funnel_records_where_each_config_died():
    df = _config_frame(oos_num_trades=[5, 50, 50, 50], oos_sharpe=[1.0, 0.1, 1.0, 1.0])
    result = _funnel().run(df)
    scatter = result.scatter_coordinates()

    assert scatter.loc[0, "rejected_at_stage"] == 1  # too few trades
    assert scatter.loc[1, "rejected_at_stage"] == 2  # sharpe too low
    assert set(scatter.columns) >= {"is_sharpe", "oos_sharpe", "survived", "rejected_at_stage"}


def test_funnel_handles_empty_input():
    result = _funnel().run(pd.DataFrame())
    assert result.survivors.empty
    assert result.stage_reports == []


def test_funnel_report_and_summary_shapes():
    df = _config_frame()
    result = _funnel().run(df)
    report = result.funnel_report()
    assert list(report["stage"]) == [1, 2, 3, 4, 5, 6]
    assert "entered" in report.columns and "survived" in report.columns
    assert isinstance(result.summary_table(), pd.DataFrame)


def test_funnel_all_rejected_still_reports_all_stages():
    df = _config_frame(oos_num_trades=[1, 1, 1, 1])
    result = _funnel().run(df)
    assert result.survivors.empty
    assert len(result.stage_reports) == 6
