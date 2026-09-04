from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.robustness import BootstrapStressTester, Verdict
from quant_backtester.src.config import BootstrapConfig


def _config(**overrides) -> BootstrapConfig:
    base = dict(
        num_simulations=500,
        random_seed=42,
        with_replacement=True,
        trade_source="oos",
        min_trades_required=20,
        min_p5_final_return=0.0,
        max_p95_drawdown=0.50,
    )
    base.update(overrides)
    return BootstrapConfig(**base)


def _winning_trades(n: int = 100, seed: int = 0) -> np.ndarray:
    """A strategy with a genuine edge: positive mean, modest dispersion."""
    rng = np.random.default_rng(seed)
    return rng.normal(0.02, 0.03, n)


def _losing_trades(n: int = 100, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(-0.01, 0.05, n)


# -- determinism --------------------------------------------------------


def test_same_seed_gives_identical_results():
    """A bootstrap that changed its answer between runs would be useless."""
    trades = _winning_trades()
    a = BootstrapStressTester(_config()).run(trades, original_max_drawdown=0.2)
    b = BootstrapStressTester(_config()).run(trades, original_max_drawdown=0.2)
    assert a == b


def test_same_seed_identical_even_with_nan_fields():
    """Determinism must hold for the NaN-valued fields too (a plain `==`
    would report them as different, since NaN != NaN)."""
    trades = _winning_trades()
    a = BootstrapStressTester(_config()).run(trades).to_row()
    b = BootstrapStressTester(_config()).run(trades).to_row()
    assert a.keys() == b.keys()
    for key in a:
        if isinstance(a[key], float) and np.isnan(a[key]):
            assert np.isnan(b[key])
        else:
            assert a[key] == b[key]


def test_different_seed_gives_different_draws():
    trades = _winning_trades()
    a = BootstrapStressTester(_config(random_seed=1)).run(trades)
    b = BootstrapStressTester(_config(random_seed=2)).run(trades)
    assert a.p5_final_return != b.p5_final_return


# -- verdict logic ------------------------------------------------------


def test_strong_edge_passes():
    result = BootstrapStressTester(_config()).run(_winning_trades(200))
    assert result.verdict == Verdict.PASS
    assert result.p5_final_return > 0


def test_losing_strategy_fails():
    result = BootstrapStressTester(_config()).run(_losing_trades(200))
    assert result.verdict == Verdict.FAIL
    assert "5th-percentile" in result.reason


def test_high_variance_strategy_fails_on_drawdown():
    """Large, wild trades can have a positive mean yet still blow through
    the drawdown ceiling once reshuffled."""
    rng = np.random.default_rng(5)
    trades = rng.normal(0.01, 0.35, 200)
    result = BootstrapStressTester(_config()).run(trades)
    assert result.verdict == Verdict.FAIL
    assert result.p95_max_drawdown > 0.50


def test_insufficient_trades_returns_sentinel_not_a_verdict():
    result = BootstrapStressTester(_config(min_trades_required=20)).run(
        np.array([0.01, 0.02, -0.01])
    )
    assert result.verdict == Verdict.INSUFFICIENT_DATA
    assert result.num_simulations == 0
    assert "only 3 trades" in result.reason


# -- distribution mechanics --------------------------------------------


def test_output_shapes_and_ordering():
    result = BootstrapStressTester(_config()).run(_winning_trades(150))
    assert result.num_trades == 150
    assert result.num_simulations == 500
    assert result.p5_final_return <= result.p50_final_return <= result.p95_final_return
    assert result.mean_max_drawdown <= result.p95_max_drawdown <= result.worst_max_drawdown


def test_drawdown_amplification_vs_original():
    trades = _winning_trades(150)
    result = BootstrapStressTester(_config()).run(trades, original_max_drawdown=0.20)
    assert result.original_max_drawdown == pytest.approx(0.20)
    assert result.drawdown_amplification == pytest.approx(result.p95_max_drawdown / 0.20)


def test_amplification_is_nan_without_original_drawdown():
    result = BootstrapStressTester(_config()).run(_winning_trades(150))
    assert np.isnan(result.drawdown_amplification)


def test_all_winners_never_draws_down():
    trades = np.full(100, 0.01)
    result = BootstrapStressTester(_config()).run(trades)
    assert result.worst_max_drawdown == pytest.approx(0.0, abs=1e-12)
    assert result.probability_of_loss == 0.0


def test_all_losers_always_loses():
    trades = np.full(100, -0.01)
    result = BootstrapStressTester(_config()).run(trades)
    assert result.probability_of_loss == 1.0
    assert result.verdict == Verdict.FAIL


def test_permutation_mode_preserves_final_equity():
    """Without replacement, every universe holds the same multiset of trades,
    so compounded final equity is identical in all of them — only the path
    (and therefore the drawdown) differs."""
    trades = _winning_trades(60)
    result = BootstrapStressTester(_config(with_replacement=False)).run(trades)
    assert result.std_final_equity == pytest.approx(0.0, abs=1e-9)
    # ...but the drawdown still varies across orderings
    assert result.worst_max_drawdown > result.mean_max_drawdown


def test_replacement_mode_varies_final_equity():
    trades = _winning_trades(60)
    result = BootstrapStressTester(_config(with_replacement=True)).run(trades)
    assert result.std_final_equity > 0


def test_loss_streaks_are_sane():
    trades = _winning_trades(120)
    result = BootstrapStressTester(_config()).run(trades)
    assert 1 <= result.mean_max_loss_streak <= 120
    assert result.worst_loss_streak >= result.p95_max_loss_streak


def test_loss_streak_of_all_losers_is_full_length():
    trades = np.full(50, -0.01)
    result = BootstrapStressTester(_config(min_trades_required=10)).run(trades)
    assert result.worst_loss_streak == 50


def test_accepts_pandas_series():
    trades = pd.Series(_winning_trades(100))
    result = BootstrapStressTester(_config()).run(trades)
    assert result.num_trades == 100


def test_non_finite_trades_are_dropped():
    trades = np.concatenate([_winning_trades(100), [np.nan, np.inf]])
    result = BootstrapStressTester(_config()).run(trades)
    assert result.num_trades == 100


def test_to_row_is_flat_and_serializable():
    row = BootstrapStressTester(_config()).run(_winning_trades(100)).to_row()
    assert row["bootstrap_verdict"] in {v.value for v in Verdict}
    assert all(not isinstance(v, (list, dict)) for v in row.values())
