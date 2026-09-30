#!/usr/bin/env bash
# The tick feed — one websocket, shared.
#
# Flattrade permits exactly ONE websocket per client id. Two processes that
# each open their own put the pair into a loop of kicking each other off
# every few seconds, and a strategy that loses the race gets no ticks at
# all: "handshake refused (t=ck, s=NOT_OK)".
#
# ticker_daemon.py holds that single connection and fans ticks out over a
# local Unix socket. common_lib.initialise_ticker looks for the socket and
# shares the feed when it is there, falling back to a direct connection
# when it is not -- so this is optional, and running it is strictly better
# whenever more than one strategy process is alive.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/venv"
LOG="$ROOT/logs/ticker_daemon.log"
SOCK="/tmp/algo_backtesting_ticker_daemon.sock"

export BROKER_NAME="${BROKER_NAME:-paper_dhan}"

running() { pgrep -f "[t]icker_daemon.py" >/dev/null 2>&1; }

case "${1:-start}" in
  start)
    if running; then echo "already running — socket $SOCK"; exit 0; fi
    # A socket left behind by a killed daemon makes every strategy try to
    # share a feed that is not there.
    [ -S "$SOCK" ] && ! running && rm -f "$SOCK"
    mkdir -p "$ROOT/logs"
    ( cd "$ROOT" && nohup "$VENV/bin/python3" ticker_daemon.py >>"$LOG" 2>&1 & )
    for _ in $(seq 1 20); do
      [ -S "$SOCK" ] && { echo "✓ ticker daemon up — one feed, shared at $SOCK"; exit 0; }
      sleep 0.5
    done
    echo "did not come up; last lines of $LOG:" >&2; tail -12 "$LOG" >&2; exit 1 ;;
  stop)
    pkill -f "[t]icker_daemon.py" 2>/dev/null && echo "stopped" || echo "not running"
    rm -f "$SOCK" ;;
  restart) "$0" stop; sleep 1; exec "$0" start ;;
  status)
    if running; then echo "ticker daemon: running (pid $(pgrep -f '[t]icker_daemon.py' | head -1))"
    else echo "ticker daemon: not running — each strategy will open its own feed,"
         echo "               and Flattrade allows only one per client id"; fi
    [ -S "$SOCK" ] && echo "socket: $SOCK" || echo "socket: absent" ;;
  logs) tail -f "$LOG" ;;
  *) echo "usage: $0 {start|stop|restart|status|logs}" >&2; exit 2 ;;
esac
