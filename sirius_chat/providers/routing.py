from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sirius_chat.providers.aliyun_bailian import AliyunBailianProvider
from sirius_chat.providers.base import AsyncLLMProvider, GenerationRequest, LLMProvider
from sirius_chat.providers.bigmodel import BigModelProvider
from sirius_chat.providers.deepseek import DeepSeekProvider
from sirius_chat.providers.openai_compatible import OpenAICompatibleProvider
from sirius_chat.providers.siliconflow import SiliconFlowProvider
from sirius_chat.providers.volcengine_ark import VolcengineArkProvider
from sirius_chat.providers.ytea import YTeaProvider
from sirius_chat.utils.layout import WorkspaceLayout

PROVIDER_KEYS_FILE = "provider_keys.json"

_OPENAI_PROVIDER_TYPES = {"openai", "openai-compatible"}
_ALIYUN_BAILIAN_PROVIDER_TYPES = {"aliyun-bailian", "bailian", "dashscope"}
_BIGMODEL_PROVIDER_TYPES = {"bigmodel", "zhipu", "zhipuai"}
_DEEPSEEK_PROVIDER_TYPES = {"deepseek"}
_SILICONFLOW_PROVIDER_TYPES = {"siliconflow"}
_VOLCENGINE_ARK_PROVIDER_TYPES = {"volcengine-ark", "ark"}
_YTEA_PROVIDER_TYPES = {"ytea"}

_SUPPORTED_PROVIDER_PLATFORMS: dict[str, dict[str, str]] = {
    "openai-compatible": {
        "default_base_url": "https://api.openai.com",
        "notes": "OpenAI-compatible chat completions endpoint",
    },
    "aliyun-bailian": {
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode",
        "notes": "Aliyun Bailian DashScope OpenAI-compatible endpoint",
    },
    "bigmodel": {
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "notes": "BigModel GLM chat completions endpoint",
    },
    "deepseek": {
        "default_base_url": "https://api.deepseek.com",
        "notes": "DeepSeek chat completions endpoint (OpenAI-compatible format)",
    },
    "siliconflow": {
        "default_base_url": "https://api.siliconflow.cn",
        "notes": "SiliconFlow OpenAI-compatible endpoint",
    },
    "volcengine-ark": {
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "notes": "Volcengine Ark chat completions endpoint",
    },
    "ytea": {
        "default_base_url": "https://api.ytea.top",
        "notes": "YTea OpenAI-compatible endpoint",
    },
}

_PROVIDER_HEALTHCHECK_SYSTEM_PROMPT = "你是可用性检查助手，请简短回复 ok。"
_PROVIDER_HEALTHCHECK_USER_MESSAGE = "ping"

logger = logging.getLogger(__name__)


def get_supported_provider_platforms() -> dict[str, dict[str, str]]:
    return dict(_SUPPORTED_PROVIDER_PLATFORMS)


@dataclass(slots=True)
class ProviderConfig:
    provider_type: str
    api_key: str
    base_url: str
    healthcheck_model: str = ""
    enabled: bool = True
    models: list[str] = field(default_factory=list)


def normalize_provider_type(provider_type: str) -> str:
    normalized = provider_type.strip().lower()
    if normalized == "openai":
        return "openai-compatible"
    if normalized == "ark":
        return "volcengine-ark"
    if normalized in {"bailian", "dashscope"}:
        return "aliyun-bailian"
    if normalized in {"zhipu", "zhipuai"}:
        return "bigmodel"
    return normalized


def ensure_provider_platform_supported(provider_type: str) -> str:
    normalized = normalize_provider_type(provider_type)
    if normalized not in _SUPPORTED_PROVIDER_PLATFORMS:
        raise RuntimeError(f"provider 平台未适配：{provider_type}")
    return normalized


