from argparse import ArgumentParser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import json

from .analysis import run_summary_draft
from .config import load_settings
from .discovery import run_discovery
from .extract import run_extract
from .fetch import run_fetch
from .db import init_db


@dataclass(frozen=True)
class RuntimeStageSummary:
    stage_name: str
    source_key: str
    crawl_run_id: int | None
    status: str


@dataclass(frozen=True)
class RuntimeSourceSummary:
    source_key: str
    status: str
    stages: tuple[RuntimeStageSummary, ...]


@dataclass(frozen=True)
class RuntimeRunResult:
    status: str
    source_count: int
    failed_source_count: int
    log_path: str
    sources: tuple[RuntimeSourceSummary, ...]


def _append_log(log_path: Path, payload: dict[str, str | int | None]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _build_runtime_log_path(log_dir: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return log_dir / f"runtime-run-{timestamp}.log"


def run_pipeline_once(
    *,
    config_path: Path | None = None,
    database_path: Path | None = None,
    raw_dir: Path | None = None,
    cleaned_dir: Path | None = None,
    derived_dir: Path | None = None,
    log_dir: Path | None = None,
    prompt_path: Path | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    timeout_seconds: int | None = None,
    run_discovery_stage=run_discovery,
    run_fetch_stage=run_fetch,
    run_extract_stage=run_extract,
    run_analysis_stage=run_summary_draft,
) -> RuntimeRunResult:
    settings = load_settings()
    db_path = init_db(database_path)
    logs_root = Path(log_dir or settings.log_dir)
    logs_root.mkdir(parents=True, exist_ok=True)
    runtime_log_path = _build_runtime_log_path(logs_root)
    stored_runtime_log_path = (Path("data") / "logs" / runtime_log_path.name).as_posix()

    _append_log(
        runtime_log_path,
        {
            "event": "runtime_started",
            "database_path": str(db_path),
            "config_path": str(Path(config_path)) if config_path is not None else None,
        },
    )

    discovery_results = run_discovery_stage(config_path=config_path, database_path=db_path)
    source_summaries: list[RuntimeSourceSummary] = []
    failed_source_count = 0

    for discovery_result in discovery_results:
        source_key = discovery_result.source_key
        stage_summaries: list[RuntimeStageSummary] = []
        source_has_failure = False
        _append_log(
            runtime_log_path,
            {
                "event": "source_started",
                "source_key": source_key,
                "discovery_run_id": discovery_result.crawl_run_id,
                "discovery_status": "success",
            },
        )

        stage_specs = (
            (
                "fetch",
                run_fetch_stage,
                {
                    "source_key": source_key,
                    "database_path": db_path,
                    "raw_dir": raw_dir,
                    "log_dir": log_dir,
                },
            ),
            (
                "extract",
                run_extract_stage,
                {
                    "source_key": source_key,
                    "database_path": db_path,
                    "raw_dir": raw_dir,
                    "cleaned_dir": cleaned_dir,
                    "log_dir": log_dir,
                },
            ),
            (
                "summary_draft",
                run_analysis_stage,
                {
                    "source_key": source_key,
                    "database_path": db_path,
                    "cleaned_dir": cleaned_dir,
                    "derived_dir": derived_dir,
                    "log_dir": log_dir,
                    "prompt_path": prompt_path,
                    "model_name": model_name,
                    "base_url": base_url,
                    "timeout_seconds": timeout_seconds,
                },
            ),
        )

        for stage_name, stage_runner, stage_kwargs in stage_specs:
            _append_log(
                runtime_log_path,
                {
                    "event": "stage_started",
                    "source_key": source_key,
                    "stage_name": stage_name,
                },
            )
            try:
                stage_result = stage_runner(**stage_kwargs)
            except Exception as exc:
                source_has_failure = True
                stage_summaries.append(
                    RuntimeStageSummary(
                        stage_name=stage_name,
                        source_key=source_key,
                        crawl_run_id=None,
                        status="failed",
                    )
                )
                _append_log(
                    runtime_log_path,
                    {
                        "event": "stage_failed",
                        "source_key": source_key,
                        "stage_name": stage_name,
                        "error": str(exc),
                    },
                )
                break

            stage_status = str(stage_result.status)
            crawl_run_id = int(stage_result.crawl_run_id)
            if stage_status != "success":
                source_has_failure = True
            stage_summaries.append(
                RuntimeStageSummary(
                    stage_name=stage_name,
                    source_key=source_key,
                    crawl_run_id=crawl_run_id,
                    status=stage_status,
                )
            )
            _append_log(
                runtime_log_path,
                {
                    "event": "stage_finished",
                    "source_key": source_key,
                    "stage_name": stage_name,
                    "crawl_run_id": crawl_run_id,
                    "status": stage_status,
                    "log_path": getattr(stage_result, "log_path", None),
                },
            )

        source_status = "partial_failure" if source_has_failure else "success"
        if source_has_failure:
            failed_source_count += 1
        source_summaries.append(
            RuntimeSourceSummary(
                source_key=source_key,
                status=source_status,
                stages=tuple(stage_summaries),
            )
        )
        _append_log(
            runtime_log_path,
            {
                "event": "source_finished",
                "source_key": source_key,
                "status": source_status,
            },
        )

    overall_status = "partial_failure" if failed_source_count else "success"
    _append_log(
        runtime_log_path,
        {
            "event": "runtime_finished",
            "status": overall_status,
            "source_count": len(source_summaries),
            "failed_source_count": failed_source_count,
        },
    )

    return RuntimeRunResult(
        status=overall_status,
        source_count=len(source_summaries),
        failed_source_count=failed_source_count,
        log_path=stored_runtime_log_path,
        sources=tuple(source_summaries),
    )


def _build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Run the auto-scrapy pipeline once outside Flask.")
    parser.add_argument("--config-path", type=Path, default=None)
    parser.add_argument("--database-path", type=Path, default=None)
    parser.add_argument("--raw-dir", type=Path, default=None)
    parser.add_argument("--cleaned-dir", type=Path, default=None)
    parser.add_argument("--derived-dir", type=Path, default=None)
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--prompt-path", type=Path, default=None)
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=None)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    result = run_pipeline_once(
        config_path=args.config_path,
        database_path=args.database_path,
        raw_dir=args.raw_dir,
        cleaned_dir=args.cleaned_dir,
        derived_dir=args.derived_dir,
        log_dir=args.log_dir,
        prompt_path=args.prompt_path,
        model_name=args.model_name,
        base_url=args.base_url,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(asdict(result), ensure_ascii=True))
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
