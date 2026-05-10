from pathlib import Path

from app import create_app
from app.db import (
    connect_db,
    finish_crawl_run,
    init_db,
    record_discovered_documents,
    start_crawl_run,
    update_document_extract_state,
    update_document_fetch_state,
    upsert_source,
)
from app.extract import build_cleaned_artifact_relative_path
from app.fetch import build_raw_artifact_relative_path
from app.versioning import persist_document_version


def test_flask_ui_browses_documents_sources_runs_and_versions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    cleaned_dir = data_dir / "cleaned"
    derived_dir = data_dir / "derived"
    log_dir = data_dir / "logs"
    database_path = tmp_path / "instance" / "ui.sqlite3"

    monkeypatch.setenv("AUTO_SCRAPY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUTO_SCRAPY_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("AUTO_SCRAPY_INSTANCE_DIR", str(tmp_path / "instance"))

    init_db(database_path)

    article_url = "https://fixture.example/article"
    raw_relative = build_raw_artifact_relative_path(article_url, "text/html; charset=utf-8")
    raw_path = raw_dir / raw_relative
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        "<html><body><article>raw fixture</article></body></html>",
        encoding="utf-8",
    )

    cleaned_relative = build_cleaned_artifact_relative_path(article_url)
    cleaned_path = cleaned_dir / cleaned_relative
    cleaned_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned_path.write_text("# Fixture Article\n\nUseful cleaned content.\n", encoding="utf-8")

    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "fetch-run-1.log").write_text(
        '{"status": "fetched", "fetch_method": "http"}\n',
        encoding="utf-8",
    )

    with connect_db(database_path) as connection:
        source_id = upsert_source(
            connection,
            source_key="fixture-ui",
            source_type="seed",
            title="Fixture UI Source",
            config_path="tests/fixture-ui",
        )
        inserted = record_discovered_documents(
            connection,
            source_id=source_id,
            canonical_urls=[article_url],
        )
        assert inserted == 1

        document_row = connection.execute(
            "SELECT id FROM documents WHERE canonical_url = ?",
            (article_url,),
        ).fetchone()
        assert document_row is not None
        document_id = int(document_row["id"])

        update_document_fetch_state(
            connection,
            document_id=document_id,
            fetch_status="fetched",
            current_raw_path=str((Path("data") / "raw" / raw_relative).as_posix()),
        )
        update_document_extract_state(
            connection,
            document_id=document_id,
            extract_status="extracted",
            current_cleaned_path=str((Path("data") / "cleaned" / cleaned_relative).as_posix()),
            title="Fixture Article",
        )

        run_id = start_crawl_run(
            connection,
            source_id=source_id,
            run_kind="fetch:http",
        )
        finish_crawl_run(
            connection,
            run_id=run_id,
            status="success",
            fetched_count=1,
            log_path="data/logs/fetch-run-1.log",
        )

    version = persist_document_version(
        document_id=document_id,
        version_kind="reader-note",
        content="# Derived Note\n\nFixture note.\n",
        database_path=database_path,
        cleaned_dir=cleaned_dir,
        derived_dir=derived_dir,
    )

    app = create_app()
    client = app.test_client()

    documents_response = client.get("/documents")
    assert documents_response.status_code == 200
    assert "Fixture Article" in documents_response.get_data(as_text=True)

    document_response = client.get(f"/documents/{document_id}")
    document_text = document_response.get_data(as_text=True)
    assert document_response.status_code == 200
    assert "data/raw/" in document_text
    assert "data/cleaned/" in document_text
    assert "reader-note" in document_text

    raw_response = client.get(f"/documents/{document_id}/artifacts/raw")
    assert raw_response.status_code == 200
    assert "raw fixture" in raw_response.get_data(as_text=True)

    cleaned_response = client.get(f"/documents/{document_id}/artifacts/cleaned")
    assert cleaned_response.status_code == 200
    assert "Useful cleaned content." in cleaned_response.get_data(as_text=True)

    sources_response = client.get("/sources")
    assert sources_response.status_code == 200
    assert "Fixture UI Source" in sources_response.get_data(as_text=True)

    source_detail_response = client.get(f"/sources/{source_id}")
    assert source_detail_response.status_code == 200
    assert "fixture-ui" in source_detail_response.get_data(as_text=True)

    runs_response = client.get("/runs")
    assert runs_response.status_code == 200
    assert "fetch:http" in runs_response.get_data(as_text=True)

    run_detail_response = client.get(f"/runs/{run_id}")
    run_text = run_detail_response.get_data(as_text=True)
    assert run_detail_response.status_code == 200
    assert "data/logs/fetch-run-1.log" in run_text
    assert "Log Status:</strong> available" in run_text
    assert "fetch_method" in run_text
    assert "http" in run_text

    version_response = client.get(f"/versions/{version.version_id}")
    version_text = version_response.get_data(as_text=True)
    assert version_response.status_code == 200
    assert "Fixture note." in version_text
    assert version.file_path in version_text


