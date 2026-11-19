#!/usr/bin/env bash
# Run the core-api test suite (including the destructive migration tests and
# the real Redis + Celery worker tests of M3) against a throwaway PostgreSQL +
# pgvector server and a throwaway Redis that exist only while this script
# runs. Usage, from anywhere:
#
#     services/core-api/scripts/test_migrations.sh [extra pytest arguments]
#
# Isolation:
#   - The database, Redis and test containers share a new Docker network
#     created with --internal: no outbound traffic and no route to the host
#     machine (so neither host.docker.internal nor anything published on the
#     host is reachable), and the docker-compose network is not attached, so
#     the development `db` and `redis` services do not even resolve.
#   - The database and Redis containers publish no port and keep their data on
#     tmpfs (Redis also has persistence switched off and requires a password).
#   - Random resource names, credentials and identity tokens per run. The
#     database token is stored as the database's COMMENT here, outside the test
#     process; the tests, migrations/env.py (MIGRATIONS_REQUIRE_DATABASE_COMMENT)
#     and the test workers (DATABASE_REQUIRE_COMMENT) refuse to touch a database
#     that does not carry it. The Redis token is stored under the key
#     studygraph:disposable-test; the broker tests refuse a Redis without it.
#     Nothing is ever flushed: each test uses broker queues with unique names.
#   - Celery workers and Beat started by the tests run inside the test
#     container, on these two servers only.
#   - Test dependencies are installed while building the test image, which is
#     the only step that needs network access.
#   - Cleanup removes only the containers, network, images and temporary
#     directory this script created (never volumes or other containers).
#
# After pytest, the script also migrates the test database to revision 0001
# and checks that its normalized `pg_dump --schema-only` equals
# tests/fixtures/scaffold_schema.sql (the dump of the original scaffold).
#
# Environment knobs:
#   PG_IMAGE              PostgreSQL image (default: pgvector/pgvector:pg16)
#   REDIS_IMAGE           Redis image (default: redis:7, as in docker-compose.yml)
#   MIGTEST_SCHEMA_DUMP   if set, also write `pg_dump --schema-only` of the test
#                         database migrated to head to this path
set -euo pipefail

CORE_API_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_IMAGE="${PG_IMAGE:-pgvector/pgvector:pg16}"
REDIS_IMAGE="${REDIS_IMAGE:-redis:7}"

rand() { openssl rand -hex "$1"; }

RUN_ID="$(date +%Y%m%d%H%M%S)-$(rand 3)"
PREFIX="studygraph-migtest-${RUN_ID}"
NETWORK="${PREFIX}-net"
DB_CONTAINER="${PREFIX}-db"
REDIS_CONTAINER="${PREFIX}-redis"
RUNNER_CONTAINER="${PREFIX}-runner"
BASE_IMAGE="studygraph-core-api-migtest:${RUN_ID}-base"
TEST_IMAGE="studygraph-core-api-migtest:${RUN_ID}"
LABEL="com.studygraph.purpose=migration-test"

DB_NAME="sg_disposable_$(rand 4)"
DB_USER="sg_test_$(rand 4)"
DB_PASSWORD="$(rand 24)"
DB_TOKEN="$(rand 16)"
MARKER="studygraph-disposable-test:${DB_TOKEN}"
REDIS_PASSWORD="$(rand 24)"
REDIS_TOKEN="$(rand 16)"

WORK_DIR="$(mktemp -d)"
created_base_image=0
created_image=0
created_network=0
created_db=0
created_redis=0
created_runner=0

