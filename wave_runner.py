#!/usr/bin/env python3
"""One process, many symbols: the Wave Extractor runner.

The dashboard used to spawn one ``ticker_single_scraper_new.py`` per
symbol. Eight symbols meant eight interpreters, eight broker sessions and
eight attempts on Flattrade's single permitted websocket -- which is why
the feed refused connections and why a laptop with a handful of contracts
running sounded like it was taking off.

The reason it was built that way is that ``common_lib`` keeps the
instrument it is working on in module-level globals: a second symbol in the
same interpreter would have traded with the first one's gap, quantity and
token. ``symbol_context.SymbolContext`` removes that constraint by swapping
the per-symbol slice of those globals around each instrument's turn, which
is safe here because the loop is sequential -- one symbol is worked at a
time, never two at once.

    python3 wave_runner.py            # reads state/wave_runner.json

The control file is a list of instruments, and it is re-read on every
cycle, so the dashboard adds and removes symbols by writing to it rather
than by spawning and killing processes:

    {"symbols": [
       {"symbol": "NIFTY26O0623450CE", "buy_gap": 12.6, "sell_gap": 12.6,
        "quantity": "300", "product_type": "NRML", "gtt": false}
    ]}

Per-symbol status files are still written exactly as before, so the
Running Instances table, the stop buttons and the log links keep working
against a runner instead of against one process per row.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import common_lib
from common_lib import (
    check_changes_in_restrictions,
    check_gtt_orders,
    check_is_any_order_active,
    check_orders,
    get_instrument_details,
    get_quote_with_retry,
    initilise_basic,
    initilise_symbol,
    is_any_gtt_pending,
    is_market_open,
    place_duo_order,
    reread_option_defaults,
    set_exchange,
    set_globalDeltaCalculationDays,
    set_scraper_last_price,
    set_tag,
    set_typeOfProduct,
    write_status_to_file,
)
from symbol_context import SymbolContext, active_symbol

ROOT = Path(__file__).resolve().parent
CONTROL_FILE = ROOT / "state" / "wave_runner.json"
CYCLE_SECONDS = 180

#: Fallback only. wave_extractor_config.json takes precedence at runtime;
#: place_duo_order() reads it first.
MULTIPLIER_SCALE = {
    "0": [1, 1], "1": [1.3, 1], "2": [1.7, 1], "3": [2.5, 1], "4": [3, 1],
    "5": [10, 1], "6": [10, 1], "7": [10, 1], "8": [15, 1], "9": [15, 1], "10": [15, 1],
    "-1": [1, 1.3], "-2": [1, 1.7], "-3": [1, 2.5], "-4": [1, 3],
    "-5": [1, 10], "-6": [1, 10], "-7": [1, 10], "-8": [1, 15], "-9": [1, 15], "-10": [1, 15],
}

logger = logging.getLogger("wave_runner")
_stopping = False


def _exchange_for(symbol: str) -> str:
    """SENSEX and BANKEX options list on BFO; everything else on NFO."""
    return "BFO" if symbol.upper().startswith(("SENSEX", "BANKEX")) else "NFO"


def read_control(path: Path) -> dict[str, dict]:
    """The instruments the dashboard wants running, keyed by symbol.

    A missing or malformed file means "nothing to run" rather than a crash:
    the runner is long-lived and must survive the dashboard writing the
    file while it is being read.
    """
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("control file unreadable (%s); keeping the current set", exc)
        return {}

    wanted: dict[str, dict] = {}
    for entry in raw.get("symbols", []):
        symbol = str(entry.get("symbol", "")).strip().upper()
        if symbol:
            wanted[symbol] = entry
    return wanted


class Instrument:
    """One symbol's context plus whether it has been prepared yet."""

    def __init__(self, spec: dict) -> None:
        self.symbol: str = spec["symbol"].strip().upper()
        self.spec = spec
        self.exchange = _exchange_for(self.symbol)
        self.product_type = spec.get("product_type") or "NRML"
        self.context = SymbolContext(
            self.symbol,
            # -1 is initilise_symbol's "not fetched yet" sentinel.
            initial_positions=-1,
            multiplier_scale=MULTIPLIER_SCALE,
        )
        self.prepared = False
        self.failures = 0

    def prepare(self) -> None:
        """Everything the old scraper did before its loop, for this symbol."""
        set_exchange(self.exchange)
        initilise_symbol(
            self.symbol,
            str(self.spec.get("quantity", "0")),
            float(self.spec.get("buy_gap", 0)),
            float(self.spec.get("sell_gap", 0)),
            MULTIPLIER_SCALE,
            self.exchange,
        )
        set_tag("Scraper")

        details = get_instrument_details(self.symbol)
        set_globalDeltaCalculationDays(details["days_to_expiry"])

        key = f"{self.exchange}:{self.symbol}"
        quote = get_quote_with_retry(key)
        set_scraper_last_price(float(quote[key]["last_price"]))

        set_typeOfProduct(self.product_type)
        place_duo_order(self.symbol, self.product_type, True)
        self.prepared = True
        logger.info("%s prepared and working", self.symbol)

    def cycle(self) -> None:
        """One pass of the old scraper's while-loop, for this symbol."""
        check_changes_in_restrictions(self.symbol)
        write_status_to_file()
        check_orders()

        if not check_is_any_order_active():
            # A GTT may have fired while the socket was down; poll before
            # placing anything, so a live trigger is not duplicated.
            check_gtt_orders()
            if not is_any_gtt_pending():
                place_duo_order(self.symbol, self.product_type, True)


def _handle_sigterm(signum: int, frame) -> None:  # noqa: ARG001
    global _stopping
    logger.info("SIGTERM received; finishing the current cycle and exiting")
    _stopping = True


def run(control_file: Path, cycle_seconds: int) -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    # The same placeholder the dashboard hands its strategy scripts: the
    # adapter authenticates from config/broker.yaml, so there is no Kite
    # request token to pass any more. See flask_app.get_token_for_script.
    initilise_basic("access:adapter")
    logger.info("broker connection established; one session for every symbol")

    live: dict[str, Instrument] = {}

    while not _stopping:
        wanted = read_control(control_file)

        for symbol in list(live):
            if symbol not in wanted:
                logger.info("%s removed from the control file; dropping it", symbol)
                live.pop(symbol)

        for symbol, spec in wanted.items():
            if symbol not in live:
                logger.info("%s added", symbol)
                live[symbol] = Instrument(spec)

        if not is_market_open():
            logger.warning("market is closed; exiting so no order or GTT is left overnight")
            return 0

        reread_option_defaults()

        for instrument in list(live.values()):
            if _stopping:
                break
            try:
                with active_symbol(common_lib, instrument.context):
                    if not instrument.prepared:
                        instrument.prepare()
                    else:
                        instrument.cycle()
                instrument.failures = 0
            except Exception:
                # One symbol failing must not take the others down -- that
                # isolation was the one genuine advantage of a process each.
                instrument.failures += 1
                logger.exception(
                    "%s failed its turn (%d in a row)", instrument.symbol, instrument.failures
                )
                if instrument.failures >= 3:
                    logger.error("%s has failed three turns; dropping it", instrument.symbol)
                    live.pop(instrument.symbol, None)

        if _stopping:
            break
        time.sleep(cycle_seconds)

    logger.info("runner stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-file", type=Path, default=CONTROL_FILE)
    parser.add_argument("--cycle-seconds", type=int, default=CYCLE_SECONDS)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args.control_file.parent.mkdir(parents=True, exist_ok=True)
    return run(args.control_file, args.cycle_seconds)


if __name__ == "__main__":
    sys.exit(main())
