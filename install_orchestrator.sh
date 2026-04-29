#!/usr/bin/env bash
set -euo pipefail

APP_HOME="/opt/netrun-orchestrator"
SERVICE_NAME="netrun-orchestrator"
WORKER_SERVICE_NAME="netrun-orchestrator-worker"
REFILL_SERVICE_NAME="netrun-orchestrator-refill"
VALIDATION_SERVICE_NAME="netrun-orchestrator-validation"
WATCHDOG_SERVICE_NAME="netrun-orchestrator-watchdog"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
WORKER_SERVICE_FILE="/etc/systemd/system/${WORKER_SERVICE_NAME}.service"
REFILL_SERVICE_FILE="/etc/systemd/system/${REFILL_SERVICE_NAME}.service"
VALIDATION_SERVICE_FILE="/etc/systemd/system/${VALIDATION_SERVICE_NAME}.service"
WATCHDOG_SERVICE_FILE="/etc/systemd/system/${WATCHDOG_SERVICE_NAME}.service"
EXTERNAL_DB=0
TMP_SOURCE=""

log() {
  printf '[install_orchestrator] %s\n' "$*"
}

die() {
  printf '[install_orchestrator] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: bash install_orchestrator.sh [--external-db]

Options:
  --external-db  Use DATABASE_URL from .env and skip local PostgreSQL setup.
EOF
}

cleanup_tmp() {
  if [ -n "$TMP_SOURCE" ] && [ -d "$TMP_SOURCE" ]; then
    rm -rf "$TMP_SOURCE"
  fi
}
trap cleanup_tmp EXIT

for arg in "$@"; do
  case "$arg" in
    --external-db)
      EXTERNAL_DB=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $arg"
      ;;
  esac
done

if [ "${EUID}" -ne 0 ]; then
  die "must_run_as_root"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_DIR="$SCRIPT_DIR"

copy_repo_to_opt() {
  local source_real
  source_real="$(realpath "$SOURCE_DIR")"
  mkdir -p "$APP_HOME"

  if [ "$source_real" = "$APP_HOME" ]; then
    log "Using existing repo at $APP_HOME"
    return 0
  fi

  log "Copying orchestrator repo to $APP_HOME"
  local saved_env=""
  if [ -f "$APP_HOME/.env" ]; then
    saved_env="$(mktemp /tmp/netrun-orchestrator-env.XXXXXX)"
    cp "$APP_HOME/.env" "$saved_env"
  fi

  tar -C "$SOURCE_DIR" \
    --exclude='./.git' \
    --exclude='./.venv' \
    --exclude='./jobs' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    -cf - . | tar -C "$APP_HOME" -xpf -

  if [ -n "$saved_env" ]; then
    mv "$saved_env" "$APP_HOME/.env"
  fi
}

install_os_packages() {
  command -v apt-get >/dev/null 2>&1 || die "apt_get_not_found"
  export DEBIAN_FRONTEND=noninteractive
  log "Installing OS dependencies"
  apt-get update
  apt-get install -y python3 python3-venv python3-pip curl jq ca-certificates redis-server
  if [ "$EXTERNAL_DB" -ne 1 ]; then
    apt-get install -y postgresql
  fi
}

random_token() {
  python3 - "${1:-48}" <<'PY'
import secrets
import string
import sys

length = int(sys.argv[1]) if len(sys.argv) > 1 else 48
alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(length)))
PY
}

ensure_env_file() {
  if [ -f "$APP_HOME/.env" ]; then
    chmod 600 "$APP_HOME/.env"
    return 0
  fi

  log "Creating .env"
  local api_key db_password
  api_key="$(random_token 48)"
  db_password="$(random_token 32)"
  cat > "$APP_HOME/.env" <<EOF
ORCHESTRATOR_API_KEY=$api_key
ORCHESTRATOR_HOST=0.0.0.0
ORCHESTRATOR_PORT=8090
DB_NAME=netrun_orchestrator
DB_USER=netrun_orchestrator
DB_PASSWORD=$db_password
DATABASE_URL=postgresql://netrun_orchestrator:$db_password@127.0.0.1:5432/netrun_orchestrator
JOBS_ROOT=/opt/netrun-orchestrator/jobs
NODE_REQUEST_TIMEOUT_SEC=1200
ORCHESTRATOR_START_PORT_MIN=32000
ORCHESTRATOR_START_PORT_MAX=65000
WORKER_POLL_INTERVAL_SEC=2
REDIS_URL=redis://127.0.0.1:6379/0
EOF
  chmod 600 "$APP_HOME/.env"
}

load_env() {
  set -a
  # shellcheck disable=SC1091
  . "$APP_HOME/.env"
  set +a
}

