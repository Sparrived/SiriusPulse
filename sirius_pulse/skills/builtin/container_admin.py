"""Developer-only access to an allowlisted host container proxy."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.skills.models import SkillInvocationContext
from sirius_pulse.skills.security import ensure_developer_access

_SOCKET_PATH = "/run/sirius-container-admin.sock"
_REQUEST_TIMEOUT = 15.0
_MAX_RESPONSE_BYTES = 50_000
_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ACTIONS = {"list", "inspect", "logs", "start", "stop", "restart"}
_LOGGER = logging.getLogger(__name__)

_config = ConfigBuilder()
_config.group("容器管理").add(
    "action",
    type="str",
    description="要执行的操作：list、inspect、logs、start、stop 或 restart。",
    required=True,
    choices=["list", "inspect", "logs", "start", "stop", "restart"],
)
_config.group("容器管理").add(
    "container",
    type="str",
    description="目标容器名称。list 操作不需要此参数。",
    default="",
)
_config.group("容器管理").add(
    "tail_lines",
    type="int",
    description="logs 操作返回的末尾行数，范围 1 到 200。",
    default=100,
)

SKILL_META = {
    "name": "container_admin",
    "description": (
        "管理宿主机上已由管理员允许的容器。可查看状态、检查详情、读取日志，并在宿主机策略允许时"
        "启动、停止或重启；inspect 会保留排障状态并在当前 QQ 会话发送容器状态卡片，"
        "不得用于执行任意 Docker 命令。"
    ),
    "version": "1.2.0",
    "side_effect": "external_write",
    "developer_only": True,
    "tags": ["docker", "container", "system", "admin"],
    "adapter_types": ["napcat"],
    "dependencies": ["playwright"],
    "parameters": _config.build(),
}


async def run(
    action: str,
    container: str = "",
    tail_lines: int = 100,
    data_store: Any = None,
    invocation_context: SkillInvocationContext | None = None,
    chat_context: dict[str, Any] | None = None,
    engine_context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Send one allowlisted container operation to the host proxy."""
    ensure_developer_access(skill_name="container_admin", invocation_context=invocation_context)
    if data_store is not None and data_store.get("_enabled", True) is False:
        return {"success": False, "error": "container_admin Skill 已被当前人格禁用"}

    try:
        request = _build_request(action=action, container=container, tail_lines=tail_lines)
        response = await asyncio.to_thread(_request_host_proxy, request)
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return {"success": False, "error": f"容器管理代理不可用: {exc}"}

    if not response.get("success"):
        return {
            "success": False,
            "error": str(response.get("error") or "容器管理操作失败"),
            "internal_metadata": {"action": request["action"], "container": request["container"]},
        }

    if request["action"] == "inspect":
        return await _inspect_and_send_card(
            diagnostics=str(response.get("output") or "").strip(),
            status=response.get("status"),
            data_store=data_store,
            chat_context=chat_context,
            engine_context=engine_context,
            invocation_context=invocation_context,
        )

    output = str(response.get("output") or "").strip()
    if not output and response.get("containers"):
        output = "\n".join(
            f"{item['name']}: {item['status']} ({item['image']})" for item in response["containers"]
        )
    return {
        "success": True,
        "summary": f"容器 {request['action']} 操作完成",
        "text_blocks": [output or "操作完成，但没有返回内容。"],
        "internal_metadata": {
            "action": request["action"],
            "container": request["container"],
            "containers": response.get("containers", []),
            "caller_user_id": invocation_context.caller_user_id,
        },
    }


