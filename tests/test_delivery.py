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


# === Wave HTTP.B — http_uri delivery ===


def test_format_http_uri_emits_only_dual_rows() -> None:
    from orchestrator.delivery import format_http_uri

    rows = [
        {"host": "h1", "port": 32000, "http_port": 22000, "login": "u1", "password": "p1"},
        {"host": "h2", "port": 32001, "http_port": None, "login": "u2", "password": "p2"},
        {"host": "h3", "port": 32002, "http_port": 22002, "login": "u3", "password": "p3"},
    ]
    out = format_http_uri(rows)
    lines = out.split("\n")
    # Only the two rows carrying http_port are emitted, on their http port.
    assert lines == ["http://u1:p1@h1:22000", "http://u3:p3@h3:22002"]


def test_format_http_uri_empty_when_no_dual_rows() -> None:
    from orchestrator.delivery import format_http_uri

    rows = [{"host": "h", "port": 1, "http_port": None, "login": "u", "password": "p"}]
    assert format_http_uri(rows) == ""


def test_socks5_uri_unaffected_by_http_port() -> None:
    """SOCKS5_URI still uses the socks port and ignores http_port."""
    from orchestrator.delivery import format_socks5_uri

    rows = [{"host": "h", "port": 32000, "http_port": 22000, "login": "u", "password": "p"}]
    assert format_socks5_uri(rows) == "socks5://u:p@h:32000"


def test_http_uri_dispatch() -> None:
    from orchestrator.delivery import generate_delivery_content
    from orchestrator.schemas import DeliveryFormat

    rows = [{"host": "h", "port": 32000, "http_port": 22000, "login": "u", "password": "p"}]
    content, ctype = generate_delivery_content(rows, DeliveryFormat.HTTP_URI)
    assert content == "http://u:p@h:22000"
    assert ctype == "text/plain"


# === Wave PROXY-FORMAT.A — parametrized template × protocol delivery ===


def _dual_row(
    *, host: str, port: int, http_port: int | None, login: str, password: str
) -> dict:
    return {
        "host": host,
        "port": port,
        "http_port": http_port,
        "login": login,
        "password": password,
    }


def test_format_template_socks5_all_layouts() -> None:
    from orchestrator.delivery import format_template

    rows = [_dual_row(host="h", port=1080, http_port=8080, login="u", password="p")]
    expect = {
        1: "socks5://u:p:h:1080",
        2: "socks5://u:p@h:1080",
        3: "socks5://h:1080@u:p",
        4: "socks5://h:1080:u:p",
    }
    for tmpl, line in expect.items():
        assert format_template(rows, template=tmpl, scheme="socks5", port_field="port") == line


def test_format_template_https_uses_http_port() -> None:
    from orchestrator.delivery import format_template

    rows = [_dual_row(host="h", port=1080, http_port=8080, login="u", password="p")]
    expect = {
        1: "https://u:p:h:8080",
        2: "https://u:p@h:8080",
        3: "https://h:8080@u:p",
        4: "https://h:8080:u:p",
    }
    for tmpl, line in expect.items():
        assert format_template(rows, template=tmpl, scheme="https", port_field="http_port") == line


def test_format_template_https_skips_null_http_port() -> None:
    from orchestrator.delivery import format_template

    rows = [
        _dual_row(host="h1", port=1080, http_port=8080, login="u1", password="p1"),
        _dual_row(host="h2", port=1081, http_port=None, login="u2", password="p2"),
        _dual_row(host="h3", port=1082, http_port=8082, login="u3", password="p3"),
    ]
    out = format_template(rows, template=2, scheme="https", port_field="http_port")
    assert out.split("\n") == ["https://u1:p1@h1:8080", "https://u3:p3@h3:8082"]


def test_format_template_https_empty_when_all_null() -> None:
    from orchestrator.delivery import format_template

    rows = [_dual_row(host="h", port=1080, http_port=None, login="u", password="p")]
    assert format_template(rows, template=1, scheme="https", port_field="http_port") == ""


def test_format_template_socks5_ignores_null_http_port() -> None:
    """socks5 uses the always-present socks port — http_port=None is fine."""
    from orchestrator.delivery import format_template

    rows = [_dual_row(host="h", port=1080, http_port=None, login="u", password="p")]
    assert format_template(rows, template=2, scheme="socks5", port_field="port") == "socks5://u:p@h:1080"


def test_format_template_rejects_invalid_template() -> None:
    import pytest

    from orchestrator.delivery import format_template

    rows = [_dual_row(host="h", port=1, http_port=2, login="u", password="p")]
    with pytest.raises(ValueError):
        format_template(rows, template=5, scheme="socks5", port_field="port")


def test_resolve_protocol() -> None:
    import pytest

    from orchestrator.delivery import resolve_protocol

    assert resolve_protocol("socks5") == ("socks5", "port")
    assert resolve_protocol("https") == ("https", "http_port")
    with pytest.raises(ValueError):
        resolve_protocol("ftp")


def test_generate_template_content() -> None:
    from orchestrator.delivery import generate_template_content

    rows = [_dual_row(host="h", port=1080, http_port=8080, login="u", password="p")]
    content, ctype = generate_template_content(rows, template=3, protocol="https")
    assert content == "https://h:8080@u:p"
    assert ctype == "text/plain"


def test_parse_template_protocol_ok() -> None:
    from orchestrator.delivery import parse_template_protocol

    assert parse_template_protocol("2", "socks5") == (2, "socks5")
    assert parse_template_protocol(4, "https") == (4, "https")


def test_parse_template_protocol_errors() -> None:
    import pytest

    from orchestrator.delivery import parse_template_protocol

    with pytest.raises(ValueError, match="template_and_protocol_required"):
        parse_template_protocol(None, "socks5")
    with pytest.raises(ValueError, match="template_and_protocol_required"):
        parse_template_protocol("1", None)
    with pytest.raises(ValueError, match="invalid_template"):
        parse_template_protocol("0", "socks5")
    with pytest.raises(ValueError, match="invalid_template"):
        parse_template_protocol("9", "socks5")
    with pytest.raises(ValueError, match="invalid_template"):
        parse_template_protocol("abc", "socks5")
    with pytest.raises(ValueError, match="invalid_protocol"):
        parse_template_protocol("1", "ftp")
