# auto-scrapy

`24-data-collector` 是CS/AI Web 知识采集系统的正式实现目录。

## 1. 当前实现范围

截至 M10，当前目录已经落地的范围是：

- M1 `项目骨架与配置加载`
- M2 `SQLite schema 与初始化`
- M3 `source 加载与 discovery`
- M4 `raw fetch 持久化与 fetch-status 记录`
- M5 `cleaned extract 与 extract-status 记录`
- M6 `document_versions` 与 `data/derived/` 版本化持久化
- M7 `本地 Flask 浏览 UI`
- M8 `本地 Ollama 单一派生产物 summary_draft`
- M9 `Flask 之外的有界 runtime 与 systemd 准备文件`
- M10 `测试、日志、稳定化`

当前还没有进入：

- OpenClaw 主 runtime
- Telegram 集成
- embeddings / vector search
- 多种分析产物扩张
- M11 及以后能力

## 2. 固定架构边界

当前实现必须持续遵守这些边界：

- Discovery is not Fetch
- Fetch is not Extract
- Extract is not Analyze
- Flask is not Scheduler
- OpenClaw is not the main crawler runtime
- Playwright remains part of fetch only
- SQLite is not the primary body store

这意味着：

- 内容主存储在磁盘，不在 SQLite 正文表里
- Flask 只做本地操作员浏览与检查，不承载 scheduler 主循环
- 本地分析走 Python 直连 Ollama，而不是先绕到 OpenClaw

## 3. 目录结构

当前实现目录的关键部分如下：

```text
24-data-collector/
├── app/
│   ├── __init__.py
│   ├── analysis.py
│   ├── config.py
│   ├── db.py
│   ├── discovery.py
│   ├── extract.py
│   ├── fetch.py
│   ├── routes.py
│   ├── runtime.py
│   ├── versioning.py
│   └── templates/
├── config/
├── crawler/
├── data/
│   ├── raw/
│   ├── cleaned/
│   ├── derived/
│   └── logs/
├── instance/
├── scripts/
│   └── run_regression.py
├── systemd/
│   ├── auto-scrapy-runtime.service
│   └── auto-scrapy-runtime.timer
├── tests/
├── pyproject.toml
└── README.md
```

## 4. 模块职责

### `app/config.py`

负责配置加载和目录设置。

### `app/db.py`

负责 SQLite 初始化、连接、核心读写辅助函数。

### `app/discovery.py`

负责 source 定义加载、RSS / sitemap / seed URL 发现、候选 URL 记录和 discovery run 可见性。

### `app/fetch.py`

负责以 Scrapy 为主的抓取流程、raw artifact 写盘、fetch 状态与日志记录；Playwright 只作为必要时升级路径。

### `app/extract.py`

负责从 raw artifact 提取 cleaned 内容，优先 Trafilatura，失败时走 fallback parser。

### `app/versioning.py`

负责 document version 持久化、`data/derived/` 路径分配和 provenance 关联。

### `app/analysis.py`

负责从 `current_cleaned_path` 读取 cleaned artifact，调用本地 Ollama，生成一个有界派生产物 `summary_draft`。

### `app/routes.py`

负责本地 Flask 浏览路由，只做浏览、检查和窄范围查看，不承载 scheduler 逻辑。

### `app/runtime.py`

负责 Flask 外的有界 runtime 顺序执行入口，用来串联当前已实现的 discovery / fetch / extract / analysis。

## 5. 当前已实现能力

当前目录内已经能做到：

1. 读取 source 配置并执行基础 discovery。
2. 把抓取结果写入 `data/raw/`。
3. 从 raw artifact 生成 cleaned artifact 到 `data/cleaned/`。
4. 把派生产物写入 `data/derived/`，并通过 `document_versions` 关联。
5. 提供本地 Flask UI 浏览 documents、sources、runs、versions。
6. 从 cleaned artifact 生成一个本地 Ollama 派生产物 `summary_draft`。
7. 通过 `python -m app.runtime` 执行一轮有界 pipeline。
8. 通过 `scripts/run_regression.py` 跑一条稳定的开发笔记本回归路径。

## 6. 处理流程

当前实现的主处理链路是：

