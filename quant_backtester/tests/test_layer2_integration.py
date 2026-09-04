"""End-to-end Layer 2: signals -> walk-forward -> funnel -> artifacts.

Uses synthetic price data and the in-process signal repository, so the whole
pipeline is exercised with no network and no Layer 1 artifacts on disk.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.backtest import VectorizedBacktester
from quant_backtester.src.config import BacktestConfig, WalkForwardSettings
from quant_backtester.src.backtest import PercentageCostModel, build_cost_model
from quant_backtester.src.backtest import Layer2Pipeline
from quant_backtester.src.backtest import (
    GeneratedSignalRepository,
    build_param_key_map,
)
from quant_backtester.src.backtest import ValidationFunnel
from quant_backtester.src.backtest import WalkForwardEngine, WindowGenerator

STRATEGY_GRID = {
    "MACrossover": {"fast_period": [20, 50], "slow_period": [100, 200]},
    "SimpleMomentum": {"lookback_period": [63, 126]},
}

FUNNEL_CONFIG = {
    "filter_1_min_trades": 5,
    "filter_2_min_oos_sharpe": 0.0,
    "filter_3_max_drawdown": 0.95,
    "filter_4_max_is_oos_sharpe_ratio": 10.0,
    "filter_5_min_profit_factor": 0.5,
    "filter_6": {"method": "fdr", "alpha": 0.5},
}


def _synthetic_ohlcv(seed: int, n: int = 3800) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2010-01-01", periods=n)
    returns = rng.normal(0.0004, 0.011, n)
    close = 100 * np.cumprod(1 + returns)
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
    return {"AAA": _synthetic_ohlcv(1), "BBB": _synthetic_ohlcv(2)}


@pytest.fixture
def pipeline(tmp_path) -> Layer2Pipeline:
    config = BacktestConfig(
        periods_per_year=252,
        risk_free_rate=0.0,
        costs={
            "model": "percentage",
            "percentage": {"commission_pct": 0.0005, "slippage_pct": 0.0002},
        },
        walk_forward=WalkForwardSettings("rolling", 3, 1, 1, 60, "sharpe"),
        funnel=FUNNEL_CONFIG,
        signals_dir=tmp_path / "signals",
        layer2_dir=tmp_path / "layer2",
    )
    engine = WalkForwardEngine(
        backtester=VectorizedBacktester(build_cost_model(config.costs)),
        window_generator=WindowGenerator(3, 1, 1, "rolling", 60),
    )
    return Layer2Pipeline(
        engine=engine,
        repository=None,  # set per-test
        funnel=ValidationFunnel.from_config(FUNNEL_CONFIG),
        config=config,
    )


def test_full_pipeline_produces_expected_configuration_count(pipeline, price_data):
    pipeline._repository = GeneratedSignalRepository(price_data, STRATEGY_GRID)
    output = pipeline.run(price_data, list(STRATEGY_GRID), run_optimized=True)

    # 2 symbols x (4 MACrossover combos + 2 SimpleMomentum combos) = 12
    assert len(output.configurations) == 12
    assert len(output.funnel_result.stage_reports) == 6
    # optimized pass: one row per (symbol, strategy)
    assert len(output.optimized_results) == 4


def test_pipeline_artifacts_written(pipeline, price_data, tmp_path):
    pipeline._repository = GeneratedSignalRepository(price_data, STRATEGY_GRID)
    output = pipeline.run(price_data, list(STRATEGY_GRID))
    pipeline.write_artifacts(output)

    out_dir = tmp_path / "layer2"
    for name in (
        "survivors.csv",
        "funnel_report.csv",
        "is_oos_scatter.csv",
        "all_configurations.csv",
        "wfa_optimized.csv",
        "manifest.json",
    ):
        assert (out_dir / name).exists(), f"missing artifact {name}"

    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["num_configurations_tested"] == 12
    assert len(manifest["stages"]) == 6


def test_scatter_coordinates_cover_every_configuration(pipeline, price_data):
    pipeline._repository = GeneratedSignalRepository(price_data, STRATEGY_GRID)
    output = pipeline.run(price_data, list(STRATEGY_GRID))
    scatter = output.funnel_result.scatter_coordinates()

    assert len(scatter) == len(output.configurations)
    assert scatter["survived"].dtype == bool
    # every non-survivor must record which gate killed it
    dead = scatter[~scatter["survived"]]
    assert dead["rejected_at_stage"].notna().all()


def test_pipeline_is_reproducible(pipeline, price_data):
    """Same inputs must give byte-identical metrics — no hidden randomness."""
    pipeline._repository = GeneratedSignalRepository(price_data, STRATEGY_GRID)
    first = pipeline.run(price_data, list(STRATEGY_GRID), run_optimized=False)
    second = pipeline.run(price_data, list(STRATEGY_GRID), run_optimized=False)
    pd.testing.assert_frame_equal(first.configurations, second.configurations)


def test_pipeline_survives_a_broken_symbol(pipeline, price_data):
    """A symbol with unusable data must not abort the whole sweep."""
    broken = price_data.copy()
    broken["CCC"] = _synthetic_ohlcv(3, n=20)  # far too short for 3y+1y windows
    pipeline._repository = GeneratedSignalRepository(broken, STRATEGY_GRID)
    output = pipeline.run(broken, list(STRATEGY_GRID), run_optimized=False)

    assert len(output.configurations) == 12  # CCC contributes nothing, others fine
    assert "CCC" not in set(output.configurations["symbol"])


def test_param_key_map_matches_strategy_param_key():
    """The repository's reconstructed keys must match what Layer 1 wrote."""
    from quant_backtester.src.strategies import MACrossover

    key_map = build_param_key_map(STRATEGY_GRID["MACrossover"])
    for key, params in key_map.items():
        assert MACrossover(**params).param_key() == key
