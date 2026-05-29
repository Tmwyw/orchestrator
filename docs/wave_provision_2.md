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

## ЭТАП A — миграции (commit `0f3f73c`)
042 vultr_accounts · 043 nodes(+vultr_account FK,+vultr_instance_id) · 044
node_provisions · 045 sku_node_bindings.target_stock · 046 MANUAL seed. Все
аддитивны/идемпотентны.

### Ручной накат (app-юзер без прав на schema_migrations)
```bash
# на сервере оркестратора 51.38.205.194, DB = netrun_orch (подставь своё имя)
cd /opt/netrun-orchestrator
for m in 042_vultr_accounts 043_nodes_vultr_cols 044_node_provisions 045_binding_target_stock; do
  sudo -u postgres psql -d netrun_orch -f migrations/$m.sql
done
# проверка:
sudo -u postgres psql -d netrun_orch -c "\d vultr_accounts" -c "\d node_provisions" \
  -c "select column_name from information_schema.columns where table_name='nodes' and column_name in ('vultr_account','vultr_instance_id');"
```

### Seed 046 (после ORCH_FERNET_KEY + ввода ключа)
```bash
# 1) сгенерить Fernet-ключ (один раз) и положить в .env:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
echo 'ORCH_FERNET_KEY=<вывод выше>' >> /opt/netrun-orchestrator/.env
# 2) зашифровать текущий watchdog Vultr-ключ:
ORCH_FERNET_KEY=<...> python scripts/encrypt_secret.py "<VULTR_API_KEY>"
# 3) вписать вывод в 046 вместо __IMPORTED_API_KEY_ENC__, а пары ip->iid (из
#    prod vultr_node_watchdog.sh `declare -A NODES`) вместо __NODE_IP_1__/__VULTR_IID_1__
# 4) накатить:
sudo -u postgres psql -d netrun_orch -f migrations/046_seed_vultr_account_import.sql
```
⚠️ Маппинг ip→iid взять из старого хардкод-watchdog'а на проде (его в git нет).

## ЭТАП B/C/E — код (commit `7b8a065`)
- `crypto.py` (Fernet, ORCH_FERNET_KEY) + `scripts/encrypt_secret.py` + `cryptography` в requirements.
- `vultr.py` per-account клиент (retry 429/5xx, list/find_by_main_ip/reboot).
- `admin_vultr.py` accounts CRUD (POST/GET-masked/PATCH/DELETE=soft-disable) + provision-prepare + status.
- `admin_nodes.reboot` → per-account ключ (fallback legacy single key).
- `/v1/nodes/register` (bare, без api-key) в main.py: 9 шагов, идемпотентно, ok=false→failed.
- config: `ORCHESTRATOR_BASE_URL` + `CLOUD_INIT_TEMPLATE_PATH`; шаблон `deploy/node/cloud-init.sh.tmpl`
  (копия Промпта ① — ⚠️ при изменении node-side cloud-init пере-синкать или указать
  `CLOUD_INIT_TEMPLATE_PATH` на живой checkout node_runtime).

## ЭТАП D — watchdog (commit `2cedda4`)
`scripts/vultr_node_watchdog.sh` (DB-driven, reboot через orchestrator-эндпоинт,
bash без Fernet) + systemd service+timer (oneshot 60s). shellcheck чисто.

### Прод-проверка watchdog (критичный авто-подъём)
```bash
# 1) деплой: cp scripts/vultr_node_watchdog.sh /opt/netrun-orchestrator/scripts/ && chmod +x
#    cp deploy/systemd/netrun-vultr-node-watchdog.{service,timer}.template /etc/systemd/system/<без .template>
#    + дописать в /opt/netrun-orchestrator/.env: ORCHESTRATOR_API_KEY=..., ORCHESTRATOR_URL=http://127.0.0.1:8090
# 2) ОСТАНОВИТЬ старый хардкод-watchdog (systemctl disable --now <old>), иначе двойные ребуты!
# 3) dry-run вручную: sudo ENV_FILE=/opt/netrun-orchestrator/.env bash scripts/vultr_node_watchdog.sh
#    → в логе "probe ... " по каждой ноде из БД; здоровые = recovery/skip, без ребутов.
# 4) проверить reboot-путь на ОДНОЙ ноде: временно стопнуть node-agent на тест-ноде,
#    подождать 5 тиков → в journalctl -t vultr-node-watchdog "REBOOT ok node=<id>".
# 5) systemctl enable --now netrun-vultr-node-watchdog.timer; systemctl list-timers | grep vultr
```

## ЭТАП F — тесты (этот commit)
6 файлов, 43 теста, mock-based (Postgres НЕ нужен): test_crypto (Fernet round-trip/rotate/mask),
test_migrations_provision (static DDL sanity 042-046), test_vultr_client (per-account ключ,
retry 5xx, find/reboot, MockTransport), test_provision_register (complete_registration steps 3-6
existing+create-SKU, endpoint secret-match/ok=false/instance-lookup-fail), test_admin_vultr
(CRUD masked + provision-prepare), test_provision_prepare (render/oneliner/job + watchdog static).
**Полный сьют: 387 passed (было 344 +43), 0 регрессий.** ruff чисто на wave-файлах
(5 pre-existing E402 в нетронутом admin.py). mypy чисто на wave-модулях.

## Журнал прогресса
- ЭТАП 0 done. BACKUP `backup/provision-2-pre`@`643446d`, ветка `wave/provision-2`.
- A `0f3f73c` · B/C/E `7b8a065` (backups/ убран amend'ом) · D `2cedda4` · F (этот).
- **НЕ запушено** (жду «ок пуш»).
- ⚠️ Pre-deploy: ORCH_FERNET_KEY в .env ОБЯЗАТЕЛЕН (иначе CRUD→500, register регает
  ноду без instance_id) · cryptography в venv (`pip install -r requirements.txt`) ·
  ORCHESTRATOR_BASE_URL для provision-prepare · миграции 042-045 вручную + 046 seed ·
  старый watchdog отключить перед включением нового.
