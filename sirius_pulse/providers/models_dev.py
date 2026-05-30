"""models.dev 社区模型数据库集成。

提供模型元数据查询能力，包括：
  - 模型列表自动获取与本地缓存（内存 + 磁盘）
  - Provider ID 映射（models.dev → Sirius Pulse）
  - 按能力/价格/上下文长度筛选模型
  - 为 ProviderConfig 自动填充模型列表

数据源: https://models.dev/api.json （社区维护，无需认证）
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from sirius_pulse.utils.json_io import atomic_write_json, read_json

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
_CACHE_FILENAME = "models_dev_cache.json"
_DEFAULT_TTL = 3600  # 1 小时


# ──────────────────────────────────────────────────────────────────
# Provider ID 映射: models.dev provider_id → Sirius Pulse provider_type
# ──────────────────────────────────────────────────────────────────

_MODELS_DEV_TO_SIRIUS: dict[str, str] = {
    "openai": "openai-compatible",
    "anthropic": "openai-compatible",
    "alibaba": "aliyun-bailian",
    "alibaba-cn": "aliyun-bailian",
    "deepseek": "deepseek",
    "gemini": "openai-compatible",
    "bigmodel": "bigmodel",
    "zai": "bigmodel",
    "siliconflow": "siliconflow",
    "volcengine-ark": "volcengine-ark",
    "fireworks-ai": "openai-compatible",
    "openrouter": "openai-compatible",
    "nvidia": "openai-compatible",
    "minimax": "openai-compatible",
    "xiaomi": "openai-compatible",
}

_SIRIUS_TO_MODELS_DEV: dict[str, list[str]] = {}
for _md_id, _sp_type in _MODELS_DEV_TO_SIRIUS.items():
    _SIRIUS_TO_MODELS_DEV.setdefault(_sp_type, []).append(_md_id)


# ──────────────────────────────────────────────────────────────────
# 数据类
# ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ModelCost:
    """模型价格信息（USD / 百万 tokens）。"""

    input_per_m: float
    output_per_m: float
    cache_read_per_m: float | None = None
    cache_write_per_m: float | None = None


@dataclass(frozen=True, slots=True)
class ModelFilter:
    """模型筛选条件。"""

    tool_call: bool | None = None
    reasoning: bool | None = None
    vision: bool | None = None
    min_context: int = 0
    max_input_cost: float | None = None
    open_weights_only: bool = False


# ──────────────────────────────────────────────────────────────────
# 缓存管理
# ──────────────────────────────────────────────────────────────────

class ModelsDevCache:
    """models.dev 本地缓存管理器。

    三级缓存策略:
      1. 内存缓存（TTL 内直接返回）
      2. 磁盘缓存（<config_root>/models_dev_cache.json）
      3. 网络拉取
    """

    def __init__(self, config_root: Path, ttl: int = _DEFAULT_TTL) -> None:
        self._cache_path = config_root / _CACHE_FILENAME
        self._ttl = ttl
        self._memory_cache: dict[str, Any] | None = None
        self._cache_time: float = 0.0

    def get(self, *, force_refresh: bool = False) -> dict[str, Any] | None:
        """获取 models.dev 完整数据，自动走缓存逻辑。"""
        now = time.time()

        # 1. 内存缓存（未过期且非强制刷新）
        if self._memory_cache is not None and not force_refresh:
            if now - self._cache_time < self._ttl:
                return self._memory_cache

        # 2. 磁盘缓存
        if not force_refresh:
            disk_data = read_json(self._cache_path)
            if isinstance(disk_data, dict) and disk_data:
                self._memory_cache = disk_data
                self._cache_time = now
                logger.debug("从磁盘缓存加载 models.dev 数据，共 %d 个 provider", len(disk_data))
                return disk_data

        # 3. 网络拉取
        data = self._fetch_from_network()
        if data:
            self._memory_cache = data
            self._cache_time = now
            self._save_to_disk(data)
            logger.info("从 models.dev 拉取最新数据，共 %d 个 provider", len(data))
        elif self._memory_cache is not None:
            # 网络失败但内存有过期缓存，降级使用
            logger.warning("网络拉取失败，使用过期内存缓存")
            return self._memory_cache

        return data

    def _fetch_from_network(self) -> dict[str, Any] | None:
        """通过 HTTP GET 拉取 models.dev 数据。"""
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(MODELS_DEV_URL)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict):
                    return data
                logger.warning("models.dev 返回数据类型异常: %s", type(data).__name__)
                return None
        except Exception as exc:
            logger.warning("拉取 models.dev 失败: %s", exc)
            return None

    def _save_to_disk(self, data: dict[str, Any]) -> None:
        """持久化缓存到磁盘。"""
        try:
            atomic_write_json(self._cache_path, data)
        except Exception as exc:
            logger.debug("写入 models.dev 磁盘缓存失败: %s", exc)


# ──────────────────────────────────────────────────────────────────
# Provider 映射
# ──────────────────────────────────────────────────────────────────

def get_models_dev_provider_ids(sirius_provider_type: str) -> list[str]:
    """返回 Sirius Pulse provider_type 对应的 models.dev provider ID 列表。"""
    return _SIRIUS_TO_MODELS_DEV.get(sirius_provider_type, [])


# ──────────────────────────────────────────────────────────────────
# 查询函数
# ──────────────────────────────────────────────────────────────────

def get_provider_models(data: dict[str, Any], provider_id: str) -> dict[str, dict[str, Any]]:
    """获取指定 models.dev provider 的所有模型。"""
    provider = data.get(provider_id, {})
    if not isinstance(provider, dict):
        return {}
    models = provider.get("models", {})
    return models if isinstance(models, dict) else {}


def get_model_info(
    data: dict[str, Any],
    provider_id: str,
    model_id: str,
) -> dict[str, Any] | None:
    """获取单个模型的完整元数据。"""
    models = get_provider_models(data, provider_id)
    result = models.get(model_id)
    return result if isinstance(result, dict) else None


def get_context_length(model: dict[str, Any], model_id: str = "") -> int:
    """获取模型上下文长度，优先 models.dev 数据。"""
    limit = model.get("limit", {})
    ctx = limit.get("context", 0) if isinstance(limit, dict) else 0
    return ctx if isinstance(ctx, int) and ctx > 0 else 0


def parse_model_cost(model: dict[str, Any], context_tokens: int = 0) -> ModelCost:
    """解析模型价格，自动处理分层定价。"""
    cost = model.get("cost", {})
    if not isinstance(cost, dict) or not cost:
        return ModelCost(input_per_m=0.0, output_per_m=0.0)

    base_input = float(cost.get("input", 0))
    base_output = float(cost.get("output", 0))

    # 检查分层定价（tiers）
    tiers = cost.get("tiers", [])
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            tier_info = tier.get("tier", {})
            tier_size = tier_info.get("size", 0) if isinstance(tier_info, dict) else 0
            if (
                isinstance(tier_info, dict)
                and tier_info.get("type") == "context"
                and isinstance(tier_size, (int, float))
                and context_tokens > tier_size
            ):
                return ModelCost(
                    input_per_m=float(tier.get("input", base_input)),
                    output_per_m=float(tier.get("output", base_output)),
                    cache_read_per_m=_optional_float(tier.get("cache_read")),
                    cache_write_per_m=_optional_float(tier.get("cache_write")),
                )

    return ModelCost(
        input_per_m=base_input,
        output_per_m=base_output,
        cache_read_per_m=_optional_float(cost.get("cache_read")),
        cache_write_per_m=_optional_float(cost.get("cache_write")),
    )


def estimate_cost(
    cost: ModelCost,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """估算单次调用成本（USD）。"""
    return (
        input_tokens * cost.input_per_m / 1_000_000
        + output_tokens * cost.output_per_m / 1_000_000
    )


def filter_models(
    data: dict[str, Any],
    provider_id: str,
    filt: ModelFilter,
) -> list[dict[str, Any]]:
    """按条件筛选模型，返回匹配的模型列表（每项含 id 键）。"""
    models = get_provider_models(data, provider_id)
    results: list[dict[str, Any]] = []

    for mid, m in models.items():
        if not isinstance(m, dict):
            continue

        if filt.tool_call is not None and m.get("tool_call") != filt.tool_call:
            continue
        if filt.reasoning is not None and m.get("reasoning") != filt.reasoning:
            continue

        if filt.vision:
            modalities = m.get("modalities", {})
            input_mods = modalities.get("input", []) if isinstance(modalities, dict) else []
            if "image" not in input_mods:
                continue

        limit = m.get("limit", {})
        ctx = limit.get("context", 0) if isinstance(limit, dict) else 0
        if isinstance(ctx, (int, float)) and ctx < filt.min_context:
            continue

        cost = m.get("cost", {})
        if filt.max_input_cost is not None and isinstance(cost, dict):
            if float(cost.get("input", 0)) > filt.max_input_cost:
                continue

        if filt.open_weights_only and not m.get("open_weights", False):
            continue

        results.append({"id": mid, **m})

    return results


def list_provider_model_ids(
    data: dict[str, Any],
    sirius_provider_type: str,
    *,
    tool_call_only: bool = False,
) -> list[str]:
    """获取 Sirius Pulse provider 对应的所有模型 ID 列表。

    自动处理一个 Sirius provider_type 对应多个 models.dev provider 的情况
    （如 openai-compatible 同时覆盖 openai、anthropic 等）。
    """
    md_ids = get_models_dev_provider_ids(sirius_provider_type)
    if not md_ids:
        return []

    result: list[str] = []
    seen: set[str] = set()
    for md_id in md_ids:
        models = get_provider_models(data, md_id)
        for mid, m in models.items():
            if mid in seen:
                continue
            seen.add(mid)
            if tool_call_only and not m.get("tool_call", False):
                continue
            result.append(mid)

    result.sort()
    return result


def list_provider_model_details(
    data: dict[str, Any],
    sirius_provider_type: str,
) -> list[dict[str, Any]]:
    """获取 Sirius Pulse provider 对应的所有模型详情。

    返回每项包含: id, name, tool_call, reasoning, vision, context, input_cost, output_cost
    """
    md_ids = get_models_dev_provider_ids(sirius_provider_type)
    if not md_ids:
        return []

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for md_id in md_ids:
        models = get_provider_models(data, md_id)
        for mid, m in models.items():
            if mid in seen or not isinstance(m, dict):
                continue
            seen.add(mid)
            modalities = m.get("modalities", {})
            input_mods = modalities.get("input", []) if isinstance(modalities, dict) else []
            limit = m.get("limit", {})
            ctx = limit.get("context", 0) if isinstance(limit, dict) else 0
            cost = m.get("cost", {})
            results.append({
                "id": mid,
                "name": m.get("name", mid),
                "tool_call": bool(m.get("tool_call", False)),
                "reasoning": bool(m.get("reasoning", False)),
                "structured_output": bool(m.get("structured_output", False)),
                "vision": "image" in input_mods,
                "audio": "audio" in input_mods,
                "context": ctx if isinstance(ctx, int) else 0,
                "input_cost": cost.get("input", 0) if isinstance(cost, dict) else 0,
                "output_cost": cost.get("output", 0) if isinstance(cost, dict) else 0,
            })

    results.sort(key=lambda x: x["id"])
    return results


# ──────────────────────────────────────────────────────────────────
# 自动填充: 供 ProviderRegistry 使用
# ──────────────────────────────────────────────────────────────────

def auto_fill_models_from_dev(
    config_root: Path,
    providers: dict[str, Any],
    *,
    tool_call_only: bool = True,
) -> bool:
    """为 models 列表为空的 provider 自动从 models.dev 填充。

    Args:
        config_root: 配置根目录（用于缓存路径）
        providers: {provider_type: ProviderConfig} 字典，原地修改
        tool_call_only: 是否只填充支持 tool_call 的模型

    Returns:
        是否有任何 provider 被填充
    """
    cache = ModelsDevCache(config_root)
    data = cache.get()
    if not data:
        logger.warning("无法获取 models.dev 数据，跳过自动填充")
        return False

    changed = False
    for provider_type, config in providers.items():
        if config.models:
            # 已有模型列表，跳过
            continue

        model_ids = list_provider_model_ids(data, provider_type, tool_call_only=tool_call_only)
        if model_ids:
            config.models = model_ids
            changed = True
            logger.info(
                "自动填充 %s 的模型列表（%d 个模型，tool_call_only=%s）",
                provider_type,
                len(model_ids),
                tool_call_only,
            )

    return changed


def _optional_float(value: Any) -> float | None:
    """尝试将值转为 float，失败返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
