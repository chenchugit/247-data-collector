from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
import json
import re

import trafilatura
from trafilatura.metadata import extract_metadata

from .config import load_settings
from .db import (
    connect_db,
    finish_crawl_run,
    get_source_id_by_key,
    init_db,
    list_documents_for_extract,
    start_crawl_run,
    update_document_extract_state,
)


EXTRACT_RUN_KIND = "extract:trafilatura"


@dataclass(frozen=True)
class ExtractRunResult:
    source_key: str
    crawl_run_id: int
    extracted_count: int
    failed_count: int
    log_path: str
    status: str


@dataclass(frozen=True)
class ExtractionOutput:
    cleaned_text: str
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    extractor_name: str = "trafilatura"


class _FallbackHTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.ignored_depth = 0
        self.title_chunks: list[str] = []
        self.text_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title":
            self.in_title = True
        if tag in {"script", "style", "noscript"}:
            self.ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in {"script", "style", "noscript"} and self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return

        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return

        if self.in_title:
            self.title_chunks.append(value)
        else:
            self.text_chunks.append(value)

    def build_output(self) -> ExtractionOutput | None:
        cleaned_text = "\n\n".join(self.text_chunks).strip()
        if not cleaned_text:
            return None

        title = " ".join(self.title_chunks).strip() or None
        return ExtractionOutput(
            cleaned_text=cleaned_text,
            title=title,
            extractor_name="fallback_html",
        )


