"""Layer 3 must never read its own output as input.

That path let an ultra-robust set from a previous run survive a run in which
nothing qualified — so the live engine kept deploying superseded research
while the funnel reported zero survivors.
"""

import pandas as pd

from quant_backtester.main import _clear_stale_selection, load_exploratory, load_selected


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def test_exploratory_selection_never_reads_the_layer3_artifact(tmp_path):
    layer2 = tmp_path / "layer2"
    layer3 = tmp_path / "layer3"
    # A stale ultra-robust set that must NOT come back.
    _write(layer3 / "ultra_robust_strategies.csv",
           [{"symbol": "STALE", "strategy": "Old", "oos_sharpe": 9.0}])
    _write(layer2 / "all_configurations.csv",
           [{"symbol": "A", "strategy": "X", "oos_sharpe": 1.0},
            {"symbol": "B", "strategy": "Y", "oos_sharpe": 0.5}])

    chosen = load_exploratory(layer2, top_n=1)

    assert list(chosen["symbol"]) == ["A"]
    assert "STALE" not in set(chosen["symbol"])


def test_exploratory_selection_without_top_n_returns_nothing(tmp_path):
    layer2 = tmp_path / "layer2"
    _write(layer2 / "all_configurations.csv",
           [{"symbol": "A", "strategy": "X", "oos_sharpe": 1.0}])

    assert load_exploratory(layer2, top_n=None).empty


def test_a_superseded_selection_is_deleted_not_left_behind(tmp_path):
    layer3 = tmp_path / "layer3"
    path = layer3 / "ultra_robust_strategies.csv"
    _write(path, [{"symbol": "STALE", "strategy": "Old", "oos_sharpe": 9.0}])

    _clear_stale_selection(layer3)

    assert not path.exists(), "a stale deployable set must not outlive its evidence"


def test_clearing_is_safe_when_there_is_nothing_to_clear(tmp_path):
    _clear_stale_selection(tmp_path / "layer3")   # must not raise


def test_load_selected_still_prefers_a_real_ultra_robust_set(tmp_path):
    """The normal path is unchanged: a genuine Layer 3 result is deployed."""
    layer2 = tmp_path / "layer2"
    layer3 = tmp_path / "layer3"
    _write(layer3 / "ultra_robust_strategies.csv",
           [{"symbol": "REAL", "strategy": "X", "oos_sharpe": 1.2}])
    _write(layer2 / "all_configurations.csv",
           [{"symbol": "OTHER", "strategy": "Y", "oos_sharpe": 2.0}])

    chosen = load_selected(layer3, layer2, top_n=5)
    assert list(chosen["symbol"]) == ["REAL"]
