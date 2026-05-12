# import json
# import os
# from pathlib import Path

# import pytest

# from app.db import connect_db, init_db, record_discovered_documents, upsert_source
# from app.discovery import discover_source_urls, load_source_definitions
# from app.fetch import run_fetch


# LIVE_ENV = "AUTO_SCRAPY_ENABLE_LIVE_NETWORK_TESTS"
# STRICT_ENV = "AUTO_SCRAPY_LIVE_STRICT"


# def _append_error(report: dict[str, object], message: str) -> None:
#     existing = str(report.get("error") or "").strip()
#     report["error"] = f"{existing}; {message}" if existing else message


# def test_target_smoke_live_discovery_and_fetch(tmp_path: Path) -> None:
#     """
#     Real-network smoke test for the current target_smoke_sources.toml.

#     Purpose:
#     - verify the current target source catalog can be loaded
#     - verify each source can at least attempt discovery against the live network
#     - verify at least one real URL can be fetched and persisted as raw data
#     - produce a JSON report for manual review

#     Safety:
#     - this test is SKIPPED unless AUTO_SCRAPY_ENABLE_LIVE_NETWORK_TESTS=1
#     - default mode is tolerant: it does not require every source to succeed
#     - strict mode can be enabled with AUTO_SCRAPY_LIVE_STRICT=1
#     """

#     if os.getenv(LIVE_ENV) != "1":
#         pytest.skip(
#             f"set {LIVE_ENV}=1 to run live network connectivity checks against target_smoke_sources.toml"
#         )

#     project_root = Path(__file__).resolve().parent.parent
#     config_path = project_root / "config" / "sources" / "target_smoke_sources.toml"
#     relative_config_path = "config/sources/target_smoke_sources.toml"

#     source_definitions = load_source_definitions(config_path)

#     database_path = tmp_path / "live_connectivity.sqlite3"
#     raw_dir = tmp_path / "data" / "raw"
#     log_dir = tmp_path / "data" / "logs"
#     report_path = tmp_path / "target_smoke_live_report.json"

#     init_db(database_path)

#     reports: list[dict[str, object]] = []

#     for source_definition in source_definitions:
#         report: dict[str, object] = {
#             "source_key": source_definition.source_key,
#             "source_type": source_definition.source_type,
#             "discovery_status": "not_run",
#             "discovered_count": 0,
#             "sample_url": None,
#             "db_inserted_count": 0,
#             "fetch_stage_status": "not_run",
#             "document_fetch_status": None,
#             "raw_path": None,
#             "raw_exists": False,
#             "raw_bytes": 0,
#             "error": None,
#         }

#         # 1) Real discovery against the current live source
#         try:
#             discovered_urls = discover_source_urls(source_definition)
#             report["discovery_status"] = "success"
#             report["discovered_count"] = len(discovered_urls)
#             if discovered_urls:
#                 report["sample_url"] = discovered_urls[0]
#             else:
#                 _append_error(report, "no URLs discovered")
#         except Exception as exc:
#             report["discovery_status"] = "failed"
#             _append_error(report, f"discovery failed: {exc}")
#             reports.append(report)
#             continue

#         # If discovery returned nothing, keep the report and move on.
#         if not report["sample_url"]:
#             reports.append(report)
#             continue

#         # 2) Persist a single sample URL into the DB for fetch-stage testing
#         with connect_db(database_path) as connection:
#             source_id = upsert_source(
#                 connection,
#                 source_key=source_definition.source_key,
#                 source_type=source_definition.source_type,
#                 title=source_definition.title,
#                 config_path=relative_config_path,
#                 enabled=source_definition.enabled,
#             )
#             inserted_count = record_discovered_documents(
#                 connection,
#                 source_id=source_id,
#                 canonical_urls=[str(report["sample_url"])],
#             )
#             report["db_inserted_count"] = inserted_count

#         # 3) Run the real fetch stage for this source
#         try:
#             fetch_result = run_fetch(
#                 source_key=source_definition.source_key,
#                 database_path=database_path,
#                 raw_dir=raw_dir,
#                 log_dir=log_dir,
#             )
#             report["fetch_stage_status"] = fetch_result.status
#         except Exception as exc:
#             report["fetch_stage_status"] = "failed"
#             _append_error(report, f"fetch stage failed: {exc}")
#             reports.append(report)
#             continue

