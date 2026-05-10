# AGENTS.md

## Scope

This file applies only to work inside `4-auto-scrapy/`.

It is subordinate to the repository root `AGENTS.md`.
Follow both files, but when implementation details are needed, this file is more specific and therefore takes precedence for code work inside `4-auto-scrapy/`.

## Active guidance sources for implementation work

Inside `4-auto-scrapy/`, the current authoritative non-code guidance should normally come from:

- root `AGENTS.md`
- `4-auto-scrapy/AGENTS.md`
- `2-action/soruces/project-overview-and-plan.md`
- `2-action/soruces/爬虫项目方案设计.md`
- `2-action/soruces/goal命令.md`
- `2-action/soruces/tutor-prompt.md`

By default, do **not** treat the following directories as active implementation guidance sources:

- `1-plan/`
- `3-reference/`

Those are historical discussion/reference areas and should normally be ignored during implementation work unless the user explicitly requests historical review, legacy design recovery, or archived reference lookup.

## Implementation identity

This directory contains the formal implementation of a local-first autonomous web knowledge harvester for CS / AI content.

The production runtime is a Python crawler pipeline.
This is not an OpenClaw-first runtime.
OpenClaw is optional and auxiliary only.

## Fixed implementation architecture

The implementation must preserve the following architecture:

1. Discovery
   - RSS / Atom
   - sitemap
   - curated seed URLs
   - optional SearXNG-assisted discovery

2. Fetch
   - Scrapy first
   - Playwright only when necessary

3. Extract
   - Trafilatura first
   - fallback parser second

4. Storage
   - disk-first storage
   - strict separation of `raw / cleaned / derived`

5. Metadata index
   - SQLite for paths, metadata, statuses, and retrieval pointers
   - not the primary store for large article bodies

6. Local web UI
   - Flask for browsing, viewing, editing, and triggering analysis

7. Runtime
   - separate scheduler + worker
   - systemd for persistence

8. Local AI analysis
   - Python directly calling Ollama

9. OpenClaw
   - optional assistant/control layer only
   - never the main crawler runtime

## Hard constraints

These are implementation-level hard rules:

- Do not use OpenClaw as the main crawler loop.
- Do not move scheduler logic into Flask.
- Do not store large article bodies primarily inside SQLite.
- Do not merge `raw`, `cleaned`, and `derived` into one directory.
- Do not make Playwright the default fetch path.
- Do not introduce paid discovery/search APIs as a hard dependency.
- Do not silently redesign the architecture.
- Do not add destructive operations without explicit approval.
- Do not expand a task into later milestones unless asked.

## Intended directory structure

Unless the repository evolves differently by explicit decision, prefer the following structure inside `4-auto-scrapy/`:

- `pyproject.toml`
- `README.md`
- `app/`
  - `__init__.py`
  - `config.py`
  - `db.py`
  - `models.py`
  - `routes/`
  - `services/`
- `crawler/`
  - spiders
  - fetch helpers
- `config/`
  - source definitions
  - prompt templates
- `scripts/`
  - bootstrap / init / maintenance scripts
- `systemd/`
  - service unit files
- `tests/`
  - smoke tests
  - unit tests
- `instance/`
  - local SQLite file and runtime state
- `data/`
  - `raw/`
  - `cleaned/`
  - `derived/`
  - `logs/`

If the implementation has not yet been scaffolded, create files and directories inside this structure instead of inventing a different layout without approval.

## Layer responsibilities

Keep responsibilities separated:

- Discovery is not fetch.
- Fetch is not extract.
- Extract is not summarize.
- Flask is not scheduler.
- OpenClaw is not the primary runtime.
- Ollama calls for production analysis are direct Python calls, not routed through OpenClaw by default.

## Preferred implementation stack

Preferred defaults for this implementation:

- Python 3.12
- virtual environment: `.venv`
- package management: `uv` preferred
- tests: `pytest`
- lint: `ruff`
- formatting: `ruff format`
- DB migration/init: lightweight SQLite-first approach

