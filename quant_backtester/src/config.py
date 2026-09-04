"""Configuration loading for Layer 1.

Single Responsibility: this module is the *only* place that reads YAML files
or environment variables. Every other module receives already-resolved,
typed config objects — no `os.getenv()` or `open(...).yaml` calls scattered
through data/strategy/runner code.

Environment variables (all optional) override the YAML values, so secrets or
per-environment paths never need to be committed:
  QB_CACHE_DIR, QB_RESULTS_DIR, QB_START_DATE, QB_END_DATE, QB_LOOKBACK_YEARS
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class FlattradeCredentials:
    """Flattrade app credentials, read from configfile.ini [flattrade].

    Only the app-level keys live here. The daily session token is NOT stored
    in config — it is minted by flattrade_token.py into a git-ignored file.
    """

    api_key: str
    api_secret: str
    client_id: str
    redirect_uri: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret and self.client_id)


def load_flattrade_credentials(
    path: str | Path = REPO_ROOT.parent / "configfile.ini",
) -> FlattradeCredentials:
    """Environment overrides configfile.ini, so a deployment can inject
    credentials without editing a tracked file."""
    import configparser

    values = {
        "api_key": os.environ.get("FLATTRADE_API_KEY", ""),
        "api_secret": os.environ.get("FLATTRADE_API_SECRET", ""),
        "client_id": os.environ.get("FLATTRADE_CLIENT_ID", ""),
        "redirect_uri": os.environ.get("FLATTRADE_REDIRECT_URI", ""),
    }
    path = Path(path)
    if path.exists():
        parser = configparser.ConfigParser()
        parser.read(path)
        if parser.has_section("flattrade"):
            for key in values:
                if not values[key] and parser.has_option("flattrade", key):
                    values[key] = parser.get("flattrade", key)
    return FlattradeCredentials(**values)


@dataclass(frozen=True)
class DataConfig:
    cache_dir: Path
    lookback_years: int
    start_date: date | None
    end_date: date | None
    price_field: str
    max_forward_fill_days: int
    # "yfinance" (US markets) | "flattrade" (Indian markets). Chosen per
    # universe, because the two providers cover disjoint instruments.
    source: str = "yfinance"
    # Output directory for this universe. Scoped per universe because results
    # from one market must never be read back while running another — a US
    # survivor list silently reused for an Indian run is invisible corruption.
    results_dir: str = "results"
    regime_benchmark: str = ""
    # Layer 4's risk-off basket. Market-specific like the benchmark: TLT and
    # IEF have no NSE listing, and a run that reaches for them either fails
    # to download or, worse, mixes US bonds into an Indian portfolio.
    regime_defensive_assets: list[str] = field(default_factory=list)
    # Ordered alternatives when `source` cannot supply a symbol's history.
    # Only list providers covering the SAME market — yfinance cannot price
    # RELIANCE-EQ, so naming it here would turn an outage into bad data.
    source_fallbacks: list[str] = field(default_factory=list)

    def resolved_start(self) -> date:
        if self.start_date is not None:
            return self.start_date
        return self.resolved_end() - timedelta(days=365 * self.lookback_years)

    def resolved_end(self) -> date:
        return self.end_date if self.end_date is not None else date.today()


@dataclass(frozen=True)
class TrainTestConfig:
    split_ratio: float


@dataclass(frozen=True)
class OutputConfig:
    results_dir: Path
    format: str  # "parquet" | "csv"


@dataclass(frozen=True)
class UniverseConfig:
    data: DataConfig
    train_test: TrainTestConfig
    asset_groups: dict[str, list[str]]

    @property
    def all_symbols(self) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for symbols in self.asset_groups.values():
            for symbol in symbols:
                if symbol not in seen:
                    seen.add(symbol)
                    ordered.append(symbol)
        return ordered


@dataclass(frozen=True)
class StrategyGridConfig:
    strategies: dict[str, dict[str, list]]
    output: OutputConfig


@dataclass(frozen=True)
class WalkForwardSettings:
    mode: str
    is_years: int
    oos_years: int
    step_years: int
    min_oos_days: int
    selection_metric: str


@dataclass(frozen=True)
class BacktestConfig:
    """Layer 2 configuration: execution drag, WFA windows, funnel thresholds."""

    # None => infer each asset's own calendar (252 for equities, 365 for
    # crypto) rather than forcing one global figure.
    periods_per_year: int | None
    risk_free_rate: float
    costs: dict
    walk_forward: WalkForwardSettings
    funnel: dict
    signals_dir: Path
    layer2_dir: Path
    # Bar-level stops/targets. Empty (or enabled: false) means naked
    # backtests, which is the default — see risk.py for why.
    risk_overlay: dict = field(default_factory=dict)


def _env_list(name: str, default: list) -> list[str]:
    """A comma-separated env override, so a universe can supply its own list."""
    raw = os.environ.get(name)
    return [s.strip() for s in raw.split(",") if s.strip()] if raw else list(default)


def _parse_periods(value) -> int | None:
    """`auto`/null means infer per asset; a number pins every asset to it."""
    if value in (None, "auto", "null", ""):
        return None
    return int(value)


def _parse_date(value: str | None) -> date | None:
    if value in (None, "null", ""):
        return None
    return date.fromisoformat(value)


def _resolve_path(raw: str, base_dir: Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else (base_dir / path)


def load_universe_config(
    path: str | Path = REPO_ROOT / "config" / "universe.yaml",
) -> UniverseConfig:
    """Load asset universe + data-acquisition config, with env-var overrides."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    base_dir = path.resolve().parent.parent  # quant_backtester/
    raw_data = raw["data"]

    cache_dir = os.environ.get("QB_CACHE_DIR", raw_data["cache_dir"])
    start_date = os.environ.get("QB_START_DATE") or raw_data.get("start_date")
    end_date = os.environ.get("QB_END_DATE") or raw_data.get("end_date")
    lookback_years = int(os.environ.get("QB_LOOKBACK_YEARS", raw_data["lookback_years"]))

    data_config = DataConfig(
        cache_dir=_resolve_path(cache_dir, base_dir),
        lookback_years=lookback_years,
        start_date=_parse_date(start_date),
        end_date=_parse_date(end_date),
        price_field=raw_data["price_field"],
        max_forward_fill_days=int(raw_data["max_forward_fill_days"]),
        source=os.environ.get("QB_DATA_SOURCE", raw_data.get("source", "yfinance")),
        results_dir=raw_data.get("results_dir", "results"),
        regime_benchmark=raw_data.get("regime_benchmark", ""),
        regime_defensive_assets=list(raw_data.get("regime_defensive_assets", []) or []),
        source_fallbacks=list(raw_data.get("source_fallbacks", []) or []),
    )
    train_test_config = TrainTestConfig(split_ratio=float(raw["train_test"]["split_ratio"]))

    return UniverseConfig(
        data=data_config,
        train_test=train_test_config,
        asset_groups=raw["assets"],
    )


