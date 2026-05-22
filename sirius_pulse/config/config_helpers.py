"""Configuration helper functions for ConfigManager."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sirius_pulse.config.helpers import build_orchestration_policy_from_dict
from sirius_pulse.config.jsonc import (
    build_default_orchestration_payload,
    load_json_document,
    write_session_config_jsonc,
)
from sirius_pulse.config.models import (
    Agent,
    AgentPreset,
    ProviderPolicy,
    SessionConfig,
    SessionDefaults,
    WorkspaceConfig,
)
from sirius_pulse.utils.layout import WorkspaceLayout


_ENV_VAR_PATTERN = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


def _coerce_int(value: object, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def _coerce_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return bool(value)


def _coerce_string(value: object, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _coerce_path(value: object, default: Path) -> Path:
    if value is None:
        return default
    text = str(value).strip()
    return Path(text) if text else default


def _sanitize_nullable_list(value: object) -> list[Any]:
    if not isinstance(value, list):
        return []

    sanitized: list[Any] = []
    for item in value:
        if item is None:
            continue
        if isinstance(item, dict):
            sanitized.append(_sanitize_nullable_mapping(item))
            continue
        if isinstance(item, list):
            sanitized.append(_sanitize_nullable_list(item))
            continue
        sanitized.append(item)
    return sanitized


def _sanitize_nullable_mapping(
    value: object,
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sanitized = dict(fallback or {})
    if not isinstance(value, dict):
        return sanitized

    for key, item in value.items():
        key_str = str(key)
        if item is None:
            continue
        existing = sanitized.get(key_str)
        if isinstance(item, dict):
            sanitized[key_str] = _sanitize_nullable_mapping(
                item,
                fallback=existing if isinstance(existing, dict) else None,
            )
            continue
        if isinstance(item, list):
            sanitized[key_str] = _sanitize_nullable_list(item)
            continue
        sanitized[key_str] = item
    return sanitized


def _normalize_orchestration_defaults(
    value: object,
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sanitized = _sanitize_nullable_mapping(value, fallback=fallback)
    sanitized.pop("task_budgets", None)
    sanitized.pop("split_marker", None)
    sanitized.pop("skill_call_marker", None)
    sanitized.pop("consolidation_enabled", None)
    policy = build_orchestration_policy_from_dict(sanitized, agent_model="")
    if policy is None:
        return {}
    normalized = asdict(policy)
    return {
        key: normalized[key]
        for key in sanitized
        if key in normalized
    }


def _build_session_defaults(payload: object, fallback: SessionDefaults) -> SessionDefaults:
    if not isinstance(payload, dict):
        return SessionDefaults(
            history_max_messages=fallback.history_max_messages,
            history_max_chars=fallback.history_max_chars,
            max_recent_participant_messages=fallback.max_recent_participant_messages,
            enable_auto_compression=fallback.enable_auto_compression,
        )

    return SessionDefaults(
        history_max_messages=_coerce_int(
            payload.get("history_max_messages"),
            fallback.history_max_messages,
        ),
        history_max_chars=_coerce_int(
            payload.get("history_max_chars"),
            fallback.history_max_chars,
        ),
        max_recent_participant_messages=_coerce_int(
            payload.get("max_recent_participant_messages"),
            fallback.max_recent_participant_messages,
        ),
        enable_auto_compression=_coerce_bool(
            payload.get("enable_auto_compression"),
            fallback.enable_auto_compression,
        ),
    )


def _build_workspace_config_from_payload(
    payload: dict[str, Any],
    *,
    layout: WorkspaceLayout,
    fallback: WorkspaceConfig,
) -> WorkspaceConfig:
    session_defaults = _build_session_defaults(
        payload.get("session_defaults"),
        fallback.session_defaults,
    )
    provider_policy_payload = payload.get("provider_policy")
    provider_policy_default = fallback.provider_policy.prefer_workspace_registry

    return WorkspaceConfig(
        work_path=_coerce_path(payload.get("work_path"), layout.config_root),
        data_path=_coerce_path(payload.get("data_path"), layout.data_root),
        layout_version=_coerce_int(payload.get("layout_version"), layout.layout_version),
        bootstrap_signature=_coerce_string(
            payload.get("bootstrap_signature"),
            fallback.bootstrap_signature,
        ),
        active_agent_key=_coerce_string(
            payload.get("active_agent_key"),
            fallback.active_agent_key,
        ),
        session_defaults=session_defaults,
        orchestration_defaults=_normalize_orchestration_defaults(
            payload.get("orchestration_defaults"),
            fallback=dict(fallback.orchestration_defaults),
        ),
        provider_policy=ProviderPolicy(
            prefer_workspace_registry=_coerce_bool(
                provider_policy_payload.get("prefer_workspace_registry")
                if isinstance(provider_policy_payload, dict)
                else None,
                provider_policy_default,
            )
        ),
    )


def _normalize_workspace_config(
    config: WorkspaceConfig,
    *,
    layout: WorkspaceLayout,
    fallback: WorkspaceConfig,
) -> WorkspaceConfig:
    session_defaults_payload = {
        "history_max_messages": getattr(config.session_defaults, "history_max_messages", None),
        "history_max_chars": getattr(config.session_defaults, "history_max_chars", None),
        "max_recent_participant_messages": getattr(
            config.session_defaults,
            "max_recent_participant_messages",
            None,
        ),
        "enable_auto_compression": getattr(
            config.session_defaults,
            "enable_auto_compression",
            None,
        ),
    }
    provider_policy_payload = {
        "prefer_workspace_registry": getattr(
            config.provider_policy,
            "prefer_workspace_registry",
            None,
        )
    }

    return WorkspaceConfig(
        work_path=layout.config_root,
        data_path=layout.data_root,
        layout_version=layout.layout_version,
        bootstrap_signature=_coerce_string(
            getattr(config, "bootstrap_signature", None),
            fallback.bootstrap_signature,
        ),
        active_agent_key=_coerce_string(
            getattr(config, "active_agent_key", None),
            fallback.active_agent_key,
        ),
        session_defaults=_build_session_defaults(
            session_defaults_payload,
            fallback.session_defaults,
        ),
        orchestration_defaults=_normalize_orchestration_defaults(
            getattr(config, "orchestration_defaults", None),
            fallback=dict(fallback.orchestration_defaults),
        ),
        provider_policy=ProviderPolicy(
            prefer_workspace_registry=_coerce_bool(
                provider_policy_payload.get("prefer_workspace_registry"),
                fallback.provider_policy.prefer_workspace_registry,
            )
        ),
    )


def _resolve_values(obj: Any) -> Any:
    """Recursively resolve environment variables in configuration."""
    if isinstance(obj, str):
        return _resolve_env_vars(obj)
    elif isinstance(obj, dict):
        return {k: _resolve_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_values(item) for item in obj]
    return obj


def _resolve_env_vars(text: str) -> str:
    """Resolve environment variables in a string."""
    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))

    return _ENV_VAR_PATTERN.sub(replacer, text)


def _load_workspace_session_snapshot(layout: WorkspaceLayout) -> dict[str, Any]:
    session_config_path = layout.session_config_path()
    if session_config_path.exists():
        payload = load_json_document(session_config_path)
        if isinstance(payload, dict):
            return payload
    return {}


def _resolve_active_agent_key(layout: WorkspaceLayout) -> str:
    path = layout.generated_agents_path()
    if path.exists():
        payload = load_json_document(path)
        if isinstance(payload, dict):
            selected = str(payload.get("selected_generated_agent", "")).strip()
            if selected:
                return selected
    return ""


def _validate_config(config: dict[str, Any]) -> None:
    """Validate configuration structure."""
    required_keys = {"work_path", "agent", "orchestration"}
    missing = required_keys - set(config.keys())
    if missing:
        raise ValueError(f"缺少必要配置键：{missing}")

    agent_config = config.get("agent", {})
    agent_required = {"name", "persona", "model"}
    agent_missing = agent_required - set(agent_config.keys())
    if agent_missing:
        raise ValueError(f"缺少必要的主角配置键：{agent_missing}")

    try:
        Path(config["work_path"])
    except (TypeError, ValueError) as e:
        raise ValueError(f"无效的 work_path：{e}")
    if "data_path" in config:
        try:
            Path(config["data_path"])
        except (TypeError, ValueError) as e:
            raise ValueError(f"无效的 data_path：{e}")


def _dict_to_session_config(config_dict: dict[str, Any], base_dir: Path) -> SessionConfig:
    """Convert configuration dictionary to SessionConfig."""
    work_path = Path(config_dict["work_path"])
    if not work_path.is_absolute():
        work_path = base_dir / work_path

    data_path_raw = config_dict.get("data_path", config_dict["work_path"])
    data_path = Path(data_path_raw)
    if not data_path.is_absolute():
        data_path = base_dir / data_path

    agent_dict = config_dict.get("agent", {})
    agent = Agent(
        name=agent_dict.get("name", ""),
        persona=agent_dict.get("persona", ""),
        model=agent_dict.get("model", ""),
        temperature=float(agent_dict.get("temperature", 0.7)),
        max_tokens=int(agent_dict.get("max_tokens", 512)),
        metadata=agent_dict.get("metadata", {}),
    )

    preset = AgentPreset(
        agent=agent,
        global_system_prompt=config_dict.get("global_system_prompt", ""),
    )

    orchestration = build_orchestration_policy_from_dict(
        config_dict.get("orchestration", {}),
        agent_model=agent.model,
    )

    return SessionConfig(
        work_path=work_path,
        data_path=data_path,
        preset=preset,
        history_max_messages=int(config_dict.get("history_max_messages", 24)),
        history_max_chars=int(config_dict.get("history_max_chars", 6000)),
        max_recent_participant_messages=int(
            config_dict.get("max_recent_participant_messages", 5)
        ),
        enable_auto_compression=config_dict.get("enable_auto_compression", True),
        orchestration=orchestration,
    )
