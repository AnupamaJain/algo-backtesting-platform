from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def synthetic_ohlcv() -> pd.DataFrame:
    """Deterministic, seeded OHLCV series — no network, no randomness across
    test runs."""
    rng = np.random.default_rng(seed=42)
    n = 500
    dates = pd.bdate_range("2020-01-01", periods=n)

    returns = rng.normal(loc=0.0003, scale=0.01, size=n)
    close = 100 * np.cumprod(1 + returns)
    high = close * (1 + rng.uniform(0, 0.01, size=n))
    low = close * (1 - rng.uniform(0, 0.01, size=n))
    open_ = low + (high - low) * rng.uniform(0, 1, size=n)
    volume = rng.integers(1_000_000, 5_000_000, size=n)

    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=dates,
    )
