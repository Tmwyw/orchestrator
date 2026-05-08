"""DNS pool curator: fetch ISP resolvers, healthcheck, upsert into ``dns_pool``.

Per-port DNS randomization (Wave C-DNS): the orchestrator owns the curated
list of ISP resolvers per geo; the node script consumes it via ``--dns-pool``
on each ``generate()``. This module is the curator side.

Pipeline:

    fetch_pingproxies_feed()  ──► parse_and_filter()  ──► healthcheck_resolver()
                                                                  │
                                                                  ▼
                                                          upsert_pool()

Trigger: ``run_dns_pool_refresh()`` from ``dns_pool_scheduler`` (24h cadence
with a randomised offset) or via ``POST /v1/admin/dns_pool/refresh``.

Robustness contract: feed unavailability is logged + skipped; seed rows in
``027_dns_pool.sql`` keep the pool usable until the next successful refresh.
"""

from __future__ import annotations

import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx

from orchestrator.db import connect, fetch_all
from orchestrator.logging_setup import get_logger

logger = get_logger("netrun-orchestrator-dns-pool")

# ASNs that must never enter the pool — datacenter / CDN / public-resolver
# operators. A resolver answering from one of these ASNs would re-create the
# fingerprinting problem (ip=ISP-residential, dns=Cloudflare). Keep in sync
# with the comment in the migration's rationale.
BLACKLIST_ASNS: frozenset[int] = frozenset(
    {
        13335,  # Cloudflare
        15169,  # Google
        19281,  # Quad9
        36692,  # OpenDNS / Cisco Umbrella
        8075,   # Microsoft
        22822,  # Limelight
        20473,  # Vultr (our own host ASN — would defeat randomization)
        14618,  # AWS
        16509,  # Amazon
        32934,  # Facebook
    }
)

# Default raw-content URL for the pingproxies public DNS directory feed.
# Override via ``DNS_POOL_FEED_URL`` if the upstream layout changes.
DEFAULT_FEED_URL = (
    "https://raw.githubusercontent.com/pingproxies/public-dns-directory/main/data/dns.json"
)

# Healthcheck domain — stable, well-resolved everywhere, no geo-load-balancing
# surprises that would skew latency_ms.
HEALTHCHECK_DOMAIN = "cloudflare.com"

# Resolver SLO bounds — tuned for cron-cadence sweeps, not interactive checks.
HEALTHCHECK_TIMEOUT_SEC = 3.0
HEALTHCHECK_LIFETIME_SEC = 5.0
HEALTHCHECK_CONCURRENCY = 20

# Filter thresholds applied to the upstream feed (only when those fields exist).
MIN_UPTIME_PCT = 95.0
MAX_LATENCY_MS = 100

# Upsert behavior: this many sequential failed checks → enabled=false. The
# row stays in the table so a later operator review can spot a flaky ISP.
DISABLE_AFTER_FAILURES = 3


@dataclass(frozen=True)
class CandidateResolver:
    geo_code: str
    ip: str
    asn: int | None
    isp_name: str | None
    city: str | None


