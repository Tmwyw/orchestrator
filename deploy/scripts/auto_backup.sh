#!/usr/bin/env bash
# Wave D: nightly pg_dump for the orchestrator DB with local retention.
#
# Layout:
#   /var/backups/netrun/orchestrator_YYYYMMDD_HHMMSS.sql.gz
#
# Retention: keep 30 days of gzipped dumps; older ones are deleted.
#
# Restore:
#   gunzip -c /var/backups/netrun/orchestrator_<DATE>.sql.gz \
#     | sudo -u postgres psql -d netrun_orchestrator
#
# Cron entry installed by install_auto_backup.sh runs at 03:00 daily.
# Errors land in /var/log/netrun-backup.log via cron's stderr redirect.

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/netrun}"
DB_NAME="${DB_NAME:-netrun_orchestrator}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

mkdir -p "$BACKUP_DIR"

DATE="$(date +%Y%m%d_%H%M%S)"
ORCH_FILE="$BACKUP_DIR/orchestrator_${DATE}.sql.gz"

# pg_dump → gzip in a pipeline. set -o pipefail upstream means a failing
# pg_dump aborts the run before the file is renamed.
sudo -u postgres pg_dump --format=plain "$DB_NAME" | gzip -9 > "$ORCH_FILE"

bytes="$(stat -c%s "$ORCH_FILE")"
echo "$(date -Iseconds) backup ok: $ORCH_FILE (${bytes} bytes)"

# Retention sweep — only our own files, never *.gz from other tools.
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'orchestrator_*.sql.gz' \
  -mtime +"$RETENTION_DAYS" -print -delete
