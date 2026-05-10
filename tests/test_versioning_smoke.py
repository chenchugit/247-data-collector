from hashlib import sha256
from pathlib import Path

from app.db import (
    connect_db,
    init_db,
    list_document_versions,
    record_discovered_documents,
    update_document_extract_state,
    upsert_source,
)
from app.extract import build_cleaned_artifact_relative_path
from app.versioning import persist_document_version


def test_persist_document_version_links_one_document_to_multiple_versions(tmp_path: Path) -> None:
    database_path = tmp_path / "versioning.sqlite3"
    data_dir = tmp_path / "data"
    cleaned_dir = data_dir / "cleaned"
    derived_dir = data_dir / "derived"

    init_db(database_path)

    article_url = "https://fixture.example/article"
    cleaned_relative_path = build_cleaned_artifact_relative_path(article_url)
    cleaned_path = cleaned_dir / cleaned_relative_path
    cleaned_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned_path.write_text("# Fixture Article\n\nUseful cleaned content.\n", encoding="utf-8")

    with connect_db(database_path) as connection:
        source_id = upsert_source(
            connection,
            source_key="fixture-versioning",
            source_type="seed",
            title="Fixture Versioning Source",
            config_path="tests/fixture-versioning",
        )
        inserted = record_discovered_documents(
            connection,
            source_id=source_id,
            canonical_urls=[article_url],
        )
        assert inserted == 1

        document_row = connection.execute(
            "SELECT id FROM documents WHERE canonical_url = ?",
            (article_url,),
        ).fetchone()
        assert document_row is not None
        document_id = int(document_row["id"])

        update_document_extract_state(
            connection,
            document_id=document_id,
            extract_status="extracted",
            current_cleaned_path=str((Path("data") / "cleaned" / cleaned_relative_path).as_posix()),
            title="Fixture Article",
        )

    first_content = "# Derived Note\n\nManual derived note.\n"
    second_content = "# Derived Digest\n\nFixture summary placeholder.\n"

    first_version = persist_document_version(
        document_id=document_id,
        version_kind="reader-note",
        content=first_content,
        database_path=database_path,
        cleaned_dir=cleaned_dir,
        derived_dir=derived_dir,
    )
    duplicate_first_version = persist_document_version(
        document_id=document_id,
        version_kind="reader-note",
        content=first_content,
        database_path=database_path,
        cleaned_dir=cleaned_dir,
        derived_dir=derived_dir,
    )
    second_version = persist_document_version(
        document_id=document_id,
        version_kind="summary-draft",
        content=second_content,
        model_name="fixture-model",
        prompt_name="fixture-prompt",
        database_path=database_path,
        cleaned_dir=cleaned_dir,
        derived_dir=derived_dir,
    )

    assert duplicate_first_version.version_id == first_version.version_id
    assert first_version.version_id != second_version.version_id
    assert first_version.file_path.startswith("data/derived/fixture.example/article/")
    assert second_version.file_path.startswith("data/derived/fixture.example/article/")
    assert "/reader-note/" in first_version.file_path
    assert "/summary-draft/" in second_version.file_path

    first_derived_path = tmp_path / Path(first_version.file_path)
    second_derived_path = tmp_path / Path(second_version.file_path)
    assert first_derived_path.exists()
    assert second_derived_path.exists()
    assert first_derived_path.read_text(encoding="utf-8") == first_content
    assert second_derived_path.read_text(encoding="utf-8") == second_content

    with connect_db(database_path) as connection:
        versions = list_document_versions(connection, document_id=document_id)
        assert len(versions) == 2

        first_row, second_row = versions
        assert first_row["document_id"] == document_id
        assert first_row["version_kind"] == "reader-note"
        assert first_row["file_path"] == first_version.file_path
        assert first_row["model_name"] is None
        assert first_row["prompt_name"] is None
        assert first_row["content_hash"] == sha256(first_content.encode("utf-8")).hexdigest()

        assert second_row["document_id"] == document_id
        assert second_row["version_kind"] == "summary-draft"
        assert second_row["file_path"] == second_version.file_path
        assert second_row["model_name"] == "fixture-model"
        assert second_row["prompt_name"] == "fixture-prompt"
        assert second_row["content_hash"] == sha256(second_content.encode("utf-8")).hexdigest()

        document_row = connection.execute(
            "SELECT current_cleaned_path, extract_status FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        assert document_row is not None
        assert document_row["extract_status"] == "extracted"
        assert document_row["current_cleaned_path"] == str(
            (Path("data") / "cleaned" / cleaned_relative_path).as_posix()
        )
