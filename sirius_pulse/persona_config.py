"""人格级配置模型与持久化。

每个人格目录下的独立配置文件：
- persona.json         → PersonaProfile（已有）
- orchestration.json   → 模型编排（已有）
- adapters.json        → 平台连接配置
- experience.json      → 体验参数（参与决策、回复频率、主动行为等）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sirius_pulse.reply_time_curve import normalize_reply_time_curve_points

logger = logging.getLogger(__name__)


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
            allowed_group_ids=[str(v) for v in data.get("allowed_group_ids", [])],
            allowed_private_user_ids=[str(v) for v in data.get("allowed_private_user_ids", [])],
            peer_ai_ids=[str(v) for v in data.get("peer_ai_ids", [])],
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
        adapters: list[AdapterConfig] = []
        for item in raw_adapters:
            if not isinstance(item, dict):
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
            return cls.from_dict(data)
        except Exception as exc:
            logger.warning("加载 adapters 配置失败 %s: %s", p, exc)
            return cls.default()

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)

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

    # 回复频率限制
    min_reply_interval_seconds: float = 0.0
    main_model_reply_cooldown_seconds: float = 0.0
    reply_time_curve_enabled: bool = False
    reply_time_curve_points: list[dict[str, float | str]] = field(default_factory=list)
    max_sentence_chars: int = 20

    # 并发与技能
    enable_skills: bool = True
    max_skill_rounds: int = 3
    auto_install_skill_deps: bool = True
    plan_mode_enabled: bool = False
    plan_mode_limit_normal_tools: bool = False
    plan_mode_allow_light_chat: bool = True
    plan_mode_chat_awareness_enabled: bool = False
    plan_mode_presence_enabled: bool = False
    plan_mode_presence_min_interval_seconds: float = 45.0

    # 日记检索参数
    diary_top_k: int = 5
    diary_token_budget: int = 800

    # 群里其他 AI/Bot 的名字（手动指定，防止抢话和身份混淆）
    other_ai_names: list[str] = field(default_factory=list)

    # 消息前缀过滤——以这些前缀开头的消息不进入引擎
    message_prefixes: list[str] = field(default_factory=list)

    def to_dict(self, *, include_updated_at: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "engagement_sensitivity": self.engagement_sensitivity,
            "expressiveness": self.expressiveness,
            "min_reply_interval_seconds": self.min_reply_interval_seconds,
            "main_model_reply_cooldown_seconds": self.main_model_reply_cooldown_seconds,
            "reply_time_curve_enabled": self.reply_time_curve_enabled,
            "reply_time_curve_points": normalize_reply_time_curve_points(
                self.reply_time_curve_points
            ),
            "max_sentence_chars": self.max_sentence_chars,
            "enable_skills": self.enable_skills,
            "max_skill_rounds": self.max_skill_rounds,
            "auto_install_skill_deps": self.auto_install_skill_deps,
            "plan_mode_enabled": self.plan_mode_enabled,
            "plan_mode_limit_normal_tools": self.plan_mode_limit_normal_tools,
            "plan_mode_allow_light_chat": self.plan_mode_allow_light_chat,
            "plan_mode_chat_awareness_enabled": self.plan_mode_chat_awareness_enabled,
            "plan_mode_presence_enabled": self.plan_mode_presence_enabled,
            "plan_mode_presence_min_interval_seconds": (
                self.plan_mode_presence_min_interval_seconds
            ),
            "diary_top_k": self.diary_top_k,
            "diary_token_budget": self.diary_token_budget,
            "other_ai_names": list(self.other_ai_names),
            "message_prefixes": list(self.message_prefixes),
        }
        if include_updated_at:
            from datetime import datetime, timezone

            d["_updated_at"] = datetime.now(timezone.utc).isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonaExperienceConfig":
        return cls(
            engagement_sensitivity=float(data.get("engagement_sensitivity", 0.5)),
            expressiveness=float(data.get("expressiveness", 0.5)),
            min_reply_interval_seconds=float(data.get("min_reply_interval_seconds", 0.0)),
            main_model_reply_cooldown_seconds=float(
                data.get("main_model_reply_cooldown_seconds", 0.0)
            ),
            reply_time_curve_enabled=bool(data.get("reply_time_curve_enabled", False)),
            reply_time_curve_points=normalize_reply_time_curve_points(
                data.get("reply_time_curve_points", [])
            ),
            max_sentence_chars=max(5, min(50, int(data.get("max_sentence_chars", 20)))),
            enable_skills=bool(data.get("enable_skills", True)),
            other_ai_names=[str(v) for v in data.get("other_ai_names", [])],
            max_skill_rounds=int(data.get("max_skill_rounds", 3)),
            auto_install_skill_deps=bool(data.get("auto_install_skill_deps", True)),
            plan_mode_enabled=bool(data.get("plan_mode_enabled", False)),
            plan_mode_limit_normal_tools=bool(
                data.get("plan_mode_limit_normal_tools", False)
            ),
            plan_mode_allow_light_chat=bool(data.get("plan_mode_allow_light_chat", True)),
            plan_mode_chat_awareness_enabled=bool(
                data.get("plan_mode_chat_awareness_enabled", False)
            ),
            plan_mode_presence_enabled=bool(data.get("plan_mode_presence_enabled", False)),
            plan_mode_presence_min_interval_seconds=float(
                data.get("plan_mode_presence_min_interval_seconds", 45.0)
            ),
            diary_top_k=int(data.get("diary_top_k", 5)),
            diary_token_budget=int(data.get("diary_token_budget", 800)),
            message_prefixes=[str(v) for v in data.get("message_prefixes", [])],
        )

    @classmethod
    def load(cls, path: Path | str) -> "PersonaExperienceConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except Exception as exc:
            logger.warning("加载 experience 配置失败 %s: %s", p, exc)
            return cls()

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(include_updated_at=True), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)


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
