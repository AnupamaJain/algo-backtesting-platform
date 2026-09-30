#!/usr/bin/env bash
# One product on one port. The terminal and the Lab are separate processes
# bound to loopback; the gateway inside Pramana is the only door, so the
# upstreams come up first and the gateway last.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmd="${1:-start}"
case "$cmd" in
  start|stop|restart|status)
    echo "── Tick feed (one shared websocket) ──";          "$ROOT/run-ticker.sh" "$cmd"
    echo "── Terminal (trading, loopback :5010) ──";        "$ROOT/run-terminal.sh" "$cmd"
    echo "── Strategy Lab (backtesting, loopback :4300) ──"; "$ROOT/run-lab.sh" "$cmd"
    echo "── Pramana (gateway, research, account) ──";      (cd "$ROOT/vriddhix" && ./run.sh "$cmd")
    if [ "$cmd" != stop ]; then
      echo
      echo "One app: http://127.0.0.1:8787"
      echo "  /app                       workspace and hub"
      echo "  /terminal/home             trading terminal"
      echo "  /lab/console/strategies    strategy lab"
      echo "Ports 5010 and 4300 are internal. Nothing else is exposed."
    fi ;;
  *) echo "usage: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
