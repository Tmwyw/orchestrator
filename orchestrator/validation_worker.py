"""Validation worker: claims pending_validation batches and applies probe results."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from orchestrator.db import connect
from orchestrator.logging_setup import get_logger
from orchestrator.validation import ProxyValidationService, ValidationResult

logger = get_logger("netrun-orchestrator-validation-worker")

DEFAULT_BATCH_SIZE = 50
DEFAULT_CONCURRENCY = 20
DEFAULT_POLL_INTERVAL_SEC = 5


class ProxyValidationWorker:
    """Async worker: poll → claim batch → probe in parallel → bulk update."""

    def __init__(
        self,
        *,
        worker_id: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        concurrency: int = DEFAULT_CONCURRENCY,
        poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
        validator: ProxyValidationService | None = None,
    ) -> None:
        self.worker_id = worker_id or f"validation-{os.getpid()}"
        self.batch_size = max(1, int(batch_size))
        self.concurrency = max(1, int(concurrency))
        self.poll_interval_sec = max(1, int(poll_interval_sec))
        self.validator = validator or ProxyValidationService()

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        logger.info(
            "validation_worker_started",
            worker_id=self.worker_id,
            batch_size=self.batch_size,
            concurrency=self.concurrency,
        )
        while not stop_event.is_set():
            try:
                processed = await self.run_once()
                if processed == 0:
                    await asyncio.sleep(self.poll_interval_sec)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("validation_loop_error")
                await asyncio.sleep(self.poll_interval_sec)

    async def run_once(self) -> int:
        rows = await asyncio.to_thread(self._sync_claim_batch)
        if not rows:
            return 0

        semaphore = asyncio.Semaphore(self.concurrency)

        async def _validate_with_lock(row: dict[str, Any]) -> ValidationResult:
            async with semaphore:
                try:
                    return await self.validator.validate_inventory_row(row)
                except Exception as exc:
                    logger.exception("validation_probe_exception", inventory_id=row.get("id"))
                    return ValidationResult(
                        inventory_id=int(row["id"]),
                        is_valid=False,
                        validation_error=f"validation_exception:{exc}",
                        external_ip=None,
                        geo_country=None,
                        geo_city=None,
                        latency_ms=None,
                        ipv6_only=None,
                        dns_sanity=None,
                    )

        results = await asyncio.gather(*(_validate_with_lock(r) for r in rows))
        valid = [r for r in results if r.is_valid]
        invalid = [r for r in results if not r.is_valid]
        await asyncio.to_thread(self._sync_mark_results, valid, invalid)
        logger.info(
            "validation_cycle_completed",
            worker_id=self.worker_id,
            claimed=len(rows),
            valid=len(valid),
            invalid=len(invalid),
        )
        return len(rows)

    # === Sync DB helpers (run inside asyncio.to_thread) ===

    def _sync_claim_batch(self) -> list[dict[str, Any]]:
        """Claim a batch of pending_validation rows via FOR UPDATE SKIP LOCKED.

        The lock is released on commit at the end of this method (claim+release).
        The probes themselves run without holding row locks. If a probe crashes,
        another worker will pick the same row up next cycle (double validation
        is acceptable — the bulk UPDATE in :meth:`_sync_mark_results` is guarded
        by ``status = 'pending_validation'`` so the second writer is a no-op).
        """
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select
                  i.id, i.login, i.password, i.host, i.port,
                  s.protocol               as protocol,
                  s.validation_require_ipv6 as validation_require_ipv6
                from proxy_inventory i
                join skus s on s.id = i.sku_id
                where i.status = 'pending_validation'
                order by i.created_at asc
                for update of i skip locked
                limit %s
                """,
                (self.batch_size,),
            )
            return [dict(r) for r in cur.fetchall()]

    def _sync_mark_results(
        self,
        valid: list[ValidationResult],
        invalid: list[ValidationResult],
    ) -> None:
        if not valid and not invalid:
            return
        with connect() as conn, conn.cursor() as cur:
            if valid:
                cur.executemany(
                    """
                    update proxy_inventory
                    set status = 'available',
                        external_ip = %s,
                        geo_country = %s,
                        geo_city = %s,
                        latency_ms = %s,
                        ipv6_only = %s,
                        dns_sanity = %s,
                        validation_error = null,
                        validated_at = now(),
                        updated_at = now()
                    where id = %s
                      and status = 'pending_validation'
                    """,
                    [
                        (
                            r.external_ip,
                            r.geo_country,
                            r.geo_city,
                            r.latency_ms,
                            r.ipv6_only,
                            r.dns_sanity,
                            r.inventory_id,
                        )
                        for r in valid
                    ],
                )
            if invalid:
                cur.executemany(
                    """
                    update proxy_inventory
                    set status = 'invalid',
                        external_ip = %s,
                        latency_ms = %s,
                        ipv6_only = %s,
                        dns_sanity = %s,
                        validation_error = %s,
                        validated_at = now(),
                        updated_at = now()
                    where id = %s
                      and status = 'pending_validation'
                    """,
                    [
                        (
                            r.external_ip,
                            r.latency_ms,
                            r.ipv6_only,
                            r.dns_sanity,
                            r.validation_error or "validation_failed",
                            r.inventory_id,
                        )
                        for r in invalid
                    ],
                )
