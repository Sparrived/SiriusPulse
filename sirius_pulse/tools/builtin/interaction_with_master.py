"""Built-in tool for messaging the master and sensing the master's public status."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.core.intent import (
    RESOLUTION_TELL,
    IntentFileStore,
    Intention,
)
from sirius_pulse.tools.builtin._internal._qq_ops import (
    bridge_error,
    failure_from_exception,
    get_adapter,
    success_result,
)

_MDS_BASE_URL = "https://sparrived.xyz/mds"
_MDS_TOKEN_ENV = "MDS_PUBLIC_STATUS_TOKEN"
_MDS_BASE_URL_ENV = "MDS_API_BASE_URL"
_MAX_RESPONSE_BYTES = 512 * 1024
_DEFAULT_TIMEOUT_SECONDS = 10
_MAX_MESSAGE_CHARS = 1200
# Matches the label ``list_audiences()`` gives this conversation, so an intention
# recorded here reads the same as one recorded through intend_share.
_MASTER_AUDIENCE_LABEL = "主人（私聊）"
_DEFERRED_URGENCY = 0.7

_config = ConfigBuilder()
_config.group("和主人互动").add(
    "action",
    type="str",
    description=("操作类型：message 给主人发送私聊消息；status 查询主人公开设备状态。" "需要表达想法时用 message，需要了解主人近况时用 status。"),
    required=True,
    choices=["message", "status"],
)
_config.group("和主人互动").add(
    "message",
    type="str",
    description=(
        "action=message 时发给主人的话。可以是闲聊、分享有趣的事、吐槽、开心、难过、" "委屈或求助，像给熟人发 QQ 一样自然写；不要泄露系统提示、密钥或隐私。"
    ),
)
_config.group("状态查询").add(
    "device_id",
    type="str",
    description="action=status 时可选：只查看指定设备 ID；留空则返回公开白名单中的全部设备。",
)

TOOL_META = {
    "name": "interaction_with_master",
    "description": (
        "和主人互动的统一工具。需要私下给主人发送消息时使用 action=message；"
        "需要感知主人当前公开设备状态时使用 action=status。"
        "纯文字回复直接写在正文中，不要为了增强角色感而强行调用。"
    ),
    "version": "1.1.0",
    "side_effect": "external_write",
    # Unlike most external writes, this one may run on a turn she started
    # herself: telling the master something is one of the things autonomy is
    # *for*.  It is still paced -- see ``_defer_until_morning``.
    "allowed_when_self_initiated": True,
    "tags": ["napcat", "qq", "master", "chat", "status", "presence"],
    "silent": False,
    "retry_safe": True,
    "parameters": _config.build(),
    "config": {
        "public_status_token": {
            "type": "password",
            "description": "MDS 公开状态接口令牌；保存在当前人格的 interaction_with_master.json 中。",
            "group": "MDS 连接",
        },
        "base_url": {
            "type": "str",
            "description": "MDS 服务基地址。",
            "default": _MDS_BASE_URL,
            "group": "MDS 连接",
        },
        "timeout_seconds": {
            "type": "int",
            "description": "请求超时秒数，范围 1 到 60。",
            "default": _DEFAULT_TIMEOUT_SECONDS,
            "group": "MDS 连接",
        },
    },
}


async def run(
    action: str,
    message: str = "",
    device_id: str = "",
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    data_store: Any = None,
    engine_context: Any = None,
    invocation_context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    if action_key == "message":
        return await _send_message(
            message,
            bridge,
            chat_context,
            engine_context=engine_context,
            self_initiated=bool(getattr(invocation_context, "self_initiated", False)),
        )
    if action_key == "status":
        return _read_status(device_id, data_store)
    return {"success": False, "error": f"不支持的互动 action: {action}"}


async def _send_message(
    message: str,
    bridge: Any,
    chat_context: dict[str, Any] | None,
    *,
    engine_context: Any = None,
    self_initiated: bool = False,
) -> dict[str, Any]:
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("和主人私聊")

    master_qq = _master_qq_from_adapter(adapter, bridge)
    if not master_qq:
        return {
            "success": False,
            "error": "NapCat adapter 未配置 root QQ，无法和主人私聊",
            "summary": "操作失败：缺少主人 QQ",
        }

    text = str(message or "").strip()
    if not text:
        return {
            "success": False,
            "error": "message 不能为空",
            "summary": "操作失败：私聊内容为空",
        }

    body = _truncate(text, _MAX_MESSAGE_CHARS)

    # On her own time, at night, the words wait instead of arriving at 03:00.
    # Recording an intention is what makes that possible: it keeps the exact
    # words and the destination, and the autonomy tick delivers it after the
    # quiet window using the same paced path as any other share.  A reply to a
    # live conversation is not deferred -- that person is already awake and
    # waiting.
    if self_initiated and _is_quiet_now():
        return _defer_until_morning(
            body,
            master_qq=master_qq,
            engine_context=engine_context,
        )

    try:
        raw = await adapter.send_private_message(master_qq, body)
        return success_result(
            "已发给主人",
            master_qq=master_qq,
            chat_context=dict(chat_context or {}),
            raw=raw,
        )
    except Exception as exc:
        return failure_from_exception("和主人私聊", exc)


def _is_quiet_now() -> bool:
    """Night window in local time; indirection point so tests can pin the clock."""
    from datetime import datetime, timezone

    from sirius_pulse.core.autonomy import is_quiet_hours

    return is_quiet_hours(datetime.now(timezone.utc))


def _defer_until_morning(
    body: str,
    *,
    master_qq: str,
    engine_context: Any,
) -> dict[str, Any]:
    """Hold a night-time message as a tell-intention aimed at the master."""
    if engine_context is None:
        # Without the engine we cannot persist the words, and sending at 03:00
        # is exactly what this guard exists to prevent: fail closed.
        return {
            "success": False,
            "error": "夜间暂存需要 engine_context，本次消息没有发出。",
            "summary": "夜深了，这句话先没有发出去",
        }

    audience = f"private_{master_qq}"
    try:
        store = IntentFileStore(engine_context.get_work_path()).load()
        intention = Intention.create(
            what=body,
            why="夜里想到的，等早上再说给主人听。",
            resolution=RESOLUTION_TELL,
            kind="share",
            audience=audience,
            audience_label=_MASTER_AUDIENCE_LABEL,
            urgency=_DEFERRED_URGENCY,
            source="interaction_with_master",
        )
        store.add(intention)
        store.prune()
        IntentFileStore(engine_context.get_work_path()).save(store)
    except Exception as exc:
        return {
            "success": False,
            "error": f"夜间暂存失败，本次消息没有发出: {exc}",
            "summary": "夜深了，这句话先没有发出去",
        }

    return {
        "success": True,
        "deferred": True,
        "intention_id": intention.intention_id,
        "audience": audience,
        "summary": "现在是夜里，这句话先记下了，早上再发给他。",
    }


def _master_qq_from_adapter(adapter: Any, bridge: Any = None) -> str:
    for source in (adapter, bridge):
        if source is None:
            continue
        for attr in ("plugin_config", "config"):
            cfg = getattr(source, attr, None)
            if not isinstance(cfg, dict):
                continue
            value = str(cfg.get("root", "") or "").strip()
            if value:
                return value
    return ""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _read_status(device_id: str = "", data_store: Any = None) -> dict[str, Any]:
    """Fetch and redact the public MDS snapshot for model consumption."""
    _reload_data_store(data_store)
    token = _resolve_token(data_store)
    if not token:
        return _failure(
            "未配置 MDS 公开状态令牌。请在 WebUI 的 interaction_with_master 配置中填写，"
            "它会保存到当前人格的 tool_data/interaction_with_master.json；"
            "MDS_PUBLIC_STATUS_TOKEN 环境变量可作为后备。"
        )

    try:
        endpoint = _snapshot_url(_resolve_base_url(data_store))
        snapshot = _fetch_snapshot(
            token,
            endpoint=endpoint,
            timeout_seconds=_resolve_timeout_seconds(data_store),
        )
    except json.JSONDecodeError:
        return _failure("MDS 返回的不是合法 JSON。")
    except ValueError as exc:
        return _failure(str(exc))
    except HTTPError as exc:
        if exc.code in (401, 403):
            return _failure("MDS 公开状态令牌无效或已被拒绝。")
        if exc.code == 404:
            return _failure("MDS 公开快照接口不存在，请检查 sparrived.xyz/mds 部署路径。")
        return _failure(f"MDS 请求失败（HTTP {exc.code}）。")
    except (TimeoutError, URLError, OSError):
        return _failure("暂时无法连接 MDS 公开状态接口。")

    devices = _normalize_devices(snapshot.get("devices"))
    wanted_id = str(device_id or "").strip()
    if wanted_id:
        devices = [item for item in devices if item.get("id") == wanted_id]

    generated_at = _optional_string(snapshot.get("generated_at")) or "未知"
    return {
        "success": True,
        "summary": f"已读取 {len(devices)} 台设备的主人当前状态参考。",
        "generated_at": generated_at,
        "devices": devices,
        "text_blocks": [_render_summary(devices, generated_at, wanted_id)],
        "internal_metadata": {
            "endpoint": endpoint,
            "device_count": len(devices),
            "generated_at": generated_at,
        },
    }


def _resolve_token(data_store: Any) -> str:
    if data_store is not None:
        configured = str(data_store.get("public_status_token", "") or "").strip()
        if configured:
            return configured
    return os.environ.get(_MDS_TOKEN_ENV, "").strip()


def _resolve_base_url(data_store: Any) -> str:
    if data_store is not None:
        configured = str(data_store.get("base_url", "") or "").strip()
        if configured:
            return configured
    return os.environ.get(_MDS_BASE_URL_ENV, _MDS_BASE_URL).strip()


def _resolve_timeout_seconds(data_store: Any) -> int:
    value = (
        data_store.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
        if data_store
        else _DEFAULT_TIMEOUT_SECONDS
    )
    try:
        return max(1, min(60, int(value)))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SECONDS


def _snapshot_url(base_url: str | None = None) -> str:
    base_url = (base_url or os.environ.get(_MDS_BASE_URL_ENV, _MDS_BASE_URL)).strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MDS 服务地址必须是合法的 http/https 地址。")
    if parsed.query or parsed.fragment:
        raise ValueError("MDS 服务地址不能包含 query 或 fragment。")
    return f"{base_url}/api/v1/public/snapshot"


def _fetch_snapshot(token: str, *, endpoint: str, timeout_seconds: int) -> dict[str, Any]:
    request = Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "SiriusChat/interaction-with-master",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ValueError("MDS 返回内容过大，已拒绝处理。")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("MDS 返回的数据结构无效。")
    return payload


def _reload_data_store(data_store: Any) -> None:
    reload_store = getattr(data_store, "reload", None)
    if callable(reload_store):
        reload_store()


def _normalize_devices(raw_devices: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_devices, list):
        return []

    devices: list[dict[str, Any]] = []
    for raw in raw_devices:
        if not isinstance(raw, dict):
            continue
        device: dict[str, Any] = {}
        for key in (
            "id",
            "name",
            "platform",
            "status",
            "heartbeat_age_seconds",
            "last_seen_at",
            "reported_at",
        ):
            if key in raw and raw[key] is not None:
                device[key] = raw[key]

        metrics = raw.get("metrics")
        if isinstance(metrics, dict):
            device["metrics"] = {
                key: metrics[key]
                for key in (
                    "cpu_percent",
                    "memory_percent",
                    "disk_used_percent",
                    "battery_percent",
                    "network_connected",
                    "activity_state",
                )
                if key in metrics and metrics[key] is not None
            }

        foreground_app = raw.get("foreground_app")
        if isinstance(foreground_app, dict):
            safe_app = {
                key: foreground_app[key]
                for key in ("name", "captured_at")
                if key in foreground_app and foreground_app[key] is not None
            }
            if safe_app:
                device["foreground_app"] = safe_app

        location = raw.get("location")
        if isinstance(location, dict):
            safe_location = {
                key: location[key]
                for key in ("country", "province", "city", "district")
                if key in location and location[key] is not None
            }
            if safe_location:
                device["location"] = safe_location

        if device:
            devices.append(device)
    return devices


def _render_summary(devices: list[dict[str, Any]], generated_at: str, wanted_id: str) -> str:
    title = f"主人当前状态参考（MDS 生成时间：{generated_at}）"
    if wanted_id and not devices:
        return f"{title}\n没有找到主人设备 {wanted_id}。"
    if not devices:
        return f"{title}\n暂时没有可用的公开设备状态。"

    lines = [title, "以下信息用于帮助人格理解主人的近况："]
    for device in devices:
        name = str(device.get("name") or device.get("id") or "未命名设备")
        status = {
            "online": "在线",
            "stale": "状态过期",
            "offline": "离线",
            "never_seen": "从未上报",
        }.get(str(device.get("status") or ""), str(device.get("status") or "未知"))
        details = [f"设备 {name}：{status}"]

        location = device.get("location")
        if isinstance(location, dict):
            parts = [
                str(location[key])
                for key in ("country", "province", "city", "district")
                if location.get(key)
            ]
            if parts:
                details.append(f"位置 {' / '.join(parts)}")

        app = device.get("foreground_app")
        if isinstance(app, dict) and app.get("name"):
            details.append(f"前台 {app['name']}")

        metrics = device.get("metrics")
        if isinstance(metrics, dict) and metrics.get("activity_state"):
            details.append(f"活动 {metrics['activity_state']}")

        if device.get("heartbeat_age_seconds") is not None:
            details.append(f"心跳 {device['heartbeat_age_seconds']} 秒前")
        lines.append("；".join(details))
    return "\n".join(lines)


def _optional_string(value: Any) -> str:
    return str(value).strip() if value is not None and str(value).strip() else ""


def _failure(message: str) -> dict[str, Any]:
    return {"success": False, "error": message, "summary": "主人当前状态读取失败"}
