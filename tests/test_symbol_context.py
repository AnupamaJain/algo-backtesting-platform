"""One process, many symbols -- and no state leaking between them.

common_lib keeps the instrument it is working on in module-level globals,
which is why the dashboard ran one OS process per symbol. SymbolContext
swaps that slice in and out so a single process can work several symbols in
turn. These tests are the correctness argument for that swap: if the
classification is wrong, a strategy places an order for one contract using
another contract's gap, quantity or token.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from symbol_context import PER_SYMBOL, PROCESS_WIDE, SymbolContext, active_symbol

ROOT = Path(__file__).resolve().parent.parent


class _FakeModule:
    """Stands in for common_lib without importing a broker."""


def test_the_classification_covers_every_global_common_lib_assigns():
    """A new global must be classified, not silently defaulted.

    Anything assigned through a `global` statement is state that survives a
    call. If it describes the instrument and is not in PER_SYMBOL, it
    leaks between symbols; if it is infrastructure and lands in PER_SYMBOL,
    swapping it breaks reconnection or loses fills.
    """
    source = (ROOT / "common_lib.py").read_text()
    declared: set[str] = set()
    for match in re.finditer(r"^\s+global ([A-Za-z_][\w, ]*)", source, re.M):
        declared.update(name.strip() for name in match.group(1).split(","))

    classified = set(PER_SYMBOL) | set(PROCESS_WIDE)
    unclassified = declared - classified
    assert not unclassified, (
        "common_lib assigns globals that symbol_context has not classified: "
        f"{sorted(unclassified)}. Decide whether each is per-symbol or "
        "process-wide and add it to the right tuple."
    )


def test_nothing_is_both_per_symbol_and_process_wide():
    assert not (set(PER_SYMBOL) & set(PROCESS_WIDE))


def test_two_symbols_do_not_see_each_others_state():
    """The bug this whole mechanism exists to prevent."""
    module = _FakeModule()

    first = SymbolContext("NIFTY26O0623450CE", buy_gap=12.6, quantity="75",
                          instrument_token=40757)
    second = SymbolContext("NIFTY26O0623800PE", buy_gap=4.2, quantity="150",
                           instrument_token=40782)

    with active_symbol(module, first):
        assert module.symbol == "NIFTY26O0623450CE"
        assert module.buy_gap == 12.6
        assert module.instrument_token == 40757

    with active_symbol(module, second):
        assert module.symbol == "NIFTY26O0623800PE"
        assert module.buy_gap == 4.2, "the first symbol's gap leaked"
        assert module.quantity == "150"
        assert module.instrument_token == 40782, "would have traded the wrong contract"


def test_a_turn_keeps_what_it_changed():
    """State advanced during a turn belongs to that symbol next time."""
    module = _FakeModule()
    ctx = SymbolContext("NIFTY26O0623450CE", duo_old_buy_price=100.0)

    with active_symbol(module, ctx):
        module.duo_old_buy_price = 118.5

    assert ctx.state["duo_old_buy_price"] == 118.5

    with active_symbol(module, ctx):
        assert module.duo_old_buy_price == 118.5


def test_state_is_restored_even_when_the_turn_raises():
    """A symbol that throws must not leave its contract installed.

    The next symbol in the loop would otherwise place its order against
    whatever the failed one was holding.
    """
    module = _FakeModule()
    module.symbol = "SENTINEL"
    module.instrument_token = 1

    ctx = SymbolContext("NIFTY26O0623450CE", instrument_token=40757)

    with pytest.raises(ValueError):
        with active_symbol(module, ctx):
            assert module.instrument_token == 40757
            raise ValueError("strategy blew up mid-turn")

    assert module.symbol == "SENTINEL"
    assert module.instrument_token == 1


def test_globals_absent_before_the_turn_are_absent_after():
    module = _FakeModule()
    assert not hasattr(module, "buy_gap")

    with active_symbol(module, SymbolContext("X", buy_gap=1.0)):
        assert module.buy_gap == 1.0

    assert not hasattr(module, "buy_gap"), "left a global behind"


def test_a_context_cannot_be_activated_twice():
    module = _FakeModule()
    ctx = SymbolContext("X")
    with active_symbol(module, ctx):
        with pytest.raises(RuntimeError):
            ctx.activate(module)
