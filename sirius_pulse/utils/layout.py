from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote


_DEFAULT_SESSION_FILENAME_SQLITE = "session_state.db"
_DEFAULT_SESSION_FILENAME_JSON = "session_state.json"


@dataclass(slots=True)
class WorkspaceLayout:
    """Single authority for config-root and data-root persistence paths."""

    work_path: Path
    config_path: Path | None = None
    layout_version: int = 2

    def __post_init__(self) -> None:
        self.work_path = Path(self.work_path)
        self.config_path = self.work_path if self.config_path is None else Path(self.config_path)

    @property
    def data_root(self) -> Path:
        return self.work_path

    @property
    def config_root(self) -> Path:
        assert self.config_path is not None
        return self.config_path

    @property
    def root(self) -> Path:
        return self.data_root

    def workspace_manifest_path(self) -> Path:
        return self.config_root / "workspace.json"

    def config_dir(self) -> Path:
        return self.config_root / "config"

    def session_config_path(self) -> Path:
        return self.config_dir() / "session_config.json"

    def providers_dir(self) -> Path:
        return self.config_root / "providers"

    def provider_registry_path(self) -> Path:
        return self.providers_dir() / "provider_keys.json"

    def sessions_dir(self) -> Path:
        return self.data_root / "sessions"

    def session_slug(self, session_id: str) -> str:
        text = str(session_id).strip() or "default"
        return quote(text, safe="")

    def session_id_from_slug(self, slug: str) -> str:
        text = str(slug).strip()
        return unquote(text) if text else "default"

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_dir() / self.session_slug(session_id)

    def session_store_path(self, session_id: str, *, backend: str = "sqlite") -> Path:
        normalized = backend.strip().lower()
        file_name = _DEFAULT_SESSION_FILENAME_SQLITE
        if normalized == "json":
            file_name = _DEFAULT_SESSION_FILENAME_JSON
        return self.session_dir(session_id) / file_name

    def session_participants_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "participants.json"

    def primary_user_path(self) -> Path:
        return self.data_root / "primary_user.json"

    def persisted_session_bundle_path(self) -> Path:
        return self.config_root / "session_config.persisted.json"

    def memory_dir(self) -> Path:
        return self.data_root / "memory"

    def user_memory_dir(self) -> Path:
        return self.memory_dir() / "users"

    def event_memory_dir(self) -> Path:
        return self.memory_dir() / "events"

    def event_memory_path(self) -> Path:
        return self.event_memory_dir() / "events.json"

    def self_memory_path(self) -> Path:
        return self.memory_dir() / "self_memory.json"

    def token_dir(self) -> Path:
        return self.data_root / "token"

    def token_usage_db_path(self) -> Path:
        return self.token_dir() / "token_usage.db"

    def roleplay_dir(self) -> Path:
        return self.config_root / "roleplay"

    def generated_agents_path(self) -> Path:
        return self.roleplay_dir() / "generated_agents.json"

    def generated_agent_trace_dir(self) -> Path:
        return self.roleplay_dir() / "generated_agent_traces"

    def skills_dir(self) -> Path:
        return self.config_root / "skills"

    def skill_data_dir(self) -> Path:
        return self.data_root / "skill_data"

    def config_watch_paths(self) -> list[Path]:
        paths = [
            self.workspace_manifest_path(),
            self.session_config_path(),
            self.provider_registry_path(),
            self.generated_agents_path(),
        ]
        skills_dir = self.skills_dir()
        if skills_dir.exists():
            paths.extend(sorted(skills_dir.glob("*.py")))
            paths.append(skills_dir / "README.md")
        return paths

    def ensure_directories(self, *, session_id: str | None = None) -> None:
        directories = [
            self.config_root,
            self.data_root,
            self.config_dir(),
            self.providers_dir(),
            self.sessions_dir(),
            self.user_memory_dir(),
            self.event_memory_dir(),
            self.token_dir(),
            self.roleplay_dir(),
            self.generated_agent_trace_dir(),
            self.skills_dir(),
            self.skill_data_dir(),
        ]
        if session_id is not None:
            directories.append(self.session_dir(session_id))
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)