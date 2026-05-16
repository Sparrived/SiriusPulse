"""GitHub 仓库活动监控被动 SKILL。

通过后台任务周期性轮询指定 GitHub 仓库的事件（Issues、PR、Release、Commit、Comment 等），
检测到新活动后使用 Playwright 截取对应页面截图，并生成人格风格的通知消息。

配置由 WebUI 写入 data_store（skill_data/github_monitor.json）：
{
    "api_base_url": "https://api.github.com",
    "poll_seconds": 120,
    "repos": [
        {
            "owner": "Sparrived",
            "repo": "SiriusChat",
            "events": ["issues", "pulls", "releases", "comments", "pushes"],
            "groups": ["gid_xxx"],
            "github_token": ""
        }
    ],
    "last_event_timestamps": {},
    "_last_poll_at": {}
}
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

SKILL_META = {
    "name": "github_monitor",
    "description": (
        "监控指定 GitHub 仓库的活动（Issues/PR/Release/Comment/Push），"
        "检测到新事件时自动截取页面截图并生成人格风格的通知消息。"
        "配置通过 WebUI 管理，无需 AI 主动调用。"
    ),
    "version": "1.0.0",
    "tags": ["github", "monitor", "notification"],
    "developer_only": False,
    "dependencies": ["playwright", "httpx"],
}

_GITHUB_API_BASE = "https://api.github.com"
_DEFAULT_POLL_SECONDS = 120
_MIN_BG_INTERVAL = 30
_MAX_EVENTS_PER_PAGE = 30
_MAX_COMMITS_IN_BODY = 5
_MAX_API_RETRIES = 3
_MAX_SCREENSHOT_RETRIES = 3

# PR 合并提交的消息模式（GitHub 自动生成）
_PR_MERGE_COMMIT_PATTERN = re.compile(r"^Merge pull request #\d+ from ")

# 用户配置的事件类型 → GitHub Event API type 集合
_EVENT_TYPE_FILTER: dict[str, set[str]] = {
    "issues": {"IssuesEvent"},
    "pulls": {"PullRequestEvent"},
    "releases": {"ReleaseEvent"},
    "comments": {
        "IssueCommentEvent",
        "PullRequestReviewCommentEvent",
        "CommitCommentEvent",
    },
    "pushes": {"PushEvent"},
}

# 事件类型 → 中文描述
_TYPE_DESC: dict[str, str] = {
    "IssuesEvent": "Issue",
    "PullRequestEvent": "Pull Request",
    "ReleaseEvent": "Release",
    "IssueCommentEvent": "评论 (Issue)",
    "PullRequestReviewCommentEvent": "评论 (PR Review)",
    "CommitCommentEvent": "评论 (Commit)",
    "PushEvent": "推送",
}

# 动作 → 中文描述
_ACTION_DESC: dict[str, str] = {
    "opened": "新建了",
    "closed": "关闭了",
    "reopened": "重新打开了",
    "edited": "编辑了",
    "deleted": "删除了",
    "published": "发布了",
    "created": "创建了",
    "merged": "合并了",
    "synchronize": "更新了",
}


def create_background_tasks(ctx: Any) -> list[Any]:
    """注册周期性 GitHub 事件轮询后台任务。

    后台以最小间隔唤醒（30s），由 _poll_github_events 内部根据
    skill 配置中的 poll_seconds 自行节流。
    """
    from sirius_chat.skills.models import BackgroundTaskSpec

    async def _check() -> None:
        await _poll_github_events(ctx)

    return [
        BackgroundTaskSpec(
            name="github_monitor_poll",
            interval_seconds=_MIN_BG_INTERVAL,
            task_func=_check,
        )
    ]


# ═══════════════════════════════════════════════════════════════════════
# 主轮询逻辑
# ═══════════════════════════════════════════════════════════════════════


async def _poll_github_events(ctx: Any) -> None:
    """遍历所有监控仓库，拉取新事件并触发通知。

    从 skill data_store 读取 poll_seconds（默认 120s）控制实际 API 调用频率，
    防止频繁请求触发 GitHub 速率限制。
    """
    store = ctx.get_data_store("github_monitor")
    # 每轮先从磁盘重载，以便 WebUI 修改 poll_seconds / repos 后无需重启即生效
    store.reload()
    repos: list[dict[str, Any]] = list(store.get("repos", []))
    if not repos:
        return

    poll_seconds: float = float(store.get("poll_seconds", _DEFAULT_POLL_SECONDS))
    api_base_url: str = str(store.get("api_base_url", "")).strip() or _GITHUB_API_BASE

    last_ts: dict[str, str] = dict(store.get("last_event_timestamps", {}) or {})
    last_poll: dict[str, float] = dict(store.get("_last_poll_at", {}) or {})

    now = time.monotonic()

    # 尝试导入 httpx，如未安装则跳过本次轮询
    try:
        import httpx
    except ImportError:
        logger.warning("github_monitor: httpx 未安装，跳过轮询")
        return

    async with httpx.AsyncClient(
        base_url=api_base_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "SiriusChat-GitHub-Monitor/1.0",
        },
        timeout=httpx.Timeout(30.0),
    ) as client:
        for repo_cfg in repos:
            owner = str(repo_cfg.get("owner", "")).strip()
            repo = str(repo_cfg.get("repo", "")).strip()
            if not owner or not repo:
                continue

            events_config: list[str] = repo_cfg.get("events", [])
            target_groups: list[str] = repo_cfg.get("groups", [])
            github_token = str(repo_cfg.get("github_token", "")).strip()

            if not events_config or not target_groups:
                continue

            # 构建允许事件类型集合
            allowed_types: set[str] = set()
            for ec in events_config:
                allowed_types.update(_EVENT_TYPE_FILTER.get(ec, set()))
            if not allowed_types:
                continue

            repo_key = f"{owner}/{repo}"

            # 按 poll_seconds 节流：距离上次 API 调用未满 poll_seconds 则跳过
            prev = last_poll.get(repo_key, 0.0)
            if now - prev < poll_seconds:
                continue

            since = last_ts.get(repo_key)

            # 拉取事件（带重试）
            logger.debug("github_monitor: 正在获取 %s 事件... (%s)", repo_key, api_base_url)
            events: list[dict[str, Any]] = []
            last_error = None
            for attempt in range(1, _MAX_API_RETRIES + 1):
                try:
                    events = await _fetch_repo_events(client, owner, repo, github_token)
                    break
                except Exception:
                    last_error = f"第 {attempt}/{_MAX_API_RETRIES} 次失败"
                    if attempt < _MAX_API_RETRIES:
                        logger.warning(
                            "github_monitor: %s 拉取 %s 失败，%.1fs 后重试",
                            last_error, repo_key, 2.0 * attempt,
                        )
                        await asyncio.sleep(2.0 * attempt)
                    else:
                        logger.warning(
                            "github_monitor: 拉取 %s 事件失败（共 %d 次）",
                            repo_key, _MAX_API_RETRIES, exc_info=True,
                        )
            if last_error and not events:
                continue

            if not events:
                # API 调用成功但无事件，更新时间戳避免频繁空轮询
                logger.info("github_monitor: %s 无新事件", repo_key)
                last_poll[repo_key] = now
                store.set("_last_poll_at", last_poll)
                store.save()
                continue

            # 筛选 since 之后的新事件，只保留启用的类型并按时间倒序
            # 同时跳过 PR 合并导致的 PushEvent（与 PullRequestEvent 重复）
            new_events: list[dict[str, Any]] = []
            skipped_pr_merges = 0
            for event in events:
                created_at = event.get("created_at", "")
                if since and created_at <= since:
                    continue
                if event.get("type", "") not in allowed_types:
                    continue
                if _is_pr_merge_push_event(event):
                    skipped_pr_merges += 1
                    continue
                new_events.append(event)

            if skipped_pr_merges:
                logger.debug(
                    "github_monitor: %s 跳过了 %d 条 PR 合并 Push 事件（与 PullRequestEvent 重复）",
                    repo_key, skipped_pr_merges,
                )

            if not new_events:
                # API 调用成功，新事件筛选后为空，更新时间戳
                last_poll[repo_key] = now
                store.set("_last_poll_at", last_poll)
                store.save()
                continue

            # 获取最新事件时间戳用于更新
            newest_ts = new_events[0].get("created_at")
            is_first_poll = not since

            if newest_ts:
                last_ts[repo_key] = newest_ts
                store.set("last_event_timestamps", last_ts)
                store.save()

            # 首次轮询（未有历史时间戳）：仅更新时间戳，跳过本次通知，
            # 避免把历史事件全部播报导致刷屏。
            if is_first_poll:
                last_poll[repo_key] = now
                store.set("_last_poll_at", last_poll)
                store.save()
                logger.info(
                    "github_monitor: %s 首次同步完成，已跳过 %d 条历史事件",
                    repo_key, len(new_events),
                )
                continue

            # 提取事件信息并按规范 URL 分组合并
            # 同一 Issue/PR/Release 页面上的多个事件合并为一次通知，
            # 避免对同一页面重复截图和 LLM 调用
            grouped: dict[str, list[dict[str, Any]]] = {}
            for event in reversed(new_events):
                event_info = _extract_event_info(event)
                canonical = event_info.get("canonical_url", event_info.get("url", ""))
                grouped.setdefault(canonical, []).append(event_info)

            logger.info(
                "github_monitor: %s 发现 %d 条新事件，合并为 %d 组",
                repo_key, len(new_events), len(grouped),
            )

            for canonical_url, group in grouped.items():
                merged_info = _merge_event_group(group)

                # 截图：PR 事件截 /files diff 页，Push 截 compare 页，其余截主页面
                screenshot_path: str | None = None
                screenshot_url = merged_info.get("screenshot_url", "") or merged_info.get("url", "") or canonical_url
                if screenshot_url:
                    try:
                        screenshot_path = await _take_screenshot(screenshot_url, store)
                    except Exception as exc:
                        logger.warning("github_monitor: 截图失败 (%s): %s", screenshot_url, exc)

                # LLM 生成：每个合并组仅调用一次
                notification = await _generate_notification_text(
                    ctx, merged_info, screenshot_path
                )

                if not notification:
                    continue

                merged_count = merged_info.get("merged_count", 1)
                ctx.log_inner_thought(
                    f"github_monitor: [{merged_info['repo']}] {merged_info['actor']} "
                    f"{'、'.join(merged_info.get('merged_actions', [merged_info.get('action_cn', '') + merged_info.get('type_desc', '')]))} "
                    f"({'合并' + str(merged_count) + '条事件' if merged_count > 1 else '1条事件'})"
                    f" - 通知已生成，分发到 {len(target_groups)} 个群"
                )

                # 分发给所有订阅群
                for gid in target_groups:
                    active_groups = ctx.get_active_groups()
                    if gid not in active_groups and not gid.startswith("private_"):
                        continue
                    try:
                        await _dispatch_notification(ctx, gid, notification, screenshot_path)
                    except Exception as exc:
                        logger.warning(
                            "github_monitor: 分发 %s 失败 (gid=%s): %s",
                            repo_key, gid, exc,
                        )

            # 本轮 API 调用完成，保存调用时间戳
            last_poll[repo_key] = time.monotonic()
            store.set("_last_poll_at", last_poll)
            store.save()


# ═══════════════════════════════════════════════════════════════════════
# GitHub API 交互
# ═══════════════════════════════════════════════════════════════════════


def _is_pr_merge_push_event(event: dict[str, Any]) -> bool:
    """判断一个 PushEvent 是否全部由 PR 合并提交构成。

    PR 合并后 GitHub 会自动生成 "Merge pull request #XX from ..." 提交并推送，
    这些 PushEvent 与 PullRequestEvent（merged）重复，应跳过以去噪。
    """
    if event.get("type", "") != "PushEvent":
        return False
    commits: list[dict[str, Any]] = (event.get("payload", {}) or {}).get("commits", [])
    if not commits:
        return False
    return all(
        _PR_MERGE_COMMIT_PATTERN.match(c.get("message", "")) for c in commits
    )


async def _fetch_repo_events(
    client: Any,
    owner: str,
    repo: str,
    token: str,
) -> list[dict[str, Any]]:
    """调用 GitHub Events API 拉取仓库最新事件列表。"""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    params: dict[str, int] = {"per_page": _MAX_EVENTS_PER_PAGE}
    path = f"/repos/{quote(owner)}/{quote(repo)}/events"

    resp = await client.get(path, headers=headers, params=params)

    if resp.status_code in (403, 429):
        logger.warning(
            "github_monitor: %s/%s API %d（可能触发速率限制）",
            owner, repo, resp.status_code,
        )
        return []

    resp.raise_for_status()
    data = resp.json()
    events_list = data if isinstance(data, list) else []
    logger.debug(
        "github_monitor: %s/%s API 200, 获取到 %d 条事件",
        owner, repo, len(events_list),
    )
    return events_list


# ═══════════════════════════════════════════════════════════════════════
# 事件信息提取
# ═══════════════════════════════════════════════════════════════════════


def _clean_canonical_url(url: str) -> str:
    """规范化 URL 用于分组合并：去除 fragment (#xxx) 和尾部斜杠。

    确保如 /pull/2 与 /pull/2#issuecomment-xxx 能正确归入同一组。
    """
    if not url:
        return url
    # 去除 fragment 锚点
    cleaned = url.split("#")[0]
    # 去除尾部斜杠
    cleaned = cleaned.rstrip("/")
    return cleaned


def _extract_event_info(event: dict[str, Any]) -> dict[str, Any]:
    """从原始 GitHub Event JSON 中提取结构化信息。"""
    etype = event.get("type", "未知事件")
    repo_info = event.get("repo", {})
    actor = event.get("actor", {})
    payload = event.get("payload", {}) or {}
    created_at = event.get("created_at", "")

    repo_name = repo_info.get("name", "未知仓库")
    actor_name = actor.get("display_login") or actor.get("login", "未知用户")
    html_url = ""
    canonical_url = ""
    title = ""
    body = ""
    action = payload.get("action", "")
    action_cn = _ACTION_DESC.get(action, action)
    # type_desc 默认从映射表取，PR 评论会覆盖
    type_desc = _TYPE_DESC.get(etype, etype)

    if etype == "IssuesEvent":
        issue = payload.get("issue", {})
        title = issue.get("title", "")
        body = _truncate_text(issue.get("body") or "")
        html_url = issue.get("html_url", "")
        canonical_url = _clean_canonical_url(html_url)
    elif etype == "PullRequestEvent":
        pr_data = payload.get("pull_request", {})
        title = pr_data.get("title", "")
        body = _truncate_text(pr_data.get("body") or "")
        html_url = pr_data.get("html_url", "")
        canonical_url = _clean_canonical_url(html_url)
        # PR 的 merged 动作特殊处理
        if pr_data.get("merged") and action == "closed":
            action_cn = "合并了"
    elif etype == "ReleaseEvent":
        release = payload.get("release", {})
        title = release.get("name") or release.get("tag_name", "")
        body = _truncate_text(release.get("body") or "")
        html_url = release.get("html_url", "")
        canonical_url = _clean_canonical_url(html_url)
    elif etype in (
        "IssueCommentEvent",
        "PullRequestReviewCommentEvent",
        "CommitCommentEvent",
    ):
        comment = payload.get("comment", {})
        body = _truncate_text(comment.get("body") or "")
        html_url = comment.get("html_url", "")
        if etype == "IssueCommentEvent":
            issue = payload.get("issue", {})
            title = issue.get("title", "")
            # 检测是否为 PR 评论：issue 含 pull_request 字段或 html_url 路径为 /pull/
            issue_url = issue.get("html_url", "") or html_url
            if issue.get("pull_request") or "/pull/" in issue_url:
                type_desc = "评论 (PR)"
                canonical_url = _clean_canonical_url(issue_url)
            else:
                canonical_url = _clean_canonical_url(issue_url)
        elif etype == "PullRequestReviewCommentEvent":
            pr_data = payload.get("pull_request", {})
            title = pr_data.get("title", "") if pr_data else ""
            canonical_url = _clean_canonical_url(pr_data.get("html_url", html_url) if pr_data else html_url)
        else:
            # CommitCommentEvent：规范 URL 为 commit 页面
            canonical_url = _clean_canonical_url(html_url)
    elif etype == "PushEvent":
        commits: list[dict[str, Any]] = payload.get("commits", [])
        ref = payload.get("ref", "")
        branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ref
        title = f"{len(commits)} 个提交 → {branch}"
        commit_lines: list[str] = []
        for c in commits[:_MAX_COMMITS_IN_BODY]:
            msg_first_line = (c.get("message", "")).split("\n")[0][:100]
            commit_lines.append(f"- {msg_first_line}")
        body = "\n".join(commit_lines)
        # 优先使用 compare URL（展示具体变更 diff），其次使用仓库主页
        html_url = payload.get("compare", "") or f"https://github.com/{repo_name}"
        canonical_url = _clean_canonical_url(html_url)

    # 截图 URL：PR 事件截 /files diff 页，Push 截 compare 页，其余截各自页面
    if etype in ("PullRequestEvent", "PullRequestReviewCommentEvent"):
        screenshot_url = html_url + "/files" if html_url else ""
    elif etype == "PushEvent":
        # html_url 已优先使用 compare URL，直接用即可
        screenshot_url = html_url
    else:
        screenshot_url = html_url

    return {
        "repo": repo_name,
        "type": etype,
        "type_desc": type_desc,
        "actor": actor_name,
        "action": action,
        "action_cn": action_cn,
        "title": title,
        "body": body,
        "url": html_url,
        "screenshot_url": screenshot_url,
        "canonical_url": canonical_url or html_url,
        "created_at": created_at,
    }


def _truncate_text(text: str, max_len: int = 500) -> str:
    """截断过长文本，用于 body 摘要。"""
    if not text:
        return ""
    cleaned = re.sub(r"```[\s\S]*?```", "[代码块已省略]", text)
    cleaned = re.sub(r"!\[.*?\]\(.*?\)", "[图片已省略]", cleaned)
    cleaned = re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", cleaned)
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "..."
    return cleaned


# ═══════════════════════════════════════════════════════════════════════
# 事件合并
# ═══════════════════════════════════════════════════════════════════════


def _merge_event_group(events: list[dict[str, Any]]) -> dict[str, Any]:
    """将同一规范页面（同一 canonical_url）的多个事件合并为一个。

    合并规则：
    - 以第一个事件为基础，保留 repo / title / url / canonical_url 等页面级字段
    - 汇总所有事件的 actor 列表（去重）和动作描述列表（去重）
    - 若只有一个事件则原样返回，不做额外包装
    """
    if len(events) == 1:
        return events[0]

    primary = dict(events[0])

    # 汇总所有参与者（去重保序）
    actors: list[str] = []
    seen_actors: set[str] = set()
    for e in events:
        actor = e.get("actor", "")
        if actor and actor not in seen_actors:
            actors.append(actor)
            seen_actors.add(actor)

    # 汇总所有动作描述（去重保序）
    merged_actions: list[str] = []
    seen_actions: set[str] = set()
    for e in events:
        desc = f"{e.get('action_cn', '')}{e.get('type_desc', '')}"
        if desc and desc not in seen_actions:
            merged_actions.append(desc)
            seen_actions.add(desc)

    # 汇总 body：拼接所有非空 body
    bodies = [e.get("body", "") for e in events if e.get("body", "")]
    merged_body = "\n---\n".join(bodies) if bodies else primary.get("body", "")

    primary["actor"] = "、".join(actors) if len(actors) > 1 else (actors[0] if actors else primary.get("actor", ""))
    primary["merged_actions"] = merged_actions
    primary["merged_count"] = len(events)
    primary["body"] = merged_body
    # url 设为规范页面 URL（合并组内所有事件共享的页面链接）
    primary["url"] = primary.get("canonical_url", primary.get("url", ""))
    # screenshot_url 若未设则退回到 url（合并后截图仍用规范页面）
    if not primary.get("screenshot_url"):
        primary["screenshot_url"] = primary["url"]

    return primary


# ═══════════════════════════════════════════════════════════════════════
# 事件分发给群
# ═══════════════════════════════════════════════════════════════════════


async def _dispatch_notification(
    ctx: Any,
    group_id: str,
    text: str,
    screenshot_path: str | None,
) -> None:
    """将已生成的通知文字和截图分发给单个群。"""
    ctx.queue_pending_message(group_id, text)
    await ctx.emit_event(
        "reminder_triggered",
        {
            "group_id": group_id,
            "reply": text,
            "image_path": screenshot_path or "",
            "adapter_type": "napcat",
        },
    )
    # 私聊群需要激活
    if group_id.startswith("private_"):
        ctx.activate_private_group(group_id)


# ═══════════════════════════════════════════════════════════════════════
# Playwright 页面截图
# ═══════════════════════════════════════════════════════════════════════


async def _take_screenshot(url: str, store: Any) -> str | None:
    """使用 Playwright 无头浏览器截取 GitHub 页面截图，存入 artifact 目录。

    返回截图文件的绝对路径，失败时返回 None 并记录警告日志。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("github_monitor: playwright 未安装，跳过截图")
        return None

    output_dir = _get_artifact_dir(store)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    output_path = output_dir / f"github_{timestamp}.png"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # 等待页面渲染完成
                await asyncio.sleep(2)

                # 全页截图：失败时重试（每次等待更久让页面充分渲染）
                last_error = None
                for attempt in range(1, _MAX_SCREENSHOT_RETRIES + 1):
                    try:
                        await page.screenshot(path=str(output_path), full_page=True)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt < _MAX_SCREENSHOT_RETRIES:
                            logger.debug(
                                "github_monitor: full_page 截图第 %d 次失败，%.1fs 后重试 (%s): %s",
                                attempt, 2.0 * attempt, url, exc,
                            )
                            await asyncio.sleep(2.0 * attempt)
                if last_error is not None:
                    raise last_error

                await context.close()
                logger.info("github_monitor: 截图已保存 → %s", output_path)
                return str(output_path)
            finally:
                await browser.close()
    except Exception as exc:
        logger.warning("github_monitor: Playwright 截图异常 (%s): %s", url, exc)
        return None


def _get_artifact_dir(store: Any) -> Path:
    """获取 SKILL artifact 目录路径。"""
    artifact_dir = getattr(store, "artifact_dir", None)
    if isinstance(artifact_dir, Path):
        return artifact_dir
    if artifact_dir:
        return Path(str(artifact_dir))
    return Path("data") / "skill_data" / "artifacts" / "github_monitor"


# ═══════════════════════════════════════════════════════════════════════
# 人格风格通知生成
# ═══════════════════════════════════════════════════════════════════════


async def _generate_notification_text(
    ctx: Any,
    event_info: dict[str, Any],
    screenshot_path: str | None,
) -> str | None:
    """调用 LLM 生成人格风格的通知消息（不绑定群，不写记忆）。

    构建包含人格身份、事件详情的 prompt，并将页面截图作为多模态输入
    传给模型，让 AI 能参考真实页面内容生成更贴合的回覆。
    """
    try:
        persona = ctx.get_persona()
        identity = persona.build_system_prompt() if persona else ""

        # 构建事件描述
        event_desc = _build_event_section(event_info, screenshot_path)

        system_prompt = (
            f"{identity}\n\n"
            f"【GitHub 仓库动态播报】\n"
            f"{event_desc}\n\n"
            f"请用你的人格风格，自然地向群友们播报这条 GitHub 仓库动态。\n"
            f"要求：\n"
            f"- 不要机械复述，像朋友分享新鲜事一样自然\n"
            f"- 简短即可，2-4 句话\n"
            f"- 必须明确提到「操作者」是谁（不要混淆为你人格设定中的人），"
            f"这个操作者是真实的 GitHub 用户\n"
            f"- 提到关键信息：谁、做了什么、涉及什么仓库\n"
            f"- 必须在播报末尾附带「链接」中的网址，让群友可以直接点击跳转\n"
            f"- 可以表达你的感受（惊讶、期待、好奇等），但要符合你的人设\n"
            f"- 如果附带了页面截图，请结合截图内容描述具体变化"
        )

        # 构建多模态 user message（如有截图则以 image_url 格式传入）
        if screenshot_path:
            user_content: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": f"（{event_info['repo']} 仓库有新动态，下方是页面截图，请参考截图播报一下）",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": screenshot_path},
                },
            ]
        else:
            user_content = f"（{event_info['repo']} 仓库有新动态，请播报一下）"

        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]

        # 使用第一个活跃群作为 generate_text 的 group_id（仅用于 token 统计/路由）
        active_groups = ctx.get_active_groups()
        group_id = active_groups[0] if active_groups else "github_monitor"

        raw_reply = await ctx.generate_text(
            system_prompt,
            messages,
            group_id,
            task_name="github_monitor_notify",
        )

        from sirius_chat.skills.executor import strip_skill_calls

        reply = strip_skill_calls(raw_reply).strip()
        return reply or None
    except Exception as exc:
        logger.warning("github_monitor: 生成通知失败: %s", exc)
        return _build_fallback_notification(event_info)


