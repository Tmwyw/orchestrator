#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${APP_HOME:-/opt/netrun-orchestrator}"
PYTHON_BIN="$APP_HOME/.venv/bin/python"

UNITS=(
  netrun-orchestrator-refill
  netrun-orchestrator-validation
  netrun-orchestrator-watchdog
)

has_systemd_units() {
  if ! command -v systemctl >/dev/null 2>&1; then
    return 1
  fi
  for u in "${UNITS[@]}"; do
    if ! systemctl list-unit-files 2>/dev/null | awk '{print $1}' | grep -qx "${u}.service"; then
      return 1
    fi
  done
  return 0
}

if has_systemd_units; then
  echo "Using systemd"
  systemctl restart "${UNITS[@]}"
  echo ""
  echo "Status:"
  for u in "${UNITS[@]}"; do
    printf '  %-40s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || echo unknown)"
  done
  echo ""
  echo "Logs: journalctl -u <unit> -f"
  exit 0
fi

echo "systemd units not installed, falling back to screen"

if ! command -v screen >/dev/null 2>&1; then
  echo "Installing screen..."
  apt-get install -y screen >/dev/null
fi

screen -ls 2>/dev/null | grep -q 'netrun-refill' && screen -S netrun-refill -X quit || true
screen -ls 2>/dev/null | grep -q 'netrun-validation' && screen -S netrun-validation -X quit || true
screen -ls 2>/dev/null | grep -q 'netrun-watchdog' && screen -S netrun-watchdog -X quit || true

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

screen -dmS netrun-watchdog bash -c "
  cd $APP_HOME
  set -a; . $APP_HOME/.env; set +a
  exec $PYTHON_BIN -m orchestrator.watchdog_scheduler 2>&1
"

sleep 1
echo "Started screen sessions:"
screen -ls | grep netrun || echo "WARNING: no netrun screens visible"

echo ""
echo "Attach with: screen -r netrun-{refill,validation,watchdog}"
echo "Detach inside screen: Ctrl+A then D"
echo "Stop: bash scripts/stop_schedulers.sh"