#         # 4) Inspect the resulting document status / raw artifact
#         with connect_db(database_path) as connection:
#             row = connection.execute(
#                 """
#                 SELECT canonical_url, fetch_status, current_raw_path
#                 FROM documents
#                 WHERE canonical_url = ?
#                 """,
#                 (str(report["sample_url"]),),
#             ).fetchone()

#         if row is None:
#             _append_error(report, "document row missing after fetch")
#             reports.append(report)
#             continue

#         report["document_fetch_status"] = row["fetch_status"]
#         report["raw_path"] = row["current_raw_path"]

#         if row["current_raw_path"]:
#             raw_path = tmp_path / Path(str(row["current_raw_path"]))
#             report["raw_exists"] = raw_path.exists()
#             if raw_path.exists():
#                 report["raw_bytes"] = raw_path.stat().st_size

#         reports.append(report)

#     summary = {
#         "source_count": len(reports),
#         "discovery_success_count": sum(1 for r in reports if r["discovery_status"] == "success"),
#         "discovery_failure_count": sum(1 for r in reports if r["discovery_status"] == "failed"),
#         "discovered_nonempty_count": sum(1 for r in reports if int(r["discovered_count"]) > 0),
#         "fetch_success_count": sum(
#             1 for r in reports if r["document_fetch_status"] == "fetched"
#         ),
#         "raw_artifact_count": sum(
#             1 for r in reports if bool(r["raw_exists"]) and int(r["raw_bytes"]) > 0
#         ),
#     }

#     full_report = {
#         "config_path": str(config_path),
#         "summary": summary,
#         "sources": reports,
#     }

#     report_path.write_text(json.dumps(full_report, indent=2, ensure_ascii=False), encoding="utf-8")
#     print(f"\n[live-target-smoke-report] {report_path}\n")
#     print(json.dumps(full_report, indent=2, ensure_ascii=False))

#     strict_mode = os.getenv(STRICT_ENV) == "1"

#     if strict_mode:
#         failed_sources = [
#             r
#             for r in reports
#             if r["discovery_status"] != "success"
#             or int(r["discovered_count"]) == 0
#             or r["document_fetch_status"] != "fetched"
#             or not bool(r["raw_exists"])
#             or int(r["raw_bytes"]) <= 0
#         ]
#         assert not failed_sources, json.dumps(
#             {
#                 "message": "strict live connectivity mode failed",
#                 "failed_sources": failed_sources,
#                 "report_path": str(report_path),
#             },
#             indent=2,
#             ensure_ascii=False,
#         )
#     else:
#         # Tolerant mode: prove the catalog is live enough to be useful,
#         # without requiring every public site to behave perfectly.
#         assert summary["source_count"] == len(source_definitions)
#         assert summary["discovery_success_count"] >= 1
#         assert summary["discovered_nonempty_count"] >= 1
#         assert summary["fetch_success_count"] >= 1
#         assert summary["raw_artifact_count"] >= 1





































import json
import multiprocessing
import os
import sys
from pathlib import Path

import pytest

from app.db import connect_db, init_db, record_discovered_documents, upsert_source
from app.discovery import discover_source_urls, load_source_definitions


LIVE_ENV = "AUTO_SCRAPY_ENABLE_LIVE_NETWORK_TESTS"
STRICT_ENV = "AUTO_SCRAPY_LIVE_STRICT"


def _append_error(report: dict[str, object], message: str) -> None:
    existing = str(report.get("error") or "").strip()
    report["error"] = f"{existing}; {message}" if existing else message


# ---------------------------------------------------------------------------
# Subprocess-isolated fetch
# ---------------------------------------------------------------------------
# Root cause of the original 24/25 failure:
#   CrawlerProcess internally starts a Twisted Reactor, which is a process-level
#   singleton. Once process.start() returns, the Reactor is stopped and CANNOT
#   be restarted in the same process. Every subsequent run_fetch() call in the
#   same pytest process therefore raises ReactorNotRestartable (whose __str__
#   is empty, producing the mysterious "fetch stage failed: " message).
#
# Fix: run each run_fetch() in a fresh child process via multiprocessing.
#   - Windows 11 (test env):  uses "spawn" context (Windows has no fork).
#   - Ubuntu (prod env):      uses "fork" context (faster, shares memory).
#   The helper below picks the right context automatically.
# ---------------------------------------------------------------------------

