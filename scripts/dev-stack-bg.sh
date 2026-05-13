#!/usr/bin/env bash
# Start the FastAPI backend and the Next.js operator UI (web/) in the
# background. Same layout as scripts/dev-stack.sh, but detached: logs go
# to files and PIDs are persisted so you can stop them later.
#
# Usage (repo root):
#   ./scripts/dev-stack-bg.sh            # start (default)
#   ./scripts/dev-stack-bg.sh start
#   ./scripts/dev-stack-bg.sh status
#   ./scripts/dev-stack-bg.sh logs       # tail -f both logs
#   ./scripts/dev-stack-bg.sh stop
#   ./scripts/dev-stack-bg.sh restart
#
# Requires:
#   * Python venv with uvicorn + deps ( pip install -r requirements.txt )
#   * web/node_modules ( cd web && npm install )
#
# Listens:
#   * API:   http://127.0.0.1:8000
#   * Web:   http://127.0.0.1:9000  (rewrites /api/* -> API; override with API_PROXY_TARGET)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUN_DIR="$ROOT/.dev-stack"
LOG_DIR="$RUN_DIR/logs"
mkdir -p "$LOG_DIR"

API_PID_FILE="$RUN_DIR/api.pid"
WEB_PID_FILE="$RUN_DIR/web.pid"
API_LOG="$LOG_DIR/api.log"
WEB_LOG="$LOG_DIR/web.log"

API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-9000}"

_is_running() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

_stop_pid_file() {
  local name="$1" pid_file="$2"
  if ! _is_running "$pid_file"; then
    echo "dev-stack-bg: ${name} not running"
    rm -f "$pid_file"
    return 0
  fi
  local pid
  pid="$(cat "$pid_file")"
  echo "dev-stack-bg: stopping ${name} (pid ${pid})"
  if command -v pkill >/dev/null 2>&1; then
    pkill -TERM -P "$pid" 2>/dev/null || true
  fi
  kill -TERM "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.3
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "dev-stack-bg: force-killing ${name} (pid ${pid})"
    if command -v pkill >/dev/null 2>&1; then
      pkill -KILL -P "$pid" 2>/dev/null || true
    fi
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
}

cmd_start() {
  if _is_running "$API_PID_FILE" || _is_running "$WEB_PID_FILE"; then
    echo "dev-stack-bg: already running — use 'status' or 'restart'" >&2
    cmd_status
    exit 1
  fi

  if [[ ! -d web/node_modules ]]; then
    echo "dev-stack-bg: install web deps first:  cd web && npm install" >&2
    exit 1
  fi

  if [[ -x .venv/bin/uvicorn ]]; then
    UVICORN=(.venv/bin/uvicorn)
  elif command -v uvicorn >/dev/null 2>&1; then
    UVICORN=(uvicorn)
  else
    echo "dev-stack-bg: uvicorn not found. Create .venv and: pip install -r requirements.txt" >&2
    exit 1
  fi

  export API_PROXY_TARGET="${API_PROXY_TARGET:-http://${API_HOST}:${API_PORT}}"

  : > "$API_LOG"
  : > "$WEB_LOG"

  echo "dev-stack-bg: API   -> http://${API_HOST}:${API_PORT}   (log: ${API_LOG})"
  nohup "${UVICORN[@]}" app.main:app --reload --host "$API_HOST" --port "$API_PORT" \
    >>"$API_LOG" 2>&1 &
  echo $! > "$API_PID_FILE"
  disown || true

  sleep 1

  echo "dev-stack-bg: Web   -> http://127.0.0.1:${WEB_PORT}   (log: ${WEB_LOG}, API proxy: ${API_PROXY_TARGET})"
  ( cd web && nohup npm run dev -- -p "$WEB_PORT" >>"$WEB_LOG" 2>&1 & echo $! > "$WEB_PID_FILE" )
  disown || true

  sleep 1
  cmd_status
  echo "dev-stack-bg: started. Stop with: ./scripts/dev-stack-bg.sh stop"
}

cmd_stop() {
  _stop_pid_file "Web" "$WEB_PID_FILE"
  _stop_pid_file "API" "$API_PID_FILE"
}

cmd_status() {
  local any=0
  for entry in "API:$API_PID_FILE:${API_HOST}:${API_PORT}" "Web:$WEB_PID_FILE:127.0.0.1:${WEB_PORT}"; do
    IFS=: read -r name pid_file host port <<<"$entry"
    if _is_running "$pid_file"; then
      echo "dev-stack-bg: ${name} running (pid $(cat "$pid_file"))  http://${host}:${port}"
      any=1
    else
      echo "dev-stack-bg: ${name} not running"
    fi
  done
  [[ "$any" -eq 1 ]]
}

cmd_logs() {
  if [[ ! -f "$API_LOG" && ! -f "$WEB_LOG" ]]; then
    echo "dev-stack-bg: no logs yet (start first)" >&2
    exit 1
  fi
  touch "$API_LOG" "$WEB_LOG"
  echo "dev-stack-bg: tailing ${API_LOG} and ${WEB_LOG} (Ctrl+C to stop)"
  exec tail -n 50 -F "$API_LOG" "$WEB_LOG"
}

cmd_restart() {
  cmd_stop || true
  sleep 1
  cmd_start
}

case "${1:-start}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  status)  cmd_status || true ;;
  logs)    cmd_logs ;;
  restart) cmd_restart ;;
  *)
    echo "usage: $0 {start|stop|status|logs|restart}" >&2
    exit 2
    ;;
esac
