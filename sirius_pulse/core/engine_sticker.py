"""表情包系统相关方法。

包含表情包初始化、选择、发送等功能。
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)


class EngineSticker:
    """表情包系统相关方法组件。"""

    def __init__(self, engine: _EmotionalGroupChatEngineBase) -> None:
        self._engine = engine

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
            engine.brain.sticker_names = []
            return

        image_extensions = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        names: set[str] = set()
        for f in stickers_dir.iterdir():
            if f.is_file() and f.suffix.lower() in image_extensions:
                stem = f.stem
                # 含 __ 的文件取前缀作为表情包名称（如 "喜欢__可爱.jpg" → "喜欢"）
                if "__" in stem:
                    names.add(stem.split("__", 1)[0])
                else:
                    names.add(stem)
        engine._sticker_names = sorted(names)
        engine.brain.sticker_names = engine._sticker_names
        logger.info(
            "表情包系统初始化完成: 共 %d 个表情包名称，来自 %d 个文件",
            len(engine._sticker_names),
            sum(1 for _ in stickers_dir.iterdir() if _.is_file() and _.suffix.lower() in image_extensions),
        )

    def _pick_sticker_file(self, names: list[str]) -> Path | None:
        """从模型选择的名称列表中随机选一个，再匹配对应的图片文件。

        模型选 1-3 个名称，本地从中随机选 1 个发送。
        匹配规则：
        - 精确匹配：`喜欢.jpg`
        - 包匹配：`喜欢__可爱.jpg`、`喜欢__生气.jpg`（`__` 前缀属于同一包）
        从所有匹配文件中随机选一个。
        """
        engine = self._engine
        if not names:
            return None

        stickers_dir = Path(engine.work_path) / "stickers"
        if not stickers_dir.is_dir():
            return None

        image_extensions = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}

        # 从模型选的名称中随机挑一个
        chosen_name = random.choice(names[:3])

        candidates: list[Path] = []

        # 1. 精确匹配：{name}.{ext}
        for ext in image_extensions:
            candidate = stickers_dir / f"{chosen_name}{ext}"
            if candidate.is_file():
                candidates.append(candidate)

        # 2. 包匹配：{name}__*.{ext}（支持同包多文件随机选一）
        for f in stickers_dir.iterdir():
            if f.is_file() and f.suffix.lower() in image_extensions:
                if f.stem.startswith(f"{chosen_name}__"):
                    candidates.append(f)

        return random.choice(candidates) if candidates else None

    async def _send_stickers_by_names(
        self,
        group_id: str,
        names: list[str],
    ) -> dict[str, Any]:
        """从模型选中的名称中随机挑一个表情包发送（sub_type=1）。"""
        engine = self._engine
        fp = self._pick_sticker_file(names)
        if fp is None:
            return {"success": False, "error": "没有匹配的表情包文件"}

        adapter = getattr(engine, "_adapter", None)
        if adapter is None:
            return {"success": False, "error": "没有可用的 adapter"}

        try:
            msg = [{"type": "image", "data": {"file": str(fp), "sub_type": "1"}}]
            if group_id.startswith("private_"):
                await adapter.send_private_msg(group_id.replace("private_", ""), msg)
            else:
                await adapter.send_group_msg(group_id, msg)

            logger.info("表情包已发送: %s -> %s", fp.name, group_id)
            return {
                "success": True,
                "sticker_name": fp.stem,
                "file_path": str(fp),
            }
        except Exception as exc:
            logger.warning("表情包发送失败: %s %s", fp.name, exc)
            return {"success": False, "error": str(exc), "file_path": str(fp)}
