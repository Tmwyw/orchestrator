-- 047_geos.sql — Wave PROXY-PARITY-1 Phase A.
--
-- Dynamic geo metadata (flag + Russian/English display name) so a new
-- country's flag/name can be picked up EVERYWHERE (shop, stats,
-- notifications, grouping) without a code edit + redeploy. Until now the
-- flag→code map lived hardcoded in TWO places:
--   * orchestrator/admin_catalog.py  → _GEO_FLAGS (code→flag)
--   * bot/strings_ru.py              → GEO_LABELS (code→flag+name_ru)
-- This table becomes the single source of truth; orchestrator owns
-- skus.geo_code and computes display_name, so geos lives here.
--
-- NOTE: this does NOT touch skus.geo_code (immutable by design) — only
-- display metadata. The existing GET /v1/admin/geos (usage counts from
-- skus) is unaffected; the new GET /v1/admin/geos/catalog reads here.
--
-- Seed = the 27 codes currently hardcoded in _GEO_FLAGS, with Russian
-- names lifted verbatim from bot GEO_LABELS (strings_ru.py). UK and GB
-- are TWO separate rows sharing one flag (🇬🇧), mirroring the bot map.
--
-- Additive + idempotent: CREATE TABLE IF NOT EXISTS + INSERT ... ON
-- CONFLICT (code) DO NOTHING — re-running applies cleanly and never
-- clobbers operator edits to flag/name/sort_order.
--
-- OWNER TO netrun_orchestrator is REQUIRED: migrations run under
-- `sudo -u postgres`, so a table created without an explicit owner stays
-- owned by postgres and the app role gets "permission denied" at runtime
-- (lesson from prior waves).
--
-- Rollback (manual, destructive — drops all geo metadata incl. operator
-- edits; the static _GEO_FLAGS / GEO_LABELS fallbacks still cover the
-- original 27 codes):
--   DROP TABLE IF EXISTS geos;

CREATE TABLE IF NOT EXISTS geos (
    code        TEXT PRIMARY KEY CHECK (code ~ '^[A-Z]{2,8}$'),
    flag        TEXT        NOT NULL DEFAULT '🌐',
    name_ru     TEXT        NOT NULL,
    name_en     TEXT,
    sort_order  INT         NOT NULL DEFAULT 0,
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE geos OWNER TO netrun_orchestrator;

INSERT INTO geos (code, flag, name_ru, sort_order) VALUES
    ('US', '🇺🇸', 'США',            1),
    ('DE', '🇩🇪', 'Германия',       2),
    ('PL', '🇵🇱', 'Польша',         3),
    ('GB', '🇬🇧', 'Великобритания', 4),
    ('UK', '🇬🇧', 'Великобритания', 5),
    ('FR', '🇫🇷', 'Франция',        6),
    ('JP', '🇯🇵', 'Япония',         7),
    ('NL', '🇳🇱', 'Нидерланды',     8),
    ('CA', '🇨🇦', 'Канада',         9),
    ('UA', '🇺🇦', 'Украина',        10),
    ('RU', '🇷🇺', 'Россия',         11),
    ('ES', '🇪🇸', 'Испания',        12),
    ('IT', '🇮🇹', 'Италия',         13),
    ('SE', '🇸🇪', 'Швеция',         14),
    ('IN', '🇮🇳', 'Индия',          15),
    ('BR', '🇧🇷', 'Бразилия',       16),
    ('AU', '🇦🇺', 'Австралия',      17),
    ('SG', '🇸🇬', 'Сингапур',       18),
    ('KR', '🇰🇷', 'Южная Корея',    19),
    ('TR', '🇹🇷', 'Турция',         20),
    ('MX', '🇲🇽', 'Мексика',        21),
    ('RO', '🇷🇴', 'Румыния',        22),
    ('IL', '🇮🇱', 'Израиль',        23),
    ('AE', '🇦🇪', 'ОАЭ',            24),
    ('ID', '🇮🇩', 'Индонезия',      25),
    ('CL', '🇨🇱', 'Чили',           26),
    ('ZA', '🇿🇦', 'ЮАР',            27)
ON CONFLICT (code) DO NOTHING;
