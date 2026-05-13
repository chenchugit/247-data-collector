from pathlib import Path

import app.discovery as discovery
from app.config import load_settings
from app.discovery import load_source_definitions, run_discovery
from app.db import connect_db


class FakeXmlResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.headers = self

    def __enter__(self) -> "FakeXmlResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self.text.encode("utf-8")

    def get_content_charset(self) -> str:
        return "utf-8"


def test_run_discovery_loads_sources_records_candidates_and_deduplicates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "discovery.sqlite3"
    log_dir = tmp_path / "data" / "logs"
    config_path = Path(__file__).resolve().parent.parent / "config" / "sources" / "demo_sources.toml"

    source_definitions = load_source_definitions(config_path)
    assert [item.source_key for item in source_definitions] == [
        "demo-rss",
        "demo-sitemap",
        "demo-seed",
    ]

    def fake_urlopen(url: str, timeout: int = 30) -> FakeXmlResponse:
        if url == "https://example.com/start":
            return FakeXmlResponse(
                """
                <html><body>
                  <a href="/article-1">Article 1</a>
                  <a href="/category/news">Category</a>
                  <a href="https://external.example/article">External</a>
                </body></html>
                """
            )
        if url == "https://example.com/docs?id=1":
            return FakeXmlResponse(
                """
                <html><body>
                  <a href="/article-2">Article 2</a>
                  <a href="/feed.xml">Feed</a>
                </body></html>
                """
            )
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(discovery, "urlopen", fake_urlopen)

    first_results = run_discovery(
        config_path=config_path,
        database_path=database_path,
        log_dir=log_dir,
    )
    second_results = run_discovery(
        config_path=config_path,
        database_path=database_path,
        log_dir=log_dir,
    )

    assert [item.discovered_count for item in first_results] == [2, 3, 2]
    assert [item.inserted_count for item in first_results] == [2, 2, 2]
    assert all(item.status == "success" for item in first_results)
    assert all(item.log_path.startswith("data/logs/discovery-run-") for item in first_results)
    assert [item.discovered_count for item in second_results] == [2, 3, 2]
    assert [item.inserted_count for item in second_results] == [0, 0, 0]

    with connect_db(database_path) as connection:
        source_rows = connection.execute(
            "SELECT source_key, source_type, config_path FROM sources ORDER BY source_key"
        ).fetchall()
        assert [row["source_key"] for row in source_rows] == [
            "demo-rss",
            "demo-seed",
            "demo-sitemap",
        ]
        assert all(row["config_path"] == "config/sources/demo_sources.toml" for row in source_rows)

        document_rows = connection.execute(
            """
            SELECT canonical_url, fetch_status, extract_status
            FROM documents
            ORDER BY canonical_url
            """
        ).fetchall()
        assert [row["canonical_url"] for row in document_rows] == [
            "https://example.com/article-1",
            "https://example.com/article-2",
            "https://example.com/docs?id=1",
            "https://example.com/rss-only",
            "https://example.com/sitemap-only",
            "https://example.com/start",
        ]
        assert {row["fetch_status"] for row in document_rows} == {"discovered"}
        assert {row["extract_status"] for row in document_rows} == {"pending"}

        crawl_run_rows = connection.execute(
            """
            SELECT run_kind, status, discovered_count, log_path
            FROM crawl_runs
            ORDER BY id
            """
        ).fetchall()
        assert len(crawl_run_rows) == 6
        assert [row["run_kind"] for row in crawl_run_rows] == [
            "discovery:rss",
            "discovery:sitemap",
            "discovery:seed",
            "discovery:rss",
            "discovery:sitemap",
            "discovery:seed",
        ]
        assert all(row["status"] == "success" for row in crawl_run_rows)
        assert [row["discovered_count"] for row in crawl_run_rows] == [2, 3, 2, 2, 3, 2]
        assert all(str(row["log_path"]).startswith("data/logs/discovery-run-") for row in crawl_run_rows)

    first_log_path = log_dir / "discovery-run-1.log"
    assert first_log_path.exists()
    first_log_text = first_log_path.read_text(encoding="utf-8")
    assert '"event": "run_started"' in first_log_text
    assert '"event": "discovered_urls"' in first_log_text
    assert '"event": "run_finished"' in first_log_text