```text
source definitions
-> discovery
-> documents candidate rows
-> fetch
-> data/raw/
-> extract
-> data/cleaned/
-> analysis(summary_draft)
-> data/derived/
-> Flask browse / runtime observe
```

按代码入口看，对应关系是：

1. `app.discovery.run_discovery()`
2. `app.fetch.run_fetch()`
3. `app.extract.run_extract()`
4. `app.analysis.run_summary_draft()`
5. `app.runtime.run_pipeline_once()`

这条链路是当前 README 最重要的“项目骨架”，后续新增能力也应围绕这条链路扩展，而不是绕开它重做一套系统。

## 7. 数据模型

当前 SQLite 核心表已经固定为：

- `sources`
- `documents`
- `document_versions`
- `crawl_runs`
- `tags`

它们的职责是：

### `sources`

保存来源定义与来源级配置索引，例如：

- `source_key`
- `source_type`
- `title`
- `config_path`

### `documents`

保存 canonical URL 级条目，以及当前 raw / cleaned 指针与处理状态。

这张表是“文档主索引”，不是正文内容仓库。

### `document_versions`

保存派生版本记录，例如当前已经落地的：

- `summary_draft`

它负责把派生产物文件与：

- `document_id`
- `version_kind`
- `file_path`
- `model_name`
- `prompt_name`
- `content_hash`

关联起来。

### `crawl_runs`

保存 discovery / fetch / extract / analysis / runtime 相关的运行记录、状态、错误和日志路径。

### `tags`

作为后续标签数据的轻量索引表保留，但当前阶段不是项目主能力中心。

## 8. 存储布局

当前实现坚持磁盘优先，内容按层分开：

- `data/raw/`
  - 原始抓取结果
- `data/cleaned/`
  - 清洗后的正文或 markdown
- `data/derived/`
  - 派生产物
- `data/logs/`
  - 各 stage 和 runtime 日志

这四层分离是当前项目最重要的落地约束之一。不要把它们并回一个目录，也不要把大正文回塞进 SQLite。

## 9. 环境与依赖

当前实现依赖以 **pyproject.toml** 为准：

- Python `>=3.12`
- Flask
- Scrapy
- scrapy-playwright
- Playwright
- Trafilatura

推荐使用 `uv`：

```powershell
uv sync
```

## 10. 常用入口命令

### 7.1 配置与数据库

```powershell
uv run python -c "from app import create_app; app = create_app(); print(app.import_name)"
uv run python -c "from app.config import load_settings; print(load_settings().data_dir)"
uv run python -c "from app.db import init_db; print(init_db())"
```

### 7.1.1 Source config files

The implementation keeps two source config roles separate:

- `config/sources/target_smoke_sources.toml` is the normal project default when present, and is the first real 1080 Ti target-machine smoke config.
- `config/sources/demo_sources.toml` is fixture/regression-only and should be selected explicitly when needed.

The source config selection order is:

1. `--config-path`
2. `AUTO_SCRAPY_SOURCES_CONFIG_PATH`
3. `config/sources/target_smoke_sources.toml`
4. `config/sources/demo_sources.toml` only when the target smoke config is absent

Use an explicit source config path when running fixture/regression checks that need the demo sources:

```powershell
$env:AUTO_SCRAPY_SOURCES_CONFIG_PATH = "config/sources/demo_sources.toml"
uv run python -m app.runtime
```

On Linux/systemd, `systemd/auto-scrapy-runtime.service` still sets `AUTO_SCRAPY_SOURCES_CONFIG_PATH=config/sources/target_smoke_sources.toml` explicitly so deployment behavior stays inspectable.

### 7.2 Flask UI

```powershell
uv run flask --app app run
```

当前 UI 重点页面包括：

- `/`
- `/documents`
- `/documents/<id>`
- `/sources`
- `/runs`
- `/versions/<id>`

### 7.3 Runtime

```powershell
uv run python -m app.runtime
uv run python -m app.runtime --help
```

### 7.4 开发笔记本回归入口

```powershell
uv run python scripts/run_regression.py
```

## 11. 代码入口与运行入口

如果要按“从读代码到理解系统”的顺序进入，推荐这样看：

1. **app/__init__.py**
   - Flask app factory
