#!/usr/bin/env bash
# VriddhiX — start, stop and check the local server.
#
#   ./run.sh start     start on http://127.0.0.1:8787
#   ./run.sh stop      stop it
#   ./run.sh restart   stop then start
#   ./run.sh status    is it up, and what does it hold
#   ./run.sh logs      follow the server log
#
# Binds to 127.0.0.1 only. The server is reachable from this machine and
# nowhere else, which is the right default for something holding a research
# database and account credentials.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$(cd "$ROOT/.." && pwd)/venv"
PYTHON="$VENV/bin/python3"
HOST="${VRIDDHIX_HOST:-127.0.0.1}"
PORT="${VRIDDHIX_PORT:-8787}"
LOG="$ROOT/state/logs/serve.log"
PATTERN="vriddhix.cli serve"

# A signing key must exist or tokens cannot be issued. Generated once and
# kept out of git, so restarting does not invalidate everyone's session.
SECRET_FILE="$ROOT/state/jwt-secret"

die() { echo "✗ $*" >&2; exit 1; }

ensure_secret() {
  if [[ ! -f "$SECRET_FILE" ]]; then
    mkdir -p "$(dirname "$SECRET_FILE")"
    "$PYTHON" -c "import secrets; print(secrets.token_urlsafe(48))" > "$SECRET_FILE"
    chmod 600 "$SECRET_FILE"
    echo "  generated a signing key at state/jwt-secret (mode 0600)"
  fi
}

pid_of() { pgrep -f "$PATTERN" 2>/dev/null | head -1; }

start() {
  local existing; existing="$(pid_of || true)"
  if [[ -n "$existing" ]]; then
    echo "already running (pid $existing) — http://$HOST:$PORT"
    return 0
  fi

  [[ -x "$PYTHON" ]] || die "no virtualenv at $VENV. Create one and pip install -r requirements.txt"
  mkdir -p "$ROOT/state/logs"
  ensure_secret

  # All three descriptors are replaced before exec, so the server holds no
  # end of a caller's pipe. Without this, `./run.sh start | tail` hangs
  # forever: tail waits for EOF that the long-lived child never sends.
  (
    cd "$ROOT"
    PYTHONPATH="$ROOT/src" \
    VRIDDHIX_API_JWT_SECRET="$(cat "$SECRET_FILE")" \
    exec "$PYTHON" -m vriddhix.cli serve --host "$HOST" --port "$PORT" \
      < /dev/null >> "$LOG" 2>&1
  ) &
  disown 2>/dev/null || true

  # Wait for it to answer rather than guessing at a sleep duration.
  for _ in $(seq 1 40); do
    if curl -fsS -o /dev/null "http://$HOST:$PORT/api/v1/ops/health" 2>/dev/null; then
      echo "✓ VriddhiX is up"
      echo
      echo "   http://$HOST:$PORT/          landing"
      echo "   http://$HOST:$PORT/learn     how the engines work"
      echo "   http://$HOST:$PORT/login     sign in"
      echo "   http://$HOST:$PORT/api/docs  API reference"
      return 0
    fi
    sleep 0.5
  done

  echo "✗ did not come up within 20s. Last lines of the log:" >&2
  tail -20 "$LOG" >&2
  return 1
}

stop() {
  local existing; existing="$(pid_of || true)"
  if [[ -z "$existing" ]]; then
    echo "not running"
    return 0
  fi
  pkill -f "$PATTERN" || true
  for _ in $(seq 1 20); do
    pid_of >/dev/null 2>&1 || { echo "✓ stopped"; return 0; }
    sleep 0.5
  done
  # Only after a graceful stop has been given time to work.
  pkill -9 -f "$PATTERN" 2>/dev/null || true
  echo "✓ stopped (forced)"
}

status() {
  local existing; existing="$(pid_of || true)"
  if [[ -z "$existing" ]]; then
    echo "server: not running"
  else
    echo "server: running (pid $existing) — http://$HOST:$PORT"
  fi

  if [[ -f "$ROOT/state/vriddhix.db" ]]; then
    "$PYTHON" - "$ROOT/state/vriddhix.db" <<'PY'
import sqlite3, sys
db = sys.argv[1]
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
def one(q, default=0):
    try:
        return c.execute(q).fetchone()[0]
    except sqlite3.Error:
        return default

first, last = c.execute("select min(date), max(date) from ohlcv_daily").fetchone()
symbols = one("select count(*) from stocks")
bars = one("select count(*) from ohlcv_daily")
scans = one("select count(*) from scan_runs where status = 'COMPLETED'")
patterns = one("select count(*) from patterns")
breakouts = one("select count(*) from breakouts")

print(f"data  : {symbols} symbols · {bars:,} bars · {first} .. {last}")
print(f"scans : {scans:,} sessions · {patterns:,} patterns · {breakouts:,} breakouts")
row = c.execute("select date, regime, round(regime_score,1) from market_regimes "
                "order by date desc limit 1").fetchone()
if row:
    print(f"regime: {row[1]} ({row[2]}) as of {row[0]}")
print(f"users : {one('select count(*) from users')}")
PY
  else
    echo "data  : no database yet — run: python -m vriddhix.cli init"
  fi

  # Captured first, then matched. `launchctl list | grep -q` looks correct
  # and is not: grep exits at the first match, launchctl takes SIGPIPE, and
  # `set -o pipefail` reports the whole pipeline as failed -- so a loaded
  # agent was reported as missing.
  local agents; agents="$(launchctl list 2>/dev/null || true)"
  echo -n "agent : "
  if [[ "$agents" == *ai.vriddhix.nightly* ]]; then
    echo "ai.vriddhix.nightly loaded (weekdays 19:00 IST)"
  else
    echo "not loaded — cp deploy/ai.vriddhix.nightly.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/ai.vriddhix.nightly.plist"
  fi
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)  status ;;
  logs)    tail -f "$LOG" ;;
  *)       echo "usage: ./run.sh {start|stop|restart|status|logs}" >&2; exit 2 ;;
esac
