"""人格自身参考图（三视图 / 立绘）的发现与 prompt 说明。

约定：把自己的形象设定图放在 ``{persona}/assets/`` 下（可以按服装、形态建子目录）。
框架在每次对话的 system prompt 里列出这些图片的**本地绝对路径**，模型才能：

- 把它们直接填进 ``image_generate`` 的 ``reference_images``，保持角色长相与服装一致；
- 用 ``read_image`` 真正看清图中的细节（发色、配饰、花纹等）。

扫描只做尽力而为：目录不存在、单个文件没权限都只是少列一张图，绝不影响对话。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ASSETS_DIR_NAME = "assets"

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
_MAX_LISTED_ASSETS = 16

# 文件名 token → 中文标注。英文按 token 精确匹配，避免 "feedback" 里的 "back" 之类误判。
_VIEW_KEYWORDS: tuple[tuple[tuple[str, ...], str, int], ...] = (
    (("turnaround", "sheet", "threeview", "three_view", "concept"), "三视图合集", 0),
    (("front",), "正视图", 1),
    (("side", "profile"), "侧视图", 2),
    (("back",), "后视图", 3),
    (("bust", "portrait", "closeup", "close_up", "head", "avatar"), "半身/头像", 4),
    (("chibi",), "Q版", 5),
)
# 中文文件名没有分隔符，只能按子串匹配；顺序即优先级。
_CHINESE_VIEW_KEYWORDS: tuple[tuple[str, str, int], ...] = (
    ("三视", "三视图合集", 0),
    ("立绘", "立绘", 5),
    ("正", "正视图", 1),
    ("侧", "侧视图", 2),
    ("背", "后视图", 3),
    ("半身", "半身/头像", 4),
    ("头像", "半身/头像", 4),
    ("q版", "Q版", 5),
)


@dataclass(frozen=True, slots=True)
class PersonaAsset:
    """一张人格自己的形象参考图。"""

    path: Path
    relative: str
    label: str
    rank: int


def _tokens(stem: str) -> list[str]:
    return [token for token in re.split(r"[^0-9a-z]+", stem.lower()) if token]


def _label_and_rank(relative: str) -> tuple[str, int]:
    """从相对路径猜「这是哪张视图」；猜不出就退回文件名。"""
    stem = Path(relative).stem
    lowered = stem.lower()
    tokens = _tokens(stem)
    for keywords, label, rank in _VIEW_KEYWORDS:
        if any(keyword in tokens for keyword in keywords):
            return label, rank
    for keyword, label, rank in _CHINESE_VIEW_KEYWORDS:
        if keyword in lowered:
            return label, rank
    return stem, 9


def collect_persona_assets(assets_dir: Path | str | None) -> list[PersonaAsset]:
    """扫描人格的形象参考图目录，按「合集 → 正/侧/后/半身 → 其他」排序。"""
    if not assets_dir:
        return []
    root = Path(assets_dir).expanduser()
    if not root.is_dir():
        return []
    try:
        candidates = sorted(root.rglob("*"))
    except OSError as exc:
        logger.debug("扫描人格参考图目录失败: %s | %s", root, exc)
        return []

    assets: list[PersonaAsset] = []
    for path in candidates:
        try:
            if not path.is_file() or path.suffix.lower() not in _IMAGE_SUFFIXES:
                continue
            relative_path = path.relative_to(root)
            if any(part.startswith(".") for part in relative_path.parts):
                continue
            relative = relative_path.as_posix()
            label, rank = _label_and_rank(relative)
            assets.append(
                PersonaAsset(
                    path=path.resolve(),
                    relative=relative,
                    label=label,
                    rank=rank,
                )
            )
        except OSError as exc:
            logger.debug("读取人格参考图失败，已跳过: %s | %s", path, exc)
            continue
    assets.sort(key=lambda asset: (asset.rank, asset.relative.lower()))
    return assets


def build_persona_assets_section(assets_dir: Path | str | None) -> str:
    """构建「你自己的形象参考图」prompt 段落；没有可用图片时返回空字符串。"""
    assets = collect_persona_assets(assets_dir)
    if not assets:
        return ""

    lines = ["【你自己的形象参考图】以下是你自己的官方设定图（三视图 / 立绘），已保存在本地："]
    for asset in assets[:_MAX_LISTED_ASSETS]:
        lines.append(f"- {asset.label}（{asset.relative}）：{asset.path}")
    if len(assets) > _MAX_LISTED_ASSETS:
        root = Path(assets_dir).expanduser().resolve()
        lines.append(f"- 还有 {len(assets) - _MAX_LISTED_ASSETS} 张未列出，全部在 {root} 下。")
    lines.append("")
    lines.append(
        "要画你自己、或需要保持长相与服装一致时，把对应路径直接传给 image_generate 的 "
        "reference_images；想仔细看清图中细节时，用 read_image 读取这些路径。"
    )
    return "\n".join(lines)
