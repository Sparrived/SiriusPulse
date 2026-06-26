"""助手端远程存储桥接 — 连接管家端数据 API 的存储层。

RemoteStorageBridge 是助手端的核心组件，负责：
1. 启动时从管家端加载完整状态快照（load_snapshot）
2. 运行时将写操作路由到 WriteBuffer（异步推送）
3. 关闭时将完整状态推送到管家端（save_snapshot）

助手端的引擎通过此桥接层访问所有持久化数据，
本地不维护 SQLite/JSON 文件，管家端是唯一的真相源。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

LOG = logging.getLogger("sirius.remote_bridge")


class RemoteStorageBridge:
    """远程存储桥接。

    对引擎暴露两个核心方法：
    - load_snapshot() — 启动时加载，返回完整状态字典
    - save_snapshot(state) — 关闭时保存，推送完整状态

    运行时写操作通过 get_write_buffer() 获取 WriteBuffer，
    由引擎在各写入点调用 buffer.add() 或 buffer.add_critical()。
    """

    def __init__(
        self,
        butler_api_url: str,
        token: str | None = None,
        flush_interval: float = 30.0,
    ) -> None:
        from sirius_pulse.network.write_buffer import WriteBuffer

        self._api_url = butler_api_url.rstrip("/")
        self._token = token
        self._write_buffer = WriteBuffer(butler_api_url, token, flush_interval)
        self._snapshot: dict[str, Any] | None = None

    @property
    def write_buffer(self) -> "WriteBuffer":
        """获取写缓冲实例。"""
        return self._write_buffer

    @property
    def snapshot(self) -> dict[str, Any] | None:
        """获取已加载的快照数据。"""
        return self._snapshot

    # ------------------------------------------------------------------
    # 快照加载/保存
    # ------------------------------------------------------------------

    async def load_snapshot(self) -> dict[str, Any]:
        """从管家端加载完整运行时状态快照。

        Returns:
            包含所有运行时数据的字典，可直接用于初始化引擎组件。
        """
        import aiohttp

        url = f"{self._api_url}/api/data/snapshot"
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._snapshot = data.get("snapshot", {})
                        LOG.info("快照加载成功，数据项: %d", len(self._snapshot))
                        return self._snapshot
                    else:
                        body = await resp.text()
                        LOG.error("快照加载失败 (HTTP %d): %s", resp.status, body[:200])
                        return {}
        except Exception as exc:
            LOG.error("快照加载异常: %s", exc)
            return {}

    async def save_snapshot(self, state: dict[str, Any]) -> bool:
        """将完整运行时状态推送到管家端。

        Args:
            state: 由引擎序列化的完整状态字典。

        Returns:
            True if saved successfully.
        """
        import aiohttp

        url = f"{self._api_url}/api/data/snapshot"
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={"state": state},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        LOG.info("快照已保存到管家端")
                        return True
                    else:
                        body = await resp.text()
                        LOG.error("快照保存失败 (HTTP %d): %s", resp.status, body[:200])
                        return False
        except Exception as exc:
            LOG.error("快照保存异常: %s", exc)
            return False

    # ------------------------------------------------------------------
    # 实时推送（关键数据，非阻塞）
    # ------------------------------------------------------------------

    def push_message(self, group_id: str, entry: dict[str, Any]) -> None:
        """实时推送新消息到管家端归档。"""
        self._write_buffer.add_critical("message", {"group_id": group_id, **entry})

    def push_messages(self, messages: list[dict[str, Any]]) -> None:
        """批量推送消息。"""
        for msg in messages:
            gid = msg.get("group_id", "default")
            self._write_buffer.add_critical("message", msg)

    def push_user_update(self, user_id: str, data: dict[str, Any]) -> None:
        """实时推送用户画像变更。"""
        self._write_buffer.add_critical("user_update", {"user_id": user_id, **data})

    def push_glossary(self, terms: dict[str, Any]) -> None:
        """实时推送术语变更。"""
        self._write_buffer.add_critical("glossary", {"terms": terms})

    # ------------------------------------------------------------------
    # 批量推送（非关键数据，定期 flush）
    # ------------------------------------------------------------------

    def push_token_usage(self, data: dict[str, Any]) -> None:
        """添加 token 使用记录到批量缓冲。"""
        self._write_buffer.add("token_usage", data)

    def push_cognition_event(self, data: dict[str, Any]) -> None:
        """添加认知事件到批量缓冲。"""
        self._write_buffer.add("cognition_event", data)

    def push_semantic_profile(self, data: dict[str, Any]) -> None:
        """添加语义画像变更到批量缓冲。"""
        self._write_buffer.add("semantic_profile", data)

    def push_working_memory(self, group_id: str, entries: list[dict[str, Any]]) -> None:
        """添加工作记忆快照到批量缓冲。"""
        self._write_buffer.add("working_memory", {"group_id": group_id, "entries": entries})

    def push_timestamps(self, timestamps: dict[str, str]) -> None:
        """添加时间戳更新到批量缓冲。"""
        self._write_buffer.add("timestamps", timestamps)

    def push_assistant_emotion(self, state: dict[str, Any]) -> None:
        """添加助手情绪状态到批量缓冲。"""
        self._write_buffer.add("assistant_emotion", state)

    def push_diary_state(self, state: dict[str, Any]) -> None:
        """添加日记状态到批量缓冲。"""
        self._write_buffer.add("diary_state", state)

    def push_user_manager(self, state: dict[str, Any]) -> None:
        """添加用户管理器状态到批量缓冲。"""
        self._write_buffer.add("user_manager", state)

    def push_basic_memory(self, state: dict[str, Any]) -> None:
        """添加基础记忆配置到批量缓冲。"""
        self._write_buffer.add("basic_memory", state)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """启动写缓冲后台任务。"""
        await self._write_buffer.start(loop)

    async def stop(self) -> None:
        """停止写缓冲并执行最后一次 flush。"""
        await self._write_buffer.stop()

    # ------------------------------------------------------------------
    # 快照解析辅助
    # ------------------------------------------------------------------

    def get_persona(self) -> dict[str, Any] | None:
        """从快照中获取 persona 配置。"""
        if self._snapshot:
            return self._snapshot.get("persona")
        return None

    def get_orchestration(self) -> dict[str, Any] | None:
        """从快照中获取 orchestration 配置。"""
        if self._snapshot:
            return self._snapshot.get("orchestration")
        return None

    def get_experience(self) -> dict[str, Any] | None:
        """从快照中获取 experience 配置。"""
        if self._snapshot:
            return self._snapshot.get("experience")
        return None

    def get_task_params(self) -> dict[str, Any]:
        """从快照中获取 task_params（仅参数字段，不含 model routing）。"""
        if self._snapshot:
            return self._snapshot.get("task_params", {})
        return {}

    def get_working_memories(self) -> dict[str, list[dict[str, Any]]]:
        """从快照中获取各群工作记忆。"""
        if self._snapshot:
            return self._snapshot.get("working_memories", {})
        return {}

    def get_basic_memory_state(self) -> dict[str, Any] | None:
        """从快照中获取基础记忆序列化状态。"""
        if self._snapshot:
            return self._snapshot.get("basic_memory")
        return None

    def get_assistant_emotion(self) -> dict[str, Any] | None:
        """从快照中获取助手情绪状态。"""
        if self._snapshot:
            return self._snapshot.get("assistant_emotion")
        return None

    def get_group_timestamps(self) -> dict[str, str]:
        """从快照中获取群组时间戳。"""
        if self._snapshot:
            return self._snapshot.get("group_timestamps", {})
        return {}

    def get_diary_state(self) -> dict[str, Any] | None:
        """从快照中获取日记状态。"""
        if self._snapshot:
            return self._snapshot.get("diary_state")
        return None

    def get_user_manager_state(self) -> dict[str, Any] | None:
        """从快照中获取用户管理器状态。"""
        if self._snapshot:
            return self._snapshot.get("user_manager")
        return None

    def get_glossary(self) -> dict[str, Any]:
        """从快照中获取术语表。"""
        if self._snapshot:
            return self._snapshot.get("glossary", {})
        return {}

    def get_users(self) -> list[dict[str, Any]]:
        """从快照中获取用户列表。"""
        if self._snapshot:
            return self._snapshot.get("users", [])
        return []

    def get_archives(self) -> dict[str, list[dict[str, Any]]]:
        """从快照中获取归档消息。"""
        if self._snapshot:
            return self._snapshot.get("archives", {})
        return {}
