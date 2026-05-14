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
    requeue_document_for_fetch,
    start_crawl_run,
    update_document_extract_state,
)


EXTRACT_RUN_KIND = "extract:trafilatura"
MIN_CLEANED_CHARS = 180
MIN_CLEANED_WORDS = 30
LOW_QUALITY_STATUS = "rejected_low_quality"
TEXTLIKE_SUFFIXES = {".md", ".markdown", ".mdown", ".rst", ".txt", ".text"}


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
    link_text_ratio: float = 0.0


class LowQualityContentError(ValueError):
    pass


class _FallbackHTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.ignored_depth = 0
        self.link_depth = 0
        self.title_chunks: list[str] = []
        self.text_chunks: list[str] = []
        self.link_text_length = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title":
            self.in_title = True
        if tag in {"script", "style", "noscript", "nav", "header", "footer", "aside"}:
            self.ignored_depth += 1
        if tag == "a":
            self.link_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in {"script", "style", "noscript", "nav", "header", "footer", "aside"} and self.ignored_depth:
            self.ignored_depth -= 1
        if tag == "a" and self.link_depth:
            self.link_depth -= 1

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
            if self.link_depth:
                self.link_text_length += len(value)

    def build_output(self) -> ExtractionOutput | None:
        cleaned_text = "\n\n".join(self.text_chunks).strip()
        if not cleaned_text:
            return None

        title = " ".join(self.title_chunks).strip() or None
        link_text_ratio = self.link_text_length / max(len(cleaned_text), 1)
        return ExtractionOutput(
            cleaned_text=cleaned_text,
            title=title,
            extractor_name="fallback_html",
            link_text_ratio=link_text_ratio,
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


def _looks_like_textlike(raw_bytes: bytes, current_raw_path: str) -> bool:
    if Path(current_raw_path).suffix.lower() in TEXTLIKE_SUFFIXES:
        return True
    if b"\x00" in raw_bytes[:4096]:
        return False

    sample = raw_bytes[:4096].decode("utf-8", errors="replace")
    if not sample.strip():
        return False
    printable_count = sum(1 for char in sample if char.isprintable() or char in "\r\n\t")
    return printable_count / max(len(sample), 1) > 0.85


def _clean_textlike_content(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    lines = [line.rstrip() for line in normalized.split("\n")]

    cleaned_lines: list[str] = []
    blank_count = 0
    for line in lines:
        if line.strip():
            blank_count = 0
            cleaned_lines.append(line)
            continue

        blank_count += 1
        if blank_count <= 2:
            cleaned_lines.append("")

    return "\n".join(cleaned_lines).strip()


def _title_from_textlike(cleaned_text: str) -> str | None:
    for line in cleaned_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or None
        if len(stripped) <= 120:
            return stripped
        return None
    return None


def _quality_check(output: ExtractionOutput) -> None:
    text = re.sub(r"\s+", " ", output.cleaned_text).strip()
    word_count = len(re.findall(r"\b\w+\b", text))
    if len(text) < MIN_CLEANED_CHARS or word_count < MIN_CLEANED_WORDS:
        raise LowQualityContentError(
            f"cleaned content is too short ({len(text)} chars, {word_count} words)"
        )
    if output.extractor_name == "fallback_html" and output.link_text_ratio > 0.45:
        raise LowQualityContentError(
            f"fallback output is nav-heavy ({output.link_text_ratio:.2f} link text ratio)"
        )


def extract_cleaned_content(raw_bytes: bytes, current_raw_path: str) -> ExtractionOutput:
    text = raw_bytes.decode("utf-8", errors="replace")
    is_html_like = _looks_like_html(raw_bytes, current_raw_path)
    if not is_html_like and _looks_like_textlike(raw_bytes, current_raw_path):
        output = ExtractionOutput(
            cleaned_text=_clean_textlike_content(text),
            title=None,
            extractor_name="textlike",
        )
        output = ExtractionOutput(
            cleaned_text=output.cleaned_text,
            title=_title_from_textlike(output.cleaned_text),
            extractor_name=output.extractor_name,
        )
        _quality_check(output)
        return output

    if not is_html_like and not text.strip():
        raise LowQualityContentError("textlike content is empty")

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
        output = ExtractionOutput(
            cleaned_text=extracted.strip(),
            title=metadata.title if metadata else None,
            author=metadata.author if metadata else None,
            published_at=metadata.date if metadata else None,
            extractor_name="trafilatura",
        )
        _quality_check(output)
        return output

    if is_html_like:
        parser = _FallbackHTMLTextParser()
        parser.feed(text)
        fallback_output = parser.build_output()
        if fallback_output is not None:
            _quality_check(fallback_output)
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
        except FileNotFoundError as exc:
            failed_count += 1
            error_text = f"{canonical_url}: raw artifact missing; queued for refetch: {current_raw_path}"
            errors.append(error_text)

            with connect_db(db_path) as connection:
                requeue_document_for_fetch(connection, document_id=document_id)

            _append_log(
                log_path,
                {
                    "url": canonical_url,
                    "document_id": document_id,
                    "status": "raw_missing_requeued",
                    "raw_path": current_raw_path,
                    "error": str(exc),
                },
            )
        except LowQualityContentError as exc:
            failed_count += 1
            error_text = f"{canonical_url}: {exc}"
            errors.append(error_text)

            with connect_db(db_path) as connection:
                update_document_extract_state(
                    connection,
                    document_id=document_id,
                    extract_status=LOW_QUALITY_STATUS,
                )

            _append_log(
                log_path,
                {
                    "url": canonical_url,
                    "document_id": document_id,
                    "status": LOW_QUALITY_STATUS,
                    "error": str(exc),
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
