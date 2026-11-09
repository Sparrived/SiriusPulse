"""把多个角色的参考图拼成少量拼图，减少生图请求携带的图片数量。

一张 2×2 拼图最多放四个角色；提示词里按格子的位置说明谁是谁。这样既保留
角色特征，又不会因为一次传五六张参考图把上游生图拖到超时。

本模块只依赖 Pillow，不参与插件加载，任何需要多角色参考图的生图流程都可以复用。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Sequence

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

#: 每个格子的目标边长；2×2 拼图约 1248px，控制在常见 1280px 参考图上限内。
CELL = 600
GAP = 16
_QUALITY = 90
#: 一次最多发给生图模型几张参考图（每张拼图最多四个人）。
MAX_REFERENCE_IMAGES = 4
MAX_PEOPLE_PER_COLLAGE = 4


@dataclass(frozen=True, slots=True)
class ReferenceCollage:
    """一张参考图：*names* 按从左到右、从上到下的顺序对应画面里的角色。"""

    names: tuple[str, ...]
    data: bytes
    columns: int
    rows: int


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, "white")
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return image.convert("RGB")


def _cell(data: bytes, size: int = CELL) -> Image.Image | None:
    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)
        image = _to_rgb(image)
    except Exception as exc:  # noqa: BLE001 - 坏图只跳过这一个角色
        logger.debug("拼图参考读取失败: %s", exc)
        return None
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (size, size), "white")
    canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
    return canvas


def _positions(columns: int, rows: int) -> list[str]:
    if columns == 1 and rows == 1:
        return ["画面"]
    if rows == 1:
        return ["左", "右", "右二", "右三"][:columns]
    return ["左上", "右上", "左下", "右下"][: columns * rows]


def _build_group(people: Sequence[tuple[str, bytes]]) -> ReferenceCollage | None:
    if not people:
        return None
    if len(people) == 1:
        name, data = people[0]
        if _cell(data) is None:
            return None
        # 单人不拼图，直接保留原图，画质最好。
        return ReferenceCollage((name,), data, 1, 1)

    cells: list[tuple[str, bytes, Image.Image]] = []
    for name, data in people:
        cell = _cell(data)
        if cell is not None:
            cells.append((name, data, cell))
    if not cells:
        return None
    if len(cells) == 1:
        name, data, _cell_image = cells[0]
        return ReferenceCollage((name,), data, 1, 1)

    columns = 2
    rows = (len(cells) + columns - 1) // columns
    width = columns * CELL + (columns + 1) * GAP
    height = rows * CELL + (rows + 1) * GAP
    canvas = Image.new("RGB", (width, height), "white")
    used: list[str] = []
    for index, (name, _data, cell) in enumerate(cells):
        column, row = index % columns, index // columns
        canvas.paste(
            cell,
            (GAP + column * (CELL + GAP), GAP + row * (CELL + GAP)),
        )
        used.append(name)
    buffer = io.BytesIO()
    canvas.save(buffer, "JPEG", quality=_QUALITY)
    return ReferenceCollage(tuple(used), buffer.getvalue(), columns, rows)


def build_reference_collages(
    people: Sequence[tuple[str, bytes]],
    *,
    max_images: int = MAX_REFERENCE_IMAGES,
    per_collage: int = MAX_PEOPLE_PER_COLLAGE,
) -> list[ReferenceCollage]:
    """把角色图按最多 *per_collage* 人一组拼起来，最多返回 *max_images* 张。"""
    max_images = max(0, min(int(max_images), MAX_REFERENCE_IMAGES))
    per_collage = max(1, min(int(per_collage), MAX_PEOPLE_PER_COLLAGE))
    if max_images <= 0:
        return []
    groups = [
        list(people[index : index + per_collage])
        for index in range(0, len(people), per_collage)
    ]
    collages: list[ReferenceCollage] = []
    for group in groups[:max_images]:
        collage = _build_group(group)
        if collage is not None:
            collages.append(collage)
    return collages


def reference_note(collages: Sequence[ReferenceCollage]) -> str:
    """写进生图提示词的参考图位置说明；没有参考图时返回空串。"""
    lines: list[str] = []
    for index, collage in enumerate(collages, 1):
        if len(collage.names) == 1:
            lines.append(
                f"第 {index} 张参考图是「{collage.names[0]}」，这个角色的长相和服装严格按这张图。"
            )
            continue
        positions = _positions(collage.columns, collage.rows)
        mapping = "，".join(
            f"{position}={name}" for position, name in zip(positions, collage.names)
        )
        lines.append(
            f"第 {index} 张参考图是一张 {collage.columns}×{collage.rows} 的角色拼图"
            f"（{mapping}）；请从对应格子取角色形象，不要把拼图网格、分割线、"
            "空白格或拼图版式画进画面，每个角色只出现一次。"
        )
    return "\n".join(lines)


__all__ = [
    "MAX_PEOPLE_PER_COLLAGE",
    "MAX_REFERENCE_IMAGES",
    "ReferenceCollage",
    "build_reference_collages",
    "reference_note",
]
