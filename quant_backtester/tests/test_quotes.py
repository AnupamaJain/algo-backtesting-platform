"""A dead broker session must not stop the platform pricing instruments.

The point of the adapter abstraction is that one broker failing is
survivable. These tests drive that directly.
"""

from datetime import datetime

import pytest

from quant_backtester.src.broker.exceptions import BrokerUnavailable, InstrumentNotFound
from quant_backtester.src.broker.models import UnifiedQuote
from quant_backtester.src.broker.quotes import ChainedQuotes, QuoteProvider


class Source(QuoteProvider):
    def __init__(self, price=None, fail=False, only=None):
        self.price, self.fail, self.only = price, fail, only
        self.calls = 0

    def get_quote(self, symbol):
        self.calls += 1
        if self.fail:
            raise BrokerUnavailable("session invalid", broker="x")
        if self.only is not None and symbol not in self.only:
            raise InstrumentNotFound(symbol, symbol=symbol)
        return UnifiedQuote(symbol=symbol, last_price=self.price, timestamp=datetime.now())


def _chain(*sources):
    return ChainedQuotes([(f"s{i}", (lambda s=s: s)) for i, s in enumerate(sources)])


def test_the_primary_is_used_when_it_works():
    a, b = Source(100.0), Source(200.0)
    chain = _chain(a, b)

    assert chain.get_quote("X").last_price == 100.0
    assert b.calls == 0, "fallback must not be touched when the primary works"


def test_a_dead_primary_falls_through_to_the_backup():
    a, b = Source(fail=True), Source(200.0)
    chain = _chain(a, b)

    assert chain.get_quote("X").last_price == 200.0
    assert chain.last_source == "s1"


def test_the_source_that_served_a_quote_is_recorded():
    """A backup quote must be distinguishable from a primary one."""
    chain = _chain(Source(fail=True), Source(200.0))
    chain.get_quote("X")
    assert chain.last_source == "s1"


def test_every_source_failing_raises_rather_than_returning_a_made_up_price():
    chain = _chain(Source(fail=True), Source(fail=True))
    with pytest.raises(BrokerUnavailable, match="no quote source"):
        chain.get_quote("X")


def test_a_broken_source_is_not_reconstructed_on_every_symbol():
    """Rebuilding a failing broker authenticates each time; once is enough."""
    attempts = []

    def explode():
        attempts.append(1)
        raise RuntimeError("token invalid")

    good = Source(150.0)
    chain = ChainedQuotes([("bad", explode), ("good", lambda: good)])
    for sym in ("A", "B", "C"):
        chain.get_quote(sym)

    assert len(attempts) == 1, "failed source was rebuilt repeatedly"


def test_a_source_is_not_constructed_until_it_is_needed():
    built = []
    good = Source(100.0)
    chain = ChainedQuotes([
        ("primary", lambda: good),
        ("backup", lambda: built.append(1) or Source(1.0)),
    ])
    chain.get_quote("X")
    assert built == [], "the backup was constructed despite the primary working"


def test_one_unpriceable_symbol_falls_through_per_symbol():
    a = Source(100.0, only={"A"})
    b = Source(200.0)
    chain = _chain(a, b)

    assert chain.get_quote("A").last_price == 100.0
    assert chain.get_quote("B").last_price == 200.0


def test_every_indian_path_prefers_flattrade():
    """Flattrade is the primary on every NSE path, Dhan the fallback.

    Not a style preference: Dhan rate-limits token generation to once every
    two minutes and its market feed to about one request per second, so a
    busy loop stalls on it. Flipping this ordering by accident would be a
    quiet performance regression rather than an obvious break, hence a test.
    """
    from pathlib import Path

    import yaml

    config = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "broker.yaml").read_text()
    )
    indian = {
        name: settings
        for name, settings in config["brokers"].items()
        if settings.get("quote_source") in ("flattrade", "dhan")
    }
    assert indian, "expected at least one NSE paper account"
    for name, settings in indian.items():
        assert settings["quote_source"] == "flattrade", (
            f"{name} should quote from Flattrade first, not {settings['quote_source']}"
        )
        assert "dhan" in settings.get("quote_fallbacks", []), (
            f"{name} should keep Dhan as a fallback"
        )


def test_the_indian_universe_ingests_from_flattrade_first():
    from pathlib import Path

    import yaml

    data = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "universe_india.yaml").read_text()
    )["data"]
    assert data["source"] == "flattrade"
    assert data.get("source_fallbacks") == ["dhan"]


def test_the_dhan_fallback_does_not_inherit_flattrades_token_file(monkeypatch):
    """`quote_source: flattrade` + `quote_fallbacks: [dhan]` share one config
    dict. Its `token_file` key names FLATTRADE's token -- if DhanAuth reads
    it unchanged, the Dhan fallback "authenticates" with a Flattrade token
    and gets a real (and misleading) 401 the moment Flattrade's own session
    lapses, i.e. exactly when the fallback is needed most.
    """
    from quant_backtester.src.broker import dhan as dhan_module
    from quant_backtester.src.broker.quotes import build_quote_provider

    seen_token_files = []

    class RecordingDhanAuth:
        def __init__(self, broker, config):
            seen_token_files.append(config.get("token_file"))

        def authenticate(self):
            raise RuntimeError("no live call in this test")

    monkeypatch.setattr(dhan_module, "DhanAuth", RecordingDhanAuth)

    config = {
        "quote_source": "flattrade",
        "quote_fallbacks": ["dhan"],
        "token_file": "state/flattrade_token.json",
    }
    provider = build_quote_provider(config, data_dir=__import__("pathlib").Path("/tmp"))
    # Force construction of the (lazy) dhan link.
    provider._provider("dhan", dict(provider._builders)["dhan"])

    assert seen_token_files == [None], (
        f"DhanAuth must not receive Flattrade's token_file, got {seen_token_files}"
    )
