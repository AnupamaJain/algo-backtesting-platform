#!/usr/bin/env python
"""Live trading runner — signals → sizing → risk → orders.

Loads the validated strategy set from Layer 3, fetches the latest price bars,
runs every deployed configuration through the TradingEngine, and places any
resulting orders through the configured broker.

Usage:
    python run_live.py                      # one pass, dry-run (safe default)
    python run_live.py --live               # one pass, real orders
    python run_live.py --dry-run --loop     # continuous dry-run loop
    python run_live.py --live --loop        # continuous live loop
    python run_live.py --top-n 10           # use top-N from all_configurations if no ultra-robust set

Safety:
    --live is required to send real orders. Without it the engine evaluates
    every signal and sizing decision but places nothing. This is not a config
    flag — it is a CLI argument that must be typed deliberately each run.

Loop mode:
    Fetches fresh bars and runs one pass every --interval-minutes (default 15).
    The loop never exits on a single bad pass — one failed data fetch or order
    rejection is logged and the loop continues. Use systemd, supervisord or a
    cron job to restart on crash.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quant_backtester.src.broker.factory import BrokerFactory
from quant_backtester.src.config import load_backtest_config, load_robustness_config, load_universe_config
from quant_backtester.src.data_loader import HistoricalDataManager
from quant_backtester.src.live.engine import (
    PositionSizer,
    RiskLimits,
    RiskManager,
    SizingPolicy,
    TradingEngine,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("run_live")

CONFIG_DIR = Path(__file__).parent / "config"
STATE_DIR = Path(__file__).parent / "state"
DATA_DIR = Path(__file__).parent / "data"


# ==========================================================================
# Helpers
# ==========================================================================


def read_artifact(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def load_deployed(backtest_config, robustness_config, top_n: int | None) -> pd.DataFrame:
    """Ultra-robust set → all_configurations top-N → empty (abort)."""
    ultra = robustness_config.robustness_dir / "ultra_robust_strategies.csv"
    frame = read_artifact(ultra)
    if not frame.empty:
        logger.info("Deploying %s ultra-robust strategies from Layer 3", len(frame))
        return frame

    if top_n:
        all_configs = backtest_config.layer2_dir / "all_configurations.csv"
        frame = read_artifact(all_configs)
        if not frame.empty:
            frame = frame.nlargest(top_n, "oos_sharpe")
            logger.warning(
                "No ultra-robust set; using top %s by OOS Sharpe — EXPLORATORY ONLY",
                len(frame),
            )
            return frame

    logger.error(
        "No strategies to deploy. Run layers 1-3 first, or pass --top-n N as a fallback."
    )
    return pd.DataFrame()


# ==========================================================================
# One trading pass
# ==========================================================================


def run_once(
    engine: TradingEngine,
    deployed: pd.DataFrame,
    data_manager: HistoricalDataManager,
    dry_run: bool,
) -> bool:
    """Fetch latest bars, generate signals, place orders. Returns True on success."""
    symbols = sorted(set(deployed["symbol"]))
    logger.info("Fetching latest bars for %s symbols", len(symbols))

    price_data = data_manager.get_universe_history(symbols)
    if not price_data:
        logger.error("No price data returned — skipping this pass")
        return False

    trading_run = engine.run(deployed, price_data, dry_run=dry_run)
    summary = trading_run.summary()

    logger.info(
        "Pass complete: %s signal(s), %s approved, %s blocked, %s placed, %s error(s)",
        summary["signals"],
        summary["approved"],
        summary["blocked"],
        summary["placed"],
        summary["errors"],
    )

    if trading_run.errors:
        for err in trading_run.errors:
            logger.warning("Order error: %s", err)

    if trading_run.conflicts:
        for symbol, detail in trading_run.conflicts.items():
            logger.warning("Signal conflict on %s: %s", symbol, detail)

    if dry_run and summary["approved"] > 0:
        logger.info(
            "DRY-RUN: %s order(s) would have been placed. Pass --live to send real orders.",
            summary["approved"],
        )

    return True


# ==========================================================================
# CLI
# ==========================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Send real orders. Without this flag the engine is always dry-run.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously, fetching fresh bars every --interval-minutes.",
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=15,
        metavar="N",
        help="Minutes between passes in loop mode (default: 15).",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=None,
        metavar="N",
        help="Fall back to the top-N OOS-Sharpe configurations if no ultra-robust set exists.",
    )
    parser.add_argument(
        "--broker",
        default=None,
        help="Override the active broker from broker.yaml (e.g. paper, paper_india).",
    )
    parser.add_argument("--universe-config", default=str(CONFIG_DIR / "universe.yaml"))
    parser.add_argument("--backtest-config", default=str(CONFIG_DIR / "backtest.yaml"))
    parser.add_argument("--robustness-config", default=str(CONFIG_DIR / "robustness.yaml"))
    parser.add_argument("--broker-config", default=str(CONFIG_DIR / "broker.yaml"))

    # Risk / sizing overrides — let operators tighten limits without editing YAML.
    parser.add_argument("--max-positions", type=int, default=10)
    parser.add_argument("--risk-per-trade", type=float, default=0.01,
                        help="Fraction of equity risked per position (default: 0.01 = 1%%).")
    parser.add_argument("--max-gross-exposure", type=float, default=2.0)
    parser.add_argument("--no-short", action="store_true", help="Disable short selling.")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = not args.live

    if args.live:
        logger.warning(
            "LIVE MODE: real orders will be placed. "
            "Ensure the broker config is correct and the market is open."
        )
    else:
        logger.info("DRY-RUN mode: signals and sizes will be computed but no orders placed.")

    # -- configs -----------------------------------------------------------
    universe_config = load_universe_config(args.universe_config)
    backtest_config = load_backtest_config(args.backtest_config)
    robustness_config = load_robustness_config(args.robustness_config)

    # -- deployed strategies -----------------------------------------------
    deployed = load_deployed(backtest_config, robustness_config, args.top_n)
    if deployed.empty:
        sys.exit(1)

    # -- broker / service --------------------------------------------------
    import yaml
    with open(args.broker_config) as fh:
        broker_cfg = yaml.safe_load(fh)
    if args.broker:
        broker_cfg = {**broker_cfg, "active": args.broker}

    factory = BrokerFactory(broker_cfg, STATE_DIR, DATA_DIR)
    service = factory.build_service()

    # -- engine ------------------------------------------------------------
    sizing_policy = SizingPolicy(
        risk_per_trade=args.risk_per_trade,
        max_position_pct=0.20,
        allow_fractional=False,
    )
    risk_limits = RiskLimits(
        max_positions=args.max_positions,
        max_gross_exposure=args.max_gross_exposure,
        allow_short=not args.no_short,
    )
    engine = TradingEngine(
        service=service,
        sizer=PositionSizer(sizing_policy),
        risk=RiskManager(risk_limits),
    )

    logger.info("Engine: %s", engine.describe())

    # -- data --------------------------------------------------------------
    data_manager = HistoricalDataManager(universe_config.data)

    # -- run ---------------------------------------------------------------
    if not args.loop:
        success = run_once(engine, deployed, data_manager, dry_run)
        sys.exit(0 if success else 1)

    interval = args.interval_minutes * 60
    logger.info("Loop mode: running every %s minutes. Ctrl-C to stop.", args.interval_minutes)
    while True:
        try:
            run_once(engine, deployed, data_manager, dry_run)
        except KeyboardInterrupt:
            logger.info("Interrupted — stopping.")
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("Unhandled error in pass (will retry): %s", exc, exc_info=True)
        logger.info("Sleeping %s minutes until next pass.", args.interval_minutes)
        time.sleep(interval)


if __name__ == "__main__":
    main()
