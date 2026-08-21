#!/usr/bin/env bash
#
# Minimal end-to-end developer smoke test for CareerLens (Prompt 0.4).
#
# Starts the Docker data services, applies migrations, starts the API and
# web app, verifies the API is reachable — including that the API's CORS
# policy would actually let the web page's browser fetch succeed — and
# shuts everything down again, on success or failure.
#
# No authentication, no product data, no new dependencies: just curl,
# lsof, docker, uv, and npx, all already required by the rest of the
# project (or standard on macOS/Linux).
#
# Usage: ./scripts/smoke.sh   (or: make smoke)
# Override ports if the defaults are busy: API_PORT=8001 WEB_PORT=3001 ./scripts/smoke.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"
# WEB_ORIGIN mirrors what a real browser sends as the Origin header when a
# developer types "localhost:3000" — used only as a header value, never
# polled directly, so it stays as "localhost".
WEB_ORIGIN="http://localhost:${WEB_PORT}"
# API_BASE and WEB_BASE are polled directly by this script. They use
# 127.0.0.1, not "localhost": uvicorn's default bind is IPv4-only
# (127.0.0.1), and "localhost" can resolve to the IPv6 loopback (::1)
# first on some systems — silently polling a *different* listener on the
# same port number instead of the one this script actually started.
API_BASE="http://127.0.0.1:${API_PORT}"
WEB_BASE="http://127.0.0.1:${WEB_PORT}"

API_PID=""
WEB_PID=""

log() { printf '[smoke] %s\n' "$1"; }
fail() {
  printf '[smoke] FAIL: %s\n' "$1" >&2
  exit 1
}

kill_port() {
  # Defensive fallback: kill anything still listening on $1, in case the
  # tracked PID was a wrapper process rather than the real server.
  local port="$1" pids
  pids="$(lsof -ti "tcp:${port}" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "$pids" | xargs kill >/dev/null 2>&1 || true
  fi
}

cleanup() {
  local exit_code=$?
  log "Cleaning up..."
  if [ -n "$WEB_PID" ]; then
    kill "$WEB_PID" >/dev/null 2>&1 || true
    wait "$WEB_PID" 2>/dev/null || true
  fi
  if [ -n "$API_PID" ]; then
    kill "$API_PID" >/dev/null 2>&1 || true
    wait "$API_PID" 2>/dev/null || true
  fi
  kill_port "$WEB_PORT"
  kill_port "$API_PORT"
  docker compose down >/dev/null 2>&1 || true
  if [ "$exit_code" = "0" ]; then
    log "Cleanup complete. Smoke test PASSED."
  else
    log "Cleanup complete. Smoke test FAILED."
  fi
  exit "$exit_code"
}
trap cleanup EXIT

wait_for_http() {
  local url="$1" tries="${2:-30}"
  for _ in $(seq 1 "$tries"); do
    if curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null | grep -q '^2'; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_postgres_healthy() {
  local tries="${1:-30}" state
  for _ in $(seq 1 "$tries"); do
    state="$(docker inspect --format='{{.State.Health.Status}}' careerlens-postgres 2>/dev/null || echo "")"
    [ "$state" = "healthy" ] && return 0
    sleep 1
  done
  return 1
}

log "Checking Docker is reachable..."
docker info >/dev/null 2>&1 || fail "Docker daemon not reachable. Start Docker Desktop, then re-run. See README Troubleshooting."

log "Starting Docker data services (Postgres + Redis)..."
docker compose up -d || fail "docker compose up -d failed."

log "Waiting for Postgres to become healthy..."
wait_for_postgres_healthy 30 || fail "Postgres did not become healthy in time."
log "Postgres is healthy."

log "Applying database migrations..."
(cd apps/api && uv run alembic upgrade head) || fail "Migration failed."
log "Migrations applied."

log "Starting API on port ${API_PORT}..."
(
  cd apps/api
  export WEB_ORIGIN="$WEB_ORIGIN"
  exec uv run uvicorn app.main:app --port "$API_PORT"
) >/tmp/careerlens-smoke-api.log 2>&1 &
API_PID=$!

wait_for_http "${API_BASE}/api/v1/health" 30 || fail "API liveness endpoint did not respond. See /tmp/careerlens-smoke-api.log"
log "API liveness OK."

[ "$(curl -s -o /dev/null -w '%{http_code}' "${API_BASE}/api/v1/health/db")" = "200" ] \
  || fail "API readiness endpoint (/health/db) did not report healthy."
log "API readiness (database) OK."

log "Starting web app on port ${WEB_PORT}..."
(
  cd apps/web
  # What the *browser* is told to fetch — "localhost", matching the
  # WEB_ORIGIN a real browser tab would use, and page.tsx's own default.
  export NEXT_PUBLIC_API_URL="http://localhost:${API_PORT}"
  exec npx next dev --port "$WEB_PORT"
) >/tmp/careerlens-smoke-web.log 2>&1 &
WEB_PID=$!

wait_for_http "$WEB_BASE" 60 || fail "Web app did not respond. See /tmp/careerlens-smoke-web.log"
log "Web app is serving."

log "Verifying the API's CORS policy allows the web page's origin..."
cors_header="$(curl -s -D - -o /dev/null -H "Origin: ${WEB_ORIGIN}" "${API_BASE}/api/v1/health" | grep -i '^access-control-allow-origin:' || true)"
echo "$cors_header" | grep -qi "$WEB_ORIGIN" \
  || fail "API did not return the expected Access-Control-Allow-Origin for ${WEB_ORIGIN}."
log "CORS OK: ${cors_header}"

log "Verifying the web page contains the health-check markup..."
curl -s "$WEB_BASE" | grep -q 'data-testid="api-health-status"' \
  || fail "Web page response did not contain the expected health-check markup."
log "Web page markup OK."

log ""
log "Automated checks all passed. This proves the API is reachable and the"
log "CORS policy would allow the browser fetch to succeed. It does NOT"
log "execute JavaScript, so it cannot see the resolved \"API is healthy\""
log "text the way a real browser would — and this script is about to shut"
log "everything back down, so there's nothing left running to look at."
log ""
log "For that final visual confirmation, run the services yourself with"
log "'make start', 'make start-api', 'make start-web' (see README) and"
log "open http://localhost:3000 in a browser — they stay up until you"
log "stop them, unlike this script."
log ""
log "SMOKE TEST PASSED"
