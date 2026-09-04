#!/usr/bin/env python3
"""Poll local trigger orders and fire them when their price is reached.

    python quant_backtester/gtt_watcher.py                 # watch until stopped
    python quant_backtester/gtt_watcher.py --once          # one pass, then exit
    python quant_backtester/gtt_watcher.py --status        # what is armed
    python quant_backtester/gtt_watcher.py --interval 15   # seconds between polls

Brokers without a native GTT (Flattrade, on the PiConnect API) get their
standing triggers from `synthetic_gtt.SyntheticGTT`. Those triggers are only
as good as something polling them — an armed trigger with no watcher is a
stop that will never fire, which is worse than no stop at all because it
looks like protection.

This process is that watcher.

IT WRITES A HEARTBEAT. state/<broker>_gtt_heartbeat.json records the time of
every completed pass. The console and `--status` read it, so "the watcher
died at 10:04" is visible rather than being discovered when a stop fails to
fire. A trigger store with a stale heartbeat is reported as UNWATCHED.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from quant_backtester.src.broker.factory import BrokerFactory  # noqa: E402
from quant_backtester.src.broker.synthetic_gtt import (  # noqa: E402
    SyntheticGTT,
    TriggerStore,
)

logger = logging.getLogger("gtt_watcher")

#: A heartbeat older than this many poll intervals means nobody is watching.
STALE_INTERVALS = 3


def heartbeat_path(broker: str) -> Path:
    return ROOT / "state" / f"{broker}_gtt_heartbeat.json"


def write_heartbeat(broker: str, armed: int, fired: int) -> None:
    path = heartbeat_path(broker)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "broker": broker,
                "last_pass": datetime.now().isoformat(timespec="seconds"),
                "armed": armed,
                "fired_this_pass": fired,
                "pid": __import__("os").getpid(),
            },
            indent=1,
        )
    )


def read_heartbeat(broker: str, interval: float = 30.0) -> dict:
    """Whether anything is currently watching this broker's triggers."""
    path = heartbeat_path(broker)
    if not path.exists():
        return {"watching": False, "reason": "no watcher has ever run"}
    try:
        data = json.loads(path.read_text())
        last = datetime.fromisoformat(data["last_pass"])
    except Exception as exc:  # noqa: BLE001
        return {"watching": False, "reason": f"unreadable heartbeat: {exc}"}

    age = (datetime.now() - last).total_seconds()
    stale = age > interval * STALE_INTERVALS
    return {
        "watching": not stale,
        "last_pass": data["last_pass"],
        "age_seconds": round(age, 1),
        "armed": data.get("armed", 0),
        "reason": f"last pass {age:.0f}s ago" if stale else "",
    }


def build(broker: str | None):
    import yaml

    config = yaml.safe_load((ROOT / "config" / "broker.yaml").read_text())
    factory = BrokerFactory(config, ROOT / "state", ROOT / "data_india")
    name = broker or factory.active_broker
    adapter = factory.build_adapter(name)
    adapter.authenticate()
    store = TriggerStore(ROOT / "state" / f"{name}_triggers.json")
    return name, SyntheticGTT(adapter, store)


def show_status(broker: str | None, interval: float) -> int:
    name, gtt = build(broker)
    armed = gtt.active()
    beat = read_heartbeat(name, interval)

    print(f"broker      : {name}")
    print(f"armed       : {len(armed)} trigger(s)")
    for t in armed:
        print(
            f"   {t.trigger_id}  {t.side:4} {t.quantity:g} {t.symbol:14} "
            f"{t.direction} {t.trigger_price:.2f}   armed {t.armed_at}"
        )

    if beat["watching"]:
        print(f"watcher     : running (last pass {beat['age_seconds']}s ago)")
        return 0

    if armed:
        # The dangerous state: protection that exists on paper only.
        print(f"watcher     : NOT RUNNING — {beat['reason']}")
        print()
        print("  ⚠ These triggers will NOT fire. Start the watcher:")
        print("      python quant_backtester/gtt_watcher.py")
        return 1

    print(f"watcher     : not running ({beat['reason']}); nothing is armed")
    return 0


def watch(broker: str | None, interval: float, once: bool) -> int:
    name, gtt = build(broker)
    stopping = {"now": False}

    def stop(signum, frame):  # noqa: ARG001
        # Say so rather than vanishing: the operator needs to know their
        # stops stopped being watched, and when.
        logger.warning("Signal %s received — the watcher is stopping. "
                       "Armed triggers will NOT fire until it restarts.", signum)
        stopping["now"] = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    logger.info("Watching %s triggers every %.0fs (Ctrl-C to stop)", name, interval)
    while not stopping["now"]:
        try:
            fired = gtt.check()
            armed = len(gtt.active())
            for record in fired:
                if "error" in record:
                    logger.error(
                        "Trigger %s reached %.2f but the order FAILED (%s) — "
                        "still armed, will retry",
                        record["trigger_id"], record["market_price"], record["error"],
                    )
                else:
                    logger.warning(
                        "FIRED %s: %s %g %s at %.2f -> order %s",
                        record["trigger_id"], record["side"], record["quantity"],
                        record["symbol"], record["market_price"], record.get("order_id"),
                    )
            write_heartbeat(name, armed, len(fired))
            if armed == 0 and not fired:
                logger.debug("Nothing armed")
        except Exception as exc:  # noqa: BLE001
            # Never die on one bad pass: a quote outage must not silently end
            # the watch. Log and try again on the next tick.
            logger.error("Poll failed (%s); retrying next interval", exc)

        if once:
            break
        for _ in range(int(interval * 10)):
            if stopping["now"]:
                break
            time.sleep(0.1)

    logger.info("Watcher stopped.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--broker", help="broker to watch (default: the active one)")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between polls")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--status", action="store_true", help="report what is armed")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.status:
        return show_status(args.broker, args.interval)
    return watch(args.broker, args.interval, args.once)


if __name__ == "__main__":
    raise SystemExit(main())