class ProviderRegistry:
    """Store provider credentials and routing hints under work_path."""

    def __init__(self, work_path: Path | WorkspaceLayout) -> None:
        self._layout = work_path if isinstance(work_path, WorkspaceLayout) else WorkspaceLayout(work_path)
        self.path = self._layout.provider_registry_path()

    @property
    def work_path(self) -> Path:
        return self._layout.config_root

    def load(self) -> dict[str, ProviderConfig]:
        if not self.path.exists():
            return {}

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        providers = raw.get("providers", {})
        results: dict[str, ProviderConfig] = {}
        needs_migration = False
        for provider_name, payload in providers.items():
            if not isinstance(payload, dict):
                continue
            provider_type = normalize_provider_type(str(payload.get("type", provider_name)))
            api_key = str(payload.get("api_key", "")).strip()
            if not api_key:
                continue
            
            # 开启自动迁移标记：如果任一 entry 缺失 models 字段
            if "models" not in payload:
                needs_migration = True

            base_url = str(payload.get("base_url", "")).strip()
            healthcheck_model = str(payload.get("healthcheck_model", "")).strip()
            enabled = bool(payload.get("enabled", True))
            models_raw = payload.get("models", [])
            models = [str(m).strip() for m in models_raw if str(m).strip()] if isinstance(models_raw, list) else []
            results[provider_type] = ProviderConfig(
                provider_type=provider_type,
                api_key=api_key,
                base_url=base_url,
                healthcheck_model=healthcheck_model,
                enabled=enabled,
                models=models,
            )
        
        if needs_migration:
            self.save(results)

        return results

    def save(self, providers: dict[str, ProviderConfig]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        providers_payload: dict[str, dict[str, object]] = {}
        for provider_type, config in providers.items():
            entry: dict[str, object] = {
                "type": config.provider_type,
                "api_key": config.api_key,
                "base_url": config.base_url,
                "healthcheck_model": config.healthcheck_model,
                "enabled": config.enabled,
                "models": config.models,
            }
            providers_payload[provider_type] = entry
        payload: dict[str, object] = {"providers": providers_payload}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def upsert(
        self,
        *,
        provider_type: str,
        api_key: str,
        base_url: str = "",
        healthcheck_model: str = "",
        models: list[str] | None = None,
    ) -> None:
        provider_key = normalize_provider_type(provider_type)
        providers = self.load()
        providers[provider_key] = ProviderConfig(
            provider_type=provider_key,
            api_key=api_key.strip(),
            base_url=base_url.strip(),
            healthcheck_model=healthcheck_model.strip(),
            enabled=True,
            models=models or [],
        )
        self.save(providers)

    def remove(self, provider_type: str) -> bool:
        provider_key = normalize_provider_type(provider_type)
        providers = self.load()
        if provider_key not in providers:
            return False
        providers.pop(provider_key)
        self.save(providers)
        return True


class WorkspaceProviderManager:
    """Workspace-scoped provider registry facade."""

    def __init__(self, work_path: Path | WorkspaceLayout) -> None:
        self._layout = work_path if isinstance(work_path, WorkspaceLayout) else WorkspaceLayout(work_path)
        self._registry = ProviderRegistry(self._layout)

    @property
    def path(self) -> Path:
        return self._registry.path

    def load(self) -> dict[str, ProviderConfig]:
        return self._registry.load()

    def save(self, providers: dict[str, ProviderConfig]) -> None:
        self._registry.save(providers)

    def merge_entries(self, providers_config: list[dict[str, object]]) -> dict[str, ProviderConfig]:
        providers = self.load()
        for item in providers_config:
            provider_type = normalize_provider_type(str(item.get("type", "")))
            existing = providers.get(provider_type)
            api_key = str(item.get("api_key", "")).strip()
            if not api_key and existing is not None:
                api_key = existing.api_key
            if not provider_type or not api_key:
                continue

            if "base_url" in item:
                base_url = str(item.get("base_url", "")).strip()
            else:
                base_url = existing.base_url if existing is not None else ""

            if "healthcheck_model" in item:
                healthcheck_model = str(item.get("healthcheck_model", "")).strip()
            else:
                healthcheck_model = existing.healthcheck_model if existing is not None else ""

            if "enabled" in item:
                enabled = bool(item.get("enabled", True))
            else:
                enabled = existing.enabled if existing is not None else True

            if "models" in item:
                models_raw = item.get("models", [])
                models = [
                    str(model).strip()
                    for model in models_raw
                    if str(model).strip()
                ] if isinstance(models_raw, list) else []
            else:
                models = list(existing.models) if existing is not None else []

            providers[provider_type] = ProviderConfig(
                provider_type=provider_type,
                api_key=api_key,
                base_url=base_url,
                healthcheck_model=healthcheck_model,
                enabled=enabled,
                models=models,
            )
        return providers

    def save_from_entries(self, providers_config: list[dict[str, object]]) -> dict[str, ProviderConfig]:
        providers = self.merge_entries(providers_config)
        self.save(providers)
        return providers

    def register(
        self,
        *,
        provider_type: str,
        api_key: str,
        base_url: str = "",
        healthcheck_model: str = "",
        models: list[str] | None = None,
    ) -> None:
        self._registry.upsert(
            provider_type=provider_type,
            api_key=api_key,
            base_url=base_url,
            healthcheck_model=healthcheck_model,
            models=models,
        )

    def remove(self, provider_type: str) -> bool:
        return self._registry.remove(provider_type)

    async def probe(self) -> None:
        await run_provider_detection_flow(providers=self.load())


def merge_provider_sources(
    *,
    work_path: Path,
    providers_config: list[dict[str, object]],
) -> dict[str, ProviderConfig]:
    """Merge providers from multiple sources with priority order.
    
    Priority (high to low):
    1. Session JSON: providers field
    2. Persistent: <work_path>/provider_keys.json
    """
    # 第一步：加载持久化providers（provider_keys.json）
    merged = ProviderRegistry(work_path).load()

    # 第二步：用Session JSON中的providers覆盖持久化配置
    for item in providers_config:
        provider_type = normalize_provider_type(str(item.get("type", "")))
        api_key = str(item.get("api_key", "")).strip()
        if not provider_type or not api_key:
            continue
        base_url = str(item.get("base_url", "")).strip()
        models_raw = item.get("models", None)
        if models_raw is not None and isinstance(models_raw, list):
            models = [str(m).strip() for m in models_raw if str(m).strip()]
        else:
            # session JSON 未显式指定 models，保留持久化配置中的模型列表
            models = merged.get(provider_type, ProviderConfig(provider_type=provider_type, api_key="", base_url="")).models
        merged[provider_type] = ProviderConfig(
            provider_type=provider_type,
            api_key=api_key,
            base_url=base_url,
            healthcheck_model=str(item.get("healthcheck_model", "")).strip(),
            enabled=bool(item.get("enabled", True)),
            models=models,
        )

    return merged


def _create_provider_instance(config: ProviderConfig) -> LLMProvider:
    provider_type = config.provider_type
    if provider_type in _ALIYUN_BAILIAN_PROVIDER_TYPES:
        return AliyunBailianProvider(
            api_key=config.api_key,
            base_url=config.base_url or "https://dashscope.aliyuncs.com/compatible-mode",
        )
    if provider_type in _BIGMODEL_PROVIDER_TYPES:
        return BigModelProvider(
            api_key=config.api_key,
            base_url=config.base_url or "https://open.bigmodel.cn/api/paas/v4",
        )
    if provider_type in _SILICONFLOW_PROVIDER_TYPES:
        return SiliconFlowProvider(api_key=config.api_key)
    if provider_type in _DEEPSEEK_PROVIDER_TYPES:
        return DeepSeekProvider(api_key=config.api_key)
    if provider_type in _VOLCENGINE_ARK_PROVIDER_TYPES:
        return VolcengineArkProvider(api_key=config.api_key)
    if provider_type in _YTEA_PROVIDER_TYPES:
        return YTeaProvider(api_key=config.api_key)
    if provider_type in _OPENAI_PROVIDER_TYPES:
        return OpenAICompatibleProvider(api_key=config.api_key, base_url=config.base_url or "https://api.openai.com")
    raise RuntimeError(f"不支持的提供商类型：{provider_type}")


class AutoRoutingProvider(AsyncLLMProvider):
    """Choose a configured provider automatically on each generation request."""

    def __init__(self, providers: dict[str, ProviderConfig]) -> None:
        self._providers = {key: value for key, value in providers.items() if value.enabled}
        self._last_provider_name = "unknown"

    def _provider_matches_model(self, provider: ProviderConfig, model: str) -> bool:
        model_stripped = model.strip()
        # Check explicit models list first
        if provider.models and model_stripped in provider.models:
            return True
        # Fallback to healthcheck_model exact match
        expected = provider.healthcheck_model.strip()
        return bool(expected) and model_stripped == expected

    def _create_provider(self, config: ProviderConfig) -> LLMProvider:
        return _create_provider_instance(config)

    def _pick_provider(self, model: str) -> tuple[ProviderConfig, str]:
        if not self._providers:
            raise RuntimeError("未配置任何提供商，请先添加至少一个提供商 API Key。")

        for provider in self._providers.values():
            model_stripped = model.strip()
            if provider.models and model_stripped in provider.models:
                return provider, "models"
            expected = provider.healthcheck_model.strip()
            if expected and model_stripped == expected:
                return provider, "healthcheck_model"

        raise RuntimeError(
            f"无法为模型 '{model}' 找到合适的提供商。请确保在 provider_keys.json 或配置中的 'models' 列表中包含了该模型。"
        )

    async def generate_async(self, request: GenerationRequest) -> str:
        selected, matched_by = self._pick_provider(request.model)
        logger.debug(
            "[Provider路由] model=%s | purpose=%s | provider_type=%s | matched_by=%s | base_url=%s | healthcheck_model=%s | models=%s",
            request.model,
            request.purpose,
            selected.provider_type,
            matched_by,
            selected.base_url or "(默认)",
            selected.healthcheck_model or "(未设置)",
            selected.models,
        )
        provider = self._create_provider(selected)
        self._last_provider_name = getattr(provider, "_provider_name", selected.provider_type)
        return await provider.generate_async(request)


async def probe_provider_availability(
    *,
    provider: AsyncLLMProvider,
    model_name: str,
) -> None:
    """Run a minimal generation request to verify provider connectivity and credentials."""

    content = await provider.generate_async(
        GenerationRequest(
            model=model_name,
            system_prompt=_PROVIDER_HEALTHCHECK_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _PROVIDER_HEALTHCHECK_USER_MESSAGE}],
            temperature=0.0,
            max_tokens=8,
            purpose="provider_healthcheck",
        )
    )
    if not content.strip():
            raise RuntimeError("提供商健康检查返回空内容")

