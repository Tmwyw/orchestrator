#!/usr/bin/env bash
# setup_geoip.sh — install db-ip.com Lite Country offline DB + monthly auto-update.
#
# Run on the ORCHESTRATOR host as root, ONCE:
#
#   sudo bash scripts/setup_geoip.sh
#
# What it does:
#   1. Downloads db-ip.com Lite Country (.mmdb) — no license key, no signup
#   2. Verifies it's a valid MMDB via geoip2 Python module
#   3. Installs a monthly systemd timer to refresh the DB
#      (db-ip.com publishes a new Lite DB on the 1st of each month)
#   4. Restarts netrun-orchestrator-validation.service to pick up new DB
#
# Why db-ip.com instead of MaxMind?
#   MaxMind requires account + license key + sometimes rejects signups from
#   proxy-adjacent businesses. db-ip.com Lite is the same .mmdb format,
#   downloadable directly. Accuracy is on-par with GeoLite2 for country
#   resolution. Library compatibility identical (Python geoip2 reads both).

set -euo pipefail

GEOIP_DIR="/opt/netrun-orchestrator/geoip"
DB_FILE="$GEOIP_DIR/GeoLite2-Country.mmdb"   # path kept for backward compat
TMP_DIR="$(mktemp -d /tmp/dbip.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

log() { printf '\033[1;36m[setup-geoip]\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m[setup-geoip]\033[0m \033[32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[setup-geoip]\033[0m ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root"

# === 1. Install required tools ===
log "1/5 Installing curl, unzip"
apt-get update -qq
apt-get install -y -qq curl unzip ca-certificates
ok "deps installed"

# === 2. Build db-ip.com Lite URL (current month, fallback to previous if 404) ===
log "2/5 Downloading db-ip.com Lite Country DB"
mkdir -p "$GEOIP_DIR"

YEAR_MONTH=$(date -u +"%Y-%m")
PREV_YEAR_MONTH=$(date -u -d "1 month ago" +"%Y-%m" 2>/dev/null || date -v-1m -u +"%Y-%m")

URL_CURRENT="https://download.db-ip.com/free/dbip-country-lite-${YEAR_MONTH}.mmdb.gz"
URL_PREV="https://download.db-ip.com/free/dbip-country-lite-${PREV_YEAR_MONTH}.mmdb.gz"

TARGET_GZ="$TMP_DIR/dbip.mmdb.gz"
if curl -fsSL -o "$TARGET_GZ" "$URL_CURRENT"; then
    log "    fetched current month: $YEAR_MONTH"
elif curl -fsSL -o "$TARGET_GZ" "$URL_PREV"; then
    log "    fetched previous month: $PREV_YEAR_MONTH"
else
    die "could not download db-ip Lite DB from db-ip.com (network issue?)"
fi

# Verify size sanity
DB_GZ_SIZE=$(stat -c %s "$TARGET_GZ" 2>/dev/null || stat -f %z "$TARGET_GZ")
[ "$DB_GZ_SIZE" -gt 100000 ] || die "downloaded file suspiciously small ($DB_GZ_SIZE bytes)"

# Extract + atomic move
gunzip -c "$TARGET_GZ" > "$TMP_DIR/db.mmdb"
mv -f "$TMP_DIR/db.mmdb" "$DB_FILE"
DB_SIZE=$(du -h "$DB_FILE" | cut -f1)
ok "DB installed: $DB_FILE ($DB_SIZE)"

# === 3. Sanity-test the lookup ===
log "3/5 Verifying mmdb lookup works"
VENV_PY="/opt/netrun-orchestrator/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
    "$VENV_PY" - <<EOF || die "geoip2 lookup test failed"
import geoip2.database
with geoip2.database.Reader("$DB_FILE") as r:
    cf = r.country("1.1.1.1").country.iso_code
    print(f"  1.1.1.1 -> {cf} (Cloudflare, any country OK)")
    vu = r.country("2401:c080::1").country.iso_code
    print(f"  2401:c080::1 -> {vu} (Vultr Mumbai, expected IN)")
EOF
    ok "lookup OK"
else
    log "    venv not found, skipping lookup test"
fi

# === 4. Monthly auto-refresh via systemd timer ===
log "4/5 Installing monthly auto-refresh timer"
INSTALL_PATH="$(realpath "${BASH_SOURCE[0]}")"
cat > /etc/systemd/system/netrun-geoip-update.service <<EOF
[Unit]
Description=NETRUN — refresh db-ip.com Lite Country DB
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash $INSTALL_PATH
EOF

cat > /etc/systemd/system/netrun-geoip-update.timer <<'EOF'
[Unit]
Description=NETRUN — monthly db-ip.com Lite Country refresh

[Timer]
# db-ip.com publishes a new free Lite DB on the 1st of every month.
# Refresh on the 5th to give them time to upload.
OnCalendar=*-*-05 03:00:00
Persistent=true
RandomizedDelaySec=3600

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now netrun-geoip-update.timer >/dev/null
ok "timer enabled (next run: 5th of each month at 03:00 UTC ±1h)"

# === 5. Restart validation worker ===
log "5/5 Restarting netrun-orchestrator-validation"
if systemctl is-active --quiet netrun-orchestrator-validation; then
    systemctl restart netrun-orchestrator-validation
    sleep 2
    if systemctl is-active --quiet netrun-orchestrator-validation; then
        ok "validation service active"
    else
        die "validation service failed to restart"
    fi
else
    log "    validation service not active — start manually after deploy"
fi

cat <<DONE

────────────────────────────────────────────────────────────────────────────
\033[1;32m✓ GeoIP DB ready\033[0m

  Source:        db-ip.com Lite Country (free, no license key)
  DB path:       $DB_FILE
  Size:          $DB_SIZE
  Auto-refresh:  netrun-geoip-update.timer (5th of every month, 03:00 UTC)
  Validation:    $(systemctl is-active netrun-orchestrator-validation 2>/dev/null || echo unknown)

Watch invalid count drop in proxy_inventory:
  sudo -u postgres psql netrun_orchestrator -c "
    SELECT n.name,
           COUNT(*) FILTER (WHERE pi.status='available') AS avail,
           COUNT(*) FILTER (WHERE pi.status='invalid')  AS invalid
    FROM nodes n LEFT JOIN proxy_inventory pi ON pi.node_id = n.id
    WHERE n.geo IS NOT NULL GROUP BY n.name;"

────────────────────────────────────────────────────────────────────────────
DONE
