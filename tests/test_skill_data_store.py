"""技能持久化数据在用户偏好场景中的业务行为测试。"""

from __future__ import annotations

from pathlib import Path

from sirius_pulse.skills.data_store import SkillDataStore


def test_skill_store_when_skill_saves_user_preference_then_next_instance_reads_it(
    tmp_path: Path,
):
    store_path = tmp_path / "skill_data" / "weather.json"
    store = SkillDataStore(store_path)

    store.set("default_city", "杭州")
    store.set("units", "metric")
    store.save()

    reloaded = SkillDataStore(store_path)
    assert reloaded.get("default_city") == "杭州"
    assert reloaded.get("units") == "metric"


def test_skill_store_when_key_is_missing_then_default_value_is_returned(tmp_path: Path):
    store = SkillDataStore(tmp_path / "skill_data" / "prefs.json")

    assert store.get("missing") is None
    assert store.get("missing", "fallback") == "fallback"


def test_skill_store_when_user_clears_preference_then_key_disappears_after_save(
    tmp_path: Path,
):
    store_path = tmp_path / "skill_data" / "prefs.json"
    store = SkillDataStore(store_path)
    store.set("timezone", "Asia/Shanghai")
    store.save()

    assert store.delete("timezone") is True
    store.save()

    assert SkillDataStore(store_path).get("timezone") is None
    assert store.delete("timezone") is False


def test_skill_store_when_webui_lists_settings_then_all_keys_are_returned(tmp_path: Path):
    store = SkillDataStore(tmp_path / "skill_data" / "prefs.json")
    store.set("a", 1)
    store.set("b", 2)

    assert set(store.keys()) == {"a", "b"}
    assert store.all() == {"a": 1, "b": 2}


def test_skill_store_when_data_changes_then_dirty_flag_tracks_unsaved_state(tmp_path: Path):
    store = SkillDataStore(tmp_path / "skill_data" / "prefs.json")

    assert store.is_dirty is False
    store.set("enabled", True)
    assert store.is_dirty is True
    store.save()
    assert store.is_dirty is False


def test_skill_store_when_existing_file_is_corrupted_then_skill_starts_with_empty_store(
    tmp_path: Path,
):
    store_path = tmp_path / "skill_data" / "prefs.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text("{broken json", encoding="utf-8")

    store = SkillDataStore(store_path)

    assert store.all() == {}
