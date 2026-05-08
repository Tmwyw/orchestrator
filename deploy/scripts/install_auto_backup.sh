#!/usr/bin/env bash
# Wave D: install auto_backup.sh into /usr/local/bin and wire it into cron.
#
# Idempotent: re-running overwrites the binary and the cron file.
#
# Usage (as root):
#   sudo bash deploy/scripts/install_auto_backup.sh

set -euo pipefail

APP_HOME="${APP_HOME:-/opt/netrun-orchestrator}"
SRC="$APP_HOME/deploy/scripts/auto_backup.sh"
BIN="/usr/local/bin/netrun-auto-backup.sh"
CRON_FILE="/etc/cron.d/netrun-backup"
LOG_FILE="/var/log/netrun-backup.log"

if [ "$EUID" -ne 0 ]; then
  echo "must run as root" >&2
  exit 1
fi
[ -f "$SRC" ] || { echo "missing source: $SRC" >&2; exit 1; }

install -m 0755 "$SRC" "$BIN"

# Cron entry: 03:00 daily, run as root, stdout+stderr to LOG_FILE.
# /etc/cron.d files require an explicit user field and a trailing newline.
cat > "$CRON_FILE" <<EOF
# NETRUN orchestrator nightly pg_dump (Wave D).
# Edits will be overwritten by install_auto_backup.sh.
0 3 * * * root $BIN >> $LOG_FILE 2>&1
EOF
chmod 0644 "$CRON_FILE"

# Touch the log so the first run can append without permission grief.
touch "$LOG_FILE"
chmod 0640 "$LOG_FILE"

echo "DONE."
echo "  binary    = $BIN"
echo "  cron      = $CRON_FILE  (03:00 daily)"
echo "  log       = $LOG_FILE"
echo ""
echo "Smoke (run once now to confirm pg_dump permissions):"
echo "  sudo $BIN"
