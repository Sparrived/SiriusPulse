"""工具持久化数据在用户偏好场景中的业务行为测试。"""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

from sirius_pulse.tools.data_store import ToolDataStore


def test_tool_store_when_tool_saves_user_preference_then_next_instance_reads_it(
    tmp_path: Path,
):
    store_path = tmp_path / "tool_data" / "weather.json"
    store = ToolDataStore(store_path)

    store.set("default_city", "杭州")
    store.set("units", "metric")
    store.save()

    reloaded = ToolDataStore(store_path)
    assert reloaded.get("default_city") == "杭州"
    assert reloaded.get("units") == "metric"


def test_tool_store_when_key_is_missing_then_default_value_is_returned(tmp_path: Path):
    store = ToolDataStore(tmp_path / "tool_data" / "prefs.json")

    assert store.get("missing") is None
    assert store.get("missing", "fallback") == "fallback"


def test_tool_store_when_user_clears_preference_then_key_disappears_after_save(
    tmp_path: Path,
):
    store_path = tmp_path / "tool_data" / "prefs.json"
    store = ToolDataStore(store_path)
    store.set("timezone", "Asia/Shanghai")
    store.save()

    assert store.delete("timezone") is True
    store.save()

    assert ToolDataStore(store_path).get("timezone") is None
    assert store.delete("timezone") is False


def test_tool_store_when_webui_lists_settings_then_all_keys_are_returned(tmp_path: Path):
    store = ToolDataStore(tmp_path / "tool_data" / "prefs.json")
    store.set("a", 1)
    store.set("b", 2)

    assert set(store.keys()) == {"a", "b"}
    assert store.all() == {"a": 1, "b": 2}


def test_tool_store_transaction_holds_lock_across_read_modify_write(tmp_path: Path):
    store = ToolDataStore(tmp_path / "tool_data" / "workflow.json")
    entered = Event()
    release = Event()
    second_started = Event()
    second_finished = Event()

    def first_transaction():
        with store.transaction():
            entered.set()
            release.wait(timeout=1)
            store.set("value", 1)

    def second_write():
        second_started.set()
        store.set("other", 2)
        second_finished.set()

    first = Thread(target=first_transaction)
    second = Thread(target=second_write)
    first.start()
    assert entered.wait(timeout=1)
    second.start()
    assert second_started.wait(timeout=1)
    assert second_finished.wait(timeout=0.05) is False
    release.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert second_finished.is_set() is True
    assert store.get("value") == 1
    assert store.get("other") == 2
    store.set("enabled", True)
    assert store.is_dirty is True
    store.save()
    assert store.is_dirty is False


def test_tool_store_when_existing_file_is_corrupted_then_tool_starts_with_empty_store(
    tmp_path: Path,
):
    store_path = tmp_path / "tool_data" / "prefs.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text("{broken json", encoding="utf-8")

    store = ToolDataStore(store_path)

    assert store.all() == {}
