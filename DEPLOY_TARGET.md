# DEPLOY_TARGET

This document describes the intended target-machine deployment model for
`24-data-collector/`.

It is written for the Ubuntu 1080 Ti runtime machine. It does not assume Codex is
available on that machine, and it should not be read as proof that long-running
target deployment has already been validated.

## 1. Ultimate Deployment Model

The intended 24/7 target-machine design is not a permanent Python crawler loop.

`app.runtime` is a bounded one-shot pipeline:

```text
discovery -> fetch -> extract -> optional analysis
```

The process should run once, write database records, artifacts, and logs, then
exit. Long-running behavior should be provided by `systemd`, not by an infinite
loop inside Python, Flask, or OpenClaw.

This preserves the project boundaries:

- Discovery is not Fetch
- Fetch is not Extract
- Extract is not Analyze
- Flask is not Scheduler
- OpenClaw is not the main crawler runtime
- SQLite is metadata/index storage, not the body store

## 2. Target-Machine Service Layout

The recommended deployment separates collection, analysis, model serving, and UI.

| Component | Runs Continuously? | Purpose | Recommended Mode |
|---|---:|---|---|
| `ollama` | Yes | Hosts the local model API, normally on `127.0.0.1:11434` | System service |
| `auto-scrapy-collection.service` | No | Runs `discovery -> fetch -> extract` | `systemd` oneshot |
| `auto-scrapy-collection.timer` | Yes | Starts collection on a schedule | Enabled at boot |
| `auto-scrapy-analysis.service` | No | Runs capped Ollama analysis | Manual first |
| `auto-scrapy-analysis.timer` | Optional | Scheduled capped analysis | Add only after safe manual runs |
| `auto-scrapy-ui.service` | Optional | Runs local Flask browsing UI | Bind to `127.0.0.1` |

The key idea is:

```text
systemd stays alive
auto-scrapy runs bounded jobs
Ollama stays available as the model server
Flask remains an optional local browser UI
```

## 3. Deployment Path And User Placeholders

Target-machine examples should use placeholders until the real Ubuntu path and
user are known:

```bash
PROJECT_DIR=/opt/auto-scrapy/24-data-collector
APP_USER=<target-linux-user>
UV_BIN=<output-of-which-uv>
```

Example:

```bash
cd "$PROJECT_DIR"
git pull
uv sync
```

Do not copy a Windows `.venv` to Ubuntu. Dependencies should be rebuilt on the
target machine with `uv sync`.

## 4. Source Config Resolution On Target

The normal project source config is:

```text
config/sources/target_smoke_sources.toml
```

The demo config is fixture/regression-only:

```text
config/sources/demo_sources.toml
```

Current source config resolution order:

1. `--config-path`
2. `AUTO_SCRAPY_SOURCES_CONFIG_PATH`
3. `config/sources/target_smoke_sources.toml`
4. `config/sources/demo_sources.toml` only if target smoke config is absent

For deployment, it is still useful to set the environment variable explicitly so
operator behavior is visible:

```ini
AUTO_SCRAPY_SOURCES_CONFIG_PATH=config/sources/target_smoke_sources.toml
```

## 5. Shared Environment File

A target machine can use a shared environment file such as:

```text
/etc/auto-scrapy.env
```

Recommended contents:

```ini
AUTO_SCRAPY_SOURCES_CONFIG_PATH=config/sources/target_smoke_sources.toml
AUTO_SCRAPY_OLLAMA_MODEL=qwen2.5-coder:7b
AUTO_SCRAPY_OLLAMA_BASE_URL=http://127.0.0.1:11434
AUTO_SCRAPY_OLLAMA_TIMEOUT_SECONDS=120
AUTO_SCRAPY_DATABASE_PATH=instance/auto_scrapy.sqlite3
```

These settings keep deployment behavior explicit:

