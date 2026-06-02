"""Unit tests for EgressWatchdogService (Wave WATCHDOG-EGRESS-CHECK).

Two layers:
  * ``_probe_egress`` classification — mock ``subprocess.run``.
  * ``run_once`` orchestration — patch the method boundaries
    (_active_nodes / _pick_probe_proxy / _probe_egress / _inbound_reachable
    / the DB record helpers / reboot_node_internal) so the streak + reboot
    gating logic is exercised without a DB or a real node.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.admin_nodes import NodeRebootError
from orchestrator.egress_watchdog import EgressWatchdogService

_MODULE = "orchestrator.egress_watchdog"


def _cfg(**overrides: Any) -> SimpleNamespace:
    base = {
        "egress_fail_threshold": 3,
        "egress_reboot_cooldown_sec": 1800,
        "egress_probe_url": "https://api64.ipify.org",
        "egress_probe_timeout_sec": 10,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


_PROXY = {"host": "1.2.3.4", "port": 32001, "login": "u", "password": "p"}
_NODE = {
    "id": "node-fr",
    "url": "http://1.2.3.4:8085",
    "api_key": "k",
    "runtime_status": "active",
    "egress_fail_streak": 0,
    "egress_last_reboot_at": None,
}


# ── _probe_egress classification ─────────────────────────────────


def test_probe_egress_success_on_exit_0() -> None:
    svc = EgressWatchdogService()
    with patch(f"{_MODULE}.subprocess.run", return_value=MagicMock(returncode=0)):
        verdict = svc._probe_egress(
            host="h", port=1, login="u", password="p", canary="c", timeout_sec=10
        )
    assert verdict == "ok"


def test_probe_egress_fail_on_socks_error_exit_7() -> None:
    svc = EgressWatchdogService()
    with patch(f"{_MODULE}.subprocess.run", return_value=MagicMock(returncode=7)):
        verdict = svc._probe_egress(
            host="h", port=1, login="u", password="p", canary="c", timeout_sec=10
        )
    assert verdict == "fail"


def test_probe_egress_ambiguous_when_curl_missing() -> None:
    svc = EgressWatchdogService()
    with patch(f"{_MODULE}.subprocess.run", side_effect=FileNotFoundError):
        verdict = svc._probe_egress(
            host="h", port=1, login="u", password="p", canary="c", timeout_sec=10
        )
    assert verdict == "ambiguous"


def test_probe_egress_ambiguous_when_wrapper_times_out() -> None:
    svc = EgressWatchdogService()
    with patch(
        f"{_MODULE}.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="curl", timeout=15),
    ):
        verdict = svc._probe_egress(
            host="h", port=1, login="u", password="p", canary="c", timeout_sec=10
        )
    assert verdict == "ambiguous"


def test_probe_egress_uses_socks5h_and_creds() -> None:
    """The argv must carry socks5h:// (remote DNS) + the proxy creds."""
    svc = EgressWatchdogService()
    captured: dict[str, Any] = {}

    def _fake_run(cmd: list[str], **kw: Any) -> MagicMock:
        captured["cmd"] = cmd
        return MagicMock(returncode=0)

    with patch(f"{_MODULE}.subprocess.run", _fake_run):
        svc._probe_egress(
            host="1.2.3.4", port=32001, login="alice", password="secret",
            canary="https://api64.ipify.org", timeout_sec=10,
        )
    joined = " ".join(captured["cmd"])
    assert "socks5h://alice:secret@1.2.3.4:32001" in joined
    assert "https://api64.ipify.org" in joined


# ── run_once orchestration ───────────────────────────────────────