2. **app/config.py**
   - 配置入口
3. **app/db.py**
   - schema 与核心数据辅助
4. **app/discovery.py**
5. **app/fetch.py**
6. **app/extract.py**
7. **app/versioning.py**
8. **app/routes.py**
9. **app/runtime.py**

如果要按“从验证系统到确认行为”的顺序进入，优先看：

1. **tests/test_db_smoke.py**
2. **tests/test_discovery_smoke.py**
3. **tests/test_fetch_smoke.py**
4. **tests/test_extract_smoke.py**
5. **tests/test_versioning_smoke.py**
6. **tests/test_ui_smoke.py**
7. **tests/test_analysis_smoke.py**
8. **tests/test_runtime_smoke.py**

## 12. 测试与验证

当前 smoke 覆盖包括：

- `tests/test_db_smoke.py`
- `tests/test_discovery_smoke.py`
- `tests/test_fetch_smoke.py`
- `tests/test_extract_smoke.py`
- `tests/test_versioning_smoke.py`
- `tests/test_ui_smoke.py`
- `tests/test_analysis_smoke.py`
- `tests/test_runtime_smoke.py`

推荐的开发笔记本验证顺序也是：

1. `tests/test_db_smoke.py`
2. `tests/test_discovery_smoke.py`
3. `tests/test_fetch_smoke.py`
4. `tests/test_extract_smoke.py`
5. `tests/test_versioning_smoke.py`
6. `tests/test_ui_smoke.py`
7. `tests/test_analysis_smoke.py`
8. `tests/test_runtime_smoke.py`

如果只想跑一条稳定的作者机级回归入口，优先用：

```powershell
uv run python scripts/run_regression.py
```

## 13. 日志与故障可见性

当前实现的故障可见性基于现有 run/log 模型，而不是新增第二套状态系统。

主要可观察面有：

- `data/logs/` 文件日志
- SQLite `crawl_runs`
- Flask UI 的 `/runs/<id>`

当前日志形态已经在 M10 做过整理，重点包括：

- `run_started`
- item / stage 过程记录
- `run_finished`

当前 UI 中的 run detail 页面会暴露：

- `status`
- `error_message`
- `log_path`
- log 文件当前是否可用

## 14. 有界重跑与恢复

当前实现不追求复杂恢复框架，而是保持“有界入口 + 明确日志 + 现有状态可见”。

推荐排障顺序：

### discovery 异常

重跑：

```powershell
uv run --with pytest pytest tests/test_discovery_smoke.py
```

适用场景：

- source 定义变更
- 候选 URL intake 异常

### fetch 异常

重跑：

```powershell
uv run --with pytest pytest tests/test_fetch_smoke.py
```

优先看：

- `crawl_runs.error_message`
- fetch log
- HTTP 失败
- browser escalation 失败

### extract 异常

重跑：

```powershell
uv run --with pytest pytest tests/test_extract_smoke.py
```

优先看：

- `extract_status`
- raw artifact 是否缺失
- extract log

### analysis 异常

重跑：

```powershell
uv run --with pytest pytest tests/test_analysis_smoke.py
```

优先看：

- analysis log
- `generate_failed`
- Ollama 连接问题

### runtime 串联异常

修完上游 stage 后再重跑：

```powershell
uv run python -m app.runtime
```

如果要做整体回归确认：

```powershell
uv run python scripts/run_regression.py
```

## 15. 当前情况总结

如果只用一句话描述当前项目状态：

`24-data-collector/` 已经是一个能在开发笔记本上完成 discovery -> fetch -> extract -> summary_draft -> browse -> bounded runtime 的本地实现，但它仍然不是一个已经在目标机完成真实长时部署验证的系统。

这句话很重要，因为它同时覆盖了：

- 当前代码已经真实具备的能力
- 当前还没有被文档夸大成“已部署完成”的部分

## 16. Current Optimization Status And Technical Notes

As of the latest development-laptop validation, the project has moved from
pipeline survivability work into content-quality and source-quality refinement.

The fixed architecture remains unchanged:

```text
source config
-> discovery
-> documents table
-> fetch
-> data/raw/
-> extract
-> data/cleaned/
-> analysis
-> data/derived/
```

