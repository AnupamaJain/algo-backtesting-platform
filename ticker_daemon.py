#!/usr/bin/env python3
"""One shared Flattrade tick feed, fanned out to every strategy process.

WHY THIS EXISTS. Flattrade's PiConnectWSAPI allows exactly one active
websocket feed per client ID -- confirmed live: running two strategy
processes at once (each independently authenticating its own
`FlattradeTicker`) put them into a loop of kicking each other's connection
off every few seconds ("Connection closed normally (code 1000)" from both,
in lockstep). Only one process may hold the real Flattrade connection at a
time; everything else needs ticks handed to it some other way.

This daemon IS that one connection. It authenticates a single
`FlattradeTicker`, and strategy processes connect to it over a local Unix
domain socket instead of Flattrade directly:

    python ticker_daemon.py                     # run until stopped

Protocol (newline-delimited JSON, both directions):
    client -> daemon   {"op": "subscribe",   "tokens": [26000, 42633]}
    client -> daemon   {"op": "unsubscribe", "tokens": [26000]}
    daemon -> client   {"event": "connect"}                  -- upstream (re)connected
    daemon -> client   {"event": "tick", "tick": {...}}       -- only for THIS client's tokens

Each client's subscribed-token set is tracked separately so one strategy
never sees another's ticks -- Survivor's on_ticks_update assumes a
single-symbol feed (`ticks[0]['last_price']`), and a blind broadcast would
silently corrupt it with an unrelated option's price.

Not started automatically: `common_lib.initialise_ticker()` tries the socket
first and falls back to a direct `FlattradeTicker` connection when no daemon
is listening, so a single strategy run works unchanged without this.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ticker_daemon")

SOCKET_PATH = Path("/tmp/algo_backtesting_ticker_daemon.sock")


_clients_lock = threading.Lock()
_clients: dict[socket.socket, set[int]] = {}  # conn -> subscribed tokens


def _broadcast(payload: dict) -> None:
    # A tick's "timestamp" field is a real datetime object (FlattradeTicker
    # returns Kite-shaped ticks, and Kite's own ticks carry one) -- plain
    # json.dumps() raises TypeError on it. That exception was being swallowed
    # by FlattradeTicker's own _safely() wrapper around the tick callback, so
    # every single broadcast silently failed and no client ever received a
    # tick, though the daemon's own upstream connection looked perfectly
    # healthy. default=str makes every value serializable instead of
    # asserting each tick's exact field set.
    line = (json.dumps(payload, default=str) + "\n").encode()
    with _clients_lock:
        dead = []
        for conn, tokens in _clients.items():
            if payload.get("event") == "tick":
                tok = payload["tick"].get("instrument_token")
                try:
                    tok = int(tok)
                except (TypeError, ValueError):
                    pass
                if tok not in tokens:
                    continue
            try:
                conn.sendall(line)
            except OSError:
                dead.append(conn)
        for conn in dead:
            _clients.pop(conn, None)


def _merged_tokens() -> list[int]:
    with _clients_lock:
        merged: set[int] = set()
        for tokens in _clients.values():
            merged |= tokens
    return list(merged)


class _Handler(socketserver.BaseRequestHandler):
    def setup(self) -> None:
        with _clients_lock:
            _clients[self.request] = set()

    def finish(self) -> None:
        with _clients_lock:
            _clients.pop(self.request, None)

    def handle(self) -> None:
        buf = b""
        while True:
            chunk = self.request.recv(4096)
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
                self._handle_message(msg)

    def _handle_message(self, msg: dict) -> None:
        op = msg.get("op")
        tokens = {int(t) for t in msg.get("tokens", [])}
        if op == "subscribe":
            with _clients_lock:
                _clients.setdefault(self.request, set()).update(tokens)
            logger.info("client subscribed to %s (merged: %s)", tokens, _merged_tokens())
            _ensure_upstream_subscribed()
        elif op == "unsubscribe":
            with _clients_lock:
                _clients.get(self.request, set()).difference_update(tokens)


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


_upstream_kws = None
_upstream_subscribed: set[int] = set()
_upstream_lock = threading.Lock()


def _ensure_upstream_subscribed() -> None:
    global _upstream_subscribed
    if _upstream_kws is None or not _upstream_kws.is_connected:
        return  # on_connect will subscribe everything once it fires
    wanted = set(_merged_tokens())
    with _upstream_lock:
        new = wanted - _upstream_subscribed
        if new:
            _upstream_kws.subscribe(list(new))
            _upstream_subscribed |= new


def _on_ticks(ws, ticks) -> None:  # noqa: ARG001
    for tick in ticks:
        _broadcast({"event": "tick", "tick": tick})


def _on_connect(ws, response) -> None:  # noqa: ARG001
    global _upstream_subscribed
    logger.info("upstream Flattrade connected")
    with _upstream_lock:
        _upstream_subscribed = set()
    _ensure_upstream_subscribed()
    _broadcast({"event": "connect"})


def _authenticate_upstream():
    from quant_backtester.src.broker.flattrade import FlattradeAuth
    from quant_backtester.src.broker.flattrade_ws import FlattradeTicker

    session = FlattradeAuth(
        "flattrade",
        {"token_file": "state/flattrade_token.json", "auto_login": True},
    ).authenticate()
    kws = FlattradeTicker(user_id=session.user_id, token=session.access_token)
    kws.on_ticks = _on_ticks
    kws.on_connect = _on_connect
    return kws


def main() -> int:
    global _upstream_kws

    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        # A stale socket from a crashed prior run -- rebinding fails
        # otherwise ("address already in use").
        SOCKET_PATH.unlink()

    _upstream_kws = _authenticate_upstream()
    _upstream_kws.connect(threaded=True)

    server = _Server(str(SOCKET_PATH), _Handler)
    os.chmod(SOCKET_PATH, 0o600)

    # `kill <pid>` sends SIGTERM, not SIGINT -- without a handler for it the
    # process dies immediately and the `finally` block below never runs, so
    # the websocket is never given a chance to send Flattrade a clean close
    # frame. An abruptly-dropped TCP connection can leave the session
    # ambiguous server-side; every earlier `kill` of a strategy process this
    # session did exactly this. Converting SIGTERM into the same clean exit
    # KeyboardInterrupt already gets closes the loop properly every time.
    def _handle_sigterm(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_sigterm)

    logger.info("Ticker daemon listening on %s (Ctrl-C or SIGTERM to stop)", SOCKET_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        _upstream_kws.close()
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
