"""框架级 GitHub 交互基础设施。

提供：
- GitHubWebhookServer: Webhook 签名验证 + HTTP 生命周期 + 事件分发
- GitHubClient: 渐近式请求封装，使用标准 API headers
- fetch_repo_events: Events API 批量获取 + 速率限制检测
- event_bridge: github_monitor → plugin 事件通知桥接
"""

from sirius_chat.github.client import GitHubClient, github_headers
from sirius_chat.github.event_bridge import (
    notify_issue_opened,
    notify_pr_event,
    register_issue_handler,
    register_pr_handler,
)
from sirius_chat.github.events import fetch_repo_events
from sirius_chat.github.webhook import GitHubWebhookServer, RepoFilter, WebhookHandler, verify_signature

__all__ = [
    "GitHubClient",
    "GitHubWebhookServer",
    "RepoFilter",
    "WebhookHandler",
    "fetch_repo_events",
    "github_headers",
    "notify_issue_opened",
    "notify_pr_event",
    "register_issue_handler",
    "register_pr_handler",
    "verify_signature",
]
