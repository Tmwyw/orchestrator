"""Egress watchdog (Wave WATCHDOG-EGRESS-CHECK).

Closes the recurrent Vultr abuse-block failure mode: the node-agent on
:8085 stays reachable (so ``/v1/nodes/health`` reports the node "green"),
while OUTBOUND proxy traffic is dead — clients get
``Can't complete SOCKS5 connection``. The France incident (2026-06-02)
needed a manual ``POST /v1/admin/nodes/{id}/reboot`` to recover.

Each cycle, for every active node we:

1. Pick a PROBE proxy from ``proxy_inventory`` — a usable proxy on that
   node. We EXCLUDE ``datacenter_pergb`` (per-GB metered → would burn a
   client's GB) and prefer ``status='available'`` (pool proxy, no client
   attached at all). Per-piece ``reserved``/``sold`` are a fine fallback:
   per-piece clients pay per proxy, not per GB, so a ~1 KB probe costs them
   nothing. A node with no usable per-piece proxy is SKIPPED (egress_ok
   left as-is).
2. Probe outbound internet via ``curl -x socks5h://…`` to a canary URL
   (``socks5h`` → DNS resolves at the PROXY, so the orchestrator's own DNS
   is never the variable). Short timeout.
3. Classify: curl exit 0 → egress OK (we reached the internet); a non-zero
   curl exit (couldn't connect / SOCKS failure / timeout) → egress FAIL;
   anything where the probe itself didn't really run (curl missing, our
   subprocess wrapper timed out, no probe proxy) → AMBIGUOUS, NOT counted
   as a node failure.
4. Persist: success → egress_ok=true, streak=0; fail → egress_ok=false,
   streak += 1; ambiguous → no change.
5. Reboot when streak >= threshold AND the node is still inbound-reachable
   (the egress-specific signature — a fully-dead node is the Vultr
   watchdog's job, not ours) AND the reboot cooldown elapsed. Reboot is the
   internal ``reboot_node_internal`` (no HTTP self-call).

State (egress_fail_streak / egress_last_reboot_at) lives in ``nodes`` so it
survives a service restart.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from orchestrator.admin_nodes import NodeRebootError, reboot_node_internal
from orchestrator.config import get_config
from orchestrator.db import connect
from orchestrator.logging_setup import get_logger
from orchestrator.node_client import check_health

logger = get_logger("netrun-orchestrator-egress-watchdog")

NODE_INBOUND_TIMEOUT_SEC = 5


class EgressWatchdogService:
    """Single-pass outbound-health probe + abuse-block reboot over all
    active nodes."""

    def run_once(self) -> dict[str, int]:
        counters: dict[str, int] = {
            "checked_ok": 0,
            "checked_failed": 0,
            "ambiguous": 0,
            "no_probe_proxy": 0,
            "reboots": 0,
            "reboots_failed": 0,
        }
        cfg = get_config()

        nodes = self._active_nodes()
        for node in nodes:
            self._check_node(node, cfg, counters)
        return counters

    # ── node selection ───────────────────────────────────────────

    @staticmethod
    def _active_nodes() -> list[dict[str, Any]]:
        """Every node not explicitly disabled. ``disabled`` is admin-set
        (out of rotation) — don't probe or reboot those."""
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select id, url, api_key, runtime_status,
                       egress_fail_streak, egress_last_reboot_at
                  from nodes
                 where runtime_status <> 'disabled'
                 order by created_at asc
                """
            )
            return list(cur.fetchall())

    @staticmethod
    def _pick_probe_proxy(node_id: str) -> dict[str, Any] | None:
        """One usable, NON-metered proxy on the node for the probe.

        Excludes ``datacenter_pergb`` (per-GB metered — probing it would
        bill a client's traffic account). Prefers ``available`` (pool, no
        client) over ``reserved``/``sold`` (per-piece, flat-rate → a probe
        costs the client nothing). Returns ``None`` when the node has no
        usable per-piece proxy."""
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select i.host, i.port, i.login, i.password
                  from proxy_inventory i
                  join skus s on s.id = i.sku_id
                 where i.node_id = %s
                   and s.product_kind <> 'datacenter_pergb'
                   and i.status in ('available', 'reserved', 'sold')
                   and coalesce(i.login, '') <> ''
                   and coalesce(i.password, '') <> ''
                 order by (i.status = 'available') desc, i.id asc
                 limit 1
                """,
                (node_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    # ── per-node check ───────────────────────────────────────────

    def _check_node(
        self, node: dict[str, Any], cfg: Any, counters: dict[str, int]
    ) -> None:
        node_id = str(node["id"])
        proxy = self._pick_probe_proxy(node_id)
        if proxy is None:
            counters["no_probe_proxy"] += 1
            logger.info("egress_no_probe_proxy", node_id=node_id)
            return

        verdict = self._probe_egress(
            host=str(proxy["host"]),
            port=int(proxy["port"]),
            login=str(proxy["login"]),
            password=str(proxy["password"]),
            canary=cfg.egress_probe_url,
            timeout_sec=int(cfg.egress_probe_timeout_sec),
        )

        if verdict == "ambiguous":
            counters["ambiguous"] += 1
            logger.info("egress_probe_ambiguous", node_id=node_id)
            return

        if verdict == "ok":
            counters["checked_ok"] += 1
            self._record_ok(node_id)
            return

        # verdict == "fail"
        counters["checked_failed"] += 1
        new_streak = self._record_fail(node_id)
        logger.warning(
            "egress_probe_failed",
            node_id=node_id,
            streak=new_streak,
            threshold=cfg.egress_fail_threshold,
        )
        self._maybe_reboot(node, new_streak, cfg, counters)

    def _maybe_reboot(
        self,
        node: dict[str, Any],
        streak: int,
        cfg: Any,
        counters: dict[str, int],
    ) -> None:
        node_id = str(node["id"])
        if streak < cfg.egress_fail_threshold:
            return
        if not self._cooldown_elapsed(node_id, cfg.egress_reboot_cooldown_sec):
            logger.info("egress_reboot_skipped_cooldown", node_id=node_id)
            return
        # Only reboot the abuse-block signature: inbound agent still up but
        # egress dead. A fully-dead node is the Vultr watchdog's job.
        if not self._inbound_reachable(node):
            logger.info("egress_reboot_skipped_inbound_down", node_id=node_id)
            return

        # Stamp the reboot time BEFORE the call so a persistently-failing
        # reboot is throttled by the cooldown (won't hammer the Vultr API
        # every cycle). The streak is only reset when the reboot succeeds.
        self._stamp_reboot_attempt(node_id)
        try:
            asyncio.run(reboot_node_internal(node_id))
        except NodeRebootError as exc:
            counters["reboots_failed"] += 1
            logger.error("egress_reboot_failed", node_id=node_id, error=exc.detail)
            return
        except Exception as exc:  # pragma: no cover - defensive
            counters["reboots_failed"] += 1
            logger.error("egress_reboot_failed", node_id=node_id, error=str(exc))
            return
        counters["reboots"] += 1
        self._record_reboot_success(node_id)
        logger.warning("egress_node_rebooted", node_id=node_id, streak=streak)

    # ── probe ────────────────────────────────────────────────────

    @staticmethod
    def _probe_egress(
        *,
        host: str,
        port: int,
        login: str,
        password: str,
        canary: str,
        timeout_sec: int,
    ) -> str:
        """Run ``curl`` through the proxy. Returns ``"ok"`` / ``"fail"`` /
        ``"ambiguous"``.

        * curl exit 0 → ``ok`` (we got an HTTP response → egress works;
          even a 4xx/5xx body means the internet was reachable).
        * curl missing / our wrapper timed out → ``ambiguous`` (the probe
          itself didn't run — nothing learned about the node).
        * any other non-zero curl exit → ``fail`` (couldn't connect /
          SOCKS failure / connect timeout — i.e. dead egress, exit 7/28/…).
        """
        proxy_url = f"socks5h://{login}:{password}@{host}:{port}"
        cmd = [
            "curl",
            "-sS",
            "-o",
            "/dev/null",
            "-x",
            proxy_url,
            "--max-time",
            str(timeout_sec),
            canary,
        ]
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec + 5,
            )
        except FileNotFoundError:
            # curl not installed — can't conclude anything about the node.
            logger.warning("egress_probe_curl_missing")
            return "ambiguous"
        except subprocess.TimeoutExpired:
            # Our own wrapper killed curl before --max-time fired — treat as
            # ambiguous rather than blaming the node.
            return "ambiguous"
        if result.returncode == 0:
            return "ok"
        return "fail"

    def _inbound_reachable(self, node: dict[str, Any]) -> bool:
        """Is the node-agent on :8085 responding? (loose check — just that
        ``/health`` returns, mirroring the abuse-block scenario where the
        agent stays up while egress is dead)."""
        try:
            check_health(
                str(node["url"]),
                node.get("api_key"),
                NODE_INBOUND_TIMEOUT_SEC,
            )
        except Exception as exc:
            logger.info(
                "egress_inbound_check_failed",
                node_id=str(node["id"]),
                error=str(exc),
            )
            return False
        return True

    # ── DB state ─────────────────────────────────────────────────

    @staticmethod
    def _record_ok(node_id: str) -> None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "update nodes set egress_ok = true, egress_fail_streak = 0, "
                "egress_checked_at = now(), updated_at = now() where id = %s",
                (node_id,),
            )

    @staticmethod
    def _record_fail(node_id: str) -> int:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "update nodes set egress_ok = false, "
                "egress_fail_streak = egress_fail_streak + 1, "
                "egress_checked_at = now(), updated_at = now() "
                "where id = %s returning egress_fail_streak",
                (node_id,),
            )
            row = cur.fetchone()
            return int(row["egress_fail_streak"]) if row else 0

    @staticmethod
    def _cooldown_elapsed(node_id: str, cooldown_sec: int) -> bool:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select (egress_last_reboot_at is null "
                "or egress_last_reboot_at < now() - (%s || ' seconds')::interval) "
                "as ok from nodes where id = %s",
                (cooldown_sec, node_id),
            )
            row = cur.fetchone()
            return bool(row["ok"]) if row else True

    @staticmethod
    def _stamp_reboot_attempt(node_id: str) -> None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "update nodes set egress_last_reboot_at = now(), updated_at = now() "
                "where id = %s",
                (node_id,),
            )

    @staticmethod
    def _record_reboot_success(node_id: str) -> None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "update nodes set egress_fail_streak = 0, "
                "egress_last_reboot_at = now(), updated_at = now() where id = %s",
                (node_id,),
            )