def _run_once_with(
    *,
    proxy: dict[str, Any] | None,
    verdict: str | None = None,
    fail_streak: int = 0,
    cooldown_elapsed: bool = True,
    inbound: bool = True,
    reboot: AsyncMock | None = None,
    cfg: SimpleNamespace | None = None,
    node: dict[str, Any] | None = None,
) -> tuple[dict[str, int], dict[str, MagicMock]]:
    """Drive run_once over a single node with all boundaries patched.
    Returns (counters, mocks)."""
    svc = EgressWatchdogService()
    reboot = reboot or AsyncMock(return_value={"rebooted": True})
    mocks: dict[str, MagicMock] = {}
    with (
        patch(f"{_MODULE}.get_config", return_value=cfg or _cfg()),
        patch.object(svc, "_active_nodes", return_value=[node or {**_NODE}]),
        patch.object(svc, "_pick_probe_proxy", return_value=proxy),
        patch.object(svc, "_probe_egress", return_value=verdict),
        patch.object(svc, "_record_ok") as rec_ok,
        patch.object(svc, "_record_fail", return_value=fail_streak) as rec_fail,
        patch.object(svc, "_cooldown_elapsed", return_value=cooldown_elapsed),
        patch.object(svc, "_inbound_reachable", return_value=inbound),
        patch.object(svc, "_stamp_reboot_attempt") as stamp,
        patch.object(svc, "_record_reboot_success") as rec_reboot,
        patch(f"{_MODULE}.reboot_node_internal", reboot),
    ):
        counters = svc.run_once()
    mocks.update(
        record_ok=rec_ok,
        record_fail=rec_fail,
        stamp=stamp,
        record_reboot_success=rec_reboot,
        reboot=reboot,
    )
    return counters, mocks


def test_success_sets_ok_and_resets_streak() -> None:
    counters, mocks = _run_once_with(proxy=_PROXY, verdict="ok")
    assert counters["checked_ok"] == 1
    assert counters["checked_failed"] == 0
    mocks["record_ok"].assert_called_once()
    mocks["record_fail"].assert_not_called()
    mocks["reboot"].assert_not_called()


def test_fail_below_threshold_increments_no_reboot() -> None:
    counters, mocks = _run_once_with(proxy=_PROXY, verdict="fail", fail_streak=1)
    assert counters["checked_failed"] == 1
    assert counters["reboots"] == 0
    mocks["record_fail"].assert_called_once()
    mocks["reboot"].assert_not_called()


def test_fail_at_threshold_with_cooldown_and_inbound_reboots() -> None:
    counters, mocks = _run_once_with(
        proxy=_PROXY, verdict="fail", fail_streak=3, cooldown_elapsed=True, inbound=True
    )
    assert counters["reboots"] == 1
    assert counters["reboots_failed"] == 0
    mocks["reboot"].assert_awaited_once()
    mocks["stamp"].assert_called_once()  # cooldown stamped before the call
    mocks["record_reboot_success"].assert_called_once()


def test_no_reboot_when_cooldown_active() -> None:
    counters, mocks = _run_once_with(
        proxy=_PROXY, verdict="fail", fail_streak=5, cooldown_elapsed=False
    )
    assert counters["reboots"] == 0
    mocks["reboot"].assert_not_called()
    mocks["stamp"].assert_not_called()


def test_no_reboot_when_inbound_down() -> None:
    """Fully-dead node (inbound also down) is the Vultr watchdog's job."""
    counters, mocks = _run_once_with(
        proxy=_PROXY, verdict="fail", fail_streak=9, cooldown_elapsed=True, inbound=False
    )
    assert counters["reboots"] == 0
    mocks["reboot"].assert_not_called()


def test_no_probe_proxy_skips_node() -> None:
    counters, mocks = _run_once_with(proxy=None)
    assert counters["no_probe_proxy"] == 1
    assert counters["checked_ok"] == 0 and counters["checked_failed"] == 0
    mocks["record_ok"].assert_not_called()
    mocks["record_fail"].assert_not_called()
    mocks["reboot"].assert_not_called()


def test_ambiguous_not_counted_as_fail() -> None:
    counters, mocks = _run_once_with(proxy=_PROXY, verdict="ambiguous")
    assert counters["ambiguous"] == 1
    assert counters["checked_failed"] == 0
    mocks["record_fail"].assert_not_called()
    mocks["record_ok"].assert_not_called()
    mocks["reboot"].assert_not_called()


def test_reboot_failure_is_counted_and_streak_kept() -> None:
    failing = AsyncMock(side_effect=NodeRebootError("vultr_reboot_failed:502"))
    counters, mocks = _run_once_with(
        proxy=_PROXY, verdict="fail", fail_streak=3, reboot=failing
    )
    assert counters["reboots"] == 0
    assert counters["reboots_failed"] == 1
    mocks["stamp"].assert_called_once()  # still throttled on a failed attempt
    mocks["record_reboot_success"].assert_not_called()
