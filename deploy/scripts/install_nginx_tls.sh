#!/usr/bin/env bash
# Wave D: install nginx + certbot, render TLS template for the orchestrator,
# obtain a Let's Encrypt cert, and reload nginx.
#
# Pre-conditions:
#   1. DNS A-record for $DOMAIN points at this host (certbot http-01 needs it).
#   2. Ports 80 and 443 are reachable from the public internet.
#   3. Orchestrator is running on 127.0.0.1:$ORCHESTRATOR_PORT (set
#      ORCHESTRATOR_HOST=127.0.0.1 in .env and restart netrun-orchestrator).
#
# Usage:
#   sudo bash deploy/scripts/install_nginx_tls.sh <domain> <email>
#
# Idempotent: re-running upgrades nginx, re-renders the site, runs certbot
# again (no-op if cert is fresh), and reloads.

set -euo pipefail

DOMAIN="${1:?Usage: bash install_nginx_tls.sh <domain> <email>}"
EMAIL="${2:?Usage: bash install_nginx_tls.sh <domain> <email>}"

APP_HOME="${APP_HOME:-/opt/netrun-orchestrator}"
ORCHESTRATOR_PORT="${ORCHESTRATOR_PORT:-8090}"
TEMPLATE="$APP_HOME/deploy/nginx/orchestrator-tls.conf.template"
SITE_FILE="/etc/nginx/sites-available/netrun-orchestrator-tls.conf"
SITE_LINK="/etc/nginx/sites-enabled/netrun-orchestrator-tls.conf"

if [ "$EUID" -ne 0 ]; then
  echo "must run as root" >&2
  exit 1
fi
[ -f "$TEMPLATE" ] || { echo "missing template: $TEMPLATE" >&2; exit 1; }

# 1. Install nginx + certbot (idempotent).
apt-get update
apt-get install -y nginx certbot python3-certbot-nginx

# 2. Render template with domain + port substituted.
sed -e "s|__NGINX_SERVER_NAME__|${DOMAIN}|g" \
    -e "s|__ORCHESTRATOR_PORT__|${ORCHESTRATOR_PORT}|g" \
    "$TEMPLATE" > "$SITE_FILE"
ln -sf "$SITE_FILE" "$SITE_LINK"

# 3. Validate before reload — fails fast on syntax errors.
nginx -t

# 4. Obtain / renew certificate. --redirect tells certbot to enforce 301
# from :80 to :443 on the rendered vhost.
certbot --nginx \
  -d "$DOMAIN" \
  --non-interactive \
  --agree-tos \
  -m "$EMAIL" \
  --redirect

# 5. Reload to pick up the certbot-injected SSL paths.
systemctl reload nginx

echo "DONE."
echo "  vhost         = $SITE_FILE"
echo "  domain        = https://$DOMAIN"
echo "  upstream      = http://127.0.0.1:$ORCHESTRATOR_PORT"
echo ""
echo "Smoke:"
echo "  curl -sI https://$DOMAIN/health    # 401 unauthorized = good (auth wall up)"
echo ""
echo "Reminder: confirm orchestrator binds 127.0.0.1 only —"
echo "  grep ORCHESTRATOR_HOST $APP_HOME/.env  # must be 127.0.0.1, not 0.0.0.0"
