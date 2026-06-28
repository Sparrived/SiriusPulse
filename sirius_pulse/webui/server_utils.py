"""WebUI 共享工具函数 — 避免 server_core 与 server_skill_api 之间的循环导入。"""

from __future__ import annotations

import functools
import json
import logging
from typing import Any, Callable

from aiohttp import web

LOG = logging.getLogger("sirius.webui")


def _json_response(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(
        data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, indent=2)
    )


def _get_name(request: web.Request) -> str:
    """从 URL 路径参数获取人格名称。"""
    return str(request.match_info.get("name", "")).strip()


def handle_api_errors(func: Callable) -> Callable:
    """WebUI API 错误处理装饰器。

    自动捕获异常并返回统一的 JSON 错误响应，消除各 API 函数中
    重复的 try/except + _json_response({"error": str(exc)}, 500) 模式。
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> web.Response:
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            LOG.warning("%s 失败: %s", func.__name__, exc)
            return _json_response({"error": str(exc)}, 500)

    return wrapper
