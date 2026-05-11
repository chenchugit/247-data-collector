# auto-scrapy

`4-auto-scrapy/` 是本仓库的正式实现目录。

这里承载的是一个本地优先、磁盘优先的 CS/AI Web 知识采集系统实现，而不是项目级历史讨论区，也不是 OpenClaw 主运行时。

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
4-auto-scrapy/
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

`4-auto-scrapy/` 已经是一个能在开发笔记本上完成 discovery -> fetch -> extract -> summary_draft -> browse -> bounded runtime 的本地实现，但它仍然不是一个已经在 1080 Ti 目标机完成真实长时部署验证的系统。

这句话很重要，因为它同时覆盖了：

- 当前代码已经真实具备的能力
- 当前还没有被文档夸大成“已部署完成”的部分

## 16. 开发笔记本与目标机边界

当前 README 只描述这个实现目录已经具备的代码与作者机级验证入口。

它不应被理解为以下事项已经在 1080 Ti 目标机完成：

- Linux 真实依赖安装
- Flask 目标机启动
- SQLite 目标机初始化
- Ollama 目标机联调
- `summary_draft` 目标机实产出
- systemd service / timer 真实运行
- 长时 runtime 稳定性

这些内容应看：

- **DEPLOY_TARGET_1080T.md**

## 17. 相关文档

当前实现目录相关说明文件：

- **AGENTS.md**
- **DEPLOY_TARGET_1080T.md**

如果要看仓库级说明，而不是实现目录说明，再回到根目录 `README.md`。
