#!/usr/bin/env bash
# Start the FastAPI backend and the Next.js operator UI (web/) together.
#
# Usage (repo root):
#   bash scripts/dev-stack.sh
#   # or, after: chmod +x scripts/dev-stack.sh
#   ./scripts/dev-stack.sh
#
# Requires:
#   * Python venv with uvicorn + deps ( pip install -r requirements.txt )
#   * web/node_modules ( cd web && npm install )
#
# Listens:
#   * API:   http://127.0.0.1:8000
#   * Web:   http://127.0.0.1:9000  (rewrites /api/* → API; override with API_PROXY_TARGET)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -d web/node_modules ]]; then
  echo "dev-stack: install web deps first:  cd web && npm install" >&2
  exit 1
fi

if [[ -x .venv/bin/uvicorn ]]; then
  UVICORN=(.venv/bin/uvicorn)
elif command -v uvicorn >/dev/null 2>&1; then
  UVICORN=(uvicorn)
else
  echo "dev-stack: uvicorn not found. Create .venv and: pip install -r requirements.txt" >&2
  exit 1
fi

cleanup() {
  if [[ -n "${WEB_PID:-}" ]] && kill -0 "$WEB_PID" 2>/dev/null; then
    if command -v pkill >/dev/null 2>&1; then
      pkill -P "$WEB_PID" 2>/dev/null || true
    fi
    kill "$WEB_PID" 2>/dev/null || true
  fi
  if [[ -n "${API_PID:-}" ]] && kill -0 "$API_PID" 2>/dev/null; then
    kill "$API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

export API_PROXY_TARGET="${API_PROXY_TARGET:-http://127.0.0.1:8000}"

echo "dev-stack: API      → http://127.0.0.1:8000  (docs /docs)"
"${UVICORN[@]}" app.main:app --reload --host 127.0.0.1 --port 8000 &
API_PID=$!

# Brief pause so the API socket is up before the first browser request.
sleep 1

echo "dev-stack: Next.js  → http://127.0.0.1:9000  (API proxy: ${API_PROXY_TARGET})"
( cd web && exec npm run dev ) &
WEB_PID=$!

echo "dev-stack: press Ctrl+C to stop both"
wait
