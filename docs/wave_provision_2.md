# Wave PROVISION-1 · Промпт ② — orchestrator: multi-account Vultr + /register + watchdog→БД

Репо: **netrun-orchestrator**. Backup-ветка `backup/provision-2-pre` @ `643446d`.
Рабочая ветка `wave/provision-2`. Вариант **B** (орк инстансы НЕ создаёт; нода
самоустанавливается и зовёт `/v1/nodes/register`). Контракт /register зафиксирован
Промптом ① (`node_runtime_new/main`, `docs/cloud_init_provisioning.md`).

---

## ЭТАП 0 — ПОДГОТОВКА (прочитано до кода)

### Якоря (проверены, сдвиги отмечены)
- `main.py:90-95` `require_api_key` — header `X-NETRUN-API-KEY` vs `cfg.api_key`. Для
  `/register` НЕ используем (нода без ключа) → bare `@app.post` без `Depends`.
- `main.py:403-545` `enroll_node` — паттерн upsert nodes `on conflict (url)` + auto-bind
  `sku_node_bindings` по гео. (Сдвиг от заявленных 332-456 — реальный диапазон 403-545.)
- `main.py:~250-336` `nodes_health` (auto-recovery heartbeat).
- `main.py:855-886` router-wiring: `app.include_router(admin_nodes_router, dependencies=[Depends(require_api_key)])`.
- `admin_nodes.py:37-49` `_vultr_api_key()` (env `VULTR_API_KEY` → fallback парс
  `/opt/netrun-orchestrator/vultr_watchdog.env`); `admin_nodes.py:108-153` `reboot_node`
  — Vultr API паттерн (list `GET /v2/instances` пагинация по cursor → match `main_ip`
  → `POST /v2/instances/{iid}/reboot`, ok=202/204). **Сейчас на ЕДИНОМ ключе** (env/файл).
- `db.py` — sync psycopg3, `connect()` (commit на выходе блока → годен для транзакции
  /register), `fetch_all/fetch_one/execute`; в async всегда `asyncio.to_thread`.
- `refill.py:38-65` `run_once` — читает **`skus.target_stock`** (per-SKU), раскидывает
  по bindings через `equal_share(caps)`. **Per-binding target_stock НЕ потребляется** →
  per-binding refill = follow-up (вне scope ②). Решение: SKU-create ставит
  `skus.target_stock=job.target_stock`, а на binding пишем `target_stock` forward-looking.

### Текущий МАКС миграции
`041` (`041_dualstack_product_kind.sql`). Свободные: **042..046**.
- 042 `vultr_accounts`
- 043 `nodes` ADD `vultr_account` + `vultr_instance_id`
- 044 `node_provisions`
- 045 `sku_node_bindings.target_stock` (forward-looking, step C.4)
- 046 `seed_vultr_account_import` (ручной, юзер запускает после ввода ключа)

### Секреты/конфиг — Fernet ОТСУТСТВУЕТ
- `config.py` `Config` (frozen dataclass) — НЕТ шифрования. `cryptography` НЕ в
  `requirements.txt` (только httpx==0.28.1). → добавляю `cryptography`, новый
  `orchestrator/crypto.py` (Fernet, ключ из env **`ORCH_FERNET_KEY`**). Vultr API-ключи
  аккаунтов хранятся `api_key_enc` = `Fernet(ORCH_FERNET_KEY).encrypt(key)`.
- `ORCH_FERNET_KEY` генерится `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
  и кладётся в orchestrator `.env` (НЕ в git). Без него accounts-CRUD/register-instance-lookup
  деградируют (CRUD → 500 `fernet_key_not_configured`; register всё равно регистрирует ноду,
  но без instance_id lookup).

### vultr_node_watchdog.sh — В РЕПО ОТСУТСТВУЕТ
- Хардкод-версия (`declare -A NODES`) живёт ТОЛЬКО на prod-сервере оркестратора
  (`51.38.205.194`), в git её нет. → пишу новый `scripts/vultr_node_watchdog.sh` с нуля,
  DB-driven. Reboot — НЕ дешифрует Fernet в bash, а зовёт оркестратор-эндпоинт
  `POST /v1/admin/nodes/{id}/reboot` (резолвит per-account ключ). Bash хранит только
  `ORCHESTRATOR_API_KEY` (он уже нужен) — не Vultr-ключи.

### Тест-инфра
- conftest пуст; **DB-тестов против живого Postgres НЕТ**. Паттерны: миграции = static
  content sanity (читаем .sql, assert подстроки); эндпоинты = mock `connect()`/fetch/execute
  + `TestClient`. Слежую этим паттернам (Postgres не требуется).

### ⚠️ Миграции — ВРУЧНУЮ
App-юзер БД без прав на `schema_migrations` → `python -m orchestrator.migrate` падает.
Пишу .sql, юзер катит `sudo -u postgres psql -d <db> -f <file>`. Команды — в REPORT.

---

## ЭТАП A — миграции
(заполняется)

## ЭТАП B — Vultr accounts слой
(заполняется)

## ЭТАП C — POST /v1/nodes/register
(заполняется)

## ЭТАП D — watchdog → БД
(заполняется)

## ЭТАП E — provision-prepare
(заполняется)

## ЭТАП F — тесты
(заполняется)

## Журнал прогресса
- ЭТАП 0 done. BACKUP `backup/provision-2-pre`@`643446d`, ветка `wave/provision-2`.