def _build_request(*, action: str, container: str, tail_lines: int) -> dict[str, Any]:
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in _ACTIONS:
        raise ValueError("action 必须是 list、inspect、logs、start、stop 或 restart")

    target = str(container or "").strip()
    if normalized_action == "list":
        if target:
            raise ValueError("list 操作不能指定 container")
    elif not _CONTAINER_NAME.fullmatch(target):
        raise ValueError("container 必须是有效的容器名称")

    try:
        requested_lines = int(tail_lines)
    except (TypeError, ValueError) as exc:
        raise ValueError("tail_lines 必须是整数") from exc
    return {
        "action": normalized_action,
        "container": target,
        "tail_lines": max(1, min(200, requested_lines)),
    }


def _request_host_proxy(request: dict[str, Any]) -> dict[str, Any]:
    socket_path = os.environ.get("SIRIUS_CONTAINER_ADMIN_SOCKET", _SOCKET_PATH)
    encoded = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(_REQUEST_TIMEOUT)
        client.connect(socket_path)
        client.sendall(encoded)
        response = bytearray()
        while len(response) <= _MAX_RESPONSE_BYTES:
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if b"\n" in chunk:
                break

    if len(response) > _MAX_RESPONSE_BYTES:
        raise ValueError("容器管理代理响应过长")
    return json.loads(response.decode("utf-8"))


async def _inspect_and_send_card(
    *,
    diagnostics: str,
    status: Any,
    data_store: Any,
    chat_context: dict[str, Any] | None,
    engine_context: Any,
    invocation_context: SkillInvocationContext,
) -> dict[str, Any]:
    normalized = _normalize_status(status)
    if normalized is None:
        return {
            "success": True,
            "summary": "容器排障状态已获取，但状态卡片未生成",
            "text_blocks": [diagnostics or "容器状态为空"],
            "internal_metadata": {"card_sent": False, "card_error": "容器管理代理未返回有效状态"},
        }

    card_path = ""
    card_error = ""
    message_id = None
    if _chat_target(chat_context)[1]:
        try:
            image_path = await _render_status_card(normalized, data_store)
            card_path = str(image_path)
            sent = await _send_status_card(
                image_path=image_path,
                engine_context=engine_context,
                invocation_context=invocation_context,
            )
            if sent.success:
                message_id = sent.internal_metadata.get("message_id")
            else:
                card_error = sent.error
        except Exception as exc:
            _LOGGER.warning("container_admin: 状态卡片发送失败", exc_info=True)
            card_error = str(exc)
    else:
        card_error = "当前调用没有 QQ 会话上下文"

    card_sent = not card_error
    return {
        "success": True,
        "summary": (
            f"已检查 {normalized['name']} 并发送状态卡片"
            if card_sent
            else f"已检查 {normalized['name']}，但状态卡片未发送"
        ),
        "text_blocks": [diagnostics or "容器状态为空", _status_summary(normalized)],
        "internal_metadata": {
            "status": normalized,
            "diagnostics": diagnostics,
            "card_path": card_path,
            "card_sent": card_sent,
            "card_error": card_error,
            "message_id": message_id,
            "chat_type": _chat_target(chat_context)[0],
            "chat_id": _chat_target(chat_context)[1],
            "caller_user_id": invocation_context.caller_user_id,
        },
    }


async def _send_status_card(
    *,
    image_path: Path,
    engine_context: Any,
    invocation_context: SkillInvocationContext,
) -> Any:
    registry = getattr(engine_context, "skill_registry", None)
    executor = getattr(engine_context, "skill_executor", None)
    if registry is None or executor is None:
        from sirius_pulse.skills.models import SkillResult

        return SkillResult(success=False, error="Skill 运行上下文未就绪，无法发送状态卡片")
    file_upload = registry.get("file_upload")
    if file_upload is None:
        from sirius_pulse.skills.models import SkillResult

        return SkillResult(success=False, error="未找到 file_upload Skill，无法发送状态卡片")
    return await executor.execute_async(
        file_upload,
        {"action": "image", "image_path": str(image_path)},
        invocation_context=invocation_context,
    )


