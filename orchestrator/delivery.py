"""Delivery format generators for proxy_inventory rows."""

from __future__ import annotations

import json
from typing import Any

from orchestrator.schemas import DeliveryFormat


def format_socks5_uri(rows: list[dict[str, Any]]) -> str:
    """``socks5://login:password@host:port`` (one per line)."""
    return "\n".join(f"socks5://{r['login']}:{r['password']}@{r['host']}:{r['port']}" for r in rows)


def format_http_uri(rows: list[dict[str, Any]]) -> str:
    """``http://login:password@host:http_port`` (one per line).

    Wave HTTP.B — only rows carrying a non-NULL ``http_port`` (dual
    proxies) are emitted; legacy socks5-only rows (http_port IS NULL) are
    skipped, so an order with no dual proxies yields an empty string. The
    caller (allocator.get_proxies) guards against that case explicitly.
    """
    return "\n".join(
        f"http://{r['login']}:{r['password']}@{r['host']}:{r['http_port']}"
        for r in rows
        if r.get("http_port") is not None
    )


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
    DeliveryFormat.HTTP_URI: (format_http_uri, "text/plain"),
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


# === Wave PROXY-FORMAT.A — parametrized template × protocol delivery ===
#
# The bot lets the buyer pick one of 4 line layouts × 2 protocols at
# DOWNLOAD time (per выгрузка), instead of the order being locked to a
# single legacy DeliveryFormat. The legacy generators above are kept
# untouched for backward compat (the bot still calls ?format= for
# per-piece IPv6 until Phase B lands).

#: Valid template ids — the 4 string layouts the buyer can choose from.
VALID_TEMPLATES: tuple[int, ...] = (1, 2, 3, 4)

#: protocol -> (uri scheme, row field carrying the port for that scheme).
#: socks5 uses the SOCKS port (``port``); https uses the dual HTTP port
#: (``http_port``) — the same column ``format_http_uri`` reads. Rows whose
#: chosen port field is NULL are skipped (legacy socks5-only proxies have
#: no http_port), exactly like the legacy http_uri path.
_PROTOCOL_MAP: dict[str, tuple[str, str]] = {
    "socks5": ("socks5", "port"),
    "https": ("https", "http_port"),
}

#: Valid protocol identifiers (the query-param whitelist).
VALID_PROTOCOLS: tuple[str, ...] = tuple(_PROTOCOL_MAP)


def resolve_protocol(protocol: str) -> tuple[str, str]:
    """Map a protocol id to ``(scheme, port_field)``.

    Raises ``ValueError`` for an unknown protocol (handler maps to 422).
    """
    try:
        return _PROTOCOL_MAP[protocol]
    except KeyError:
        raise ValueError(f"invalid protocol: {protocol!r}") from None


def format_template(
    rows: list[dict[str, Any]],
    *,
    template: int,
    scheme: str,
    port_field: str,
) -> str:
    """Render ``rows`` in one of the 4 layouts (one line per row).

    ``port_field`` selects which port column to emit (``port`` for socks5,
    ``http_port`` for https). A row whose ``port_field`` is NULL/absent is
    skipped — so an https request over legacy socks5-only rows yields an
    empty string (the caller turns that into a 409).

    Layouts (``{scheme}`` is ``socks5`` | ``https``):
      1. ``{scheme}://{login}:{password}:{host}:{port}``
      2. ``{scheme}://{login}:{password}@{host}:{port}``
      3. ``{scheme}://{host}:{port}@{login}:{password}``
      4. ``{scheme}://{host}:{port}:{login}:{password}``
    """
    if template not in VALID_TEMPLATES:
        raise ValueError(f"invalid template: {template!r}")
    lines: list[str] = []
    for r in rows:
        port = r.get(port_field)
        if port is None:
            continue
        login = r["login"]
        password = r["password"]
        host = r["host"]
        if template == 1:
            lines.append(f"{scheme}://{login}:{password}:{host}:{port}")
        elif template == 2:
            lines.append(f"{scheme}://{login}:{password}@{host}:{port}")
        elif template == 3:
            lines.append(f"{scheme}://{host}:{port}@{login}:{password}")
        else:  # template == 4
            lines.append(f"{scheme}://{host}:{port}:{login}:{password}")
    return "\n".join(lines)


def generate_template_content(
    rows: list[dict[str, Any]],
    *,
    template: int,
    protocol: str,
) -> tuple[str, str]:
    """Return ``(content_string, "text/plain")`` for a template × protocol.

    Raises ``ValueError`` on an invalid template or protocol.
    """
    scheme, port_field = resolve_protocol(protocol)
    content = format_template(rows, template=template, scheme=scheme, port_field=port_field)
    return content, "text/plain"


def parse_template_protocol(template: Any, protocol: Any) -> tuple[int, str]:
    """Validate raw ``template`` + ``protocol`` query-param values.

    Both must be supplied together. Returns the normalized
    ``(template_int, protocol_str)`` on success. On failure raises
    ``ValueError`` whose message is a stable error code the handler maps to
    a 422: ``template_and_protocol_required`` / ``invalid_template`` /
    ``invalid_protocol``.
    """
    if template is None or protocol is None:
        raise ValueError("template_and_protocol_required")
    try:
        tmpl = int(template)
    except (TypeError, ValueError):
        raise ValueError("invalid_template") from None
    if tmpl not in VALID_TEMPLATES:
        raise ValueError("invalid_template")
    if protocol not in VALID_PROTOCOLS:
        raise ValueError("invalid_protocol")
    return tmpl, protocol
