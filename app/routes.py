from pathlib import Path

from flask import Blueprint, abort, current_app, render_template

from .db import connect_db, get_document_version


bp = Blueprint("ui", __name__)


def _database_path() -> Path:
    return Path(current_app.config["DATABASE_PATH"])


def _storage_root(layer: str) -> Path:
    root_map = {
        "raw": Path(current_app.config["RAW_DIR"]),
        "cleaned": Path(current_app.config["CLEANED_DIR"]),
        "derived": Path(current_app.config["DERIVED_DIR"]),
        "logs": Path(current_app.config["LOG_DIR"]),
    }
    root = root_map.get(layer)
    if root is None:
        raise FileNotFoundError(f"unsupported storage layer: {layer}")
    return root.resolve()


def _resolve_stored_path(stored_path: str, *, expected_layer: str) -> Path:
    candidate = Path(stored_path)
    if candidate.is_absolute():
        raise FileNotFoundError(f"absolute stored paths are not allowed: {stored_path}")

    parts = candidate.parts
    if len(parts) < 3 or parts[0] != "data" or parts[1] != expected_layer:
        raise FileNotFoundError(f"unsupported stored path: {stored_path}")

    root = _storage_root(expected_layer)
    resolved = (root / Path(*parts[2:])).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FileNotFoundError(f"stored path escapes storage root: {stored_path}") from exc
    return resolved


def _read_stored_text(stored_path: str, *, expected_layer: str) -> str:
    resolved = _resolve_stored_path(stored_path, expected_layer=expected_layer)
    return resolved.read_text(encoding="utf-8", errors="replace")


def _get_document(document_id: int):
    with connect_db(_database_path()) as connection:
        document = connection.execute(
            """
            SELECT d.id, d.canonical_url, d.title, d.author, d.published_at,
                   d.current_raw_path, d.current_cleaned_path,
                   d.fetch_status, d.extract_status,
                   d.created_at, d.updated_at,
                   s.id AS source_id, s.source_key, s.title AS source_title
            FROM documents d
            LEFT JOIN sources s ON s.id = d.source_id
            WHERE d.id = ?
            """,
            (document_id,),
        ).fetchone()
        if document is None:
            return None

        versions = connection.execute(
            """
            SELECT id, document_id, version_kind, file_path, model_name, prompt_name, content_hash, created_at
            FROM document_versions
            WHERE document_id = ?
            ORDER BY id DESC
            """,
            (document_id,),
        ).fetchall()
        return document, versions


@bp.route("/")
def index():
    return documents()


@bp.route("/documents")
def documents():
    with connect_db(_database_path()) as connection:
        rows = connection.execute(
            """
            SELECT d.id, d.canonical_url, d.title, d.fetch_status, d.extract_status,
                   d.updated_at, s.title AS source_title,
                   COUNT(v.id) AS version_count
            FROM documents d
            LEFT JOIN sources s ON s.id = d.source_id
            LEFT JOIN document_versions v ON v.document_id = d.id
            GROUP BY d.id, d.canonical_url, d.title, d.fetch_status, d.extract_status, d.updated_at, s.title
            ORDER BY d.updated_at DESC, d.id DESC
            """
        ).fetchall()
    return render_template("documents.html", documents=rows)


@bp.route("/documents/<int:document_id>")
def document_detail(document_id: int):
    payload = _get_document(document_id)
    if payload is None:
        abort(404)
    document, versions = payload
    return render_template("document_detail.html", document=document, versions=versions)


@bp.route("/documents/<int:document_id>/artifacts/<artifact_kind>")
def document_artifact(document_id: int, artifact_kind: str):
    payload = _get_document(document_id)
    if payload is None:
        abort(404)
    document, _versions = payload

    if artifact_kind == "raw":
        stored_path = document["current_raw_path"]
    elif artifact_kind == "cleaned":
        stored_path = document["current_cleaned_path"]
    else:
        abort(404)

    if stored_path is None:
        abort(404)

    try:
        content = _read_stored_text(str(stored_path), expected_layer=artifact_kind)
    except FileNotFoundError:
        abort(404)

    return render_template(
        "artifact_detail.html",
        title=f"Document {document_id} {artifact_kind}",
        stored_path=stored_path,
        content=content,
        back_url=f"/documents/{document_id}",
    )


