#!/usr/bin/env bash
# All three apps. Each keeps its own launcher; this calls them in order.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmd="${1:-start}"
case "$cmd" in
  start|stop|restart|status)
    echo "── Pramana (research + account) ──"; (cd "$ROOT/vriddhix" && ./run.sh "$cmd")
    echo "── Terminal (trading) ──";           "$ROOT/run-terminal.sh" "$cmd"
    echo "── Strategy Lab (backtesting) ──";   "$ROOT/run-lab.sh" "$cmd" ;;
  *) echo "usage: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
