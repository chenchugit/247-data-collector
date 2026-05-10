from pathlib import Path
import sqlite3

from .config import load_settings


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    config_path TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    source_id INTEGER,
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT,
    author TEXT,
    published_at TEXT,
    current_raw_path TEXT,
    current_cleaned_path TEXT,
    fetch_status TEXT NOT NULL DEFAULT 'discovered',
    extract_status TEXT NOT NULL DEFAULT 'pending',
    last_seen_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_id) REFERENCES sources (id)
);

CREATE TABLE IF NOT EXISTS document_versions (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL,
    version_kind TEXT NOT NULL,
    file_path TEXT NOT NULL,
    model_name TEXT,
    prompt_name TEXT,
    content_hash TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY,
    source_id INTEGER,
    run_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    extracted_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    log_path TEXT,
    FOREIGN KEY (source_id) REFERENCES sources (id)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    tag_source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (document_id, tag, tag_source),
    FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
);
"""


def get_database_path() -> Path:
    return load_settings().database_path


def connect_db(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or get_database_path())
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def init_db(database_path: Path | None = None) -> Path:
    path = Path(database_path or get_database_path())
    path.parent.mkdir(parents=True, exist_ok=True)

    with connect_db(path) as connection:
        connection.executescript(SCHEMA_SQL)

    return path


def upsert_source(
    connection: sqlite3.Connection,
    *,
    source_key: str,
    source_type: str,
    title: str,
    config_path: str | None,
    enabled: bool = True,
) -> int:
    connection.execute(
        """
        INSERT INTO sources (source_key, source_type, title, config_path, enabled)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            source_type = excluded.source_type,
            title = excluded.title,
            config_path = excluded.config_path,
            enabled = excluded.enabled,
            updated_at = CURRENT_TIMESTAMP
        """,
        (source_key, source_type, title, config_path, int(enabled)),
    )

    row = connection.execute(
        "SELECT id FROM sources WHERE source_key = ?",
        (source_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"failed to upsert source: {source_key}")
    return int(row["id"])


def start_crawl_run(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    run_kind: str,
    status: str = "running",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO crawl_runs (source_id, run_kind, status)
        VALUES (?, ?, ?)
        """,
        (source_id, run_kind, status),
    )
    return int(cursor.lastrowid)


def finish_crawl_run(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    status: str,
    discovered_count: int = 0,
    fetched_count: int = 0,
    extracted_count: int = 0,
    error_message: str | None = None,
    log_path: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE crawl_runs
        SET status = ?,
            finished_at = CURRENT_TIMESTAMP,
            discovered_count = ?,
            fetched_count = ?,
            extracted_count = ?,
            error_message = ?,
            log_path = ?
        WHERE id = ?
        """,
        (
            status,
            discovered_count,
            fetched_count,
            extracted_count,
            error_message,
            log_path,
            run_id,
        ),
    )


def record_discovered_document(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    canonical_url: str,
) -> bool:
    existing = connection.execute(
        "SELECT id FROM documents WHERE canonical_url = ?",
        (canonical_url,),
    ).fetchone()
    if existing is not None:
        connection.execute(
            """
            UPDATE documents
            SET last_seen_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE canonical_url = ?
            """,
            (canonical_url,),
        )
        return False

    connection.execute(
        """
        INSERT INTO documents (
            source_id,
            canonical_url,
            fetch_status,
            extract_status,
            last_seen_at
        )
        VALUES (?, ?, 'discovered', 'pending', CURRENT_TIMESTAMP)
        """,
        (source_id, canonical_url),
    )
    return True


def record_discovered_documents(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    canonical_urls: list[str],
) -> int:
    inserted_count = 0
    for canonical_url in canonical_urls:
        inserted = record_discovered_document(
            connection,
            source_id=source_id,
            canonical_url=canonical_url,
        )
        if inserted:
            inserted_count += 1
    return inserted_count


def get_source_id_by_key(connection: sqlite3.Connection, source_key: str) -> int | None:
    row = connection.execute(
        "SELECT id FROM sources WHERE source_key = ?",
        (source_key,),
    ).fetchone()
    if row is None:
        return None
    return int(row["id"])


def list_documents_for_fetch(
    connection: sqlite3.Connection,
    *,
    source_id: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT id, canonical_url, fetch_status
        FROM documents
        WHERE source_id = ?
          AND fetch_status IN ('discovered', 'fetch_failed', 'needs_browser')
        ORDER BY id
        """,
        (source_id,),
    ).fetchall()


