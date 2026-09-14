"""Configuration loading and validation."""

from __future__ import annotations

import textwrap

import pytest

from vriddhix.config import Config, ConfigError, load_config


def test_loads_packaged_default(cfg):
    assert cfg.get("market.primary_exchange") == "NSE"
    assert cfg.get("features.atr_period") == 14
    assert cfg.source is not None


def test_dotted_access_and_defaults(cfg):
    assert cfg.get("vcp.min_base_days") == 30
    assert cfg.get("vcp.does_not_exist", 7) == 7


def test_missing_key_raises_rather_than_returning_none(cfg):
    """A typo'd threshold that silently becomes None would compare false
    against every price and disable a gate without any signal."""
    with pytest.raises(ConfigError, match="missing config key"):
        cfg.get("vcp.min_base_dayz")


def test_missing_section_raises(cfg):
    with pytest.raises(ConfigError, match="missing config section"):
        cfg.section("nope")


def test_contains(cfg):
    assert "vcp.min_base_days" in cfg
    assert "vcp.nonsense" not in cfg


# ---------------------------------------------------------------------------
# Weight validation -- the check that matters most
# ---------------------------------------------------------------------------


def _write(tmp_path, body: str):
    path = tmp_path / "cfg.yaml"
    path.write_text(textwrap.dedent(body))
    return path


MINIMAL = """
    market: {primary_exchange: NSE}
    data: {providers: [csv_cache], cache_dir: ./x}
    features:
      ema_periods: [20]
      atr_period: 14
      volume_avg_periods: [20]
      return_periods: {ret_1m: 21}
      high_low_lookback: 252
    universe: {default_index: NIFTY500}
    quality: {suspected_split_pct: 25, zero_volume_severity: WARN,
              stale_after_days: 5, max_missing_bar_streak: 5}
"""


def test_weights_must_sum_to_one(tmp_path):
    """A weight block summing to 0.95 rescales every score in the system by
    5%: invisible in any single output, corrupting across versions."""
    path = _write(tmp_path, MINIMAL + """
    scoring:
      vcp: {prior_trend: 0.5, base_quality: 0.45}
    """)
    with pytest.raises(ConfigError, match=r"sum to 0\.95"):
        load_config(path)


def test_weights_summing_to_one_pass(tmp_path):
    path = _write(tmp_path, MINIMAL + """
    scoring:
      vcp: {prior_trend: 0.5, base_quality: 0.5}
    """)
    assert load_config(path).get("scoring.vcp.prior_trend") == 0.5


def test_packaged_config_weight_blocks_are_valid(cfg):
    for block in ("scoring.vcp", "scoring.confluence", "regime.weights", "rs.horizon_weights"):
        total = sum(float(v) for v in cfg.get(block).values())
        assert total == pytest.approx(1.0), f"{block} sums to {total}"


def test_required_sections_enforced(tmp_path):
    path = _write(tmp_path, "market: {primary_exchange: NSE}\n")
    with pytest.raises(ConfigError, match="missing required config sections"):
        load_config(path)


def test_invalid_feature_periods_rejected(tmp_path):
    path = _write(tmp_path, MINIMAL.replace("atr_period: 14", "atr_period: 0"))
    with pytest.raises(ConfigError, match="atr_period"):
        load_config(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.yaml")


# ---------------------------------------------------------------------------
# Environment overrides
# ---------------------------------------------------------------------------


def test_env_override_applies_and_is_typed(tmp_path, monkeypatch):
    monkeypatch.setenv("VRIDDHIX__FEATURES__ATR_PERIOD", "21")
    path = _write(tmp_path, MINIMAL)
    loaded = load_config(path)
    assert loaded.get("features.atr_period") == 21
    assert isinstance(loaded.get("features.atr_period"), int)


def test_env_override_parses_yaml_scalars(tmp_path, monkeypatch):
    monkeypatch.setenv("VRIDDHIX__UNIVERSE__ALLOW_CURRENT_UNIVERSE_FALLBACK", "false")
    path = _write(tmp_path, MINIMAL)
    assert load_config(path).get("universe.allow_current_universe_fallback") is False


def test_config_is_frozen(cfg):
    """A service mutating config mid-run makes a scan non-reproducible while
    still appearing to succeed."""
    with pytest.raises(Exception):
        cfg.source = "elsewhere"  # type: ignore[misc]


def test_database_url_prefers_environment(monkeypatch, cfg):
    monkeypatch.setenv("VRIDDHIX_DATABASE_URL", "postgresql://host/db")
    assert cfg.database_url == "postgresql://host/db"
