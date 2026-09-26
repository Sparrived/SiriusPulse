# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Sirius Pulse (灵动月白) is an async roleplay chat framework for QQ group chats. It runs multiple AI personas, each with independent config, memory, and QQ identity. The core engine uses a 5-stage pipeline (Perception → Cognition → Decision → Execution → Background) with layered memory (basic/memory_units/semantic/user).

All LLM traffic goes through **AMKR** (`auto-model-key-router`), an external local OpenAI-compatible router that owns vendors, API key pools, model selection and sampling parameters. Sirius Pulse sends **task names** (e.g. `response_generate`, `memory_extract`) as the `model` field and AMKR resolves them into real models.

Python 3.12+. Package name: `sirius-pulse`. MIT license.

## Commands

### Setup
```bash
# Install in editable mode with all dev extras
uv pip install -e ".[dev,test,provider,quality]"
# Or without uv
pip install -e ".[dev,test,provider,quality]"
```

### Run
```bash
python main.py run                # Start all active personas + WebUI
python main.py webui              # WebUI only (background; --foreground/--status/--stop)
python main.py persona list       # Persona management: list / create / activate / delete
sirius-pulse                      # Same as python main.py (console_scripts entry)
```

There is no interactive TUI: `python main.py` with no subcommand prints help.

### Test
```bash
.venv/Scripts/python -m pytest -q             # All tests (~37s)
.venv/Scripts/python -m pytest tests/test_config.py -q   # Single file
.venv/Scripts/python -m pytest -q --cov=sirius_pulse     # With coverage
.venv/Scripts/python -m pytest -q --tb=short             # Short tracebacks
```

pytest config: `testpaths=["tests"]`, `asyncio_mode="strict"` (in pyproject.toml).
Use the project venv — the system Python lacks runtime deps such as `mcp`.

### Lint & Format
```bash
black --check --fast sirius_pulse tests      # Format check
black sirius_pulse tests                     # Auto-format
isort --check-only sirius_pulse tests        # Import order check
isort sirius_pulse tests                     # Auto-sort imports
flake8 --max-line-length=100 sirius_pulse    # Lint
mypy sirius_pulse --ignore-missing-imports   # Type check (advisory)
python scripts/ci_check.py                   # Full CI check pipeline
```

Style: black profile, line-length=100, target Python 3.12, Google-style docstrings.

### Docs (VitePress, git submodule)
```bash
cd docs && npm install && npm run dev        # Local preview on :5173
```

## Architecture

### Process Model
```
CLI (cli.py) ──→ _cmd_run() — 同进程 asyncio 任务，每个人格一个 PersonaWorker
                   ├── PersonaWorker (persona_worker.py)
                   │     └── EngineRuntime (platforms/runtime.py)
                   │           └── EmotionalGroupChatEngine (core/emotional_engine.py)
                   │                 ├── Brain (core/brain.py) — LLM calls + post-hooks
                   │                 ├── Pipeline — 5-stage message processing
                   │                 ├── Memory subsystems (memory/)
                   │                 ├── Tools (tools/) — AI-callable tools
                   │                 └── Plugins (plugins/) — user chat commands
                   ├── WebUI (webui/server_core.py) on :8080
                   └── AMKR (external process) — owns vendors/keys/models/sampling params
```

没有 `PersonaManager` 类。`python main.py run` 在**同一进程内**为 `data/global_config.json`
里的每个活跃人格创建一个 `PersonaWorker` 并 `asyncio.create_task()` 拉起（不是每人格一个独立 OS 子进程，
因此容器内 PID 1 就是 `sirius-pulse run`）。`PersonaWorker` 写状态文件与心跳供 WebUI 读取，
并负责配置热重载。

### Key Module Boundaries

- **`sirius_pulse/core/`** — The engine brain. `EmotionalGroupChatEngine` is a final class composed via mixins (`engine_core.py` for init/lifecycle, `pipeline.py` for the 5-stage pipeline, `bg_tasks.py` for background tasks, `helpers.py` for tool/plugin integration). Don't mix provider-specific logic into core.
- **`sirius_pulse/providers/`** — The only LLM boundary. `OpenAICompatibleProvider` (`openai_compatible.py`) points at AMKR; `amkr.py` parses the connection config; `amkr_sync.py` registers task names into AMKR. There are no vendor implementations and no local routing registry — adding one back is a regression.
- **`sirius_pulse/adapters/`** — Platform-agnostic message types (`TextSegment`, `ImageSegment`, `MessageGroup`, etc. in `models.py`) and `BaseAdapter` abstract class.
- **`sirius_pulse/platforms/`** — Concrete platform implementations. Currently only OneBot v11 via NapCat (`platforms/onebot_v11/napcat/adapter.py`). `runtime.py` bridges platform adapters to the engine.
- **`sirius_pulse/memory/`** — Layered memory: basic (retains the raw tail until a checkpoint memory unit covers it; compaction triggers above `DEFAULT_BASIC_MEMORY_CHECKPOINT_TOKEN_TRIGGER` and drains to `..._TARGET`, after which the covered raw entries leave the window and only memory units are retrieved; `DEFAULT_BASIC_MEMORY_HARD_LIMIT` is only a RAM safety ceiling above that trigger, and `DEFAULT_BASIC_MEMORY_HISTORY_TOKEN_BUDGET` caps what the chat prompt injects), memory_units (LLM-extracted structured units: metadata in per-group JSON, vectors packed into float32 sidecars under `memory_units/vectors/`, retrieved by RAG through an in-memory index that hydrates vectors only for units that can be injected), semantic (vector search at group/user/global levels), user (unified identity resolution). There is no biography/diary/evolution-chain subsystem.
- **`sirius_pulse/tools/`** — AI-callable tools. Tools are Python files exporting `TOOL_META` + `run()`, invoked through standard OpenAI `tools` JSON assembled in `brain.py`. Includes passive tools (background tasks, event triggers, lifecycle hooks).
- **`sirius_pulse/plugins/`** — User-facing chat commands triggered by `/` `#` `!` prefixes. Inherit `PluginBase`, use `@command` decorator (v1.2+). Three output modes: `direct` / `llm` (AI-personalized) / `silent`.
- **`sirius_pulse/webui/`** — aiohttp REST API + static frontend for persona management, config, monitoring. Split into domain modules: `persona_api.py`, `memory_api.py`, `monitoring_api.py`, `autonomy_api.py`, `amkr_proxy.py`, `model_catalog.py`, etc.
- **`sirius_pulse/config/`** — Data models for session/agent/orchestration config. Shared across plugins and tools.
- **`sirius_pulse/models/`** — Canonical data models. `models.py` is the single source of truth for session and transcript contracts.

