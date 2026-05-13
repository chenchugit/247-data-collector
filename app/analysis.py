from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib import error, request
import json

from .config import load_settings
from .db import (
    connect_db,
    finish_crawl_run,
    get_source_id_by_key,
    get_document_version_for_source_content,
    init_db,
    list_documents_for_analysis,
    start_crawl_run,
)
from .versioning import persist_document_version, resolve_artifact_path


SUMMARY_DRAFT_VERSION_KIND = "summary_draft"
SUMMARY_DRAFT_RUN_KIND = "analyze:summary_draft"


@dataclass(frozen=True)
class SummaryDraftRunResult:
    source_key: str
    crawl_run_id: int
    generated_count: int
    failed_count: int
    skipped_count: int
    log_path: str
    status: str
    model_name: str
    prompt_name: str
    version_kind: str = SUMMARY_DRAFT_VERSION_KIND


def _append_log(log_path: Path, payload: dict[str, str | int]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _load_prompt_template(prompt_path: Path) -> str:
    template = prompt_path.read_text(encoding="utf-8").strip()
    if "{{ cleaned_content }}" not in template:
        raise ValueError(
            f"prompt template must contain '{{{{ cleaned_content }}}}': {prompt_path}"
        )
    return template


def _render_prompt(template: str, *, cleaned_content: str) -> str:
    normalized_content = cleaned_content.strip()
    if not normalized_content:
        raise ValueError("cleaned artifact is empty")
    return template.replace("{{ cleaned_content }}", normalized_content)


def generate_summary_draft_with_ollama(
    *,
    cleaned_text: str,
    model_name: str,
    prompt_template: str,
    base_url: str,
    timeout_seconds: int,
) -> str:
    prompt = _render_prompt(prompt_template, cleaned_content=cleaned_text)
    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
    }
    endpoint = base_url.rstrip("/") + "/api/generate"
    req = request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ollama request failed with HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"ollama request failed: {exc.reason}") from exc

    parsed = json.loads(body)
    response_text = parsed.get("response")
    if not isinstance(response_text, str) or not response_text.strip():
        raise ValueError("ollama returned an empty response")
    return response_text.strip() + "\n"


