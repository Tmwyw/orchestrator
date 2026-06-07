"""Wave PERGB-POOL-1 Phase A part 2 — lock the per-USER account resolution.

traffic_accounts is now one pool per user (migration 051). Every order-ref →
traffic_account lookup MUST resolve via the order's OWNER (``o.user_id =
t.user_id``), never via ``t.order_id = o.id`` — otherwise only the canonical
order's ref would hit the pool and the others would 404 (the original bug).

Source-text guard (the SQL runs against a live DB only in CI/integration);
this pins the join so a refactor can't quietly reintroduce per-order lookup.
"""

from __future__ import annotations

from pathlib import Path

_ORCH = Path(__file__).resolve().parent.parent / "orchestrator"
_ADMIN = (_ORCH / "admin.py").read_text(encoding="utf-8")
_PERGB_SVC = (_ORCH / "pergb_service.py").read_text(encoding="utf-8")


def test_set_quota_resolves_account_by_user() -> None:
    # admin _sync_set_quota must join orders→pool by owner, not order_id.
    assert "join orders o on o.user_id = ta.user_id" in _ADMIN
    assert "join orders o on o.id = ta.order_id" not in _ADMIN


def test_get_traffic_and_status_resolve_account_by_user() -> None:
    # pergb_service get_traffic + panel-status both resolve by owner.
    assert _PERGB_SVC.count("on t.user_id = o.user_id") >= 2
    # The legacy per-order join must be gone from these read paths.
    assert "join traffic_accounts t on t.order_id = o.id" not in _PERGB_SVC
