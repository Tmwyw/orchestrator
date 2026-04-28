#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${APP_HOME:-/opt/netrun-orchestrator}"
PYTHON_BIN="$APP_HOME/.venv/bin/python"

if ! command -v screen >/dev/null 2>&1; then
  echo "Installing screen..."
  apt-get install -y screen >/dev/null
fi

screen -ls 2>/dev/null | grep -q 'netrun-refill' && screen -S netrun-refill -X quit || true
screen -ls 2>/dev/null | grep -q 'netrun-validation' && screen -S netrun-validation -X quit || true

cd "$APP_HOME"

screen -dmS netrun-refill bash -c "
  cd $APP_HOME
  set -a; . $APP_HOME/.env; set +a
  exec $PYTHON_BIN -m orchestrator.refill_scheduler 2>&1
"

screen -dmS netrun-validation bash -c "
  cd $APP_HOME
  set -a; . $APP_HOME/.env; set +a
  exec $PYTHON_BIN -m orchestrator.validation_scheduler 2>&1
"

sleep 1
echo "Started screen sessions:"
screen -ls | grep netrun || echo "WARNING: no netrun screens visible"

echo ""
echo "Attach with: screen -r netrun-refill / screen -r netrun-validation"
echo "Detach inside screen: Ctrl+A then D"
echo "Stop both: bash scripts/stop_schedulers.sh"