def run_summary_draft(
    *,
    source_key: str,
    database_path: Path | None = None,
    cleaned_dir: Path | None = None,
    derived_dir: Path | None = None,
    log_dir: Path | None = None,
    prompt_path: Path | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    timeout_seconds: int | None = None,
    max_documents: int | None = None,
    generate_summary=generate_summary_draft_with_ollama,
) -> SummaryDraftRunResult:
    settings = load_settings()
    db_path = init_db(database_path)
    cleaned_root = Path(cleaned_dir or settings.cleaned_dir)
    derived_root = Path(derived_dir or settings.derived_dir)
    logs_root = Path(log_dir or settings.log_dir)
    prompt_file = Path(prompt_path or settings.summary_draft_prompt_path)
    active_model_name = model_name or settings.ollama_model
    active_base_url = base_url or settings.ollama_base_url
    active_timeout_seconds = timeout_seconds or settings.ollama_timeout_seconds

    derived_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)
    prompt_template = _load_prompt_template(prompt_file)

    with connect_db(db_path) as connection:
        source_id = get_source_id_by_key(connection, source_key)
        if source_id is None:
            raise ValueError(f"unknown source_key: {source_key}")
        documents = [
            {
                "id": int(row["id"]),
                "canonical_url": str(row["canonical_url"]),
                "current_cleaned_path": str(row["current_cleaned_path"]),
            }
            for row in list_documents_for_analysis(connection, source_id=source_id)
        ]
        crawl_run_id = start_crawl_run(
            connection,
            source_id=source_id,
            run_kind=SUMMARY_DRAFT_RUN_KIND,
        )

    log_path = logs_root / f"summary-draft-run-{crawl_run_id}.log"
    _append_log(
        log_path,
        {
            "event": "run_started",
            "run_kind": SUMMARY_DRAFT_RUN_KIND,
            "crawl_run_id": crawl_run_id,
            "source_key": source_key,
            "status": "running",
            "model_name": active_model_name,
            "prompt_name": prompt_file.stem,
        },
    )
    generated_count = 0
    failed_count = 0
    skipped_count = 0
    errors: list[str] = []
    prompt_name = prompt_file.stem

    for document in documents:
        if max_documents is not None and generated_count >= max_documents:
            break

        document_id = int(document["id"])
        canonical_url = str(document["canonical_url"])
        current_cleaned_path = str(document["current_cleaned_path"])
        cleaned_path = resolve_artifact_path(cleaned_root, current_cleaned_path)

        try:
            cleaned_text = cleaned_path.read_text(encoding="utf-8")
            source_content_hash = sha256(cleaned_text.encode("utf-8")).hexdigest()
            with connect_db(db_path) as connection:
                existing_version = get_document_version_for_source_content(
                    connection,
                    document_id=document_id,
                    version_kind=SUMMARY_DRAFT_VERSION_KIND,
                    model_name=active_model_name,
                    prompt_name=prompt_name,
                    source_content_hash=source_content_hash,
                )
            if existing_version is not None:
                skipped_count += 1
                _append_log(
                    log_path,
                    {
                        "url": canonical_url,
                        "document_id": document_id,
                        "status": "skipped_unchanged",
                        "version_kind": SUMMARY_DRAFT_VERSION_KIND,
                        "version_id": int(existing_version["id"]),
                        "file_path": str(existing_version["file_path"]),
                        "model_name": active_model_name,
                        "prompt_name": prompt_name,
                    },
                )
                continue

            summary_text = generate_summary(
                cleaned_text=cleaned_text,
                model_name=active_model_name,
                prompt_template=prompt_template,
                base_url=active_base_url,
                timeout_seconds=active_timeout_seconds,
            )
            version = persist_document_version(
                document_id=document_id,
                version_kind=SUMMARY_DRAFT_VERSION_KIND,
                content=summary_text,
                model_name=active_model_name,
                prompt_name=prompt_name,
                database_path=db_path,
                cleaned_dir=cleaned_root,
                derived_dir=derived_root,
                source_content_hash=source_content_hash,
            )

            generated_count += 1
            _append_log(
                log_path,
                {
                    "url": canonical_url,
                    "document_id": document_id,
                    "status": "generated",
                    "version_kind": SUMMARY_DRAFT_VERSION_KIND,
                    "version_id": version.version_id,
                    "file_path": version.file_path,
                    "model_name": active_model_name,
                    "prompt_name": prompt_name,
                },
            )
        except Exception as exc:
            failed_count += 1
            error_text = f"{canonical_url}: {exc}"
            errors.append(error_text)
            _append_log(
                log_path,
                {
                    "url": canonical_url,
                    "document_id": document_id,
                    "status": "generate_failed",
                    "model_name": active_model_name,
                    "prompt_name": prompt_name,
                    "cleaned_path": current_cleaned_path,
                    "error": str(exc),
                },
            )

    if failed_count:
        status = "partial_failure" if generated_count else "failed"
    else:
        status = "success"

    error_message = "; ".join(errors) if errors else None
    relative_log_path = (Path("data") / "logs" / log_path.name).as_posix()
    _append_log(
        log_path,
        {
            "event": "run_finished",
            "run_kind": SUMMARY_DRAFT_RUN_KIND,
            "crawl_run_id": crawl_run_id,
            "source_key": source_key,
            "status": status,
            "generated_count": generated_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "model_name": active_model_name,
            "prompt_name": prompt_name,
            "log_path": relative_log_path,
            "error": error_message or "",
        },
    )

    with connect_db(db_path) as connection:
        finish_crawl_run(
            connection,
            run_id=crawl_run_id,
            status=status,
            extracted_count=generated_count,
            error_message=error_message,
            log_path=relative_log_path,
        )

    return SummaryDraftRunResult(
        source_key=source_key,
        crawl_run_id=crawl_run_id,
        generated_count=generated_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        log_path=relative_log_path,
        status=status,
        model_name=active_model_name,
        prompt_name=prompt_name,
    )
