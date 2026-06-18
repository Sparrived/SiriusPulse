"""小跟班功能的单元测试。

覆盖：
1. SidekickConfig 默认值和序列化
2. PersonaExperienceConfig 向后兼容（缺少 sidekick 字段时默认关闭）
3. ParsedEvent at_user_ids / mention_all 字段
"""

from __future__ import annotations

from pathlib import Path

from sirius_pulse.adapters.models import ParsedEvent
from sirius_pulse.persona_config import (
    PersonaExperienceConfig,
    SidekickConfig,
)


# ---------------------------------------------------------------------------
# SidekickConfig 默认值
# ---------------------------------------------------------------------------


def test_sidekick_config_defaults_are_safe():
    """新建 SidekickConfig 应默认关闭，无宿主配置。"""
    cfg = SidekickConfig()

    assert cfg.enabled is False
    assert cfg.host_qq_ids == []
    assert cfg.host_persona_names == []
    assert cfg.host_aliases == []
    assert cfg.require_at_self is True
    assert cfg.allow_text_alias_trigger is False
    assert cfg.allow_private_from_host is False
    assert cfg.enable_skills is True
    assert cfg.max_skill_rounds is None
    assert cfg.trust_host_as_developer is False
    assert cfg.mention_host_on_report is False
    assert cfg.reply_to_trigger_message is True


def test_sidekick_config_roundtrip():
    """to_dict → from_dict 往返应保留所有字段。"""
    original = SidekickConfig(
        enabled=True,
        host_qq_ids=["123456", "789012"],
        host_persona_names=["akane"],
        host_aliases=["小茜"],
        require_at_self=True,
        allow_text_alias_trigger=False,
        allow_private_from_host=True,
        strip_self_mention_from_task=True,
        report_to_group=True,
        mention_host_on_report=False,
        reply_to_trigger_message=True,
        enable_skills=True,
        max_skill_rounds=5,
        task_timeout_seconds=90.0,
        bypass_engagement_for_trusted_host=True,
        trust_host_as_developer=False,
        allowed_skills=["bing_search", "file_read"],
        denied_skills=["desktop_screenshot"],
    )

    data = original.to_dict()
    restored = SidekickConfig.from_dict(data)

    assert restored.enabled is True
    assert restored.host_qq_ids == ["123456", "789012"]
    assert restored.host_persona_names == ["akane"]
    assert restored.host_aliases == ["小茜"]
    assert restored.allow_private_from_host is True
    assert restored.max_skill_rounds == 5
    assert restored.task_timeout_seconds == 90.0
    assert restored.allowed_skills == ["bing_search", "file_read"]
    assert restored.denied_skills == ["desktop_screenshot"]


def test_sidekick_config_from_none_returns_defaults():
    """from_dict(None) 应返回默认关闭的配置。"""
    cfg = SidekickConfig.from_dict(None)

    assert cfg.enabled is False
    assert cfg.host_qq_ids == []
    assert cfg.max_skill_rounds is None


def test_sidekick_config_from_empty_dict_returns_defaults():
    """from_dict({}) 应返回默认配置。"""
    cfg = SidekickConfig.from_dict({})

    assert cfg.enabled is False
    assert cfg.require_at_self is True
    assert cfg.enable_skills is True


def test_sidekick_config_max_skill_rounds_none_serialization():
    """max_skill_rounds=None 应正确序列化和反序列化。"""
    cfg = SidekickConfig(max_skill_rounds=None)
    data = cfg.to_dict()
    restored = SidekickConfig.from_dict(data)

    assert restored.max_skill_rounds is None


# ---------------------------------------------------------------------------
# PersonaExperienceConfig 向后兼容
# ---------------------------------------------------------------------------


def test_experience_config_missing_sidekick_defaults_to_disabled():
    """旧配置文件缺少 sidekick 字段时，应默认关闭小跟班。"""
    config = PersonaExperienceConfig.from_dict({
        "reply_mode": "auto",
        "engagement_sensitivity": 0.5,
    })

    assert config.sidekick.enabled is False
    assert config.sidekick.host_qq_ids == []


def test_experience_config_sidekick_roundtrip():
    """PersonaExperienceConfig 包含 sidekick 时应正确序列化。"""
    config = PersonaExperienceConfig()
    config.sidekick = SidekickConfig(enabled=True, host_qq_ids=["111"])

    data = config.to_dict()
    assert "sidekick" in data
    assert data["sidekick"]["enabled"] is True
    assert data["sidekick"]["host_qq_ids"] == ["111"]

    restored = PersonaExperienceConfig.from_dict(data)
    assert restored.sidekick.enabled is True
    assert restored.sidekick.host_qq_ids == ["111"]


def test_experience_config_load_save_preserves_sidekick(tmp_path: Path):
    """磁盘读写应保留 sidekick 配置。"""
    config_path = tmp_path / "experience.json"
    config = PersonaExperienceConfig()
    config.sidekick = SidekickConfig(
        enabled=True,
        host_persona_names=["akane"],
        max_skill_rounds=8,
    )
    config.save(config_path)

    loaded = PersonaExperienceConfig.load(config_path)
    assert loaded.sidekick.enabled is True
    assert loaded.sidekick.host_persona_names == ["akane"]
    assert loaded.sidekick.max_skill_rounds == 8


def test_experience_config_to_dict_includes_sidekick():
    """to_dict() 输出应包含 sidekick 键。"""
    config = PersonaExperienceConfig()
    payload = config.to_dict()

    assert "sidekick" in payload
    assert isinstance(payload["sidekick"], dict)
    assert "enabled" in payload["sidekick"]
    assert "host_qq_ids" in payload["sidekick"]
    assert "enable_skills" in payload["sidekick"]


# ---------------------------------------------------------------------------
# ParsedEvent @ 提及元数据
# ---------------------------------------------------------------------------


def test_parsed_event_at_user_ids_default_empty():
    """ParsedEvent 默认 at_user_ids 为空列表。"""
    event = ParsedEvent(group_id="g1", user_id="u1")

    assert event.at_user_ids == []
    assert event.mention_all is False


def test_parsed_event_at_user_ids_preserved():
    """ParsedEvent 应保留 at_user_ids 和 mention_all。"""
    event = ParsedEvent(
        group_id="g1",
        user_id="u1",
        self_id="bot1",
        at_user_ids=["bot1", "u2"],
        mention_all=False,
    )

    assert "bot1" in event.at_user_ids
    assert "u2" in event.at_user_ids
    assert event.mention_all is False


def test_parsed_event_mention_all_flag():
    """ParsedEvent 应支持 mention_all=True。"""
    event = ParsedEvent(
        group_id="g1",
        user_id="u1",
        at_user_ids=[],
        mention_all=True,
    )

    assert event.mention_all is True
    assert event.at_user_ids == []  # @all 不进入具体 ID 列表
