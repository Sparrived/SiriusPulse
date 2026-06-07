"""JWT 认证管理模块 — 纯标准库实现。

提供 HMAC-SHA256 签名的 JWT 令牌签发与验证，支持 admin/viewer 两种角色。
首次启动时自动生成管理员密码并持久化到 data/auth_secret.json。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Any

LOG = logging.getLogger("sirius.webui.auth")

# auth_secret.json 文件名
_AUTH_SECRET_FILE = "auth_secret.json"


def _b64_encode(data: bytes) -> str:
    """URL-safe Base64 编码，去除尾部 '=' 填充。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64_decode(s: str) -> bytes:
    """URL-safe Base64 解码，自动补齐 '=' 填充。"""
    # 补齐 padding
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def _hash_password(password: str, salt: str) -> str:
    """SHA-256 加盐哈希密码。"""
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


class AuthManager:
    """JWT 认证管理器。"""

    def __init__(self, data_path: Path) -> None:
        """初始化，data_path 是 data/ 目录路径。"""
        self._data_path = data_path
        self._secret_file = data_path / _AUTH_SECRET_FILE
        self._config: dict[str, Any] = {}
        self._load_or_create()

    def _load_or_create(self) -> None:
        """加载或初始化 auth_secret.json 配置。"""
        if self._secret_file.exists():
            try:
                with open(self._secret_file, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
                LOG.info("已加载认证配置: %s", self._secret_file)
            except (json.JSONDecodeError, OSError) as exc:
                LOG.warning("加载认证配置失败，将重新生成: %s", exc)
                self._config = {}
        else:
            LOG.info("认证配置文件不存在，将首次生成")

    def _save_config(self) -> None:
        """持久化认证配置到文件。"""
        self._data_path.mkdir(parents=True, exist_ok=True)
        with open(self._secret_file, "w", encoding="utf-8") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)
        LOG.info("认证配置已保存: %s", self._secret_file)

    def get_or_create_secret(self) -> str:
        """获取或首次生成 JWT 密钥。"""
        secret = self._config.get("jwt_secret")
        if secret:
            return str(secret)
        # 首次生成 32 字节随机密钥
        secret = secrets.token_hex(32)
        self._config["jwt_secret"] = secret
        self._save_config()
        LOG.info("已生成新的 JWT 密钥")
        return secret

    def get_or_create_admin_password(self) -> str:
        """获取或首次生成管理员密码。

        首次启动时生成随机密码，打印到控制台并保存哈希。
        返回明文密码（仅首次生成时可用，后续调用返回空字符串）。
        """
        if self._config.get("admin_password_hash"):
            LOG.info("管理员密码已存在，跳过生成")
            return ""

        # 首次生成 16 字符随机密码
        password = secrets.token_urlsafe(12)[:16]
        salt = secrets.token_hex(16)
        password_hash = _hash_password(password, salt)

        self._config["admin_password_hash"] = password_hash
        self._config["admin_salt"] = salt
        self._save_config()

        # 打印到控制台（仅首次可见）
        LOG.warning("=" * 50)
        LOG.warning("首次启动，已自动生成管理员密码:")
        LOG.warning("  用户名: admin")
        LOG.warning("  密  码: %s", password)
        LOG.warning("请妥善保管，此密码仅显示一次！")
        LOG.warning("=" * 50)

        return password

    def create_token(self, username: str, role: str = "admin", expires_hours: int = 24) -> str:
        """签发 JWT 令牌。

        Args:
            username: 用户名
            role: 角色 ("admin" 或 "viewer")
            expires_hours: 过期时间（小时）

        Returns:
            JWT 令牌字符串
        """
        import time

        secret = self.get_or_create_secret()

        # 构造 header
        header = {"alg": "HS256", "typ": "JWT"}

        # 构造 payload
        now = int(time.time())
        payload = {
            "sub": username,
            "role": role,
            "iat": now,
            "exp": now + (expires_hours * 3600),
        }

        # Base64 编码
        header_b64 = _b64_encode(json.dumps(header).encode("utf-8"))
        payload_b64 = _b64_encode(json.dumps(payload).encode("utf-8"))

        # HMAC-SHA256 签名
        message = f"{header_b64}.{payload_b64}".encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
        signature_b64 = _b64_encode(signature)

        token = f"{header_b64}.{payload_b64}.{signature_b64}"
        LOG.debug("已签发令牌: user=%s, role=%s, expires=%dh", username, role, expires_hours)
        return token

    def verify_token(self, token: str) -> dict[str, Any] | None:
        """验证 JWT 令牌，返回 payload 或 None。

        验证内容：
        1. 签名有效性
        2. 过期时间

        Args:
            token: JWT 令牌字符串

        Returns:
            payload 字典 或 None（验证失败）
        """
        import time

        try:
            parts = token.split(".")
            if len(parts) != 3:
                LOG.debug("令牌格式无效: 应有 3 段，实际 %d 段", len(parts))
                return None

            header_b64, payload_b64, signature_b64 = parts

            # 验证签名
            secret = self.get_or_create_secret()
            message = f"{header_b64}.{payload_b64}".encode("utf-8")
            expected_sig = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
            actual_sig = _b64_decode(signature_b64)

            if not hmac.compare_digest(expected_sig, actual_sig):
                LOG.debug("令牌签名验证失败")
                return None

            # 解析 payload
            payload_bytes = _b64_decode(payload_b64)
            payload: dict[str, Any] = json.loads(payload_bytes)

            # 验证过期时间
            exp = payload.get("exp", 0)
            if int(time.time()) > exp:
                LOG.debug("令牌已过期: exp=%d", exp)
                return None

            LOG.debug("令牌验证成功: user=%s, role=%s", payload.get("sub"), payload.get("role"))
            return payload

        except Exception as exc:
            LOG.debug("令牌验证异常: %s", exc)
            return None

    def authenticate(self, username: str, password: str) -> str | None:
        """验证用户名密码，成功返回 token，失败返回 None。

        当前仅支持 "admin" 用户。

        Args:
            username: 用户名
            password: 密码

        Returns:
            JWT 令牌字符串 或 None（验证失败）
        """
        # 仅支持 admin 用户
        if username != "admin":
            LOG.debug("认证失败: 不支持的用户 '%s'", username)
            return None

        # 确保密码已初始化
        stored_hash = self._config.get("admin_password_hash")
        stored_salt = self._config.get("admin_salt")

        if not stored_hash or not stored_salt:
            LOG.warning("认证失败: 管理员密码未初始化")
            return None

        # 验证密码
        input_hash = _hash_password(password, stored_salt)
        if not hmac.compare_digest(input_hash, stored_hash):
            LOG.debug("认证失败: 密码错误")
            return None

        LOG.info("认证成功: user=%s", username)
        return self.create_token(username, role="admin")
