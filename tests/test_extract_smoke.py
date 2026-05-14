from pathlib import Path

from app.db import (
    connect_db,
    init_db,
    record_discovered_documents,
    update_document_fetch_state,
    upsert_source,
)
from app.extract import run_extract
from app.fetch import build_raw_artifact_relative_path


def test_run_extract_consumes_current_raw_path_and_requeues_missing_raw_artifact(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "extract.sqlite3"
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    cleaned_dir = data_dir / "cleaned"
    log_dir = data_dir / "logs"

    init_db(database_path)

    article_url = "https://fixture.example/article"
    missing_url = "https://fixture.example/missing-raw"

    relative_raw_path = build_raw_artifact_relative_path(article_url, "text/html; charset=utf-8")
    written_raw_path = raw_dir / relative_raw_path
    written_raw_path.parent.mkdir(parents=True, exist_ok=True)
    written_raw_path.write_text(
        "<html><head><title>Fixture Article</title></head>"
        "<body><article><h1>Fixture Article</h1><p>"
        "Useful extracted content with enough detail to pass the quality gate. "
        "This paragraph describes a concrete technical topic, includes meaningful context, "
        "and provides enough article-like body text for downstream processing."
        "</p></article></body></html>",
        encoding="utf-8",
    )

    with connect_db(database_path) as connection:
        source_id = upsert_source(
            connection,
            source_key="fixture-extract",
            source_type="seed",
            title="Fixture Extract Source",
            config_path="tests/fixture-extract",
        )
        inserted = record_discovered_documents(
            connection,
            source_id=source_id,
            canonical_urls=[article_url, missing_url],
        )
        assert inserted == 2

        article_row = connection.execute(
            "SELECT id FROM documents WHERE canonical_url = ?",
            (article_url,),
        ).fetchone()
        missing_row = connection.execute(
            "SELECT id FROM documents WHERE canonical_url = ?",
            (missing_url,),
        ).fetchone()

        assert article_row is not None
        assert missing_row is not None

        update_document_fetch_state(
            connection,
            document_id=int(article_row["id"]),
            fetch_status="fetched",
            current_raw_path=str((Path("data") / "raw" / relative_raw_path).as_posix()),
        )
        update_document_fetch_state(
            connection,
            document_id=int(missing_row["id"]),
            fetch_status="fetched",
            current_raw_path="data/raw/fixture/missing.html",
        )

    result = run_extract(
        source_key="fixture-extract",
        database_path=database_path,
        raw_dir=raw_dir,
        cleaned_dir=cleaned_dir,
        log_dir=log_dir,
    )

    assert result.source_key == "fixture-extract"
    assert result.extracted_count == 1
    assert result.failed_count == 1
    assert result.status == "partial_failure"
    assert result.log_path == "data/logs/extract-run-1.log"

    with connect_db(database_path) as connection:
        rows = connection.execute(
            """
            SELECT canonical_url, fetch_status, extract_status, current_raw_path, current_cleaned_path, title
            FROM documents
            ORDER BY canonical_url
            """
        ).fetchall()
        row_map = {row["canonical_url"]: row for row in rows}

        article_row = row_map[article_url]
        missing_row = row_map[missing_url]

        assert article_row["fetch_status"] == "fetched"
        assert article_row["extract_status"] == "extracted"
        assert article_row["current_raw_path"] is not None
        assert article_row["current_cleaned_path"] is not None
        assert article_row["title"] == "Fixture Article"

        cleaned_path = tmp_path / Path(article_row["current_cleaned_path"])
        assert cleaned_path.exists()
        cleaned_text = cleaned_path.read_text(encoding="utf-8")
        assert "Fixture Article" in cleaned_text
        assert "Useful extracted content with enough detail" in cleaned_text

        assert missing_row["fetch_status"] == "discovered"
        assert missing_row["extract_status"] == "pending"
        assert missing_row["current_raw_path"] is None
        assert missing_row["current_cleaned_path"] is None

        crawl_run = connection.execute(
            """
            SELECT run_kind, status, extracted_count, error_message, log_path
            FROM crawl_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert crawl_run is not None
        assert crawl_run["run_kind"] == "extract:trafilatura"
        assert crawl_run["status"] == "partial_failure"
        assert crawl_run["extracted_count"] == 1
        assert crawl_run["log_path"] == "data/logs/extract-run-1.log"
        assert "missing-raw" in crawl_run["error_message"]

    extract_log_path = log_dir / "extract-run-1.log"
    assert extract_log_path.exists()
    log_text = extract_log_path.read_text(encoding="utf-8")
    assert '"event": "run_started"' in log_text
    assert '"status": "extracted"' in log_text
    assert '"status": "raw_missing_requeued"' in log_text
    assert '"event": "run_finished"' in log_text


def test_run_extract_rejects_thin_landing_page_content(tmp_path: Path) -> None:
    database_path = tmp_path / "extract-thin.sqlite3"
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    cleaned_dir = data_dir / "cleaned"
    log_dir = data_dir / "logs"

    init_db(database_path)

    thin_url = "https://fixture.example/"
    relative_raw_path = build_raw_artifact_relative_path(thin_url, "text/html; charset=utf-8")
    written_raw_path = raw_dir / relative_raw_path
    written_raw_path.parent.mkdir(parents=True, exist_ok=True)
    written_raw_path.write_text(
        """
        <html><head><title>Home</title></head>
        <body>
          <nav><a href="/a">A</a><a href="/b">B</a></nav>
          <main><p>Welcome. Latest posts and links.</p></main>
        </body></html>
        """,
        encoding="utf-8",
    )

    with connect_db(database_path) as connection:
        source_id = upsert_source(
            connection,
            source_key="fixture-extract-thin",
            source_type="seed",
            title="Fixture Extract Thin Source",
            config_path="tests/fixture-extract-thin",
        )
        record_discovered_documents(
            connection,
            source_id=source_id,
            canonical_urls=[thin_url],
        )
        row = connection.execute(
            "SELECT id FROM documents WHERE canonical_url = ?",
            (thin_url,),
        ).fetchone()
        assert row is not None
        update_document_fetch_state(
            connection,
            document_id=int(row["id"]),
            fetch_status="fetched",
            current_raw_path=str((Path("data") / "raw" / relative_raw_path).as_posix()),
        )

    result = run_extract(
        source_key="fixture-extract-thin",
        database_path=database_path,
        raw_dir=raw_dir,
        cleaned_dir=cleaned_dir,
        log_dir=log_dir,
    )

    assert result.extracted_count == 0
    assert result.failed_count == 1
    assert result.status == "failed"

    with connect_db(database_path) as connection:
        row = connection.execute(
            "SELECT extract_status, current_cleaned_path FROM documents WHERE canonical_url = ?",
            (thin_url,),
        ).fetchone()
        assert row is not None
        assert row["extract_status"] == "rejected_low_quality"
        assert row["current_cleaned_path"] is None


def test_run_extract_cleans_markdown_raw_artifact(tmp_path: Path) -> None:
    database_path = tmp_path / "extract-markdown.sqlite3"
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    cleaned_dir = data_dir / "cleaned"
    log_dir = data_dir / "logs"

    init_db(database_path)

    doc_url = "https://docs.fixture.example/guide/intro"
    raw_relative_path = Path("docs.fixture.example") / "guide-intro.md"
    raw_path = raw_dir / raw_relative_path
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        "# Guide Introduction\r\n\r\n"
        "This markdown document explains a local integration workflow for developers. "
        "It includes installation notes, configuration details, operational guidance, "
        "and enough meaningful prose to pass the extraction quality gate safely.\r\n\r\n"
        "- Install the package in a controlled environment.\r\n"
        "- Configure the service endpoint and authentication settings.\r\n"
        "- Run a smoke check before enabling scheduled collection.\r\n\r\n"
        "```bash\r\nexample --check\r\n```\r\n",
        encoding="utf-8",
    )

    with connect_db(database_path) as connection:
        source_id = upsert_source(
            connection,
            source_key="fixture-markdown",
            source_type="seed",
            title="Fixture Markdown Source",
            config_path="tests/fixture-markdown",
        )
        record_discovered_documents(connection, source_id=source_id, canonical_urls=[doc_url])
        row = connection.execute(
            "SELECT id FROM documents WHERE canonical_url = ?",
            (doc_url,),
        ).fetchone()
        assert row is not None
        update_document_fetch_state(
            connection,
            document_id=int(row["id"]),
            fetch_status="fetched",
            current_raw_path=str((Path("data") / "raw" / raw_relative_path).as_posix()),
        )

    result = run_extract(
        source_key="fixture-markdown",
        database_path=database_path,
        raw_dir=raw_dir,
        cleaned_dir=cleaned_dir,
        log_dir=log_dir,
    )

    assert result.status == "success"
    assert result.extracted_count == 1

    with connect_db(database_path) as connection:
        row = connection.execute(
            "SELECT title, extract_status, current_cleaned_path FROM documents WHERE canonical_url = ?",
            (doc_url,),
        ).fetchone()
        assert row is not None
        assert row["extract_status"] == "extracted"
        assert row["title"] == "Guide Introduction"
        cleaned_path = tmp_path / Path(row["current_cleaned_path"])

    cleaned_text = cleaned_path.read_text(encoding="utf-8")
    assert "# Guide Introduction" in cleaned_text
    assert "- Install the package" in cleaned_text
    assert "```bash" in cleaned_text


def test_run_extract_cleans_plain_text_raw_artifact(tmp_path: Path) -> None:
    database_path = tmp_path / "extract-text.sqlite3"
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    cleaned_dir = data_dir / "cleaned"
    log_dir = data_dir / "logs"

    init_db(database_path)

    doc_url = "https://docs.fixture.example/reference.txt"
    raw_relative_path = Path("docs.fixture.example") / "reference.txt"
    raw_path = raw_dir / raw_relative_path
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        "Reference Guide\n\n"
        "This plain text document describes a production workflow for configuring a "
        "local documentation collector. It explains source selection, validation, "
        "operator review, and repeated smoke checks with enough detail for downstream "
        "analysis. The content is not HTML and should be preserved as readable text.\n\n"
        "Operators should verify the configuration, run a bounded extraction pass, "
        "inspect failures, and only then enable long-running scheduled collection.\n",
        encoding="utf-8",
    )

    with connect_db(database_path) as connection:
        source_id = upsert_source(
            connection,
            source_key="fixture-text",
            source_type="seed",
            title="Fixture Text Source",
            config_path="tests/fixture-text",
        )
        record_discovered_documents(connection, source_id=source_id, canonical_urls=[doc_url])
        row = connection.execute(
            "SELECT id FROM documents WHERE canonical_url = ?",
            (doc_url,),
        ).fetchone()
        assert row is not None
        update_document_fetch_state(
            connection,
            document_id=int(row["id"]),
            fetch_status="fetched",
            current_raw_path=str((Path("data") / "raw" / raw_relative_path).as_posix()),
        )

    result = run_extract(
        source_key="fixture-text",
        database_path=database_path,
        raw_dir=raw_dir,
        cleaned_dir=cleaned_dir,
        log_dir=log_dir,
    )

    assert result.status == "success"
    assert result.extracted_count == 1

    with connect_db(database_path) as connection:
        row = connection.execute(
            "SELECT title, extract_status, current_cleaned_path FROM documents WHERE canonical_url = ?",
            (doc_url,),
        ).fetchone()
        assert row is not None
        assert row["extract_status"] == "extracted"
        assert row["title"] == "Reference Guide"
        cleaned_path = tmp_path / Path(row["current_cleaned_path"])

    cleaned_text = cleaned_path.read_text(encoding="utf-8")
    assert "Reference Guide" in cleaned_text
    assert "This plain text document describes" in cleaned_text


def test_run_extract_rejects_tiny_markdown_raw_artifact(tmp_path: Path) -> None:
    database_path = tmp_path / "extract-tiny-markdown.sqlite3"
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    cleaned_dir = data_dir / "cleaned"
    log_dir = data_dir / "logs"

    init_db(database_path)

    doc_url = "https://docs.fixture.example/tiny"
    raw_relative_path = Path("docs.fixture.example") / "tiny.md"
    raw_path = raw_dir / raw_relative_path
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("# Tiny\n\nTODO\n", encoding="utf-8")

    with connect_db(database_path) as connection:
        source_id = upsert_source(
            connection,
            source_key="fixture-tiny-markdown",
            source_type="seed",
            title="Fixture Tiny Markdown Source",
            config_path="tests/fixture-tiny-markdown",
        )
        record_discovered_documents(connection, source_id=source_id, canonical_urls=[doc_url])
        row = connection.execute(
            "SELECT id FROM documents WHERE canonical_url = ?",
            (doc_url,),
        ).fetchone()
        assert row is not None
        update_document_fetch_state(
            connection,
            document_id=int(row["id"]),
            fetch_status="fetched",
            current_raw_path=str((Path("data") / "raw" / raw_relative_path).as_posix()),
        )

    result = run_extract(
        source_key="fixture-tiny-markdown",
        database_path=database_path,
        raw_dir=raw_dir,
        cleaned_dir=cleaned_dir,
        log_dir=log_dir,
    )

    assert result.status == "failed"
    assert result.extracted_count == 0
    assert result.failed_count == 1

    with connect_db(database_path) as connection:
        row = connection.execute(
            "SELECT extract_status, current_cleaned_path FROM documents WHERE canonical_url = ?",
            (doc_url,),
        ).fetchone()
        assert row is not None
        assert row["extract_status"] == "rejected_low_quality"
        assert row["current_cleaned_path"] is None


def test_run_extract_rejects_empty_markdown_raw_artifact_as_low_quality(tmp_path: Path) -> None:
    database_path = tmp_path / "extract-empty-markdown.sqlite3"
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    cleaned_dir = data_dir / "cleaned"
    log_dir = data_dir / "logs"

    init_db(database_path)

    doc_url = "https://docs.fixture.example/empty"
    raw_relative_path = Path("docs.fixture.example") / "empty.md"
    raw_path = raw_dir / raw_relative_path
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("\n\n   \n", encoding="utf-8")

    with connect_db(database_path) as connection:
        source_id = upsert_source(
            connection,
            source_key="fixture-empty-markdown",
            source_type="seed",
            title="Fixture Empty Markdown Source",
            config_path="tests/fixture-empty-markdown",
        )
        record_discovered_documents(connection, source_id=source_id, canonical_urls=[doc_url])
        row = connection.execute(
            "SELECT id FROM documents WHERE canonical_url = ?",
            (doc_url,),
        ).fetchone()
        assert row is not None
        update_document_fetch_state(
            connection,
            document_id=int(row["id"]),
            fetch_status="fetched",
            current_raw_path=str((Path("data") / "raw" / raw_relative_path).as_posix()),
        )

    result = run_extract(
        source_key="fixture-empty-markdown",
        database_path=database_path,
        raw_dir=raw_dir,
        cleaned_dir=cleaned_dir,
        log_dir=log_dir,
    )

    assert result.status == "failed"
    assert result.extracted_count == 0
    assert result.failed_count == 1

    with connect_db(database_path) as connection:
        row = connection.execute(
            "SELECT extract_status, current_cleaned_path FROM documents WHERE canonical_url = ?",
            (doc_url,),
        ).fetchone()
        assert row is not None
        assert row["extract_status"] == "rejected_low_quality"
        assert row["current_cleaned_path"] is None

    extract_log_path = log_dir / "extract-run-1.log"
    log_text = extract_log_path.read_text(encoding="utf-8")
    assert '"status": "rejected_low_quality"' in log_text
    assert "cleaned content is too short" in log_text
