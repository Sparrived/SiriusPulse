"""表情包系统相关方法。

包含表情包初始化、选择、发送等功能。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StickerChoice:
    """一次表情包选择结果。"""

    name: str
    file_path: Path


STICKER_MISSEND_PROBABILITY = 0.05
STICKER_MISSEND_RECALL_DELAY_SECONDS = 1.2
STICKER_OPPOSITION_CACHE_FILE = "sticker_oppositions.json"


class EngineSticker:
    """表情包系统相关方法组件。"""

    def __init__(self, engine: _EmotionalGroupChatEngineBase) -> None:
        self._engine = engine

    def _available_sticker_files(self) -> list[Path]:
        """获取当前人格目录下可用的表情包图片文件。"""
        stickers_dir = Path(self._engine.work_path) / "stickers"
        if not stickers_dir.is_dir():
            return []
        image_extensions = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        return [
            f for f in stickers_dir.iterdir()
            if f.is_file() and f.suffix.lower() in image_extensions
        ]

    def _sticker_name_from_file(self, file_path: Path) -> str:
        """从文件名解析表情包名称，兼容 name__variant 命名。"""
        return file_path.stem.split("__", 1)[0] if "__" in file_path.stem else file_path.stem

    def _init_sticker_system(self) -> None:
        """扫描 stickers 文件夹，获取可用表情包名称列表。

        支持 `__` 分隔符命名：`喜欢__可爱.jpg`、`喜欢__生气.jpg`
        都属于"喜欢"表情包，AI 发送 [STICKERS: "喜欢"] 时从中随机选一张。
        """
        engine = self._engine
        stickers_dir = Path(engine.work_path) / "stickers"
        if not stickers_dir.is_dir():
            logger.info("表情包目录不存在，跳过初始化: %s", stickers_dir)
            engine._sticker_names = []
            engine._sticker_oppositions = {}
            engine.brain.sticker_names = []
            return

        sticker_files = self._available_sticker_files()
        names = {self._sticker_name_from_file(f) for f in sticker_files}
        engine._sticker_names = sorted(names)
        engine.brain.sticker_names = engine._sticker_names
        engine._sticker_oppositions = self._load_cached_oppositions()
        logger.info(
            "表情包系统初始化完成: 共 %d 个表情包名称，来自 %d 个文件",
            len(engine._sticker_names),
            len(sticker_files),
        )

    def _pick_sticker_choice(self, names: list[str]) -> StickerChoice | None:
        """从模型选择的名称列表中随机选一个，再匹配对应的图片文件。"""
        if not names:
            return None

        chosen_name = random.choice(names[:3])
        candidates = [
            f for f in self._available_sticker_files()
            if self._sticker_name_from_file(f) == chosen_name
        ]
        if not candidates:
            return None
        return StickerChoice(name=chosen_name, file_path=random.choice(candidates))

    def _sticker_name_snapshot(self) -> str:
        """生成表情包词表快照，用于判断词表是否变化。"""
        names = sorted(getattr(self._engine, "_sticker_names", []))
        return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()

    def _load_cached_oppositions(self) -> dict[str, list[str]]:
        """加载已缓存的表情包二元组。"""
        cache_file = Path(self._engine.work_path) / "engine_state" / STICKER_OPPOSITION_CACHE_FILE
        if not cache_file.is_file():
            return {}
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("读取表情包二元组缓存失败", exc_info=True)
            return {}
        if not isinstance(data, dict):
            return {}
        snapshot = data.get("snapshot")
        oppositions = data.get("oppositions")
        current_snapshot = self._sticker_name_snapshot()
        if snapshot != current_snapshot or not isinstance(oppositions, dict):
            return {}
        result: dict[str, list[str]] = {}
        available = set(getattr(self._engine, "_sticker_names", []))
        for key, value in oppositions.items():
            if not isinstance(key, str) or not isinstance(value, list):
                continue
            filtered = [item for item in value if isinstance(item, str) and item in available]
            if filtered:
                result[key] = filtered[:5]
        return result

    def _save_cached_oppositions(self, oppositions: dict[str, list[str]]) -> None:
        """保存表情包二元组缓存到引擎状态目录。"""
        path = Path(self._engine.work_path) / "engine_state" / STICKER_OPPOSITION_CACHE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "snapshot": self._sticker_name_snapshot(),
            "oppositions": oppositions,
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    async def _build_opposition_cache(self) -> dict[str, list[str]]:
        """按当前词表调用一次 LLM，批量生成二元对立缓存。"""
        brain = getattr(self._engine, "brain", None)
        raw_call = getattr(brain, "raw_call", None)
        names = list(getattr(self._engine, "_sticker_names", []))
        if raw_call is None or not names:
            return {}

        from sirius_pulse.core.brain import RawRequest

        system_prompt = (
            "你只负责把表情包名称分成明显的二元对立关系。"
            "必须只使用给定名称，不允许发明新词。"
            "返回 JSON，格式为 {\"pairs\": [{\"base\": \"A\", \"opposites\": [\"B\", \"C\"]}] }。"
            "base 和 opposites 都必须来自候选列表。"
            "只保留你非常确定的对立关系。"
        )
        user_payload = {
            "candidates": names,
            "rules": [
                "base 与 opposites 都必须是 candidates 原样存在的名称",
                "只保留明显不符合语境的对立关系",
                "最多返回 20 个 base 项目",
            ],
        }
        try:
            raw = await raw_call(
                RawRequest(
                    model=getattr(self._engine, "_default_model", ""),
                    system_prompt=system_prompt,
                    messages=[
                        {
                            "role": "user",
                            "content": json.dumps(user_payload, ensure_ascii=False),
                        }
                    ],
                    temperature=0.0,
                    max_tokens=512,
                    timeout_seconds=20.0,
                    purpose="sticker_opposition_bootstrap",
                    response_format={"type": "json_object"},
                    retry_max=1,
                )
            )
            data = json.loads(raw)
        except Exception as exc:
            logger.warning("生成表情包二元组失败: %s", exc)
            return {}

        pairs = data.get("pairs") if isinstance(data, dict) else None
        if not isinstance(pairs, list):
            return {}
        available = set(names)
        oppositions: dict[str, list[str]] = {}
        for item in pairs:
            if not isinstance(item, dict):
                continue
            base = item.get("base")
            opps = item.get("opposites")
            if not isinstance(base, str) or not isinstance(opps, list):
                continue
            filtered = [
                name for name in opps
                if isinstance(name, str) and name in available and name != base
            ]
            if filtered:
                oppositions[base] = sorted(dict.fromkeys(filtered))[:5]
        return oppositions

    async def warmup_opposition_cache(self) -> None:
        """启动后按表情包词表变化预热二元对立缓存。"""
        engine = self._engine
        if not getattr(engine, "_sticker_names", []):
            engine._sticker_oppositions = {}
            return

        cached = self._load_cached_oppositions()
        if cached:
            engine._sticker_oppositions = cached
            logger.info("表情包二元对立缓存已加载: %d 项", len(cached))
            return

        oppositions = await self._build_opposition_cache()
        engine._sticker_oppositions = oppositions
        self._save_cached_oppositions(oppositions)
        logger.info("表情包二元对立缓存已生成: %d 项", len(oppositions))

    def _pick_sticker_file(self, names: list[str]) -> Path | None:
        """从模型选择的名称列表中随机选一个，再匹配对应的图片文件。

        模型选 1-3 个名称，本地从中随机选 1 个发送。
        匹配规则：
        - 精确匹配：`喜欢.jpg`
        - 包匹配：`喜欢__可爱.jpg`、`喜欢__生气.jpg`（`__` 前缀属于同一包）
        从所有匹配文件中随机选一个。
        """
        choice = self._pick_sticker_choice(names)
        return choice.file_path if choice else None

    def _pick_wrong_sticker_choice(self, intended_name: str) -> StickerChoice | None:
        """从预生成二元对立缓存中挑选错误表情包。"""
        oppositions = getattr(self._engine, "_sticker_oppositions", {})
        if not isinstance(oppositions, dict):
            return None
        wrong_names = oppositions.get(intended_name, [])
        if not isinstance(wrong_names, list) or not wrong_names:
            return None
        return self._pick_sticker_choice(wrong_names)

    def _build_missend_followup_text(self) -> str:
        """生成撤回后补发时附带的人格化失误说明。"""
        persona_name = getattr(getattr(self._engine, "persona", None), "name", "我") or "我"
        candidates = [
            f"刚刚手滑点错了，{persona_name}先把它撤了……这张才对。",
            "等下，刚才那个表情完全不对劲，我重发一下。",
            "啊不是那张，我刚刚发错表情包了，换这张。",
            "刚才那个不算，表情包选错频道了。",
        ]
        return random.choice(candidates)

    async def _send_sticker_message(
        self,
        adapter: Any,
        group_id: str,
        choice: StickerChoice,
        text: str = "",
    ) -> dict[str, Any]:
        """发送单张表情包，可选附带文本。"""
        msg: list[dict[str, Any]] = []
        if text:
            msg.append({"type": "text", "data": {"text": text}})
        msg.append({"type": "image", "data": {"file": str(choice.file_path), "sub_type": "1"}})
        if group_id.startswith("private_"):
            return await adapter.send_private_msg(group_id.replace("private_", ""), msg)
        return await adapter.send_group_msg(group_id, msg)

    async def _recall_message(self, adapter: Any, result: dict[str, Any]) -> None:
        """根据发送接口返回的 message_id 调用适配器撤回接口。"""
        data = result.get("data", {}) if isinstance(result, dict) else {}
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if message_id is None:
            logger.warning("错发表情包撤回失败：发送结果缺少 message_id")
            return
        await adapter.delete_message(str(message_id))

    async def _send_stickers_by_names(
        self,
        group_id: str,
        names: list[str],
    ) -> dict[str, Any]:
        """从模型选中的名称中随机挑一个表情包发送（sub_type=1）。"""
        engine = self._engine
        intended = self._pick_sticker_choice(names)
        if intended is None:
            return {"success": False, "error": "没有匹配的表情包文件"}

        adapter = getattr(engine, "_adapter", None)
        if adapter is None:
            return {"success": False, "error": "没有可用的 adapter"}

        try:
            wrong = None
            if random.random() < STICKER_MISSEND_PROBABILITY:
                wrong = self._pick_wrong_sticker_choice(intended.name)

            if wrong is None:
                await self._send_sticker_message(adapter, group_id, intended)
                logger.info("表情包已发送: %s -> %s", intended.file_path.name, group_id)
                return {
                    "success": True,
                    "sticker_name": intended.file_path.stem,
                    "file_path": str(intended.file_path),
                }

            wrong_result = await self._send_sticker_message(adapter, group_id, wrong)
            await asyncio.sleep(STICKER_MISSEND_RECALL_DELAY_SECONDS)
            await self._recall_message(adapter, wrong_result)
            await self._send_sticker_message(
                adapter,
                group_id,
                intended,
                text=self._build_missend_followup_text(),
            )
            logger.info(
                "触发表情包错发: wrong=%s intended=%s group=%s",
                wrong.file_path.name,
                intended.file_path.name,
                group_id,
            )
            return {
                "success": True,
                "missent": True,
                "sticker_name": intended.file_path.stem,
                "wrong_sticker_name": wrong.file_path.stem,
                "file_path": str(intended.file_path),
                "wrong_file_path": str(wrong.file_path),
            }
        except Exception as exc:
            logger.warning("表情包发送失败: %s %s", intended.file_path.name, exc)
            return {"success": False, "error": str(exc), "file_path": str(intended.file_path)}