validate_pg_identifier() {
  local value="$1"
  [[ "$value" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "invalid_postgres_identifier: $value"
}

setup_local_postgres() {
  if [ "$EXTERNAL_DB" -eq 1 ]; then
    [ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL is required with --external-db"
    return 0
  fi

  log "Configuring local PostgreSQL"
  systemctl enable postgresql >/dev/null
  systemctl start postgresql >/dev/null

  DB_NAME="${DB_NAME:-netrun_orchestrator}"
  DB_USER="${DB_USER:-netrun_orchestrator}"
  DB_PASSWORD="${DB_PASSWORD:-netrun_orchestrator}"
  validate_pg_identifier "$DB_NAME"
  validate_pg_identifier "$DB_USER"
  [[ "$DB_PASSWORD" =~ ^[A-Za-z0-9_@.-]+$ ]] || die "DB_PASSWORD contains unsupported shell/setup characters"

  if ! runuser -u postgres -- psql -tAc "select 1 from pg_roles where rolname='${DB_USER}'" | grep -q 1; then
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "create role ${DB_USER} login password '${DB_PASSWORD}'"
  else
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "alter role ${DB_USER} with login password '${DB_PASSWORD}'"
  fi

  if ! runuser -u postgres -- psql -tAc "select 1 from pg_database where datname='${DB_NAME}'" | grep -q 1; then
    runuser -u postgres -- createdb -O "$DB_USER" "$DB_NAME"
  fi
}

setup_local_redis() {
  log "Configuring local Redis"
  systemctl enable redis-server >/dev/null
  systemctl start redis-server >/dev/null
  if ! redis-cli ping | grep -q PONG; then
    die "redis_ping_failed"
  fi
}

setup_python_env() {
  log "Installing Python dependencies"
  python3 -m venv "$APP_HOME/.venv"
  "$APP_HOME/.venv/bin/python" -m pip install --upgrade pip
  "$APP_HOME/.venv/bin/pip" install -r "$APP_HOME/requirements.txt"
}

run_migrations() {
  log "Running migrations"
  (cd "$APP_HOME" && "$APP_HOME/.venv/bin/python" -m orchestrator.migrate)
}

install_service() {
  log "Installing systemd services"
  install -m 0644 "$APP_HOME/deploy/systemd/netrun-orchestrator.service.template" "$SERVICE_FILE"
  install -m 0644 "$APP_HOME/deploy/systemd/netrun-orchestrator-worker.service.template" "$WORKER_SERVICE_FILE"
  install -m 0644 "$APP_HOME/deploy/systemd/netrun-orchestrator-refill.service.template" "$REFILL_SERVICE_FILE"
  install -m 0644 "$APP_HOME/deploy/systemd/netrun-orchestrator-validation.service.template" "$VALIDATION_SERVICE_FILE"
  install -m 0644 "$APP_HOME/deploy/systemd/netrun-orchestrator-watchdog.service.template" "$WATCHDOG_SERVICE_FILE"
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null
  systemctl enable "$WORKER_SERVICE_NAME" >/dev/null
  systemctl enable "$REFILL_SERVICE_NAME" >/dev/null
  systemctl enable "$VALIDATION_SERVICE_NAME" >/dev/null
  systemctl enable "$WATCHDOG_SERVICE_NAME" >/dev/null
  systemctl restart "$SERVICE_NAME"
  systemctl restart "$WORKER_SERVICE_NAME"
  systemctl restart "$REFILL_SERVICE_NAME"
  systemctl restart "$VALIDATION_SERVICE_NAME"
  systemctl restart "$WATCHDOG_SERVICE_NAME"
}

wait_health() {
  log "Waiting for /health"
  local url="http://127.0.0.1:${ORCHESTRATOR_PORT:-8090}/health"
  local response=""
  for _ in $(seq 1 30); do
    response="$(curl -fsS -H "X-NETRUN-API-KEY: ${ORCHESTRATOR_API_KEY}" "$url" 2>/dev/null || true)"
    if [ -n "$response" ] && printf '%s' "$response" | jq -e '.success == true and .status == "ready"' >/dev/null 2>&1; then
      printf '%s\n' "$response" | jq .
      return 0
    fi
    sleep 1
  done

  systemctl status "$SERVICE_NAME" --no-pager || true
  journalctl -u "$SERVICE_NAME" -n 100 --no-pager || true
  die "health_check_failed"
}

main() {
  copy_repo_to_opt
  install_os_packages
  ensure_env_file
  load_env
  mkdir -p "${JOBS_ROOT:-$APP_HOME/jobs}"
  chmod +x "$APP_HOME/install_orchestrator.sh" "$APP_HOME/scripts/"*.sh
  setup_local_postgres
  setup_local_redis
  setup_python_env
  run_migrations
  install_service
  wait_health

  log "Install complete"
  log "APP_HOME=$APP_HOME"
  log "SERVICE=$SERVICE_NAME"
  log "WORKER_SERVICE=$WORKER_SERVICE_NAME"
  log "REFILL_SERVICE=$REFILL_SERVICE_NAME"
  log "VALIDATION_SERVICE=$VALIDATION_SERVICE_NAME"
  log "WATCHDOG_SERVICE=$WATCHDOG_SERVICE_NAME"
  log "HEALTH=http://127.0.0.1:${ORCHESTRATOR_PORT:-8090}/health"
  log "API key is stored in $APP_HOME/.env"
}

main "$@"