| Variable | Purpose |
|---|---|
| `AUTO_SCRAPY_SOURCES_CONFIG_PATH` | Forces target source config |
| `AUTO_SCRAPY_OLLAMA_MODEL` | Pins the Ollama model |
| `AUTO_SCRAPY_OLLAMA_BASE_URL` | Points analysis to local Ollama |
| `AUTO_SCRAPY_OLLAMA_TIMEOUT_SECONDS` | Bounds Ollama request time |
| `AUTO_SCRAPY_DATABASE_PATH` | Keeps SQLite metadata path stable |

Runtime artifacts should stay out of Git:

```text
data/raw/
data/cleaned/
data/derived/
data/logs/
instance/auto_scrapy.sqlite3
```

## 6. Manual Target-Machine Validation First

Before enabling timers, validate the target machine manually.

Collection-only run:

```bash
cd "$PROJECT_DIR"
uv run python -m app.runtime --skip-analysis
```

This runs:

```text
discovery -> fetch -> extract
```

Small capped analysis run:

```bash
uv run python -m app.runtime --source-key openai-news --analysis-limit-per-source 2
```

This verifies Ollama integration without pushing the GPU too hard.

Useful checks:

```bash
uv run python -c "from app.config import load_settings; s=load_settings(); print(s.sources_config_path); print(s.ollama_model); print(s.database_path)"
uv run python -c "from app.db import init_db; print(init_db())"
uv run python -m app.runtime --help
```

If analysis is enabled, verify Ollama separately:

```bash
ollama list
curl http://127.0.0.1:11434/api/tags
```

The configured model should be available, normally:

```text
qwen2.5-coder:7b
```

## 7. Collection Service

Collection should be the default scheduled job because it is mostly
network/CPU/disk work and lower GPU pressure.

Template:

```ini
[Unit]
Description=Auto Scrapy collection run
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=oneshot
User=<target-linux-user>
Group=<target-linux-user>
WorkingDirectory=/opt/auto-scrapy/24-data-collector
EnvironmentFile=/etc/auto-scrapy.env
ExecStart=<uv-bin> run python -m app.runtime --skip-analysis

Nice=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Example `ExecStart` after checking `which uv`:

```ini
ExecStart=/home/<target-linux-user>/.local/bin/uv run python -m app.runtime --skip-analysis
```

## 8. Collection Timer

The collection timer should run the bounded collection service on a schedule.

Template:

```ini
[Unit]
Description=Run Auto Scrapy collection daily

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
RandomizedDelaySec=15m
Unit=auto-scrapy-collection.service

[Install]
WantedBy=timers.target
```

Recommended behavior:

| Setting | Reason |
|---|---|
| `OnCalendar=*-*-* 03:00:00` | Runs daily at 03:00 |
| `Persistent=true` | Catches missed runs after reboot |
| `RandomizedDelaySec=15m` | Avoids hitting sites at exactly the same second |
| `Type=oneshot` service | Runtime exits after one bounded pipeline pass |

## 9. Capped Analysis Service

Analysis should be separate from collection because it calls Ollama and may heat
the 1080 Ti.

Template:

```ini
[Unit]
Description=Auto Scrapy capped analysis run
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=oneshot
User=<target-linux-user>
Group=<target-linux-user>
WorkingDirectory=/opt/auto-scrapy/24-data-collector
EnvironmentFile=/etc/auto-scrapy.env
ExecStart=<uv-bin> run python -m app.runtime --analysis-limit-per-source 5

Nice=15
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Start manually first:

```bash
sudo systemctl daemon-reload
sudo systemctl start auto-scrapy-analysis.service
journalctl -u auto-scrapy-analysis.service -n 100 --no-pager
```

Do not enable scheduled analysis until multiple manual runs show safe GPU
temperature and acceptable runtime duration.

## 10. Optional Analysis Timer

Only add this after safe manual validation.

Example:

```ini
[Unit]
Description=Run Auto Scrapy capped analysis twice weekly

[Timer]
OnCalendar=Tue,Fri *-*-* 04:30:00
Persistent=true
RandomizedDelaySec=30m
Unit=auto-scrapy-analysis.service

[Install]
WantedBy=timers.target
```

For the 1080 Ti target machine, uncapped analysis should not be scheduled
automatically.

## 11. Optional Flask UI Service

