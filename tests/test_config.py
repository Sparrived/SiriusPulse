"""人格体验配置的业务行为测试。"""

from __future__ import annotations

from pathlib import Path

from sirius_pulse.persona_config import PersonaExperienceConfig


def test_experience_config_when_new_persona_starts_then_uses_safe_defaults(tmp_path: Path):
    config = PersonaExperienceConfig.load(tmp_path / "experience.json")

    assert config.engagement_sensitivity == 0.5
    assert config.expressiveness == 0.5
    assert config.max_skill_rounds == 3
    assert config.max_sentence_chars == 20


def test_experience_config_when_admin_saves_changes_then_next_startup_reads_them(
    tmp_path: Path,
):
    config_path = tmp_path / "persona_a" / "experience.json"
    config = PersonaExperienceConfig.load(config_path)
    config.engagement_sensitivity = 0.9
    config.expressiveness = 0.7
    config.max_sentence_chars = 35
    config.other_ai_names = ["HelperBot"]

    config.save(config_path)
    reloaded = PersonaExperienceConfig.load(config_path)

    assert reloaded.engagement_sensitivity == 0.9
    assert reloaded.expressiveness == 0.7
    assert reloaded.max_sentence_chars == 35
    assert reloaded.other_ai_names == ["HelperBot"]


def test_experience_config_when_webui_loads_form_then_all_user_options_are_serialized(
    tmp_path: Path,
):
    config = PersonaExperienceConfig.load(tmp_path / "experience.json")

    payload = config.to_dict()

    assert "engagement_sensitivity" in payload
    assert "enable_skills" in payload
    assert payload["max_sentence_chars"] == 20
    assert "plan_mode_enabled" in payload
    assert "plan_mode_limit_normal_tools" in payload
    assert "plan_mode_allow_light_chat" in payload
    assert "plan_mode_chat_awareness_enabled" in payload
    assert "plan_mode_presence_enabled" in payload
    assert "diary_token_budget" in payload
    assert "memory_depth" not in payload


def test_experience_config_when_webui_posts_partial_payload_then_missing_values_keep_defaults():
    config = PersonaExperienceConfig.from_dict(
        {
            "engagement_sensitivity": 0.2,
            "enable_skills": False,
        }
    )

    assert config.engagement_sensitivity == 0.2
    assert config.enable_skills is False
    assert config.plan_mode_enabled is False
    assert config.plan_mode_allow_light_chat is True
    assert config.plan_mode_chat_awareness_enabled is False
    assert config.plan_mode_presence_enabled is False
    assert config.expressiveness == 0.5
    assert config.max_sentence_chars == 20
    assert config.diary_top_k == 5


def test_experience_config_when_sentence_limit_is_out_of_range_then_clamps():
    assert PersonaExperienceConfig.from_dict({"max_sentence_chars": 2}).max_sentence_chars == 5
    assert PersonaExperienceConfig.from_dict({"max_sentence_chars": 99}).max_sentence_chars == 50


def test_experience_config_when_file_is_corrupted_then_runtime_falls_back_to_defaults(
    tmp_path: Path,
):
    config_path = tmp_path / "experience.json"
    config_path.write_text("{broken json", encoding="utf-8")

    config = PersonaExperienceConfig.load(config_path)

    assert config.engagement_sensitivity == 0.5
