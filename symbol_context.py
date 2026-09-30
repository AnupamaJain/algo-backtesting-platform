"""Per-symbol state, so one process can run many symbols.

Why this exists
---------------
``common_lib`` keeps the state of the instrument it is working on in
module-level globals -- ``symbol``, ``buy_gap``, ``segment``,
``instrument_token``, the duo prices, the restriction flags, the order
bookkeeping. Seventy-two of them are assigned through a ``global``
statement somewhere in the file.

That is the whole reason the dashboard ran one operating-system process per
symbol: the globals ARE the per-symbol state, so a second symbol in the
same interpreter would silently trade with the first one's gaps, quantity
and token. It is also why eight running instances fought over Flattrade's
single permitted websocket.

Extracting all of that into objects would mean rewriting most of a
5,700-line module that places real orders. This does the same job with a
far smaller blast radius: the per-symbol globals are swapped in and out
around each symbol's turn. The strategy loop is already sequential -- it
wakes every 180 seconds and works through its instruments -- so no two
symbols are ever mid-flight at once, which is exactly the condition that
makes swapping safe.

What is per-symbol and what is not
----------------------------------
The distinction is the entire correctness argument, so it is written down
rather than inferred:

* **Per-symbol** -- anything describing the instrument being traded or the
  progress of its own orders. Swapping these is the point.
* **Process-wide** -- the broker client, the websocket, the access token,
  the instrument cache, the quote caches, exchange constants, and the
  order maps keyed by broker order id. Swapping these would break
  reconnection, re-authenticate needlessly, or lose fills that arrive for
  one symbol while another has the floor.

``orders`` and ``order_ids_completed`` are deliberately shared: they are
keyed by broker order id, and a fill can arrive on the websocket for any
symbol at any moment, including while a different symbol is being worked.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Globals that describe one instrument and its own orders. Everything here
#: is saved and restored around a symbol's turn.
PER_SYMBOL: tuple[str, ...] = (
    # identity
    "symbol", "segment", "exchange", "instrument_token", "symbol_type",
    # sizing and spacing
    "buy_gap", "sell_gap", "initial_sell_gap",
    "buy_gap_percentage", "sell_gap_percentage",
    "quantity", "buy_quantity", "sell_quantity",
    "multiplier_scale", "_last_multiplier_info",
    # product
    "product_type", "typeOfProduct", "tag",
    # live working state
    "scraper_last_price", "old_quote_price",
    "duo_old_buy_price", "duo_old_sell_price",
    "already_executing_order", "already_updating_order",
    "global_restrict_buy", "global_restrict_sell",
    "initial_positions", "current_positions", "cached_positions",
    "delta_calculation_days", "todays_volatility",
    "_pending_trigger_index", "_pending_trigger_spot",
    # this symbol's own order bookkeeping (NOT the id-keyed shared maps)
    "order_numbers_storage", "script_order_ids", "order_gtt_regular",
    "pending_gtt_fallbacks",
    "start_time",
)

#: Never swapped. Listed explicitly so that adding a global to common_lib
#: and forgetting about it shows up as a decision rather than a silent
#: default. test_symbol_context.py asserts this stays exhaustive.
PROCESS_WIDE: tuple[str, ...] = (
    "kite", "kws", "access_token", "request_token",
    "all_instruments", "token_symbol_map",
    "orders", "order_ids_completed",
    "last_tick_time", "tick",
    "_subscribed_tokens", "_stored_ticker_callbacks",
    "_ws_last_connected_at", "_ws_last_noreconnect_at",
    "_index_quote_cache", "_index_quote_total_calls", "_memcache_client",
    "_we_config_cache", "_flattrade_session_for_ticker",
    "nifty_lot_size", "sensex_lot_size", "bank_nifty_lot_size",
    "nifty_strike_gap", "sensex_strike_gap", "interest_rate",
    "max_nifty_delta", "min_nifty_delta",
    "max_bank_nifty_delta", "min_bank_nifty_delta",
    "delta_limits_config", "gift_nifty_symbol", "exceptNFBNF",
    "last_positions_fetch_time", "on_order_update_solo_callback",
)

_MISSING = object()


class SymbolContext:
    """One symbol's slice of common_lib's globals.

    Created once per instrument. ``activate()`` installs this symbol's
    state into the module and captures whatever was there before, so the
    previous occupant is not lost; ``deactivate()`` puts that back and
    keeps whatever the turn changed.
    """

    def __init__(self, symbol: str, **initial: Any) -> None:
        self.symbol = symbol
        self.state: dict[str, Any] = {"symbol": symbol, **initial}
        self._saved: dict[str, Any] | None = None

    # -- the swap ---------------------------------------------------------

    def activate(self, module) -> None:
        """Install this symbol's state, remembering what it displaced."""
        if self._saved is not None:
            raise RuntimeError(f"{self.symbol} is already active")

        self._saved = {}
        for name in PER_SYMBOL:
            self._saved[name] = getattr(module, name, _MISSING)
            if name in self.state:
                setattr(module, name, self.state[name])

    def deactivate(self, module) -> None:
        """Take this symbol's state back out and restore what was there."""
        if self._saved is None:
            raise RuntimeError(f"{self.symbol} is not active")

        for name in PER_SYMBOL:
            current = getattr(module, name, _MISSING)
            if current is not _MISSING:
                self.state[name] = current

            previous = self._saved[name]
            if previous is _MISSING:
                if hasattr(module, name):
                    delattr(module, name)
            else:
                setattr(module, name, previous)

        self._saved = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SymbolContext {self.symbol} active={self._saved is not None}>"


class active_symbol:  # noqa: N801 - reads as a statement at the call site
    """Context manager: run a block with one symbol's state installed.

        with active_symbol(common_lib, ctx):
            place_duo_order(ctx.symbol, ...)

    The state is restored even if the block raises, so one symbol failing
    cannot leave its gaps and token installed for the next one -- which in
    a trading loop would place the next order against the wrong contract.
    """

    def __init__(self, module, context: SymbolContext) -> None:
        self._module = module
        self._context = context

    def __enter__(self) -> SymbolContext:
        self._context.activate(self._module)
        return self._context

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._context.deactivate(self._module)
        except Exception:  # pragma: no cover - restoration must not mask
            logger.exception("failed to restore state after %s", self._context.symbol)
        return None
