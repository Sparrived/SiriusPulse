"""Shared WebUI model catalog contract."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, TypedDict

from sirius_pulse.providers.models_dev import ModelsDevCache, list_provider_model_details
from sirius_pulse.providers.routing import WorkspaceProviderManager, normalize_provider_type

LOG = logging.getLogger("sirius.webui")


class ModelChoice(TypedDict, total=False):
    label: str
    value: str
    tags: list[str]


class ModelCatalog(TypedDict):
    available_models: list[str]
    model_choices: list[ModelChoice]


def build_model_catalog(data_path: Any) -> ModelCatalog:
    """Build the shared WebUI model selection contract."""
    available_models: list[str] = []
    model_choices: list[ModelChoice] = []
    seen_models: set[str] = set()
    seen_choices: set[str] = set()
    provider_models: dict[str, list[str]] = {}

    try:
        for provider_type, models in _configured_provider_models(Path(data_path)):
            if provider_type not in provider_models:
                provider_models[provider_type] = []
            provider_models[provider_type].extend(models)
            for model in models:
                if model not in seen_models:
                    seen_models.add(model)
                    available_models.append(model)
                composite = format_model_choice_value(provider_type, model)
                if composite in seen_choices:
                    continue
                seen_choices.add(composite)
                model_choices.append({"label": composite, "value": composite})
    except Exception:
        LOG.warning("获取模型列表失败", exc_info=True)

    enrich_model_choices(data_path, model_choices, provider_models)
    return {"available_models": available_models, "model_choices": model_choices}


def format_model_choice_value(provider_type: str, model_id: str) -> str:
    return f"{provider_type}/{model_id}"


def _configured_provider_models(data_path: Path) -> list[tuple[str, list[str]]]:
    path = WorkspaceProviderManager(data_path).path
    if not path.exists():
        return []

    raw = json.loads(path.read_text(encoding="utf-8"))
    providers = raw.get("providers", {}) if isinstance(raw, dict) else {}
    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(providers, dict):
        entries = [(str(name), cfg) for name, cfg in providers.items() if isinstance(cfg, dict)]
    elif isinstance(providers, list):
        entries = [
            (str(cfg.get("name") or cfg.get("type") or cfg.get("platform_type") or idx), cfg)
            for idx, cfg in enumerate(providers)
            if isinstance(cfg, dict)
        ]

    result: list[tuple[str, list[str]]] = []
    for name, cfg in entries:
        if not bool(cfg.get("enabled", True)):
            continue
        if not str(cfg.get("api_key", "")).strip():
            continue
        provider_type = normalize_provider_type(str(cfg.get("type") or cfg.get("platform_type") or name))
        models_raw = cfg.get("models", [])
        if not isinstance(models_raw, list):
            continue
        models = [str(model).strip() for model in models_raw if str(model).strip()]
        if models:
            result.append((provider_type, models))
    return result


def enrich_model_choices(
    data_path: Any,
    model_choices: list[ModelChoice],
    provider_models: dict[str, list[str]] | None = None,
) -> None:
    """Enrich model choices with provider-aware models.dev capability tags."""
    try:
        data = ModelsDevCache(Path(data_path)).get()
        if not data:
            return
        provider_types = (
            list(provider_models) if provider_models is not None else _provider_types(model_choices)
        )
        tag_index = _build_capability_tag_index(data, provider_types)
        for choice in model_choices:
            parsed = parse_model_choice_value(choice["value"])
            if parsed is None:
                continue
            tags = tag_index.get(parsed)
            if tags:
                choice["tags"] = tags
    except Exception:
        LOG.debug("注入模型能力标签失败", exc_info=True)


def parse_model_choice_value(value: str) -> tuple[str, str] | None:
    provider_type, sep, model_id = value.partition("/")
    if not sep or not provider_type or not model_id:
        return None
    return provider_type, model_id


def _provider_types(model_choices: list[ModelChoice]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for choice in model_choices:
        parsed = parse_model_choice_value(choice["value"])
        if parsed is None:
            continue
        provider_type, _ = parsed
        if provider_type not in seen:
            seen.add(provider_type)
            result.append(provider_type)
    return result


def _build_capability_tag_index(
    data: dict[str, Any],
    provider_types: list[str],
) -> dict[tuple[str, str], list[str]]:
    index: dict[tuple[str, str], list[str]] = {}
    for provider_type in provider_types:
        for item in list_provider_model_details(data, provider_type):
            model_id = str(item.get("id", "")).strip()
            if not model_id:
                continue
            tags = _capability_tags(item)
            if tags:
                index[(provider_type, model_id)] = tags
    return index


def _capability_tags(model: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    if model.get("tool_call"):
        tags.append("函数调用")
    if model.get("reasoning"):
        tags.append("推理")
    if model.get("structured_output"):
        tags.append("结构化")
    if model.get("vision"):
        tags.append("视觉")
    if model.get("audio"):
        tags.append("音频")
    return tags
