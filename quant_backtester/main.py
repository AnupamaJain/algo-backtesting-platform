#!/usr/bin/env python
"""Quant backtester — unified system runner.

Layers:
  1  data ingestion + signal generation across the asset universe
  2  walk-forward backtest + 6-stage validation funnel
  3  parameter sensitivity + bootstrap stress testing
  4  HMM regime detection + dynamic portfolio vs. static baselines
  pead  post-earnings announcement drift study (event-driven, separate from indicator layers)

Usage:
    python main.py all                       # run the whole pipeline (layers 1-4)
    python main.py layer1                    # generate signals only
    python main.py layer2 --symbols SPY,QQQ
    python main.py layer3 --top-n 12
    python main.py layer4 --top-n 12
    python main.py pead                      # run the PEAD event-driven study
    python main.py pead --min-surprise 3.0 --holding-days 15
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quant_backtester.src.backtest import (
    BatchSignalGenerator,
    FileSignalRepository,
    GeneratedSignalRepository,
    Layer2Pipeline,
    ValidationFunnel,
    VectorizedBacktester,
    WalkForwardEngine,
    WindowGenerator,
    build_cost_model,
)
from quant_backtester.src.risk import RiskOverlay
from quant_backtester.src.config import (
    load_backtest_config,
    load_regime_config,
    load_robustness_config,
    load_strategy_grid_config,
    load_universe_config,
)
from quant_backtester.src.data_loader import HistoricalDataManager
from quant_backtester.src.regime import (
    BuyAndHoldPortfolio,
    DynamicRegimePortfolio,
    MarketRegimeDetector,
    PerformanceComparator,
    RiskManager,
    SignalBlender,
    StaticPortfolio,
)
from quant_backtester.src.robustness import (
    BootstrapStressTester,
    ParameterSensitivityChecker,
    RobustnessSuite,
)
from quant_backtester.src.pead import PEADConfig, pead_portfolio_returns, exposure_stats
from quant_backtester.src.events import EarningsCalendar

def scope_outputs_to_universe(universe_config) -> None:
    """Point every layer's output at this universe's own results tree.

    Layers 2-4 read each other's artifacts by path. Without scoping, running
    an Indian universe would happily load the survivor list from a previous US
    run and "evaluate" QQQ against NSE prices — which is exactly what happened
    before this existed. Silent, and wrong in a way no error surfaces.
    """
    root = universe_config.data.results_dir
    os.environ.setdefault("QB_RESULTS_DIR", root)
    os.environ["QB_RESULTS_DIR"] = root
    os.environ["QB_LAYER2_DIR"] = f"{root}/layer2"
    os.environ["QB_LAYER3_DIR"] = f"{root}/layer3"
    os.environ["QB_LAYER4_DIR"] = f"{root}/layer4"
    if universe_config.data.regime_benchmark:
        os.environ["QB_REGIME_BENCHMARK"] = universe_config.data.regime_benchmark
    if universe_config.data.regime_defensive_assets:
        # Without this the Indian run reaches for TLT/IEF, which have no NSE
        # listing — the download fails and Layer 4 silently loses its
        # risk-off leg.
        os.environ["QB_REGIME_DEFENSIVE"] = ",".join(
            universe_config.data.regime_defensive_assets
        )


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("main")

CONFIG_DIR = Path(__file__).parent / "config"


# ==========================================================================
# Shared construction
# ==========================================================================


def build_engine(backtest_config) -> WalkForwardEngine:
    wf = backtest_config.walk_forward
    return WalkForwardEngine(
        backtester=VectorizedBacktester(
            cost_model=build_cost_model(backtest_config.costs),
            risk_free_rate=backtest_config.risk_free_rate,
            periods_per_year=backtest_config.periods_per_year,
            risk_overlay=RiskOverlay.from_config(backtest_config.risk_overlay),
        ),
        window_generator=WindowGenerator(
            is_years=wf.is_years,
            oos_years=wf.oos_years,
            step_years=wf.step_years,
            mode=wf.mode,
            min_oos_days=wf.min_oos_days,
        ),
        risk_free_rate=backtest_config.risk_free_rate,
        periods_per_year=backtest_config.periods_per_year,
        selection_metric=wf.selection_metric,
    )


def resolve_symbols(args, universe_config) -> list[str]:
    if args.symbols:
        return [s.strip() for s in args.symbols.split(",")]
    return universe_config.all_symbols


def read_artifact(path: Path) -> pd.DataFrame:
    """Read a pipeline artifact, tolerating the empty case.

    "Nothing survived" is a legitimate result that produces an empty file.
    Treating that as a crash turns a valid finding into an outage.
    """
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def load_exploratory(layer2_dir: Path, top_n: int | None) -> pd.DataFrame:
    """The top-N configurations, explicitly labelled as not having passed.

    Separate from `load_selected` so Layer 3 can never read its own output as
    input — the path that let a stale ultra-robust set survive a run in which
    nothing qualified.
    """
    if not top_n:
        return pd.DataFrame()
    frame = read_artifact(layer2_dir / "all_configurations.csv")
    if frame.empty:
        return frame
    frame = frame.nlargest(top_n, "oos_sharpe")
    logger.warning(
        "Nothing cleared the funnel; evaluating the top %s configurations by "
        "OOS Sharpe. These are EXPLORATORY — they did not pass validation.",
        len(frame),
    )
    return frame


def _clear_stale_selection(layer3_dir: Path) -> None:
    """Remove a previous run's deployable set once it no longer holds."""
    path = layer3_dir / "ultra_robust_strategies.csv"
    if path.exists():
        logger.warning(
            "Nothing to evaluate; removing the previous ultra-robust set at %s "
            "so the live engine cannot deploy superseded research.",
            path,
        )
        path.unlink()