If the project later adopts different real tools, update this file to reflect reality rather than keeping stale preferences.

## Command policy

Do not invent fake commands and present them as already working.

Use this rule:

1. If real commands already exist in this directory, use those.
2. If commands do not exist yet, scaffold the project using the preferred toolchain in this file.
3. After scaffolding, document the actual commands here and in `README.md`.

### Preferred commands after scaffold

These are the target conventions Codex should prefer when creating the project:

- create/update env:
  - `uv sync`

- run Flask UI:
  - `uv run flask --app app run`

- run tests:
  - `uv run pytest`

- authoring-level discovery smoke check:
  - `uv run python -c "from app.discovery import run_discovery; print(run_discovery())"`
  - `uv run --with pytest pytest tests/test_discovery_smoke.py`

- authoring-level fetch smoke check:
  - `uv run --with pytest pytest tests/test_fetch_smoke.py`

- authoring-level extract smoke check:
  - `uv run --with pytest pytest tests/test_extract_smoke.py`

- authoring-level versioning smoke check:
  - `uv run --with pytest pytest tests/test_versioning_smoke.py`

- authoring-level Flask UI smoke check:
  - `uv run --with pytest pytest tests/test_ui_smoke.py`

- authoring-level analysis smoke check:
  - `uv run --with pytest pytest tests/test_analysis_smoke.py`

- authoring-level runtime smoke check:
  - `uv run --with pytest pytest tests/test_runtime_smoke.py`

- run bounded runtime once outside Flask:
  - `uv run python -m app.runtime`

- run the bounded M3-M10 regression suite in one command:
  - `uv run python scripts/run_regression.py`

- lint:
  - `uv run ruff check .`

- format:
  - `uv run ruff format .`

If the actual entrypoints differ, replace these with the real commands once implemented.

## Database rules

- SQLite stores metadata, file paths, statuses, version references, and retrieval pointers.
- SQLite is not the primary store for raw article bodies.
- Keep schema simple and local-first.
- Design for tables such as:
  - `sources`
  - `documents`
  - `document_versions`
  - `crawl_runs`
  - `tags`
- If migrations are introduced, keep them minimal and explicit.
- Do not manually corrupt or rewrite migration history.

## Storage rules

The filesystem is the content store.

Use and preserve clear separation:

- `data/raw/`
  Raw fetched HTML / source payloads

- `data/cleaned/`
  Extracted clean markdown / normalized content

- `data/derived/`
  AI-generated summaries, tags, improved text, and later-stage artifacts

- `data/logs/`
  crawler, worker, and runtime logs

Never collapse these into a single flat directory.

## Fetch and browser rules

- Fetch must be HTTP-first.
- Use Scrapy as the primary fetch mechanism.
- Use Playwright only when necessary for JS-heavy or render-dependent pages.
- Do not default every site to browser automation.
- Prefer low-cost and low-exposure fetch paths.
- Record fetch status and failures explicitly.

## Extract rules

- Trafilatura is the first extractor.
- Fallback parser is second.
- Preserve raw input separately from cleaned output.
- Extraction failure must be visible in status/logs, not silently ignored.
- Preserve key metadata such as title, date, author, URL, and extraction status when available.

## Ollama and analysis rules

- Production analysis path is Python directly calling Ollama.
- Use explicit model calls and explicit prompt templates.
- Derived outputs must be versioned where practical.
- Prefer summary / tags / improved content as separate derived artifacts.
- Keep prompt and model usage inspectable.
- Avoid uncontrolled context growth.

## Flask rules

- Flask is for local browsing, inspection, manual triggering, and operator workflows.
- Flask must not become the scheduler host.
- Flask routes should stay thin and delegate work to services.
- Do not bury crawler logic inside route handlers.

## Scheduler and worker rules

- Scheduler and worker are separate concerns.
- Scheduler dispatches work.
- Worker executes discovery / fetch / extract / analyze / store steps.
- Use systemd for persistence and restart behavior.
- Failures must be logged and reflected in DB / status outputs.
- Avoid hidden background loops inside Flask or ad-hoc scripts.

