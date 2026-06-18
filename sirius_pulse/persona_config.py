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
class SidekickConfig:
    """小跟班运行配置。"""

    enabled: bool = False

    # 宿主身份。优先使用平台 ID；人格名用于运行时自动解析。
    host_qq_ids: list[str] = field(default_factory=list)
    host_persona_names: list[str] = field(default_factory=list)
    host_aliases: list[str] = field(default_factory=list)

    # 触发策略。
    require_at_self: bool = True
    allow_text_alias_trigger: bool = False
    allow_private_from_host: bool = False
    strip_self_mention_from_task: bool = True

    # 回复策略。
    report_to_group: bool = True
    mention_host_on_report: bool = False
    reply_to_trigger_message: bool = True

    # Agent / SKILL 策略。
    enable_skills: bool = True
    max_skill_rounds: int | None = None
    task_timeout_seconds: float = 120.0
    bypass_engagement_for_trusted_host: bool = True
    trust_host_as_developer: bool = False
    allowed_skills: list[str] = field(default_factory=list)
    denied_skills: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "host_qq_ids": list(self.host_qq_ids),
            "host_persona_names": list(self.host_persona_names),
            "host_aliases": list(self.host_aliases),
            "require_at_self": self.require_at_self,
            "allow_text_alias_trigger": self.allow_text_alias_trigger,
            "allow_private_from_host": self.allow_private_from_host,
            "strip_self_mention_from_task": self.strip_self_mention_from_task,
            "report_to_group": self.report_to_group,
            "mention_host_on_report": self.mention_host_on_report,
            "reply_to_trigger_message": self.reply_to_trigger_message,
            "enable_skills": self.enable_skills,
            "max_skill_rounds": self.max_skill_rounds,
            "task_timeout_seconds": self.task_timeout_seconds,
            "bypass_engagement_for_trusted_host": self.bypass_engagement_for_trusted_host,
            "trust_host_as_developer": self.trust_host_as_developer,
            "allowed_skills": list(self.allowed_skills),
            "denied_skills": list(self.denied_skills),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SidekickConfig":
        if not isinstance(data, dict):
            return cls()
        raw_max_rounds = data.get("max_skill_rounds")
        max_skill_rounds = None if raw_max_rounds in (None, "") else int(raw_max_rounds)
        return cls(
            enabled=bool(data.get("enabled", False)),
            host_qq_ids=[str(v) for v in data.get("host_qq_ids", [])],
            host_persona_names=[str(v) for v in data.get("host_persona_names", [])],
            host_aliases=[str(v) for v in data.get("host_aliases", [])],
            require_at_self=bool(data.get("require_at_self", True)),
            allow_text_alias_trigger=bool(data.get("allow_text_alias_trigger", False)),
            allow_private_from_host=bool(data.get("allow_private_from_host", False)),
            strip_self_mention_from_task=bool(data.get("strip_self_mention_from_task", True)),
            report_to_group=bool(data.get("report_to_group", True)),
            mention_host_on_report=bool(data.get("mention_host_on_report", False)),
            reply_to_trigger_message=bool(data.get("reply_to_trigger_message", True)),
            enable_skills=bool(data.get("enable_skills", True)),
            max_skill_rounds=max_skill_rounds,
            task_timeout_seconds=float(data.get("task_timeout_seconds", 120.0)),
            bypass_engagement_for_trusted_host=bool(
                data.get("bypass_engagement_for_trusted_host", True)
            ),
            trust_host_as_developer=bool(data.get("trust_host_as_developer", False)),
            allowed_skills=[str(v) for v in data.get("allowed_skills", [])],
            denied_skills=[str(v) for v in data.get("denied_skills", [])],
        )