def load_selected(layer3_dir: Path, layer2_dir: Path, top_n: int | None) -> pd.DataFrame:
    """Load the strategies to deploy: Layer 3's ultra-robust set if it exists,
    otherwise the top-N configurations as an explicitly exploratory fallback."""
    ultra = layer3_dir / "ultra_robust_strategies.csv"
    if ultra.exists():
        # A header-less/empty artifact is normal when nothing survived; it must
        # read as "no rows" rather than raising EmptyDataError.
        frame = read_artifact(ultra)
        if not frame.empty:
            logger.info("Deploying %s ultra-robust strategies", len(frame))
            return frame

    if top_n:
        path = layer2_dir / "all_configurations.csv"
        frame = read_artifact(path)
        if not frame.empty:
            frame = frame.nlargest(top_n, "oos_sharpe")
            logger.warning(
                "No ultra-robust set; deploying the top %s configurations by OOS "
                "Sharpe. These did NOT clear the full gauntlet — exploratory only.",
                len(frame),
            )
            return frame
    logger.error("Nothing to deploy. Run layers 1-3 first, or pass --top-n.")
    return pd.DataFrame()


# ==========================================================================
# Layers
# ==========================================================================


def run_layer1(args) -> None:
    universe_config = load_universe_config(args.universe_config)
    scope_outputs_to_universe(universe_config)
    grid_config = load_strategy_grid_config(args.grid_config)
    symbols = resolve_symbols(args, universe_config)

    data_manager = HistoricalDataManager(universe_config.data)
    generator = BatchSignalGenerator(data_manager, grid_config.strategies, grid_config.output)
    results = generator.run(symbols)
    failures = [r for r in results if r.status == "error"]
    logger.info("Layer 1: %s configurations, %s failures", len(results), len(failures))


def run_layer2(args) -> None:
    universe_config = load_universe_config(args.universe_config)
    scope_outputs_to_universe(universe_config)
    grid_config = load_strategy_grid_config(args.grid_config)
    backtest_config = load_backtest_config(args.backtest_config)
    symbols = resolve_symbols(args, universe_config)

    data_manager = HistoricalDataManager(universe_config.data)
    price_data = data_manager.get_universe_history(symbols)
    if not price_data:
        logger.error("No price data; aborting.")
        return

    engine = build_engine(backtest_config)
    repository = (
        GeneratedSignalRepository(price_data, grid_config.strategies)
        if args.generate_signals
        else FileSignalRepository(
            backtest_config.signals_dir, grid_config.strategies, grid_config.output.format
        )
    )
    pipeline = Layer2Pipeline(
        engine,
        repository,
        ValidationFunnel.from_config(
            backtest_config.funnel, periods_per_year=backtest_config.periods_per_year
        ),
        backtest_config,
    )
    output = pipeline.run(price_data, list(grid_config.strategies), run_optimized=True)
    pipeline.write_artifacts(output)

    print("\n=== Validation Funnel ===")
    print(output.funnel_result.funnel_report().to_string(index=False))
    survivors = output.funnel_result.summary_table()
    print(f"\n=== Survivors ({len(survivors)}) ===")
    print(survivors.head(20).to_string(index=False) if not survivors.empty else "(none)")