def load_strategy_grid_config(
    path: str | Path = REPO_ROOT / "config" / "strategy_grid.yaml",
) -> StrategyGridConfig:
    """Load the strategy parameter-sweep grid, with env-var override for output dir."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    base_dir = path.resolve().parent.parent  # quant_backtester/
    results_dir = os.environ.get("QB_RESULTS_DIR", raw["output"]["results_dir"])

    output_config = OutputConfig(
        results_dir=_resolve_path(results_dir, base_dir),
        format=raw["output"]["format"],
    )
    return StrategyGridConfig(strategies=raw["strategies"], output=output_config)


@dataclass(frozen=True)
class SensitivityConfig:
    int_offsets: list[int]
    float_relative_offsets: list[float]
    metric: str
    min_retention_ratio: float
    min_stability_fraction: float


@dataclass(frozen=True)
class BootstrapConfig:
    num_simulations: int
    random_seed: int
    with_replacement: bool
    trade_source: str
    min_trades_required: int
    min_p5_final_return: float
    max_p95_drawdown: float


@dataclass(frozen=True)
class RobustnessConfig:
    sensitivity: SensitivityConfig
    bootstrap: BootstrapConfig
    robustness_dir: Path


def load_robustness_config(
    path: str | Path = REPO_ROOT / "config" / "robustness.yaml",
) -> RobustnessConfig:
    """Load Layer 3 config (sensitivity neighbourhood + bootstrap settings)."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    base_dir = path.resolve().parent.parent  # quant_backtester/
    sens, boot = raw["sensitivity"], raw["bootstrap"]

    robustness_dir = os.environ.get("QB_LAYER3_DIR", raw["output"]["robustness_dir"])
    seed = int(os.environ.get("QB_BOOTSTRAP_SEED", boot["random_seed"]))

    return RobustnessConfig(
        sensitivity=SensitivityConfig(
            int_offsets=[int(v) for v in sens["int_offsets"]],
            float_relative_offsets=[float(v) for v in sens["float_relative_offsets"]],
            metric=sens["metric"],
            min_retention_ratio=float(sens["min_retention_ratio"]),
            min_stability_fraction=float(sens["min_stability_fraction"]),
        ),
        bootstrap=BootstrapConfig(
            num_simulations=int(boot["num_simulations"]),
            random_seed=seed,
            with_replacement=bool(boot["with_replacement"]),
            trade_source=boot["trade_source"],
            min_trades_required=int(boot["min_trades_required"]),
            min_p5_final_return=float(boot["min_p5_final_return"]),
            max_p95_drawdown=float(boot["max_p95_drawdown"]),
        ),
        robustness_dir=_resolve_path(robustness_dir, base_dir),
    )