## Read-first and diff-first rules

Before modifying existing code:

1. inspect relevant files first
2. explain current behavior
3. identify the smallest correct change
4. prefer minimal diffs over broad rewrites

Before implementation-oriented work in this directory:

1. read current authoritative guidance first:
   - root `AGENTS.md`
   - this file
   - relevant files in `2-action/soruces/`
2. summarize the task and fixed constraints
3. identify relevant files
4. state assumptions, risks, and unknowns
5. if the task is large, propose a milestone-first plan before editing

Only consult `1-plan/` or `3-reference/` when explicitly requested or when historical reasoning or archived references are truly needed.

Do not rewrite large files if a narrow change is sufficient.

## Code transparency rules

Before editing:

1. explain the goal
2. explain which files will change
3. explain the main risks
4. explain which commands you plan to run

After editing:

1. provide a diff-style summary
2. explain why each changed file changed
3. report tests, checks, or manual verification
4. report unresolved risks or follow-up work

## Task discipline

For implementation work:

- do one milestone at a time
- keep scope explicit
- avoid cross-milestone sprawl
- stop when architecture decisions are needed
- stop when requested work conflicts with existing project documents
- stop before destructive operations

## Testing and verification

A task is not done merely because code was written.

### Minimum expectations

Every completed milestone should leave behind:

- runnable code or a valid implementation artifact for the current stage
- a clear verification path
- minimal tests or smoke checks appropriate to the current machine
- an explicit note of what remains

### Preferred verification ladder

1. static sanity check
   - imports resolve
   - config loads
   - module structure is coherent

2. authoring-level smoke check on the current machine
   - app skeleton can be constructed where applicable
   - config and paths behave as expected
   - project structure matches the intended layout

3. target-runtime validation on the deployment machine
   - real dependency installation
   - Flask startup under the real environment
   - SQLite initialization under the real environment
   - Ollama connectivity
   - crawler execution
   - scheduler / worker / systemd behavior

4. manual validation notes
   - explain what was verified now
   - explain what remains deferred to the target machine

Do not claim target-runtime verification unless it was actually performed on the target machine.

## Validation mode rule

At the current stage, Codex is used only on the development laptop.

For this project, the environment split is fixed:

- development laptop = the only Codex interaction environment
- target 1080 Ti machine = no Codex, only OpenClaw and the real runtime environment

Therefore, distinguish clearly between:

- authoring-level validation on the development laptop with Codex
- target-runtime validation on the 1080 Ti deployment machine without Codex

Authoring-level validation may include:
- file structure checks
- config parsing
- import sanity
- minimal app construction checks
- documentation consistency

Target-runtime validation may include:
- real dependency installation
- Flask startup under the real environment
- SQLite initialization under the real environment
- Ollama/OpenClaw runtime behavior
- crawler execution
- scheduler / worker / systemd behavior

Do not claim target-runtime verification unless it was actually performed on the target machine.
Do not generate plans that assume Codex will continue operating on the target machine.
Always report which checks were performed on the development laptop and which are deferred to the 1080 Ti target machine.

## What "done" means in this directory

For implementation tasks, done means:

- the requested scope is satisfied
- the change is inside `4-auto-scrapy/`
- architecture constraints remain intact
- the correct layer handled the task
- relevant checks or smoke tests were run
- remaining risks or follow-up work were reported

## First-task rule for an empty implementation directory

If `4-auto-scrapy/` is still mostly empty, do not jump directly into complex feature work.

Preferred first sequence:

1. scaffold project structure
2. add `pyproject.toml`
3. add base config and app skeleton
4. add first smoke-test or import/config sanity path
5. only then proceed to SQLite schema / initialization as the next milestone
6. only after that proceed to discovery / fetch / extract milestones

## Documentation rule

Whenever a real command, entrypoint, directory, or workflow becomes established here:

- update this file
- update `README.md`
- keep both consistent

This file should describe how the implementation actually works, not how it worked in an earlier draft.