The important status change is that the fetch layer is no longer the main
bottleneck. Recent runs show that several previously unstable sources now
complete fetch successfully and fail later, mostly at extraction quality gates.

### Latest Observed Runtime Status

| Source | Fetch Stage | Extract Stage | Meaning |
|---|---:|---:|---|
| `anthropic-news` | `success` | `partial_failure` | Fetch timeout instability is improved; remaining failures are extract/content-quality related |
| `huggingface-blog` | `success` | `partial_failure` | Fetch succeeds; remaining issue is source filtering and page usefulness |
| `google-research-blog` | `success` | `partial_failure` | Fetch succeeds; remaining issue is extractor compatibility with some pages |

The pipeline status has shifted:

```text
Before:
discovery quality problems
-> invalid/low-value candidates
-> fetch failures/timeouts
-> runtime instability

Now:
discovery and fetch mostly survive
-> extraction rejects low-value or hard-to-clean pages
-> remaining work is source tuning and extraction quality refinement
```

### Technical Changes Already Applied

| Layer | File Area | Technical Change | Result |
|---|---|---|---|
| Discovery | `app/discovery.py` | Seed discovery now fetches seed/index pages and extracts same-domain child URLs | Seed sources no longer only return configured seed URLs |
| Discovery | `app/discovery.py` | Added bounded seed traversal with `max_depth` | Seed crawling remains controlled and does not become broad crawling |
| Discovery | `app/discovery.py` | Added per-source `allow_url_patterns` and `deny_url_patterns` | Source configs can narrow article candidates without changing code |
| Discovery | `app/discovery.py` | Added raw child-link rejection before normalization | Invalid hrefs do not become document candidates |
| Discovery | `app/discovery.py` | Added sitemapindex recursion | Child sitemap XML files are read recursively and final content URLs are returned |
| Discovery | `app/discovery.py` | Treats `llms.txt` as a discovery index | Docs sources can expand into real documentation URLs |
| Discovery | `app/discovery.py` | Discovery HTTP reads use `Request` headers and handle `HTTPError` / `URLError` | 403/429/5xx seed expansion failures degrade instead of crashing the whole runtime |
| Fetch | `app/fetch.py` | Increased `DOWNLOAD_TIMEOUT` from `10` to `30` seconds | Slow legitimate pages have more time to complete |
| Fetch | `app/fetch.py` | Enabled Scrapy retry middleware with `RETRY_TIMES = 1` | Transient failures get one retry |
| Fetch | `app/fetch.py` | Retries only transient HTTP codes: `408`, `429`, `500`, `502`, `503`, `504` | Retry behavior is narrow and does not hide persistent bad URLs |
| Extract | `app/extract.py` | Kept Trafilatura-first HTML path | Existing article extraction behavior remains intact |
| Extract | `app/extract.py` | Added textlike/markdown extraction path for `.md`, `.txt`, `.rst`, etc. | `llms.txt`-expanded docs pages can produce cleaned markdown/text |
| Extract | `app/extract.py` | Added low-quality rejection status such as `rejected_low_quality` | Thin landing pages and low-value pages are not silently marked successful |
| Extract | `app/extract.py` | Missing raw artifacts are requeued for fetch | Stale `current_raw_path` values no longer trap documents in terminal extract failure |
| Analysis | `app/analysis.py` | Summary generation skips unchanged cleaned content for same model/prompt | Repeated runs avoid unnecessary Ollama work |
| Runtime | `app/runtime.py` | Added `--skip-analysis` | Collection can run without forcing summary generation |
| Runtime | `app/runtime.py` | Added `--analysis-limit-per-source` | Analysis can be capped for limited hardware |
| Runtime | `app/runtime.py` | Added repeatable `--source-key` | Operators can target a subset of sources |

### Current Fetch Settings

The fetch layer currently uses Scrapy as the primary fetch engine.

```python
DOWNLOAD_TIMEOUT = 30
RETRY_ENABLED = True
RETRY_TIMES = 1
RETRY_HTTP_CODES = [408, 429, 500, 502, 503, 504]
```

This is intentionally modest. It gives slow or transient pages a better chance
without turning fetch into a broad masking layer for discovery problems.

