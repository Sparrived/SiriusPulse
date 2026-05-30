"""Engine state persistence for EmotionalGroupChatEngine.

Provides save/load of runtime state with group isolation:
- working_memory snapshots per group
- assistant_emotion state
- delayed_queue pending items
- group_last_message_at timestamps

Storage layout::

    {work_path}/engine_state/
        ├── assistant_emotion.json
        ├── delayed_queue.json
        ├── group_timestamps.json
        └── groups/
            └── {group_id}.json   # working memory snapshot
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sirius_pulse.utils.json_io import atomic_write_json

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)

# Per-file locks to prevent concurrent writes on Windows (WinError 32)
_write_locks: dict[str, threading.Lock] = {}
_write_locks_lock = threading.Lock()


def _get_file_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _write_locks_lock:
        if key not in _write_locks:
            _write_locks[key] = threading.Lock()
        return _write_locks[key]


class EngineStateStore:
    """Handles serialization and deserialization of engine runtime state."""

    def __init__(self, work_path: Path | str) -> None:
        self._base = Path(work_path) / "engine_state"
        self._base.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save_working_memory(
        self,
        group_id: str,
        entries: list[dict[str, Any]],
    ) -> None:
        """Save working memory snapshot for a group."""
        path = self._base / "groups" / f"{self._safe_name(group_id)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, {"group_id": group_id, "entries": entries})

    def save_assistant_emotion(self, state: dict[str, Any]) -> None:
        """Save assistant emotion state."""
        path = self._base / "assistant_emotion.json"
        _atomic_write(path, state)

    def save_delayed_queue(self, items: list[dict[str, Any]]) -> None:
        """Save delayed queue pending items."""
        path = self._base / "delayed_queue.json"
        _atomic_write(path, {"items": items})

    def save_group_timestamps(self, timestamps: dict[str, str]) -> None:
        """Save last message timestamps per group."""
        path = self._base / "group_timestamps.json"
        _atomic_write(path, timestamps)

    def save_token_usage_records(self, records: list[dict[str, Any]]) -> None:
        """Save token usage records."""
        path = self._base / "token_usage_records.json"
        _atomic_write(path, {"records": records})

    def save_event_memory(self, state: dict[str, Any]) -> None:
        """Save event memory v2 state."""
        path = self._base / "event_memory.json"
        _atomic_write(path, state)

    def save_basic_memory(self, state: dict[str, Any]) -> None:
        """Save basic memory state."""
        path = self._base / "basic_memory.json"
        _atomic_write(path, state)

    def save_diary_state(self, state: dict[str, Any]) -> None:
        """Save diary manager state."""
        path = self._base / "diary_state.json"
        _atomic_write(path, state)

    def save_user_manager(self, state: dict[str, Any]) -> None:
        """Save user manager state."""
        path = self._base / "user_manager.json"
        _atomic_write(path, state)

    def save_pinned_messages(self, state: dict[str, Any]) -> None:
        """Save pinned messages state."""
        path = self._base / "pinned_messages.json"
        _atomic_write(path, state)

    def save_all(
        self,
        *,
        working_memories: dict[str, list[dict[str, Any]]],
        assistant_emotion: dict[str, Any],
        delayed_queue: list[dict[str, Any]],
        group_timestamps: dict[str, str],
        token_usage_records: list[dict[str, Any]] | None = None,
        event_memory: dict[str, Any] | None = None,
        basic_memory: dict[str, Any] | None = None,
        diary_state: dict[str, Any] | None = None,
        pinned_messages: dict[str, Any] | None = None,
    ) -> None:
        """Convenience: save all state in one call."""
        for group_id, entries in working_memories.items():
            self.save_working_memory(group_id, entries)
        self.save_assistant_emotion(assistant_emotion)
        self.save_delayed_queue(delayed_queue)
        self.save_group_timestamps(group_timestamps)
        if token_usage_records is not None:
            self.save_token_usage_records(token_usage_records)
        if event_memory is not None:
            self.save_event_memory(event_memory)
        if basic_memory is not None:
            self.save_basic_memory(basic_memory)
        if diary_state is not None:
            self.save_diary_state(diary_state)
        if pinned_messages is not None:
            self.save_pinned_messages(pinned_messages)
        logger.info(
            "把现在的状态记下来啦，%d 个群的上下文都好好存着呢～",
            len(working_memories),
        )

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load_working_memory(self, group_id: str) -> list[dict[str, Any]]:
        """Load working memory snapshot for a group."""
        path = self._base / "groups" / f"{self._safe_name(group_id)}.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return list(data.get("entries", []))
        except (OSError, json.JSONDecodeError):
            return []

    def load_assistant_emotion(self) -> dict[str, Any] | None:
        """Load assistant emotion state."""
        path = self._base / "assistant_emotion.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def load_delayed_queue(self) -> list[dict[str, Any]]:
        """Load delayed queue pending items."""
        path = self._base / "delayed_queue.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return list(data.get("items", []))
        except (OSError, json.JSONDecodeError):
            return []

    def load_group_timestamps(self) -> dict[str, str]:
        """Load last message timestamps per group."""
        path = self._base / "group_timestamps.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
            return {}
        except (OSError, json.JSONDecodeError):
            return {}

    def load_event_memory(self) -> dict[str, Any] | None:
        """Load event memory v2 state."""
        path = self._base / "event_memory.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def load_basic_memory(self) -> dict[str, Any] | None:
        """Load basic memory state."""
        path = self._base / "basic_memory.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def load_diary_state(self) -> dict[str, Any] | None:
        """Load diary manager state."""
        path = self._base / "diary_state.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def load_user_manager(self) -> dict[str, Any] | None:
        """Load user manager state."""
        path = self._base / "user_manager.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def load_pinned_messages(self) -> dict[str, Any] | None:
        """Load pinned messages state."""
        path = self._base / "pinned_messages.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def load_all(self) -> dict[str, Any]:
        """Convenience: load all state in one call.

        Returns dict with keys:
            working_memories, assistant_emotion, delayed_queue,
            group_timestamps, event_memory, basic_memory, diary_state,
            user_manager
        """
        groups_dir = self._base / "groups"
        group_ids: set[str] = set()
        if groups_dir.exists():
            for p in groups_dir.glob("*.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    gid = data.get("group_id")
                    if gid:
                        group_ids.add(str(gid))
                except (OSError, json.JSONDecodeError):
                    continue

        working_memories = {gid: self.load_working_memory(gid) for gid in group_ids}

        return {
            "working_memories": working_memories,
            "assistant_emotion": self.load_assistant_emotion(),
            "delayed_queue": self.load_delayed_queue(),
            "group_timestamps": self.load_group_timestamps(),
            "event_memory": self.load_event_memory(),
            "basic_memory": self.load_basic_memory(),
            "diary_state": self.load_diary_state(),
            "user_manager": self.load_user_manager(),
            "pinned_messages": self.load_pinned_messages(),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_name(name: str) -> str:
        base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
        base = re.sub(r"_+", "_", base).strip("_")
        return base or "default"


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically using temp file + replace.

    Uses a per-file lock to prevent concurrent writes, and retries on
    Windows PermissionError (file locked by another reader/writer).
    """
    lock = _get_file_lock(path)
    with lock:
        for attempt in range(5):
            try:
                atomic_write_json(path, data)
                return
            except PermissionError:
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    raise
        return


