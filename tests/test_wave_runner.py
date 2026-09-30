"""The multi-symbol runner's own logic, without a broker.

The runner replaces one OS process per symbol with one process working
several symbols in turn. What has to hold: symbols are added and removed by
editing a file rather than by spawning and killing processes, a malformed
file never takes the runner down, and one symbol failing does not stop the
others -- that isolation was the single genuine advantage of a process
each, and it has to survive the consolidation.
"""

from __future__ import annotations

import json

import pytest

import wave_runner
from wave_runner import Instrument, _exchange_for, read_control


class TestExchange:
    def test_sensex_and_bankex_options_list_on_bfo(self):
        assert _exchange_for("SENSEX2620583200PE") == "BFO"
        assert _exchange_for("BANKEX26O0659000CE") == "BFO"

    def test_everything_else_is_nfo(self):
        assert _exchange_for("NIFTY26O0623450CE") == "NFO"
        assert _exchange_for("RELIANCE26OCT1400CE") == "NFO"


class TestControlFile:
    def test_symbols_are_read_and_keyed(self, tmp_path):
        path = tmp_path / "wave_runner.json"
        path.write_text(json.dumps({"symbols": [
            {"symbol": "NIFTY26O0623450CE", "buy_gap": 12.6},
            {"symbol": "nifty26o0623800pe", "buy_gap": 4.2},
        ]}))
        wanted = read_control(path)
        assert set(wanted) == {"NIFTY26O0623450CE", "NIFTY26O0623800PE"}, (
            "symbols should be normalised to upper case"
        )

    def test_a_missing_file_means_nothing_to_run(self, tmp_path):
        assert read_control(tmp_path / "absent.json") == {}

    def test_a_half_written_file_does_not_take_the_runner_down(self, tmp_path):
        """The dashboard writes this file while the runner reads it."""
        path = tmp_path / "wave_runner.json"
        path.write_text('{"symbols": [{"symbol": "NIF')
        assert read_control(path) == {}

    def test_entries_without_a_symbol_are_ignored(self, tmp_path):
        path = tmp_path / "wave_runner.json"
        path.write_text(json.dumps({"symbols": [{"buy_gap": 1}, {"symbol": "  "}]}))
        assert read_control(path) == {}


class TestInstrument:
    def test_a_fresh_instrument_carries_the_not_fetched_sentinel(self):
        """initilise_symbol only fetches the opening position when
        initial_positions is -1; without it the first cycle would trade
        against an empty book."""
        inst = Instrument({"symbol": "NIFTY26O0623450CE"})
        assert inst.context.state["initial_positions"] == -1

    def test_product_type_defaults_to_nrml(self):
        assert Instrument({"symbol": "X26O0623450CE"}).product_type == "NRML"
        assert Instrument({"symbol": "X26O0623450CE", "product_type": "MIS"}).product_type == "MIS"

    def test_each_instrument_owns_its_state(self):
        a = Instrument({"symbol": "NIFTY26O0623450CE", "buy_gap": 12.6})
        b = Instrument({"symbol": "NIFTY26O0623800PE", "buy_gap": 4.2})
        assert a.context is not b.context
        assert a.context.state["symbol"] != b.context.state["symbol"]


def test_one_symbol_failing_does_not_stop_the_others(monkeypatch, tmp_path):
    """The isolation a process each used to provide, kept.

    A symbol that raises is retried, and dropped only after three
    consecutive failures -- so a bad contract cannot silently stall the
    book, and a transient broker error does not discard a good one.
    """
    path = tmp_path / "wave_runner.json"
    path.write_text(json.dumps({"symbols": [
        {"symbol": "GOOD26O0623450CE"},
        {"symbol": "BAD26O0623800PE"},
    ]}))

    worked: list[str] = []

    def fake_prepare(self):
        if self.symbol.startswith("BAD"):
            raise RuntimeError("this contract is not tradable")
        worked.append(self.symbol)
        self.prepared = True

    monkeypatch.setattr(Instrument, "prepare", fake_prepare)
    monkeypatch.setattr(Instrument, "cycle", lambda self: worked.append(self.symbol))
    monkeypatch.setattr(wave_runner, "initilise_basic", lambda *a, **k: None)
    monkeypatch.setattr(wave_runner, "reread_option_defaults", lambda: None)
    monkeypatch.setattr(wave_runner, "is_market_open", lambda: True)

    cycles = {"n": 0}

    def fake_sleep(_seconds):
        cycles["n"] += 1
        if cycles["n"] >= 4:
            wave_runner._stopping = True

    monkeypatch.setattr(wave_runner.time, "sleep", fake_sleep)
    monkeypatch.setattr(wave_runner, "_stopping", False)

    try:
        assert wave_runner.run(path, cycle_seconds=0) == 0
    finally:
        wave_runner._stopping = False

    assert worked, "the good symbol never ran"
    assert all(s.startswith("GOOD") for s in worked)
    assert len(worked) >= 3, "the good symbol should keep working every cycle"


def test_the_runner_exits_when_the_market_closes(monkeypatch, tmp_path):
    """No order or GTT may be left running overnight."""
    path = tmp_path / "wave_runner.json"
    path.write_text(json.dumps({"symbols": [{"symbol": "NIFTY26O0623450CE"}]}))

    monkeypatch.setattr(wave_runner, "initilise_basic", lambda *a, **k: None)
    monkeypatch.setattr(wave_runner, "is_market_open", lambda: False)
    monkeypatch.setattr(wave_runner, "_stopping", False)

    prepared: list[str] = []
    monkeypatch.setattr(Instrument, "prepare", lambda self: prepared.append(self.symbol))

    assert wave_runner.run(path, cycle_seconds=0) == 0
    assert not prepared, "nothing should be started into a closed market"
