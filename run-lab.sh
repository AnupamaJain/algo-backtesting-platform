#!/usr/bin/env bash
# The Strategy Lab — start, stop and check.
#
# Sources the same shared secret the other two apps read. Without it the Lab
# refuses every request (middleware.ts fails closed), which is the intended
# state for an install that has not been paired.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB="$ROOT/quant_backtester/web"
PORT=4300
LOG="$ROOT/logs/strategy-lab.log"
if [ -f "$ROOT/vriddhix/state/sso.env" ]; then set -a; . "$ROOT/vriddhix/state/sso.env"; set +a; fi
export NEXT_PUBLIC_PRAMANA_URL="${NEXT_PUBLIC_PRAMANA_URL:-http://127.0.0.1:8787}"
running() { pgrep -f "next dev -p $PORT" >/dev/null 2>&1; }
case "${1:-start}" in
  start)
    if running; then echo "already running — http://127.0.0.1:$PORT"; exit 0; fi
    mkdir -p "$ROOT/logs"
    # Loopback only: the gateway is the door. It was listening on the LAN.
    (cd "$WEB" && nohup npm run dev -- -H 127.0.0.1 > "$LOG" 2>&1 &)
    for _ in $(seq 1 60); do
      sleep 1
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/lab/sso" 2>/dev/null || true)
      if [ "$code" = "403" ] || [ "$code" = "404" ]; then
        echo "✓ lab is up on loopback :$PORT — reach it at the gateway: http://127.0.0.1:8787/lab"
        [ -n "${PRAMANA_SSO_SECRET:-}" ] && echo "  sign-in: paired with Pramana" || echo "  sign-in: NOT paired (PRAMANA_SSO_SECRET unset) — every request will be refused"
        exit 0
      fi
    done
    echo "did not come up; last lines of $LOG:" >&2; tail -15 "$LOG" >&2; exit 1 ;;
  stop)    pkill -f "next dev -p $PORT" 2>/dev/null && echo "stopped" || echo "not running" ;;
  restart) "$0" stop; sleep 2; "$0" start ;;
  status)
    if running; then echo "lab: running — http://127.0.0.1:$PORT"; else echo "lab: not running"; fi
    [ -f "$ROOT/vriddhix/state/sso.env" ] && echo "sign-in: paired" || echo "sign-in: standalone (refuses all)" ;;
  logs)    tail -f "$LOG" ;;
  *)       echo "usage: $0 {start|stop|restart|status|logs}" >&2; exit 2 ;;
esac
