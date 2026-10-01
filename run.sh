#!/usr/bin/env bash
# Starts this project's Postgres and resets only its three tables.
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example to .env first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

: "${POSTGRES_DB:?}"
: "${POSTGRES_USER:?}"
: "${POSTGRES_PASSWORD:?}"
: "${POSTGRES_PORT:?}"
: "${TOOLBOX_DB_USER:?}"
: "${TOOLBOX_DB_PASSWORD:?}"

for ident in "$POSTGRES_DB" "$POSTGRES_USER" "$TOOLBOX_DB_USER"; do
  if [[ ! "$ident" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "Database and role names must be plain identifiers. Got: $ident" >&2
    exit 1
  fi
done
toolbox_password_sql=${TOOLBOX_DB_PASSWORD//\'/\'\'}

wait_postgres() {
  local i
  for i in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Postgres is not up. Run ./run.sh up first." >&2
  exit 1
}

psql_admin() {
  docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
    psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"
}

phoenix_healthy() {
  python3 - <<'PY'
import sys
import urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:6006/healthz", timeout=2)
except Exception:
    sys.exit(1)
PY
}

start_phoenix() {
  if phoenix_healthy; then
    echo "Phoenix is already up on 127.0.0.1:6006."
    return
  fi
  mkdir -p .phoenix
  # Disk storage in .phoenix, so a CLI restart does not wipe traces (O-1).
  PHOENIX_WORKING_DIR="$PWD/.phoenix" \
  PHOENIX_HOST=127.0.0.1 \
  PHOENIX_PORT=6006 \
    nohup phoenix serve > .phoenix/serve.log 2>&1 &
  echo $! > .phoenix/phoenix.pid
  local i
  for i in $(seq 1 60); do
    if phoenix_healthy; then
      echo "Phoenix is up on 127.0.0.1:6006."
      return
    fi
    sleep 1
  done
  echo "Phoenix did not become ready. See .phoenix/serve.log." >&2
  exit 1
}

stop_phoenix() {
  if [[ -f .phoenix/phoenix.pid ]]; then
    kill "$(cat .phoenix/phoenix.pid)" 2>/dev/null || true
    rm -f .phoenix/phoenix.pid
  fi
}

judge_ready() {
  python3 - <<'PY'
import sys
import urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:10002/.well-known/agent.json", timeout=2)
except Exception:
    sys.exit(1)
PY
}

start_judge() {
  if judge_ready; then
    echo "Security Judge is already up on 127.0.0.1:10002."
    return
  fi
  mkdir -p logs
  nohup python3 -m guards.judge > logs/judge.log 2>&1 &
  echo $! > logs/judge.pid
  local i
  for i in $(seq 1 30); do
    if judge_ready; then
      echo "Security Judge is up on 127.0.0.1:10002."
      return
    fi
    sleep 1
  done
  echo "Security Judge did not become ready. See logs/judge.log." >&2
  exit 1
}

stop_judge() {
  if [[ -f logs/judge.pid ]]; then
    kill "$(cat logs/judge.pid)" 2>/dev/null || true
    rm -f logs/judge.pid
  fi
}

masker_ready() {
  python3 - <<'PY'
import sys
import urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:10003/.well-known/agent.json", timeout=2)
except Exception:
    sys.exit(1)
PY
}

start_masker() {
  if masker_ready; then
    echo "Data Masker is already up on 127.0.0.1:10003."
    return
  fi
  mkdir -p logs
  nohup python3 -m guards.masker > logs/masker.log 2>&1 &
  echo $! > logs/masker.pid
  local i
  for i in $(seq 1 30); do
    if masker_ready; then
      echo "Data Masker is up on 127.0.0.1:10003."
      return
    fi
    sleep 1
  done
  echo "Data Masker did not become ready. See logs/masker.log." >&2
  exit 1
}

stop_masker() {
  if [[ -f logs/masker.pid ]]; then
    kill "$(cat logs/masker.pid)" 2>/dev/null || true
    rm -f logs/masker.pid
  fi
}

cmd_up() {
  docker compose up -d postgres
  wait_postgres
  echo "Postgres is up on 127.0.0.1:${POSTGRES_PORT}."
  start_phoenix
  start_judge
  start_masker
}

cmd_reset() {
  wait_postgres
  psql_admin <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${TOOLBOX_DB_USER}') THEN
    CREATE ROLE ${TOOLBOX_DB_USER} LOGIN PASSWORD '${toolbox_password_sql}'
      NOSUPERUSER NOCREATEDB NOCREATEROLE;
  ELSE
    ALTER ROLE ${TOOLBOX_DB_USER} WITH LOGIN PASSWORD '${toolbox_password_sql}'
      NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END
\$\$;
DROP TABLE IF EXISTS actions_log, customer_orders, users;
SQL
  docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
    psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f - < db/seed.sql
  psql_admin <<SQL
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${TOOLBOX_DB_USER};
GRANT USAGE ON SCHEMA public TO ${TOOLBOX_DB_USER};
GRANT SELECT ON users, customer_orders TO ${TOOLBOX_DB_USER};
GRANT INSERT ON actions_log TO ${TOOLBOX_DB_USER};
GRANT USAGE, SELECT ON SEQUENCE actions_log_id_seq TO ${TOOLBOX_DB_USER};
SQL
  echo "Reset complete."
}

cmd_stop() {
  stop_masker
  stop_judge
  stop_phoenix
  docker compose stop postgres
}

case "${1:-}" in
  up) cmd_up ;;
  reset) cmd_reset ;;
  stop) cmd_stop ;;
  *)
    echo "Usage: ./run.sh up | reset | stop" >&2
    exit 1
    ;;
esac
