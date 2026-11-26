#!/usr/bin/env bash
# Read-only smoke check of this checkout's local docker-compose stack (WP-13).
#
#   bash infra/dev/smoke.sh
#
# It starts, stops, recreates, migrates and writes nothing. It reads container state and port bindings
# (docker inspect), sends GET requests to the published ports, and runs read-only checks inside the
# running containers with services/core-api/scripts/dev_smoke.py: one READ ONLY transaction as the
# runtime login in core-api, and each worker's own start-up checks. It never connects as the schema
# owner or a superuser.
#
# Which stack: the Compose project of the checkout this file is in (COMPOSE_PROJECT_NAME from the
# environment or that checkout's .env, else the folder name). Extra compose files: set COMPOSE_FILE.
# Overrides: SMOKE_API_URL (default http://127.0.0.1:8000), SMOKE_AI_URL (default http://127.0.0.1:8001),
# SMOKE_RUNTIME_USER (default studygraph_app).
#
# Exit status: 0 every check passed (SKIP and NOTE lines do not fail), 1 a check failed, 2 the stack
# could not be checked (Docker unreachable, invalid compose configuration, no containers).
#
# A pass does not mean the product works end to end: there is no login system yet (protected routes
# answer 401) and the packaged ai-service is a fixture mock that core-api refuses. The NOTE lines say so.

set -uo pipefail

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/../.." || exit 2
API_URL="${SMOKE_API_URL:-http://127.0.0.1:8000}"
AI_URL="${SMOKE_AI_URL:-http://127.0.0.1:8001}"
RUNTIME_USER="${SMOKE_RUNTIME_USER:-studygraph_app}"

passed=0
failed=0
skipped=0
TMP="$(mktemp -d "${TMPDIR:-/tmp}/studygraph-smoke.XXXXXX")" || exit 2
trap 'rm -rf "$TMP"' EXIT

report() { # STATUS CHECK DETAIL
  printf '%-4s  %-13s %s\n' "$1" "$2" "$3"
  case "$1" in
    PASS) passed=$((passed + 1)) ;;
    FAIL) failed=$((failed + 1)) ;;
    SKIP) skipped=$((skipped + 1)) ;;
  esac
}

relay() { # OUTPUT CHECK: report the STATUS|check|detail lines of a container check
  local status name detail seen=0
  while IFS='|' read -r status name detail; do
    case "$status" in
      PASS | FAIL | SKIP | NOTE) report "$status" "$name" "$detail"; seen=1 ;;
    esac
  done <<EOF
$1
EOF
  if [ "$seen" -eq 0 ]; then
    report FAIL "$2" "no result: $(printf '%s\n' "$1" | sed '/^[[:space:]]*$/d' | tail -n 1)"
  fi
}

stop() { # MESSAGE: the stack cannot be checked at all
  printf 'FAIL  %-13s %s\n' "precondition" "$1"
  exit 2
}

role_of() { # worker service -> preflight role (app.ingestion.runtime)
  case "$1" in
    worker | worker-maintenance) echo processing ;;
    worker-index) echo index ;;
    worker-harvest) echo harvest ;;
    worker-mastery) echo mastery ;;
    *) echo "" ;;
  esac
}

# --- preconditions --------------------------------------------------------------------------------
# `docker info` can exit 0 without a server; `docker version` cannot print the server version without one.
docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || stop "Docker is not reachable (is Docker Desktop running?)"
docker compose config >"$TMP/config" 2>"$TMP/config.err" \
  || stop "docker compose config failed: $(tail -n 1 "$TMP/config.err")"
project="$(sed -n 's/^name: //p' "$TMP/config" | head -n 1)"
services="$(docker compose config --services 2>/dev/null | sort)"
ids="$(docker compose ps -a -q 2>"$TMP/ps.err")" || stop "docker compose ps failed: $(tail -n 1 "$TMP/ps.err")"
[ -n "$ids" ] || stop "no containers for project ${project:-?}; start it with: docker compose up -d"

echo "StudyGraph local smoke check (read-only)"
echo "project: $project  api: $API_URL  ai-service: $AI_URL  checkout: $(pwd)"
echo

# service | oneoff | status | exit code | health | host bindings. `index` because a container without a
# healthcheck may have no Health key at all, which a plain field lookup refuses.
# shellcheck disable=SC2086 # one argument per container id
docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.oneoff"}}|{{.State.Status}}|{{.State.ExitCode}}|{{with index .State "Health"}}{{index . "Status"}}{{end}}|{{range $p, $b := .HostConfig.PortBindings}}{{range $b}}{{.HostIp}}:{{.HostPort}}->{{$p}} {{end}}{{end}}' \
  $ids >"$TMP/containers" 2>"$TMP/inspect.err" || stop "docker inspect failed for project $project: $(tail -n 1 "$TMP/inspect.err")"

