"""Delivery format generators for proxy_inventory rows."""

from __future__ import annotations

import json
from typing import Any

from orchestrator.schemas import DeliveryFormat


def format_socks5_uri(rows: list[dict[str, Any]]) -> str:
    """``socks5://login:password@host:port`` (one per line)."""
    return "\n".join(f"socks5://{r['login']}:{r['password']}@{r['host']}:{r['port']}" for r in rows)


def format_host_port_user_pass(rows: list[dict[str, Any]]) -> str:
    """``host:port:login:password`` (one per line)."""
    return "\n".join(f"{r['host']}:{r['port']}:{r['login']}:{r['password']}" for r in rows)


def format_user_pass_at_host_port(rows: list[dict[str, Any]]) -> str:
    """``login:password@host:port`` (one per line)."""
    return "\n".join(f"{r['login']}:{r['password']}@{r['host']}:{r['port']}" for r in rows)


def format_json(rows: list[dict[str, Any]]) -> str:
    """JSON array of ``{host, port, login, password, expires_at, geo_country}``."""
    items = [
        {
            "host": r["host"],
            "port": int(r["port"]),
            "login": r["login"],
            "password": r["password"],
            "expires_at": r["expires_at"].isoformat() if r.get("expires_at") else None,
            "geo_country": r.get("geo_country"),
        }
        for r in rows
    ]
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


_DISPATCH: dict[DeliveryFormat, tuple[Any, str]] = {
    DeliveryFormat.SOCKS5_URI: (format_socks5_uri, "text/plain"),
    DeliveryFormat.HOST_PORT_USER_PASS: (format_host_port_user_pass, "text/plain"),
    DeliveryFormat.USER_PASS_AT_HOST_PORT: (format_user_pass_at_host_port, "text/plain"),
    DeliveryFormat.JSON: (format_json, "application/json"),
}


def generate_delivery_content(
    rows: list[dict[str, Any]],
    format: DeliveryFormat,
) -> tuple[str, str]:
    """Return (content_string, content_type) for the given format."""
    formatter, content_type = _DISPATCH[format]
    return formatter(rows), content_type
