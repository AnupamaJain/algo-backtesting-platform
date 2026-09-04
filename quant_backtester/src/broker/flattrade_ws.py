"""Flattrade tick stream, presented as a KiteTicker.

The production strategies are tick-driven: they set `kws.on_ticks`, call
`kws.subscribe(tokens)` and block. That interface is KiteTicker's. Flattrade
speaks Noren's websocket protocol instead, which differs in every particular
— connection handshake, subscription message, tick field names, and the fact
that Noren sends *partial* updates where Kite sends complete ones.

`FlattradeTicker` bridges the two so the strategies need no changes.

THE PARTIAL-UPDATE TRAP, which is the whole difficulty here. Noren sends a
full snapshot once (`t: "tk"`) and thereafter sends only the fields that
CHANGED (`t: "tf"`) — a tick may contain nothing but a new last price, with
no volume, no OHLC, not even the symbol. Forwarding those straight through
produces ticks whose `last_price` is None every other message, and a
strategy reading `tick['last_price']` either crashes or, worse, treats a
missing field as a zero. So the last full state per token is retained and
each partial update is merged into it before the tick is emitted.

TWO DIFFERENT WEBSOCKETS, TWO DIFFERENT HANDSHAKES. Flattrade exposes
`PiConnectWSTp` (shared with the web app and mobile app — only one such
session at a time) and a dedicated `PiConnectWSAPI` for API use. They do not
speak the same protocol:

    WSTp  auth frame: {"t":"c", "uid", "actid", "susertoken", "source":"API"}
          reply:      {"t":"ck", "s": "OK" | "NOT_OK"}

    WSAPI auth frame: {"t":"a", "uid", "actid", "source":"API", "accesstoken"}
          reply:      {"t":"ak","s": "OK" | "NOT_OK"}

Sending the WSTp frame to WSAPI (or vice versa) is rejected outright — this
is what "logging out of the mobile app changes nothing" looks like from the
outside, and it cost real debugging time before the mismatch was confirmed
against live ticks. WSAPI is used here because it does not contend with the
web/mobile session for the single-feed slot.

WHAT IS NOT PROVIDED. Kite's full market depth (five bid/ask levels) is
available from Noren's SNAPQUOTE feed but is not mapped here; MODE_FULL
returns touchline fields only. A strategy that reads `tick['depth']` will
find it absent rather than fabricated.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

__all__ = ["FlattradeTicker"]

WS_URL = "wss://piconnect.flattrade.in/PiConnectWSAPI/"

#: Noren touchline field -> the Kite tick key a strategy expects.
_FIELD_MAP = {
    "lp": "last_price",
    "v": "volume_traded",
    "o": "ohlc_open",
    "h": "ohlc_high",
    "l": "ohlc_low",
    "c": "ohlc_close",
    "ap": "average_traded_price",
    "oi": "oi",
    "bp1": "depth_buy",
    "sp1": "depth_sell",
    "pc": "change",
}


class FlattradeTicker:
    """KiteTicker's interface over Flattrade's websocket.

    Assign the callbacks the strategies assign — `on_ticks`, `on_connect`,
    `on_close`, `on_error`, `on_order_update` — then `connect(threaded=True)`.
    """

    # KiteTicker exposes these as instance constants; strategies pass them to
    # set_mode(). Noren has no equivalent granularity, so they are accepted
    # and recorded rather than acted on.
    MODE_LTP = "ltp"
    MODE_QUOTE = "quote"
    MODE_FULL = "full"

    def __init__(self, user_id: str, token: str, url: str = WS_URL) -> None:
        self._user_id = user_id
        self._token = token
        self._url = url

        self.on_ticks = None
        self.on_connect = None
        self.on_close = None
        self.on_error = None
        self.on_order_update = None
        self.on_noreconnect = None

        self._ws = None
        self._thread = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._send_lock = threading.Lock()
        #: Set when the broker refuses the handshake. Retrying will not help.
        self._rejected = False
        self.rejection_reason: str | None = None
        self._subscribed: set[str] = set()
        self._mode = self.MODE_FULL
        # Last complete state per token, so partial updates can be merged.
        self._state: dict[str, dict] = {}

    # -- lifecycle -----------------------------------------------------

    def connect(self, threaded: bool = True, **_ignored) -> None:
        import websocket

        self._stop.clear()
        self._ws = websocket.WebSocketApp(
            self._url,
            on_open=self._handle_open,
            on_data=self._handle_data,
            on_error=self._handle_error,
            on_close=self._handle_close,
        )

        def run():
            while not self._stop.is_set():
                try:
                    # NOT `ping_interval=`/`ping_payload=` -- that sends a raw
                    # WebSocket-protocol PING control frame every N seconds,
                    # not a Noren message. Confirmed live: with that in place
                    # the server closed the connection every ~2-3s ("code
                    # 1000 - goodbye") even with nothing else touching the
                    # feed. Noren's own heartbeat is a plain `{"t":"h"}` TEXT
                    # frame, sent by `_heartbeat_loop()` below instead.
                    self._ws.run_forever()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Flattrade websocket ended: %s", exc)
                if self._stop.is_set():
                    break
                if self._rejected:
                    # A refused handshake will be refused again. Spinning here
                    # burns the connection and hides the real cause behind a
                    # wall of reconnect messages.
                    logger.error(
                        "Flattrade refused the feed: %s. Not reconnecting.",
                        self.rejection_reason,
                    )
                    if self.on_noreconnect:
                        self._safely(self.on_noreconnect, self)
                    break
                # Otherwise reconnect: a dropped feed mid-session would leave
                # a tick-driven strategy silently blind.
                logger.warning("Flattrade websocket dropped; reconnecting")
                time.sleep(1.0)

        self._thread = threading.Thread(target=run, daemon=True, name="flattrade-ws")
        self._thread.start()

        if not threaded:
            self._thread.join()

    def close(self, *_args, **_kwargs) -> None:
        self._stop.set()
        self._connected.clear()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self.close()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    # -- subscription ---------------------------------------------------

    def subscribe(self, instrument_tokens) -> None:
        """Subscribe to tokens, in Noren's `EXCHANGE|TOKEN` form.

        Kite passes bare numeric tokens; Noren needs the exchange too. A bare
        token is assumed NSE, which is what the equity strategies use.
        """
        tokens = [self._as_noren(t) for t in _as_list(instrument_tokens)]
        self._subscribed.update(tokens)
        self._send({"t": "t", "k": "#".join(tokens)})
        logger.info("Subscribed to %s instrument(s)", len(tokens))

    def unsubscribe(self, instrument_tokens) -> None:
        tokens = [self._as_noren(t) for t in _as_list(instrument_tokens)]
        self._subscribed.difference_update(tokens)
        self._send({"t": "u", "k": "#".join(tokens)})

    def set_mode(self, mode, instrument_tokens) -> None:
        """Accepted for interface compatibility.

        Noren's touchline feed has no per-subscription mode. Recording the
        request rather than silently ignoring it means `MODE_FULL` does not
        quietly become something else.
        """
        self._mode = mode
        logger.debug("set_mode(%s) recorded; Noren has no equivalent", mode)

    def resubscribe(self) -> None:
        if self._subscribed:
            self._send({"t": "t", "k": "#".join(sorted(self._subscribed))})

    # -- websocket callbacks --------------------------------------------

    def _handle_open(self, *_args) -> None:
        # Authenticates ON the socket, after it opens — WSAPI's frame, not
        # WSTp's. Field name AND message type ("a" vs "c") both differ.
        self._send_raw(
            {
                "t": "a",
                "uid": self._user_id,
                "actid": self._user_id,
                "source": "API",
                "accesstoken": self._token,
            }
        )

    def _handle_data(self, ws=None, message=None, data_type=None, continue_flag=None) -> None:
        try:
            payload = json.loads(message)
        except Exception:  # noqa: BLE001 - a malformed frame must not kill the feed
            logger.debug("Unparseable websocket frame: %r", message)
            return

        kind = payload.get("t")

        if kind == "ck" or kind == "ak":
            if payload.get("s") == "OK":
                self._connected.set()
                logger.info("Flattrade websocket authenticated")
                self.resubscribe()
                self._start_heartbeat()
                if self.on_connect:
                    self._safely(self.on_connect, self, payload)
            else:
                # Almost always the single-feed limit: the mobile app or a
                # charting bridge already holds this account's one slot.
                self._rejected = True
                self.rejection_reason = (
                    "handshake refused (t=ck, s=NOT_OK). Flattrade allows ONE "
                    "websocket per client ID — close the mobile app and any "
                    "charting bridge (Tradetron, OpenAlgo) holding the feed, "
                    "then reconnect."
                )
                logger.error("%s", self.rejection_reason)
                if self.on_error:
                    self._safely(self.on_error, self, payload)
            return

        if kind in ("tk", "tf", "dk", "df"):
            tick = self._to_kite_tick(payload, snapshot=kind in ("tk", "dk"))
            if tick and self.on_ticks:
                self._safely(self.on_ticks, self, [tick])
            return

        if kind == "om" and self.on_order_update:
            self._safely(self.on_order_update, self, payload)

    def _handle_error(self, ws=None, error=None) -> None:
        if self.on_error:
            self._safely(self.on_error, self, error if error is not None else ws)

    def _handle_close(self, ws=None, close_status_code=None, close_msg=None) -> None:  # noqa: ARG002
        self._connected.clear()
        # The server closing a just-authenticated, otherwise-idle connection
        # within ~1s is almost always another active session for this same
        # client ID (mobile app, web login, or another API process) winning
        # Flattrade's one-feed-per-account limit -- surfaced plainly here so
        # it isn't buried in the underlying `websocket` library's own debug
        # log, which is silent unless trace logging is explicitly enabled.
        logger.warning(
            "Flattrade websocket closed by the server (code=%s, msg=%s). If this "
            "repeats within ~1s of every reconnect, another session (mobile app, "
            "web login, or another script) is holding this account's one feed.",
            close_status_code, close_msg,
        )
        if self.on_close:
            self._safely(self.on_close, self, close_status_code, close_msg)

    # -- heartbeat --------------------------------------------------------

    _HEARTBEAT_INTERVAL_SECONDS = 30

    def _start_heartbeat(self) -> None:
        """Keep the session alive with Noren's own message, not a WS ping.

        A protocol-level ping (`websocket-client`'s `ping_interval=`) sends a
        control frame Noren does not expect and gets the connection closed
        for it; this sends the plain `{"t":"h"}` text frame the API actually
        wants, on its own thread so it survives regardless of what the
        strategy's main thread is doing.
        """
        thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="flattrade-ws-heartbeat"
        )
        thread.start()

    def _heartbeat_loop(self) -> None:
        # Captures the connection generation implicitly via `is_connected`:
        # once this drops (close, or a fresh connect resets `_connected`
        # then sets it again), this loop's own connection has ended and it
        # exits rather than sending a stale heartbeat into the new one.
        while not self._stop.is_set() and self._connected.is_set():
            for _ in range(self._HEARTBEAT_INTERVAL_SECONDS):
                if self._stop.is_set() or not self._connected.is_set():
                    return
                time.sleep(1)
            if self._stop.is_set() or not self._connected.is_set():
                return
            self._send({"t": "h"})

    # -- translation -----------------------------------------------------

    def _to_kite_tick(self, payload: dict, *, snapshot: bool) -> dict | None:
        """Merge a Noren update into the token's last known state.

        Noren sends a full snapshot once, then only changed fields. Emitting
        a partial as though it were complete gives a strategy a tick whose
        last_price is missing — which reads as zero to code that does not
        check, and that is a wrong price, not a missing one.
        """
        token = payload.get("tk")
        if not token:
            return None

        state = self._state.setdefault(token, {"instrument_token": token})
        if snapshot:
            state["tradingsymbol"] = payload.get("ts", state.get("tradingsymbol", ""))
            state["exchange"] = payload.get("e", state.get("exchange", ""))

        for noren_key, kite_key in _FIELD_MAP.items():
            if noren_key in payload:
                value = _num(payload[noren_key])
                if value is not None:
                    state[kite_key] = value

        if "last_price" not in state:
            # Nothing usable yet: a first frame carrying only a symbol name.
            return None

        ohlc = {
            k: state.get(f"ohlc_{k}")
            for k in ("open", "high", "low", "close")
            if state.get(f"ohlc_{k}") is not None
        }
        tick = {
            "instrument_token": _int_or(token),
            "tradingsymbol": state.get("tradingsymbol", ""),
            "exchange": state.get("exchange", ""),
            "last_price": state["last_price"],
            "volume_traded": state.get("volume_traded", 0),
            "average_traded_price": state.get("average_traded_price"),
            "oi": state.get("oi"),
            "change": state.get("change"),
            "timestamp": datetime.now(),
            "mode": self._mode,
        }
        if ohlc:
            tick["ohlc"] = ohlc
        return tick

    # -- plumbing --------------------------------------------------------

    def _as_noren(self, token) -> str:
        text = str(token)
        return text if "|" in text else f"NSE|{text}"

    def _send(self, values: dict) -> None:
        # Wait briefly for the handshake rather than dropping the message.
        if not self._connected.wait(timeout=10):
            logger.warning("Websocket not connected; %s not sent", values.get("t"))
            return
        self._send_raw(values)

    def _send_raw(self, values: dict) -> None:
        if self._ws is None:
            return
        with self._send_lock:
            try:
                self._ws.send(json.dumps(values))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Websocket send failed: %s", exc)

    @staticmethod
    def _safely(callback, *args) -> None:
        """A raising callback must not kill the feed for every other symbol."""
        try:
            callback(*args)
        except Exception as exc:  # noqa: BLE001
            logger.error("Tick callback raised: %s", exc, exc_info=True)


def _as_list(value):
    return value if isinstance(value, (list, tuple, set)) else [value]


def _num(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _int_or(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class SharedTickerClient:
    """A `FlattradeTicker`-shaped client of `ticker_daemon.py`'s Unix socket.

    Flattrade allows exactly one live PiConnectWSAPI feed per client ID.
    Running two strategy processes at once, each with its own
    `FlattradeTicker`, put them into a loop of kicking each other's
    connection off every few seconds -- confirmed live. This class lets a
    strategy process get ticks WITHOUT holding that one real connection
    itself: `ticker_daemon.py` holds it and fans ticks out over the socket,
    filtered so each client only ever sees the tokens it subscribed to.

    Presents the same surface `initialise_ticker()` already drives
    (`on_ticks`, `on_connect`, `subscribe`, `set_mode`, `connect(threaded=)`)
    so no strategy script needs to change to use it.
    """

    MODE_FULL = "full"

    def __init__(self, socket_path) -> None:
        self._socket_path = str(socket_path)
        self._sock: socket.socket | None = None
        self._connected = threading.Event()
        self._subscribed: set[int] = set()
        self._stop = False
        self._send_lock = threading.Lock()

        self.on_ticks = None
        self.on_connect = None
        self.on_order_update = None
        self.on_noreconnect = None

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def connect(self, threaded: bool = True, **_ignored) -> None:
        thread = threading.Thread(target=self._run, daemon=True, name="shared-ticker-client")
        thread.start()
        if not threaded:
            thread.join()

    def _run(self) -> None:
        while not self._stop:
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._sock.connect(self._socket_path)
                self._connected.set()
                if self._subscribed:
                    self._send({"op": "subscribe", "tokens": list(self._subscribed)})
                self._read_loop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ticker_daemon connection lost (%s); reconnecting", exc)
            finally:
                self._connected.clear()
            if self._stop:
                return
            time.sleep(2)

    def _read_loop(self) -> None:
        buf = b""
        while True:
            chunk = self._sock.recv(4096)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                self._handle(msg)

    def _handle(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "connect" and self.on_connect:
            self._safely(self.on_connect, self, {})
        elif event == "tick" and self.on_ticks:
            self._safely(self.on_ticks, self, [msg["tick"]])

    def subscribe(self, instrument_tokens) -> None:
        tokens = [int(t) for t in _as_list(instrument_tokens)]
        self._subscribed.update(tokens)
        if self._connected.is_set():
            self._send({"op": "subscribe", "tokens": tokens})

    def unsubscribe(self, instrument_tokens) -> None:
        tokens = [int(t) for t in _as_list(instrument_tokens)]
        self._subscribed.difference_update(tokens)
        if self._connected.is_set():
            self._send({"op": "unsubscribe", "tokens": tokens})

    def set_mode(self, mode, instrument_tokens) -> None:
        """The daemon's upstream feed is always full/touchline; nothing to
        negotiate per client. Accepted for interface compatibility."""

    def _send(self, payload: dict) -> None:
        with self._send_lock:
            try:
                self._sock.sendall((json.dumps(payload) + "\n").encode())
            except OSError as exc:
                logger.warning("ticker_daemon send failed: %s", exc)

    def close(self) -> None:
        self._stop = True
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def stop(self) -> None:
        self.close()

    @staticmethod
    def _safely(callback, *args) -> None:
        try:
            callback(*args)
        except Exception as exc:  # noqa: BLE001
            logger.error("Tick callback raised: %s", exc, exc_info=True)
