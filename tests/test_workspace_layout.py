from __future__ import annotations

from sirius_pulse.utils.layout import WorkspaceLayout


def test_workspace_layout_when_config_and_data_roots_differ_then_paths_use_expected_roots(tmp_path):
    config_root = tmp_path / "config-root"
    data_root = tmp_path / "data-root"
    layout = WorkspaceLayout(data_root, config_path=config_root)

    assert layout.workspace_manifest_path() == config_root / "workspace.json"
    assert layout.session_config_path() == config_root / "config" / "session_config.json"
    assert layout.provider_registry_path() == config_root / "providers" / "provider_keys.json"
    assert layout.generated_agents_path() == config_root / "roleplay" / "generated_agents.json"
    assert layout.token_usage_db_path() == data_root / "token" / "token_usage.db"
    assert (
        layout.session_store_path("chat-1")
        == data_root / "sessions" / "chat-1" / "session_state.db"
    )
    assert (
        layout.session_store_path("chat-1", backend="json")
        == data_root / "sessions" / "chat-1" / "session_state.json"
    )


def test_workspace_layout_when_session_id_has_special_chars_then_slug_round_trips(tmp_path):
    layout = WorkspaceLayout(tmp_path)
    session_id = "group/user 1"

    slug = layout.session_slug(session_id)

    assert slug == "group%2Fuser%201"
    assert layout.session_id_from_slug(slug) == session_id
    assert layout.session_slug("") == "default"
    assert layout.session_id_from_slug("") == "default"


def test_workspace_layout_when_ensuring_directories_then_creates_runtime_and_config_dirs(tmp_path):
    config_root = tmp_path / "config-root"
    data_root = tmp_path / "data-root"
    layout = WorkspaceLayout(data_root, config_path=config_root)

    layout.ensure_directories(session_id="chat A/B")

    assert layout.config_dir().is_dir()
    assert layout.providers_dir().is_dir()
    assert layout.generated_agent_trace_dir().is_dir()
    assert layout.user_memory_dir().is_dir()
    assert layout.event_memory_dir().is_dir()
    assert layout.token_dir().is_dir()
    assert layout.session_dir("chat A/B").is_dir()


def test_workspace_layout_when_skills_exist_then_watch_paths_include_skill_files_and_readme(
    tmp_path,
):
    layout = WorkspaceLayout(tmp_path)
    layout.ensure_directories()
    (layout.skills_dir() / "b.py").write_text("", encoding="utf-8")
    (layout.skills_dir() / "a.py").write_text("", encoding="utf-8")

    paths = layout.config_watch_paths()

    assert paths[:4] == [
        layout.workspace_manifest_path(),
        layout.session_config_path(),
        layout.provider_registry_path(),
        layout.generated_agents_path(),
    ]
    assert paths[-3:] == [
        layout.skills_dir() / "a.py",
        layout.skills_dir() / "b.py",
        layout.skills_dir() / "README.md",
    ]
