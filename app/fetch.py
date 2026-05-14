from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from shutil import which
from urllib.parse import urlsplit
import json
import mimetypes
import os
import re

import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy_playwright.page import PageMethod

from .config import load_settings
from .db import (
    connect_db,
    finish_crawl_run,
    get_source_id_by_key,
    init_db,
    list_documents_for_fetch,
    start_crawl_run,
    update_document_fetch_state,
)


FETCH_RUN_KIND = "fetch:http"
BROWSER_ESCALATION_HEADER = b"x-auto-scrapy-requires-browser"
BROWSER_ESCALATION_MARKER = '<meta name="auto-scrapy-requires-browser" content="1">'
BROWSER_READY_SELECTOR = "#hydrated"
FETCH_DOWNLOAD_TIMEOUT_SECONDS = 30
FETCH_RETRY_TIMES = 1
FETCH_RETRY_HTTP_CODES = [408, 429, 500, 502, 503, 504]


@dataclass(frozen=True)
class FetchRunResult:
    source_key: str
    crawl_run_id: int
    fetched_count: int
    failed_count: int
    needs_browser_count: int
    browser_fetched_count: int
    log_path: str
    status: str


def _sanitize_path_part(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return sanitized.strip("-") or "index"


def _guess_raw_extension(content_type: str, url: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    if content_type == "text/html":
        return ".html"

    guessed = mimetypes.guess_extension(content_type) or ""
    if guessed:
        return guessed

    suffix = Path(urlsplit(url).path).suffix
    if suffix:
        return suffix

    return ".bin"


def build_raw_artifact_relative_path(url: str, content_type: str) -> Path:
    parts = urlsplit(url)
    host_part = _sanitize_path_part(parts.netloc.lower())
    path_part = _sanitize_path_part(parts.path.strip("/"))
    name_seed = path_part if path_part != "index" else "index"
    digest = sha256(url.encode("utf-8")).hexdigest()[:12]
    extension = _guess_raw_extension(content_type, url)
    return Path(host_part) / f"{name_seed}-{digest}{extension}"


def should_escalate_to_browser(response: scrapy.http.Response) -> bool:
    header_value = response.headers.get(BROWSER_ESCALATION_HEADER, b"").strip().lower()
    if header_value == b"1":
        return True

    content_type = response.headers.get(b"content-type", b"").decode("latin-1").lower()
    if "text/html" not in content_type:
        return False

    return BROWSER_ESCALATION_MARKER in response.text.lower()


def _detect_playwright_channel() -> str | None:
    override = os.environ.get("AUTO_SCRAPY_PLAYWRIGHT_CHANNEL", "").strip()
    if override:
        return override

    candidates = [
        (
            "msedge",
            [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ],
        ),
        (
            "chrome",
            [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ],
        ),
    ]

    for channel, known_paths in candidates:
        if which(channel):
            return channel
        if any(Path(path).exists() for path in known_paths):
            return channel
    return None


def _build_crawler_settings() -> dict[str, object]:
    settings: dict[str, object] = {
        "LOG_ENABLED": False,
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "PLAYWRIGHT_BROWSER_TYPE": "chromium",
        "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": 10000,
    }

    channel = _detect_playwright_channel()
    if channel is not None:
        settings["PLAYWRIGHT_LAUNCH_OPTIONS"] = {
            "headless": True,
            "channel": channel,
        }

    return settings


def _append_log(log_path: Path, payload: dict[str, str | int]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _persist_raw_artifact(
    *,
    response: scrapy.http.Response,
    raw_dir: Path,
) -> str:
    content_type = response.headers.get(b"content-type", b"application/octet-stream").decode(
        "latin-1"
    )
    relative_raw_path = build_raw_artifact_relative_path(response.url, content_type)
    raw_path = raw_dir / relative_raw_path
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(response.body)
    return str((Path("data") / "raw" / relative_raw_path).as_posix())


class RawFetchSpider(scrapy.Spider):
    name = "raw_fetch"
    custom_settings = {
        "LOG_ENABLED": False,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": FETCH_RETRY_TIMES,
        "RETRY_HTTP_CODES": FETCH_RETRY_HTTP_CODES,
        "DOWNLOAD_TIMEOUT": FETCH_DOWNLOAD_TIMEOUT_SECONDS,
        "USER_AGENT": "auto-scrapy/0.1",
    }

    def __init__(
        self,
        *,
        documents: list[dict[str, int | str]],
        database_path: str,
        raw_dir: str,
        log_path: str,
        run_stats: dict[str, object],
    ) -> None:
        super().__init__()
        self.documents = documents
        self.database_path = Path(database_path)
        self.raw_dir = Path(raw_dir)
        self.log_path = Path(log_path)
        self.run_stats = run_stats

    def start_requests(self):
        for document in self.documents:
            yield scrapy.Request(
                url=str(document["canonical_url"]),
                callback=self.parse_document,
                errback=self.handle_failure,
                dont_filter=True,
                meta={
                    "document_id": int(document["id"]),
                    "fetch_method": "http",
                },
            )

    def parse_document(self, response: scrapy.http.Response):
        document_id = int(response.meta["document_id"])
        fetch_method = str(response.meta.get("fetch_method", "http"))

        if fetch_method == "http" and should_escalate_to_browser(response):
            self.run_stats["needs_browser_count"] = int(self.run_stats["needs_browser_count"]) + 1

            with connect_db(self.database_path) as connection:
                update_document_fetch_state(
                    connection,
                    document_id=document_id,
                    fetch_status="needs_browser",
                )

            _append_log(
                self.log_path,
                {
                    "url": response.url,
                    "document_id": document_id,
                    "status": "needs_browser",
                    "fetch_method": "http",
                },
            )

            yield response.request.replace(
                callback=self.parse_document,
                errback=self.handle_failure,
                dont_filter=True,
                meta={
                    **response.meta,
                    "fetch_method": "browser",
                    "playwright": True,
                    "playwright_include_page": False,
                    "playwright_page_methods": [
                        PageMethod("wait_for_load_state", "load"),
                        PageMethod("wait_for_selector", BROWSER_READY_SELECTOR),
                    ],
                },
            )
            return

        current_raw_path = _persist_raw_artifact(response=response, raw_dir=self.raw_dir)
        self.run_stats["fetched_count"] = int(self.run_stats["fetched_count"]) + 1
        if fetch_method == "browser":
            self.run_stats["browser_fetched_count"] = (
                int(self.run_stats["browser_fetched_count"]) + 1
            )

        with connect_db(self.database_path) as connection:
            update_document_fetch_state(
                connection,
                document_id=document_id,
                fetch_status="fetched",
                current_raw_path=current_raw_path,
            )

        _append_log(
            self.log_path,
            {
                "url": response.url,
                "document_id": document_id,
                "status": "fetched",
                "raw_path": current_raw_path,
                "fetch_method": fetch_method,
            },
        )

    def handle_failure(self, failure):
        request = failure.request
        document_id = int(request.meta["document_id"])
        error_text = str(failure.value)
        fetch_method = str(request.meta.get("fetch_method", "http"))

        with connect_db(self.database_path) as connection:
            update_document_fetch_state(
                connection,
                document_id=document_id,
                fetch_status="fetch_failed",
            )

        self.run_stats["failed_count"] = int(self.run_stats["failed_count"]) + 1
        errors = list(self.run_stats["errors"])
        errors.append(f"{request.url}: {error_text}")
        self.run_stats["errors"] = errors

        _append_log(
            self.log_path,
            {
                "url": request.url,
                "document_id": document_id,
                "status": "fetch_failed",
                "error": error_text,
                "fetch_method": fetch_method,
            },
        )


def run_fetch(
    *,
    source_key: str,
    database_path: Path | None = None,
    raw_dir: Path | None = None,
    log_dir: Path | None = None,
) -> FetchRunResult:
    settings = load_settings()
    db_path = init_db(database_path)
    raw_root = Path(raw_dir or settings.raw_dir)
    logs_root = Path(log_dir or settings.log_dir)
    raw_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)

    with connect_db(db_path) as connection:
        source_id = get_source_id_by_key(connection, source_key)
        if source_id is None:
            raise ValueError(f"unknown source_key: {source_key}")
        documents = [
            {"id": int(row["id"]), "canonical_url": str(row["canonical_url"])}
            for row in list_documents_for_fetch(connection, source_id=source_id)
        ]
        crawl_run_id = start_crawl_run(
            connection,
            source_id=source_id,
            run_kind=FETCH_RUN_KIND,
        )

    log_path = logs_root / f"fetch-run-{crawl_run_id}.log"
    _append_log(
        log_path,
        {
            "event": "run_started",
            "run_kind": FETCH_RUN_KIND,
            "crawl_run_id": crawl_run_id,
            "source_key": source_key,
            "status": "running",
        },
    )
    run_stats: dict[str, object] = {
        "fetched_count": 0,
        "failed_count": 0,
        "needs_browser_count": 0,
        "browser_fetched_count": 0,
        "errors": [],
    }

    if documents:
        process = CrawlerProcess(settings=_build_crawler_settings())
        process.crawl(
            RawFetchSpider,
            documents=documents,
            database_path=str(db_path),
            raw_dir=str(raw_root),
            log_path=str(log_path),
            run_stats=run_stats,
        )
        process.start()

    fetched_count = int(run_stats["fetched_count"])
    failed_count = int(run_stats["failed_count"])
    needs_browser_count = int(run_stats["needs_browser_count"])
    browser_fetched_count = int(run_stats["browser_fetched_count"])
    errors = list(run_stats["errors"])

    if failed_count:
        status = "partial_failure" if fetched_count or needs_browser_count else "failed"
    else:
        status = "success"

    error_message = "; ".join(str(item) for item in errors) if errors else None
    relative_log_path = (Path("data") / "logs" / log_path.name).as_posix()
    _append_log(
        log_path,
        {
            "event": "run_finished",
            "run_kind": FETCH_RUN_KIND,
            "crawl_run_id": crawl_run_id,
            "source_key": source_key,
            "status": status,
            "fetched_count": fetched_count,
            "failed_count": failed_count,
            "needs_browser_count": needs_browser_count,
            "browser_fetched_count": browser_fetched_count,
            "log_path": relative_log_path,
            "error": error_message or "",
        },
    )

    with connect_db(db_path) as connection:
        finish_crawl_run(
            connection,
            run_id=crawl_run_id,
            status=status,
            fetched_count=fetched_count,
            error_message=error_message,
            log_path=relative_log_path,
        )

    return FetchRunResult(
        source_key=source_key,
        crawl_run_id=crawl_run_id,
        fetched_count=fetched_count,
        failed_count=failed_count,
        needs_browser_count=needs_browser_count,
        browser_fetched_count=browser_fetched_count,
        log_path=relative_log_path,
        status=status,
    )
