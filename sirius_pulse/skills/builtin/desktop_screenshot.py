"""Built-in skill for capturing the host desktop as an image artifact."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.skills.models import SkillInvocationContext
from sirius_pulse.skills.security import ensure_developer_access

_config = ConfigBuilder()
_config.group("截图设置").add(
    "all_screens",
    type="bool",
    description="是否尽量捕获所有显示器；部分平台可能只支持主屏幕",
    default=True,
)
_config.group("截图设置").add(
    "focus",
    type="str",
    description="本次截图关注点，例如 判断主机当前在做什么、确认前台窗口、查看当前页面",
    default="",
)

SKILL_META = {
    "name": "desktop_screenshot",
    "description": "开发者让你看看电脑屏幕、当前窗口、报错画面或“现在发生了什么”时使用；截图只供你分析后再在群里说明。",
    "version": "1.1.0",
    "tags": ["system", "image"],
    "developer_only": True,
    "dependencies": ["Pillow"],
    "parameters": _config.build(),
}


def run(
    all_screens: bool = True,
    focus: str = "",
    data_store: Any = None,
    invocation_context: SkillInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    ensure_developer_access(
        skill_name="desktop_screenshot",
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
        raise RuntimeError(
            "桌面截图需要 Pillow，请启用 auto_install_skill_deps 或手动安装 Pillow。"
        ) from exc

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
    return Path(tempfile.gettempdir()) / "sirius_pulse" / "skill_artifacts" / "desktop_screenshot"
