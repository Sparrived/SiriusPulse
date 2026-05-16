"""GitHub 事件桥接 —— 让 github_monitor SKILL 检测到的事件能被 Plugin 消费。

github_monitor 在检测到新 Issue/PR 后调用 notify_* 函数，
coding_agent 等插件通过 register_* 注册处理器，无需自行搭建 webhook 或轮询。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

IssueHandler = Callable[[dict[str, Any], str], Awaitable[None]]
PrHandler = Callable[[dict[str, Any], str, str], Awaitable[None]]

_issue_handlers: list[IssueHandler] = []
_pr_handlers: list[PrHandler] = []


def register_issue_handler(handler: IssueHandler) -> None:
    """注册 Issue 事件处理器。

    handler(body: dict, repo_name: str) -> None
    body 为 GitHub webhook body 格式（含 action/issue/repository/sender 等）。
    仅 issue opened 事件会触发。
    """
    _issue_handlers.append(handler)


def register_pr_handler(handler: PrHandler) -> None:
    """注册 PR 事件处理器。

    handler(body: dict, repo_name: str, action: str) -> None
    body 为 GitHub webhook body 格式。
    action: "opened" | "synchronize"
    """
    _pr_handlers.append(handler)


async def notify_issue_opened(body: dict[str, Any], repo_name: str) -> None:
    """由 github_monitor 调用：通知所有注册者新 Issue 已创建。"""
    for handler in _issue_handlers:
        try:
            await handler(body, repo_name)
        except Exception:
            logger.exception("Issue handler 异常: repo=%s", repo_name)


async def notify_pr_event(body: dict[str, Any], repo_name: str, action: str) -> None:
    """由 github_monitor 调用：通知所有注册者 PR 事件。"""
    for handler in _pr_handlers:
        try:
            await handler(body, repo_name, action)
        except Exception:
            logger.exception("PR handler 异常: repo=%s action=%s", repo_name, action)
