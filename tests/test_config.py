"""人格体验配置的业务行为测试。"""

from __future__ import annotations

from pathlib import Path

from sirius_pulse.persona_config import PersonaExperienceConfig


def test_experience_config_when_new_persona_starts_then_uses_safe_defaults(tmp_path: Path):
    config = PersonaExperienceConfig.load(tmp_path / "experience.json")

    assert config.engagement_sensitivity == 0.5
    assert config.expressiveness == 0.5
    assert config.max_tool_rounds == 3
    assert config.max_sentence_chars == 20
    assert config.memory_unit_top_k == 5


def test_experience_config_when_admin_saves_changes_then_next_startup_reads_them(
    tmp_path: Path,
):
    config_path = tmp_path / "persona_a" / "experience.json"
    config = PersonaExperienceConfig.load(config_path)
    config.engagement_sensitivity = 0.9
    config.expressiveness = 0.7
    config.max_sentence_chars = 35
    config.memory_unit_top_k = 8
    config.other_ai_names = ["HelperBot"]

    config.save(config_path)
    reloaded = PersonaExperienceConfig.load(config_path)

    assert reloaded.engagement_sensitivity == 0.9
    assert reloaded.expressiveness == 0.7
    assert reloaded.max_sentence_chars == 35
    assert reloaded.memory_unit_top_k == 8
    assert reloaded.other_ai_names == ["HelperBot"]


def test_experience_config_group_reply_strategies_round_trip_and_ignore_invalid_values(
    tmp_path: Path,
):
    config_path = tmp_path / "persona_a" / "experience.json"
    config = PersonaExperienceConfig.from_dict(
        {
            "group_reply_strategies": {
                "100": "keyword",
                "200": "smart",
                "300": "unsupported",
                "": "keyword",
            }
        }
    )

    config.save(config_path)
    reloaded = PersonaExperienceConfig.load(config_path)

    assert reloaded.group_reply_strategies == {"100": "keyword", "200": "smart"}


def test_experience_config_when_webui_loads_form_then_all_user_options_are_serialized(
    tmp_path: Path,
):
    config = PersonaExperienceConfig.load(tmp_path / "experience.json")

    payload = config.to_dict()

    assert "engagement_sensitivity" in payload
    assert "enable_tools" in payload
    assert payload["max_sentence_chars"] == 20
    assert payload["memory_unit_top_k"] == 5
    assert payload["memory_unit_token_budget"] == 20_000
    assert "reply_time_curve_enabled" not in payload
    assert "reply_time_curve_points" in payload
    assert payload["group_reply_strategies"] == {}
    assert "memory_depth" not in payload


def test_experience_config_when_webui_posts_partial_payload_then_missing_values_keep_defaults():
    config = PersonaExperienceConfig.from_dict(
        {
            "engagement_sensitivity": 0.2,
            "enable_tools": False,
        }
    )

    assert config.engagement_sensitivity == 0.2
    assert config.enable_tools is False
    assert config.expressiveness == 0.5
    assert config.max_sentence_chars == 20
    assert config.memory_unit_top_k == 5
    assert config.memory_unit_token_budget == 20_000


def test_experience_config_memory_unit_top_k_defaults_to_diary_top_k_for_old_payload():
    """老 experience.json 里的 diary_* 键仍被读取：diary 子系统已删除，但磁盘上的
    数据不该让用户设置静默重置为默认值。"""
    config = PersonaExperienceConfig.from_dict({"diary_top_k": 7, "diary_token_budget": 900})

    assert config.memory_unit_top_k == 7
    assert config.memory_unit_token_budget == 900


def test_experience_config_when_sentence_limit_is_out_of_range_then_clamps():
    assert PersonaExperienceConfig.from_dict({"max_sentence_chars": 2}).max_sentence_chars == 5
    assert PersonaExperienceConfig.from_dict({"max_sentence_chars": 99}).max_sentence_chars == 50