def _fetch_worker(
    source_key: str,
    database_path: str,
    raw_dir: str,
    log_dir: str,
    result_queue: "multiprocessing.Queue[tuple[str, object]]",
) -> None:
    """
    Runs inside the child process.
    Imports are deferred to here so that Twisted/Scrapy globals are never
    touched in the parent process.
    """
    try:
        from app.fetch import run_fetch  # local import — keeps parent clean

        result = run_fetch(
            source_key=source_key,
            database_path=Path(database_path),
            raw_dir=Path(raw_dir),
            log_dir=Path(log_dir),
        )
        result_queue.put((
            "ok",
            {
                "status": result.status,
                "fetched_count": result.fetched_count,
                "failed_count": result.failed_count,
                "needs_browser_count": result.needs_browser_count,
                "browser_fetched_count": result.browser_fetched_count,
                "log_path": result.log_path,
                "crawl_run_id": result.crawl_run_id,
            },
        ))
    except Exception as exc:
        result_queue.put(("err", f"[{type(exc).__name__}] {exc!r}"))


def _run_fetch_isolated(
    *,
    source_key: str,
    database_path: Path,
    raw_dir: Path,
    log_dir: Path,
    timeout_seconds: int = 120,
) -> dict[str, object]:
    """
    Spawns a child process to call run_fetch(), waits for it to finish,
    and returns the result dict — or raises RuntimeError on failure/timeout.

    Context selection:
      "spawn"  — works on Windows (no fork) and is safe everywhere.
      "fork"   — Linux/macOS only; faster because it skips re-importing.
    We use "fork" on non-Windows and "spawn" on Windows.
    """
    ctx_name = "spawn" if sys.platform == "win32" else "fork"
    ctx = multiprocessing.get_context(ctx_name)
    queue: "multiprocessing.Queue[tuple[str, object]]" = ctx.Queue()

    process = ctx.Process(
        target=_fetch_worker,
        args=(
            source_key,
            str(database_path),
            str(raw_dir),
            str(log_dir),
            queue,
        ),
        daemon=True,
    )
    process.start()
    process.join(timeout=timeout_seconds)

    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        raise TimeoutError(
            f"run_fetch for '{source_key}' exceeded {timeout_seconds}s timeout"
        )

    if process.exitcode != 0:
        raise RuntimeError(
            f"child process for '{source_key}' exited with code {process.exitcode}"
        )

    try:
        status, payload = queue.get_nowait()
    except Exception:
        raise RuntimeError(
            f"child process for '{source_key}' produced no result (crashed silently)"
        )

    if status == "err":
        raise RuntimeError(payload)

    return payload  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def test_target_smoke_live_discovery_and_fetch(tmp_path: Path) -> None:
    """
    Real-network smoke test for the current target_smoke_sources.toml.

    Purpose:
    - verify the current target source catalog can be loaded
    - verify each source can at least attempt discovery against the live network
    - verify at least one real URL can be fetched and persisted as raw data
    - produce a JSON report for manual review

    Safety:
    - this test is SKIPPED unless AUTO_SCRAPY_ENABLE_LIVE_NETWORK_TESTS=1
    - default mode is tolerant: it does not require every source to succeed
    - strict mode can be enabled with AUTO_SCRAPY_LIVE_STRICT=1

    Reactor fix:
    - each run_fetch() call is isolated in its own child process so that the
      Twisted Reactor singleton is never reused across calls (ReactorNotRestartable)
    """

    if os.getenv(LIVE_ENV) != "1":
        pytest.skip(
            f"set {LIVE_ENV}=1 to run live network connectivity checks against target_smoke_sources.toml"
        )

    project_root = Path(__file__).resolve().parent.parent
    config_path = project_root / "config" / "sources" / "target_smoke_sources.toml"
    relative_config_path = "config/sources/target_smoke_sources.toml"

    source_definitions = load_source_definitions(config_path)

    database_path = tmp_path / "live_connectivity.sqlite3"
    raw_dir = tmp_path / "data" / "raw"
    log_dir = tmp_path / "data" / "logs"
    report_path = tmp_path / "target_smoke_live_report.json"

    init_db(database_path)

    reports: list[dict[str, object]] = []

    for source_definition in source_definitions:
        report: dict[str, object] = {
            "source_key": source_definition.source_key,
            "source_type": source_definition.source_type,
            "discovery_status": "not_run",
            "discovered_count": 0,
            "sample_url": None,
            "db_inserted_count": 0,
            "fetch_stage_status": "not_run",
            "document_fetch_status": None,
            "raw_path": None,
            "raw_exists": False,
            "raw_bytes": 0,
            "error": None,
        }

        # 1) Real discovery against the current live source
        try:
            discovered_urls = discover_source_urls(source_definition)
            report["discovery_status"] = "success"
            report["discovered_count"] = len(discovered_urls)
            if discovered_urls:
                report["sample_url"] = discovered_urls[0]
            else:
                _append_error(report, "no URLs discovered")
        except Exception as exc:
            report["discovery_status"] = "failed"
            _append_error(report, f"discovery failed [{type(exc).__name__}]: {exc!r}")
            reports.append(report)
            continue

        # If discovery returned nothing, keep the report and move on.
        if not report["sample_url"]:
            reports.append(report)
            continue

        # 2) Persist a single sample URL into the DB for fetch-stage testing
        with connect_db(database_path) as connection:
            source_id = upsert_source(
                connection,
                source_key=source_definition.source_key,
                source_type=source_definition.source_type,
                title=source_definition.title,
                config_path=relative_config_path,
                enabled=source_definition.enabled,
            )
            inserted_count = record_discovered_documents(
                connection,
                source_id=source_id,
                canonical_urls=[str(report["sample_url"])],
            )
            report["db_inserted_count"] = inserted_count

        # 3) Run the real fetch stage — isolated in a child process to avoid
        #    Twisted ReactorNotRestartable across loop iterations.
        try:
            fetch_result = _run_fetch_isolated(
                source_key=source_definition.source_key,
                database_path=database_path,
                raw_dir=raw_dir,
                log_dir=log_dir,
            )
            report["fetch_stage_status"] = fetch_result["status"]
        except Exception as exc:
            report["fetch_stage_status"] = "failed"
            _append_error(report, f"fetch stage failed [{type(exc).__name__}]: {exc!r}")
            reports.append(report)
            continue

        # 4) Inspect the resulting document status / raw artifact
        with connect_db(database_path) as connection:
            row = connection.execute(
                """
                SELECT canonical_url, fetch_status, current_raw_path
                FROM documents
                WHERE canonical_url = ?
                """,
                (str(report["sample_url"]),),
            ).fetchone()

        if row is None:
            _append_error(report, "document row missing after fetch")
            reports.append(report)
            continue

        report["document_fetch_status"] = row["fetch_status"]
        report["raw_path"] = row["current_raw_path"]

        if row["current_raw_path"]:
            raw_path = tmp_path / Path(str(row["current_raw_path"]))
            report["raw_exists"] = raw_path.exists()
            if raw_path.exists():
                report["raw_bytes"] = raw_path.stat().st_size

        reports.append(report)

    summary = {
        "source_count": len(reports),
        "discovery_success_count": sum(1 for r in reports if r["discovery_status"] == "success"),
        "discovery_failure_count": sum(1 for r in reports if r["discovery_status"] == "failed"),
        "discovered_nonempty_count": sum(1 for r in reports if int(r["discovered_count"]) > 0),
        "fetch_success_count": sum(
            1 for r in reports if r["document_fetch_status"] == "fetched"
        ),
        "raw_artifact_count": sum(
            1 for r in reports if bool(r["raw_exists"]) and int(r["raw_bytes"]) > 0
        ),
    }

    full_report = {
        "config_path": str(config_path),
        "summary": summary,
        "sources": reports,
    }

    report_path.write_text(json.dumps(full_report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[live-target-smoke-report] {report_path}\n")
    print(json.dumps(full_report, indent=2, ensure_ascii=False))

    strict_mode = os.getenv(STRICT_ENV) == "1"

    if strict_mode:
        failed_sources = [
            r
            for r in reports
            if r["discovery_status"] != "success"
            or int(r["discovered_count"]) == 0
            or r["document_fetch_status"] != "fetched"
            or not bool(r["raw_exists"])
            or int(r["raw_bytes"]) <= 0
        ]
        assert not failed_sources, json.dumps(
            {
                "message": "strict live connectivity mode failed",
                "failed_sources": failed_sources,
                "report_path": str(report_path),
            },
            indent=2,
            ensure_ascii=False,
        )
    else:
        # Tolerant mode: prove the catalog is live enough to be useful,
        # without requiring every public site to behave perfectly.
        assert summary["source_count"] == len(source_definitions)
        assert summary["discovery_success_count"] >= 1
        assert summary["discovered_nonempty_count"] >= 1
        assert summary["fetch_success_count"] >= 1
        assert summary["raw_artifact_count"] >= 1