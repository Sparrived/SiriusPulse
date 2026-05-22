"""人格配置加载测试。"""
from __future__ import annotations

from pathlib import Path

from sirius_pulse.persona_config import PersonaExperienceConfig


def test_load_default_experience_config(tmp_path: Path):
    """加载默认体验配置。"""
    config_path = tmp_path / "experience.json"

    config = PersonaExperienceConfig.load(config_path)
    assert config.engagement_sensitivity == 0.5
    assert config.reply_mode == "auto"
    assert config.expressiveness == 0.5


def test_save_and_load_experience_config(tmp_path: Path):
    """保存并重新加载体验配置。"""
    config_path = tmp_path / "experience.json"

    config = PersonaExperienceConfig.load(config_path)
    config.engagement_sensitivity = 0.9
    config.expressiveness = 0.7
    config.save(config_path)

    reloaded = PersonaExperienceConfig.load(config_path)
    assert reloaded.engagement_sensitivity == 0.9
    assert reloaded.expressiveness == 0.7


def test_experience_config_to_dict(tmp_path: Path):
    """配置转字典。"""
    config = PersonaExperienceConfig.load(tmp_path / "experience.json")
    d = config.to_dict()
    assert isinstance(d, dict)
    assert "engagement_sensitivity" in d
    assert "reply_mode" in d
    assert d["engagement_sensitivity"] == 0.5


def test_experience_config_from_dict(tmp_path: Path):
    """从字典加载配置。"""
    data = {
        "engagement_sensitivity": 0.8,
        "reply_mode": "always",
        "expressiveness": 0.6,
    }
    config = PersonaExperienceConfig.from_dict(data)
    assert config.engagement_sensitivity == 0.8
    assert config.reply_mode == "always"
    assert config.expressiveness == 0.6
