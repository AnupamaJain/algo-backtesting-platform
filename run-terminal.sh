#!/usr/bin/env bash
# The trading terminal — start, stop and check.
#
# Kept separate from vriddhix/run.sh because these are separate processes
# with separate risk: this one can place orders, that one cannot. Pairing
# them for sign-in does not merge them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/venv"
PORT=5010
LOG="$ROOT/logs/flask_app.log"

# Which broker the strategies reach. paper_dhan is simulated money against
# real Dhan prices -- no order can leave the building, and the vendor is Dhan
# rather than Zerodha. common_lib reads this; without it the strategies
# default to Flattrade.
export PRAMANA_PUBLIC_URL="${PRAMANA_PUBLIC_URL:-http://127.0.0.1:8787}"
export LAB_PUBLIC_URL="${LAB_PUBLIC_URL:-http://127.0.0.1:4300}"
export BROKER_NAME="${BROKER_NAME:-paper_dhan}"

# Same shared secret the research platform reads. Absent, /sso does not exist.
if [ -f "$ROOT/vriddhix/state/sso.env" ]; then
  set -a; . "$ROOT/vriddhix/state/sso.env"; set +a
fi

running() { pgrep -f "[f]lask_app.py" >/dev/null 2>&1; }

case "${1:-start}" in
  start)
    if running; then echo "already running — http://127.0.0.1:$PORT"; exit 0; fi
    test -d "$VENV" || { echo "no virtualenv at $VENV" >&2; exit 1; }
    mkdir -p "$ROOT/logs"
    # shellcheck disable=SC1091
    . "$VENV/bin/activate"
    nohup python3 "$ROOT/flask_app.py" > "$LOG" 2>&1 &
    for _ in $(seq 1 30); do
      sleep 1
      if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/app_login" 2>/dev/null; then
        echo "✓ terminal is up — http://127.0.0.1:$PORT"
        echo "  broker: $BROKER_NAME"
        grep -m1 "live_trading" "$LOG" 2>/dev/null || true
        exit 0
      fi
    done
    echo "did not come up; last lines of $LOG:" >&2
    tail -15 "$LOG" >&2
    exit 1
    ;;
  stop)    pkill -f "[f]lask_app.py" 2>/dev/null && echo "stopped" || echo "not running" ;;
  restart) "$0" stop; sleep 2; "$0" start ;;
  status)
    if running; then
      echo "terminal: running (pid $(pgrep -f '[f]lask_app.py' | head -1)) — http://127.0.0.1:$PORT"
      grep -m1 "live_trading" "$LOG" 2>/dev/null || true
      echo "broker  : ${BROKER_NAME:-flattrade}"
      if [ -f "$ROOT/vriddhix/state/sso.env" ]; then
        echo "sign-in : paired with the research platform"
      else
        echo "sign-in : standalone (Gatekeeper only)"
      fi
    else
      echo "terminal: not running"
    fi
    ;;
  logs)    tail -f "$LOG" ;;
  *)       echo "usage: $0 {start|stop|restart|status|logs}" >&2; exit 2 ;;
esac
