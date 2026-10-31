"""Postgres connection and a minimal forward-only migration runner."""

import psycopg
from psycopg.types.json import Jsonb

from itn.config import database_url, migrations_dir


def connect() -> psycopg.Connection:
    return psycopg.connect(database_url())


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply any migrations in schema/migrations not yet recorded. Returns applied names."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    done = {row[0] for row in conn.execute("SELECT name FROM schema_migration")}
    files = sorted(migrations_dir().glob("*.sql"))
    if not files:
        raise RuntimeError(f"No migration files found in {migrations_dir()}")
    applied = []
    for path in files:
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
        applied.append(path.name)
    conn.commit()
    return applied


def upsert_artifact(conn: psycopg.Connection, a: dict) -> None:
    conn.execute(
        """
        INSERT INTO artifact (id, type, subtype, title, effective_date, in_scope, contentious,
                              closure_depth, sources, official_summary, text_key, text_sha256,
                              metadata)
        VALUES (%(id)s, %(type)s, %(subtype)s, %(title)s, %(effective_date)s, %(in_scope)s,
                %(contentious)s, %(closure_depth)s, %(sources)s, %(official_summary)s,
                %(text_key)s, %(text_sha256)s, %(metadata)s)
        ON CONFLICT (id) DO UPDATE SET
            subtype = EXCLUDED.subtype,
            title = EXCLUDED.title,
            effective_date = EXCLUDED.effective_date,
            in_scope = EXCLUDED.in_scope,
            contentious = COALESCE(EXCLUDED.contentious, artifact.contentious),
            closure_depth = LEAST(artifact.closure_depth, EXCLUDED.closure_depth),
            sources = EXCLUDED.sources,
            official_summary = COALESCE(EXCLUDED.official_summary, artifact.official_summary),
            text_key = COALESCE(EXCLUDED.text_key, artifact.text_key),
            text_sha256 = COALESCE(EXCLUDED.text_sha256, artifact.text_sha256),
            metadata = artifact.metadata || EXCLUDED.metadata,
            updated_at = now()
        """,
        {"contentious": None, **a, "sources": Jsonb(a["sources"]), "metadata": Jsonb(a["metadata"])},
    )


def get_cursor(conn: psycopg.Connection, source: str) -> dict | None:
    row = conn.execute("SELECT cursor FROM ingest_cursor WHERE source = %s", (source,)).fetchone()
    return row[0] if row else None


def set_cursor(conn: psycopg.Connection, source: str, cursor: dict) -> None:
    conn.execute(
        """
        INSERT INTO ingest_cursor (source, cursor) VALUES (%s, %s)
        ON CONFLICT (source) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()
        """,
        (source, Jsonb(cursor)),
    )