def run_layer3(args) -> None:
    universe_config = load_universe_config(args.universe_config)
    scope_outputs_to_universe(universe_config)
    backtest_config = load_backtest_config(args.backtest_config)
    robustness_config = load_robustness_config(args.robustness_config)

    survivors_path = backtest_config.layer2_dir / "survivors.csv"
    survivors = read_artifact(survivors_path)
    if survivors.empty:
        # NOT load_selected() here. That reads ultra_robust_strategies.csv,
        # which is THIS layer's own output — so a stale set from a previous
        # run would feed straight back in and re-bless itself, and the
        # deployed strategies would silently stop reflecting the funnel.
        # When nothing survived, the honest input is the exploratory top-N
        # or nothing at all.
        survivors = load_exploratory(backtest_config.layer2_dir, args.top_n)
    if survivors.empty:
        # Clear the previous run's artifact: leaving it in place is what lets
        # the live engine deploy research that no longer holds.
        _clear_stale_selection(robustness_config.robustness_dir)
        return

    data_manager = HistoricalDataManager(universe_config.data)
    price_data = data_manager.get_universe_history(sorted(set(survivors["symbol"])))

    # Belt and braces: even with scoped output paths, never evaluate a symbol
    # this universe cannot price.
    known = set(price_data)
    unknown = sorted(set(survivors["symbol"]) - known)
    if unknown:
        logger.warning(
            "Dropping %s selection(s) with no price data in this universe: %s",
            len(unknown), ", ".join(unknown[:8]),
        )
        survivors = survivors[survivors["symbol"].isin(known)]
    if survivors.empty:
        logger.error("No selections remain after filtering to this universe.")
        return

    engine = build_engine(backtest_config)

    suite = RobustnessSuite(
        sensitivity_checker=ParameterSensitivityChecker(engine, robustness_config.sensitivity),
        bootstrap_tester=BootstrapStressTester(robustness_config.bootstrap),
        engine=engine,
        config=robustness_config,
    )
    output = suite.run(survivors, price_data)
    suite.write_artifacts(output)

    if output.annotated.empty:
        print("\nNo configurations could be evaluated.")
        return
    columns = [
        c
        for c in (
            "symbol",
            "strategy",
            "oos_sharpe",
            "sensitivity_score",
            "is_stable",
            "bootstrap_p5_final_return",
            "bootstrap_p95_max_drawdown",
            "bootstrap_verdict",
        )
        if c in output.annotated.columns
    ]
    print("\n=== Robustness ===")
    print(output.annotated[columns].round(3).to_string(index=False))
    print(f"\n=== Ultra-robust ({len(output.ultra_robust)}) ===")
    print(
        output.ultra_robust[columns].round(3).to_string(index=False)
        if not output.ultra_robust.empty
        else "(none)"
    )