def _sanitize_path_part(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return sanitized.strip("-") or "index"


def build_cleaned_artifact_relative_path(url: str) -> Path:
    parts = urlsplit(url)
    host_part = _sanitize_path_part(parts.netloc.lower())
    path_part = _sanitize_path_part(parts.path.strip("/"))
    name_seed = path_part if path_part != "index" else "index"
    digest = sha256(url.encode("utf-8")).hexdigest()[:12]
    return Path(host_part) / f"{name_seed}-{digest}.md"


def _append_log(log_path: Path, payload: dict[str, str | int]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def resolve_artifact_path(root_dir: Path, stored_path: str) -> Path:
    candidate = Path(stored_path)
    if candidate.is_absolute():
        return candidate

    parts = candidate.parts
    if len(parts) >= 3 and parts[0] == "data":
        return root_dir / Path(*parts[2:])
    return root_dir / candidate


def _looks_like_html(raw_bytes: bytes, current_raw_path: str) -> bool:
    if Path(current_raw_path).suffix.lower() in {".html", ".htm", ".xhtml"}:
        return True

    sample = raw_bytes[:512].lower()
    return b"<html" in sample or b"<!doctype html" in sample


def extract_cleaned_content(raw_bytes: bytes, current_raw_path: str) -> ExtractionOutput:
    text = raw_bytes.decode("utf-8", errors="replace")
    extracted = trafilatura.extract(
        text,
        output_format="markdown",
        include_formatting=True,
        include_links=False,
        include_images=False,
        favor_precision=True,
    )
    if extracted:
        metadata = extract_metadata(text)
        return ExtractionOutput(
            cleaned_text=extracted.strip(),
            title=metadata.title if metadata else None,
            author=metadata.author if metadata else None,
            published_at=metadata.date if metadata else None,
            extractor_name="trafilatura",
        )

    if _looks_like_html(raw_bytes, current_raw_path):
        parser = _FallbackHTMLTextParser()
        parser.feed(text)
        fallback_output = parser.build_output()
        if fallback_output is not None:
            return fallback_output

    raise ValueError("extractor produced no cleaned content")


def run_extract(
    *,
    source_key: str,
    database_path: Path | None = None,
    raw_dir: Path | None = None,
    cleaned_dir: Path | None = None,
    log_dir: Path | None = None,
) -> ExtractRunResult:
    settings = load_settings()
    db_path = init_db(database_path)
    raw_root = Path(raw_dir or settings.raw_dir)
    cleaned_root = Path(cleaned_dir or settings.cleaned_dir)
    logs_root = Path(log_dir or settings.log_dir)
    cleaned_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)

    with connect_db(db_path) as connection:
        source_id = get_source_id_by_key(connection, source_key)
        if source_id is None:
            raise ValueError(f"unknown source_key: {source_key}")
        documents = [
            {
                "id": int(row["id"]),
                "canonical_url": str(row["canonical_url"]),
                "current_raw_path": str(row["current_raw_path"]),
            }
            for row in list_documents_for_extract(connection, source_id=source_id)
        ]
        crawl_run_id = start_crawl_run(
            connection,
            source_id=source_id,
            run_kind=EXTRACT_RUN_KIND,
        )

    log_path = logs_root / f"extract-run-{crawl_run_id}.log"
    _append_log(
        log_path,
        {
            "event": "run_started",
            "run_kind": EXTRACT_RUN_KIND,
            "crawl_run_id": crawl_run_id,
            "source_key": source_key,
            "status": "running",
        },
    )
    extracted_count = 0
    failed_count = 0
    errors: list[str] = []

    for document in documents:
        document_id = int(document["id"])
        canonical_url = str(document["canonical_url"])
        current_raw_path = str(document["current_raw_path"])
        raw_path = resolve_artifact_path(raw_root, current_raw_path)

        try:
            raw_bytes = raw_path.read_bytes()
            output = extract_cleaned_content(raw_bytes, current_raw_path)

            relative_cleaned_path = build_cleaned_artifact_relative_path(canonical_url)
            cleaned_path = cleaned_root / relative_cleaned_path
            cleaned_path.parent.mkdir(parents=True, exist_ok=True)
            cleaned_path.write_text(output.cleaned_text.rstrip() + "\n", encoding="utf-8")

            stored_cleaned_path = str((Path("data") / "cleaned" / relative_cleaned_path).as_posix())
            with connect_db(db_path) as connection:
                update_document_extract_state(
                    connection,
                    document_id=document_id,
                    extract_status="extracted",
                    current_cleaned_path=stored_cleaned_path,
                    title=output.title,
                    author=output.author,
                    published_at=output.published_at,
                )

            extracted_count += 1
            _append_log(
                log_path,
                {
                    "url": canonical_url,
                    "document_id": document_id,
                    "status": "extracted",
                    "cleaned_path": stored_cleaned_path,
                    "extractor": output.extractor_name,
                },
            )
        except Exception as exc:
            failed_count += 1
            error_text = f"{canonical_url}: {exc}"
            errors.append(error_text)

            with connect_db(db_path) as connection:
                update_document_extract_state(
                    connection,
                    document_id=document_id,
                    extract_status="extract_failed",
                )

            _append_log(
                log_path,
                {
                    "url": canonical_url,
                    "document_id": document_id,
                    "status": "extract_failed",
                    "error": str(exc),
                },
            )

    if failed_count:
        status = "partial_failure" if extracted_count else "failed"
    else:
        status = "success"

    error_message = "; ".join(errors) if errors else None
    relative_log_path = (Path("data") / "logs" / log_path.name).as_posix()
    _append_log(
        log_path,
        {
            "event": "run_finished",
            "run_kind": EXTRACT_RUN_KIND,
            "crawl_run_id": crawl_run_id,
            "source_key": source_key,
            "status": status,
            "extracted_count": extracted_count,
            "failed_count": failed_count,
            "log_path": relative_log_path,
            "error": error_message or "",
        },
    )

    with connect_db(db_path) as connection:
        finish_crawl_run(
            connection,
            run_id=crawl_run_id,
            status=status,
            extracted_count=extracted_count,
            error_message=error_message,
            log_path=relative_log_path,
        )

    return ExtractRunResult(
        source_key=source_key,
        crawl_run_id=crawl_run_id,
        extracted_count=extracted_count,
        failed_count=failed_count,
        log_path=relative_log_path,
        status=status,
    )