Flask is for browsing and inspection only. It is not the scheduler.

Template:

```ini
[Unit]
Description=Auto Scrapy local Flask UI
After=network-online.target

[Service]
Type=simple
User=<target-linux-user>
Group=<target-linux-user>
WorkingDirectory=/opt/auto-scrapy/24-data-collector
EnvironmentFile=/etc/auto-scrapy.env
ExecStart=<uv-bin> run flask --app app run --host 127.0.0.1 --port 5050
Restart=on-failure
RestartSec=5

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Access through SSH tunnel:

```bash
ssh -L 5050:127.0.0.1:5050 <target-linux-user>@<target-machine-ip>
```

Then open on the development laptop:

```text
http://127.0.0.1:5050
```

Do not bind Flask to `0.0.0.0` unless firewall and access control are handled
separately.

## 12. Recommended Operating Schedule

| Job | Command Shape | Frequency | Reason |
|---|---|---:|---|
| Collection | `app.runtime --skip-analysis` | Daily | Low GPU pressure |
| Capped analysis | `app.runtime --analysis-limit-per-source 5` | Manual first, then optional weekly/twice weekly | Protect 1080 Ti |
| Flask UI | `flask --app app run --host 127.0.0.1` | Optional always-on | Local inspection |
| Full uncapped analysis | `app.runtime` | Avoid for now | GPU heat and long runtime risk |

## 13. Monitoring Commands

Service status:

```bash
sudo systemctl status auto-scrapy-collection.service
sudo systemctl status auto-scrapy-collection.timer
sudo systemctl status auto-scrapy-analysis.service
sudo systemctl status auto-scrapy-ui.service
```

Service logs:

```bash
journalctl -u auto-scrapy-collection.service -n 200 --no-pager
journalctl -u auto-scrapy-analysis.service -n 200 --no-pager
journalctl -u auto-scrapy-ui.service -n 100 --no-pager
```

Timer status:

```bash
systemctl list-timers --all | grep auto-scrapy
```

Runtime logs:

```bash
ls -lh data/logs/
tail -n 100 data/logs/runtime-run-*.log
```

GPU monitoring:

```bash
watch -n 5 nvidia-smi
```

## 14. Update Flow

When new code is pushed from the development machine:

```bash
cd "$PROJECT_DIR"
git pull
uv sync
sudo systemctl restart auto-scrapy-ui.service
```

The next timer run will use the updated code.

For immediate validation:

```bash
sudo systemctl start auto-scrapy-collection.service
journalctl -u auto-scrapy-collection.service -n 100 --no-pager
```

## 15. What Not To Do

Do not run the crawler as a permanent Python `while True` process.

Do not put scheduler logic into Flask.

Do not make OpenClaw the main crawler runtime.

Do not schedule uncapped analysis on the 1080 Ti.

Do not expose Flask to the LAN or public network without separate security
controls.

Do not treat development-laptop validation as target-machine deployment
validation.

## 16. Minimal Target-Machine Done Criteria

The target deployment should only be considered minimally validated after these
succeed on the Ubuntu target machine:

```bash
uv sync
uv run python -c "from app.db import init_db; print(init_db())"
uv run python -m app.runtime --help
uv run python -m app.runtime --skip-analysis
uv run python -m app.runtime --source-key openai-news --analysis-limit-per-source 2
sudo systemctl start auto-scrapy-collection.service
sudo systemctl enable --now auto-scrapy-collection.timer
```

Also confirm:

- `instance/auto_scrapy.sqlite3` exists
- `data/logs/` contains new runtime and stage logs
- `data/raw/` and `data/cleaned/` receive artifacts
- Flask `/runs` can show runtime records if UI is enabled
- Ollama is reachable before analysis validation
- `systemctl list-timers --all` shows the collection timer
- GPU temperature is safe during analysis

## 17. Deployment Status Note

This document describes the intended ultimate deployment model.

It should not be read as proof that the target 1080 Ti machine has already
completed long-running production validation. That validation must be performed
directly on the target Ubuntu system, without assuming Codex is present there.
