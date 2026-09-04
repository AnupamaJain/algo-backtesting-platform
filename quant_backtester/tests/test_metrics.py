from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src import backtest as metrics


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.bdate_range("2020-01-01", periods=len(values)))


def test_max_drawdown_known_case():
    # equity: 1.10 -> 0.55 -> 0.66; peak 1.10, trough 0.55 => 50% drawdown
    returns = _series([0.10, -0.50, 0.20])
    assert metrics.compute_max_drawdown(returns) == pytest.approx(0.5)


def test_max_drawdown_is_zero_for_monotonic_gains():
    returns = _series([0.01, 0.02, 0.03])
    assert metrics.compute_max_drawdown(returns) == pytest.approx(0.0)


def test_sharpe_of_constant_series_is_zero_not_nan():
    returns = _series([0.001] * 50)
    assert metrics.compute_sharpe(returns) == 0.0


def test_sharpe_scales_with_annualization():
    rng = np.random.default_rng(0)
    returns = _series(list(rng.normal(0.001, 0.01, 252)))
    daily_sr = returns.mean() / returns.std(ddof=1)
    assert metrics.compute_sharpe(returns) == pytest.approx(daily_sr * np.sqrt(252))


def test_sharpe_empty_series_is_zero():
    assert metrics.compute_sharpe(pd.Series(dtype=float)) == 0.0


def test_num_trades_counts_entries_only():
    position = _series([0, 1, 1, 0, -1, -1, 0])
    # two entries: flat->long at idx1, flat->short at idx4
    assert metrics.compute_num_trades(position) == 2


def test_num_trades_counts_direct_flip_as_one_entry():
    position = _series([0, 1, 1, -1, -1])
    assert metrics.compute_num_trades(position) == 2


def test_extract_trade_returns_compounds_within_trade():
    returns = _series([0.0, 0.10, 0.10, 0.0, -0.05])
    position = _series([0, 1, 1, 0, 0])
    trades = metrics.extract_trade_returns(returns, position)
    assert len(trades) == 1
    # (1.10 * 1.10) - 1 == 0.21
    assert trades.iloc[0] == pytest.approx(0.21)


def test_profit_factor_known_case():
    trades = pd.Series([0.2, 0.1, -0.1, -0.05])
    # gains 0.3, losses 0.15 -> PF 2.0
    assert metrics.compute_profit_factor(trades) == pytest.approx(2.0)


def test_profit_factor_all_winners_is_inf():
    assert metrics.compute_profit_factor(pd.Series([0.1, 0.2])) == float("inf")


def test_profit_factor_no_trades_is_zero():
    assert metrics.compute_profit_factor(pd.Series(dtype=float)) == 0.0


def test_cagr_doubling_over_one_year():
    returns = _series([0.0] * 252)
    returns.iloc[0] = 1.0  # instant 100% gain, held flat for a year
    assert metrics.compute_cagr(returns, periods_per_year=252) == pytest.approx(1.0, rel=1e-6)


def test_sharpe_p_value_decreases_with_higher_sharpe():
    weak = metrics.sharpe_p_value(0.2, 1000)
    strong = metrics.sharpe_p_value(2.0, 1000)
    assert strong < weak
    assert 0.0 <= strong <= 1.0


def test_compute_metrics_bundle_shape():
    returns = _series([0.01, -0.02, 0.03, 0.0])
    position = _series([1, 1, 1, 0])
    bundle = metrics.compute_metrics(returns, position)
    assert bundle.num_periods == 4
    assert isinstance(bundle.to_dict(), dict)
