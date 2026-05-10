from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

from app.db import connect_db, init_db, record_discovered_documents, upsert_source
from app.fetch import run_fetch


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/static":
            body = b"<html><body><article>static page</article></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/needs-browser":
            body = (
                b'<html><head><meta name="auto-scrapy-requires-browser" content="1"></head>'
                b"<body><div id='app'></div>"
                b"<script>"
                b"window.addEventListener('DOMContentLoaded', function () {"
                b"document.getElementById('app').innerHTML = \"<article id='hydrated'>browser page</article>\";"
                b"});"
                b"</script></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/broken":
            body = b"server error"
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return


def test_run_fetch_persists_raw_updates_fetch_status_and_marks_browser_escalation(tmp_path: Path) -> None:
    database_path = tmp_path / "fetch.sqlite3"
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    log_dir = data_dir / "logs"

    init_db(database_path)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        base_url = f"http://127.0.0.1:{server.server_port}"

        with connect_db(database_path) as connection:
            source_id = upsert_source(
                connection,
                source_key="fixture-fetch",
                source_type="seed",
                title="Fixture Fetch Source",
                config_path="tests/fixture-fetch",
            )
            inserted = record_discovered_documents(
                connection,
                source_id=source_id,
                canonical_urls=[
                    f"{base_url}/static",
                    f"{base_url}/needs-browser",
                    f"{base_url}/broken",
                ],
            )
            assert inserted == 3

        result = run_fetch(
            source_key="fixture-fetch",
            database_path=database_path,
            raw_dir=raw_dir,
            log_dir=log_dir,
        )

        assert result.source_key == "fixture-fetch"
        assert result.fetched_count == 2
        assert result.needs_browser_count == 1
        assert result.browser_fetched_count == 1
        assert result.failed_count == 1
        assert result.status == "partial_failure"
        assert result.log_path == "data/logs/fetch-run-1.log"

        with connect_db(database_path) as connection:
            document_rows = connection.execute(
                """
                SELECT canonical_url, fetch_status, current_raw_path
                FROM documents
                ORDER BY canonical_url
                """
            ).fetchall()
            fetch_map = {row["canonical_url"]: row for row in document_rows}

            assert fetch_map[f"{base_url}/static"]["fetch_status"] == "fetched"
            assert fetch_map[f"{base_url}/needs-browser"]["fetch_status"] == "fetched"
            assert fetch_map[f"{base_url}/broken"]["fetch_status"] == "fetch_failed"

            static_raw = fetch_map[f"{base_url}/static"]["current_raw_path"]
            browser_raw = fetch_map[f"{base_url}/needs-browser"]["current_raw_path"]
            failed_raw = fetch_map[f"{base_url}/broken"]["current_raw_path"]

            assert static_raw is not None
            assert browser_raw is not None
            assert failed_raw is None

            static_raw_path = tmp_path / Path(static_raw)
            browser_raw_path = tmp_path / Path(browser_raw)
            assert static_raw_path.exists()
            assert browser_raw_path.exists()
            assert static_raw_path.read_text(encoding="utf-8") == "<html><body><article>static page</article></body></html>"
            browser_raw_text = browser_raw_path.read_text(encoding="utf-8")
            assert "browser page" in browser_raw_text
            assert 'id="hydrated"' in browser_raw_text

            crawl_run = connection.execute(
                """
                SELECT run_kind, status, fetched_count, error_message, log_path
                FROM crawl_runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            assert crawl_run is not None
            assert crawl_run["run_kind"] == "fetch:http"
            assert crawl_run["status"] == "partial_failure"
            assert crawl_run["fetched_count"] == 2
            assert crawl_run["log_path"] == "data/logs/fetch-run-1.log"
            assert "/broken" in crawl_run["error_message"]

        log_path = log_dir / "fetch-run-1.log"
        assert log_path.exists()
        log_text = log_path.read_text(encoding="utf-8")
        assert '"event": "run_started"' in log_text
        assert '"status": "fetched"' in log_text
        assert '"status": "needs_browser"' in log_text
        assert '"status": "fetch_failed"' in log_text
        assert '"event": "run_finished"' in log_text
        assert '"fetch_method": "http"' in log_text
        assert '"fetch_method": "browser"' in log_text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
