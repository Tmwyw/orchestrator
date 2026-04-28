from orchestrator.config import PROJECT_ROOT
from orchestrator.db import connect

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def run_migrations() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
                create table if not exists schema_migrations (
                  version text primary key,
                  applied_at timestamptz not null default now()
                )
                """
        )
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.name
            cur.execute("select 1 from schema_migrations where version = %s", (version,))
            if cur.fetchone():
                continue
            sql = path.read_text(encoding="utf-8").strip()
            if sql:
                # psycopg3 simple-query protocol handles multi-statement SQL
                # (incl. DO $$...$$ blocks) when no parameters are bound.
                cur.execute(sql)
            cur.execute("insert into schema_migrations(version) values (%s)", (version,))
            print(f"applied migration: {version}")


def main() -> None:
    run_migrations()


if __name__ == "__main__":
    main()