def _create_provider_from_config(config: ProviderConfig) -> LLMProvider:
    provider_type = ensure_provider_platform_supported(config.provider_type)
    config = ProviderConfig(
        provider_type=provider_type,
        api_key=config.api_key,
        base_url=config.base_url,
        healthcheck_model=config.healthcheck_model,
        enabled=config.enabled,
        models=config.models,
    )
    return _create_provider_instance(config)


async def run_provider_detection_flow(
    *,
    providers: dict[str, ProviderConfig],
) -> None:
    """Framework-level provider checks.

    1) Ensure provider platform/API config exists.
    2) Ensure platform is supported by current framework.
    3) Ensure provider is available using the registered healthcheck model.
    """

    if not providers:
        raise RuntimeError("未检测到已配置 provider（需包含平台与 API Key）")

    for provider_type, config in providers.items():
        ensure_provider_platform_supported(provider_type)
        if not config.api_key.strip():
            raise RuntimeError(f"provider 缺少 API Key：{provider_type}")
        if not config.healthcheck_model.strip():
            raise RuntimeError(f"provider 缺少 healthcheck_model：{provider_type}")

        provider = _create_provider_from_config(config)
        await probe_provider_availability(provider=provider, model_name=config.healthcheck_model)


async def register_provider_with_validation(
    *,
    work_path: Path,
    provider_type: str,
    api_key: str,
    healthcheck_model: str,
    base_url: str = "",
) -> str:
    """Register provider only after support and availability checks pass."""

    normalized_provider_type = ensure_provider_platform_supported(provider_type)
    model_name = healthcheck_model.strip()
    if not model_name:
        raise RuntimeError("注册 provider 需要提供 healthcheck_model")
    if not api_key.strip():
        raise RuntimeError("注册 provider 需要提供 API Key")

    config = ProviderConfig(
        provider_type=normalized_provider_type,
        api_key=api_key.strip(),
        base_url=base_url.strip(),
        healthcheck_model=model_name,
        enabled=True,
    )
    provider = _create_provider_from_config(config)
    await probe_provider_availability(provider=provider, model_name=model_name)

    ProviderRegistry(work_path).upsert(
        provider_type=normalized_provider_type,
        api_key=config.api_key,
        base_url=config.base_url,
        healthcheck_model=config.healthcheck_model,
    )
    return normalized_provider_type