Playwright remains a secondary escalation path only. Fetch still processes
documents that discovery has already inserted; it does not perform document
discovery.

### Current Discovery Filtering Model

Discovery filtering now has three layers:

```text
raw href validity filter
-> same-domain filter
-> default + source-specific content filters
```

Raw hrefs are rejected before `urljoin()`, normalization, or database insertion
when they are:

- empty
- whitespace-only
- hash-only
- `javascript:` links
- pseudo-links like `javascript(0):void`
- `void(0)` links
- `mailto:` links
- `tel:` links

Default content filtering rejects common low-value paths such as:

- tag/category/search pages
- feed/rss/atom pages
- sitemap links
- author/about/contact/privacy/terms pages
- archive and pagination pages
- static or binary file extensions

Source-specific filtering is configured in
`config/sources/target_smoke_sources.toml`.

Example source-specific rules:

```toml
[[sources]]
source_key = "openai-news"
allow_url_patterns = [
  "^https://openai\\.com/index/[^/?#]+/?$",
]
deny_url_patterns = [
  "^https://openai\\.com/news/?$",
  "^https://openai\\.com/news/\\?",
  "^https://openai\\.com/(about|api|business|careers|chatgpt|policies|research|sora)(/|$)",
]
```

```toml
[[sources]]
source_key = "pytorch-blog"
allow_url_patterns = [
  "^https://pytorch\\.org/blog/[^/?#]+/?$",
]
deny_url_patterns = [
  "^https://pytorch\\.org/blog/?$",
  "^https://pytorch\\.org/(community|docs|features|forums|foundation|resources|tutorials|webinars)(/|$)",
  "^https://pytorch\\.org/(get-started|projects)(/|$)",
]
```

```toml
[[sources]]
source_key = "huggingface-blog"
deny_url_patterns = [
  "^https://huggingface\\.co/(login|join|logout|settings|account|oauth)(/|\\?|$)",
]
```

### Current Extraction Model

Extraction is intentionally split by content shape.

For HTML/article-like pages:

```text
raw HTML
-> Trafilatura
-> fallback HTML parser if needed
-> quality gate
-> data/cleaned/
```

For markdown/textlike pages:

```text
raw .md/.txt/.rst or textlike content
-> UTF-8 decode with replacement
-> line-ending normalization
-> blank-line cleanup
-> title inference from first heading/short first line
-> quality gate
-> data/cleaned/
```

The quality gate rejects:

- too-short cleaned content
- too-few-word cleaned content
- nav-heavy fallback HTML output
- empty or near-empty markdown/textlike output
- thin landing-page style content

This means an extract `partial_failure` is not automatically a pipeline failure.
It often means the extractor correctly rejected low-value pages.

### Current Database Recovery Behavior

The `documents` table remains the canonical processing index. Large bodies are
still stored on disk.

Important current status fields:

```text
documents.fetch_status
documents.extract_status
documents.current_raw_path
documents.current_cleaned_path
```

If extraction sees a stale `current_raw_path` that no longer exists on disk, the
document is recovered by clearing the raw pointer and returning it to the fetch
queue:

```text
fetch_status = 'discovered'
extract_status = 'pending'
current_raw_path = NULL
```

This allows a later fetch run to recover the document without schema redesign.

### Current Runtime Controls

The runtime CLI supports bounded operation:

```cmd
uv run python -m app.runtime --skip-analysis
```

Runs:

```text
discovery -> fetch -> extract
```

and skips:

```text
analysis -> Ollama -> data/derived/
```

For capped analysis:

```cmd
uv run python -m app.runtime --analysis-limit-per-source 5
```

For targeted runs:

```cmd
uv run python -m app.runtime --source-key openai-news --analysis-limit-per-source 5
```

Multiple source keys can be passed:

```cmd
uv run python -m app.runtime --source-key openai-news --source-key pytorch-blog --skip-analysis
```

### Recommended Operating Mode

At this stage, broad architecture work should stop unless a real target-machine
failure proves it necessary.

Recommended collection-only run:

```cmd
set AUTO_SCRAPY_SOURCES_CONFIG_PATH=config/sources/target_smoke_sources.toml
set AUTO_SCRAPY_OLLAMA_MODEL=qwen2.5-coder:7b
uv run python -m app.runtime --skip-analysis > nightly_collection.log 2>&1
```

