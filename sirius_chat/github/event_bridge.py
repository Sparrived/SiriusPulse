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
CommentHandler = Callable[[dict[str, Any], str], Awaitable[None]]

_issue_handlers: list[IssueHandler] = []
_pr_handlers: list[PrHandler] = []
_comment_handlers: list[CommentHandler] = []

# 哪些仓库注册了 Issue 处理器（由插件在 on_load 时填入）
_issue_repos: set[str] = set()
_coding_bot_login: str = ""


def set_issue_repos(repos: set[str]) -> None:
    """由插件调用：声明哪些仓库被 Issue 处理器覆盖。

    github_monitor 据此判断是否跳过 \"opened\" 通知，
    改为等待 \"labeled\" 事件（由 coding_agent 贴标签后触发）。
    """
    global _issue_repos
    _issue_repos = set(repos)


def set_coding_bot_login(login: str) -> None:
    """由 coding_agent 调用：设置 AI bot 的 GitHub 用户名。

    github_monitor 据此判断哪些评论是 AI 发表的，
    在 coding 覆盖仓库中仅转发 AI 评论到群聊。
    """
    global _coding_bot_login
    _coding_bot_login = login


def get_coding_bot_login() -> str:
    """获取 AI bot 的 GitHub 用户名，空串表示未配置。"""
    return _coding_bot_login


def get_issue_repos() -> set[str]:
    """获取被插件覆盖的仓库列表。"""
    return _issue_repos


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


def register_comment_handler(handler: CommentHandler) -> None:
    """注册 Issue 评论事件处理器。

    handler(body: dict, repo_name: str) -> None
    body 为 GitHub webhook body 格式（含 action/comment/issue/repository）。
    仅 comment created 事件会触发。
    """
    _comment_handlers.append(handler)


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


async def notify_issue_comment(body: dict[str, Any], repo_name: str) -> None:
    """由 github_monitor 调用：通知所有注册者 Issue 新评论。"""
    for handler in _comment_handlers:
        try:
            await handler(body, repo_name)
        except Exception:
            logger.exception("Comment handler 异常: repo=%s", repo_name)
