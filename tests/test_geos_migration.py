"""Static-content sanity for migrations/047_geos.sql (PROXY-PARITY-1 A.1).

Tests in this repo don't run against a live DB, so we pin the migration's
intent instead of asserting post-apply rows: the geos table shape, the
mandatory OWNER TO, idempotency markers, and the full 27-code seed lifted
from the bot's GEO_LABELS (UK + GB are TWO rows sharing one flag).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_GEO_FILE = _MIGRATIONS_DIR / "047_geos.sql"

# (code, russian name) — must match strings_ru.py GEO_LABELS verbatim.
_EXPECTED_SEED = [
    ("US", "США"),
    ("DE", "Германия"),
    ("PL", "Польша"),
    ("GB", "Великобритания"),
    ("UK", "Великобритания"),
    ("FR", "Франция"),
    ("JP", "Япония"),
    ("NL", "Нидерланды"),
    ("CA", "Канада"),
    ("UA", "Украина"),
    ("RU", "Россия"),
    ("ES", "Испания"),
    ("IT", "Италия"),
    ("SE", "Швеция"),
    ("IN", "Индия"),
    ("BR", "Бразилия"),
    ("AU", "Австралия"),
    ("SG", "Сингапур"),
    ("KR", "Южная Корея"),
    ("TR", "Турция"),
    ("MX", "Мексика"),
    ("RO", "Румыния"),
    ("IL", "Израиль"),
    ("AE", "ОАЭ"),
    ("ID", "Индонезия"),
    ("CL", "Чили"),
    ("ZA", "ЮАР"),
]


@pytest.fixture(scope="module")
def geo_sql() -> str:
    return _GEO_FILE.read_text(encoding="utf-8")


def test_migration_file_exists() -> None:
    assert _GEO_FILE.is_file(), f"geos migration missing: {_GEO_FILE}"


def test_creates_geos_table_idempotently_with_check(geo_sql: str) -> None:
    assert "CREATE TABLE IF NOT EXISTS geos" in geo_sql
    # The code-format CHECK is the guard against junk codes.
    assert "CHECK (code ~ '^[A-Z]{2,8}$')" in geo_sql
    # Expected columns present.
    for col in ("flag", "name_ru", "name_en", "sort_order", "is_active"):
        assert col in geo_sql, f"missing column {col}"


def test_owner_to_orchestrator_role_present(geo_sql: str) -> None:
    # Without this the app role hits permission denied (migrations run
    # under sudo -u postgres).
    assert "ALTER TABLE geos OWNER TO netrun_orchestrator;" in geo_sql


def test_seed_is_idempotent(geo_sql: str) -> None:
    assert "INSERT INTO geos" in geo_sql
    assert "ON CONFLICT (code) DO NOTHING" in geo_sql


def test_seed_contains_all_27_codes_with_russian_names(geo_sql: str) -> None:
    for code, name_ru in _EXPECTED_SEED:
        assert f"'{code}'" in geo_sql, f"missing seed code {code}"
        assert f"'{name_ru}'" in geo_sql, f"missing russian name for {code}: {name_ru}"
    assert len(_EXPECTED_SEED) == 27


def test_uk_and_gb_are_both_seeded_sharing_one_flag(geo_sql: str) -> None:
    # Both rows present and both carry the 🇬🇧 flag (legacy parity).
    assert "'GB', '🇬🇧'" in geo_sql
    assert "'UK', '🇬🇧'" in geo_sql
