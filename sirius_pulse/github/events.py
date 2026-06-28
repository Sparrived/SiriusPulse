"""GitHub Events API —— 从 github_monitor SKILL 提取的公共事件获取逻辑。

提供仓库活动事件获取，带速率限制检测与可配置的重试机制。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sirius_pulse.github.client import GitHubClient

logger = logging.getLogger(__name__)

_MAX_EVENTS_PER_PAGE = 30
_DEFAULT_RETRIES = 3


async def fetch_repo_events(
    client: GitHubClient,
    owner: str,
    repo: str,
    *,
    per_page: int = _MAX_EVENTS_PER_PAGE,
    max_retries: int = _DEFAULT_RETRIES,
    extra_headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """调用 GitHub Events API 拉取仓库最新事件列表。

    自动处理 403/429（速率限制）并支持重试。

    Args:
        client: 已创建的 GitHubClient 实例（需已 __aenter__）
        owner: 仓库所有者
        repo: 仓库名
        per_page: 每页事件数量
        max_retries: 最大重试次数
        extra_headers: 额外的请求头（如 per-repo Authorization）
    """
    from urllib.parse import quote

    path = f"/repos/{quote(owner)}/{quote(repo)}/events"
    params: dict[str, int] = {"per_page": per_page}
    headers = dict(extra_headers or {}) if extra_headers else None

    for attempt in range(1, max_retries + 1):
        resp = await client.get(path, params=params, headers=headers)

        if resp.status_code in (403, 429):
            logger.warning(
                "github: %s/%s API %d（可能触发速率限制，第 %d/%d 次）",
                owner,
                repo,
                resp.status_code,
                attempt,
                max_retries,
            )
            if attempt < max_retries:
                await asyncio.sleep(2.0 * attempt)
                continue
            return []

        if resp.status_code == 200:
            data = resp.json()
            events_list: list[dict[str, Any]] = data if isinstance(data, list) else []
            logger.debug("github: %s/%s API 200, 获取到 %d 条事件", owner, repo, len(events_list))
            return events_list

        logger.error("github: %s/%s Events API 返回 %d", owner, repo, resp.status_code)
        if attempt < max_retries:
            await asyncio.sleep(2.0 * attempt)
            continue
        return []

    return []
