"""Tests for the geo metadata CRUD + display-name flag cache.

Wave PROXY-PARITY-1 Phase A. Endpoint tests mirror test_admin_catalog.py:
TestClient + monkeypatch the ``_*_sync`` helpers (write paths) or
``fetch_all`` (read path), so nothing touches a live DB. The cache tests
exercise ``_geo_flag`` / ``_compute_display_name`` against a stubbed
loader.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "test-api-key")


@pytest.fixture(autouse=True)
def _reset_geo_cache():
    """Keep the process-global flag cache from leaking across tests."""
    from orchestrator import admin_catalog

    admin_catalog.invalidate_geo_cache()
    yield
    admin_catalog.invalidate_geo_cache()


@pytest.fixture
def _no_auth():
    from orchestrator.main import app, require_api_key

    app.dependency_overrides[require_api_key] = lambda: None
    yield
    app.dependency_overrides.pop(require_api_key, None)


# === GET /v1/admin/geos/catalog ===


def test_list_geos_catalog_returns_metadata(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    fake_rows = [
        {
            "code": "US",
            "flag": "🇺🇸",
            "name_ru": "США",
            "name_en": None,
            "sort_order": 1,
            "is_active": True,
            "sku_count": 3,
        },
        {
            "code": "PK",
            "flag": "🌐",
            "name_ru": "Пакистан",
            "name_en": "Pakistan",
            "sort_order": 99,
            "is_active": True,
            "sku_count": 0,  # pre-created, no SKU yet — still listed
        },
    ]

    def fake_fetch_all(query: str, params: Any = None) -> list[dict[str, Any]]:
        assert "FROM geos g" in query
        assert "LEFT JOIN" in query  # geos with 0 SKUs still appear
        return fake_rows

    monkeypatch.setattr("orchestrator.admin_catalog.fetch_all", fake_fetch_all)

    from orchestrator.main import app

    r = TestClient(app).get("/v1/admin/geos/catalog")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert items[0]["code"] == "US"
    assert items[0]["sku_count"] == 3
    assert items[1]["code"] == "PK"
    assert items[1]["sku_count"] == 0


def test_list_geos_catalog_requires_api_key() -> None:
    from orchestrator.main import app

    r = TestClient(app).get("/v1/admin/geos/catalog")
    assert r.status_code == 401


def test_legacy_geos_usage_endpoint_untouched(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    """GET /geos (usage counts from skus) must keep its original shape."""

    def fake_fetch_all(query: str, params: Any = None) -> list[dict[str, Any]]:
        assert "FROM skus" in query
        return [{"geo_code": "US", "sku_count": 2, "active_count": 1}]

    monkeypatch.setattr("orchestrator.admin_catalog.fetch_all", fake_fetch_all)
    from orchestrator.main import app

    r = TestClient(app).get("/v1/admin/geos")
    assert r.status_code == 200
    assert r.json()["items"][0]["geo_code"] == "US"


# === POST /v1/admin/geos ===


def test_create_geo_happy_path(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    captured: dict[str, Any] = {}

    def fake_create(payload: Any) -> dict[str, Any]:
        captured["payload"] = payload
        return {
            "code": payload.code,
            "flag": payload.flag,
            "name_ru": payload.name_ru,
            "name_en": payload.name_en,
            "sort_order": payload.sort_order,
            "is_active": payload.is_active,
        }

    invalidated: list[bool] = []
    monkeypatch.setattr("orchestrator.admin_catalog._create_geo_sync", fake_create)
    monkeypatch.setattr(
        "orchestrator.admin_catalog.invalidate_geo_cache",
        lambda: invalidated.append(True),
    )

    from orchestrator.main import app

    # lowercase code must be normalised to upper by the request model.
    r = TestClient(app).post(
        "/v1/admin/geos",
        json={"code": "pk", "flag": "🇵🇰", "name_ru": "Пакистан"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["code"] == "PK"
    assert body["flag"] == "🇵🇰"
    assert body["sku_count"] == 0
    assert captured["payload"].code == "PK"
    assert invalidated == [True]  # cache dropped on success


def test_create_geo_409_on_duplicate(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._create_geo_sync", lambda _p: "duplicate_code"
    )
    from orchestrator.main import app

    r = TestClient(app).post(
        "/v1/admin/geos", json={"code": "US", "flag": "🇺🇸", "name_ru": "США"}
    )
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_code"


@pytest.mark.parametrize("bad_code", ["P", "P1", "TOOLONGCODE", "U S", "12"])
def test_create_geo_422_on_invalid_code(bad_code: str, _no_auth: None) -> None:
    from orchestrator.main import app

    r = TestClient(app).post(
        "/v1/admin/geos", json={"code": bad_code, "name_ru": "x"}
    )
    assert r.status_code == 422


# === PATCH /v1/admin/geos/{code} ===


def test_patch_geo_happy_path(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    invalidated: list[bool] = []
    monkeypatch.setattr(
        "orchestrator.admin_catalog._update_geo_sync",
        lambda code, payload: {
            "code": code.upper(),
            "flag": "🏴",
            "name_ru": "Тест",
            "name_en": None,
            "sort_order": 5,
            "is_active": True,
        },
    )
    monkeypatch.setattr(
        "orchestrator.admin_catalog.invalidate_geo_cache",
        lambda: invalidated.append(True),
    )
    from orchestrator.main import app

    r = TestClient(app).patch("/v1/admin/geos/US", json={"flag": "🏴"})
    assert r.status_code == 200
    assert r.json()["flag"] == "🏴"
    assert invalidated == [True]


def test_patch_geo_deactivate_always_allowed(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    """is_active=false is a PATCH and is allowed even with SKUs present —
    only physical DELETE is guarded."""
    captured: dict[str, Any] = {}

    def fake_update(code: str, payload: Any) -> dict[str, Any]:
        captured["fields"] = payload.model_dump(exclude_none=True)
        return {
            "code": code,
            "flag": "🇺🇸",
            "name_ru": "США",
            "name_en": None,
            "sort_order": 1,
            "is_active": False,
        }

    monkeypatch.setattr("orchestrator.admin_catalog._update_geo_sync", fake_update)
    from orchestrator.main import app

    r = TestClient(app).patch("/v1/admin/geos/US", json={"is_active": False})
    assert r.status_code == 200
    assert r.json()["is_active"] is False
    assert captured["fields"] == {"is_active": False}  # False is kept, not dropped


def test_patch_geo_404_when_missing(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._update_geo_sync", lambda _c, _p: "geo_not_found"
    )
    from orchestrator.main import app

    r = TestClient(app).patch("/v1/admin/geos/ZZ", json={"flag": "🏴"})
    assert r.status_code == 404
    assert r.json()["error"] == "geo_not_found"


def test_patch_geo_400_when_no_fields(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._update_geo_sync", lambda _c, _p: "no_fields_to_update"
    )
    from orchestrator.main import app

    r = TestClient(app).patch("/v1/admin/geos/US", json={})
    assert r.status_code == 400
    assert r.json()["error"] == "no_fields_to_update"


# === DELETE /v1/admin/geos/{code} ===


def test_delete_geo_happy(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    invalidated: list[bool] = []
    monkeypatch.setattr(
        "orchestrator.admin_catalog._delete_geo_sync", lambda _c: {"code": "PK"}
    )
    monkeypatch.setattr(
        "orchestrator.admin_catalog.invalidate_geo_cache",
        lambda: invalidated.append(True),
    )
    from orchestrator.main import app

    r = TestClient(app).delete("/v1/admin/geos/PK")
    assert r.status_code == 200
    assert r.json() == {"success": True, "deleted_code": "PK"}
    assert invalidated == [True]


def test_delete_geo_409_when_in_use(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._delete_geo_sync",
        lambda _c: ("geo_in_use", {"sku_count": 7}),
    )
    from orchestrator.main import app

    r = TestClient(app).delete("/v1/admin/geos/US")
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "geo_in_use"
    assert body["extra"]["sku_count"] == 7


def test_delete_geo_404_when_missing(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._delete_geo_sync", lambda _c: "geo_not_found"
    )
    from orchestrator.main import app

    r = TestClient(app).delete("/v1/admin/geos/ZZ")
    assert r.status_code == 404
    assert r.json()["error"] == "geo_not_found"


# === _geo_flag / _compute_display_name + cache ===


def test_geo_flag_reads_from_db(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import admin_catalog

    monkeypatch.setattr(admin_catalog, "_load_geo_flags", lambda: {"PK": "🇵🇰"})
    admin_catalog.invalidate_geo_cache()
    assert admin_catalog._geo_flag("PK") == "🇵🇰"
    assert admin_catalog._geo_flag("pk") == "🇵🇰"  # case-insensitive


def test_geo_flag_empty_code_is_globe(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import admin_catalog

    monkeypatch.setattr(admin_catalog, "_load_geo_flags", lambda: {})
    admin_catalog.invalidate_geo_cache()
    assert admin_catalog._geo_flag("") == "🌐"
    assert admin_catalog._geo_flag(None) == "🌐"


def test_geo_flag_unknown_code_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import admin_catalog

    # DB returns nothing for ZZ; not in static _GEO_FLAGS either → 🏳️.
    monkeypatch.setattr(admin_catalog, "_load_geo_flags", lambda: {})
    admin_catalog.invalidate_geo_cache()
    assert admin_catalog._geo_flag("ZZ") == "🏳️"
    # A code only in the static fallback still resolves.
    assert admin_catalog._geo_flag("US") == "🇺🇸"


def test_geo_flag_db_failure_uses_static_fallback_without_poisoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator import admin_catalog

    def boom() -> dict[str, str]:
        raise RuntimeError("DB down")

    monkeypatch.setattr(admin_catalog, "_load_geo_flags", boom)
    admin_catalog.invalidate_geo_cache()
    # Falls back to the static map, and the cache stays None (not poisoned).
    assert admin_catalog._geo_flag("US") == "🇺🇸"
    assert admin_catalog._geo_flag_cache is None


def test_geo_flag_caches_after_first_load(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import admin_catalog

    calls: list[int] = []

    def counting_loader() -> dict[str, str]:
        calls.append(1)
        return {"US": "🅰️"}

    monkeypatch.setattr(admin_catalog, "_load_geo_flags", counting_loader)
    admin_catalog.invalidate_geo_cache()
    assert admin_catalog._geo_flag("US") == "🅰️"
    assert admin_catalog._geo_flag("US") == "🅰️"
    assert len(calls) == 1  # loaded once, then cached
    # Invalidation forces a reload with fresh data.
    monkeypatch.setattr(admin_catalog, "_load_geo_flags", lambda: {"US": "🅱️"})
    admin_catalog.invalidate_geo_cache()
    assert admin_catalog._geo_flag("US") == "🅱️"


def test_compute_display_name_uses_db_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import admin_catalog

    monkeypatch.setattr(admin_catalog, "_load_geo_flags", lambda: {"PK": "🇵🇰"})
    admin_catalog.invalidate_geo_cache()
    name = admin_catalog._compute_display_name(
        kind="ipv6", geo_code="PK", protocol="socks5", duration_days=30
    )
    assert name == "🇵🇰 IPv6 PK SOCKS5 (30d)"
