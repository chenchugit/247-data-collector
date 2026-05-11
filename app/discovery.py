from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
import json
import tomllib
import xml.etree.ElementTree as ET

from .config import load_settings
from .db import (
    connect_db,
    finish_crawl_run,
    init_db,
    record_discovered_documents,
    start_crawl_run,
    upsert_source,
)


SUPPORTED_SOURCE_TYPES = {"rss", "sitemap", "seed"}


@dataclass(frozen=True)
class SourceDefinition:
    source_key: str
    source_type: str
    title: str
    enabled: bool
    config_path: Path
    path: Path | str | None = None
    seeds: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryResult:
    source_key: str
    source_type: str
    crawl_run_id: int
    discovered_count: int
    inserted_count: int
    log_path: str
    status: str


def get_sources_config_path() -> Path:
    return load_settings().sources_config_path


def load_source_definitions(config_path: Path | None = None) -> list[SourceDefinition]:
    path = Path(config_path or get_sources_config_path())
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_sources = payload.get("sources")

    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError(f"source config must define a non-empty sources list: {path}")

    definitions: list[SourceDefinition] = []
    for item in raw_sources:
        source_key = str(item["source_key"]).strip()
        source_type = str(item["source_type"]).strip().lower()
        title = str(item["title"]).strip()
        enabled = bool(item.get("enabled", True))

        if source_type not in SUPPORTED_SOURCE_TYPES:
            raise ValueError(f"unsupported source_type for {source_key}: {source_type}")

        source_path = item.get("path")
        seeds = item.get("seeds", [])

        if source_type in {"rss", "sitemap"} and not source_path:
            raise ValueError(f"{source_key} requires path for {source_type} discovery")
        if source_type == "seed" and not seeds:
            raise ValueError(f"{source_key} requires at least one seed URL")

        resolved_path = None
        if source_path:
            source_path_text = str(source_path)
            source_path_parts = urlsplit(source_path_text)
            if source_path_parts.scheme in {"http", "https"}:
                resolved_path = source_path_text
            else:
                resolved_path = (path.parent / source_path_text).resolve()

        definitions.append(
            SourceDefinition(
                source_key=source_key,
                source_type=source_type,
                title=title,
                enabled=enabled,
                config_path=path.resolve(),
                path=resolved_path,
                seeds=tuple(str(url) for url in seeds),
            )
        )

    return definitions


def normalize_url(url: str) -> str:
    trimmed = url.strip()
    if not trimmed:
        raise ValueError("url must not be empty")

    parts = urlsplit(trimmed)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"url must include scheme and host: {url}")

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    query = parts.query
    return urlunsplit((scheme, netloc, path, query, ""))


def deduplicate_urls(urls: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()

    for raw_url in urls:
        normalized = normalize_url(raw_url)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)

    return deduped


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _read_xml_input(path: Path | str) -> str:
    if isinstance(path, str) and urlsplit(path).scheme in {"http", "https"}:
        with urlopen(path, timeout=30) as response:
            return response.read().decode("utf-8")

    local_path = Path(path)
    if not local_path.exists():
        raise FileNotFoundError(path)
    return local_path.read_text(encoding="utf-8")


