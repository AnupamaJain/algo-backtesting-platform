"""The tick bridge must never hand a strategy a wrong price.

Noren sends a full snapshot once and thereafter only the fields that
changed. Forwarding a partial as though it were complete gives a strategy a
tick whose last_price is missing — which reads as zero to code that does not
check. That is a wrong price, not a missing one, and it is the failure these
tests exist to prevent.
"""

import json

import pytest

from quant_backtester.src.broker.flattrade_ws import FlattradeTicker


def _ticker():
    return FlattradeTicker(user_id="FZ1", token="TOK")


def _feed(ticker, payload):
    ticker._handle_data(message=json.dumps(payload))


# -- the partial-update problem -----------------------------------------


def test_a_partial_update_keeps_the_last_known_price():
    received = []
    t = _ticker()
    t.on_ticks = lambda ws, ticks: received.extend(ticks)

    # Snapshot, then an update carrying ONLY a new volume.
    _feed(t, {"t": "tk", "tk": "2885", "ts": "RELIANCE-EQ", "e": "NSE",
              "lp": "1300.5", "v": "1000", "o": "1290", "h": "1310",
              "l": "1285", "c": "1295"})
    _feed(t, {"t": "tf", "tk": "2885", "v": "1500"})

    assert len(received) == 2
    assert received[1]["last_price"] == 1300.5, "price was lost on a partial update"
    assert received[1]["volume_traded"] == 1500
    assert received[1]["ohlc"]["high"] == 1310, "OHLC was lost on a partial update"


def test_a_partial_price_update_merges_over_the_snapshot():
    received = []
    t = _ticker()
    t.on_ticks = lambda ws, ticks: received.extend(ticks)

    _feed(t, {"t": "tk", "tk": "2885", "ts": "RELIANCE-EQ", "lp": "1300", "v": "10"})
    _feed(t, {"t": "tf", "tk": "2885", "lp": "1305"})

    assert received[1]["last_price"] == 1305
    assert received[1]["volume_traded"] == 10
    assert received[1]["tradingsymbol"] == "RELIANCE-EQ", "symbol lost on update"


def test_a_frame_before_any_price_emits_nothing():
    """Better no tick than a tick with no price in it."""
    received = []
    t = _ticker()
    t.on_ticks = lambda ws, ticks: received.extend(ticks)

    _feed(t, {"t": "tk", "tk": "2885", "ts": "RELIANCE-EQ"})
    assert received == []


def test_tokens_are_tracked_independently():
    received = []
    t = _ticker()
    t.on_ticks = lambda ws, ticks: received.extend(ticks)

    _feed(t, {"t": "tk", "tk": "1", "ts": "A", "lp": "100"})
    _feed(t, {"t": "tk", "tk": "2", "ts": "B", "lp": "200"})
    _feed(t, {"t": "tf", "tk": "1", "lp": "101"})

    assert received[-1]["instrument_token"] == 1
    assert received[-1]["last_price"] == 101
    assert received[1]["last_price"] == 200, "the other token was corrupted"


# -- connection handshake -----------------------------------------------


def test_authentication_is_sent_on_open():
    """The WSAPI handshake frame specifically: message type "a" and the
    token under "accesstoken" — NOT WSTp's "c"/"susertoken" shape. Sending
    the wrong one is rejected outright, and was the actual cause behind an
    extended debugging session before this was confirmed against live ticks.
    """
    sent = []
    t = _ticker()
    t._ws = type("WS", (), {"send": lambda self, m: sent.append(json.loads(m))})()
    t._handle_open()

    assert sent[0]["t"] == "a"
    assert sent[0]["uid"] == "FZ1"
    assert sent[0]["source"] == "API"
    assert sent[0]["accesstoken"] == "TOK"
    assert "susertoken" not in sent[0]


def test_on_connect_fires_only_after_the_server_confirms():
    connected = []
    t = _ticker()
    t.on_connect = lambda ws, resp: connected.append(resp)
    t._ws = type("WS", (), {"send": lambda self, m: None})()

    _feed(t, {"t": "ak", "s": "NOT_OK"})
    assert connected == [], "connected before the server said OK"

    _feed(t, {"t": "ak", "s": "OK"})
    assert len(connected) == 1
    assert t.is_connected


def test_a_failed_handshake_reports_an_error():
    errors = []
    t = _ticker()
    t.on_error = lambda ws, e: errors.append(e)
    _feed(t, {"t": "ak", "s": "NOT_OK", "emsg": "bad token"})
    assert errors and errors[0]["emsg"] == "bad token"


# -- subscription -------------------------------------------------------


def test_bare_tokens_are_qualified_with_an_exchange():
    """Kite passes numeric tokens; Noren needs EXCHANGE|TOKEN."""
    t = _ticker()
    assert t._as_noren("2885") == "NSE|2885"
    assert t._as_noren("NFO|68407") == "NFO|68407"


def test_subscriptions_are_replayed_after_a_reconnect():
    """A reconnect that forgot its subscriptions would leave a strategy
    connected and permanently silent."""
    sent = []
    t = _ticker()
    t._ws = type("WS", (), {"send": lambda self, m: sent.append(json.loads(m))})()
    t._connected.set()
    t.subscribe(["2885", "1333"])
    sent.clear()

    _feed(t, {"t": "ak", "s": "OK"})       # a fresh handshake
    assert any(m.get("t") == "t" for m in sent), "subscriptions were not replayed"


# -- resilience ---------------------------------------------------------


def test_a_raising_callback_does_not_kill_the_feed():
    """One strategy's bug must not blind every other subscriber."""
    calls = []

    def explode(ws, ticks):
        calls.append(1)
        raise ValueError("strategy bug")

    t = _ticker()
    t.on_ticks = explode
    _feed(t, {"t": "tk", "tk": "1", "ts": "A", "lp": "100"})
    _feed(t, {"t": "tf", "tk": "1", "lp": "101"})
    assert len(calls) == 2, "the feed stopped after a callback raised"


def test_a_malformed_frame_is_ignored():
    t = _ticker()
    t.on_ticks = lambda ws, ticks: pytest.fail("should not emit")
    t._handle_data(message="{not json")


def test_order_updates_are_routed_separately():
    orders = []
    t = _ticker()
    t.on_order_update = lambda ws, o: orders.append(o)
    _feed(t, {"t": "om", "norenordno": "1", "status": "COMPLETE"})
    assert orders[0]["norenordno"] == "1"


def test_it_exposes_the_kiteticker_surface_strategies_use():
    for attr in ("MODE_FULL", "MODE_LTP", "MODE_QUOTE", "connect", "close",
                 "subscribe", "unsubscribe", "set_mode"):
        assert hasattr(FlattradeTicker, attr), f"missing {attr}"


def test_a_refused_handshake_stops_reconnecting():
    """A NOT_OK on WSAPI means the auth frame itself was rejected — a
    refusal is policy, not a network blip. Retrying spins forever and
    buries the cause."""
    t = _ticker()
    t._ws = type("WS", (), {"send": lambda self, m: None})()
    _feed(t, {"t": "ak", "s": "NOT_OK"})

    assert t._rejected is True


def test_a_successful_handshake_leaves_reconnect_enabled():
    t = _ticker()
    t._ws = type("WS", (), {"send": lambda self, m: None})()
    _feed(t, {"t": "ak", "s": "OK"})
    assert t._rejected is False
