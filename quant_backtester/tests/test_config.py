

def test_a_universe_can_supply_its_own_defensive_assets(tmp_path):
    """Layer 4's risk-off basket is market-specific.

    TLT and IEF have no NSE listing, so an Indian run that inherits the US
    default either fails to download them or quietly loses its defensive leg.
    """
    from quant_backtester.src.config import load_universe_config

    path = tmp_path / "u.yaml"
    path.write_text("""
data:
  source: flattrade
  cache_dir: data_x
  results_dir: results_x
  regime_benchmark: NIFTYBEES-EQ
  regime_defensive_assets: [GOLDBEES-EQ]
  lookback_years: 5
  start_date: null
  end_date: null
  price_field: "Adj Close"
  max_forward_fill_days: 3
train_test:
  split_ratio: 0.7
assets:
  core: [RELIANCE-EQ]
""")
    config = load_universe_config(path)
    assert config.data.regime_defensive_assets == ["GOLDBEES-EQ"]


def test_a_universe_without_defensive_assets_falls_back_to_the_regime_config(tmp_path):
    from quant_backtester.src.config import load_universe_config

    path = tmp_path / "u.yaml"
    path.write_text("""
data:
  source: yfinance
  cache_dir: data_x
  results_dir: results_x
  lookback_years: 5
  start_date: null
  end_date: null
  price_field: "Adj Close"
  max_forward_fill_days: 3
train_test:
  split_ratio: 0.7
assets:
  core: [SPY]
""")
    config = load_universe_config(path)
    assert config.data.regime_defensive_assets == []


def test_the_defensive_env_override_is_parsed_as_a_list(monkeypatch):
    from quant_backtester.src.config import _env_list

    monkeypatch.setenv("QB_TEST_DEFENSIVE", "A-EQ, B-EQ ,C-EQ")
    assert _env_list("QB_TEST_DEFENSIVE", ["X"]) == ["A-EQ", "B-EQ", "C-EQ"]
    monkeypatch.delenv("QB_TEST_DEFENSIVE")
    assert _env_list("QB_TEST_DEFENSIVE", ["X"]) == ["X"]
