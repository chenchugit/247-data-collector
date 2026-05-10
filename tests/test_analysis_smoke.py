from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import threading

from app.analysis import run_summary_draft
from app.db import (
    connect_db,
    init_db,
    list_document_versions,
    record_discovered_documents,
    update_document_extract_state,
    upsert_source,
)
from app.extract import build_cleaned_artifact_relative_path


def test_run_summary_draft_reads_cleaned_content_calls_ollama_and_records_failure(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "analysis.sqlite3"
    data_dir = tmp_path / "data"
    cleaned_dir = data_dir / "cleaned"
    derived_dir = data_dir / "derived"
    log_dir = data_dir / "logs"
    prompt_path = tmp_path / "summary_draft_fixture.txt"

    init_db(database_path)

    article_url = "https://fixture.example/article"
    missing_url = "https://fixture.example/missing-cleaned"
    cleaned_relative_path = build_cleaned_artifact_relative_path(article_url)
    cleaned_path = cleaned_dir / cleaned_relative_path
    cleaned_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned_path.write_text("# Fixture Article\n\nUseful cleaned content.\n", encoding="utf-8")
    prompt_path.write_text(
        "Write a short summary draft.\n\n{{ cleaned_content }}\n",
        encoding="utf-8",
    )

    with connect_db(database_path) as connection:
        source_id = upsert_source(
            connection,
            source_key="fixture-summary",
            source_type="seed",
            title="Fixture Summary Source",
            config_path="tests/fixture-summary",
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

        update_document_extract_state(
            connection,
            document_id=int(article_row["id"]),
            extract_status="extracted",
            current_cleaned_path=str((Path("data") / "cleaned" / cleaned_relative_path).as_posix()),
            title="Fixture Article",
        )
        update_document_extract_state(
            connection,
            document_id=int(missing_row["id"]),
            extract_status="extracted",
            current_cleaned_path="data/cleaned/fixture/missing-summary.md",
            title="Missing Summary Fixture",
        )

    captured_requests: list[dict[str, object]] = []

    class _OllamaHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            captured_requests.append(payload)
            body = json.dumps({"response": "# Summary Draft\n\n- Useful cleaned content.\n"}).encode(
                "utf-8"
            )

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        result = run_summary_draft(
            source_key="fixture-summary",
            database_path=database_path,
            cleaned_dir=cleaned_dir,
            derived_dir=derived_dir,
            log_dir=log_dir,
            prompt_path=prompt_path,
            model_name="fixture-model",
            base_url=f"http://127.0.0.1:{server.server_port}",
            timeout_seconds=5,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.source_key == "fixture-summary"
    assert result.generated_count == 1
    assert result.failed_count == 1
    assert result.status == "partial_failure"
    assert result.log_path == "data/logs/summary-draft-run-1.log"
    assert result.model_name == "fixture-model"
    assert result.prompt_name == "summary_draft_fixture"
    assert result.version_kind == "summary_draft"

    assert len(captured_requests) == 1
    request_payload = captured_requests[0]
    assert request_payload["model"] == "fixture-model"
    assert request_payload["stream"] is False
    assert "Useful cleaned content." in str(request_payload["prompt"])

    with connect_db(database_path) as connection:
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

        article_versions = list_document_versions(connection, document_id=int(article_row["id"]))
        missing_versions = list_document_versions(connection, document_id=int(missing_row["id"]))
        assert len(article_versions) == 1
        assert len(missing_versions) == 0

        version_row = article_versions[0]
        assert version_row["version_kind"] == "summary_draft"
        assert version_row["model_name"] == "fixture-model"
        assert version_row["prompt_name"] == "summary_draft_fixture"
        assert str(version_row["file_path"]).startswith("data/derived/fixture.example/article/")

        crawl_run = connection.execute(
            """
            SELECT run_kind, status, extracted_count, error_message, log_path
            FROM crawl_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert crawl_run is not None
        assert crawl_run["run_kind"] == "analyze:summary_draft"
        assert crawl_run["status"] == "partial_failure"
        assert crawl_run["extracted_count"] == 1
        assert crawl_run["log_path"] == "data/logs/summary-draft-run-1.log"
        assert "missing-cleaned" in str(crawl_run["error_message"])

    derived_path = tmp_path / Path(str(version_row["file_path"]))
    assert derived_path.exists()
    assert derived_path.read_text(encoding="utf-8") == "# Summary Draft\n\n- Useful cleaned content.\n"

    log_text = (log_dir / "summary-draft-run-1.log").read_text(encoding="utf-8")
    assert '"event": "run_started"' in log_text
    assert '"status": "generated"' in log_text
    assert '"status": "generate_failed"' in log_text
    assert '"event": "run_finished"' in log_text
