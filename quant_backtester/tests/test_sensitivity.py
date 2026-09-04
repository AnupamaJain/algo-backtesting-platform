from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.backtest import VectorizedBacktester
from quant_backtester.src.config import SensitivityConfig
from quant_backtester.src.backtest import PercentageCostModel
from quant_backtester.src.robustness import (
    ParameterSensitivityChecker,
    SensitivityResult,
    StepPerturbationPolicy,
)
from quant_backtester.src.backtest import WalkForwardEngine, WindowGenerator


def _config(**overrides) -> SensitivityConfig:
    base = dict(
        int_offsets=[-2, -1, 1, 2],
        float_relative_offsets=[-0.2, -0.1, 0.1, 0.2],
        metric="sharpe",
        min_retention_ratio=0.5,
        min_stability_fraction=0.6,
    )
    base.update(overrides)
    return SensitivityConfig(**base)


# -- perturbation policy -----------------------------------------------


def test_int_params_step_by_whole_units():
    policy = StepPerturbationPolicy(int_offsets=[-2, -1, 1, 2])
    variants = policy.neighbours({"rsi_period": 14})
    values = sorted(v.perturbed_value for v in variants)
    assert values == [12, 13, 15, 16]


def test_float_params_step_multiplicatively():
    policy = StepPerturbationPolicy(float_relative_offsets=[-0.1, 0.1])
    variants = policy.neighbours({"num_std": 2.0})
    values = sorted(v.perturbed_value for v in variants)
    assert values == pytest.approx([1.8, 2.2])


def test_perturbation_is_one_at_a_time():
    """Each variant must differ from the centre in exactly one parameter."""
    centre = {"fast_period": 50, "slow_period": 200}
    for variant in StepPerturbationPolicy().neighbours(centre):
        differing = [k for k in centre if variant.params[k] != centre[k]]
        assert differing == [variant.changed_param]


def test_int_perturbation_never_goes_below_one():
    variants = StepPerturbationPolicy(int_offsets=[-5, -2, 2]).neighbours({"period": 2})
    assert all(v.perturbed_value >= 1 for v in variants)


def test_non_numeric_and_bool_params_are_skipped():
    variants = StepPerturbationPolicy().neighbours(
        {"mode": "fast", "enabled": True, "period": 10}
    )
    assert {v.changed_param for v in variants} == {"period"}


# -- scoring semantics --------------------------------------------------


def _result(centre: float, neighbours: list[float]) -> SensitivityResult:
    return SensitivityResult(
        symbol="X",
        strategy_name="S",
        params={"p": 1},
        centre_metric=centre,
        neighbour_metrics=neighbours,
        num_neighbours_tested=len(neighbours),
    )


def test_plateau_scores_high():
    """Neighbours performing like the centre => a stable plateau."""
    result = _result(1.0, [0.95, 1.05, 0.98, 1.02])
    assert result.retention_ratio == pytest.approx(1.0, abs=0.02)
    assert result.sensitivity_score(0.5) > 0.9
    assert result.is_stable(_config())


def test_island_scores_low():
    """Neighbours collapsing => an overfit parameter island."""
    result = _result(1.5, [0.05, -0.1, 0.02, 0.0])
    assert result.retention_ratio < 0.1
    assert result.sensitivity_score(0.5) == pytest.approx(0.0)
    assert not result.is_stable(_config())


def test_mixed_neighbourhood_fails_stability_fraction():
    """Half the neighbourhood holding up is not enough at a 0.6 threshold."""
    result = _result(1.0, [1.0, 1.0, 0.0, 0.0])
    assert result.stability_fraction(0.5) == pytest.approx(0.5)
    assert not result.is_stable(_config(min_stability_fraction=0.6))


def test_outperforming_neighbours_are_capped_not_rewarded():
    """This measures stability, not performance: neighbours beating the
    centre must not push the score above 1.0."""
    result = _result(1.0, [3.0, 3.0, 3.0, 3.0])
    assert result.retention_ratio == pytest.approx(3.0)
    assert result.sensitivity_score(0.5) == pytest.approx(1.0)


def test_negative_centre_scores_zero():
    """Stability around a losing configuration is not a virtue."""
    result = _result(-0.5, [-0.5, -0.5, -0.5])
    assert result.sensitivity_score(0.5) == 0.0
    assert not result.is_stable(_config())


def test_empty_neighbourhood_is_not_stable():
    result = _result(1.0, [])
    assert result.sensitivity_score(0.5) == 0.0
    assert not result.is_stable(_config())


def test_coefficient_of_variation_reflects_dispersion():
    tight = _result(1.0, [1.0, 1.01, 0.99])
    loose = _result(1.0, [0.2, 1.8, 1.0])
    assert tight.coefficient_of_variation < loose.coefficient_of_variation


# -- integration with the engine ---------------------------------------


@pytest.fixture
def ohlcv() -> pd.DataFrame:
    rng = np.random.default_rng(4)
    n = 3800
    dates = pd.bdate_range("2010-01-01", periods=n)
    close = 100 * np.cumprod(1 + rng.normal(0.0004, 0.011, n))
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.005,
            "Low": close * 0.995,
            "Close": close,
            "Volume": 1_000_000,
        },
        index=dates,
    )


@pytest.fixture
def checker() -> ParameterSensitivityChecker:
    engine = WalkForwardEngine(
        backtester=VectorizedBacktester(PercentageCostModel(0.0005, 0.0002)),
        window_generator=WindowGenerator(3, 1, 1),
    )
    return ParameterSensitivityChecker(engine, _config())


def test_checker_evaluates_real_neighbourhood(checker, ohlcv):
    result = checker.check("TEST", "RSIReversion", {"rsi_period": 14}, ohlcv)
    assert result is not None
    assert result.num_neighbours_tested == 4
    assert len(result.neighbour_metrics) > 0


def test_checker_counts_invalid_neighbours_separately(checker, ohlcv):
    """fast_period perturbations that cross slow_period are impossible
    parameter sets, not zero-performing ones — they must not drag the mean."""
    result = checker.check(
        "TEST", "MACrossover", {"fast_period": 50, "slow_period": 51}, ohlcv
    )
    assert result is not None
    assert result.num_neighbours_failed > 0
    assert len(result.neighbour_metrics) == (
        result.num_neighbours_tested - result.num_neighbours_failed
    )


def test_checker_returns_none_for_parameterless_strategy(checker, ohlcv):
    assert checker.check("TEST", "RSIReversion", {}, ohlcv) is None


def test_to_row_contains_expected_columns(checker, ohlcv):
    result = checker.check("TEST", "SimpleMomentum", {"lookback_period": 126}, ohlcv)
    row = result.to_row(_config())
    for column in (
        "sensitivity_score",
        "retention_ratio",
        "stability_fraction",
        "coefficient_of_variation",
        "is_stable",
        "num_neighbours_tested",
    ):
        assert column in row