def run_layer4(args) -> None:
    universe_config = load_universe_config(args.universe_config)
    scope_outputs_to_universe(universe_config)
    backtest_config = load_backtest_config(args.backtest_config)
    robustness_config = load_robustness_config(args.robustness_config)
    regime_config = load_regime_config(args.regime_config)

    selected = load_selected(
        robustness_config.robustness_dir, backtest_config.layer2_dir, args.top_n
    )
    if selected.empty:
        return

    data_manager = HistoricalDataManager(universe_config.data)
    symbols = sorted(
        set(selected["symbol"])
        | {regime_config.detection.benchmark_symbol}
        | set(regime_config.allocation.defensive_assets)
    )
    price_data = data_manager.get_universe_history(symbols)

    benchmark = price_data.get(regime_config.detection.benchmark_symbol)
    if benchmark is None:
        logger.error(
            "Benchmark %s unavailable; cannot detect regimes.",
            regime_config.detection.benchmark_symbol,
        )
        return

    detector = MarketRegimeDetector(regime_config.detection).fit(benchmark)
    risk_manager = RiskManager(regime_config.risk)
    backtester = VectorizedBacktester(
        cost_model=build_cost_model(backtest_config.costs),
        risk_free_rate=backtest_config.risk_free_rate,
        periods_per_year=backtest_config.periods_per_year,
        risk_overlay=RiskOverlay.from_config(backtest_config.risk_overlay),
    )

    dynamic = DynamicRegimePortfolio(
        detector=detector,
        risk_manager=risk_manager,
        backtester=backtester,
        allocation=regime_config.allocation,
        blender=SignalBlender(
            regime_config.allocation.correlation_window, regime_config.allocation.min_weight
        ),
    ).run(price_data, selected, benchmark)

    static = StaticPortfolio(risk_manager, backtester).run(price_data, selected)
    buy_hold = BuyAndHoldPortfolio(backtester).run(
        {s: price_data[s] for s in sorted(set(selected["symbol"])) if s in price_data}
    )

    comparator = PerformanceComparator(regime_config)
    portfolios = [dynamic, static, buy_hold]
    comparator.write_artifacts(portfolios, dynamic=dynamic)

    print("\n=== Portfolio Comparison ===")
    print(comparator.compare(portfolios).round(4).to_string(index=False))

    regime_counts = dynamic.regimes.value_counts().sort_index()
    print("\n=== Regime Distribution (days) ===")
    for code, count in regime_counts.items():
        from quant_backtester.src.regime import Regime

        print(f"  {int(code)} {Regime.name(int(code)):<16} {count:>6}")

    attribution = comparator.regime_attribution(dynamic)
    if not attribution.empty:
        print("\n=== Dynamic Portfolio: performance by regime ===")
        print(attribution.round(4).to_string(index=False))

    correlation = comparator.rolling_correlation(portfolios)
    if not correlation.empty:
        print("\n=== Rolling correlation (mean over full period) ===")
        print(correlation.mean().round(3).to_string())


def run_pead(args) -> None:
    """Post-earnings announcement drift study.

    Downloads earnings calendars for every symbol in the universe, builds
    the PEAD signal series for each (entry strictly after the announcement,
    holding for `holding_days` sessions), backtests them through the same
    VectorizedBacktester as the indicator strategies, and writes results to
    `results/pead/`.

    Results are descriptive research, not a validated deployable set —
    no funnel is applied because PEAD is not a parameter-sweep strategy.
    """
    universe_config = load_universe_config(args.universe_config)
    scope_outputs_to_universe(universe_config)
    backtest_config = load_backtest_config(args.backtest_config)
    symbols = resolve_symbols(args, universe_config)

    pead_config = PEADConfig(
        min_surprise_pct=args.min_surprise,
        holding_days=args.holding_days,
        direction=args.direction,
    )
    logger.info("PEAD config: %s", pead_config.describe())

    data_manager = HistoricalDataManager(universe_config.data)
    price_data = data_manager.get_universe_history(symbols)
    if not price_data:
        logger.error("No price data; aborting PEAD study.")
        return

    # Load earnings calendars from the data_events/ directory.
    events_dir = Path(__file__).parent / "data_events"
    calendar = EarningsCalendar(events_dir)
    calendars = calendar.load_many(symbols)

    loaded = [s for s in symbols if s in calendars and calendars[s]]
    missing = [s for s in symbols if s not in loaded]
    logger.info(
        "Earnings calendars: %s symbols loaded, %s without data",
        len(loaded), len(missing),
    )
    if missing:
        logger.debug("No earnings data for: %s", ", ".join(missing[:10]))

    if not loaded:
        logger.error(
            "No earnings calendar data found in %s. "
            "Download earnings data first (see data_events/).",
            events_dir,
        )
        return

    from quant_backtester.src.backtest import VectorizedBacktester, build_cost_model
    from quant_backtester.src.risk import RiskOverlay

    backtester = VectorizedBacktester(
        cost_model=build_cost_model(backtest_config.costs),
        risk_free_rate=backtest_config.risk_free_rate,
        periods_per_year=backtest_config.periods_per_year,
        risk_overlay=RiskOverlay.from_config(backtest_config.risk_overlay),
    )

    # Exposure summary — logged before the backtest so the numbers are
    # visible even if the backtest itself fails.
    exposure = exposure_stats(price_data, calendars, pead_config)
    logger.info(
        "PEAD exposure: %.1f%% of bars in-market (%s position changes)",
        exposure["time_in_market_pct"],
        exposure["position_changes"],
    )

    portfolio_returns = pead_portfolio_returns(price_data, calendars, pead_config, backtester)

    if portfolio_returns.empty or (portfolio_returns == 0).all():
        logger.warning("No PEAD trades generated — check that surprise thresholds are not too tight.")
        return

    # Compute summary statistics using the backtester's own metric functions.
    from quant_backtester.src.backtest import compute_metrics, infer_periods_per_year
    import numpy as np

    periods_per_year = (
        backtest_config.periods_per_year
        if backtest_config.periods_per_year is not None
        else infer_periods_per_year(portfolio_returns.index)
    )
    position = (portfolio_returns != 0).astype(float)
    metrics = compute_metrics(portfolio_returns, position, periods_per_year=periods_per_year)

    print("\n=== PEAD Study ===")
    print(f"Config:          {pead_config.describe()}")
    print(f"Symbols:         {len(loaded)} with earnings data")
    print(f"Time in market:  {exposure['time_in_market_pct']:.1f}%")
    print(f"Position changes:{exposure['position_changes']}")
    print(f"Total return:    {metrics.total_return:.2%}")
    print(f"CAGR:            {metrics.cagr:.2%}")
    print(f"Sharpe:          {metrics.sharpe:.3f}")
    print(f"Max drawdown:    {metrics.max_drawdown:.2%}")
    print(f"Profit factor:   {metrics.profit_factor:.3f}")
    print(f"Win rate:        {metrics.win_rate:.2%}")
    print(f"Trades:          {metrics.num_trades}")

    # Write results to results/pead/.
    results_root = Path(os.environ.get("QB_RESULTS_DIR", "results"))
    pead_dir = results_root / "pead"
    pead_dir.mkdir(parents=True, exist_ok=True)

    import json as _json
    summary = {
        "config": {
            "min_surprise_pct": pead_config.min_surprise_pct,
            "holding_days": pead_config.holding_days,
            "direction": pead_config.direction,
        },
        "exposure": exposure,
        "metrics": metrics.to_dict(),
        "symbols_with_data": loaded,
        "symbols_missing_data": missing,
    }
    (pead_dir / "summary.json").write_text(_json.dumps(summary, indent=2, default=str))

    portfolio_returns.to_csv(pead_dir / "portfolio_returns.csv", header=["return"])
    logger.info("PEAD artifacts written to %s", pead_dir)