def load_backtest_config(
    path: str | Path = REPO_ROOT / "config" / "backtest.yaml",
) -> BacktestConfig:
    """Load Layer 2 config (execution costs, WFA windows, funnel thresholds)."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    base_dir = path.resolve().parent.parent  # quant_backtester/
    wf = raw["walk_forward"]
    output = raw["output"]

    signals_dir = os.environ.get("QB_RESULTS_DIR", output["results_dir"])
    layer2_dir = os.environ.get("QB_LAYER2_DIR", output["layer2_dir"])

    return BacktestConfig(
        periods_per_year=_parse_periods(raw["execution"]["periods_per_year"]),
        risk_free_rate=float(raw["execution"]["risk_free_rate"]),
        costs=raw["costs"],
        walk_forward=WalkForwardSettings(
            mode=wf["mode"],
            is_years=int(wf["is_years"]),
            oos_years=int(wf["oos_years"]),
            step_years=int(wf["step_years"]),
            min_oos_days=int(wf["min_oos_days"]),
            selection_metric=wf["selection_metric"],
        ),
        funnel=raw["funnel"],
        risk_overlay=raw.get("risk_overlay", {}) or {},
        signals_dir=_resolve_path(signals_dir, base_dir),
        layer2_dir=_resolve_path(layer2_dir, base_dir),
    )


@dataclass(frozen=True)
class RegimeDetectionConfig:
    benchmark_symbol: str
    n_states: int
    volatility_window: int
    fast_ma: int
    slow_ma: int
    atr_period: int
    covariance_type: str
    n_iter: int
    random_seed: int
    train_fraction: float
    min_regime_persistence_days: int


@dataclass(frozen=True)
class RiskConfig:
    target_annual_volatility: float
    atr_period: int
    max_position_leverage: float
    max_portfolio_heat: float
    drawdown_window_days: int
    drawdown_threshold: float
    drawdown_scale_factor: float
    recovery_days: int


@dataclass(frozen=True)
class AllocationConfig:
    regime_strategies: dict[int, list[str]]
    regime_exposure: dict[int, float]
    defensive_assets: list[str]
    correlation_window: int
    min_weight: float


@dataclass(frozen=True)
class RegimeConfig:
    """Layer 4 configuration."""

    detection: RegimeDetectionConfig
    risk: RiskConfig
    allocation: AllocationConfig
    regime_dir: Path


def load_regime_config(
    path: str | Path = REPO_ROOT / "config" / "regime.yaml",
) -> RegimeConfig:
    """Load Layer 4 config (regime detection, risk gates, capital routing)."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    base_dir = path.resolve().parent.parent  # quant_backtester/
    reg, risk, alloc = raw["regime"], raw["risk"], raw["allocation"]

    regime_dir = os.environ.get("QB_LAYER4_DIR", raw["output"]["regime_dir"])
    seed = int(os.environ.get("QB_REGIME_SEED", reg["random_seed"]))

    return RegimeConfig(
        detection=RegimeDetectionConfig(
            # A universe-scoped override: the regime benchmark must be an
            # instrument the active market actually lists.
            benchmark_symbol=os.environ.get(
                "QB_REGIME_BENCHMARK", reg["benchmark_symbol"]
            ),
            n_states=int(reg["n_states"]),
            volatility_window=int(reg["volatility_window"]),
            fast_ma=int(reg["fast_ma"]),
            slow_ma=int(reg["slow_ma"]),
            atr_period=int(reg["atr_period"]),
            covariance_type=reg["covariance_type"],
            n_iter=int(reg["n_iter"]),
            random_seed=seed,
            train_fraction=float(reg["train_fraction"]),
            min_regime_persistence_days=int(reg["min_regime_persistence_days"]),
        ),
        risk=RiskConfig(
            target_annual_volatility=float(risk["target_annual_volatility"]),
            atr_period=int(risk["atr_period"]),
            max_position_leverage=float(risk["max_position_leverage"]),
            max_portfolio_heat=float(risk["max_portfolio_heat"]),
            drawdown_window_days=int(risk["drawdown_window_days"]),
            drawdown_threshold=float(risk["drawdown_threshold"]),
            drawdown_scale_factor=float(risk["drawdown_scale_factor"]),
            recovery_days=int(risk["recovery_days"]),
        ),
        allocation=AllocationConfig(
            regime_strategies={int(k): list(v) for k, v in alloc["regime_strategies"].items()},
            regime_exposure={int(k): float(v) for k, v in alloc["regime_exposure"].items()},
            defensive_assets=_env_list("QB_REGIME_DEFENSIVE", alloc["defensive_assets"]),
            correlation_window=int(alloc["correlation_window"]),
            min_weight=float(alloc["min_weight"]),
        ),
        regime_dir=_resolve_path(regime_dir, base_dir),
    )
