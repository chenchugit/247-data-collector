from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import threading

from app.db import connect_db
from app.runtime import run_pipeline_once


class _FetchFixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/article":
            body = (
                b"<html><head><title>Fixture Article</title></head>"
                b"<body><article><h1>Fixture Article</h1><p>runtime smoke body</p></article></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return


class _OllamaFixtureHandler(BaseHTTPRequestHandler):
    request_payloads: list[dict[str, object]] = []

    def do_POST(self) -> None:
        if self.path != "/api/generate":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        self.__class__.request_payloads.append(payload)

        response_body = json.dumps({"response": "summary draft from fixture"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format: str, *args) -> None:
        return


def test_run_pipeline_once_orchestrates_existing_stages_and_is_repeatable(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    cleaned_dir = data_dir / "cleaned"
    derived_dir = data_dir / "derived"
    log_dir = data_dir / "logs"
    config_dir = tmp_path / "config"
    sources_dir = config_dir / "sources"
    prompts_dir = config_dir / "prompts"
    sources_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)

    fetch_server = ThreadingHTTPServer(("127.0.0.1", 0), _FetchFixtureHandler)
    fetch_thread = threading.Thread(target=fetch_server.serve_forever, daemon=True)
    fetch_thread.start()

    ollama_server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaFixtureHandler)
    ollama_thread = threading.Thread(target=ollama_server.serve_forever, daemon=True)
    ollama_thread.start()
    _OllamaFixtureHandler.request_payloads.clear()

    try:
        base_fetch_url = f"http://127.0.0.1:{fetch_server.server_port}"
        config_path = sources_dir / "runtime_sources.toml"
        config_path.write_text(
            "\n".join(
                [
                    "[[sources]]",
                    'source_key = "runtime-seed"',
                    'source_type = "seed"',
                    'title = "Runtime Seed"',
                    "enabled = true",
                    f'seeds = ["{base_fetch_url}/article"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        prompt_path = prompts_dir / "summary_draft_runtime.txt"
        prompt_path.write_text(
            "Create a short summary.\n\n{{ cleaned_content }}\n",
            encoding="utf-8",
        )

        first_result = run_pipeline_once(
            config_path=config_path,
            database_path=database_path,
            raw_dir=raw_dir,
            cleaned_dir=cleaned_dir,
            derived_dir=derived_dir,
            log_dir=log_dir,
            prompt_path=prompt_path,
            model_name="fixture-model",
            base_url=f"http://127.0.0.1:{ollama_server.server_port}",
            timeout_seconds=5,
        )
        second_result = run_pipeline_once(
            config_path=config_path,
            database_path=database_path,
            raw_dir=raw_dir,
            cleaned_dir=cleaned_dir,
            derived_dir=derived_dir,
            log_dir=log_dir,
            prompt_path=prompt_path,
            model_name="fixture-model",
            base_url=f"http://127.0.0.1:{ollama_server.server_port}",
            timeout_seconds=5,
        )

        assert first_result.status == "success"
        assert first_result.source_count == 1
        assert first_result.failed_source_count == 0
        assert len(first_result.sources) == 1
        assert first_result.sources[0].source_key == "runtime-seed"
        assert [stage.stage_name for stage in first_result.sources[0].stages] == [
            "fetch",
            "extract",
            "summary_draft",
        ]
        assert all(stage.status == "success" for stage in first_result.sources[0].stages)
        assert second_result.status == "success"
        assert second_result.source_count == 1
        assert second_result.failed_source_count == 0
        assert len(_OllamaFixtureHandler.request_payloads) == 2
        assert "runtime smoke body" in str(_OllamaFixtureHandler.request_payloads[0]["prompt"])
        assert "runtime smoke body" in str(_OllamaFixtureHandler.request_payloads[1]["prompt"])

        first_runtime_log = tmp_path / Path(first_result.log_path)
        second_runtime_log = tmp_path / Path(second_result.log_path)
        assert first_runtime_log.exists()
        assert second_runtime_log.exists()
        first_runtime_log_text = first_runtime_log.read_text(encoding="utf-8")
        assert '"event": "stage_started"' in first_runtime_log_text
        assert '"stage_name": "fetch"' in first_runtime_log_text
        assert '"stage_name": "extract"' in first_runtime_log_text
        assert '"stage_name": "summary_draft"' in first_runtime_log_text
        assert '"event": "runtime_finished"' in first_runtime_log_text

        with connect_db(database_path) as connection:
            document_row = connection.execute(
                """
                SELECT canonical_url, fetch_status, extract_status, current_raw_path, current_cleaned_path
                FROM documents
                WHERE canonical_url = ?
                """,
                (f"{base_fetch_url}/article",),
            ).fetchone()
            assert document_row is not None
            assert document_row["fetch_status"] == "fetched"
            assert document_row["extract_status"] == "extracted"
            assert document_row["current_raw_path"] is not None
            assert document_row["current_cleaned_path"] is not None

            crawl_runs = connection.execute(
                """
                SELECT run_kind, status, discovered_count, fetched_count, extracted_count, log_path
                FROM crawl_runs
                ORDER BY id
                """
            ).fetchall()
            assert [row["run_kind"] for row in crawl_runs] == [
                "discovery:seed",
                "fetch:http",
                "extract:trafilatura",
                "analyze:summary_draft",
                "discovery:seed",
                "fetch:http",
                "extract:trafilatura",
                "analyze:summary_draft",
            ]
            assert all(row["status"] == "success" for row in crawl_runs)
            assert crawl_runs[0]["discovered_count"] == 1
            assert crawl_runs[1]["fetched_count"] == 1
            assert crawl_runs[2]["extracted_count"] == 1
            assert crawl_runs[3]["extracted_count"] == 1
            assert crawl_runs[5]["fetched_count"] == 0
            assert crawl_runs[6]["extracted_count"] == 0
            assert crawl_runs[7]["extracted_count"] == 1
            assert crawl_runs[1]["log_path"] == "data/logs/fetch-run-2.log"
            assert crawl_runs[2]["log_path"] == "data/logs/extract-run-3.log"
            assert crawl_runs[3]["log_path"] == "data/logs/summary-draft-run-4.log"

            versions = connection.execute(
                """
                SELECT version_kind, model_name, prompt_name, file_path
                FROM document_versions
                ORDER BY id
                """
            ).fetchall()
            assert len(versions) == 1
            assert versions[0]["version_kind"] == "summary_draft"
            assert versions[0]["model_name"] == "fixture-model"
            assert versions[0]["prompt_name"] == "summary_draft_runtime"
            assert str(versions[0]["file_path"]).startswith("data/derived/")

        fetch_log_path = log_dir / "fetch-run-2.log"
        extract_log_path = log_dir / "extract-run-3.log"
        analysis_log_path = log_dir / "summary-draft-run-4.log"
        assert fetch_log_path.exists()
        assert extract_log_path.exists()
        assert analysis_log_path.exists()
    finally:
        fetch_server.shutdown()
        fetch_server.server_close()
        fetch_thread.join(timeout=5)
        ollama_server.shutdown()
        ollama_server.server_close()
        ollama_thread.join(timeout=5)
