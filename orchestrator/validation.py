"""Validation pipeline: probe a proxy's liveness, IP, geo, and IPv6 status."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger("netrun-orchestrator-validation")


@dataclass(slots=True)
class ValidationResult:
    inventory_id: int
    is_valid: bool
    validation_error: str | None
    external_ip: str | None
    geo_country: str | None
    geo_city: str | None
    latency_ms: int | None
    ipv6_only: bool | None
    dns_sanity: bool | None


class ProxyValidationService:
    """Async probe + geo lookup for a single proxy_inventory row."""

    def __init__(
        self,
        *,
        connect_timeout_sec: float = 5.0,
        ip_lookup_timeout_sec: float = 8.0,
        geo_lookup_timeout_sec: float = 6.0,
    ) -> None:
        self.connect_timeout_sec = max(1.0, float(connect_timeout_sec))
        self.ip_lookup_timeout_sec = max(2.0, float(ip_lookup_timeout_sec))
        self.geo_lookup_timeout_sec = max(2.0, float(geo_lookup_timeout_sec))

    async def validate_inventory_row(self, row: dict[str, Any]) -> ValidationResult:
        inventory_id = int(row["id"])
        protocol = str(row.get("protocol") or "socks5").strip().lower()
        require_ipv6 = bool(row.get("validation_require_ipv6"))

        login = str(row.get("login") or "").strip()
        password = str(row.get("password") or "").strip()
        host = str(row.get("host") or "").strip()
        try:
            port = int(row.get("port") or 0)
        except (TypeError, ValueError):
            port = 0

        if not login or not password or not host or port <= 0:
            return ValidationResult(
                inventory_id=inventory_id,
                is_valid=False,
                validation_error="parse_failed",
                external_ip=None,
                geo_country=None,
                geo_city=None,
                latency_ms=None,
                ipv6_only=None,
                dns_sanity=None,
            )

        if protocol == "http":
            probe = await self._probe_http_proxy(host, port, login, password)
        else:
            probe = await self._probe_socks5_proxy(host, port, login, password)

        external_ip = probe.get("external_ip")
        latency_ms = probe.get("latency_ms")
        dns_sanity = bool(probe.get("dns_sanity"))
        error_text = probe.get("error")

        if not external_ip:
            return ValidationResult(
                inventory_id=inventory_id,
                is_valid=False,
                validation_error=str(error_text) if error_text else "external_ip_check_failed",
                external_ip=None,
                geo_country=None,
                geo_city=None,
                latency_ms=int(latency_ms) if isinstance(latency_ms, int) else None,
                ipv6_only=None,
                dns_sanity=dns_sanity,
            )

        external_ip_str = str(external_ip)
        ipv6_only = ":" in external_ip_str and "." not in external_ip_str
        if require_ipv6 and not ipv6_only:
            return ValidationResult(
                inventory_id=inventory_id,
                is_valid=False,
                validation_error="not_ipv6_only",
                external_ip=external_ip_str,
                geo_country=None,
                geo_city=None,
                latency_ms=int(latency_ms) if isinstance(latency_ms, int) else None,
                ipv6_only=ipv6_only,
                dns_sanity=dns_sanity,
            )

        geo_country, geo_city = await self._lookup_geo(external_ip_str)
        return ValidationResult(
            inventory_id=inventory_id,
            is_valid=True,
            validation_error=None,
            external_ip=external_ip_str,
            geo_country=geo_country,
            geo_city=geo_city,
            latency_ms=int(latency_ms) if isinstance(latency_ms, int) else None,
            ipv6_only=ipv6_only,
            dns_sanity=dns_sanity,
        )

    async def _probe_http_proxy(self, host: str, port: int, login: str, password: str) -> dict[str, Any]:
        start = time.perf_counter()
        proxy_url = f"http://{quote(login, safe='')}:{quote(password, safe='')}@{host}:{port}"
        timeout = httpx.Timeout(self.ip_lookup_timeout_sec)
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout, verify=False) as client:
                response = await client.get("https://api64.ipify.org?format=text")
                if response.status_code != 200:
                    return {"error": f"http_probe_status_{response.status_code}"}
                external_ip = _normalize_ip(response.text.strip())
                if not external_ip:
                    return {"error": "http_probe_invalid_ip"}
                return {
                    "external_ip": external_ip,
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                    "dns_sanity": True,
                }
        except Exception as exc:
            return {"error": f"http_probe_failed:{exc}"}

    async def _probe_socks5_proxy(self, host: str, port: int, login: str, password: str) -> dict[str, Any]:
        start = time.perf_counter()
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=self.connect_timeout_sec,
            )
            assert writer is not None  # noqa: S101 — mypy narrowing after open_connection

            methods = [0x00, 0x02]
            writer.write(bytes([0x05, len(methods), *methods]))
            await writer.drain()

            greeting = await asyncio.wait_for(reader.readexactly(2), timeout=self.connect_timeout_sec)
            if greeting[0] != 0x05:
                return {"error": "socks5_invalid_version"}
            method = greeting[1]
            if method == 0xFF:
                return {"error": "socks5_no_supported_auth"}
            if method == 0x02:
                u = login.encode("utf-8")
                p = password.encode("utf-8")
                if len(u) > 255 or len(p) > 255:
                    return {"error": "socks5_credentials_too_long"}
                writer.write(bytes([0x01, len(u)]) + u + bytes([len(p)]) + p)
                await writer.drain()
                auth_resp = await asyncio.wait_for(reader.readexactly(2), timeout=self.connect_timeout_sec)
                if auth_resp[1] != 0x00:
                    return {"error": "socks5_auth_failed"}
            elif method != 0x00:
                return {"error": "socks5_auth_method_unsupported"}

            target_host = b"api64.ipify.org".decode("ascii").encode("idna")
            target_port = 80
            connect_req = (
                bytes([0x05, 0x01, 0x00, 0x03, len(target_host)])
                + target_host
                + target_port.to_bytes(2, "big")
            )
            writer.write(connect_req)
            await writer.drain()

            header = await asyncio.wait_for(reader.readexactly(4), timeout=self.connect_timeout_sec)
            if header[1] != 0x00:
                return {"error": f"socks5_connect_reply_{header[1]}"}
            atyp = header[3]
            if atyp == 0x01:
                await asyncio.wait_for(reader.readexactly(4), timeout=self.connect_timeout_sec)
            elif atyp == 0x03:
                size = await asyncio.wait_for(reader.readexactly(1), timeout=self.connect_timeout_sec)
                await asyncio.wait_for(reader.readexactly(size[0]), timeout=self.connect_timeout_sec)
            elif atyp == 0x04:
                await asyncio.wait_for(reader.readexactly(16), timeout=self.connect_timeout_sec)
            await asyncio.wait_for(reader.readexactly(2), timeout=self.connect_timeout_sec)

            writer.write(b"GET /?format=text HTTP/1.1\r\nHost: api64.ipify.org\r\nConnection: close\r\n\r\n")
            await writer.drain()
            raw = await asyncio.wait_for(_read_to_eof(reader), timeout=self.ip_lookup_timeout_sec)
            body = _extract_http_body(raw)
            external_ip = _normalize_ip(body)
            if not external_ip:
                return {"error": "socks5_external_ip_missing"}

            return {
                "external_ip": external_ip,
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "dns_sanity": True,
            }
        except Exception as exc:
            return {"error": f"socks5_probe_failed:{exc}"}
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

    async def _lookup_geo(self, ip_value: str) -> tuple[str | None, str | None]:
        timeout = httpx.Timeout(self.geo_lookup_timeout_sec)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"https://ipapi.co/{ip_value}/json/")
                if response.status_code != 200:
                    return None, None
                data = response.json()
                if not isinstance(data, dict):
                    return None, None
                country = str(data.get("country_name") or data.get("country") or "").strip() or None
                city = str(data.get("city") or "").strip() or None
                return country, city
        except Exception:
            return None, None


async def _read_to_eof(reader: asyncio.StreamReader, max_size: int = 64 * 1024) -> bytes:
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            break
        parts.append(chunk)
        total += len(chunk)
        if total >= max_size:
            break
    return b"".join(parts)


def _extract_http_body(raw_payload: bytes) -> str:
    if not raw_payload:
        return ""
    text = raw_payload.decode("utf-8", errors="ignore")
    if "\r\n\r\n" in text:
        return text.split("\r\n\r\n", 1)[1].strip()
    return text.strip()


def _normalize_ip(candidate: str) -> str | None:
    text = str(candidate or "").strip()
    if not text:
        return None
    first_line = text.splitlines()[0].strip()
    try:
        parsed = ipaddress.ip_address(first_line)
    except Exception:
        return None
    return str(parsed)