def discover_rss_urls(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    urls: list[str] = []

    for element in root.iter():
        if _local_name(element.tag) == "item":
            for child in element:
                if _local_name(child.tag) == "link" and child.text:
                    urls.append(child.text)
        if _local_name(element.tag) == "entry":
            for child in element:
                if _local_name(child.tag) != "link":
                    continue
                href = child.attrib.get("href")
                rel = child.attrib.get("rel", "alternate")
                if href and rel == "alternate":
                    urls.append(href)

    return deduplicate_urls(urls)


def discover_sitemap_urls(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    urls: list[str] = []

    for element in root.iter():
        if _local_name(element.tag) == "loc" and element.text:
            urls.append(element.text)

    return deduplicate_urls(urls)


def discover_seed_urls(seeds: tuple[str, ...]) -> list[str]:
    return deduplicate_urls(list(seeds))


def discover_source_urls(source_definition: SourceDefinition) -> list[str]:
    if source_definition.source_type == "rss":
        return discover_rss_urls(_read_xml_input(source_definition.path))
    if source_definition.source_type == "sitemap":
        return discover_sitemap_urls(_read_xml_input(source_definition.path))
    if source_definition.source_type == "seed":
        return discover_seed_urls(source_definition.seeds)

    raise ValueError(f"unsupported source type: {source_definition.source_type}")


def _config_path_for_db(path: Path) -> str:
    root_dir = load_settings().root_dir.resolve()
    try:
        return path.resolve().relative_to(root_dir).as_posix()
    except ValueError:
        return str(path.resolve())


def _append_log(log_path: Path, payload: dict[str, str | int]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def run_discovery(
    *,
    config_path: Path | None = None,
    database_path: Path | None = None,
    log_dir: Path | None = None,
) -> list[DiscoveryResult]:
    settings = load_settings()
    source_definitions = [
        source_definition
        for source_definition in load_source_definitions(config_path)
        if source_definition.enabled
    ]
    db_path = init_db(database_path)
    logs_root = Path(log_dir or settings.log_dir)
    logs_root.mkdir(parents=True, exist_ok=True)
    results: list[DiscoveryResult] = []

    with connect_db(db_path) as connection:
        for source_definition in source_definitions:
            source_id = upsert_source(
                connection,
                source_key=source_definition.source_key,
                source_type=source_definition.source_type,
                title=source_definition.title,
                config_path=_config_path_for_db(source_definition.config_path),
                enabled=source_definition.enabled,
            )
            crawl_run_id = start_crawl_run(
                connection,
                source_id=source_id,
                run_kind=f"discovery:{source_definition.source_type}",
            )
            run_kind = f"discovery:{source_definition.source_type}"
            log_path = logs_root / f"discovery-run-{crawl_run_id}.log"
            relative_log_path = (Path("data") / "logs" / log_path.name).as_posix()
            _append_log(
                log_path,
                {
                    "event": "run_started",
                    "run_kind": run_kind,
                    "crawl_run_id": crawl_run_id,
                    "source_key": source_definition.source_key,
                    "source_type": source_definition.source_type,
                    "status": "running",
                },
            )

            try:
                canonical_urls = discover_source_urls(source_definition)
                inserted_count = record_discovered_documents(
                    connection,
                    source_id=source_id,
                    canonical_urls=canonical_urls,
                )
                _append_log(
                    log_path,
                    {
                        "event": "discovered_urls",
                        "run_kind": run_kind,
                        "crawl_run_id": crawl_run_id,
                        "source_key": source_definition.source_key,
                        "source_type": source_definition.source_type,
                        "status": "success",
                        "discovered_count": len(canonical_urls),
                        "inserted_count": inserted_count,
                    },
                )
                finish_crawl_run(
                    connection,
                    run_id=crawl_run_id,
                    status="success",
                    discovered_count=len(canonical_urls),
                    log_path=relative_log_path,
                )
                _append_log(
                    log_path,
                    {
                        "event": "run_finished",
                        "run_kind": run_kind,
                        "crawl_run_id": crawl_run_id,
                        "source_key": source_definition.source_key,
                        "source_type": source_definition.source_type,
                        "status": "success",
                        "discovered_count": len(canonical_urls),
                        "inserted_count": inserted_count,
                        "log_path": relative_log_path,
                    },
                )
            except Exception as exc:
                _append_log(
                    log_path,
                    {
                        "event": "run_failed",
                        "run_kind": run_kind,
                        "crawl_run_id": crawl_run_id,
                        "source_key": source_definition.source_key,
                        "source_type": source_definition.source_type,
                        "status": "failed",
                        "error": str(exc),
                    },
                )
                finish_crawl_run(
                    connection,
                    run_id=crawl_run_id,
                    status="failed",
                    error_message=str(exc),
                    log_path=relative_log_path,
                )
                raise

            results.append(
                DiscoveryResult(
                    source_key=source_definition.source_key,
                    source_type=source_definition.source_type,
                    crawl_run_id=crawl_run_id,
                    discovered_count=len(canonical_urls),
                    inserted_count=inserted_count,
                    log_path=relative_log_path,
                    status="success",
                )
            )

    return results
