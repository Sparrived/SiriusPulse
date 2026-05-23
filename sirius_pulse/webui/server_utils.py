"""WebUI 共享工具函数 — 避免 server_core 与 server_skill_api 之间的循环导入。"""

from __future__ import annotations

import json
from typing import Any

from aiohttp import web


def _json_response(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(
        data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, indent=2)
    )


def _get_name(request: web.Request) -> str:
    """从 URL 路径参数获取人格名称。"""
    return str(request.match_info.get("name", "")).strip()
