"""内置的 read_image TOOL：把本地图片 / 图片 URL 读进模型的视觉通道。

模型平时只能看到文字；``read_image`` 把图片交给模型自己看，用于：

- 查看自己的三视图 / 立绘（路径由 system prompt 的「你自己的形象参考图」给出）；
- 看清群里发来的图片原图（历史 XML 或「本地图片路径」里给出的缓存路径）；
- 确认某个本地文件 / 链接到底是不是图片、画了什么。

本地图片以路径形式返回，由传输层统一转成 data URL 内联给上游；URL 会先下载到
人格的 tool_data 下再内联，避免上游下载签名链接失败（与聊天图片同一套教训）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.tools.builtin._internal._bash_runtime import (
    expand_persona_path,
    home_root_path,
)
from sirius_pulse.tools.models import ToolInvocationContext

logger = logging.getLogger(__name__)

_MAX_IMAGES = 4
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 30.0
_DOWNLOAD_KEEP_FILES = 50

#: 有些图床按 UA 拦机器请求；与生图/聊天图片的下载保持一致的浏览器 UA。
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_EXTENSION_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}

_config = ConfigBuilder()
_config.group("读图").add(
    "paths",
    type="list",
    required=True,
    description=("要查看的图片，1-4 张。每项是本地绝对路径或 http(s) 图片 URL；" "本地路径支持 ~（人格 home），也支持聊天图片缓存路径。"),
)
_config.group("读图").add(
    "focus",
    type="str",
    default="",
    description="可选：这次想重点看什么（例如发色、服装花纹、画面里的文字），帮助你把注意力放在细节上。",
)

TOOL_META = {
    "name": "read_image",
    "description": (
        "查看图片的实际画面：把本地图片或图片 URL 读进你的视觉通道。"
        "用于看自己的三视图/设定图（路径见 system prompt 的「你自己的形象参考图」）、"
        "看聊天图片的原图、确认某个本地图片文件的内容。一次最多 4 张，"
        "返回的图片会作为随附图片给你；只能读图片，不要用它读文本或其它文件。"
    ),
    "version": "1.0.0",
    "side_effect": "read_only",
    "tags": ["image", "vision", "read"],
    "dependencies": ["httpx"],
    "parameters": _config.build(),
}


async def run(
    paths: list[str] | None = None,
    focus: str = "",
    data_store: Any = None,
    invocation_context: ToolInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """读取图片并返回多模态结果，让模型真正看到图片内容。"""
    del invocation_context, kwargs
    try:
        sources = _normalize_sources(paths)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if not sources:
        return {
            "success": False,
            "error": "paths 不能为空：请给出至少一张图片的本地绝对路径或 http(s) URL。",
        }
    if len(sources) > _MAX_IMAGES:
        return {
            "success": False,
            "error": f"一次最多读 {_MAX_IMAGES} 张图片，这次给了 {len(sources)} 张。",
        }

    resolved: list[tuple[Path, str, int, str]] = []
    total_bytes = 0
    try:
        for index, source in enumerate(sources, start=1):
            path, mime_type, size = await _resolve_source(source, data_store)
            total_bytes += size
            if total_bytes > _MAX_TOTAL_BYTES:
                raise ValueError(f"图片总大小超过 {_MAX_TOTAL_BYTES // 1024 // 1024}MB，请分几次读。")
            resolved.append((path, mime_type, size, _image_label(source, index)))
    except (OSError, ValueError, httpx.HTTPError) as exc:
        return {"success": False, "error": str(exc)}

    source_lines = [
        f"- {label}：{path}（{mime_type}，{size / 1024:.0f} KB）"
        for path, mime_type, size, label in resolved
    ]
    text_blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "label": "summary",
            "value": f"已读取 {len(resolved)} 张图片，图片内容见随附图片。",
        },
        {
            "type": "text",
            "label": "sources",
            "value": "来源与本地位置：\n" + "\n".join(source_lines),
        },
    ]
    focus_text = " ".join(str(focus or "").split())
    if focus_text:
        text_blocks.append(
            {
                "type": "text",
                "label": "analysis_hint",
                "value": f"这次读图的重点：{focus_text}。请结合图片回答，看不清就说看不清，不要臆测。",
            }
        )

    return {
        "success": True,
        "text_blocks": text_blocks,
        "multimodal_blocks": [
            {
                "type": "image",
                "label": label,
                "value": str(path),
                "mime_type": mime_type,
            }
            for path, mime_type, _size, label in resolved
        ],
        "internal_metadata": {
            "sources": [source for source in sources],
            "resolved_paths": [str(path) for path, _mime, _size, _label in resolved],
            "focus": focus_text,
        },
    }


def _normalize_sources(raw: Any) -> list[str]:
    """把模型给的 paths 参数整理成字符串列表（兼容被序列化成 JSON 字符串的数组）。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        raw = parsed if isinstance(parsed, list) else [text]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("paths 必须是字符串数组")
    return [str(item).strip() for item in raw if str(item or "").strip()]