cleanup() {
  local status=$?
  if [ "$created_runner" = 1 ]; then docker rm -f "$RUNNER_CONTAINER" >/dev/null 2>&1 || true; fi
  if [ "$created_redis" = 1 ]; then docker rm -f -v "$REDIS_CONTAINER" >/dev/null 2>&1 || true; fi
  if [ "$created_db" = 1 ]; then docker rm -f -v "$DB_CONTAINER" >/dev/null 2>&1 || true; fi
  if [ "$created_network" = 1 ]; then docker network rm "$NETWORK" >/dev/null 2>&1 || true; fi
  if [ "$created_image" = 1 ]; then docker image rm "$TEST_IMAGE" >/dev/null 2>&1 || true; fi
  if [ "$created_base_image" = 1 ]; then docker image rm "$BASE_IMAGE" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK_DIR"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Never adopt (and later delete) something that already exists under our names.
for name in "$DB_CONTAINER" "$REDIS_CONTAINER" "$RUNNER_CONTAINER"; do
  if docker container inspect "$name" >/dev/null 2>&1; then
    echo "Container $name already exists; refusing to continue." >&2
    exit 1
  fi
done
if docker network inspect "$NETWORK" >/dev/null 2>&1; then
  echo "Network $NETWORK already exists; refusing to continue." >&2
  exit 1
fi

echo "==> Building test image $TEST_IMAGE"
docker build --quiet --label "$LABEL" -t "$BASE_IMAGE" "$CORE_API_DIR" >/dev/null
created_base_image=1
docker build --quiet --label "$LABEL" -t "$TEST_IMAGE" - >/dev/null <<EOF
FROM $BASE_IMAGE
RUN pip install --quiet --no-cache-dir --disable-pip-version-check --root-user-action=ignore -r requirements-dev.txt && pip check
EOF
created_image=1

echo "==> Creating internal network $NETWORK"
docker network create --internal --label "$LABEL" "$NETWORK" >/dev/null
created_network=1

umask 077
cat >"$WORK_DIR/db.env" <<EOF
POSTGRES_DB=$DB_NAME
POSTGRES_USER=$DB_USER
POSTGRES_PASSWORD=$DB_PASSWORD
EOF
cat >"$WORK_DIR/test.env" <<EOF
TEST_DATABASE_URL=postgresql+psycopg://$DB_USER:$DB_PASSWORD@$DB_CONTAINER:5432/$DB_NAME
TEST_DATABASE_TOKEN=$DB_TOKEN
TEST_REDIS_URL=redis://:$REDIS_PASSWORD@$REDIS_CONTAINER:6379/0
TEST_REDIS_TOKEN=$REDIS_TOKEN
EOF
cat >"$WORK_DIR/redis.env" <<EOF
REDIS_PASSWORD=$REDIS_PASSWORD
REDISCLI_AUTH=$REDIS_PASSWORD
EOF
cat >"$WORK_DIR/alembic.env" <<EOF
DATABASE_URL=postgresql+psycopg://$DB_USER:$DB_PASSWORD@$DB_CONTAINER:5432/$DB_NAME
MIGRATIONS_REQUIRE_DATABASE_COMMENT=$MARKER
EOF

echo "==> Starting disposable PostgreSQL ($PG_IMAGE) as $DB_CONTAINER (no published ports, tmpfs data)"
created_db=1
docker run -d --rm --name "$DB_CONTAINER" --label "$LABEL" \
  --network "$NETWORK" --env-file "$WORK_DIR/db.env" \
  --tmpfs /var/lib/postgresql/data "$PG_IMAGE" >/dev/null

# The image's init phase runs a temporary server without TCP; waiting on TCP
# means we only continue once the real server is up.
ready=0
for _ in $(seq 1 60); do
  if docker exec "$DB_CONTAINER" pg_isready -q -h 127.0.0.1 -p 5432 -U "$DB_USER" -d "$DB_NAME"; then
    ready=1
    break
  fi
  sleep 1
done
if [ "$ready" != 1 ]; then
  echo "Disposable PostgreSQL did not become ready." >&2
  exit 1
fi

docker exec "$DB_CONTAINER" psql -q -X -v ON_ERROR_STOP=1 -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" \
  -c "COMMENT ON DATABASE \"$DB_NAME\" IS '$MARKER'" >/dev/null

echo "==> Starting disposable Redis ($REDIS_IMAGE) as $REDIS_CONTAINER (no published ports, password, no persistence)"
created_redis=1
# The password comes from the env file, not the command line of this script.
docker run -d --rm --name "$REDIS_CONTAINER" --label "$LABEL" \
  --network "$NETWORK" --env-file "$WORK_DIR/redis.env" --tmpfs /data "$REDIS_IMAGE" \
  sh -c 'exec redis-server --requirepass "$REDIS_PASSWORD" --save "" --appendonly no' >/dev/null
ready=0
for _ in $(seq 1 30); do
  if [ "$(docker exec "$REDIS_CONTAINER" redis-cli --no-auth-warning ping 2>/dev/null)" = "PONG" ]; then
    ready=1
    break
  fi
  sleep 1
done
if [ "$ready" != 1 ]; then
  echo "Disposable Redis did not become ready." >&2
  exit 1
fi
docker exec "$REDIS_CONTAINER" redis-cli --no-auth-warning \
  SET studygraph:disposable-test "studygraph-disposable-test:$REDIS_TOKEN" >/dev/null

run_in_network() {  # run a command in the test image on the internal network
  local env_file="$1"
  shift
  created_runner=1
  docker run --rm --name "$RUNNER_CONTAINER" --label "$LABEL" \
    --network "$NETWORK" --env-file "$env_file" -e PYTHONDONTWRITEBYTECODE=1 \
    "$TEST_IMAGE" "$@"
}

echo "==> Running pytest in $RUNNER_CONTAINER"
run_in_network "$WORK_DIR/test.env" python -m pytest "$@"

echo "==> Comparing pg_dump of revision 0001 with tests/fixtures/scaffold_schema.sql"
run_in_network "$WORK_DIR/alembic.env" sh -c 'alembic downgrade base && alembic upgrade 0001' \
  >"$WORK_DIR/alembic.log" 2>&1 || { cat "$WORK_DIR/alembic.log" >&2; exit 1; }
docker exec "$DB_CONTAINER" pg_dump -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" \
  --schema-only --no-owner --no-privileges >"$WORK_DIR/baseline.dump.sql"
docker run --rm -i --network none "$TEST_IMAGE" python scripts/normalize_schema_dump.py \
  <"$WORK_DIR/baseline.dump.sql" >"$WORK_DIR/baseline.normalized.sql"
diff -u "$CORE_API_DIR/tests/fixtures/scaffold_schema.sql" "$WORK_DIR/baseline.normalized.sql"
echo "    identical"

if [ -n "${MIGTEST_SCHEMA_DUMP:-}" ]; then
  echo "==> Writing schema dump of the test database at head to $MIGTEST_SCHEMA_DUMP"
  run_in_network "$WORK_DIR/alembic.env" alembic upgrade head \
    >"$WORK_DIR/alembic.log" 2>&1 || { cat "$WORK_DIR/alembic.log" >&2; exit 1; }
  docker exec "$DB_CONTAINER" pg_dump -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" \
    --schema-only --no-owner --no-privileges >"$MIGTEST_SCHEMA_DUMP"
fi

echo "==> Done; removing disposable resources"
