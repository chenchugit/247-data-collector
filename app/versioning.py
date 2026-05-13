from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
import re

from .config import load_settings
from .db import (
    connect_db,
    get_document_for_versioning,
    init_db,
    insert_document_version,
)


@dataclass(frozen=True)
class StoredDocumentVersion:
    version_id: int
    document_id: int
    version_kind: str
    file_path: str
    content_hash: str
    model_name: str | None
    prompt_name: str | None


def _sanitize_path_part(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return sanitized.strip("-") or "index"


def build_derived_artifact_relative_path(
    url: str,
    *,
    version_kind: str,
    content_hash: str,
    extension: str = "md",
) -> Path:
    parts = urlsplit(url)
    host_part = _sanitize_path_part(parts.netloc.lower())
    path_part = _sanitize_path_part(parts.path.strip("/"))
    kind_part = _sanitize_path_part(version_kind.lower())
    suffix = extension.lstrip(".") or "txt"
    return Path(host_part) / path_part / kind_part / f"{content_hash[:16]}.{suffix}"


def resolve_artifact_path(root_dir: Path, stored_path: str) -> Path:
    candidate = Path(stored_path)
    if candidate.is_absolute():
        return candidate

    parts = candidate.parts
    if len(parts) >= 3 and parts[0] == "data":
        return root_dir / Path(*parts[2:])
    return root_dir / candidate


def persist_document_version(
    *,
    document_id: int,
    version_kind: str,
    content: str | bytes,
    model_name: str | None = None,
    prompt_name: str | None = None,
    source_content_hash: str | None = None,
    extension: str = "md",
    database_path: Path | None = None,
    cleaned_dir: Path | None = None,
    derived_dir: Path | None = None,
) -> StoredDocumentVersion:
    settings = load_settings()
    db_path = init_db(database_path)
    cleaned_root = Path(cleaned_dir or settings.cleaned_dir)
    derived_root = Path(derived_dir or settings.derived_dir)
    derived_root.mkdir(parents=True, exist_ok=True)

    with connect_db(db_path) as connection:
        document_row = get_document_for_versioning(connection, document_id=document_id)
        if document_row is None:
            raise ValueError(f"unknown document_id: {document_id}")

        canonical_url = str(document_row["canonical_url"])
        current_cleaned_path = document_row["current_cleaned_path"]
        if current_cleaned_path is None:
            raise ValueError(f"document {document_id} has no current_cleaned_path")

        cleaned_path = resolve_artifact_path(cleaned_root, str(current_cleaned_path))
        if not cleaned_path.exists():
            raise FileNotFoundError(f"cleaned artifact missing: {cleaned_path}")

    artifact_bytes = content.encode("utf-8") if isinstance(content, str) else content
    content_hash = sha256(artifact_bytes).hexdigest()
    relative_path = build_derived_artifact_relative_path(
        canonical_url,
        version_kind=version_kind,
        content_hash=content_hash,
        extension=extension,
    )
    artifact_path = derived_root / relative_path
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(artifact_bytes)

    stored_path = str((Path("data") / "derived" / relative_path).as_posix())
    with connect_db(db_path) as connection:
        version_id = insert_document_version(
            connection,
            document_id=document_id,
            version_kind=version_kind,
            file_path=stored_path,
            model_name=model_name,
            prompt_name=prompt_name,
            content_hash=content_hash,
            source_content_hash=source_content_hash,
        )

    return StoredDocumentVersion(
        version_id=version_id,
        document_id=document_id,
        version_kind=version_kind,
        file_path=stored_path,
        content_hash=content_hash,
        model_name=model_name,
        prompt_name=prompt_name,
    )
