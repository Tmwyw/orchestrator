# Wave PROXY-FORMAT — Phase A (orchestrator)

Status: **DONE** (branch `wave/proxy-format-a`, not merged/pushed). Bot side = Phase B (separate).

## Goal
Let the bot request proxy delivery in **4 line templates × 2 protocols** (socks5 | https),
chosen by the buyer per download (на выгрузке) — for both per-piece IPv6 orders and pergb batches.

## Templates (`{scheme}` = `socks5` | `https`)
1. `{scheme}://{login}:{password}:{host}:{port}`
2. `{scheme}://{login}:{password}@{host}:{port}`
3. `{scheme}://{host}:{port}@{login}:{password}`
4. `{scheme}://{host}:{port}:{login}:{password}`

Protocol → port column: `socks5` uses `port`, `https` uses `http_port` (the dual HTTP port).
A row whose chosen port is NULL is skipped (socks5-only proxies have no `http_port`) — exactly
like the legacy `http_uri` path. An https request with zero usable rows → 409
`https_not_available_for_order`.

## API contract (additions; legacy paths untouched)
- **IPv6 order:** `GET /v1/orders/{order_ref}/proxies?template={1|2|3|4}&protocol={socks5|https}`
  → `text/plain`, header `X-Line-Count`. Legacy `?format=` still works when template/protocol absent.
  No `delivery_files` cache / format-lock on this path — re-download in any template is allowed.
- **pergb batch:** `GET /v1/pergb/{order_ref}/batches/{batch_id}/proxies?template=&protocol=`
  → `text/plain`. Legacy raw `/batches/{batch_id}/ports` (JSON) kept.
- Errors: 422 `template_and_protocol_required` / `invalid_template` / `invalid_protocol`;
  404 `order_not_found` (order) / `batch_not_found` (pergb); 409 `order_not_committed`,
  `inventory_empty`, `https_not_available_for_order`.

## Code
- `orchestrator/delivery.py` — `format_template`, `generate_template_content`, `resolve_protocol`,
  `parse_template_protocol`, `VALID_TEMPLATES`, `VALID_PROTOCOLS`. Legacy generators unchanged.
- `orchestrator/allocator.py` — `get_proxies_templated()` (no caching/locking).
- `orchestrator/main.py` — order endpoint accepts optional `template`/`protocol`.
- `orchestrator/pergb.py` — new `list_batch_proxies` endpoint.
- `orchestrator/pergb_service.py` — `GeneratedPortRow.http_port` + `pi.http_port` added to the two
  pergb port SQL selects so pergb rows now carry `http_port` (enables https for pergb).

## Notes
- pergb rows **now carry `http_port`** (added this wave). If a pergb pool is socks5-only,
  https yields 409 as designed.
- No DB migration (uses existing `proxy_inventory.http_port`).
