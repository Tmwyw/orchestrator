"""Unit tests for dualstack profile path (O1+O2 wave).

Covers:
- DUALSTACK_PROFILE shape and the inviolable inheritance from PRODUCTION_PROFILE
- profile_for_sku() switching on product_kind
- refill._PRODUCT_BY_KIND dualstack mapping
- RefillService._build_refill_payload selects the right profile per SKU
- node_client.generate() builds payload from per-job profile (default + dualstack)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


def test_dualstack_profile_inherits_production_fields_except_ipv6_policy() -> None:
    from shared.contracts import DUALSTACK_PROFILE, PRODUCTION_PROFILE

    assert DUALSTACK_PROFILE["ipv6_policy"] == "strict_dual_stack"
    for key in (
        "network_profile",
        "fingerprint_profile_version",
        "intended_client_os_profile",
        "client_os_profile_enforcement",
        "actual_client_profile",
        "effective_client_os_profile",
    ):
        assert DUALSTACK_PROFILE[key] == PRODUCTION_PROFILE[key], key


def test_production_profile_unchanged_invariant() -> None:
    from shared.contracts import PRODUCTION_PROFILE

    assert PRODUCTION_PROFILE["ipv6_policy"] == "ipv6_only"
    assert PRODUCTION_PROFILE["network_profile"] == "high_compatibility"
    assert PRODUCTION_PROFILE["fingerprint_profile_version"] == "v2_android_ipv6_only_dns_custom"


def test_profile_for_sku_dualstack() -> None:
    from shared.contracts import DUALSTACK_PROFILE, profile_for_sku

    assert profile_for_sku({"product_kind": "dualstack"}) is DUALSTACK_PROFILE


def test_profile_for_sku_ipv6_returns_production_profile() -> None:
    from shared.contracts import PRODUCTION_PROFILE, profile_for_sku

    assert profile_for_sku({"product_kind": "ipv6"}) is PRODUCTION_PROFILE
    assert profile_for_sku({"product_kind": "datacenter_pergb"}) is PRODUCTION_PROFILE
    assert profile_for_sku({}) is PRODUCTION_PROFILE
    assert profile_for_sku({"product_kind": None}) is PRODUCTION_PROFILE


def test_refill_product_by_kind_includes_dualstack() -> None:
    from orchestrator.refill import _PRODUCT_BY_KIND

    assert _PRODUCT_BY_KIND["dualstack"] == "dualstack_ipv6"
    # Existing entries must remain.
    assert _PRODUCT_BY_KIND["ipv6"] == "android_ipv6_only"
    assert _PRODUCT_BY_KIND["datacenter_pergb"] == "datacenter_pergb"


def _sku(product_kind: str) -> dict[str, Any]:
    return {
        "id": 1,
        "code": f"{product_kind}-us",
        "product_kind": product_kind,
        "protocol": "socks5",
        "geo_code": "US",
        "validation_require_ipv6": True,
    }


def test_build_refill_payload_dualstack_profile() -> None:
    from orchestrator.refill import RefillService

    payload = RefillService()._build_refill_payload(sku=_sku("dualstack"), count=10)
    assert payload["profile"]["ipv6_policy"] == "strict_dual_stack"
    assert payload["profile"]["fingerprint_profile_version"] == "v2_android_ipv6_only_dns_custom"


def test_build_refill_payload_ipv6_profile_unchanged() -> None:
    from orchestrator.refill import RefillService
    from shared.contracts import PRODUCTION_PROFILE

    payload = RefillService()._build_refill_payload(sku=_sku("ipv6"), count=10)
    assert payload["profile"] == PRODUCTION_PROFILE


def _fake_httpx_post_capturing(captured: dict[str, Any]) -> Any:
    """Stand in for httpx.Client(): captures the POST payload, returns a 200."""

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"success": True, "status": "ready", "items": []}

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, endpoint: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
            captured["endpoint"] = endpoint
            captured["json"] = json
            captured["headers"] = headers
            return _Resp()

    return _Client


def test_node_client_generate_default_profile_is_production() -> None:
    from orchestrator import node_client
    from shared.contracts import PRODUCTION_PROFILE

    captured: dict[str, Any] = {}
    with patch.object(node_client.httpx, "Client", _fake_httpx_post_capturing(captured)):
        node_client.generate(
            url="http://node",
            api_key="k",
            job_id="j1",
            count=5,
            start_port=32000,
            timeout_sec=30,
        )
    body = captured["json"]
    assert body["ipv6Policy"] == PRODUCTION_PROFILE["ipv6_policy"] == "ipv6_only"
    assert body["networkProfile"] == PRODUCTION_PROFILE["network_profile"]
    assert body["fingerprintProfileVersion"] == PRODUCTION_PROFILE["fingerprint_profile_version"]
    assert body["intendedClientOsProfile"] == PRODUCTION_PROFILE["intended_client_os_profile"]
    assert body["clientOsProfileEnforcement"] == PRODUCTION_PROFILE["client_os_profile_enforcement"]
    assert body["actualClientProfile"] == PRODUCTION_PROFILE["actual_client_profile"]
    assert body["effectiveClientOsProfile"] == PRODUCTION_PROFILE["effective_client_os_profile"]
    assert body["proxyType"] == "socks5"


def test_node_client_generate_dualstack_profile_passthrough() -> None:
    from orchestrator import node_client
    from shared.contracts import DUALSTACK_PROFILE

    captured: dict[str, Any] = {}
    with patch.object(node_client.httpx, "Client", _fake_httpx_post_capturing(captured)):
        node_client.generate(
            url="http://node",
            api_key="k",
            job_id="j2",
            count=3,
            start_port=32100,
            timeout_sec=30,
            profile=DUALSTACK_PROFILE,
        )
    body = captured["json"]
    assert body["ipv6Policy"] == "strict_dual_stack"
    # Remaining fields keep the production fingerprint contract.
    assert body["fingerprintProfileVersion"] == DUALSTACK_PROFILE["fingerprint_profile_version"]
    assert body["networkProfile"] == DUALSTACK_PROFILE["network_profile"]


def test_refill_insert_job_writes_dualstack_profile_to_db() -> None:
    """End-to-end (mocked) check: dualstack SKU → jobs.profile JSONB is DUALSTACK_PROFILE."""
    from orchestrator.refill import RefillService
    from shared.contracts import DUALSTACK_PROFILE

    service = RefillService()
    fake_cursor = MagicMock()
    fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_conn = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    service._insert_refill_job(
        fake_conn,
        job_id="j-ds-1",
        sku_id=1,
        node_id="n1",
        count=10,
        priority=10,
        product="dualstack_ipv6",
        payload={"x": 1},
        sku=_sku("dualstack"),
    )
    args = fake_cursor.execute.call_args.args[1]
    # Profile is the 8th positional bind (index 7) — see _insert_refill_job SQL.
    profile_jsonb = args[7]
    assert profile_jsonb.obj == DUALSTACK_PROFILE


def test_refill_insert_job_writes_production_profile_for_ipv6() -> None:
    from orchestrator.refill import RefillService
    from shared.contracts import PRODUCTION_PROFILE

    service = RefillService()
    fake_cursor = MagicMock()
    fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_conn = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    service._insert_refill_job(
        fake_conn,
        job_id="j-v6-1",
        sku_id=2,
        node_id="n1",
        count=10,
        priority=10,
        product="android_ipv6_only",
        payload={"x": 1},
        sku=_sku("ipv6"),
    )
    args = fake_cursor.execute.call_args.args[1]
    profile_jsonb = args[7]
    assert profile_jsonb.obj == PRODUCTION_PROFILE