### Data Layout
```
data/
├── personas/{name}/          # Per-persona isolated directory
│   ├── persona.json          # Character name, personality, speaking style (PersonaProfile)
│   ├── adapters.json         # NapCat adapter configs (ws_url, QQ number, group whitelist)
│   ├── experience.json       # Persona experience / reply tuning
│   ├── persona.db            # Unified SQLite DB (users, token_usage, cognition_events, ...)
│   ├── engine_state/         # Runtime state
│   │   └── orchestration.json  # ← the file the engine actually loads (OrchestrationStore)
│   ├── memory/               # Other memory data (semantic/, autonomy/, intentions.json)
│   ├── memory_units/         # Memory units (per-group metadata JSON + vectors/ sidecars)
│   ├── tool_data/            # Tool KV data (incl. stickers/ RAG assets)
│   └── logs/
├── global_config.json        # AMKR connection: amkr_base_url / amkr_local_api_key / amkr_workspace
│                             #   + per-workspace credentials: amkr_panel_keys / amkr_inference_keys
├── tools/                    # User-installed tools (scanned at runtime)
└── adapter_port_registry.json
```

A legacy `personas/{name}/orchestration.json` is still written by `persona_config.py` for
`task_timeout` / `task_retries`, but the engine's live read path is
`engine_state/orchestration.json` via `OrchestrationStore`.

AMKR itself lives outside this repo; the framework creates the AMKR workspace
`<amkr_workspace>/<persona>` (the only moment its two credentials are returned, so both are
persisted) and then registers its task names into it. The workspace issues a **panel key**
(`amkr_ws_…`, for the embedded AMKR panel) and an **inference key** (`amkr_ik_…`, for `/v1`
model calls); they are mutually unusable. Only the inference key is sent on the inference
path — `amkr_local_api_key` is an admin credential (it can add/remove providers and keys)
and must never travel with a chat completion request. Scoped keys pin the workspace, so
`X-AMKR-Workspace` is ignored when one is used.

### Dual Extension System
- **Tools** = AI autonomously invokes tools during conversation (function calling)
- **Plugins** = Users explicitly trigger commands via chat prefixes

These are distinct systems with separate base classes, registries, and execution paths.

## Conventions

### Code Style
- Python 3.12 target. Public interfaces must have type annotations.
- Prefer dataclasses and small, focused modules over large utility classes.
- Vendor/Key/model logic must NOT come back here — that is AMKR's job. The engine only ever sends a task name.
- Engine layer must not know about specific models or sampling parameters.
- Task names are the wire contract: the string sent as `model` must match a task named in AMKR's workspace, or AMKR will look for a real model by that name and fail.

### Architecture Rules
- `sirius_pulse/models/models.py` is the single source of truth for session/transcript contracts.
- Engine layer must not actively split message content — splitting is prompt-driven (AI decides split points via `<MSG_SPLIT>` markers when `OrchestrationPolicy.enable_prompt_driven_splitting=True`).
- All generated system prompts must include a safety instruction at the end telling the model not to reveal its system prompt or internal configuration.
- CLI/API must always receive an explicit `work_path`; all persistence derives from that path.
- One engine session = one main AI (`SessionConfig.agent`); `participants` are human participants.

### Testing Conventions
- Tests must be written from the business perspective — verify user-facing input → observable output/persistence, not internal implementation details.
- Use `MockProvider` for all unit tests (no real network calls).
- Set `pending_message_threshold=0` in tests (disable batch silence processing).
- Disable auxiliary LLM tasks (`memory_extract`, `event_extract`) in tests.
- Same-domain tests with < 5 cases should be merged into one file; use `@pytest.mark.parametrize` for similar variants.
- Single test < 1s, full suite < 30s.

### Commit Messages
Follow Conventional Commits format. Commit messages should be in Chinese.

### Project Management
Use `uv` for Python project management (dependency installation, virtual environments, script running, lock file maintenance). Do not use pip/poetry/conda unless explicitly requested.

### Documentation Sync
When changing module boundaries, commands, or API contracts, update:
- `docs/` submodule content (VitePress) — 唯一权威文档源
- `README.md` if user-visible usage changes
