"""Static-content sanity for Wave D ops assets.

Tests in this repo don't run nginx, certbot, cron, or Grafana, so
there's nothing dynamic to assert. What we CAN do is pin the file
contents against accidental edits — the most common failure mode is
"someone removed a line and broke prod six hours later."

Covered:
  - deploy/nginx/orchestrator-tls.conf.template
  - deploy/scripts/install_nginx_tls.sh
  - deploy/scripts/auto_backup.sh
  - deploy/scripts/install_auto_backup.sh
  - deploy/grafana/orchestrator.json (parses + has expected panels)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_NGINX_TLS = _REPO / "deploy" / "nginx" / "orchestrator-tls.conf.template"
_INSTALL_TLS = _REPO / "deploy" / "scripts" / "install_nginx_tls.sh"
_BACKUP = _REPO / "deploy" / "scripts" / "auto_backup.sh"
_INSTALL_BACKUP = _REPO / "deploy" / "scripts" / "install_auto_backup.sh"
_GRAFANA = _REPO / "deploy" / "grafana" / "orchestrator.json"


# === nginx TLS template ===


@pytest.fixture(scope="module")
def nginx_tls_conf() -> str:
    return _NGINX_TLS.read_text(encoding="utf-8")


def test_nginx_tls_template_exists() -> None:
    assert _NGINX_TLS.is_file(), f"missing nginx TLS template: {_NGINX_TLS}"


def test_nginx_tls_redirects_80_to_443(nginx_tls_conf: str) -> None:
    # The :80 server block must permanent-redirect to https.
    assert "listen 80;" in nginx_tls_conf
    assert "return 301 https://$host$request_uri;" in nginx_tls_conf


def test_nginx_tls_listens_on_443_with_modern_protocols(nginx_tls_conf: str) -> None:
    assert "listen 443 ssl http2;" in nginx_tls_conf
    # No SSLv3 / TLSv1.0 / TLSv1.1.
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in nginx_tls_conf


def test_nginx_tls_metrics_is_localhost_only(nginx_tls_conf: str) -> None:
    # /metrics must be inside its own location block, with allow 127.0.0.1
    # and a deny all, so Prometheus on the same host works but the public
    # internet gets 403.
    assert "location /metrics" in nginx_tls_conf
    assert "allow 127.0.0.1;" in nginx_tls_conf
    assert "deny all;" in nginx_tls_conf


def test_nginx_tls_uses_template_substitution_tokens(nginx_tls_conf: str) -> None:
    # The installer relies on these exact tokens — renaming one without
    # updating install_nginx_tls.sh would silently break.
    assert "__NGINX_SERVER_NAME__" in nginx_tls_conf
    assert "__ORCHESTRATOR_PORT__" in nginx_tls_conf


def test_nginx_tls_proxies_to_loopback_upstream(nginx_tls_conf: str) -> None:
    # The point of TLS termination — backend stays on 127.0.0.1.
    assert "server 127.0.0.1:__ORCHESTRATOR_PORT__;" in nginx_tls_conf


# === install_nginx_tls.sh ===


@pytest.fixture(scope="module")
def install_tls() -> str:
    return _INSTALL_TLS.read_text(encoding="utf-8")


def test_install_tls_uses_strict_bash(install_tls: str) -> None:
    assert install_tls.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in install_tls


def test_install_tls_takes_domain_and_email_positional_args(install_tls: str) -> None:
    # ${1:?...} / ${2:?...} so calling without args fails fast with a
    # usage hint instead of running certbot against an empty domain.
    assert 'DOMAIN="${1:?' in install_tls
    assert 'EMAIL="${2:?' in install_tls


def test_install_tls_runs_certbot_nginx_with_redirect(install_tls: str) -> None:
    assert "certbot --nginx" in install_tls
    assert "--redirect" in install_tls
    assert "--non-interactive" in install_tls
    assert "--agree-tos" in install_tls


def test_install_tls_validates_before_reload(install_tls: str) -> None:
    # nginx -t must run before systemctl reload — otherwise a bad render
    # takes the vhost down.
    idx_t = install_tls.find("nginx -t")
    idx_reload = install_tls.find("systemctl reload nginx")
    assert idx_t > 0 and idx_reload > 0
    assert idx_t < idx_reload, "nginx -t must precede reload"


# === auto_backup.sh ===


@pytest.fixture(scope="module")
def backup_sh() -> str:
    return _BACKUP.read_text(encoding="utf-8")


def test_backup_uses_strict_bash(backup_sh: str) -> None:
    assert backup_sh.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in backup_sh


def test_backup_pg_dumps_the_orchestrator_db(backup_sh: str) -> None:
    # We dump as the postgres OS user; default DB is netrun_orchestrator.
    assert "sudo -u postgres pg_dump" in backup_sh
    assert 'DB_NAME="${DB_NAME:-netrun_orchestrator}"' in backup_sh


def test_backup_writes_gzipped_dated_filename(backup_sh: str) -> None:
    # Filename pattern is also what the retention sweep matches —
    # changing one without the other would silently stop GC.
    assert "gzip -9" in backup_sh
    assert "orchestrator_${DATE}.sql.gz" in backup_sh
    assert "'orchestrator_*.sql.gz'" in backup_sh


def test_backup_has_retention_default_30_days(backup_sh: str) -> None:
    assert 'RETENTION_DAYS="${RETENTION_DAYS:-30}"' in backup_sh
    assert '-mtime +"$RETENTION_DAYS"' in backup_sh
    assert "-delete" in backup_sh


# === install_auto_backup.sh ===


@pytest.fixture(scope="module")
def install_backup() -> str:
    return _INSTALL_BACKUP.read_text(encoding="utf-8")


def test_install_backup_writes_cron_d_file(install_backup: str) -> None:
    # /etc/cron.d entries must include an explicit user field. Run at 03:00.
    assert "/etc/cron.d/netrun-backup" in install_backup
    assert "0 3 * * * root" in install_backup


def test_install_backup_installs_executable_to_usr_local_bin(install_backup: str) -> None:
    assert 'install -m 0755 "$SRC" "$BIN"' in install_backup
    assert "/usr/local/bin/netrun-auto-backup.sh" in install_backup


# === Grafana dashboard JSON ===


@pytest.fixture(scope="module")
def grafana_dash() -> dict:
    return json.loads(_GRAFANA.read_text(encoding="utf-8"))


def test_grafana_json_parses() -> None:
    json.loads(_GRAFANA.read_text(encoding="utf-8"))


def test_grafana_dashboard_metadata(grafana_dash: dict) -> None:
    assert grafana_dash["title"] == "NETRUN Orchestrator"
    assert grafana_dash["uid"] == "netrun-orchestrator"
    assert "netrun" in grafana_dash["tags"]


def test_grafana_panels_reference_real_metrics(grafana_dash: dict) -> None:
    # Pin that the panels query metric names actually exposed by
    # orchestrator/metrics.py — typos here mean empty graphs in prod.
    expected_metrics = {
        "netrun_http_requests_total",
        "netrun_http_duration_sec_bucket",
        "netrun_reserve_total",
        "netrun_commit_total",
        "netrun_release_total",
        "netrun_scheduler_run_total",
        "netrun_watchdog_actions_total",
        "netrun_inventory_available",
        "netrun_traffic_accounts_active",
        "netrun_traffic_accounts_depleted",
        "netrun_traffic_poll_lag_sec",
        "netrun_traffic_bytes_total",
    }
    expressions = " ".join(
        target["expr"] for panel in grafana_dash["panels"] for target in panel.get("targets", [])
    )
    missing = sorted(m for m in expected_metrics if m not in expressions)
    assert not missing, f"dashboard missing queries for: {missing}"
