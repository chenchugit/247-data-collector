from pathlib import Path

from app.discovery import load_source_definitions, run_discovery
from app.db import connect_db


def test_run_discovery_loads_sources_records_candidates_and_deduplicates(tmp_path: Path) -> None:
    database_path = tmp_path / "discovery.sqlite3"
    log_dir = tmp_path / "data" / "logs"
    config_path = Path(__file__).resolve().parent.parent / "config" / "sources" / "demo_sources.toml"

    source_definitions = load_source_definitions(config_path)
    assert [item.source_key for item in source_definitions] == [
        "demo-rss",
        "demo-sitemap",
        "demo-seed",
    ]

    first_results = run_discovery(
        config_path=config_path,
        database_path=database_path,
        log_dir=log_dir,
    )
    second_results = run_discovery(
        config_path=config_path,
        database_path=database_path,
        log_dir=log_dir,
    )

    assert [item.discovered_count for item in first_results] == [2, 3, 2]
    assert [item.inserted_count for item in first_results] == [2, 2, 0]
    assert all(item.status == "success" for item in first_results)
    assert all(item.log_path.startswith("data/logs/discovery-run-") for item in first_results)
    assert [item.discovered_count for item in second_results] == [2, 3, 2]
    assert [item.inserted_count for item in second_results] == [0, 0, 0]

    with connect_db(database_path) as connection:
        source_rows = connection.execute(
            "SELECT source_key, source_type, config_path FROM sources ORDER BY source_key"
        ).fetchall()
        assert [row["source_key"] for row in source_rows] == [
            "demo-rss",
            "demo-seed",
            "demo-sitemap",
        ]
        assert all(row["config_path"] == "config/sources/demo_sources.toml" for row in source_rows)

        document_rows = connection.execute(
            """
            SELECT canonical_url, fetch_status, extract_status
            FROM documents
            ORDER BY canonical_url
            """
        ).fetchall()
        assert [row["canonical_url"] for row in document_rows] == [
            "https://example.com/docs?id=1",
            "https://example.com/rss-only",
            "https://example.com/sitemap-only",
            "https://example.com/start",
        ]
        assert {row["fetch_status"] for row in document_rows} == {"discovered"}
        assert {row["extract_status"] for row in document_rows} == {"pending"}

        crawl_run_rows = connection.execute(
            """
            SELECT run_kind, status, discovered_count, log_path
            FROM crawl_runs
            ORDER BY id
            """
        ).fetchall()
        assert len(crawl_run_rows) == 6
        assert [row["run_kind"] for row in crawl_run_rows] == [
            "discovery:rss",
            "discovery:sitemap",
            "discovery:seed",
            "discovery:rss",
            "discovery:sitemap",
            "discovery:seed",
        ]
        assert all(row["status"] == "success" for row in crawl_run_rows)
        assert [row["discovered_count"] for row in crawl_run_rows] == [2, 3, 2, 2, 3, 2]
        assert all(str(row["log_path"]).startswith("data/logs/discovery-run-") for row in crawl_run_rows)

    first_log_path = log_dir / "discovery-run-1.log"
    assert first_log_path.exists()
    first_log_text = first_log_path.read_text(encoding="utf-8")
    assert '"event": "run_started"' in first_log_text
    assert '"event": "discovered_urls"' in first_log_text
    assert '"event": "run_finished"' in first_log_text
