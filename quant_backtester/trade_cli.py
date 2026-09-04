#!/usr/bin/env python
"""Run the live trading engine: signals -> sizing -> risk -> orders.

    python quant_backtester/trade_cli.py --dry-run          # evaluate only
    python quant_backtester/trade_cli.py                    # place paper orders
    python quant_backtester/trade_cli.py --broker paper_india \
        --universe-config config/universe_india.yaml

Deploys whatever cleared the research gauntlet (Layer 3's ultra-robust set),
falling back to the top-N configurations with --top-n.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quant_backtester.src.broker import BrokerFactory  # noqa: E402
from quant_backtester.src.config import load_universe_config  # noqa: E402
from quant_backtester.src.data_loader import HistoricalDataManager  # noqa: E402
from quant_backtester.src.live.engine import (  # noqa: E402
    PositionSizer,
    RiskLimits,
    RiskManager,
    SignalGenerator,
    SizingPolicy,
    TradingEngine,
)

ROOT = Path(__file__).resolve().parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("trade")


def _poll_local_triggers(adapter, *, dry_run: bool) -> None:
    """Fire any local trigger whose price has been reached, and warn if
    triggers are armed with nothing watching them.

    A trigger nobody polls is worse than no trigger: it looks like
    protection. This makes the gap loud rather than silent.
    """
    if getattr(adapter, "capabilities", None) and adapter.capabilities.supports_gtt:
        return  # the exchange is doing the watching

    try:
        from quant_backtester.gtt_watcher import read_heartbeat
        from quant_backtester.src.broker.synthetic_gtt import SyntheticGTT, TriggerStore

        from quant_backtester.src.broker.adapter import account_key

        name = account_key(adapter)
        store = TriggerStore(ROOT / "state" / f"{name}_triggers.json")
        gtt = SyntheticGTT(adapter, store)
        armed = gtt.active()
        if not armed:
            return

        if dry_run:
            print(f"\n=== Local triggers ===\n  {len(armed)} armed (dry run: not polled)")
            return

        fired = gtt.check()
        beat = read_heartbeat(name)
        print(f"\n=== Local triggers ===")
        print(f"  {len(gtt.active())} armed, {len(fired)} fired this pass")
        for record in fired:
            state = record.get("error") or f"order {record.get('order_id')}"
            print(f"    {record['symbol']:14} @ {record['market_price']:.2f} -> {state}")
        if not beat["watching"]:
            print("  ⚠ No watcher process running — these fire only when this "
                  "command runs.\n    Start one: python quant_backtester/gtt_watcher.py")
    except Exception as exc:  # noqa: BLE001 - never let this break a trading run
        logger.warning("Could not poll local triggers: %s", exc)


def load_deployed(
    results_dir: Path, top_n: int | None, per_strategy: int | None = None
) -> pd.DataFrame:
    """Ultra-robust strategies if any, else an explicitly-exploratory fallback.

    `per_strategy` deploys the best N configurations of EVERY strategy rather
    than the global top-N. Top-N by Sharpe is usually dominated by one or two
    families, so it exercises a fraction of the library; per-strategy gives
    every family live representation, which is what "run all the strategies"
    means in practice.
    """
    ultra = results_dir / "layer3" / "ultra_robust_strategies.csv"
    if ultra.exists():
        try:
            frame = pd.read_csv(ultra)
        except pd.errors.EmptyDataError:
            frame = pd.DataFrame()
        if not frame.empty:
            logger.info("Deploying %s ultra-robust strategies", len(frame))
            return frame

    path = results_dir / "layer2" / "all_configurations.csv"

    if per_strategy:
        if path.exists():
            frame = pd.read_csv(path)
            best = (
                frame.sort_values("oos_sharpe", ascending=False)
                .groupby("strategy", as_index=False)
                .head(per_strategy)
            )
            logger.warning(
                "No ultra-robust set; deploying the best %s configuration(s) of "
                "each of %s strategies (%s total). These did NOT clear the full "
                "gauntlet — exploratory only.",
                per_strategy, best["strategy"].nunique(), len(best),
            )
            return best
        return pd.DataFrame()

    if top_n:
        if path.exists():
            frame = pd.read_csv(path).nlargest(top_n, "oos_sharpe")
            logger.warning(
                "No ultra-robust set; deploying the top %s by OOS Sharpe. "
                "These did NOT clear the full gauntlet.", len(frame),
            )
            return frame
    return pd.DataFrame()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe-config", default=str(ROOT / "config" / "universe.yaml"))
    parser.add_argument("--broker-config", default=str(ROOT / "config" / "broker.yaml"))
    parser.add_argument("--broker", help="override the active broker (e.g. paper_india)")
    parser.add_argument("--top-n", type=int, default=None)
    parser.add_argument(
        "--per-strategy", type=int, default=None,
        help="deploy the best N configs of EVERY strategy (all families live)",
    )
    parser.add_argument("--dry-run", action="store_true", help="evaluate without placing orders")
    parser.add_argument("--risk-per-trade", type=float, default=0.01)
    parser.add_argument("--max-positions", type=int, default=10)
    parser.add_argument("--max-gross", type=float, default=2.0)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    universe = load_universe_config(args.universe_config)
    results_dir = ROOT / universe.data.results_dir

    deployed = load_deployed(results_dir, args.top_n, args.per_strategy)
    if deployed.empty:
        logger.error("Nothing to deploy. Run layers 1-3, or pass --top-n.")
        return 1

    data_manager = HistoricalDataManager(universe.data)
    symbols = sorted(set(deployed["symbol"]))
    price_data = data_manager.get_universe_history(symbols)
    if not price_data:
        logger.error("No price data for the deployed symbols.")
        return 1

    with open(args.broker_config, "r", encoding="utf-8") as fh:
        broker_config = yaml.safe_load(fh)
    if args.broker:
        broker_config["active"] = args.broker

    factory = BrokerFactory(broker_config, ROOT / "state", ROOT / universe.data.cache_dir)
    engine = TradingEngine(
        service=factory.build_service(),
        sizer=PositionSizer(SizingPolicy(risk_per_trade=args.risk_per_trade)),
        risk=RiskManager(
            RiskLimits(max_positions=args.max_positions, max_gross_exposure=args.max_gross)
        ),
        generator=SignalGenerator(),
    )

    run = engine.run(deployed, price_data, dry_run=args.dry_run)

    if args.json:
        print(json.dumps({
            "summary": run.summary(),
            "signals": [
                {"symbol": s.symbol, "strategy": s.strategy, "direction": s.direction,
                 "price": s.price, "as_of": s.as_of.isoformat()}
                for s in run.signals
            ],
            "decisions": [d.to_dict() for d in run.decisions],
            "placed": [o.to_dict() for o in run.placed],
            "errors": run.errors,
        }, default=str))
        return 0

    # Local triggers only fire while something polls them. Every engine run
    # is a poll, so a scheduled trade_cli keeps stops honest even when the
    # standalone watcher is not running.
    _poll_local_triggers(factory.build_adapter(), dry_run=args.dry_run)

    summary = run.summary()
    print(f"\n=== Trading run ({'DRY RUN' if args.dry_run else 'LIVE'}) ===")
    print(f"  equity {summary['equity']:,.2f}   signals {summary['signals']}   "
          f"actionable {summary['actionable']}   approved {summary['approved']}   "
          f"blocked {summary['blocked']}   placed {summary['placed']}")

    if run.signals:
        print("\n=== Signals ===")
        for s in run.signals[:25]:
            print(f"  {s.symbol:14} {s.strategy:22} {s.direction:5} @ {s.price:>10,.2f}  ({s.as_of.date()})")

    if run.decisions:
        print("\n=== Sizing & risk ===")
        for d in run.decisions[:25]:
            mark = "OK " if d.approved else "BLOCK"
            o = d.order
            print(f"  [{mark}] {o.side.value:4} {o.quantity:>8,.0f} {o.symbol:14} "
                  f"risk {o.risk_amount:>8,.0f}  stop {o.stop_distance:>8,.2f}  {d.reason}")

    for err in run.errors:
        print(f"  ERROR: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