async def _resolve_source(source: str, data_store: Any) -> tuple[Path, str, int]:
    """把一项来源解析成可交给传输层的本地图片路径与媒体类型。"""
    if source.lower().startswith(("http://", "https://")):
        data = await _download_image(source)
        mime_type = _sniff_image_mime(data)
        if not mime_type:
            raise ValueError(f"链接内容不是可识别的图片：{source}")
        path = _save_download(data, mime_type, data_store)
        return path, mime_type, path.stat().st_size

    path = _resolve_local_path(source, data_store)
    if path is None or not path.is_file():
        raise ValueError(f"图片不存在：{source}")
    size = path.stat().st_size
    if size > _MAX_IMAGE_BYTES:
        raise ValueError(f"图片过大（{size} 字节），单张最多 {_MAX_IMAGE_BYTES // 1024 // 1024}MB：{source}")
    mime_type = _sniff_image_mime(path.read_bytes())
    if not mime_type:
        raise ValueError(f"不是可识别的图片文件：{source}")
    return path.resolve(), mime_type, size


def _resolve_local_path(source: str, data_store: Any) -> Path | None:
    text = str(source or "").strip()
    if not text:
        return None
    if text.lower().startswith("file://"):
        parsed = urlparse(text)
        raw_path = unquote(parsed.path or "")
        if parsed.netloc:
            raw_path = f"//{parsed.netloc}{raw_path}"
        if raw_path.startswith("/") and len(raw_path) >= 3 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        return Path(raw_path)
    return expand_persona_path(text, home=home_root_path(data_store))


def _image_label(source: str, index: int) -> str:
    """给图片取一个方便模型引用的短标签（文件名，没有就用序号）。"""
    lowered = source.lower()
    if lowered.startswith(("http://", "https://", "file://")):
        name = Path(unquote(urlparse(source).path)).name
    else:
        name = Path(source.rstrip("/")).name
    stem = Path(name).stem.strip()
    cleaned = re.sub(r"\s+", " ", stem)[:60]
    return cleaned or f"图片{index}"


def _sniff_image_mime(data: bytes) -> str:
    """按字节魔数判断图片类型；识别不了返回空字符串。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return ""


async def _download_image(url: str) -> bytes:
    """下载图片，边读边计数，避免被超大文件拖垮内存。"""
    async with httpx.AsyncClient(
        timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as client:
        async with client.stream(
            "GET", url, headers={"User-Agent": _BROWSER_USER_AGENT}
        ) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > _MAX_IMAGE_BYTES:
                raise ValueError(f"图片过大，单张最多 {_MAX_IMAGE_BYTES // 1024 // 1024}MB：{url}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > _MAX_IMAGE_BYTES:
                    raise ValueError(f"图片过大，单张最多 {_MAX_IMAGE_BYTES // 1024 // 1024}MB：{url}")
                chunks.append(chunk)
            data = b"".join(chunks)
            if not data:
                raise ValueError(f"图片内容为空：{url}")
            return data


def _save_download(data: bytes, mime_type: str, data_store: Any) -> Path:
    """按内容哈希保存下载的图片，重复读取同一 URL 时直接复用。"""
    directory = _artifact_dir(data_store) / "downloads"
    directory.mkdir(parents=True, exist_ok=True)
    suffix = _EXTENSION_BY_MIME.get(mime_type, ".img")
    path = directory / f"{hashlib.sha1(data).hexdigest()[:20]}{suffix}"
    if not path.is_file():
        path.write_bytes(data)
        _cleanup_downloads(directory)
    return path.resolve()


def _cleanup_downloads(directory: Path, *, keep: int = _DOWNLOAD_KEEP_FILES) -> None:
    """下载缓存同样要有上限，避免整天读 URL 把磁盘堆满。"""
    try:
        files = sorted(
            (item for item in directory.iterdir() if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for old_file in files[keep:]:
        try:
            old_file.unlink(missing_ok=True)
        except OSError:
            pass


def _artifact_dir(data_store: Any) -> Path:
    artifact_dir = getattr(data_store, "artifact_dir", None)
    if isinstance(artifact_dir, Path):
        return artifact_dir
    if artifact_dir:
        return Path(str(artifact_dir))
    return Path.home() / ".sirius_pulse" / "tool_artifacts" / "read_image"