@dataclass(slots=True)
class PersonaExperienceConfig:
    """人格体验参数——控制运行时行为风格。"""

    # 参与决策
    reply_mode: str = "auto"  # auto|always|never
    engagement_sensitivity: float = 0.5  # 0.0~1.0
    expressiveness: float = 0.5  # 0.0~1.0 单旋钮活泼度
    heat_window_seconds: float = 60.0

    # 主动行为
    proactive_enabled: bool = True
    proactive_interval_seconds: float = 300.0
    proactive_active_start_hour: int = 8
    proactive_active_end_hour: int = 23

    # 延迟回复
    delay_reply_enabled: bool = True
    pending_message_threshold: float = 4.0

    # 回复频率限制
    min_reply_interval_seconds: float = 0.0
    reply_frequency_window_seconds: float = 60.0
    reply_frequency_max_replies: int = 8
    reply_frequency_exempt_on_mention: bool = True

    # 并发与技能
    max_concurrent_llm_calls: int = 1
    enable_skills: bool = True
    max_skill_rounds: int = 3
    skill_execution_timeout: float = 30.0
    auto_install_skill_deps: bool = True

    # 小跟班模式
    sidekick: SidekickConfig = field(default_factory=SidekickConfig)

    # 记忆深度（影响 prompt 注入的日记/记忆数量）
    memory_depth: str = "deep"  # shallow|moderate|deep

    # 日记检索参数
    diary_top_k: int = 5
    diary_token_budget: int = 800

    # 群里其他 AI/Bot 的名字（手动指定，防止抢话和身份混淆）
    other_ai_names: list[str] = field(default_factory=list)

    # 消息前缀过滤——以这些前缀开头的消息不进入引擎
    message_prefixes: list[str] = field(default_factory=list)

    # 消息钉住最大携带次数（超过后自动取消钉住）
    pinned_message_max_carry_count: int = 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply_mode": self.reply_mode,
            "engagement_sensitivity": self.engagement_sensitivity,
            "expressiveness": self.expressiveness,
            "heat_window_seconds": self.heat_window_seconds,
            "proactive_enabled": self.proactive_enabled,
            "proactive_interval_seconds": self.proactive_interval_seconds,
            "proactive_active_start_hour": self.proactive_active_start_hour,
            "proactive_active_end_hour": self.proactive_active_end_hour,
            "delay_reply_enabled": self.delay_reply_enabled,
            "pending_message_threshold": self.pending_message_threshold,
            "min_reply_interval_seconds": self.min_reply_interval_seconds,
            "reply_frequency_window_seconds": self.reply_frequency_window_seconds,
            "reply_frequency_max_replies": self.reply_frequency_max_replies,
            "reply_frequency_exempt_on_mention": self.reply_frequency_exempt_on_mention,
            "max_concurrent_llm_calls": self.max_concurrent_llm_calls,
            "enable_skills": self.enable_skills,
            "max_skill_rounds": self.max_skill_rounds,
            "skill_execution_timeout": self.skill_execution_timeout,
            "auto_install_skill_deps": self.auto_install_skill_deps,
            "sidekick": self.sidekick.to_dict(),
            "memory_depth": self.memory_depth,
            "diary_top_k": self.diary_top_k,
            "diary_token_budget": self.diary_token_budget,
            "other_ai_names": list(self.other_ai_names),
            "message_prefixes": list(self.message_prefixes),
            "pinned_message_max_carry_count": self.pinned_message_max_carry_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonaExperienceConfig":
        return cls(
            reply_mode=str(data.get("reply_mode", "auto")),
            engagement_sensitivity=float(data.get("engagement_sensitivity", 0.5)),
            expressiveness=float(data.get("expressiveness", 0.5)),
            heat_window_seconds=float(data.get("heat_window_seconds", 60.0)),
            proactive_enabled=bool(data.get("proactive_enabled", True)),
            proactive_interval_seconds=float(data.get("proactive_interval_seconds", 300.0)),
            proactive_active_start_hour=int(data.get("proactive_active_start_hour", 8)),
            proactive_active_end_hour=int(data.get("proactive_active_end_hour", 23)),
            delay_reply_enabled=bool(data.get("delay_reply_enabled", True)),
            pending_message_threshold=float(data.get("pending_message_threshold", 4.0)),
            min_reply_interval_seconds=float(data.get("min_reply_interval_seconds", 0.0)),
            reply_frequency_window_seconds=float(data.get("reply_frequency_window_seconds", 60.0)),
            reply_frequency_max_replies=int(data.get("reply_frequency_max_replies", 8)),
            reply_frequency_exempt_on_mention=bool(
                data.get("reply_frequency_exempt_on_mention", True)
            ),
            max_concurrent_llm_calls=int(data.get("max_concurrent_llm_calls", 1)),
            enable_skills=bool(data.get("enable_skills", True)),
            other_ai_names=[str(v) for v in data.get("other_ai_names", [])],
            max_skill_rounds=int(data.get("max_skill_rounds", 3)),
            skill_execution_timeout=float(data.get("skill_execution_timeout", 30.0)),
            auto_install_skill_deps=bool(data.get("auto_install_skill_deps", True)),
            sidekick=SidekickConfig.from_dict(data.get("sidekick")),
            memory_depth=str(data.get("memory_depth", "deep")),
            diary_top_k=int(data.get("diary_top_k", 5)),
            diary_token_budget=int(data.get("diary_token_budget", 800)),
            message_prefixes=[str(v) for v in data.get("message_prefixes", [])],
            pinned_message_max_carry_count=int(data.get("pinned_message_max_carry_count", 100)),
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
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
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
    "SidekickConfig",
    "PersonaExperienceConfig",
    "PersonaConfigPaths",
]
