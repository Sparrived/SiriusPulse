"""持久化相关方法。

包含状态保存/加载、记忆持久化等功能。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)


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
