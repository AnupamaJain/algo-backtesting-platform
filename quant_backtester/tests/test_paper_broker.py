

def test_a_stale_quote_will_not_produce_a_fill():
    """The quote provider falls back to the last cached close when the feed
    is down. That is right for marking a book and wrong for executing: it
    books P&L at a price nobody could have traded."""
    from datetime import datetime, timedelta

    import pytest

    from quant_backtester.src.broker.exceptions import OrderRejected
    from quant_backtester.src.broker.models import Side, UnifiedOrder, UnifiedQuote
    from quant_backtester.src.broker.paper import PaperBroker
    from quant_backtester.src.broker.quotes import QuoteProvider
    from quant_backtester.src.broker.store import BrokerStore
    import tempfile
    from pathlib import Path

    class TwoDaysOld(QuoteProvider):
        def get_quote(self, symbol):
            return UnifiedQuote(
                symbol=symbol, last_price=100.0,
                timestamp=datetime.now() - timedelta(days=2),
            )

    tmp = Path(tempfile.mkdtemp())
    broker = PaperBroker(
        store=BrokerStore(tmp / "p.db"), quotes=TwoDaysOld(),
        config={"starting_cash": 100_000.0, "universe": ["X"]},
    )
    with pytest.raises(OrderRejected, match="minutes old"):
        broker.place_order(UnifiedOrder(symbol="X", side=Side.BUY, quantity=1))


def test_the_staleness_limit_can_be_disabled_for_deliberate_replay():
    from datetime import datetime, timedelta
    from pathlib import Path
    import tempfile

    from quant_backtester.src.broker.models import Side, UnifiedOrder, UnifiedQuote
    from quant_backtester.src.broker.paper import PaperBroker
    from quant_backtester.src.broker.quotes import QuoteProvider
    from quant_backtester.src.broker.store import BrokerStore

    class Old(QuoteProvider):
        def get_quote(self, symbol):
            return UnifiedQuote(symbol=symbol, last_price=100.0,
                                timestamp=datetime.now() - timedelta(days=30))

    tmp = Path(tempfile.mkdtemp())
    broker = PaperBroker(
        store=BrokerStore(tmp / "p.db"), quotes=Old(),
        config={"starting_cash": 100_000.0, "universe": ["X"],
                "max_quote_age_seconds": 0},
    )
    placed = broker.place_order(UnifiedOrder(symbol="X", side=Side.BUY, quantity=1))
    assert placed.broker_order_id
