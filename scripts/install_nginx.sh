#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${APP_HOME:-/opt/netrun-orchestrator}"
[ -f "$APP_HOME/.env" ] || { echo "missing $APP_HOME/.env"; exit 1; }
set -a; . "$APP_HOME/.env"; set +a

NGINX_LISTEN_PORT="${NGINX_LISTEN_PORT:-8091}"
NGINX_SERVER_NAME="${NGINX_SERVER_NAME:-orchestrator.localhost}"
METRICS_ALLOW_NETWORKS="${METRICS_ALLOW_NETWORKS:-}"
ORCHESTRATOR_PORT="${ORCHESTRATOR_PORT:-8090}"
SITE_FILE="/etc/nginx/sites-available/netrun-orchestrator.conf"
SITE_LINK="/etc/nginx/sites-enabled/netrun-orchestrator.conf"
TEMPLATE="$APP_HOME/deploy/nginx/orchestrator.conf.template"

if [ "$EUID" -ne 0 ]; then
  echo "must run as root"; exit 1
fi
[ -f "$TEMPLATE" ] || { echo "missing template: $TEMPLATE"; exit 1; }

# Install nginx if missing (idempotent: apt-get install reports already-installed).
apt-get install -y nginx

# Build ACL block: always include 127.0.0.1, then any operator-supplied CIDRs.
acl_block="        allow 127.0.0.1;"
if [ -n "$METRICS_ALLOW_NETWORKS" ]; then
  IFS=',' read -ra cidrs <<< "$METRICS_ALLOW_NETWORKS"
  for cidr in "${cidrs[@]}"; do
    cidr="${cidr// /}"
    [ -z "$cidr" ] && continue
    acl_block+=$'\n        allow '"$cidr"';'
  done
fi

tmp="$(mktemp)"
sed -e "s|__ORCHESTRATOR_PORT__|${ORCHESTRATOR_PORT}|g" \
    -e "s|__NGINX_LISTEN_PORT__|${NGINX_LISTEN_PORT}|g" \
    -e "s|__NGINX_SERVER_NAME__|${NGINX_SERVER_NAME}|g" \
    "$TEMPLATE" > "$tmp"

# Replace __METRICS_ACL_BLOCK__ with multi-line acl_block.
awk -v block="$acl_block" '
  /__METRICS_ACL_BLOCK__/ { print block; next }
  { print }
' "$tmp" > "$SITE_FILE"
rm -f "$tmp"

# Symlink (or refresh if already present — idempotent).
ln -sf "$SITE_FILE" "$SITE_LINK"

# Validate and reload (fails fast on syntax errors).
nginx -t
systemctl reload nginx

echo "nginx orchestrator site installed/updated"
echo "  listen        = $NGINX_LISTEN_PORT"
echo "  server_name   = $NGINX_SERVER_NAME"
echo "  metrics ACL   = 127.0.0.1${METRICS_ALLOW_NETWORKS:+, $METRICS_ALLOW_NETWORKS}"
echo "  orchestrator  = http://127.0.0.1:$ORCHESTRATOR_PORT (upstream)"
echo ""
echo "WARN: This config is HTTP-only. For TLS, run certbot manually"
echo "      against the configured server_name and update SITE_FILE listen → 443 ssl."
