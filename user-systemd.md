## Phase 1: File System Preparation

Before touching `systemd`, ensure your user has the absolute right to manage the data and logs in the project directory.

1. **Grant Ownership:** Since your project is in `/opt`, ensure your user owns the folder so the scraper can write to SQLite.
```bash
sudo chown -R $USER:$USER /opt/247-data-collector

```


2. **Create the Unit Directory:** Systemd looks in a specific hidden folder for user-level automation.
```bash
mkdir -p ~/.config/systemd/user/

```



---

## Phase 2: Configuration (The Blueprints)

Create these two files exactly as shown. These versions use the **absolute path** to your `uv` binary, which was the key to solving your previous errors.

### 1. The Service File

**Location:** `~/.config/systemd/user/auto-scrapy-runtime.service`
This defines **what** runs.

```ini
[Unit]
Description=Run the auto-scrapy pipeline once
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/247-data-collector

# Environment Variables
Environment=AUTO_SCRAPY_SOURCES_CONFIG_PATH=config/sources/target_smoke_sources.toml
Environment=PYTHONUNBUFFERED=1

# The Execution Engine (Absolute path to your Mamba env's uv)
ExecStart=/home/xuan/xuanStudy/env-utils/miniforge3/envs/247-data-collector/bin/uv run python -m app.runtime

[Install]
WantedBy=default.target

```

### 2. The Timer File

**Location:** `~/.config/systemd/user/auto-scrapy-runtime.timer`
This defines **when** it runs.

```ini
[Unit]
Description=Schedule for auto-scrapy research pipeline

[Timer]
# Wait 5 mins after boot to let the system/network settle
OnBootSec=5m
# Trigger 1 minute after the PREVIOUS task started
OnUnitActiveSec=1m
# Explicitly link to the service above
Unit=auto-scrapy-runtime.service

[Install]
WantedBy=timers.target

```

---

## Phase 3: Deployment (The "Systemd Ritual")

You must run these commands in order to register and activate your automation.

1. **Reload the Daemon:** Tell systemd to scan for the new files.
```bash
systemctl --user daemon-reload

```


2. **Enable and Start the Timer:** You only need to activate the timer; it will handle starting the service.
```bash
systemctl --user enable --now auto-scrapy-runtime.timer

```



---

## Phase 4: Monitoring & Debugging

As a researcher, you need to know if your data collection is healthy. Use these commands to inspect the "heartbeat" of your system.

### 1. Check the Schedule

See exactly when the next crawl is scheduled to occur:

```bash
systemctl --user list-timers

```

### 2. View Live Logs

This is the most important command. It shows you the Scrapy output and your AI processing in real-time:

```bash
journalctl --user -u auto-scrapy-runtime.service -f

```

### 3. Check for Collisions

If the task takes longer than 1 minute, systemd will skip the next trigger. You can verify the status with:

```bash
systemctl --user status auto-scrapy-runtime.service

```

> **Note:** If it says `activating (start)`, it is currently running. If it says `inactive (dead)`, it is waiting for the next timer pulse.

---

## Summary of Logic

* **Collision Prevention:** Because the service is `Type=oneshot`, systemd will never start a second instance if the first one is still processing metadata with your 1080 Ti.
* **Path Integrity:** By using the absolute path to `uv` in your Mamba environment, you ensure that `Scrapy`, `Ollama`, and your specific Python dependencies are always found.
* **Isolation:** This is a **User Unit**. It runs with your permissions, meaning it won't interfere with system-level processes and doesn't require `sudo` for daily management.