# --- 1. services ----------------------------------------------------------------------------------
total=0
up=0
problems=""
exit78=0
for svc in $services; do
  total=$((total + 1))
  rows="$(awk -F'|' -v s="$svc" '$1 == s && $2 != "True"' "$TMP/containers")"
  if [ -z "$rows" ]; then
    problems="$problems $svc(no container)"
    continue
  fi
  ok=1
  while IFS='|' read -r _ _ status code health _; do
    if [ "$status" != "running" ]; then
      ok=0
      problems="$problems $svc($status, exit $code)"
      [ "$code" = "78" ] && exit78=1
    elif [ -n "$health" ] && [ "$health" != "healthy" ]; then
      ok=0
      problems="$problems $svc($health)"
    fi
  done <<EOF
$rows
EOF
  [ "$ok" -eq 1 ] && up=$((up + 1))
done
if [ -z "$problems" ]; then
  report PASS services "$up of $total running, healthchecks healthy"
else
  hint=""
  [ "$exit78" -eq 1 ] && hint=" (exit 78: schema revision mismatch; docker compose run --rm migrate && docker compose run --rm db-init)"
  report FAIL services "$up of $total running; not running or not healthy:$problems$hint"
fi

# --- 2. host port bindings ------------------------------------------------------------------------
published=""
exposed=""
while IFS='|' read -r svc oneoff _ _ _ bindings; do
  [ "$oneoff" = "True" ] && continue
  for binding in $bindings; do
    host="${binding%%->*}"
    case "$host" in
      127.0.0.1:* | "[::1]":* | ::1:*) published="$published $svc=$host" ;;
      :*) exposed="$exposed $svc=*$host(all interfaces)" ;;
      *) exposed="$exposed $svc=$host" ;;
    esac
  done
done <"$TMP/containers"
if [ -n "$exposed" ]; then
  report FAIL ports "published beyond loopback:$exposed (docs/database-security.md, Development)"
elif [ -n "$published" ]; then
  report PASS ports "loopback only:$published"
else
  report FAIL ports "nothing is published; $API_URL and $AI_URL cannot answer"
fi

# --- 3. readiness ---------------------------------------------------------------------------------
code="$(curl -sS -o "$TMP/ready" -w '%{http_code}' --max-time 5 "$API_URL/health/ready" 2>"$TMP/curl.err")"
if [ "$code" = "200" ] && grep -q '"ready"' "$TMP/ready"; then
  report PASS readiness "GET /health/ready: 200 ready"
elif [ "$code" = "000" ]; then
  report FAIL readiness "no answer from $API_URL: $(tail -n 1 "$TMP/curl.err")"
else
  report FAIL readiness "GET /health/ready: $code $(head -c 200 "$TMP/ready")"
fi

# --- 4. core-api: identity, schema, grants, AI logins, superuser sessions, upload store -----------
if awk -F'|' '$1 == "core-api" && $2 != "True" && $3 == "running" {found=1} END {exit !found}' "$TMP/containers"; then
  out="$(docker compose exec -T core-api python -m scripts.dev_smoke api "$RUNTIME_USER" </dev/null 2>&1)"
  relay "$out" core-api
else
  report SKIP core-api "core-api is not running: identity, schema, grants and upload store not checked"
fi

# --- 5. worker start-up checks --------------------------------------------------------------------
for svc in $services; do
  case "$svc" in worker | worker-*) ;; *) continue ;; esac
  role="$(role_of "$svc")"
  if [ -z "$role" ]; then
    report SKIP preflight "$svc: no start-up check known for this service"
  elif awk -F'|' -v s="$svc" '$1 == s && $2 != "True" && $3 == "running" {found=1} END {exit !found}' "$TMP/containers"; then
    out="$(docker compose exec -T "$svc" python -m scripts.dev_smoke worker "$role" "$svc" </dev/null 2>&1)"
    relay "$out" preflight
  else
    report SKIP preflight "$svc is not running"
  fi
done

# --- 6. authentication seam -----------------------------------------------------------------------
code="$(curl -sS -D "$TMP/auth.headers" -o /dev/null -w '%{http_code}' --max-time 5 "$API_URL/api/v1/courses" 2>/dev/null)"
if [ "$code" = "401" ] && grep -qi '^www-authenticate: *bearer' "$TMP/auth.headers"; then
  report PASS auth-seam "GET /api/v1/courses without a token: 401, WWW-Authenticate: Bearer"
  report NOTE auth-seam "this is the fail-closed seam, not a login system: no user can sign in yet (CE-3, P0-A)"
else
  report FAIL auth-seam "GET /api/v1/courses without a token: expected 401 with WWW-Authenticate: Bearer, got ${code:-no answer}"
fi

# --- 7. ai-service --------------------------------------------------------------------------------
code="$(curl -sS -o "$TMP/ai" -w '%{http_code}' --max-time 5 "$AI_URL/health" 2>/dev/null)"
if [ "$code" = "200" ]; then
  report PASS ai-service "GET /health: 200"
  if tr -d ' \n' <"$TMP/ai" | grep -q '"mock":true'; then
    report NOTE ai-service "the fixture mock: core-api refuses it by design, so no AI feature runs end to end"
  else
    report NOTE ai-service "reports itself as non-mock; core-api still checks its capabilities per feature"
  fi
else
  report FAIL ai-service "GET $AI_URL/health: ${code:-no answer}"
fi

echo
echo "Summary: $passed passed, $failed failed, $skipped skipped"
[ "$failed" -eq 0 ]