def test_experience_config_when_time_curve_is_loaded_then_points_are_normalized():
    config = PersonaExperienceConfig.from_dict(
        {
            "reply_time_curve_points": [
                {"time": "08:30", "coefficient": 1.5},
                {"time": "24:00", "coefficient": 3.0},
                {"time": "bad", "coefficient": 0.4},
                {"time": "02:00", "coefficient": -1.0},
            ],
        }
    )

    assert config.reply_time_curve_points == [
        {"time": "02:00", "coefficient": 0.0},
        {"time": "08:30", "coefficient": 1.5},
        {"time": "24:00", "coefficient": 2.0},
    ]


def test_experience_config_when_file_is_corrupted_then_runtime_falls_back_to_defaults(
    tmp_path: Path,
):
    config_path = tmp_path / "experience.json"
    config_path.write_text("{broken json", encoding="utf-8")

    config = PersonaExperienceConfig.load(config_path)

    assert config.engagement_sensitivity == 0.5


def test_experience_config_when_one_field_is_garbage_then_other_fields_survive():
    """单个坏字段不能让整份配置回退。

    配置文件是手改的：把 engagement_sensitivity 写成 "high" 时，早先的实现会让
    float() 抛异常、load() 吞掉异常返回默认值——用户同时改好的 max_tool_rounds
    一并静默丢失。这里锁住「坏字段降级，好字段保留」。
    """
    config = PersonaExperienceConfig.from_dict(
        {
            "engagement_sensitivity": "high",
            "max_tool_rounds": 25,
            "max_sentence_chars": 33,
            "memory_unit_top_k": 9,
        }
    )

    assert config.engagement_sensitivity == 0.5  # 坏值 → 默认
    assert config.max_tool_rounds == 25  # 好值保留
    assert config.max_sentence_chars == 33
    assert config.memory_unit_top_k == 9


def test_experience_config_when_list_field_is_a_string_then_it_is_not_shredded():
    """字符串名单不能被拆成单字符。

    ``other_ai_names="小星"`` 经 ``list()`` 会变成 ``["小","星"]``——看起来有配置，
    实际是两个单字名字，会污染抢话判定。宁可为空。
    """
    config = PersonaExperienceConfig.from_dict(
        {"other_ai_names": "小星", "message_prefixes": "!", "max_tool_rounds": 4}
    )

    assert config.other_ai_names == []
    assert config.message_prefixes == []
    assert config.max_tool_rounds == 4


def test_experience_config_when_numeric_fields_are_negative_then_they_are_clamped():
    config = PersonaExperienceConfig.from_dict(
        {"max_tool_rounds": -5, "memory_unit_top_k": -1, "memory_unit_token_budget": -100}
    )

    assert config.max_tool_rounds == 0
    assert config.memory_unit_top_k == 0
    assert config.memory_unit_token_budget == 0


def test_experience_config_when_file_is_a_json_list_then_defaults_are_used(tmp_path: Path):
    """JSON 合法但不是对象时也要走默认值，而不是抛 TypeError。"""
    config_path = tmp_path / "experience.json"
    config_path.write_text("[1, 2, 3]", encoding="utf-8")

    config = PersonaExperienceConfig.load(config_path)

    assert config.engagement_sensitivity == 0.5
    assert config.max_tool_rounds == 3


def test_adapters_config_when_one_adapter_is_corrupt_then_others_still_load(tmp_path: Path):
    """一个 adapter 坏掉不该让整个适配器列表消失。"""
    from sirius_pulse.persona_config import PersonaAdaptersConfig

    config_path = tmp_path / "adapters.json"
    config_path.write_text(
        '{"adapters": ['
        '{"type": "napcat", "qq_number": "10001", "dispatch_priority": "urgent"},'
        '{"type": "unknown-platform"},'
        '"not-an-object",'
        '{"type": "napcat", "qq_number": "10002"}'
        "]}",
        encoding="utf-8",
    )

    config = PersonaAdaptersConfig.load(config_path)

    assert [a.qq_number for a in config.adapters] == ["10001", "10002"]
    # 坏值降级为默认值，好字段（qq_number）保留。
    assert config.adapters[0].dispatch_priority == 0.0
    assert config.adapters[0].qq_number == "10001"