class EnginePersistence:
    """持久化相关方法组件。"""

    def __init__(self, engine: _EmotionalGroupChatEngineBase) -> None:
        self._engine = engine

    def persist_group_state(self, group_id: str) -> None:
        """Persist basic memory and timestamps for a single group in real-time."""
        engine = self._engine
        entries = engine.basic_memory.get_all(group_id)[-100:]
        engine._state_store.save_working_memory(
            group_id,
            [
                {
                    "user_id": e.user_id,
                    "role": e.role,
                    "content": e.content,
                    "timestamp": e.timestamp,
                }
                for e in entries
            ],
        )
        engine._state_store.save_group_timestamps(dict(engine._group_last_message_at))

    def persist_full_state(self) -> None:
        """Persist all runtime state to disk (used on graceful shutdown)."""
        engine = self._engine
        working_memories: dict[str, list[dict[str, Any]]] = {}
        for group_id in engine.basic_memory.list_groups():
            entries = engine.basic_memory.get_all(group_id)[-100:]
            working_memories[group_id] = [
                {
                    "user_id": e.user_id,
                    "role": e.role,
                    "content": e.content,
                    "timestamp": e.timestamp,
                }
                for e in entries
            ]

        import dataclasses

        engine._state_store.save_all(
            working_memories=working_memories,
            assistant_emotion=dataclasses.asdict(engine.assistant_emotion),
            delayed_queue=[],
            group_timestamps=dict(engine._group_last_message_at),
            token_usage_records=[r.to_dict() for r in engine.token_usage_records],
            basic_memory=engine.basic_memory.to_dict(),
            diary_state={
                "diarized_sources": {
                    gid: list(sids) for gid, sids in engine.diary_manager._diarized_sources.items()
                }
            },
            pinned_messages=engine._pinned_manager.to_dict(),
        )

        # Save proactive state
        self.save_proactive_state()

        # Save persona
        from sirius_pulse.core.persona_store import PersonaStore

        PersonaStore.save(engine.work_path, engine.persona)

    def save_state(self) -> None:
        """Persist all runtime state to disk."""
        self.persist_full_state()

    def load_state(self) -> None:
        """Restore runtime state from disk."""
        engine = self._engine
        try:
            state = engine._state_store.load_all()

            # Basic memory
            basic_mem_data = state.get("basic_memory")
            if basic_mem_data:
                try:
                    from sirius_pulse.memory.basic import BasicMemoryManager
                    engine.basic_memory = BasicMemoryManager.from_dict(basic_mem_data)
                except Exception as exc:
                    logger.warning("基础记忆恢复失败，使用空实例: %s", exc)
                    from sirius_pulse.memory.basic import BasicMemoryManager
                    engine.basic_memory = BasicMemoryManager(
                        hard_limit=engine.config.get("basic_memory_hard_limit", 30),
                        context_window=engine.config.get("basic_memory_context_window", 5),
                    )

            # Assistant emotion
            ae = state.get("assistant_emotion")
            if ae:
                for key, value in ae.items():
                    if hasattr(engine.assistant_emotion, key):
                        setattr(engine.assistant_emotion, key, value)

            # Group timestamps
            engine._group_last_message_at = dict(state.get("group_timestamps", {}))

            # Reset timestamps to now so the proactive silence timer starts fresh
            # after engine restart; otherwise offline time would be mis-counted as
            # group silence.
            now_iso = datetime.now(timezone.utc).isoformat()
            for gid in list(engine._group_last_message_at.keys()):
                engine._group_last_message_at[gid] = now_iso

            # Diary state
            diary_state = state.get("diary_state")
            if diary_state:
                try:
                    sources = diary_state.get("diarized_sources", {})
                    engine.diary_manager._diarized_sources = {
                        gid: set(sids) for gid, sids in sources.items()
                    }
                except Exception as exc:
                    logger.warning("日记状态恢复失败: %s", exc)

            # User manager (with cross-group global profiles)
            user_mgr_data = state.get("user_manager")
            if user_mgr_data:
                try:
                    from sirius_pulse.memory.user.simple import UserManager
                    engine.user_manager = UserManager.from_dict(user_mgr_data)
                except Exception as exc:
                    logger.warning("用户管理器恢复失败，使用空实例: %s", exc)

            # Re-bind context assembler to restored basic_memory
            from sirius_pulse.memory.context_assembler import ContextAssembler
            engine.context_assembler = ContextAssembler(
                engine.basic_memory,
                engine.diary_manager._retriever,
            )

            # Token usage records
            from sirius_pulse.config import TokenUsageRecord

            for rec_data in state.get("token_usage_records", []):
                try:
                    engine.token_usage_records.append(TokenUsageRecord.from_dict(rec_data))
                except Exception:
                    logger.warning("反序列化 token_usage_records 失败", exc_info=True)
                    pass

            # Load persona
            from sirius_pulse.core.persona_store import PersonaStore

            loaded = PersonaStore.load(engine.work_path)
            if loaded:
                engine.persona = loaded
                logger.info("我的人设已经加载好了，我是 %s～", loaded.name)

            logger.info(
                "之前的记忆都找回来啦，一共 %d 个群的上下文我都记得。",
                len(engine.basic_memory.list_groups()),
            )

            # Initialize sticker system
            engine._sticker._init_sticker_system()

            # Load pinned messages
            pinned_data = state.get("pinned_messages")
            if pinned_data:
                try:
                    from sirius_pulse.core.pinned_message import PinnedMessageManager
                    engine._pinned_manager = PinnedMessageManager.from_dict(pinned_data)
                    logger.info(
                        "钉住消息已恢复，共 %d 条",
                        len(engine._pinned_manager._pinned_messages),
                    )
                except Exception as exc:
                    logger.warning("钉住消息恢复失败，使用空实例: %s", exc)
        except Exception as exc:
            logger.warning("状态恢复部分出错，继续尝试加载 proactive 状态: %s", exc)
        finally:
            # Proactive state must always be attempted regardless of other failures
            self.load_proactive_state()

    def save_proactive_state(self) -> None:
        """Persist proactive enabled/disabled groups and last trigger timestamps."""
        engine = self._engine
        path = Path(engine.work_path) / "engine_state" / "proactive_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        data = {
            "enabled_groups": sorted(engine._proactive_enabled_groups),
            "disabled_groups": sorted(engine._proactive_disabled_groups),
            "last_proactive_at": dict(engine._last_proactive_at),
        }
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def load_proactive_state(self) -> None:
        """Restore proactive state from disk."""
        engine = self._engine
        path = Path(engine.work_path) / "engine_state" / "proactive_state.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning("Proactive state file is not a dict, skipping")
                return
            # Force str keys to avoid int/str mismatch
            engine._proactive_enabled_groups = {str(g) for g in data.get("enabled_groups", [])}
            engine._proactive_disabled_groups = {str(g) for g in data.get("disabled_groups", [])}
            engine._last_proactive_at = {
                str(k): str(v) for k, v in dict(data.get("last_proactive_at", {})).items()
            }
            # Sync into ProactiveTrigger
            engine.proactive_trigger._last_proactive = dict(engine._last_proactive_at)
            logger.info(
                "Proactive state loaded: %d enabled, %d disabled groups",
                len(engine._proactive_enabled_groups),
                len(engine._proactive_disabled_groups),
            )
        except Exception as exc:
            logger.warning("Proactive state 加载失败: %s", exc)

    def set_proactive_enabled(self, group_id: str, enabled: bool) -> None:
        """Enable or disable proactive triggers for a specific group."""
        engine = self._engine
        gid = str(group_id)
        if enabled:
            engine._proactive_enabled_groups.add(gid)
            engine._proactive_disabled_groups.discard(gid)
        else:
            engine._proactive_enabled_groups.discard(gid)
            engine._proactive_disabled_groups.add(gid)
        self.save_proactive_state()

    def is_proactive_enabled(self, group_id: str) -> bool:
        """Check if proactive triggers are enabled for a group.

        Priority:
        1. If group is in disabled list -> False
        2. If enabled_groups is not empty and group not in it -> False
        3. Otherwise -> True
        """
        engine = self._engine
        gid = str(group_id)
        if gid in engine._proactive_disabled_groups:
            return False
        if engine._proactive_enabled_groups:
            return gid in engine._proactive_enabled_groups
        return True
