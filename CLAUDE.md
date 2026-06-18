# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Sirius Pulse (灵动月白) is an async roleplay chat framework for QQ group chats. It runs multiple AI personas as isolated OS subprocesses, each with independent config, memory, and QQ identity. The core engine uses a 5-stage pipeline (Perception → Cognition → Decision → Execution → Background) with layered memory (basic/diary/semantic/biography/glossary) and supports multiple LLM providers with auto-routing.

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
python main.py                    # Interactive TUI (Textual-based)
python main.py run                # Start all personas + WebUI
python main.py webui              # Background WebUI only
python main.py persona start <n>  # Start single persona
sirius-pulse                      # Same as python main.py (console_scripts entry)
```

### Test
```bash
pytest -q                                    # All tests (~2s)
pytest tests/test_config.py -q               # Single file
pytest -q --cov=sirius_pulse                 # With coverage
pytest -q --tb=short                         # Short tracebacks
```

pytest config: `testpaths=["tests"]`, `asyncio_mode="strict"` (in pyproject.toml).

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
CLI (cli.py) ──→ PersonaManager (persona_manager.py)
                   ├── spawns → PersonaWorker subprocess (persona_worker.py)
                   │               └── EngineRuntime (platforms/runtime.py)
                   │                     └── EmotionalGroupChatEngine (core/emotional_engine.py)
                   │                           ├── Brain (core/brain.py) — LLM calls + post-hooks
                   │                           ├── Pipeline — 5-stage message processing
                   │                           ├── Memory subsystems (memory/)
                   │                           ├── Skills (skills/) — AI-callable tools
                   │                           └── Plugins (plugins/) — user chat commands
                   ├── spawns → WebUI (webui/server_core.py) on :8080
                   └── manages → NapCat instances (QQ OneBot v11 gateway)
```

Each persona runs as an isolated subprocess (`python -m sirius_pulse.persona_worker --config data/personas/<name>`). PersonaManager monitors health via heartbeat files and manages WebSocket port allocation (starting from 3001).

### Key Module Boundaries

- **`sirius_pulse/core/`** — The engine brain. `EmotionalGroupChatEngine` is a final class composed via mixins (`engine_core.py` for init/lifecycle, `pipeline.py` for the 5-stage pipeline, `bg_tasks.py` for background tasks, `helpers.py` for skill/plugin integration). Don't mix provider-specific logic into core.
- **`sirius_pulse/providers/`** — LLM provider abstraction. All providers implement `LLMProvider` from `base.py`. `AutoRoutingProvider` handles multi-provider failover. New providers go here only.
- **`sirius_pulse/adapters/`** — Platform-agnostic message types (`TextSegment`, `ImageSegment`, `MessageGroup`, etc. in `models.py`) and `BaseAdapter` abstract class.
- **`sirius_pulse/platforms/`** — Concrete platform implementations. Currently only OneBot v11 via NapCat (`platforms/onebot_v11/napcat/adapter.py`). `runtime.py` bridges platform adapters to the engine.
- **`sirius_pulse/memory/`** — Layered memory: basic (sliding window, 30-msg hard limit), diary (LLM summaries + ChromaDB vectors), semantic (vector search at group/user/global levels), biography (cross-session character profiles), glossary (learnable terms).
- **`sirius_pulse/skills/`** — AI-callable tools. Skills are Python files exporting `SKILL_META` + `run()`. The LLM invokes them via `[SKILL_CALL: name | {params}]`. Includes passive skills (background tasks, event triggers, lifecycle hooks).
- **`sirius_pulse/plugins/`** — User-facing chat commands triggered by `/` `#` `!` prefixes. Inherit `PluginBase`, use `@command` decorator (v1.2+). Three output modes: `direct` / `llm` (AI-personalized) / `silent`.
- **`sirius_pulse/webui/`** — aiohttp REST API + static frontend for persona management, config, monitoring. Split into domain modules: `persona_api.py`, `memory_api.py`, `biography_api.py`, `evolution_api.py`, `monitoring_api.py`, etc.
- **`sirius_pulse/config/`** — Data models for session/agent/orchestration config. Shared across plugins and skills.
- **`sirius_pulse/models/`** — Canonical data models. `models.py` is the single source of truth for session and transcript contracts.

### Data Layout
```
data/
├── personas/{name}/          # Per-persona isolated directory
│   ├── persona.json          # Character name, personality, speaking style
│   ├── orchestration.json    # OrchestrationPolicy, model assignments
│   ├── adapters.json         # NapCat adapter configs (ws_url, QQ number, group whitelist)
│   ├── experience.json       # Persona experience/background
│   └── persona.db            # Unified SQLite DB (memory, tokens, cognition events, session state)
├── providers/                # Global provider configs (API keys, endpoints)
├── skills/                   # User-installed skills (scanned at runtime)
└── adapter_port_registry.json
```

### Dual Extension System
- **Skills** = AI autonomously invokes tools during conversation (function calling)
- **Plugins** = Users explicitly trigger commands via chat prefixes

These are distinct systems with separate base classes, registries, and execution paths.

## Conventions

### Code Style
- Python 3.12 target. Public interfaces must have type annotations.
- Prefer dataclasses and small, focused modules over large utility classes.
- Provider implementations must stay isolated in `sirius_pulse/providers/`.
- Don't add provider dependencies in the engine layer — always go through the provider abstraction.

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
- `.github/skills/framework-quickstart/SKILL.md`
- `docs/` submodule content (VitePress)
- `README.md` if user-visible usage changes
