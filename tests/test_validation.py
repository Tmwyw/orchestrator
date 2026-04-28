"""Unit tests for ProxyValidationService — without network."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch


def test_validation_result_dataclass() -> None:
    from orchestrator.validation import ValidationResult

    result = ValidationResult(
        inventory_id=42,
        is_valid=True,
        validation_error=None,
        external_ip="2a01:4f8:c012::1",
        geo_country="DE",
        geo_city="Berlin",
        latency_ms=120,
        ipv6_only=True,
        dns_sanity=True,
    )
    assert result.inventory_id == 42
    assert result.is_valid is True
    assert result.external_ip == "2a01:4f8:c012::1"
    assert result.ipv6_only is True


def test_normalize_ip_valid_ipv4_ipv6() -> None:
    from orchestrator.validation import _normalize_ip

    assert _normalize_ip("203.0.113.5") == "203.0.113.5"
    assert _normalize_ip(" 203.0.113.5 \n") == "203.0.113.5"
    assert _normalize_ip("2a01:4f8:c012::1") == "2a01:4f8:c012::1"
    # Multi-line: take first line only
    assert _normalize_ip("203.0.113.5\nextra") == "203.0.113.5"


def test_normalize_ip_invalid() -> None:
    from orchestrator.validation import _normalize_ip

    assert _normalize_ip("") is None
    assert _normalize_ip("   ") is None
    assert _normalize_ip("not-an-ip") is None
    assert _normalize_ip("999.999.999.999") is None
    assert _normalize_ip("hello world") is None


def test_extract_http_body_with_headers() -> None:
    from orchestrator.validation import _extract_http_body

    payload = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n203.0.113.5\n"
    assert _extract_http_body(payload) == "203.0.113.5"


def test_extract_http_body_without_headers() -> None:
    from orchestrator.validation import _extract_http_body

    assert _extract_http_body(b"raw body no headers") == "raw body no headers"
    assert _extract_http_body(b"") == ""


async def test_validate_inventory_row_invalid_credentials() -> None:
    from orchestrator.validation import ProxyValidationService

    service = ProxyValidationService()
    row: dict[str, Any] = {
        "id": 1,
        "login": "",
        "password": "secret",
        "host": "1.2.3.4",
        "port": 1080,
        "protocol": "socks5",
        "validation_require_ipv6": False,
    }
    result = await service.validate_inventory_row(row)
    assert result.is_valid is False
    assert result.validation_error == "parse_failed"
    assert result.external_ip is None


async def test_validate_inventory_row_ipv6_required_but_v4() -> None:
    from orchestrator.validation import ProxyValidationService

    service = ProxyValidationService()
    row: dict[str, Any] = {
        "id": 7,
        "login": "user",
        "password": "pass",
        "host": "1.2.3.4",
        "port": 1080,
        "protocol": "socks5",
        "validation_require_ipv6": True,
    }
    fake_probe = {
        "external_ip": "203.0.113.5",
        "latency_ms": 80,
        "dns_sanity": True,
    }
    with patch.object(service, "_probe_socks5_proxy", new=AsyncMock(return_value=fake_probe)):
        result = await service.validate_inventory_row(row)
    assert result.is_valid is False
    assert result.validation_error == "not_ipv6_only"
    assert result.external_ip == "203.0.113.5"
    assert result.ipv6_only is False


async def test_validate_inventory_row_success() -> None:
    from orchestrator.validation import ProxyValidationService

    service = ProxyValidationService()
    row: dict[str, Any] = {
        "id": 11,
        "login": "user",
        "password": "pass",
        "host": "node.example.com",
        "port": 1080,
        "protocol": "socks5",
        "validation_require_ipv6": True,
    }
    fake_probe = {
        "external_ip": "2a01:4f8:c012::1",
        "latency_ms": 105,
        "dns_sanity": True,
    }
    with (
        patch.object(service, "_probe_socks5_proxy", new=AsyncMock(return_value=fake_probe)),
        patch.object(service, "_lookup_geo", new=AsyncMock(return_value=("DE", "Frankfurt"))),
    ):
        result = await service.validate_inventory_row(row)
    assert result.is_valid is True
    assert result.validation_error is None
    assert result.external_ip == "2a01:4f8:c012::1"
    assert result.ipv6_only is True
    assert result.geo_country == "DE"
    assert result.geo_city == "Frankfurt"
    assert result.latency_ms == 105


async def test_validate_inventory_row_probe_failure() -> None:
    from orchestrator.validation import ProxyValidationService

    service = ProxyValidationService()
    row: dict[str, Any] = {
        "id": 99,
        "login": "user",
        "password": "pass",
        "host": "node.example.com",
        "port": 1080,
        "protocol": "socks5",
        "validation_require_ipv6": False,
    }
    fake_probe = {"error": "socks5_auth_failed"}
    with patch.object(service, "_probe_socks5_proxy", new=AsyncMock(return_value=fake_probe)):
        result = await service.validate_inventory_row(row)
    assert result.is_valid is False
    assert result.validation_error == "socks5_auth_failed"
    assert result.external_ip is None