def _normalize_status(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    keys = {
        "name",
        "image",
        "status",
        "running",
        "exit_code",
        "started_at",
        "finished_at",
        "health",
        "restart_policy",
    }
    if not keys.issubset(value):
        return None
    return {
        **{key: str(value[key] or "").strip() for key in keys},
        "resources": _normalize_metrics(value.get("resources"), _resource_defaults()),
        "host": _normalize_metrics(value.get("host"), _host_defaults()),
    }


async def _render_status_card(status: dict[str, Any], data_store: Any) -> Path:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "container_admin 需要 Playwright；请重新部署包含 Chromium 的镜像"
        ) from exc

    output_dir = _artifact_dir(data_store)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"container_status_{timestamp}.png"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(
                viewport={"width": 800, "height": 800}, device_scale_factor=1
            )
            await page.set_content(_build_status_card_html(status), wait_until="load")
            await page.locator("#container-status-card").screenshot(path=str(output_path))
        finally:
            await browser.close()
    return output_path


def _artifact_dir(data_store: Any) -> Path:
    artifact_dir = getattr(data_store, "artifact_dir", None)
    if isinstance(artifact_dir, Path):
        return artifact_dir
    if artifact_dir:
        return Path(str(artifact_dir))
    return Path(tempfile.gettempdir()) / "sirius_pulse" / "skill_artifacts" / "container_admin"


def _build_status_card_html(status: dict[str, Any]) -> str:
    status_key = status["status"].lower()
    tone = (
        "healthy"
        if status_key == "running"
        else "warning" if status_key == "paused" else "critical"
    )
    labels = {
        "running": "运行中",
        "paused": "已暂停",
        "restarting": "重启中",
        "exited": "已退出",
        "dead": "异常中",
    }
    state_label = labels.get(status_key, status["status"].upper() or "未知")
    started_at = _format_timestamp(status["started_at"])
    status_fields = [
        ("运行时长", _format_uptime(status["started_at"]) if status_key == "running" else "-"),
        ("健康检查", status["health"] or "未上报"),
        ("重启策略", status["restart_policy"] or "未设置"),
        ("退出码", status["exit_code"] or "-"),
    ]
    resource_fields = [
        ("CPU", status["resources"]["cpu_percent"]),
        ("内存", status["resources"]["memory_usage"]),
        ("内存占比", status["resources"]["memory_percent"]),
        ("网络 I/O", status["resources"]["network_io"]),
        ("块 I/O", status["resources"]["block_io"]),
        ("PID", status["resources"]["pids"]),
    ]
    host_fields = [
        ("主机 CPU", status["host"]["cpu_percent"]),
        ("主机内存", status["host"]["memory_usage"]),
        ("根磁盘", status["host"]["disk_usage"]),
        ("负载 (1m)", status["host"]["load_1"]),
        ("主机运行时长", status["host"]["uptime"]),
    ]

    def field_html(fields: list[tuple[str, str]]) -> str:
        return "".join(
            f'<div class="field"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
            for label, value in fields
        )

    return f"""<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #111315; color: #f3f5f6; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }}
#container-status-card {{ width: 800px; display: grid; grid-template-columns: 12px 1fr; border: 1px solid #384046; border-radius: 8px; overflow: hidden; background: #1a1e21; }}
.signal-rail {{ background: #37d99e; }}
.signal-rail.warning {{ background: #ffc857; }}
.signal-rail.critical {{ background: #ff6b6b; }}
.content {{ padding: 30px 32px 28px; }}
.eyebrow {{ color: #aeb7bd; font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; font-weight: 700; letter-spacing: 0; }}
.topline {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; margin: 10px 0 26px; }}
h1 {{ margin: 0; color: #ffffff; font-family: "Cascadia Mono", Consolas, monospace; font-size: 30px; line-height: 1.2; letter-spacing: 0; overflow-wrap: anywhere; }}
.state {{ display: inline-flex; align-items: center; gap: 8px; color: #d8e0e3; font-family: "Cascadia Mono", Consolas, monospace; font-size: 13px; white-space: nowrap; }}
.state i {{ width: 10px; height: 10px; border-radius: 50%; background: #37d99e; display: block; }}
.state.warning i {{ background: #ffc857; }} .state.critical i {{ background: #ff6b6b; }}
.image {{ border-top: 1px solid #384046; border-bottom: 1px solid #384046; color: #d6dde0; font-family: "Cascadia Mono", Consolas, monospace; font-size: 14px; line-height: 1.5; padding: 16px 0; overflow-wrap: anywhere; }}
.section {{ margin-top: 22px; }}
.section-title {{ color: #aeb7bd; font-family: "Cascadia Mono", Consolas, monospace; font-size: 11px; font-weight: 700; letter-spacing: 0; margin-bottom: 4px; }}
.grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); column-gap: 24px; }}
.field {{ min-height: 54px; border-bottom: 1px solid #2e3438; padding: 11px 0; }}
.field span {{ color: #98a4ab; display: block; font-size: 12px; }}
.field strong {{ color: #f3f5f6; display: block; font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; font-weight: 600; line-height: 1.45; margin-top: 4px; overflow-wrap: anywhere; }}
.footer {{ color: #7f8b91; font-family: "Cascadia Mono", Consolas, monospace; font-size: 11px; margin-top: 18px; }}
</style></head><body><article id=\"container-status-card\"><div class=\"signal-rail {tone}\"></div><div class=\"content\">
<div class=\"eyebrow\">CONTAINER / LIVE STATUS</div><div class=\"topline\"><h1>{html.escape(status['name'])}</h1><div class=\"state {tone}\"><i></i>{html.escape(state_label)}</div></div>
<div class=\"image\">{html.escape(status['image'] or '镜像未报告')}</div><section class=\"section\"><div class=\"section-title\">STATUS</div><div class=\"grid\">{field_html(status_fields)}</div></section><section class=\"section\"><div class=\"section-title\">CONTAINER RESOURCE</div><div class=\"grid\">{field_html(resource_fields)}</div></section><section class=\"section\"><div class=\"section-title\">HOST SNAPSHOT</div><div class=\"grid\">{field_html(host_fields)}</div></section><div class=\"footer\">STARTED {html.escape(started_at)} · SIRIUS CONTAINER ADMIN</div>
</div></article></body></html>"""