def test_source_config_default_prefers_target_smoke_and_env_can_override(monkeypatch) -> None:
    project_root = Path(__file__).resolve().parent.parent
    target_config = project_root / "config" / "sources" / "target_smoke_sources.toml"
    demo_config = project_root / "config" / "sources" / "demo_sources.toml"

    monkeypatch.delenv("AUTO_SCRAPY_SOURCES_CONFIG_PATH", raising=False)
    assert load_settings().sources_config_path == target_config

    monkeypatch.setenv("AUTO_SCRAPY_SOURCES_CONFIG_PATH", str(demo_config))
    assert load_settings().sources_config_path == demo_config


# def test_target_smoke_sources_preserve_existing_schema() -> None:
#     config_path = Path(__file__).resolve().parent.parent / "config" / "sources" / "target_smoke_sources.toml"

#     source_definitions = load_source_definitions(config_path)

#     assert [item.source_key for item in source_definitions] == [
#         "arxiv-cs-ai-rss",
#         "anthropic-sitemap",
#         "openai-news",
#         "github-changelog",
#         "google-research-blog",
#     ]
#     assert [item.source_type for item in source_definitions] == [
#         "rss",
#         "sitemap",
#         "seed",
#         "seed",
#         "seed",
#     ]
#     assert source_definitions[0].path == "https://rss.arxiv.org/rss/cs.AI"
#     assert source_definitions[1].path == "https://www.anthropic.com/sitemap.xml"


def test_target_smoke_sources_preserve_existing_schema() -> None:
    config_path = Path(__file__).resolve().parent.parent / "config" / "sources" / "target_smoke_sources.toml"

    source_definitions = load_source_definitions(config_path)

    actual_keys = [item.source_key for item in source_definitions]
    expected_keys = [
        "the-ai-summer",
        "lil-log",
        "jay-alammar",
        "colah-blog",
        "distill",
        "explained-ai",
        "arxiv-cs-ai-rss",
        "huggingface-blog",
        "github-changelog",
        "mcp-llmstxt",
        "langchain-llmstxt",
        "openai-news",
        "anthropic-news",
        "pytorch-blog",
        "google-research-blog",
    ]

    assert set(actual_keys) == set(expected_keys)
    assert len(actual_keys) == len(expected_keys)

    actual_types = [item.source_type for item in source_definitions]
    assert actual_types.count("rss") == 1
    assert actual_types.count("seed") == 14
    assert set(actual_types) == {"rss", "seed"}

    source_map = {item.source_key: item for item in source_definitions}

    assert source_map["arxiv-cs-ai-rss"].path == "https://rss.arxiv.org/rss/cs.AI"
    assert source_map["anthropic-news"].seeds == ("https://www.anthropic.com/news",)
    assert source_map["the-ai-summer"].seeds == (
        "https://theaisummer.com/",
        "https://theaisummer.com/learn-ai/",
    )
    assert source_map["distill"].seeds == (
        "https://distill.pub/",
        "https://distill.pub/archive/",
    )
    assert source_map["explained-ai"].seeds == (
        "https://explained.ai/",
        "https://mlbook.explained.ai/",
    )
    assert source_map["mcp-llmstxt"].seeds == (
        "https://modelcontextprotocol.io/llms.txt",
    )
    assert source_map["langchain-llmstxt"].seeds == (
        "https://docs.langchain.com/llms.txt",
    )


