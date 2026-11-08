"""群里刚出现的表情包：她可以现学现用，原图直接拿来回敬。

别人发的表情包会先被缓存到本地、再生成一句描述（她「看得懂」），但以前她只能从
自己固定的十来个表情类别里挑，看懂了也用不上。这里把最近几张表情包按回合编号
（``刚才1``、``刚才2``……），连同描述放进提示词；她输出 ``[STICKER:刚才1]`` 时，
表情包发送路径会把那张原图发出去。

只借表情包和梗图：普通照片（自拍、截图、私人图片）不会出现在这里，转发别人的照片
不是玩梗，是冒犯。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 每轮最多给她看几张可借用的图。
MAX_BORROWED = 4
#: 往回看多少条消息找图。
LOOKBACK = 40
TOKEN_PREFIX = "刚才"

_STICKER_RE = re.compile(r"\[动画表情[：:]([^\]]+)\]")
_IMAGE_RE = re.compile(r"\[图片描述[：:]([^\]]+)\]")
#: 普通图片只有描述里明确是表情包或梗图时才算可借用。
_MEME_WORDS = ("表情包", "梗图", "玩梗", "meme")


@dataclass(frozen=True, slots=True)
class BorrowedImage:
    token: str
    path: Path
    caption: str
    speaker: str


def _caption(content: str) -> str:
    match = _STICKER_RE.search(content)
    if match:
        return match.group(1).strip()
    match = _IMAGE_RE.search(content)
    if match and any(word in match.group(1) for word in _MEME_WORDS):
        return match.group(1).strip()
    return ""


def _image_path(entry: Any) -> Path | None:
    for item in getattr(entry, "multimodal_inputs", None) or []:
        if not isinstance(item, dict) or item.get("type") not in {"image", "sticker"}:
            continue
        raw = str(item.get("file_path") or item.get("value") or "")
        if raw and not raw.startswith(("http://", "https://", "data:")):
            path = Path(raw)
            if path.is_file():
                return path
    return None


def recent_reusable_images(entries: list[Any], limit: int = MAX_BORROWED) -> list[BorrowedImage]:
    """从最近的群消息里挑出可以借用的表情包，最新的在前。"""
    found: list[BorrowedImage] = []
    seen: set[Path] = set()
    for entry in reversed(entries[-LOOKBACK:]):
        if getattr(entry, "role", "") == "assistant":
            continue
        caption = _caption(str(getattr(entry, "content", "") or ""))
        if not caption:
            continue
        path = _image_path(entry)
        if path is None or path in seen:
            continue
        seen.add(path)
        speaker = str(getattr(entry, "speaker_name", "") or getattr(entry, "user_id", "") or "")
        found.append(BorrowedImage(f"{TOKEN_PREFIX}{len(found) + 1}", path, caption, speaker))
        if len(found) >= limit:
            break
    return found


def remember(engine: Any, group_id: str, images: list[BorrowedImage]) -> None:
    """记下这一轮的编号，供标记解析与发送时查回原图。"""
    registry = getattr(engine, "_borrowed_images", None)
    if not isinstance(registry, dict):
        registry = {}
        setattr(engine, "_borrowed_images", registry)
    registry[group_id] = {image.token: image.path for image in images}


def names_for(engine: Any, group_id: str) -> list[str]:
    registry = getattr(engine, "_borrowed_images", None) or {}
    return list((registry.get(group_id) or {}).keys())


def lookup(engine: Any, group_id: str, name: str) -> Path | None:
    registry = getattr(engine, "_borrowed_images", None) or {}
    return (registry.get(group_id) or {}).get(name)


def render_block(images: list[BorrowedImage]) -> str:
    if not images:
        return ""
    lines = ["【群里刚出现的表情包】别人刚发的图也是对话的一部分：可以接梗、学着用，" "或者原样拿来回敬，用 [STICKER:刚才1] 这样的标记发出去（和你自己的表情包一样）。"]
    for image in images:
        caption = image.caption if len(image.caption) <= 60 else image.caption[:60] + "…"
        lines.append(f"- {image.token}：{caption}（{image.speaker} 发的）")
    return "\n".join(lines)
