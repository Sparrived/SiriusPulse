"""WebUI 设置项的密钥识别与掩码约定 —— Plugin、Tool 与 MCP 配置 API 共用。

读取时非空密钥一律回显 ``SECRET_MASK``；提交时掩码（或留空）表示「保留已保存的值」。
"""

from __future__ import annotations

import re
from typing import Any

SECRET_MASK = "********"
MASKED_SECRET_VALUES = {"", SECRET_MASK, "[已隐藏]", "••••••••"}
SECRET_PARAMETER_TYPES = {"password", "secret"}

_SECRET_NAMES = {
    "password",
    "passwords",
    "secret",
    "secrets",
    "token",
    "tokens",
    "key",
    "keys",
    "api_key",
    "api_keys",
    "access_token",
    "refresh_token",
    "authorization",
    "auth",
    "authentication",
    "bearer",
    "client_secret",
    "credential",
    "credentials",
    "session",
    "session_id",
}
_SECRET_SUFFIXES = (
    "_token",
    "_tokens",
    "_key",
    "_keys",
    "_secret",
    "_secrets",
    "_password",
    "_passwords",
    "_credential",
    "_credentials",
    "_auth",
    "_session",
)


def normalize_setting_key(key: Any) -> str:
    text = str(key)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def is_secret_setting_name(key: Any) -> bool:
    """按设置项名称判断是否为密钥（如 ``api_key``、``githubToken``）。"""
    normalized = normalize_setting_key(key)
    return normalized in _SECRET_NAMES or normalized.endswith(_SECRET_SUFFIXES)


def is_masked_secret_value(value: Any) -> bool:
    """浏览器回传的掩码占位符或空串 —— 表示不修改已保存的密钥。"""
    return isinstance(value, str) and value.strip() in MASKED_SECRET_VALUES
