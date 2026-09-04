"""End-to-end Layer 3: survivors -> sensitivity + bootstrap -> ultra-robust set."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.backtest import VectorizedBacktester
from quant_backtester.src.robustness import BootstrapStressTester
from quant_backtester.src.config import (
    BootstrapConfig,
    RobustnessConfig,
    SensitivityConfig,
)
from quant_backtester.src.backtest import PercentageCostModel
from quant_backtester.src.robustness import RobustnessSuite
from quant_backtester.src.robustness import ParameterSensitivityChecker
from quant_backtester.src.backtest import WalkForwardEngine, WindowGenerator


def _ohlcv(seed: int, n: int = 3800) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
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
def price_data() -> dict[str, pd.DataFrame]:
    return {"AAA": _ohlcv(1), "BBB": _ohlcv(2)}


@pytest.fixture
def robustness_config(tmp_path) -> RobustnessConfig:
    return RobustnessConfig(
        sensitivity=SensitivityConfig(
            int_offsets=[-2, -1, 1, 2],
            float_relative_offsets=[-0.1, 0.1],
            metric="sharpe",
            min_retention_ratio=0.5,
            min_stability_fraction=0.6,
        ),
        bootstrap=BootstrapConfig(
            num_simulations=200,
            random_seed=42,
            with_replacement=True,
            trade_source="oos",
            min_trades_required=10,
            min_p5_final_return=0.0,
            max_p95_drawdown=0.50,
        ),
        robustness_dir=tmp_path / "layer3",
    )


@pytest.fixture
def suite(robustness_config) -> RobustnessSuite:
    engine = WalkForwardEngine(
        backtester=VectorizedBacktester(PercentageCostModel(0.0005, 0.0002)),
        window_generator=WindowGenerator(3, 1, 1),
    )
    return RobustnessSuite(
        sensitivity_checker=ParameterSensitivityChecker(
            engine, robustness_config.sensitivity
        ),
        bootstrap_tester=BootstrapStressTester(robustness_config.bootstrap),
        engine=engine,
        config=robustness_config,
    )


@pytest.fixture
def survivors() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "strategy": "RSIReversion",
                "param_key": "rsi_period=14",
                "params": {"rsi_period": 14},
                "oos_sharpe": 0.7,
                "oos_max_drawdown": 0.2,
                "oos_num_trades": 40,
            },
            {
                "symbol": "BBB",
                "strategy": "SimpleMomentum",
                "param_key": "lookback_period=126",
                "params": {"lookback_period": 126},
                "oos_sharpe": 0.6,
                "oos_max_drawdown": 0.25,
                "oos_num_trades": 35,
            },
        ]
    )


def test_suite_appends_both_robustness_metrics(suite, survivors, price_data):
    output = suite.run(survivors, price_data)
    assert len(output.annotated) == 2
    for column in ("sensitivity_score", "bootstrap_verdict", "is_stable"):
        assert column in output.annotated.columns


def test_ultra_robust_requires_passing_both_gates(suite, survivors, price_data):
    output = suite.run(survivors, price_data)
    for _, row in output.ultra_robust.iterrows():
        assert row["is_stable"]
        assert row["bootstrap_verdict"] == "PASS"
    assert len(output.ultra_robust) <= len(output.annotated)


def test_suite_is_reproducible(suite, survivors, price_data):
    first = suite.run(survivors, price_data)
    second = suite.run(survivors, price_data)
    pd.testing.assert_frame_equal(first.annotated, second.annotated)


def test_params_round_trip_through_csv(suite, survivors, price_data, tmp_path):
    """Layer 2 writes survivors to CSV, which stringifies the params dict.
    Layer 3 must still be able to rebuild the strategy from that."""
    path = tmp_path / "survivors.csv"
    survivors.to_csv(path, index=False)
    reloaded = pd.read_csv(path)
    assert isinstance(reloaded.loc[0, "params"], str)  # stringified by CSV

    output = suite.run(reloaded, price_data)
    assert len(output.annotated) == 2
    # a real neighbourhood was tested, i.e. params were genuinely recovered
    assert (output.annotated["num_neighbours_tested"] > 0).all()


def test_unparseable_params_are_skipped_not_crashed(suite, price_data):
    broken = pd.DataFrame(
        [{"symbol": "AAA", "strategy": "RSIReversion", "params": "not-a-dict"}]
    )
    output = suite.run(broken, price_data)
    assert output.annotated.empty


def test_missing_price_data_is_skipped(suite, survivors):
    output = suite.run(survivors, {"AAA": _ohlcv(1)})  # BBB absent
    assert len(output.annotated) == 1
    assert set(output.annotated["symbol"]) == {"AAA"}


def test_empty_survivors_returns_empty(suite, price_data):
    output = suite.run(pd.DataFrame(), price_data)
    assert output.annotated.empty
    assert output.ultra_robust.empty


def test_artifacts_written(suite, survivors, price_data, robustness_config):
    output = suite.run(survivors, price_data)
    suite.write_artifacts(output)

    out_dir = robustness_config.robustness_dir
    assert (out_dir / "robustness_full.csv").exists()
    assert (out_dir / "ultra_robust_strategies.csv").exists()

    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["num_evaluated"] == 2
    # the seed must be recorded, or the run is not reproducible from artifacts
    assert manifest["bootstrap"]["random_seed"] == 42


def test_one_broken_config_does_not_abort_the_suite(suite, survivors, price_data):
    broken = pd.concat(
        [
            survivors,
            pd.DataFrame(
                [
                    {
                        "symbol": "AAA",
                        "strategy": "NoSuchStrategy",
                        "params": {"x": 1},
                        "oos_sharpe": 1.0,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    output = suite.run(broken, price_data)
    # the two good rows still get evaluated
    assert len(output.annotated) == 3
    assert output.annotated["sensitivity_score"].notna().sum() >= 2