def _format_timestamp(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("0001-"):
        return "未运行"
    return text.replace("T", " ").split(".", 1)[0].replace("Z", " UTC")


def _format_uptime(value: str) -> str:
    try:
        started = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "未知"
    seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes = seconds // 60
    if days:
        return f"{days}天 {hours}小时"
    if hours:
        return f"{hours}小时 {minutes}分"
    return f"{minutes}分"


def _status_summary(status: dict[str, Any]) -> str:
    health = status["health"] or "未上报"
    return f"{status['name']}：{status['status'] or 'unknown'}，健康检查 {health}"


def _chat_target(chat_context: dict[str, Any] | None) -> tuple[str, str]:
    context = chat_context or {}
    chat_type = str(context.get("chat_type") or "").strip()
    if chat_type not in {"group", "private"}:
        return "", ""
    return chat_type, str(context.get("chat_id") or context.get("group_id") or "").strip()


def _normalize_metrics(value: Any, defaults: dict[str, str]) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    return {key: str(source.get(key) or default) for key, default in defaults.items()}


def _resource_defaults() -> dict[str, str]:
    return {
        "cpu_percent": "未上报",
        "memory_usage": "未上报",
        "memory_percent": "未上报",
        "network_io": "未上报",
        "block_io": "未上报",
        "pids": "未上报",
    }


def _host_defaults() -> dict[str, str]:
    return {
        "cpu_percent": "未上报",
        "memory_usage": "未上报",
        "disk_usage": "未上报",
        "load_1": "未上报",
        "uptime": "未上报",
    }