def test_flask_ui_rejects_artifact_paths_outside_project_storage_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    cleaned_dir = data_dir / "cleaned"
    database_path = tmp_path / "instance" / "ui.sqlite3"
    outside_path = tmp_path / "outside.txt"

    monkeypatch.setenv("AUTO_SCRAPY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUTO_SCRAPY_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("AUTO_SCRAPY_INSTANCE_DIR", str(tmp_path / "instance"))

    init_db(database_path)

    article_url = "https://fixture.example/restricted"
    raw_relative = build_raw_artifact_relative_path(article_url, "text/html; charset=utf-8")
    raw_path = raw_dir / raw_relative
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        "<html><body><article>safe raw fixture</article></body></html>",
        encoding="utf-8",
    )

    cleaned_relative = build_cleaned_artifact_relative_path(article_url)
    cleaned_path = cleaned_dir / cleaned_relative
    cleaned_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned_path.write_text("# Restricted Fixture\n\nSafe cleaned content.\n", encoding="utf-8")

    outside_path.write_text("outside fixture", encoding="utf-8")

    with connect_db(database_path) as connection:
        source_id = upsert_source(
            connection,
            source_key="fixture-ui-restricted",
            source_type="seed",
            title="Fixture UI Restricted Source",
            config_path="tests/fixture-ui-restricted",
        )
        inserted = record_discovered_documents(
            connection,
            source_id=source_id,
            canonical_urls=[article_url],
        )
        assert inserted == 1

        document_row = connection.execute(
            "SELECT id FROM documents WHERE canonical_url = ?",
            (article_url,),
        ).fetchone()
        assert document_row is not None
        document_id = int(document_row["id"])

        update_document_fetch_state(
            connection,
            document_id=document_id,
            fetch_status="fetched",
            current_raw_path=str((Path("data") / "raw" / raw_relative).as_posix()),
        )
        update_document_extract_state(
            connection,
            document_id=document_id,
            extract_status="extracted",
            current_cleaned_path=str(outside_path),
            title="Restricted Fixture",
        )

    app = create_app()
    client = app.test_client()

    valid_raw_response = client.get(f"/documents/{document_id}/artifacts/raw")
    assert valid_raw_response.status_code == 200
    assert "safe raw fixture" in valid_raw_response.get_data(as_text=True)

    blocked_cleaned_response = client.get(f"/documents/{document_id}/artifacts/cleaned")
    assert blocked_cleaned_response.status_code == 404


def test_flask_ui_rejects_version_paths_outside_project_storage_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "data"
    cleaned_dir = data_dir / "cleaned"
    database_path = tmp_path / "instance" / "ui.sqlite3"
    outside_path = tmp_path / "outside-derived.md"

    monkeypatch.setenv("AUTO_SCRAPY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUTO_SCRAPY_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("AUTO_SCRAPY_INSTANCE_DIR", str(tmp_path / "instance"))

    init_db(database_path)

    article_url = "https://fixture.example/version-restricted"
    cleaned_relative = build_cleaned_artifact_relative_path(article_url)
    cleaned_path = cleaned_dir / cleaned_relative
    cleaned_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned_path.write_text("# Fixture Article\n\nUseful cleaned content.\n", encoding="utf-8")

    outside_path.write_text("outside derived fixture", encoding="utf-8")

    with connect_db(database_path) as connection:
        source_id = upsert_source(
            connection,
            source_key="fixture-ui-version-restricted",
            source_type="seed",
            title="Fixture UI Version Restricted Source",
            config_path="tests/fixture-ui-version-restricted",
        )
        inserted = record_discovered_documents(
            connection,
            source_id=source_id,
            canonical_urls=[article_url],
        )
        assert inserted == 1

        document_row = connection.execute(
            "SELECT id FROM documents WHERE canonical_url = ?",
            (article_url,),
        ).fetchone()
        assert document_row is not None
        document_id = int(document_row["id"])

        update_document_extract_state(
            connection,
            document_id=document_id,
            extract_status="extracted",
            current_cleaned_path=str((Path("data") / "cleaned" / cleaned_relative).as_posix()),
            title="Version Restricted Fixture",
        )
    version_id = persist_document_version(
        document_id=document_id,
        version_kind="reader-note",
        content="# Derived Note\n\nFixture note.\n",
        database_path=database_path,
        cleaned_dir=cleaned_dir,
        derived_dir=data_dir / "derived",
    ).version_id

    with connect_db(database_path) as connection:
        connection.execute(
            "UPDATE document_versions SET file_path = ? WHERE id = ?",
            (str(outside_path), version_id),
        )

    app = create_app()
    client = app.test_client()

    blocked_version_response = client.get(f"/versions/{version_id}")
    assert blocked_version_response.status_code == 404


def test_flask_ui_reports_missing_run_log_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    database_path = tmp_path / "instance" / "ui.sqlite3"

    monkeypatch.setenv("AUTO_SCRAPY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUTO_SCRAPY_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("AUTO_SCRAPY_INSTANCE_DIR", str(tmp_path / "instance"))

    init_db(database_path)

    with connect_db(database_path) as connection:
        source_id = upsert_source(
            connection,
            source_key="fixture-ui-log-missing",
            source_type="seed",
            title="Fixture UI Missing Log Source",
            config_path="tests/fixture-ui-log-missing",
        )
        run_id = start_crawl_run(
            connection,
            source_id=source_id,
            run_kind="fetch:http",
        )
        finish_crawl_run(
            connection,
            run_id=run_id,
            status="failed",
            error_message="fixture failure",
            log_path="data/logs/missing-run.log",
        )

    app = create_app()
    client = app.test_client()

    run_detail_response = client.get(f"/runs/{run_id}")
    run_text = run_detail_response.get_data(as_text=True)
    assert run_detail_response.status_code == 200
    assert "Log Status:</strong> missing" in run_text
    assert "fixture failure" in run_text
    assert "Recorded log file is not currently available" in run_text
