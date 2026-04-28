"""Smoke tests: module imports and static contracts."""

from __future__ import annotations


def test_orchestrator_modules_importable() -> None:
    """All orchestrator modules import without side effects."""
    from orchestrator import (
        config,
        db,
        jobs,
        main,
        migrate,
        node_client,
        server,
        worker,
    )

    assert config.get_config is not None
    assert hasattr(main, "app")
    assert callable(server.main)
    assert callable(worker.run_loop)
    assert callable(migrate.run_migrations)
    assert callable(node_client.check_health)
    assert callable(jobs.select_node)
    assert hasattr(db, "connect")


def test_shared_contracts_production_profile() -> None:
    """PRODUCTION_PROFILE locks the IPv6 + Android contract."""
    from shared.contracts import PRODUCTION_PROFILE

    assert PRODUCTION_PROFILE["ipv6_policy"] == "ipv6_only"
    assert PRODUCTION_PROFILE["network_profile"] == "high_compatibility"
    assert PRODUCTION_PROFILE["fingerprint_profile_version"] == "v2_android_ipv6_only_dns_custom"
    assert PRODUCTION_PROFILE["intended_client_os_profile"] == "android_mobile"
    assert PRODUCTION_PROFILE["client_os_profile_enforcement"] == "not_controlled_by_proxy"


def test_shared_contracts_forbidden_job_fields() -> None:
    """FORBIDDEN_JOB_FIELDS rejects all critical contract overrides."""
    from shared.contracts import FORBIDDEN_JOB_FIELDS

    must_be_forbidden = {
        "ipv6Policy",
        "ipv6_policy",
        "fingerprintProfileVersion",
        "fingerprint_profile_version",
        "generatorScript",
        "startPort",
        "start_port",
        "networkProfile",
        "proxyType",
    }
    missing = must_be_forbidden - FORBIDDEN_JOB_FIELDS
    assert not missing, f"missing forbidden fields: {missing}"


def test_main_allowed_products() -> None:
    """ALLOWED_PRODUCTS is exactly two entries: android_ipv6_only and smoke."""
    from orchestrator.main import ALLOWED_PRODUCTS

    assert {"android_ipv6_only", "smoke"} == ALLOWED_PRODUCTS