def test_run_discovery_accepts_remote_rss_and_sitemap_paths(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "remote-discovery.sqlite3"
    log_dir = tmp_path / "data" / "logs"
    config_path = tmp_path / "remote_sources.toml"
    config_path.write_text(
        "\n".join(
            [
                "[[sources]]",
                'source_key = "remote-rss"',
                'source_type = "rss"',
                'title = "Remote RSS"',
                'path = "https://example.com/feed.xml"',
                "",
                "[[sources]]",
                'source_key = "remote-sitemap"',
                'source_type = "sitemap"',
                'title = "Remote Sitemap"',
                'path = "https://example.com/sitemap.xml"',
            ]
        ),
        encoding="utf-8",
    )

    def fake_urlopen(url: str, timeout: int = 30) -> FakeXmlResponse:
        assert timeout == 30
        if url.endswith("feed.xml"):
            return FakeXmlResponse(
                """
                <rss>
                  <channel>
                    <item><link>https://example.com/rss-remote</link></item>
                  </channel>
                </rss>
                """
            )
        return FakeXmlResponse(
            """
            <urlset>
              <url><loc>https://example.com/sitemap-remote</loc></url>
            </urlset>
            """
        )

    monkeypatch.setattr(discovery, "urlopen", fake_urlopen)

    results = run_discovery(
        config_path=config_path,
        database_path=database_path,
        log_dir=log_dir,
    )

    assert [item.source_key for item in results] == ["remote-rss", "remote-sitemap"]
    assert [item.discovered_count for item in results] == [1, 1]

    with connect_db(database_path) as connection:
        document_rows = connection.execute(
            "SELECT canonical_url FROM documents ORDER BY canonical_url"
        ).fetchall()
    assert [row["canonical_url"] for row in document_rows] == [
            "https://example.com/rss-remote",
            "https://example.com/sitemap-remote",
        ]


def test_seed_discovery_follows_same_domain_child_links_with_bounded_depth(
    monkeypatch,
) -> None:
    pages = {
        "https://example.com/start": """
            <html><body>
              <a href="/article-1">Article 1</a>
              <a href="/category/ml">Category</a>
              <a href="https://other.example/article">External</a>
            </body></html>
        """,
        "https://example.com/article-1": """
            <html><body>
              <a href="/article-2">Article 2</a>
            </body></html>
        """,
    }

    def fake_urlopen(url: str, timeout: int = 30) -> FakeXmlResponse:
        return FakeXmlResponse(pages[url])

    monkeypatch.setattr(discovery, "urlopen", fake_urlopen)

    assert discovery.discover_seed_urls(("https://example.com/start",), max_depth=1) == [
        "https://example.com/article-1"
    ]
    assert discovery.discover_seed_urls(("https://example.com/start",), max_depth=2) == [
        "https://example.com/article-1",
        "https://example.com/article-2",
    ]


def test_sitemapindex_recurses_to_content_urls(monkeypatch) -> None:
    def fake_urlopen(url: str, timeout: int = 30) -> FakeXmlResponse:
        assert url == "https://example.com/post-sitemap.xml"
        return FakeXmlResponse(
            """
            <urlset>
              <url><loc>https://example.com/article</loc></url>
            </urlset>
            """
        )

    monkeypatch.setattr(discovery, "urlopen", fake_urlopen)

    urls = discovery.discover_sitemap_urls(
        """
        <sitemapindex>
          <sitemap><loc>https://example.com/post-sitemap.xml</loc></sitemap>
        </sitemapindex>
        """
    )

    assert urls == ["https://example.com/article"]


def test_llms_txt_seed_is_parsed_as_discovery_index(monkeypatch) -> None:
    def fake_urlopen(url: str, timeout: int = 30) -> FakeXmlResponse:
        assert url == "https://docs.example.com/llms.txt"
        return FakeXmlResponse(
            """
            # Docs
            - https://docs.example.com/guide/intro
            - https://docs.example.com/search?q=skip
            - https://other.example.com/guide
            """
        )

    monkeypatch.setattr(discovery, "urlopen", fake_urlopen)

    urls = discovery.discover_seed_urls(("https://docs.example.com/llms.txt",), max_depth=1)

    assert urls == ["https://docs.example.com/guide/intro"]
