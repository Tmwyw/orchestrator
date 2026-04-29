#!/usr/bin/env bash
set -euo pipefail

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
  systemctl stop "${UNITS[@]}"
  for u in "${UNITS[@]}"; do
    printf '  %-40s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || echo unknown)"
  done
  exit 0
fi

echo "systemd units not installed, stopping screen sessions"
screen -ls 2>/dev/null | grep -q 'netrun-refill'     && screen -S netrun-refill     -X quit && echo "stopped netrun-refill"     || echo "netrun-refill not running"
screen -ls 2>/dev/null | grep -q 'netrun-validation' && screen -S netrun-validation -X quit && echo "stopped netrun-validation" || echo "netrun-validation not running"
screen -ls 2>/dev/null | grep -q 'netrun-watchdog'   && screen -S netrun-watchdog   -X quit && echo "stopped netrun-watchdog"   || echo "netrun-watchdog not running"
