"""Singleton async Redis client for orchestrator."""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from orchestrator.config import get_config

logger = logging.getLogger("netrun-orchestrator-redis")

_client: Redis | None = None


async def get_redis() -> Redis:
    """Return a process-wide Redis client, lazily initialized and ping-checked."""
    global _client
    if _client is None:
        cfg = get_config()
        client = Redis.from_url(cfg.redis_url, decode_responses=True)
        try:
            await client.ping()
        except Exception:
            logger.exception("redis ping failed url=%s", cfg.redis_url)
            await client.aclose()
            raise
        _client = client
    return _client


async def close_redis() -> None:
    """Close the singleton client (used on shutdown / in tests)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