def run_all(args) -> None:
    for step in (run_layer1, run_layer2, run_layer3, run_layer4):
        logger.info("=" * 70)
        logger.info("Running %s", step.__name__)
        logger.info("=" * 70)
        step(args)


# ==========================================================================
# CLI
# ==========================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "layer",
        choices=["layer1", "layer2", "layer3", "layer4", "pead", "all"],
        help="Which layer to run.",
    )
    parser.add_argument("--symbols", default=None, help="Comma-separated symbol subset")
    parser.add_argument("--top-n", type=int, default=None, help="Exploratory fallback size")
    parser.add_argument(
        "--generate-signals",
        action="store_true",
        help="Generate signals in-process rather than reading Layer 1 artifacts.",
    )
    parser.add_argument("--universe-config", default=str(CONFIG_DIR / "universe.yaml"))
    parser.add_argument("--grid-config", default=str(CONFIG_DIR / "strategy_grid.yaml"))
    parser.add_argument("--backtest-config", default=str(CONFIG_DIR / "backtest.yaml"))
    parser.add_argument("--robustness-config", default=str(CONFIG_DIR / "robustness.yaml"))
    parser.add_argument("--regime-config", default=str(CONFIG_DIR / "regime.yaml"))
    # PEAD-specific arguments (ignored by other layers).
    parser.add_argument(
        "--min-surprise", type=float, default=5.0,
        help="[pead] Minimum earnings surprise %% to trigger a trade (default: 5.0).",
    )
    parser.add_argument(
        "--holding-days", type=int, default=20,
        help="[pead] Number of sessions to hold the drift position (default: 20).",
    )
    parser.add_argument(
        "--direction", choices=["both", "long_only", "short_only"], default="both",
        help="[pead] Trade positive surprises, negative surprises, or both (default: both).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    {
        "layer1": run_layer1,
        "layer2": run_layer2,
        "layer3": run_layer3,
        "layer4": run_layer4,
        "pead": run_pead,
        "all": run_all,
    }[args.layer](args)


if __name__ == "__main__":
    main()
