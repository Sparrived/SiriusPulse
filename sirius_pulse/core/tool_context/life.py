"""她自己的生活：心情的起伏与回落，以及她认识的人。

实现 :class:`sirius_pulse.tools.engine_context.LifeAccess`。
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sirius_pulse.life.clock import DaySchedule

logger = logging.getLogger(__name__)


class LifeMixin:
    """她自己的生活：心情的起伏与回落，以及她认识的人。"""

    _engine: Any

    def nudge_mood(
        self, valence_delta: float, arousal_delta: float = 0.0, reason: str = ""
    ) -> None:
        """Let something in her own life move her mood, and persist it."""
        emotion = getattr(self._engine, "assistant_emotion", None)
        if emotion is None:
            return
        if valence_delta or arousal_delta:
            emotion.apply_event(valence_delta, arousal_delta)
            logger.info("心情因生活事件变化 dv=%+.2f da=%+.2f：%s", valence_delta, arousal_delta, reason)
        self._save_emotion()

    def settle_mood(self, baseline_shift: float = 0.0) -> None:
        """Drift back toward her baseline (and optionally move the baseline itself)."""
        emotion = getattr(self._engine, "assistant_emotion", None)
        if emotion is None:
            return
        if baseline_shift:
            emotion.shift_baseline(baseline_shift)
        emotion.tick_recovery()
        self._save_emotion()

    def mood_label(self) -> str:
        """Her current mood as a short Chinese phrase (e.g. 「心情不错，挺来劲的」)."""
        from sirius_pulse.models.emotion import describe_mood

        emotion = getattr(self._engine, "assistant_emotion", None)
        if emotion is None:
            return ""
        return describe_mood(emotion.valence, emotion.arousal)

    def _save_emotion(self) -> None:
        # 心情写在 engine_state/assistant_emotion.json 里：这是 EngineStateStore 的活，
        # EnginePersistence 只有整体落盘（persist_full_state），没有单存心情的入口。
        store = getattr(self._engine, "_state_store", None)
        emotion = getattr(self._engine, "assistant_emotion", None)
        if store is None or emotion is None:
            return
        try:
            store.save_assistant_emotion(dataclasses.asdict(emotion))
        except Exception:
            logger.warning("保存心情状态失败", exc_info=True)

    def get_day_schedule(self) -> DaySchedule:
        from sirius_pulse.core.life_bridge import day_schedule

        return day_schedule(self._engine)

    def list_acquaintances(self) -> list[Any]:
        """她认识的人：每人最后一次出现的时间、亲近程度，以及在哪些会话里聊过。

        按人汇总各会话的语义画像，并带上她自己对这个人的情绪偏向（user_bias）。
        画像里没有名字时退回用户档案里的名字；仍然叫不出名字的人照常返回，
        由调用方决定要不要理会。
        """
        from sirius_pulse.life.missing import gather

        manager = getattr(self._engine, "semantic_memory", None)
        lister = getattr(manager, "list_all_user_profiles", None)
        if not callable(lister):
            return []
        try:
            rows = list(lister())
        except Exception:
            logger.debug("读取语义画像失败", exc_info=True)
            return []
        emotion = getattr(self._engine, "assistant_emotion", None)
        people = gather(rows, bias=getattr(emotion, "user_bias", None) or {})
        for person in people:
            if person.name:
                continue
            try:
                info = self.get_user_info(person.user_id)
            except Exception:
                info = None
            person.name = str((info or {}).get("name", "") or "").strip()
        return people