def _is_ipv4(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit():
            return False
        n = int(p)
        if n < 0 or n > 255:
            return False
    return True


def fetch_pingproxies_feed(
    url: str | None = None, timeout_sec: float = 30.0
) -> list[dict[str, Any]] | None:
    """Download the upstream resolver directory.

    Returns the parsed JSON array on success, ``None`` on any failure. The
    caller is expected to fall back to seed/existing rows.
    """
    feed_url = url or os.getenv("DNS_POOL_FEED_URL", DEFAULT_FEED_URL)
    try:
        with httpx.Client(timeout=timeout_sec, follow_redirects=True) as client:
            response = client.get(feed_url)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("dns_pool_feed_unavailable", url=feed_url, error=str(exc))
        return None
    if isinstance(data, dict):
        # Some directory layouts wrap the array under a key.
        for key in ("resolvers", "data", "items"):
            inner = data.get(key)
            if isinstance(inner, list):
                return inner
        logger.warning("dns_pool_feed_unexpected_shape", keys=list(data.keys())[:10])
        return None
    if not isinstance(data, list):
        logger.warning("dns_pool_feed_unexpected_shape", type=type(data).__name__)
        return None
    return data


def _active_geo_codes() -> set[str]:
    rows = fetch_all(
        "select distinct upper(geo) as geo from nodes where geo is not null and geo <> ''"
    )
    return {str(r["geo"]).strip().upper() for r in rows if r.get("geo")}


def parse_and_filter(
    feed: list[dict[str, Any]],
    *,
    active_geos: set[str] | None = None,
) -> list[CandidateResolver]:
    """Apply the static filter chain (blacklist ASN, geo, uptime, latency, IPv4)."""
    if active_geos is None:
        active_geos = _active_geo_codes()
    out: list[CandidateResolver] = []
    for entry in feed:
        if not isinstance(entry, dict):
            continue
        ip = str(entry.get("ip") or entry.get("address") or "").strip()
        if not ip or not _is_ipv4(ip):
            continue
        asn_raw = entry.get("asn") or entry.get("as_number")
        asn: int | None
        try:
            asn = int(asn_raw) if asn_raw is not None else None
        except (TypeError, ValueError):
            asn = None
        if asn is not None and asn in BLACKLIST_ASNS:
            continue
        geo = str(entry.get("country") or entry.get("geo_code") or entry.get("country_code") or "").strip().upper()
        if not geo or geo not in active_geos:
            continue
        uptime = entry.get("uptime") or entry.get("uptime_pct") or entry.get("reliability")
        if isinstance(uptime, (int, float)) and float(uptime) < MIN_UPTIME_PCT:
            continue
        latency = entry.get("latency_ms") or entry.get("avg_latency_ms")
        if isinstance(latency, (int, float)) and float(latency) > MAX_LATENCY_MS:
            continue
        out.append(
            CandidateResolver(
                geo_code=geo,
                ip=ip,
                asn=asn,
                isp_name=str(entry.get("isp") or entry.get("isp_name") or entry.get("organization") or "").strip() or None,
                city=str(entry.get("city") or "").strip() or None,
            )
        )
    return out


def healthcheck_resolver(ip: str) -> tuple[bool, int | None]:
    """Resolve ``HEALTHCHECK_DOMAIN`` via ``ip``. Returns (ok, latency_ms)."""
    try:
        import dns.resolver  # type: ignore[import-not-found]
    except ImportError:
        logger.error("dns_pool_dnspython_missing")
        return False, None

    r = dns.resolver.Resolver(configure=False)
    r.nameservers = [ip]
    r.timeout = HEALTHCHECK_TIMEOUT_SEC
    r.lifetime = HEALTHCHECK_LIFETIME_SEC
    start = time.monotonic()
    try:
        r.resolve(HEALTHCHECK_DOMAIN, "A")
    except Exception:
        return False, None
    latency_ms = int((time.monotonic() - start) * 1000)
    return True, latency_ms


def _check_one(candidate: CandidateResolver) -> tuple[CandidateResolver, bool, int | None]:
    ok, latency = healthcheck_resolver(candidate.ip)
    return candidate, ok, latency


def upsert_pool(
    resolvers: list[CandidateResolver],
    health_results: dict[str, tuple[bool, int | None]],
) -> dict[str, int]:
    """Upsert health results into ``dns_pool``.

    For each resolver: increment ``consecutive_failures`` on failure, reset to
    0 on success; set ``enabled=false`` once failures crosses
    ``DISABLE_AFTER_FAILURES``.
    """
    inserted = updated = disabled = healthy = failed = 0
    with connect() as conn, conn.cursor() as cur:
        for candidate in resolvers:
            ok, latency_ms = health_results.get(candidate.ip, (False, None))
            if ok:
                healthy += 1
            else:
                failed += 1
            cur.execute(
                """
                insert into dns_pool (
                    geo_code, ip, asn, isp_name, city,
                    last_check_at, last_check_ok, latency_ms,
                    consecutive_failures, enabled
                )
                values (%s, %s, %s, %s, %s, now(), %s, %s, %s, true)
                on conflict (geo_code, ip) do update set
                    asn = coalesce(excluded.asn, dns_pool.asn),
                    isp_name = coalesce(excluded.isp_name, dns_pool.isp_name),
                    city = coalesce(excluded.city, dns_pool.city),
                    last_check_at = excluded.last_check_at,
                    last_check_ok = excluded.last_check_ok,
                    latency_ms = case when excluded.last_check_ok then excluded.latency_ms else dns_pool.latency_ms end,
                    consecutive_failures = case
                        when excluded.last_check_ok then 0
                        else dns_pool.consecutive_failures + 1
                    end,
                    enabled = case
                        when excluded.last_check_ok then dns_pool.enabled
                        when dns_pool.consecutive_failures + 1 >= %s then false
                        else dns_pool.enabled
                    end
                returning (xmax = 0) as inserted, enabled, consecutive_failures
                """,
                (
                    candidate.geo_code,
                    candidate.ip,
                    candidate.asn,
                    candidate.isp_name,
                    candidate.city,
                    ok,
                    latency_ms if ok else None,
                    0 if ok else 1,
                    DISABLE_AFTER_FAILURES,
                ),
            )
            row = cur.fetchone() or {}
            if row.get("inserted"):
                inserted += 1
            else:
                updated += 1
            if not row.get("enabled", True):
                disabled += 1
    return {
        "inserted": inserted,
        "updated": updated,
        "healthy": healthy,
        "failed": failed,
        "disabled": disabled,
    }


def run_dns_pool_refresh(
    *,
    feed_url: str | None = None,
    feed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Full curate cycle: fetch → parse → healthcheck → upsert.

    ``feed`` overrides the network fetch (used by tests). When the feed is
    unavailable, healthcheck the existing rows so stale ones get marked.
    """
    raw_feed = feed if feed is not None else fetch_pingproxies_feed(feed_url)

    active_geos = _active_geo_codes()
    if raw_feed is None:
        logger.info("dns_pool_feed_skipped_using_existing_rows")
        existing = fetch_all(
            "select geo_code, ip, asn, isp_name, city from dns_pool where enabled = true"
        )
        candidates = [
            CandidateResolver(
                geo_code=str(r["geo_code"]).upper(),
                ip=str(r["ip"]),
                asn=r.get("asn"),
                isp_name=r.get("isp_name"),
                city=r.get("city"),
            )
            for r in existing
            if str(r["geo_code"]).upper() in active_geos
        ]
    else:
        candidates = parse_and_filter(raw_feed, active_geos=active_geos)

    if not candidates:
        logger.warning("dns_pool_no_candidates", active_geos=sorted(active_geos))
        return {"countries": len(active_geos), "total": 0, "healthy": 0, "by_geo": {}}

    health_results: dict[str, tuple[bool, int | None]] = {}
    with ThreadPoolExecutor(max_workers=HEALTHCHECK_CONCURRENCY) as pool:
        for candidate, ok, latency in pool.map(_check_one, candidates):
            health_results[candidate.ip] = (ok, latency)

    counters = upsert_pool(candidates, health_results)
    healthy_by_geo: dict[str, int] = {}
    for c in candidates:
        ok, _ = health_results.get(c.ip, (False, None))
        if ok:
            healthy_by_geo[c.geo_code] = healthy_by_geo.get(c.geo_code, 0) + 1

    logger.info(
        "dns_pool_refreshed",
        countries=len(active_geos),
        total=len(candidates),
        healthy=counters["healthy"],
        failed=counters["failed"],
        inserted=counters["inserted"],
        updated=counters["updated"],
        disabled=counters["disabled"],
        by_geo=healthy_by_geo,
    )
    return {
        "countries": len(active_geos),
        "total": len(candidates),
        "healthy": counters["healthy"],
        "failed": counters["failed"],
        "inserted": counters["inserted"],
        "updated": counters["updated"],
        "disabled": counters["disabled"],
        "by_geo": healthy_by_geo,
    }


def select_pool_for_geo(geo_code: str, limit: int = 10) -> list[str]:
    """Top-N healthy resolvers for ``geo_code``, ordered by latency.

    Returns a list of IP strings (possibly empty). Callers fall back to
    legacy DNS selection when the result is too small.
    """
    rows = fetch_all(
        """
        select ip
        from dns_pool
        where geo_code = %s
          and enabled = true
          and last_check_ok = true
        order by coalesce(latency_ms, 9999) asc, ip asc
        limit %s
        """,
        (geo_code.upper(), int(limit)),
    )
    return [str(r["ip"]) for r in rows if r.get("ip")]


def jittered_initial_delay_sec(max_offset_sec: int = 3600) -> int:
    """Random delay before the first cron tick — avoids waking up colocated
    schedulers on the same minute. Module-level helper so the scheduler can
    use it without re-importing ``random``.
    """
    return random.randint(0, max_offset_sec)