Recommended capped analysis run:

```cmd
set AUTO_SCRAPY_SOURCES_CONFIG_PATH=config/sources/target_smoke_sources.toml
set AUTO_SCRAPY_OLLAMA_MODEL=qwen2.5-coder:7b
uv run python -m app.runtime --analysis-limit-per-source 5 > runtime_full_with_analysis_capped.log 2>&1
```

Recommended selective analysis:

```cmd
uv run python -m app.runtime --source-key openai-news --analysis-limit-per-source 5
uv run python -m app.runtime --source-key pytorch-blog --analysis-limit-per-source 5
```

### Remaining Technical Work

The remaining work is mostly source-quality and extractor-quality refinement,
not pipeline survival.

| Area | Possible Work | Reason |
|---|---|---|
| Hugging Face | Add a stricter blog allow pattern if needed | Prevent broad `/models`, `/spaces`, login/account, or docs-like pages from entering the dataset |
| Google Research | Inspect failed raw artifacts and cleaned failures | Some pages may be JS-heavy, thin, or not article-shaped |
| Anthropic | Inspect partial extraction failures | Some URLs may be index/policy/news listing pages rather than article pages |
| Source configs | Tune allow/deny rules from real collected URLs | Improve dataset usefulness without changing architecture |
| Extraction | Add narrow site-aware fallbacks only when justified by repeated failures | Avoid weakening the global quality gate |
| Target machine | Run real collection and capped analysis on the 1080 Ti machine | Development-laptop tests do not prove target deployment stability |

### Current Project Interpretation

The project should now be understood as:

```text
A functioning local-first CS/AI web collection pipeline
with stable enough discovery/fetch/runtime behavior for controlled use,
but still requiring source-level and extraction-level tuning for dataset quality.
```

It should not yet be described as fully production-validated on the target
1080 Ti machine until the same runtime paths have been executed and observed
there without Codex.

## 17. Ultimate Deployment Model

The intended target-machine deployment is a `systemd`-scheduled bounded runtime,
not a permanent Python crawler loop.

`app.runtime` should run once and exit:

```text
discovery -> fetch -> extract -> optional analysis
```

Long-running behavior belongs to `systemd` timers and services:

| Component | Role | Recommended Mode |
|---|---|---|
| `ollama` | Local model API server | long-running system service |
| `auto-scrapy-collection.service` | Runs `discovery -> fetch -> extract` | oneshot |
| `auto-scrapy-collection.timer` | Schedules collection | enabled at boot |
| `auto-scrapy-analysis.service` | Runs capped Ollama analysis | manual first |
| `auto-scrapy-analysis.timer` | Optional scheduled capped analysis | add only after safe manual runs |
| `auto-scrapy-ui.service` | Optional local Flask browsing UI | bind to `127.0.0.1` |

The recommended production posture is:

```text
daily collection with --skip-analysis
manual or low-frequency capped analysis with --analysis-limit-per-source
optional local-only Flask UI
no uncapped automatic analysis on the 1080 Ti
```

The detailed Ubuntu/systemd deployment guide, including environment file
templates, service templates, timer templates, monitoring commands, and
target-machine done criteria, is maintained in:

- **DEPLOY_TARGET.md**

This README only summarizes the deployment model. Target-machine validation is
not complete until the commands in `DEPLOY_TARGET.md` have been run and observed
on the actual Ubuntu 1080 Ti machine.

## 18. 开发笔记本与目标机边界

当前 README 只描述这个实现目录已经具备的代码与作者机级验证入口。

它不应被理解为以下事项已经在目标机完成：

- Linux 真实依赖安装
- Flask 目标机启动
- SQLite 目标机初始化
- Ollama 目标机联调
- `summary_draft` 目标机实产出
- systemd service / timer 真实运行
- 长时 runtime 稳定性

这些内容应看：

- **DEPLOY_TARGET.md**

## 19. 相关文档

当前实现目录相关说明文件：

- **AGENTS.md**
- **DEPLOY_TARGET.md**

如果要看仓库级说明，而不是实现目录说明，再回到根目录 `README.md`。
