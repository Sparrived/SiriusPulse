"""人格级配置模型与持久化。

每个人格目录下的独立配置文件：
- persona.json         → PersonaProfile（已有）
- orchestration.json   → 模型编排（已有）
- adapters.json        → 平台连接配置
- experience.json      → 体验参数（参与决策、回复频率、主动行为等）
- mcp.json             → MCP server 连接配置
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sirius_pulse.reply_time_curve import normalize_reply_time_curve_points
from sirius_pulse.utils.json_io import replace_with_retry

logger = logging.getLogger(__name__)

_GROUP_REPLY_STRATEGIES = {"smart", "keyword"}


def _as_float(value: Any, default: float, *, field_name: str) -> float:
    """把配置里的值转成 float；转不了就用默认值并记账。

    不能直接 ``float(value)``：配置文件是用户手改的，一个笔误（``"high"``）会抛异常，
    而 ``load()`` 会因此丢掉**整份**配置——用户其余正确的设置一起静默回退到默认值。
    单字段降级 + 一条 warning，坏值可定位，好值留下来。
    """
    if isinstance(value, bool) or value is None:
        logger.warning("experience 配置 %s=%r 不是数字，改用默认值 %r", field_name, value, default)
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("experience 配置 %s=%r 无法解析为数字，改用默认值 %r", field_name, value, default)
        return default


def _as_int(value: Any, default: int, *, field_name: str) -> int:
    """把配置里的值转成 int；语义同 :func:`_as_float`。"""
    if isinstance(value, bool) or value is None:
        logger.warning("experience 配置 %s=%r 不是整数，改用默认值 %r", field_name, value, default)
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("experience 配置 %s=%r 无法解析为整数，改用默认值 %r", field_name, value, default)
        return default


def _as_str_list(value: Any) -> list[str]:
    """把配置里的值转成字符串列表；非列表（含字符串）一律视为空。

    字符串会被 ``list()`` 拆成单字符，得到的名单看起来"有值"却全是碎片，
    比直接当空更糟，所以这里明确拒绝。
    """
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def normalize_group_reply_strategies(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for group_id, strategy in value.items():
        group_key = str(group_id).strip()
        strategy_key = str(strategy).strip().lower()
        if group_key and strategy_key in _GROUP_REPLY_STRATEGIES:
            result[group_key] = strategy_key
    return result


# ---------------------------------------------------------------------------
# Adapter 配置
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class NapCatAdapterConfig:
    """NapCat OneBot v11 连接配置。"""

    type: str = "napcat"
    enabled: bool = True
    ws_url: str = "ws://localhost:3001"
    token: str = "napcat_ws"
    qq_number: str = ""
    allowed_group_ids: list[str] = field(default_factory=list)
    allowed_private_user_ids: list[str] = field(default_factory=list)
    peer_ai_ids: list[str] = field(default_factory=list)
    group_dispatch_enabled: bool = True
    dispatch_db_path: str = ""
    dispatch_priority: float = 0.0
    dispatch_min_reply_interval_seconds: float = 3.0
    dispatch_lease_seconds: float = 120.0
    dispatch_peer_cooldown_seconds: float = 5.0
    dispatch_max_peer_turns: int = 3
    dispatch_score_collection_seconds: float = 0.15
    dispatch_activity_window_seconds: float = 300.0
    dispatch_activity_penalty_per_reply: float = 0.12
    dispatch_max_activity_penalty: float = 0.6
    enable_group_chat: bool = True
    enable_private_chat: bool = True
    root: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "enabled": self.enabled,
            "ws_url": self.ws_url,
            "token": self.token,
            "qq_number": self.qq_number,
            "allowed_group_ids": list(self.allowed_group_ids),
            "allowed_private_user_ids": list(self.allowed_private_user_ids),
            "peer_ai_ids": list(self.peer_ai_ids),
            "group_dispatch_enabled": self.group_dispatch_enabled,
            "dispatch_db_path": self.dispatch_db_path,
            "dispatch_priority": self.dispatch_priority,
            "dispatch_min_reply_interval_seconds": self.dispatch_min_reply_interval_seconds,
            "dispatch_lease_seconds": self.dispatch_lease_seconds,
            "dispatch_peer_cooldown_seconds": self.dispatch_peer_cooldown_seconds,
            "dispatch_max_peer_turns": self.dispatch_max_peer_turns,
            "dispatch_score_collection_seconds": self.dispatch_score_collection_seconds,
            "dispatch_activity_window_seconds": self.dispatch_activity_window_seconds,
            "dispatch_activity_penalty_per_reply": self.dispatch_activity_penalty_per_reply,
            "dispatch_max_activity_penalty": self.dispatch_max_activity_penalty,
            "enable_group_chat": self.enable_group_chat,
            "enable_private_chat": self.enable_private_chat,
            "root": self.root,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NapCatAdapterConfig":
        return cls(
            type=str(data.get("type", "napcat")),
            enabled=bool(data.get("enabled", True)),
            ws_url=str(data.get("ws_url", "ws://localhost:3001")),
            token=str(data.get("token", "napcat_ws")),
            qq_number=str(data.get("qq_number", "")),
            allowed_group_ids=_as_str_list(data.get("allowed_group_ids", [])),
            allowed_private_user_ids=_as_str_list(data.get("allowed_private_user_ids", [])),
            peer_ai_ids=_as_str_list(data.get("peer_ai_ids", [])),
            group_dispatch_enabled=bool(data.get("group_dispatch_enabled", True)),
            dispatch_db_path=str(data.get("dispatch_db_path", "")),
            dispatch_priority=_as_float(
                data.get("dispatch_priority", 0.0), 0.0, field_name="dispatch_priority"
            ),
            dispatch_min_reply_interval_seconds=max(
                0.0,
                _as_float(
                    data.get("dispatch_min_reply_interval_seconds", 3.0),
                    3.0,
                    field_name="dispatch_min_reply_interval_seconds",
                ),
            ),
            dispatch_lease_seconds=max(
                5.0,
                _as_float(
                    data.get("dispatch_lease_seconds", 120.0),
                    120.0,
                    field_name="dispatch_lease_seconds",
                ),
            ),
            dispatch_peer_cooldown_seconds=max(
                0.0,
                _as_float(
                    data.get("dispatch_peer_cooldown_seconds", 5.0),
                    5.0,
                    field_name="dispatch_peer_cooldown_seconds",
                ),
            ),
            dispatch_max_peer_turns=max(
                0,
                _as_int(
                    data.get("dispatch_max_peer_turns", 3),
                    3,
                    field_name="dispatch_max_peer_turns",
                ),
            ),
            dispatch_score_collection_seconds=max(
                0.05,
                _as_float(
                    data.get("dispatch_score_collection_seconds", 0.15),
                    0.15,
                    field_name="dispatch_score_collection_seconds",
                ),
            ),
            dispatch_activity_window_seconds=max(
                30.0,
                _as_float(
                    data.get("dispatch_activity_window_seconds", 300.0),
                    300.0,
                    field_name="dispatch_activity_window_seconds",
                ),
            ),
            dispatch_activity_penalty_per_reply=max(
                0.0,
                _as_float(
                    data.get("dispatch_activity_penalty_per_reply", 0.12),
                    0.12,
                    field_name="dispatch_activity_penalty_per_reply",
                ),
            ),
            dispatch_max_activity_penalty=max(
                0.0,
                _as_float(
                    data.get("dispatch_max_activity_penalty", 0.6),
                    0.6,
                    field_name="dispatch_max_activity_penalty",
                ),
            ),
            enable_group_chat=bool(data.get("enable_group_chat", True)),
            enable_private_chat=bool(data.get("enable_private_chat", True)),
            root=str(data.get("root", "")),
        )


AdapterConfig = NapCatAdapterConfig  # 未来可扩展为 Union


@dataclass(slots=True)
class PersonaAdaptersConfig:
    """人格的平台连接配置。"""

    adapters: list[AdapterConfig] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"adapters": [a.to_dict() for a in self.adapters]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonaAdaptersConfig":
        raw_adapters = data.get("adapters", [])
        if not isinstance(raw_adapters, list):
            logger.warning("adapters 配置的 adapters 字段不是列表，已忽略")
            return cls.default()
        adapters: list[AdapterConfig] = []
        for item in raw_adapters:
            if not isinstance(item, dict):
                logger.warning("跳过非对象的 adapter 条目: %r", item)
                continue
            t = str(item.get("type", "napcat"))
            if t == "napcat":
                adapters.append(NapCatAdapterConfig.from_dict(item))
            else:
                logger.warning("未知 adapter 类型: %s，已跳过", t)
        return cls(adapters=adapters)

    @classmethod
    def load(cls, path: Path | str) -> "PersonaAdaptersConfig":
        p = Path(path)
        if not p.exists():
            return cls.default()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("加载 adapters 配置失败 %s: %s", p, exc)
            return cls.default()
        if not isinstance(data, dict):
            logger.warning("adapters 配置不是对象，忽略 %s（实际为 %s）", p, type(data).__name__)
            return cls.default()
        # 单个 adapter 坏掉不再丢整份配置：from_dict 内部逐项跳过并记账。
        return cls.from_dict(data)

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        replace_with_retry(tmp, p)

    @classmethod
    def default(cls) -> "PersonaAdaptersConfig":
        return cls(adapters=[NapCatAdapterConfig()])


# ---------------------------------------------------------------------------
# Experience 配置
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PersonaExperienceConfig:
    """人格体验参数——控制运行时行为风格。"""

    # 参与决策
    engagement_sensitivity: float = 0.5  # 0.0~1.0
    expressiveness: float = 0.5  # 0.0~1.0 单旋钮活泼度
    # 群级回复策略；未配置的群使用 smart
    group_reply_strategies: dict[str, str] = field(default_factory=dict)

    # 回复频率限制
    min_reply_interval_seconds: float = 0.0
    main_model_reply_cooldown_seconds: float = 0.0
    reply_time_curve_points: list[dict[str, float | str]] = field(default_factory=list)
    max_sentence_chars: int = 20

    # 并发与工具
    enable_tools: bool = True
    max_tool_rounds: int = 3
    auto_install_tool_deps: bool = True

    # 记忆单元检索参数
    memory_unit_top_k: int = 5
    memory_unit_token_budget: int = 20_000

    # 群里其他 AI/Bot 的名字（手动指定，防止抢话和身份混淆）
    other_ai_names: list[str] = field(default_factory=list)

    # 消息前缀过滤——以这些前缀开头的消息不进入引擎
    message_prefixes: list[str] = field(default_factory=list)

    def to_dict(self, *, include_updated_at: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "engagement_sensitivity": self.engagement_sensitivity,
            "expressiveness": self.expressiveness,
            "group_reply_strategies": normalize_group_reply_strategies(self.group_reply_strategies),
            "min_reply_interval_seconds": self.min_reply_interval_seconds,
            "main_model_reply_cooldown_seconds": self.main_model_reply_cooldown_seconds,
            "reply_time_curve_points": normalize_reply_time_curve_points(
                self.reply_time_curve_points
            ),
            "max_sentence_chars": self.max_sentence_chars,
            "enable_tools": self.enable_tools,
            "max_tool_rounds": self.max_tool_rounds,
            "auto_install_tool_deps": self.auto_install_tool_deps,
            "memory_unit_top_k": self.memory_unit_top_k,
            "memory_unit_token_budget": self.memory_unit_token_budget,
            "other_ai_names": list(self.other_ai_names),
            "message_prefixes": list(self.message_prefixes),
        }
        if include_updated_at:
            from datetime import datetime, timezone

            d["_updated_at"] = datetime.now(timezone.utc).isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonaExperienceConfig":
        # 历史字段 diary_top_k / diary_token_budget 仍被读取：diary 子系统已删除，
        # 但老 experience.json 里存的是数据，丢掉会让用户设置静默重置为默认值。
        memory_unit_top_k = data.get("memory_unit_top_k", data.get("diary_top_k", 5))
        memory_unit_token_budget = data.get(
            "memory_unit_token_budget", data.get("diary_token_budget", 20_000)
        )
        return cls(
            engagement_sensitivity=_as_float(
                data.get("engagement_sensitivity", 0.5), 0.5, field_name="engagement_sensitivity"
            ),
            expressiveness=_as_float(
                data.get("expressiveness", 0.5), 0.5, field_name="expressiveness"
            ),
            group_reply_strategies=normalize_group_reply_strategies(
                data.get("group_reply_strategies", {})
            ),
            min_reply_interval_seconds=_as_float(
                data.get("min_reply_interval_seconds", 0.0),
                0.0,
                field_name="min_reply_interval_seconds",
            ),
            main_model_reply_cooldown_seconds=_as_float(
                data.get("main_model_reply_cooldown_seconds", 0.0),
                0.0,
                field_name="main_model_reply_cooldown_seconds",
            ),
            reply_time_curve_points=normalize_reply_time_curve_points(
                data.get("reply_time_curve_points", [])
            ),
            max_sentence_chars=max(
                5,
                min(
                    50,
                    _as_int(
                        data.get("max_sentence_chars", 20), 20, field_name="max_sentence_chars"
                    ),
                ),
            ),
            enable_tools=bool(data.get("enable_tools", True)),
            other_ai_names=_as_str_list(data.get("other_ai_names", [])),
            # 0 是合法值（不允许工具续写），只挡负数。
            max_tool_rounds=max(
                0, _as_int(data.get("max_tool_rounds", 3), 3, field_name="max_tool_rounds")
            ),
            auto_install_tool_deps=bool(data.get("auto_install_tool_deps", True)),
            memory_unit_top_k=max(0, _as_int(memory_unit_top_k, 5, field_name="memory_unit_top_k")),
            memory_unit_token_budget=max(
                0,
                _as_int(memory_unit_token_budget, 20_000, field_name="memory_unit_token_budget"),
            ),
            message_prefixes=_as_str_list(data.get("message_prefixes", [])),
        )

    @classmethod
    def load(cls, path: Path | str) -> "PersonaExperienceConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("加载 experience 配置失败 %s: %s", p, exc)
            return cls()
        if not isinstance(data, dict):
            logger.warning("experience 配置不是对象，忽略 %s（实际为 %s）", p, type(data).__name__)
            return cls()
        # 单个字段坏掉不再丢整份配置：from_dict 内部逐字段降级并记账。
        return cls.from_dict(data)

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(include_updated_at=True), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        replace_with_retry(tmp, p)


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------


class PersonaConfigPaths:
    """人格目录下各配置文件的路径约定。"""

    def __init__(self, persona_dir: Path | str) -> None:
        self.dir = Path(persona_dir).resolve()

    @property
    def persona(self) -> Path:
        return self.dir / "persona.json"

    @property
    def orchestration(self) -> Path:
        return self.dir / "orchestration.json"

    @property
    def adapters(self) -> Path:
        return self.dir / "adapters.json"

    @property
    def experience(self) -> Path:
        return self.dir / "experience.json"

    @property
    def mcp(self) -> Path:
        return self.dir / "mcp.json"

    @property
    def engine_state(self) -> Path:
        return self.dir / "engine_state"

    @property
    def image_cache(self) -> Path:
        return self.dir / "image_cache"


__all__ = [
    "NapCatAdapterConfig",
    "AdapterConfig",
    "PersonaAdaptersConfig",
    "PersonaExperienceConfig",
    "PersonaConfigPaths",
]
