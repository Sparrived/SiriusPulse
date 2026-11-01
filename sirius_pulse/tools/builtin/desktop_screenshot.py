"""Built-in tool for capturing the host desktop as an image artifact."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.tools.models import ToolInvocationContext
from sirius_pulse.tools.security import ensure_developer_access

_config = ConfigBuilder()
_config.group("截图设置").add(
    "all_screens",
    type="bool",
    description="是否捕获所有显示器（默认 True，部分平台仅支持主屏幕）。",
    default=True,
)
_config.group("截图设置").add(
    "focus",
    type="str",
    description="截图关注目标说明（如前台窗口、报错对话框等）。",
    default="",
)

TOOL_META = {
    "name": "desktop_screenshot",
    "description": (
        "【适用场景】开发者请求查看计算机桌面、当前应用窗口、系统报错或前台状态时使用。\n"
        "【使用方法】all_screens 指定是否多屏捕获（默认 True）；focus 传入关注目标说明。\n"
        "【注意事项】仅限开发者权限调用；返回本地截图路径供视觉分析，不直接公开发送原始图片。"
    ),
    "version": "1.1.0",
    "side_effect": "read_only",
    "tags": ["system", "image"],
    "developer_only": True,
    "dependencies": ["Pillow"],
    "parameters": _config.build(),
}


def run(
    all_screens: bool = True,
    focus: str = "",
    data_store: Any = None,
    invocation_context: ToolInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    ensure_developer_access(
        tool_name="desktop_screenshot",
        invocation_context=invocation_context,
    )

    image = _capture_desktop_image(all_screens=all_screens)
    output_path = _save_capture(image=image, data_store=data_store)
    captured_at = datetime.now(timezone.utc).isoformat()
    analysis_focus = _normalize_focus(focus)

    if data_store is not None:
        history = data_store.get("captures", [])
        history.append(
            {
                "captured_at": captured_at,
                "path": str(output_path),
                "analysis_focus": analysis_focus,
                "all_screens": bool(all_screens),
                "caller_user_id": invocation_context.caller_user_id if invocation_context else "",
            }
        )
        data_store.set("captures", history[-10:])

    return {
        "text_blocks": [
            {
                "type": "text",
                "label": "summary",
                "value": "已捕获当前主机桌面截图。这张图像适合判断主机当前在做什么，以及前台窗口、应用、页面或编辑器/终端状态。",
            },
            {
                "type": "text",
                "label": "analysis_hint",
                "value": (
                    f"分析重点：{analysis_focus}。"
                    "优先观察前台窗口、可见应用、页面标题、编辑器或终端内容；"
                    "如果截图不足以确认后台任务或隐含动作，请明确说明不确定，不要臆测。"
                ),
            },
            {
                "type": "text",
                "label": "artifact_path",
                "value": f"截图已保存到本地路径：{output_path}",
            },
        ],
        "multimodal_blocks": [
            {
                "type": "image",
                "label": "desktop_screenshot",
                "value": str(output_path),
                "mime_type": "image/png",
            }
        ],
        "internal_metadata": {
            "captured_at": captured_at,
            "artifact_path": str(output_path),
            "analysis_focus": analysis_focus,
            "all_screens": bool(all_screens),
            "caller_user_id": invocation_context.caller_user_id if invocation_context else "",
            "caller_name": invocation_context.caller_name if invocation_context else "",
        },
    }


def _normalize_focus(focus: str) -> str:
    value = str(focus or "").strip()
    if value:
        return value
    return "判断主机当前在做什么，以及前台窗口、应用、页面和任务状态"


def _capture_desktop_image(*, all_screens: bool) -> Any:
    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise RuntimeError("桌面截图需要 Pillow，请启用 auto_install_tool_deps 或手动安装 Pillow。") from exc

    try:
        return ImageGrab.grab(all_screens=all_screens)
    except TypeError:
        return ImageGrab.grab()
    except Exception as exc:
        raise RuntimeError(f"桌面截图失败: {exc}") from exc


def _save_capture(*, image: Any, data_store: Any) -> Path:
    output_dir = _resolve_output_dir(data_store)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_path = output_dir / f"desktop_screenshot_{timestamp}.png"
    image.save(output_path, format="PNG")
    return output_path


def _resolve_output_dir(data_store: Any) -> Path:
    artifact_dir = getattr(data_store, "artifact_dir", None)
    if isinstance(artifact_dir, Path):
        return artifact_dir
    if artifact_dir:
        return Path(str(artifact_dir))
    return Path(tempfile.gettempdir()) / "sirius_pulse" / "tool_artifacts" / "desktop_screenshot"
