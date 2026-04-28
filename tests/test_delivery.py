"""Unit tests for delivery format generators."""

from __future__ import annotations

import json
from datetime import datetime, timezone


def _row(
    *,
    host: str,
    port: int,
    login: str,
    password: str,
    expires_at: datetime | None = None,
    geo_country: str | None = None,
) -> dict:
    return {
        "host": host,
        "port": port,
        "login": login,
        "password": password,
        "expires_at": expires_at,
        "geo_country": geo_country,
    }


def test_format_socks5_uri() -> None:
    from orchestrator.delivery import format_socks5_uri

    rows = [
        _row(host="h1.example", port=1080, login="u1", password="p1"),
        _row(host="h2.example", port=1081, login="u2", password="p2"),
    ]
    output = format_socks5_uri(rows)
    assert output == "socks5://u1:p1@h1.example:1080\nsocks5://u2:p2@h2.example:1081"


def test_format_host_port_user_pass() -> None:
    from orchestrator.delivery import format_host_port_user_pass

    rows = [
        _row(host="h1.example", port=1080, login="u1", password="p1"),
        _row(host="h2.example", port=1081, login="u2", password="p2"),
    ]
    assert format_host_port_user_pass(rows) == "h1.example:1080:u1:p1\nh2.example:1081:u2:p2"


def test_format_user_pass_at_host_port() -> None:
    from orchestrator.delivery import format_user_pass_at_host_port

    rows = [
        _row(host="h1.example", port=1080, login="u1", password="p1"),
        _row(host="h2.example", port=1081, login="u2", password="p2"),
    ]
    assert format_user_pass_at_host_port(rows) == "u1:p1@h1.example:1080\nu2:p2@h2.example:1081"


def test_format_json_includes_expires_at_iso() -> None:
    from orchestrator.delivery import format_json

    expires = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _row(
            host="h1.example",
            port=1080,
            login="u1",
            password="p1",
            expires_at=expires,
            geo_country="DE",
        ),
    ]
    parsed = json.loads(format_json(rows))
    assert isinstance(parsed, list) and len(parsed) == 1
    item = parsed[0]
    assert item["host"] == "h1.example"
    assert item["port"] == 1080
    assert item["login"] == "u1"
    assert item["password"] == "p1"
    assert item["geo_country"] == "DE"
    assert item["expires_at"] == "2026-05-28T12:00:00+00:00"


def test_format_json_handles_null_geo_and_expires() -> None:
    from orchestrator.delivery import format_json

    rows = [
        _row(host="h1.example", port=1080, login="u1", password="p1"),
    ]
    parsed = json.loads(format_json(rows))
    assert parsed[0]["expires_at"] is None
    assert parsed[0]["geo_country"] is None


def test_generate_delivery_content_dispatch() -> None:
    from orchestrator.delivery import generate_delivery_content
    from orchestrator.schemas import DeliveryFormat

    rows = [_row(host="h", port=1, login="u", password="p")]
    content, ctype = generate_delivery_content(rows, DeliveryFormat.SOCKS5_URI)
    assert "socks5://u:p@h:1" in content
    assert ctype == "text/plain"

    content_json, ctype_json = generate_delivery_content(rows, DeliveryFormat.JSON)
    assert json.loads(content_json) == [
        {"host": "h", "port": 1, "login": "u", "password": "p", "expires_at": None, "geo_country": None}
    ]
    assert ctype_json == "application/json"
