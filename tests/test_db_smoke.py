from pathlib import Path

from app.db import connect_db, init_db


def test_init_db_creates_core_tables_and_supports_minimal_insert_query(tmp_path: Path) -> None:
    database_path = tmp_path / "smoke.sqlite3"

    init_db(database_path)

    with connect_db(database_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "sources",
            "documents",
            "document_versions",
            "crawl_runs",
            "tags",
        }.issubset(table_names)

        cursor = connection.execute(
            """
            INSERT INTO sources (source_key, source_type, title, config_path)
            VALUES (?, ?, ?, ?)
            """,
            ("seed-openai", "seed", "OpenAI Blog Seed", "config/sources/openai.toml"),
        )
        source_id = cursor.lastrowid

        cursor = connection.execute(
            """
            INSERT INTO documents (
                source_id,
                canonical_url,
                title,
                current_raw_path,
                current_cleaned_path,
                fetch_status,
                extract_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                "https://example.com/article-1",
                "Example Article",
                "data/raw/example.com/article-1.html",
                "data/cleaned/example.com/article-1.md",
                "fetched",
                "extracted",
            ),
        )
        document_id = cursor.lastrowid

        connection.execute(
            """
            INSERT INTO document_versions (
                document_id,
                version_kind,
                file_path,
                model_name,
                prompt_name
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                document_id,
                "summary",
                "data/derived/example.com/article-1.summary.md",
                "llama3",
                "summary-v1",
            ),
        )

        connection.execute(
            """
            INSERT INTO crawl_runs (
                source_id,
                run_kind,
                status,
                discovered_count,
                fetched_count,
                extracted_count,
                log_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                "seed",
                "success",
                1,
                1,
                1,
                "data/logs/crawl-run-1.log",
            ),
        )

        connection.execute(
            """
            INSERT INTO tags (document_id, tag, tag_source)
            VALUES (?, ?, ?)
            """,
            (document_id, "llm", "manual"),
        )

        row = connection.execute(
            """
            SELECT d.canonical_url, s.source_key, COUNT(t.id) AS tag_count
            FROM documents AS d
            JOIN sources AS s ON s.id = d.source_id
            LEFT JOIN tags AS t ON t.document_id = d.id
            WHERE d.id = ?
            GROUP BY d.id, s.id
            """,
            (document_id,),
        ).fetchone()

        assert row is not None
        assert row["canonical_url"] == "https://example.com/article-1"
        assert row["source_key"] == "seed-openai"
        assert row["tag_count"] == 1
