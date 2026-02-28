#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8865}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${2:-$ROOT_DIR/.state/phase2-proof/pr5/host-smoke-$STAMP}"
HOST="127.0.0.1"
BASE_URL="http://$HOST:$PORT"

mkdir -p "$OUT_DIR"
cd "$ROOT_DIR"

SERVER_LOG="$OUT_DIR/serve-v2.log"
STATUS_LOG="$OUT_DIR/status.txt"

python3 run.py serve-v2 --host "$HOST" --port "$PORT" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT

ready=0
for _ in $(seq 1 80); do
  if curl -sSf "$BASE_URL/v2/portfolio" -o "$OUT_DIR/portfolio.html"; then
    ready=1
    break
  fi
  sleep 0.25
done

if [[ "$ready" -ne 1 ]]; then
  echo "serve-v2 did not become ready on $BASE_URL" | tee "$STATUS_LOG"
  exit 1
fi

echo "GET /v2/portfolio = 200" | tee "$STATUS_LOG"
curl -sSf "$BASE_URL/v2/intake" -o "$OUT_DIR/intake.html"
echo "GET /v2/intake = 200" | tee -a "$STATUS_LOG"
curl -sSf "$BASE_URL/v2/exceptions" -o "$OUT_DIR/exceptions.html"
echo "GET /v2/exceptions = 200" | tee -a "$STATUS_LOG"

echo "host_ui_smoke: PASS" | tee -a "$STATUS_LOG"
echo "artifacts=$OUT_DIR" | tee -a "$STATUS_LOG"
