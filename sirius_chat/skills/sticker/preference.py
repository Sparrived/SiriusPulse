"""人格表情包偏好管理：自动生成、运行时学习。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sirius_chat.skills.sticker.models import StickerPreference

logger = logging.getLogger(__name__)

_TAG_GENERATION_PROMPT = """根据以下人格设定，推断该角色喜欢的表情包风格。

人格名称：{name}
性格特点：{traits}
说话风格：{communication_style}
社交角色：{social_role}
幽默风格：{humor_style}

要求输出 JSON：
{{
  "preferred_tags": ["标签1", "标签2", ...],
  "avoided_tags": ["标签1", ...],
  "style_weights": {{"cute": 0.3, "sarcastic": 0.7, ...}},
  "novelty_preference": 0.5,
  "emotion_tag_map": {{
    "joy": ["大笑", "庆祝", "得意"],
    "anger": ["生气", "吐槽", "无奈"],
    "sadness": ["委屈", "哭泣", "安慰"],
    "anxiety": ["紧张", "尴尬", "捂脸"],
    "neutral": ["日常", "默认", "随便"]
  }}
}}

标签要求：
- 用中文，每个标签 2-4 个字
- preferred_tags：该人格会主动选择的表情包风格
- avoided_tags：该人格不会选择的表情包风格
- style_weights：各种风格的倾向权重（0-1）
- novelty_preference：喜新程度（0=恋旧，1=追新）
- emotion_tag_map：每种情绪下偏好的标签"""


class StickerPreferenceManager:
    """管理人格表情包偏好的生成、加载、保存和更新。"""

    def __init__(
        self,
        work_path: Path | str,
        persona_name: str,
        model_name: str = "gpt-4o-mini",
        token_callback: Any | None = None,
    ) -> None:
        self._work_path = Path(work_path)
        self._persona_name = persona_name
        self._model_name = model_name
        self._token_callback = token_callback
        self._preference: StickerPreference | None = None
        self._file_path = self._work_path / "sticker_preference.json"

    def load(self) -> StickerPreference:
        """加载偏好，如果不存在则返回默认。"""
        if self._preference is not None:
            return self._preference

        if self._file_path.exists():
            try:
                data = json.loads(self._file_path.read_text(encoding="utf-8"))
                self._preference = StickerPreference.from_dict(data)
                logger.info("加载表情包偏好: %s", self._persona_name)
                return self._preference
            except Exception as exc:
                logger.warning("加载表情包偏好失败: %s", exc)

        self._preference = StickerPreference()
        return self._preference

    def save(self) -> None:
        """保存偏好到磁盘。"""
        if self._preference is None:
            return
        self._work_path.mkdir(parents=True, exist_ok=True)
        try:
            self._file_path.write_text(
                json.dumps(self._preference.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("保存表情包偏好失败: %s", exc)

    async def generate_from_persona(self, persona: Any, provider_async: Any | None = None) -> StickerPreference:
        """根据人格设定自动生成偏好。"""
        preference = StickerPreference()

        if provider_async is None:
            logger.warning("无 provider，使用默认偏好")
            self._preference = preference
            self.save()
            return preference

        try:
            prompt = _TAG_GENERATION_PROMPT.format(
                name=getattr(persona, "name", "未知"),
                traits=", ".join(getattr(persona, "personality_traits", [])),
                communication_style=getattr(persona, "communication_style", ""),
                social_role=getattr(persona, "social_role", ""),
                humor_style=getattr(persona, "humor_style", ""),
            )

            from sirius_chat.providers.base import GenerationRequest

            request = GenerationRequest(
                model=self._model_name,
                system_prompt=prompt,
                messages=[{"role": "user", "content": "生成表情包偏好"}],
                temperature=0.3,
                max_tokens=512,
                purpose="sticker_preference_generate",
            )

            import time

            t0 = time.perf_counter()
            raw = await provider_async.generate_async(request)
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)

            if self._token_callback is not None:
                try:
                    self._token_callback(
                        task_name="sticker_preference_generate",
                        model_name=self._model_name,
                        group_id="",
                        request=request,
                        duration_ms=duration_ms,
                    )
                except Exception:
                    pass

            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0]
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0]
            data = json.loads(raw.strip())

            preference.preferred_tags = list(data.get("preferred_tags", []))
            preference.avoided_tags = list(data.get("avoided_tags", []))
            preference.style_weights = dict(data.get("style_weights", {}))
            preference.novelty_preference = float(data.get("novelty_preference", 0.5))
            preference.emotion_tag_map = dict(data.get("emotion_tag_map", {}))

            logger.info("自动生成表情包偏好: preferred=%s, avoided=%s",
                        preference.preferred_tags, preference.avoided_tags)
        except Exception as exc:
            logger.warning("自动生成表情包偏好失败: %s", exc)

        self._preference = preference
        self.save()
        return preference

    def record_usage(self, sticker_id: str, tags: list[str], emotion: str = "neutral") -> None:
        """记录一次表情包使用。"""
        pref = self.load()

        # 更新近期使用窗口
        pref.recent_usage_window.append({
            "sticker_id": sticker_id,
            "tags": list(tags),
            "emotion": emotion,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if len(pref.recent_usage_window) > pref.recent_window_size:
            pref.recent_usage_window = pref.recent_usage_window[-pref.recent_window_size:]

        self.save()

    def update_tag_success(self, sticker_id: str, tags: list[str], success: bool) -> None:
        """更新标签成功率。"""
        pref = self.load()
        delta = 0.05 if success else -0.03

        for tag in tags:
            current = pref.tag_success_rate.get(tag, 0.5)
            pref.tag_success_rate[tag] = max(0.0, min(1.0, current + delta))

        self.save()

    def update_group_feedback(self, tags: list[str], positive: bool) -> None:
        """更新群聊标签反馈。"""
        pref = self.load()
        delta = 0.04 if positive else -0.02

        for tag in tags:
            current = pref.group_tag_feedback.get(tag, 0.5)
            pref.group_tag_feedback[tag] = max(0.0, min(1.0, current + delta))

        self.save()

    @property
    def preference(self) -> StickerPreference:
        return self.load()
