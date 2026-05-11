# DEPLOY_TARGET

本文件是 `24-data-collector/` 在 1080 Ti 目标机上的部署与验证手册。

它只描述目标机应如何安装、验证和启动当前已经在 M1-M10 落地的实现，不把未来能力提前写成既成事实，也不假设目标机上存在 Codex。

## 1. 部署目标

目标机职责：

- 作为真实 runtime 机器运行 `24-data-collector/`
- 安装 Linux 环境下的真实依赖
- 验证 Flask / SQLite / Ollama / runtime / systemd
- 承担长时运行

目标机不承担：

- Codex 交互
- 开发期代码生成
- 用 OpenClaw 充当主 crawler runtime

## 2. 当前实现对应的真实范围

截至 M10，当前目标机上需要承接的是：

- 一个本地 Python crawler pipeline
- 本地 SQLite 元数据索引
- 本地磁盘内容存储
- 本地 Flask 浏览 UI
- 一个有界 runtime 入口
- 一个本地 Ollama 派生产物类型：`summary_draft`
- 一组 systemd 准备文件

当前不应在目标机部署文档里假设已经存在：

- OpenClaw 主控制链路
- Telegram 发送链路
- embeddings / vector search
- 多种 AI 派生产物体系

## 3. 目标机前提

建议目标环境：

- OS: `Ubuntu 24.04.x LTS`
- 项目根目录：`/opt/auto-scrapy/24-data-collector`
- Python: `3.12+`
- `uv`
- `git`
- `curl`
- 可访问本机 Ollama API 的环境

如果系统包名不同，以目标机实际发行版为准，但不要把 Windows `.venv` 直接复制到 Linux 使用。

## 4. 拷贝代码到目标机

推荐把整个仓库同步过去，但实际运行目录以 `24-data-collector/` 为准。

推荐目标路径：

```bash
/opt/auto-scrapy/
└── 24-data-collector/
```

如果你只部署实现目录，也要确保 `24-data-collector/` 内文件完整，包括：

- `app/`
- `config/`
- `crawler/`
- `scripts/`
- `systemd/`
- `tests/`
- `pyproject.toml`
- `uv.lock`

## 5. 目标机环境初始化

进入实现目录：

```bash
cd /opt/auto-scrapy/24-data-collector
```

安装依赖：

```bash
uv sync
```

说明：

- 不要复制开发笔记本的 `.venv`
- 使用目标机本地 `uv sync` 重建环境
- 当前实现依赖以 `pyproject.toml` 和 `uv.lock` 为准

## 6. 必要目录

当前实现依赖以下目录：

- `data/raw/`
- `data/cleaned/`
- `data/derived/`
- `data/logs/`
- `instance/`

这些目录通常会由应用初始化逻辑或运行过程创建；如果目标机有权限或用户隔离要求，应提前确认运行用户对项目目录有读写权限。

## 7. 目标机上的最小启动验证

建议按这个顺序做：

### 7.1 初始化数据库

```bash
uv run python -c "from app.db import init_db; init_db()"
```

### 7.2 确认 runtime 入口存在

```bash
uv run python -m app.runtime --help
```

### 7.3 确认真实目标机 smoke source 配置

普通项目运行在 `config/sources/target_smoke_sources.toml` 存在时默认使用它。目标机首次真实 smoke validation 也可以显式设置同一路径，便于 systemd 和人工排查时确认配置来源：

```bash
export AUTO_SCRAPY_SOURCES_CONFIG_PATH=config/sources/target_smoke_sources.toml
```

该文件包含且只包含当前目标机 smoke 的五个真实来源：

- arXiv cs.AI RSS
- Anthropic sitemap
- OpenAI News seed
- GitHub Changelog seed
- Google Research Blog seed

`config/sources/demo_sources.toml` 只用于显式 fixture / regression，不作为普通项目默认配置，也不作为 1080 Ti 目标机真实 smoke source 配置。

### 7.4 跑一条作者机同构的回归命令

```bash
uv run python scripts/run_regression.py
```

注意：

- 这条命令在开发笔记本已经作为 M10 的稳定验证入口使用
- 在目标机上运行它，才算目标机自己的本地回归验证
- 不应把开发笔记本上的通过结果直接视为目标机已通过

### 7.5 启动 Flask

```bash
uv run flask --app app run --host 0.0.0.0 --port 5000
```

然后再从局域网或本机浏览器验证已有页面，例如：

- `/`
- `/documents`
- `/sources`
- `/runs`

## 8. Ollama 检查

当前实现的分析层依赖本地 Ollama API，默认检查点应包括：

