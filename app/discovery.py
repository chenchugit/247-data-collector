from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib import error
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import json
import re
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
DEFAULT_SEED_MAX_DEPTH = 1
DENY_PATH_RE = re.compile(
    r"(^|/)(tag|tags|category|categories|search|feed|rss|atom|sitemap|author|authors|"
    r"privacy|terms|about|contact|archive|archives|page)(\.|/|$)|/page/\d+/?$",
    re.IGNORECASE,
)
DENY_EXTENSIONS = {
    ".7z",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".svg",
    ".tar",
    ".tgz",
    ".webp",
    ".zip",
}
ABSOLUTE_URL_RE = re.compile(r"https?://[^\s\])>\"']+")
HTML_ACCEPT_HEADER = "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8"
XML_ACCEPT_HEADER = "application/xml,text/xml,application/rss+xml,application/atom+xml,*/*;q=0.8"
INVALID_CHILD_HREF_RE = re.compile(
    r"^(?:javascript\s*:|javascript\s*\([^)]*\)\s*:|void\s*\(|mailto\s*:|tel\s*:)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SourceDefinition:
    source_key: str
    source_type: str
    title: str
    enabled: bool
    config_path: Path
    path: Path | str | None = None
    seeds: tuple[str, ...] = ()
    max_depth: int = DEFAULT_SEED_MAX_DEPTH
    allow_url_patterns: tuple[str, ...] = ()
    deny_url_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryResult:
    source_key: str
    source_type: str
    crawl_run_id: int
    discovered_count: int
    inserted_count: int
    log_path: str
    status: str
    max_depth: int | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class SeedDiscoveryOutput:
    urls: list[str]
    failed_seeds: tuple[str, ...] = ()


class DiscoveryReadError(RuntimeError):
    pass


class _HTMLLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.hrefs.append(value)


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
        allow_url_patterns = tuple(str(pattern) for pattern in item.get("allow_url_patterns", []))
        deny_url_patterns = tuple(str(pattern) for pattern in item.get("deny_url_patterns", []))
        max_depth = int(item.get("max_depth", DEFAULT_SEED_MAX_DEPTH))
        if max_depth < 0:
            raise ValueError(f"{source_key} max_depth must be >= 0")

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
                max_depth=max_depth,
                allow_url_patterns=allow_url_patterns,
                deny_url_patterns=deny_url_patterns,
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


def _build_discovery_request(url: str, *, accept: str) -> Request:
    return Request(
        url,
        headers={
            "User-Agent": load_settings().discovery_user_agent,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def _read_remote_text(url: str, *, accept: str, timeout: int = 30) -> str:
    request = _build_discovery_request(url, accept=accept)
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except error.HTTPError as exc:
        raise DiscoveryReadError(f"{url}: HTTP {exc.code}") from exc
    except error.URLError as exc:
        raise DiscoveryReadError(f"{url}: {exc.reason}") from exc


def _read_xml_input(path: Path | str) -> str:
    if isinstance(path, str) and urlsplit(path).scheme in {"http", "https"}:
        return _read_remote_text(path, accept=XML_ACCEPT_HEADER)

    local_path = Path(path)
    if not local_path.exists():
        raise FileNotFoundError(path)
    return local_path.read_text(encoding="utf-8")


def _read_text_url(url: str, *, timeout: int = 30) -> str:
    return _read_remote_text(url, accept=HTML_ACCEPT_HEADER, timeout=timeout)


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
    root_name = _local_name(root.tag)
    urls: list[str] = []

    if root_name == "sitemapindex":
        for element in root.iter():
            if _local_name(element.tag) == "loc" and element.text:
                child_xml = _read_xml_input(element.text.strip())
                urls.extend(discover_sitemap_urls(child_xml))
    else:
        for element in root.iter():
            if _local_name(element.tag) == "loc" and element.text:
                urls.append(element.text)

    return deduplicate_urls(urls)


def _same_domain(candidate_url: str, seed_url: str) -> bool:
    return urlsplit(candidate_url).netloc.lower() == urlsplit(seed_url).netloc.lower()


def _matches_any_pattern(url: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, url) for pattern in patterns)


def _is_valid_child_href(href: str) -> bool:
    value = href.strip()
    if not value:
        return False
    if value.startswith("#"):
        return False
    return INVALID_CHILD_HREF_RE.search(value) is None


def _is_probable_content_url(
    url: str,
    *,
    allow_url_patterns: tuple[str, ...] = (),
    deny_url_patterns: tuple[str, ...] = (),
) -> bool:
    parts = urlsplit(url)
    path = parts.path or "/"
    if DENY_PATH_RE.search(path):
        return False
    if Path(path).suffix.lower() in DENY_EXTENSIONS:
        return False
    if deny_url_patterns and _matches_any_pattern(url, deny_url_patterns):
        return False
    if allow_url_patterns and not _matches_any_pattern(url, allow_url_patterns):
        return False
    return True


def _extract_html_child_urls(
    html_text: str,
    *,
    base_url: str,
    allow_url_patterns: tuple[str, ...] = (),
    deny_url_patterns: tuple[str, ...] = (),
) -> list[str]:
    parser = _HTMLLinkParser()
    parser.feed(html_text)
    urls: list[str] = []
    for href in parser.hrefs:
        if not _is_valid_child_href(href):
            continue
        absolute_url = urljoin(base_url, href)
        parts = urlsplit(absolute_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            continue
        normalized = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
        if _same_domain(normalized, base_url) and _is_probable_content_url(
            normalized,
            allow_url_patterns=allow_url_patterns,
            deny_url_patterns=deny_url_patterns,
        ):
            urls.append(normalized)
    return deduplicate_urls(urls)


def parse_llms_txt_urls(
    text: str,
    *,
    base_url: str,
    allow_url_patterns: tuple[str, ...] = (),
    deny_url_patterns: tuple[str, ...] = (),
) -> list[str]:
    urls: list[str] = []
    for match in ABSOLUTE_URL_RE.finditer(text):
        candidate = match.group(0).rstrip(".,;:")
        if _same_domain(candidate, base_url) and _is_probable_content_url(
            candidate,
            allow_url_patterns=allow_url_patterns,
            deny_url_patterns=deny_url_patterns,
        ):
            urls.append(candidate)
    return deduplicate_urls(urls)


def discover_seed_urls_with_failures(
    seeds: tuple[str, ...],
    *,
    max_depth: int = DEFAULT_SEED_MAX_DEPTH,
    allow_url_patterns: tuple[str, ...] = (),
    deny_url_patterns: tuple[str, ...] = (),
) -> SeedDiscoveryOutput:
    discovered: list[str] = []
    failed_seeds: list[str] = []
    seen_pages: set[str] = set()
    frontier = [(seed_url, 0, seed_url) for seed_url in deduplicate_urls(list(seeds))]

    while frontier:
        current_url, depth, root_seed_url = frontier.pop(0)
        if current_url in seen_pages:
            continue
        seen_pages.add(current_url)

        try:
            if urlsplit(current_url).path.endswith("/llms.txt"):
                child_urls = parse_llms_txt_urls(
                    _read_text_url(current_url),
                    base_url=current_url,
                    allow_url_patterns=allow_url_patterns,
                    deny_url_patterns=deny_url_patterns,
                )
            else:
                child_urls = _extract_html_child_urls(
                    _read_text_url(current_url),
                    base_url=current_url,
                    allow_url_patterns=allow_url_patterns,
                    deny_url_patterns=deny_url_patterns,
                )
        except DiscoveryReadError:
            failed_seeds.append(current_url)
            if depth == 0:
                discovered.append(current_url)
            continue

        discovered.extend(child_urls)
        next_depth = depth + 1
        if next_depth >= max_depth:
            continue
        for child_url in child_urls:
            if child_url not in seen_pages and _same_domain(child_url, root_seed_url):
                frontier.append((child_url, next_depth, root_seed_url))

    return SeedDiscoveryOutput(
        urls=deduplicate_urls(discovered),
        failed_seeds=tuple(deduplicate_urls(failed_seeds)),
    )


def discover_seed_urls(
    seeds: tuple[str, ...],
    *,
    max_depth: int = DEFAULT_SEED_MAX_DEPTH,
    allow_url_patterns: tuple[str, ...] = (),
    deny_url_patterns: tuple[str, ...] = (),
) -> list[str]:
    return discover_seed_urls_with_failures(
        seeds,
        max_depth=max_depth,
        allow_url_patterns=allow_url_patterns,
        deny_url_patterns=deny_url_patterns,
    ).urls


def discover_source_urls(source_definition: SourceDefinition) -> list[str]:
    if source_definition.source_type == "rss":
        return discover_rss_urls(_read_xml_input(source_definition.path))
    if source_definition.source_type == "sitemap":
        return discover_sitemap_urls(_read_xml_input(source_definition.path))
    if source_definition.source_type == "seed":
        return discover_seed_urls(
            source_definition.seeds,
            max_depth=source_definition.max_depth,
            allow_url_patterns=source_definition.allow_url_patterns,
            deny_url_patterns=source_definition.deny_url_patterns,
        )

    raise ValueError(f"unsupported source type: {source_definition.source_type}")


def _discover_source_urls_with_failures(
    source_definition: SourceDefinition,
) -> tuple[list[str], tuple[str, ...]]:
    if source_definition.source_type == "seed":
        output = discover_seed_urls_with_failures(
            source_definition.seeds,
            max_depth=source_definition.max_depth,
            allow_url_patterns=source_definition.allow_url_patterns,
            deny_url_patterns=source_definition.deny_url_patterns,
        )
        return output.urls, output.failed_seeds
    return discover_source_urls(source_definition), ()


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
    source_keys: tuple[str, ...] | None = None,
) -> list[DiscoveryResult]:
    settings = load_settings()
    requested_source_keys = set(source_keys or ())
    source_definitions = [
        source_definition
        for source_definition in load_source_definitions(config_path)
        if source_definition.enabled
        and (not requested_source_keys or source_definition.source_key in requested_source_keys)
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
                        "max_depth": source_definition.max_depth,
                    },
                )

            try:
                canonical_urls, failed_seeds = _discover_source_urls_with_failures(source_definition)
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
                        "status": "partial_failure" if failed_seeds else "success",
                        "discovered_count": len(canonical_urls),
                        "inserted_count": inserted_count,
                        "max_depth": source_definition.max_depth,
                        "failed_seed_count": len(failed_seeds),
                    },
                )
                for failed_seed in failed_seeds:
                    _append_log(
                        log_path,
                        {
                            "event": "seed_expansion_failed",
                            "run_kind": run_kind,
                            "crawl_run_id": crawl_run_id,
                            "source_key": source_definition.source_key,
                            "source_type": source_definition.source_type,
                            "status": "fallback_to_seed",
                            "seed_url": failed_seed,
                        },
                    )
                run_status = "partial_failure" if failed_seeds else "success"
                finish_crawl_run(
                    connection,
                    run_id=crawl_run_id,
                    status=run_status,
                    discovered_count=len(canonical_urls),
                    error_message=(
                        f"seed expansion failed for {len(failed_seeds)} seed(s)"
                        if failed_seeds
                        else None
                    ),
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
                        "status": run_status,
                        "discovered_count": len(canonical_urls),
                        "inserted_count": inserted_count,
                        "log_path": relative_log_path,
                        "max_depth": source_definition.max_depth,
                        "failed_seed_count": len(failed_seeds),
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
                results.append(
                    DiscoveryResult(
                        source_key=source_definition.source_key,
                        source_type=source_definition.source_type,
                        crawl_run_id=crawl_run_id,
                        discovered_count=0,
                        inserted_count=0,
                        log_path=relative_log_path,
                        status="failed",
                        max_depth=source_definition.max_depth,
                        error_message=str(exc),
                    )
                )
                continue

            results.append(
                DiscoveryResult(
                    source_key=source_definition.source_key,
                    source_type=source_definition.source_type,
                    crawl_run_id=crawl_run_id,
                    discovered_count=len(canonical_urls),
                    inserted_count=inserted_count,
                    log_path=relative_log_path,
                    status=run_status,
                    max_depth=source_definition.max_depth,
                    error_message=(
                        f"seed expansion failed for {len(failed_seeds)} seed(s)"
                        if failed_seeds
                        else None
                    ),
                )
            )

    return results
