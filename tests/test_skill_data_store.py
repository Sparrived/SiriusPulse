"""技能数据存储持久化测试。"""
from __future__ import annotations

from pathlib import Path

from sirius_pulse.skills.data_store import SkillDataStore


def test_get_set_delete(tmp_path: Path):
    """基本读写删除。"""
    store = SkillDataStore(tmp_path / "test_skill.json")
    assert store.get("key1") is None
    assert store.get("key1", "default") == "default"

    store.set("key1", "value1")
    assert store.get("key1") == "value1"

    assert store.delete("key1") is True
    assert store.get("key1") is None
    assert store.delete("nonexistent") is False


def test_persistence(tmp_path: Path):
    """数据持久化到文件。"""
    store_path = tmp_path / "test_persist.json"
    store = SkillDataStore(store_path)
    store.set("name", "test")
    store.set("count", 42)
    store.save()

    # 从文件重新加载
    store2 = SkillDataStore(store_path)
    assert store2.get("name") == "test"
    assert store2.get("count") == 42


def test_keys_and_all(tmp_path: Path):
    """获取所有键和值。"""
    store = SkillDataStore(tmp_path / "test_keys.json")
    store.set("a", 1)
    store.set("b", 2)
    store.set("c", 3)

    keys = store.keys()
    assert set(keys) == {"a", "b", "c"}

    all_data = store.all()
    assert all_data == {"a": 1, "b": 2, "c": 3}


def test_dirty_flag(tmp_path: Path):
    """脏数据标记。"""
    store = SkillDataStore(tmp_path / "test_dirty.json")
    assert not store.is_dirty

    store.set("k", "v")
    assert store.is_dirty

    store.save()
    assert not store.is_dirty