```bash
curl http://localhost:11434/api/tags
```

如果该接口不可用，M8 的 `summary_draft` 派生产物路径不能视为已在目标机验证成功。

当前文档只确认“项目代码支持本地 Ollama 路径”，不确认“目标机上的具体模型已经准备好并成功产出内容”，这一步需要在目标机实际联调。

## 9. systemd 准备文件

当前仓库内已有：

- `systemd/auto-scrapy-runtime.service`
- `systemd/auto-scrapy-runtime.timer`

其设计意图是：

- `service` 执行一次有界 runtime
- `timer` 定时触发该 `service`

当前 service 文件内容使用：

- `WorkingDirectory=%h/auto-scrapy/24-data-collector`
- `Environment=AUTO_SCRAPY_SOURCES_CONFIG_PATH=config/sources/target_smoke_sources.toml`
- `ExecStart=/usr/bin/env sh -lc 'uv run python -m app.runtime'`

这意味着正式部署前通常需要按目标机实际用户和目录做一次对齐。

## 10. systemd 部署建议

先验证 service，再启用 timer。

推荐步骤：

1. 复制并按实际路径调整 unit 文件
2. 先手动运行 service
3. 确认日志、`crawl_runs`、数据目录都正常
4. 再启用 timer

典型步骤示例：

```bash
mkdir -p ~/.config/systemd/user
cp systemd/auto-scrapy-runtime.service ~/.config/systemd/user/
cp systemd/auto-scrapy-runtime.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start auto-scrapy-runtime.service
systemctl --user status auto-scrapy-runtime.service
systemctl --user enable --now auto-scrapy-runtime.timer
systemctl --user list-timers
```

如果你采用 system-level unit 而不是 user-level unit，则路径、用户和权限策略需要自己按部署方式调整。

## 11. 日志与可观测性

当前实现的 operator-facing 可观测性主要来自三处：

- `data/logs/` 下的文件日志
- SQLite `crawl_runs`
- Flask UI 中的 run detail 页面

目标机验证时应至少确认：

- runtime 执行后生成了新的日志文件
- `crawl_runs` 中有对应 run 记录
- UI 中可以看到 run 状态、错误信息和日志可用性

## 12. 恢复与重跑

当前实现采用“有界重跑”而不是复杂恢复框架。

推荐恢复动作：

### 12.1 先做整体回归确认

```bash
uv run python scripts/run_regression.py
```

### 12.2 单独确认 runtime 入口

```bash
uv run python -m app.runtime --help
```

### 12.3 手动执行一轮 runtime

```bash
uv run python -m app.runtime
```

### 12.4 再看 UI 和日志

检查：

- `data/logs/`
- `instance/` 下数据库
- Flask 的 `/runs`

当前没有引入新的持久化恢复模型，也没有引入新的队列状态机；恢复策略以“明确日志 + 可重跑入口 + 现有状态可见性”为主。

## 13. 当前已经在开发笔记本验证过的事

基于 M1-M10，当前已在开发笔记本完成过作者机级验证的典型项目包括：

- Flask app 构造与路由存在
- discovery / fetch / extract / versioning / UI / analysis / runtime smoke tests
- `scripts/run_regression.py` 作为有界回归入口可运行

这些结果只能说明：

- 代码结构和作者机级验证路径已经具备

不能直接说明：

- 1080 Ti 目标机已经安装成功
- 目标机 systemd 已真实稳定运行
- 目标机 Ollama 已真实产出 `summary_draft`

## 14. 仍待 1080 Ti 目标机验证的事项

以下事项必须在目标机上单独验证，不能沿用开发笔记本结论：

- `uv sync` 是否成功
- Flask 是否能在目标机环境启动
- SQLite 初始化是否正常
- Ollama API 是否可连通
- 目标模型是否已安装
- `summary_draft` 是否能真实生成
- systemd service 是否能按实际路径成功执行
- systemd timer 是否能按预期触发
- 长时运行是否稳定

## 15. 部署时最常见的边界错误

避免以下错误：

- 把 Windows `.venv` 拷到 Linux 复用
- 把开发笔记本测试结果当成目标机已验证
- 没先验证 `service` 就直接启用 `timer`
- 让 Flask 充当 scheduler host
- 把 OpenClaw 当成当前阶段的主 runtime
- 把 SQLite 当正文内容主存储

## 16. 与其他文档的关系

如果你要看：

- 项目级全局说明：看仓库根目录 `README.md`
- 实现目录说明：看 `README.md`
- systemd 准备文件：看 `systemd/`
- 当前权威约束：看 `24-data-collector/AGENTS.md`
