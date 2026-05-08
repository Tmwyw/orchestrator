"""Tests for orchestrator.dns_pool — filter chain + healthcheck + refresh fallback.

Heavy DB work (upsert) is exercised end-to-end in integration; here we keep
unit tests around the pure-functional pieces: blacklist filter, healthcheck
result handling, feed-unavailable behaviour. The upsert state machine
(consecutive_failures → enabled=false) is covered by an in-memory shim
because reaching for a real Postgres in CI would slow the suite.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from orchestrator import dns_pool
from orchestrator.dns_pool import (
    BLACKLIST_ASNS,
    CandidateResolver,
    parse_and_filter,
    run_dns_pool_refresh,
)


# === parse_and_filter ===


def _entry(**overrides: Any) -> dict[str, Any]:
    base = {
        "ip": "203.112.2.4",
        "asn": 4713,
        "country": "JP",
        "isp": "NTT",
        "uptime": 99.5,
        "latency_ms": 35,
    }
    base.update(overrides)
    return base


def test_blacklist_filters_cloudflare() -> None:
    feed = [_entry(ip="1.1.1.1", asn=13335, isp="Cloudflare")]
    result = parse_and_filter(feed, active_geos={"JP"})
    assert result == []


def test_blacklist_filters_all_listed_asns() -> None:
    # Sanity: every ASN we hardcoded as banned is rejected.
    feed = [_entry(ip="9.9.9.9", asn=asn) for asn in BLACKLIST_ASNS]
    result = parse_and_filter(feed, active_geos={"JP"})
    assert result == []


def test_geo_outside_active_set_skipped() -> None:
    feed = [_entry(country="DE")]
    result = parse_and_filter(feed, active_geos={"JP", "NL"})
    assert result == []


def test_low_uptime_skipped() -> None:
    feed = [_entry(uptime=80.0)]
    result = parse_and_filter(feed, active_geos={"JP"})
    assert result == []


def test_high_latency_skipped() -> None:
    feed = [_entry(latency_ms=250)]
    result = parse_and_filter(feed, active_geos={"JP"})
    assert result == []


def test_ipv6_only_resolver_skipped() -> None:
    feed = [_entry(ip="2001:4860:4860::8888")]
    result = parse_and_filter(feed, active_geos={"JP"})
    assert result == []


def test_happy_path_keeps_isp_resolver() -> None:
    feed = [_entry()]
    result = parse_and_filter(feed, active_geos={"JP"})
    assert len(result) == 1
    candidate = result[0]
    assert isinstance(candidate, CandidateResolver)
    assert candidate.geo_code == "JP"
    assert candidate.ip == "203.112.2.4"
    assert candidate.asn == 4713


def test_alternate_field_names_accepted() -> None:
    """Upstream layouts vary — accept ``address``/``country_code``/``organization``."""
    feed = [
        {
            "address": "203.112.2.5",
            "as_number": 4713,
            "country_code": "JP",
            "organization": "NTT",
        }
    ]
    result = parse_and_filter(feed, active_geos={"JP"})
    assert len(result) == 1
    assert result[0].ip == "203.112.2.5"
    assert result[0].asn == 4713


# === healthcheck wrapper ===


def test_healthcheck_dead_resolver_marks_failure() -> None:
    """Unreachable resolver → (False, None), no exception leaks."""
    fake_resolver_cls = _DummyResolverCls(should_raise=True)

    with patch.dict(
        "sys.modules",
        {"dns": _DummyDnsModule(fake_resolver_cls), "dns.resolver": _DummyDnsResolverModule(fake_resolver_cls)},
    ):
        ok, latency = dns_pool.healthcheck_resolver("198.51.100.1")
    assert ok is False
    assert latency is None


def test_healthcheck_live_resolver_returns_latency() -> None:
    fake_resolver_cls = _DummyResolverCls(should_raise=False)
    with patch.dict(
        "sys.modules",
        {"dns": _DummyDnsModule(fake_resolver_cls), "dns.resolver": _DummyDnsResolverModule(fake_resolver_cls)},
    ):
        ok, latency = dns_pool.healthcheck_resolver("203.112.2.4")
    assert ok is True
    assert latency is not None
    assert latency >= 0


# === run_dns_pool_refresh ===


def test_refresh_uses_existing_rows_when_feed_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feed=None path: the function must not crash; it should fall through
    to existing rows and re-healthcheck them. With no existing rows, returns
    a zero-counter dict.
    """
    monkeypatch.setattr(dns_pool, "fetch_pingproxies_feed", lambda *a, **kw: None)
    monkeypatch.setattr(dns_pool, "_active_geo_codes", lambda: {"JP", "NL"})
    monkeypatch.setattr(dns_pool, "fetch_all", lambda *_args, **_kw: [])

    result = run_dns_pool_refresh()
    assert result["total"] == 0
    assert result["healthy"] == 0
    assert result["countries"] == 2


def test_upsert_state_machine_disable_after_failures() -> None:
    """In-memory simulation of the upsert state machine: third consecutive
    failure flips ``enabled`` to false. Mirrors the SQL CASE in upsert_pool.
    """
    # row state: consecutive_failures, enabled
    state = {"consecutive_failures": 0, "enabled": True}

    def upsert(ok: bool) -> None:
        if ok:
            state["consecutive_failures"] = 0
        else:
            state["consecutive_failures"] += 1
            if state["consecutive_failures"] >= dns_pool.DISABLE_AFTER_FAILURES:
                state["enabled"] = False

    upsert(False)
    assert state["enabled"] is True
    upsert(False)
    assert state["enabled"] is True
    upsert(False)
    assert state["enabled"] is False  # third failure disables
    upsert(True)
    assert state["consecutive_failures"] == 0


# === fakes for dnspython mocking ===


class _DummyAnswer:
    pass


class _DummyResolverCls:
    """Stand-in for ``dns.resolver.Resolver`` used to control resolve()
    outcomes without making real network calls.
    """

    def __init__(self, should_raise: bool) -> None:
        self._should_raise = should_raise

    def __call__(self, configure: bool = True) -> "_DummyResolverInstance":
        return _DummyResolverInstance(self._should_raise)


class _DummyResolverInstance:
    def __init__(self, should_raise: bool) -> None:
        self.nameservers: list[str] = []
        self.timeout: float = 0
        self.lifetime: float = 0
        self._should_raise = should_raise

    def resolve(self, domain: str, rrtype: str) -> list[_DummyAnswer]:
        if self._should_raise:
            raise OSError("simulated unreachable")
        return [_DummyAnswer()]


class _DummyDnsModule:
    def __init__(self, resolver_cls: _DummyResolverCls) -> None:
        self.resolver = _DummyDnsResolverModule(resolver_cls)


class _DummyDnsResolverModule:
    def __init__(self, resolver_cls: _DummyResolverCls) -> None:
        self.Resolver = resolver_cls