def update_document_fetch_state(
    connection: sqlite3.Connection,
    *,
    document_id: int,
    fetch_status: str,
    current_raw_path: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE documents
        SET fetch_status = ?,
            current_raw_path = COALESCE(?, current_raw_path),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (fetch_status, current_raw_path, document_id),
    )


def list_documents_for_extract(
    connection: sqlite3.Connection,
    *,
    source_id: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT id, canonical_url, current_raw_path, fetch_status, extract_status
        FROM documents
        WHERE source_id = ?
          AND fetch_status = 'fetched'
          AND current_raw_path IS NOT NULL
          AND extract_status IN ('pending', 'extract_failed')
        ORDER BY id
        """,
        (source_id,),
    ).fetchall()


def update_document_extract_state(
    connection: sqlite3.Connection,
    *,
    document_id: int,
    extract_status: str,
    current_cleaned_path: str | None = None,
    title: str | None = None,
    author: str | None = None,
    published_at: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE documents
        SET extract_status = ?,
            current_cleaned_path = COALESCE(?, current_cleaned_path),
            title = COALESCE(?, title),
            author = COALESCE(?, author),
            published_at = COALESCE(?, published_at),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            extract_status,
            current_cleaned_path,
            title,
            author,
            published_at,
            document_id,
        ),
    )


def get_document_for_versioning(
    connection: sqlite3.Connection,
    *,
    document_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, canonical_url, current_cleaned_path
        FROM documents
        WHERE id = ?
        """,
        (document_id,),
    ).fetchone()


def list_documents_for_analysis(
    connection: sqlite3.Connection,
    *,
    source_id: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT id, canonical_url, current_cleaned_path
        FROM documents
        WHERE source_id = ?
          AND extract_status = 'extracted'
          AND current_cleaned_path IS NOT NULL
        ORDER BY id
        """,
        (source_id,),
    ).fetchall()


def insert_document_version(
    connection: sqlite3.Connection,
    *,
    document_id: int,
    version_kind: str,
    file_path: str,
    model_name: str | None = None,
    prompt_name: str | None = None,
    content_hash: str | None = None,
) -> int:
    existing = connection.execute(
        """
        SELECT id
        FROM document_versions
        WHERE document_id = ?
          AND version_kind = ?
          AND file_path = ?
          AND COALESCE(model_name, '') = COALESCE(?, '')
          AND COALESCE(prompt_name, '') = COALESCE(?, '')
          AND COALESCE(content_hash, '') = COALESCE(?, '')
        """,
        (
            document_id,
            version_kind,
            file_path,
            model_name,
            prompt_name,
            content_hash,
        ),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])

    cursor = connection.execute(
        """
        INSERT INTO document_versions (
            document_id,
            version_kind,
            file_path,
            model_name,
            prompt_name,
            content_hash
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            version_kind,
            file_path,
            model_name,
            prompt_name,
            content_hash,
        ),
    )
    return int(cursor.lastrowid)


def list_document_versions(
    connection: sqlite3.Connection,
    *,
    document_id: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT id, document_id, version_kind, file_path, model_name, prompt_name, content_hash, created_at
        FROM document_versions
        WHERE document_id = ?
        ORDER BY id
        """,
        (document_id,),
    ).fetchall()


def get_document_version(
    connection: sqlite3.Connection,
    *,
    version_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, document_id, version_kind, file_path, model_name, prompt_name, content_hash, created_at
        FROM document_versions
        WHERE id = ?
        """,
        (version_id,),
    ).fetchone()