@bp.route("/sources")
def sources():
    with connect_db(_database_path()) as connection:
        rows = connection.execute(
            """
            SELECT s.id, s.source_key, s.source_type, s.title, s.enabled, s.updated_at,
                   COUNT(DISTINCT d.id) AS document_count,
                   COUNT(DISTINCT r.id) AS run_count
            FROM sources s
            LEFT JOIN documents d ON d.source_id = s.id
            LEFT JOIN crawl_runs r ON r.source_id = s.id
            GROUP BY s.id, s.source_key, s.source_type, s.title, s.enabled, s.updated_at
            ORDER BY s.id
            """
        ).fetchall()
    return render_template("sources.html", sources=rows)


@bp.route("/sources/<int:source_id>")
def source_detail(source_id: int):
    with connect_db(_database_path()) as connection:
        source = connection.execute(
            """
            SELECT id, source_key, source_type, title, config_path, enabled, created_at, updated_at
            FROM sources
            WHERE id = ?
            """,
            (source_id,),
        ).fetchone()
        if source is None:
            abort(404)

        documents = connection.execute(
            """
            SELECT id, canonical_url, title, fetch_status, extract_status, updated_at
            FROM documents
            WHERE source_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 20
            """,
            (source_id,),
        ).fetchall()
        runs = connection.execute(
            """
            SELECT id, run_kind, status, started_at, finished_at, discovered_count, fetched_count, extracted_count
            FROM crawl_runs
            WHERE source_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (source_id,),
        ).fetchall()
    return render_template("source_detail.html", source=source, documents=documents, runs=runs)


@bp.route("/runs")
def runs():
    with connect_db(_database_path()) as connection:
        rows = connection.execute(
            """
            SELECT r.id, r.run_kind, r.status, r.started_at, r.finished_at,
                   r.discovered_count, r.fetched_count, r.extracted_count,
                   s.id AS source_id, s.title AS source_title
            FROM crawl_runs r
            LEFT JOIN sources s ON s.id = r.source_id
            ORDER BY r.id DESC
            """
        ).fetchall()
    return render_template("runs.html", runs=rows)


@bp.route("/runs/<int:run_id>")
def run_detail(run_id: int):
    with connect_db(_database_path()) as connection:
        run = connection.execute(
            """
            SELECT r.id, r.run_kind, r.status, r.started_at, r.finished_at,
                   r.discovered_count, r.fetched_count, r.extracted_count,
                   r.error_message, r.log_path,
                   s.id AS source_id, s.source_key, s.title AS source_title
            FROM crawl_runs r
            LEFT JOIN sources s ON s.id = r.source_id
            WHERE r.id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            abort(404)

    log_text = None
    log_status = "not_recorded"
    if run["log_path"]:
        try:
            log_text = _read_stored_text(str(run["log_path"]), expected_layer="logs")
            log_status = "available"
        except FileNotFoundError:
            log_text = None
            log_status = "missing"

    return render_template("run_detail.html", run=run, log_text=log_text, log_status=log_status)


@bp.route("/versions/<int:version_id>")
def version_detail(version_id: int):
    with connect_db(_database_path()) as connection:
        version = get_document_version(connection, version_id=version_id)
        if version is None:
            abort(404)
        document = connection.execute(
            """
            SELECT id, canonical_url, title
            FROM documents
            WHERE id = ?
            """,
            (version["document_id"],),
        ).fetchone()
        if document is None:
            abort(404)

    try:
        content = _read_stored_text(str(version["file_path"]), expected_layer="derived")
    except FileNotFoundError:
        abort(404)

    return render_template(
        "version_detail.html",
        version=version,
        document=document,
        content=content,
    )