def _build_event_section(
    event_info: dict[str, Any],
    screenshot_path: str | None,
) -> str:
    """构建注入 prompt 的事件描述 section。"""
    lines = [
        f"仓库: {event_info['repo']}",
        f"事件: {event_info['type_desc']}",
        f"操作者: {event_info['actor']}",
    ]

    # 合并事件：列出所有动作
    merged_actions = event_info.get("merged_actions")
    if merged_actions:
        lines.append(f"合并动作: {'、'.join(merged_actions)}")
        lines.append(f"（本组共合并了 {event_info.get('merged_count', 1)} 条关联事件）")
    elif event_info.get("action_cn"):
        lines.append(f"动作: {event_info['action_cn']}")

    if event_info.get("title"):
        lines.append(f"标题: {event_info['title']}")
    if event_info.get("body"):
        lines.append(f"内容: {event_info['body']}")
    if event_info.get("url"):
        lines.append(f"链接: {event_info['url']}")
    if screenshot_path:
        lines.append(f"页面截图: {screenshot_path}（可用作参考）")
    return "\n".join(lines)


def _build_fallback_notification(event_info: dict[str, Any]) -> str:
    """LLM 调用失败时的降级纯文本通知。"""
    repo = event_info.get("repo", "未知仓库")
    actor = event_info.get("actor", "有人")
    title = event_info.get("title", "")
    url = event_info.get("url", "")

    # 合并事件：列出所有动作
    merged_actions = event_info.get("merged_actions")
    if merged_actions:
        action_desc = "、".join(merged_actions)
        parts = [f"🔔 [{repo}] {actor} {action_desc}"]
    else:
        action_cn = event_info.get("action_cn", "")
        type_desc = event_info.get("type_desc", "")
        parts = [f"🔔 [{repo}] {actor} {action_cn}{type_desc}"]

    if title:
        parts.append(f"「{title}」")
    if url:
        parts.append(f"🔗 {url}")
    return " ".join(parts)
