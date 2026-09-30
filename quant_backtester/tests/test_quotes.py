"""A dead broker session must not stop the platform pricing instruments.

The point of the adapter abstraction is that one broker failing is
survivable. These tests drive that directly.
"""

from datetime import datetime, timezone

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


def test_the_dhan_account_quotes_from_dhan_and_keeps_a_fallback():
    """The Dhan paper account prices from Dhan, with Flattrade behind it.

    This asserted the opposite for a long time, on a real measurement:
    pointed at Dhan, the profile logged 17 rate-limit retries for one
    served quote, because Dhan's market feed allows roughly one request per
    second. What made that bite was a missing quote TTL (every poll of
    every symbol reached the feed) and unbatched fetches (one request per
    symbol). Both are fixed -- 5s TTL, and LegacyBrokerShim.quote() batches
    through get_quotes -- so the book, the orders, the contracts and now
    the prices all describe the same broker.

    The fallback is the part worth protecting: without it, a rate-limit
    stall or an expired token leaves the book unpriced.
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
        fallbacks = settings.get("quote_fallbacks", [])
        assert fallbacks, f"{name} has no quote fallback; one feed failing unprices the book"
        assert settings["quote_source"] not in fallbacks, (
            f"{name} lists its own primary as a fallback"
        )
        assert settings.get("quote_ttl_seconds"), (
            f"{name} has no quote TTL. Dhan's feed allows about one request a "
            f"second, so an untimed poll loop stalls on rate limits."
        )

    assert config["brokers"]["paper_dhan"]["quote_source"] == "dhan", (
        "the Dhan account should price from Dhan"
    )
    assert "flattrade" in config["brokers"]["paper_dhan"]["quote_fallbacks"]


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


_NOW = datetime(2026, 9, 29, 11, 0, tzinfo=timezone.utc)


def test_the_shim_prices_a_book_in_one_call():
    """DhanAdapter.get_quotes batches; the shim used to loop past it.

    Its docstring says a per-symbol loop "turns marking a 20-symbol book into
    a 20-second stall" on a one-request-per-second feed. Nothing called it:
    every strategy reaches the broker through LegacyBrokerShim.quote(), which
    asked one symbol at a time. Marking a book produced a burst of rate-limit
    retries and a single served quote.
    """
    from quant_backtester.src.broker.legacy import LegacyBrokerShim
    from quant_backtester.src.broker.models import UnifiedQuote

    calls = {"single": 0, "batched": 0}

    class Batching:
        name = "dhan"

        def get_quote(self, symbol):
            calls["single"] += 1
            return UnifiedQuote(symbol=symbol, last_price=100.0, timestamp=_NOW)

        def get_quotes(self, symbols):
            calls["batched"] += 1
            return {s: UnifiedQuote(symbol=s, last_price=100.0, timestamp=_NOW) for s in symbols}

    shim = LegacyBrokerShim(Batching())
    out = shim.quote([f"NSE:SYM{i}-EQ" for i in range(20)])

    assert len(out) == 20
    assert calls["batched"] == 1, "the book was not priced in one request"
    assert calls["single"] == 0, "fell back to per-symbol despite get_quotes"


def test_a_failed_batch_still_prices_what_it_can():
    """A batch endpoint having a bad day must not blank the whole book."""
    from quant_backtester.src.broker.legacy import LegacyBrokerShim
    from quant_backtester.src.broker.models import UnifiedQuote

    class BrokenBatch:
        name = "dhan"

        def get_quote(self, symbol):
            if symbol == "BAD-EQ":
                raise RuntimeError("no such instrument")
            return UnifiedQuote(symbol=symbol, last_price=50.0, timestamp=_NOW)

        def get_quotes(self, symbols):
            raise RuntimeError("batch endpoint down")

    out = LegacyBrokerShim(BrokenBatch()).quote(
        ["NSE:GOOD-EQ", "NSE:BAD-EQ", "NSE:ALSOGOOD-EQ"]
    )

    assert set(out) == {"NSE:GOOD-EQ", "NSE:ALSOGOOD-EQ"}


def test_an_adapter_without_batching_still_works():
    """Flattrade and the paper broker have no get_quotes."""
    from quant_backtester.src.broker.legacy import LegacyBrokerShim
    from quant_backtester.src.broker.models import UnifiedQuote

    class NoBatch:
        name = "flattrade"

        def get_quote(self, symbol):
            return UnifiedQuote(symbol=symbol, last_price=10.0, timestamp=_NOW)

    out = LegacyBrokerShim(NoBatch()).quote(["NSE:A-EQ", "NSE:B-EQ"])
    assert len(out) == 2


def test_history_fails_over_like_quotes_do():
    """A paper account trades real prices, so it can read real history.

    Only the money is simulated. Refusing outright made the early-exit
    preview -- which needs one specific one-minute candle to compute a fair
    value -- answer 500 on a paper account.
    """
    from quant_backtester.src.broker.quotes import ChainedQuotes

    class NoHistory:
        def get_quote(self, symbol):  # pragma: no cover - not exercised
            raise RuntimeError("no")

    class HasHistory:
        def get_quote(self, symbol):  # pragma: no cover - not exercised
            raise RuntimeError("no")

        def get_history(self, symbol, start, end, interval="day"):
            return [("bar", symbol, interval)]

    chain = ChainedQuotes([("first", NoHistory), ("second", HasHistory)])
    assert chain.get_history("NIFTY 50", None, None, "minute") == [("bar", "NIFTY 50", "minute")]


def test_history_says_so_when_no_source_has_any():
    from quant_backtester.src.broker.exceptions import UnsupportedOperation
    from quant_backtester.src.broker.quotes import ChainedQuotes

    class NoHistory:
        def get_quote(self, symbol):  # pragma: no cover - not exercised
            raise RuntimeError("no")

    chain = ChainedQuotes([("only", NoHistory)])
    with pytest.raises(UnsupportedOperation):
        chain.get_history("NIFTY 50", None, None)


def test_a_token_is_translated_back_to_its_symbol():
    """Kite addressed instruments by number; every adapter here uses symbols.

    A caller holding a token read it off a quote, so the quote is where the
    translation comes from. Without it the token went out as a symbol and
    Dhan answered "'26000' is not in the Dhan scrip master".
    """
    from quant_backtester.src.broker.legacy import LegacyBrokerShim

    class Adapter:
        name = "stub"
        asked: list = []

        def get_quote(self, symbol):
            from quant_backtester.src.broker.models import UnifiedQuote
            from datetime import datetime
            return UnifiedQuote(symbol=symbol, last_price=1.0,
                                timestamp=datetime.now(), broker_token="26000")

        def get_history(self, symbol, start, end, interval="day"):
            Adapter.asked.append(symbol)
            import pandas as pd
            return pd.DataFrame(
                [{"Open": 1, "High": 1, "Low": 1, "Close": 1, "Volume": 0}],
                index=pd.to_datetime(["2026-09-30"]),
            )

    shim = LegacyBrokerShim(Adapter(), default_exchange="NSE")
    served = shim.quote("NSE:NIFTY 50")
    assert served["NSE:NIFTY 50"]["instrument_token"] == "26000"

    shim.historical_data(26000, "2026-09-30", "2026-09-30", "minute")
    assert Adapter.asked == ["NIFTY 50"], "the numeric token reached the adapter"
