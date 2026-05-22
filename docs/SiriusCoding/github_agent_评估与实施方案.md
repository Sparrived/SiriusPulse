# github_agent 插件可行性评估与实施方案

> 本文将原始设计方案（与 Gemini 对话产物）与 Sirius Pulse v1.1.0 框架源码逐项对照，输出：
> 1. 每个设计节点的可行性判定
> 2. 框架层需要的改动（最小化侵入）
> 3. 插件层每个文件的详细设计
> 4. 开发路径建议

---

## 最重要：人格性 -> Agent 从了解ISSUE/PR等等内容开始，输出的内容必须保证符合其人格属性

## 一、总览：框架现有能力对照表

| 设计文档中的需求 | 框架对应能力 | 落地状态 | 关键源码 |
|---|---|---|---|
| 插件入口 `__init__.py` | `PluginBase` + `on_load()` / `on_unload()` 生命周期 | ✅ 直接可用 | `plugins/base.py` |
| 配置管理 `config.py` | `PluginDataStore` + `plugins/_config.json` settings 字段 | ✅ 直接可用 | `plugins/context.py` L121-L169 |
| 私聊指令 `/gh` | `@command` 装饰器，支持 prefix / pattern / 参数类型注解 | ✅ 直接可用 | `plugins/decorators.py` |
| 私聊消息发送 | `adapter.send_private_message(user_id, message)` | ✅ 直接可用 | `platforms/.../adapter.py` L327-L335 |
| LLM 调用 | `EngineProxy.generate_text()` 复用框架完整生成链路 | ✅ 直接可用 | `plugins/context.py` L42-L52 |
| 大模型路由 | `ModelRouter` 任务感知模型选择 | ✅ 直接可用 | `core/model_router.py` |
| subprocess 沙盒 | Python 标准库 `asyncio.create_subprocess_exec` | ✅ 可直接使用 | — |
| 自定义事件 | `PluginEventType.CUSTOM` + `PluginEvent` dataclass | ✅ 直接可用 | `plugins/events.py` |
| Webhook HTTP 路由注册 | **无**。`TriggerType.EVENT_WEBHOOK` 枚举已定义但无实现 | ❌ 框架缺口 | `plugins/models.py` L38 |
| Issue 自动标签 | `EngineProxy.generate_text_analysis()` 用于快速分类 + GitHub Labels API | ✅ 直接可用 | `plugins/context.py` L57-L66 |
| Issue 智能回复 | `EngineProxy.generate_text_analysis()` 用于生成评论 + GitHub Issues Comments API | ✅ 直接可用 | `plugins/context.py` L57-L66 |
| PR 自动审阅 | `EngineProxy.generate_text_analysis()` 用于快速扫描 + GitHub Reviews API | ✅ 直接可用 | `plugins/context.py` L57-L66 |
| GitPython 依赖 | `PluginLoader` 通过 `_plugin_dependencies` 自动安装 | ✅ 直接可用 | `plugins/loader.py` |

**结论**：11 项需求中 10 项已有直接支持。唯一阻塞项是 **Webhook HTTP 路由的插件级注册能力**。

---

## 二、阻塞项详解：Webhook 路由注入

### 2.1 问题描述

原始设计期望插件在初始化时向 Sirius Pulse WebUI 注册 `POST /api/webhook/github` 路由：

```
路由注入：在插件初始化时，向 SiriusChat WebUI 注册 POST /api/webhook/github 路由。
```

### 2.2 框架现状

- `TriggerType.EVENT_WEBHOOK` 枚举在 `plugins/models.py` 中定义，但只是占位符——没有任何路由注册实现与之关联。
- 当前 WebUI 所有路由均在 `webui/server_core.py` 的 `_setup_routes()` 中**硬编码**注册（约 60 条 `app.router.add_xxx(...)`）。
- 插件系统没有向 aiohttp `Application` 动态注入路由的 API。

```python
# webui/server_core.py L112 — 所有路由都是硬编码的
def _setup_routes(self) -> None:
    self.app.router.add_get("/", self.index)
    self.app.router.add_get("/api/plugins", self.api_plugins_get)
    # ... 60+ 条硬编码路由
```

### 2.3 解决方案

**推荐方案：在 `WebUIServer` 上暴露 `register_plugin_route()` 方法。**

改动范围：
1. `webui/server_core.py` — 新增 `register_plugin_route(method, path, handler)` 方法 + 存储字典
2. `plugins/context.py` 或新建 `plugins/webhook.py` — 插件侧的调用入口
3. 插件在 `on_load()` 中通过 EngineProxy 或 PluginContext 间接调用

**改动量估算**：框架层约 50-80 行新增代码。

**备选方案（不推荐）**：插件自行启动独立 aiohttp 子服务监听新端口。缺点：违背"复用框架 WebUI"的设计意图，增加端口管理负担。

---

## 三、节点级可行性分析

### 3.1 节点①：感知与触发（Webhook Event Listener）

**原始设计**：

> 解析 Request Body，提取 repo_name、issue_number、action、issue_body。
> 过滤噪音：无关 action 或 issue 不含触发词 → 返回 200 并丢弃。
> 生成全局唯一 TaskID，广播 `GithubWebhookEvent`。

**框架匹配度**：⚠️ 需框架扩展（见第二章）后即可完全落地。

**落地后流程**：

```
GitHub Webhook POST → aiohttp handler(在 on_load 中注册)
  → 解析 body → 过滤 → 生成 TaskID
  → 存入 PluginDataStore (状态: PENDING_APPROVAL)
  → adapter.send_private_message(admin_user_id, "新Issue #X：[标题]，是否启动？")
  → 等待管理员的 /gh 指令
```

**关键实现点**：
- GitHub Webhook 签名验证：可选，用 `hmac` 标准库在 handler 中实现
- TaskID 生成：`uuid.uuid4().hex[:12]`
- 状态持久化：`PluginDataStore.set(f"task_{task_id}", {...})`

---

### 3.1.1 增强能力：Issue 自动分类与标签（Auto-Labeling）

**设计目标**：收到新 Issue 后，在通知管理员之前，由 LLM 自动分析 Issue 内容并打上合适的标签，降低人工分类成本。

**触发时机**：Webhook 收到 `action == "opened"` 的 Issue 事件后，在 PENDING_APPROVAL 挂起之前执行。

**标签分类体系**：

| 标签类别 | 示例标签 | 触发条件 |
|---|---|---|
| **类型 (type:)** | `type:bug`, `type:feature`, `type:docs`, `type:question`, `type:refactor` | 由 LLM 根据 Issue 内容推断 |
| **优先级 (priority:)** | `priority:critical`, `priority:high`, `priority:medium`, `priority:low` | 由 LLM 综合紧急程度推断 |
| **难度 (difficulty:)** | `difficulty:easy`, `difficulty:medium`, `difficulty:hard` | 由 LLM 根据修改范围推断 |
| **状态 (status:)** | `status:needs-triage`, `status:good-first-issue`, `status:help-wanted` | 自动 + 手动 |
| **模块 (area:)** | `area:core`, `area:api`, `area:ui`, `area:docs`, `area:tests` | 由 LLM 根据影响范围推断 |

**落地方案**：

```python
# webhook.py 或新建 labeler.py

async def auto_label_issue(
    issue_data: dict,
    repo_name: str,
    config: dict,
    engine_proxy: "EngineProxy",  # 复用框架 LLM 链路
) -> list[str]:
    """使用 LLM 对 Issue 进行自动分类并返回建议标签列表。

    通过 EngineProxy.generate_text_analysis() 调用轻量分析模型，
    输出结构化 JSON 供程序解析和应用。
    """
    prompt = f"""你是一个 Issue 分类助手。分析以下 GitHub Issue，输出 JSON 格式的标签建议。

严格遵守以下标签命名规范：
- 类型标签: type:bug / type:feature / type:docs / type:question / type:refactor
- 优先级标签: priority:critical / priority:high / priority:medium / priority:low
- 难度标签: difficulty:easy / difficulty:medium / difficulty:hard
- 模块标签: area:core / area:api / area:ui / area:docs / area:tests / area:config

Issue 标题: {issue_data['title']}
Issue 内容:
{issue_data['body'][:3000]}  # 截断防止超 Token

请输出严格 JSON（不要 Markdown 代码块包裹）:
{{
    "type": "bug|feature|docs|question|refactor",
    "priority": "critical|high|medium|low",
    "difficulty": "easy|medium|hard",
    "areas": ["area:xxx", ...],
    "auto_apply": true,    // true=直接打标签, false=仅建议
    "reason_brief": "一句话理由"
}}
"""
    try:
        result = await engine_proxy.generate_text_analysis(prompt)
        label_data = json.loads(result.strip())
    except (json.JSONDecodeError, Exception):
        # LLM 输出非标准 JSON 时降级为关键字匹配
        return _fallback_label_by_keywords(issue_data)

    labels: list[str] = []

    # 类型标签
    type_map = {
        "bug": "type:bug", "feature": "type:feature",
        "docs": "type:docs", "question": "type:question",
        "refactor": "type:refactor",
    }
    if label_data.get("type") in type_map:
        labels.append(type_map[label_data["type"]])

    # 优先级标签
    priority_map = {
        "critical": "priority:critical", "high": "priority:high",
        "medium": "priority:medium", "low": "priority:low",
    }
    if label_data.get("priority") in priority_map:
        labels.append(priority_map[label_data["priority"]])

    # 难度标签
    difficulty_map = {
        "easy": "difficulty:easy", "medium": "difficulty:medium",
        "hard": "difficulty:hard",
    }
    if label_data.get("difficulty") in difficulty_map:
        labels.append(difficulty_map[label_data["difficulty"]])

    # 模块标签
    valid_areas = {"area:core", "area:api", "area:ui", "area:docs", "area:tests", "area:config"}
    for area in label_data.get("areas", []):
        if area in valid_areas:
            labels.append(area)

    # 自动应用状态标签
    labels.append("status:needs-triage")

    # good-first-issue 推断：难度低 + 类型为 bug/feature
    if (label_data.get("difficulty") == "easy" and
            label_data.get("type") in ("bug", "feature")):
        labels.append("status:good-first-issue")

    return labels


def _fallback_label_by_keywords(issue_data: dict) -> list[str]:
    """LLM 分类失败时的关键词降级方案。"""
    text = f"{issue_data.get('title', '')} {issue_data.get('body', '')}".lower()
    labels = ["status:needs-triage"]

    # 关键词 → 类型
    if any(kw in text for kw in ["bug", "报错", "错误", "crash", "崩溃", "异常"]):
        labels.append("type:bug")
    elif any(kw in text for kw in ["feature", "功能", "建议", "希望", "新增"]):
        labels.append("type:feature")
    elif any(kw in text for kw in ["doc", "文档", "说明", "readme"]):
        labels.append("type:docs")
    else:
        labels.append("type:question")

    # 关键词 → 优先级
    if any(kw in text for kw in ["紧急", "urgent", "critical", "严重", "线上"]):
        labels.append("priority:critical")
    elif any(kw in text for kw in ["重要", "high", "核心"]):
        labels.append("priority:high")

    return labels


async def apply_labels_to_issue(
    repo_full_name: str,
    issue_number: int,
    labels: list[str],
    config: dict,
) -> bool:
    """通过 GitHub REST API 将标签应用到 Issue。

    先检查仓库是否存在该标签，若不存在则创建后再应用。
    """
    async with httpx.AsyncClient(headers=github_headers(config)) as client:
        # 获取仓库已有标签列表
        existing_resp = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/labels",
            params={"per_page": 100},
        )
        existing_labels: set[str] = set()
        if existing_resp.status_code == 200:
            for label in existing_resp.json():
                existing_labels.add(label["name"])

        # 对不存在的标签，按前缀归类自动创建
        new_labels = [l for l in labels if l not in existing_labels]
        for label_name in new_labels:
            color, description = _label_metadata(label_name)
            await client.post(
                f"https://api.github.com/repos/{repo_full_name}/labels",
                json={"name": label_name, "color": color, "description": description},
            )
            logger.info("创建新标签: %s (%s)", label_name, repo_full_name)

        # 应用标签到 Issue
        await client.post(
            f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/labels",
            json={"labels": labels},
        )
        return True


def _label_metadata(label_name: str) -> tuple[str, str]:
    """根据标签名前缀返回对应的颜色和描述。"""
    _LABEL_META = {
        "type:bug":       ("d73a4a", "Something isn't working"),
        "type:feature":   ("a2eeef", "New feature or request"),
        "type:docs":      ("0075ca", "Improvements or additions to documentation"),
        "type:question":  ("d876e3", "Further information is requested"),
        "type:refactor":  ("fbca04", "Code refactoring without feature change"),
        "priority:critical": ("b60205", "Must be resolved ASAP"),
        "priority:high":     ("d93f0b", "High priority"),
        "priority:medium":   ("fbca04", "Medium priority"),
        "priority:low":      ("0e8a16", "Low priority"),
        "difficulty:easy":   ("0e8a16", "Good for newcomers"),
        "difficulty:medium": ("fbca04", "Some experience required"),
        "difficulty:hard":   ("b60205", "Requires deep expertise"),
        "status:needs-triage":  ("ededed", "Awaiting triage"),
        "status:good-first-issue": ("7057ff", "Good for newcomers"),
        "status:help-wanted":     ("008672", "Extra attention is needed"),
        "area:core":    ("0052cc", "Core engine / runtime"),
        "area:api":     ("5319e7", "API / endpoints"),
        "area:ui":      ("d4c5f9", "User interface"),
        "area:docs":    ("0075ca", "Documentation"),
        "area:tests":   ("006b75", "Testing infrastructure"),
        "area:config":  ("bfdadc", "Configuration"),
    }
    return _LABEL_META.get(label_name, ("cccccc", ""))
```

**集成到 Webhook 流程中**：

```python
# webhook.py 中的 webhook_handler

async def webhook_handler(request: web.Request) -> web.Response:
    event_type = request.headers.get("X-GitHub-Event", "")
    body = await request.json()

    # 仅处理 Issue 打开事件
    if event_type == "issues" and body.get("action") == "opened":
        issue_data = body["issue"]
        repo_name = body["repository"]["full_name"]

        # ── 自动标签（非阻塞，失败不影响主流程）──
        try:
            labels = await auto_label_issue(
                issue_data, repo_name, _config, _engine_proxy
            )
            await apply_labels_to_issue(repo_name, issue_data["number"], labels, _config)
            logger.info("Issue #%d 自动标签: %s", issue_data["number"], labels)
        except Exception as exc:
            logger.warning("自动标签失败（不阻塞主流程）: %s", exc)
            labels = []

        # ── 继续原有流程：生成 TaskID → 持久化 → 通知管理员 ──
        task_id = uuid.uuid4().hex[:12]
        task_data = {
            "task_id": task_id,
            "repo": repo_name,
            "issue_number": issue_data["number"],
            "issue_title": issue_data["title"],
            "issue_body": issue_data.get("body", ""),
            "labels": labels,
            "status": "PENDING_APPROVAL",
            "created_at": time.time(),
        }
        _data_store.set(f"task_{task_id}", task_data)

        # 通知中附带标签信息
        label_str = " ".join(f"[{l}]" for l in labels) if labels else "（未自动标签）"
        await _adapter.send_private_message(
            _config["admin_user_id"],
            f"📥 新 Issue #{issue_data['number']}: {issue_data['title']}\n"
            f"🏷 自动标签: {label_str}\n"
            f"📋 仓库: {repo_name}\n"
            f"💬 回复 /gh {task_id} auto 启动自动修复"
        )

        return web.json_response({"status": "ok", "task_id": task_id})

    return web.json_response({"status": "ignored"})
```

**关键设计决策**：
- 标签分类使用 `EngineProxy.generate_text_analysis()`（task_name="plugin_analyze"），走框架的轻量分析模型，速度快且成本低
- 自动标签失败时降级到关键词匹配，确保不影响主流程（标签打不上比整个 Webhook 崩溃要好）
- 新标签自动创建并设定统一颜色，保持仓库标签体系整洁

---

### 3.1.2 智能 Issue 回复（Auto-Comment on Issue）

**设计目标**：收到新 Issue 后，agent 在打标签的同时，在 Issue 下发表一条智能评论。评论内容包括：（1）感谢提交并确认收到、（2）附上自动标签分析结果、（3）给出初步分析或引导性提问、（4）告知后续流程（管理员将审批是否启动自动修复）。让 Issue 提交者立刻获得反馈，提升社区体验。

**触发时机**：Webhook 收到 `action == "opened"` 的 Issue 事件后，标签分析完成后、通知管理员之前。

**与自动标签的关系**：自动标签和智能回复共享 LLM 分析结果——一次分析同时输出标签建议和评论内容，避免重复 LLM 调用。

**落地方案**：

```python
# commenter.py — Issue 智能回复模块

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def generate_issue_comment(
    issue_data: dict,
    labels: list[str],
    repo_full_name: str,
    engine_proxy: "EngineProxy",
) -> str:
    """使用 LLM 生成 Issue 智能回复评论。

    Args:
        issue_data: Issue 数据（title, body, number）
        labels: 已自动分析的标签列表（用于在评论中展示）
        repo_full_name: 仓库全名
        engine_proxy: 引擎代理

    Returns:
        Markdown 格式的评论正文
    """
    label_display = " ".join(f"`{l}`" for l in labels) if labels else "（待人工分类）"

    prompt = f"""你是开源项目的维护者。收到以下 GitHub Issue，请生成一条友善、专业的回复评论。

Issue #{issue_data['number']}: {issue_data['title']}

Issue 内容:
{issue_data.get('body', '')[:3000]}

已自动分析并应用的标签: {label_display}

回复要求：
1. 开头感谢用户提交 Issue
2. 简要复述你理解的问题（1-2 句，表明你认真读了）
3. 如果 issue 描述不够清晰，提出 1-2 个追问帮助澄清
4. 如果 issue 包含了复现步骤/错误日志，肯定用户的详细描述
5. 结尾告知后续流程：标签已自动分析，管理员将评估是否启动自动修复
6. 整体语气友善、专业，使用中文
7. 长度控制在 100-200 字，不要过长
8. 输出纯文本（Markdown 格式，但不要代码块包裹）

请直接输出评论正文，不要包含任何前缀说明。"""
    try:
        result = await engine_proxy.generate_text_analysis(prompt)
        return result.strip()
    except Exception:
        # LLM 调用失败时使用模板降级
        issue_title = issue_data.get("title", "未知")
        issue_number = issue_data.get("number", "?")
        return (
            f"感谢提交 Issue #{issue_number}：{issue_title}！\n\n"
            f"已自动分析并应用标签：{label_display}\n\n"
            f"管理员将尽快评估此 Issue，届时可能启动自动修复流程。感谢你的反馈！"
        )


async def post_issue_comment(
    repo_full_name: str,
    issue_number: int,
    comment_body: str,
    config: dict,
) -> bool:
    """通过 GitHub API 在 Issue 下发表评论。

    Args:
        repo_full_name: 仓库全名 (owner/repo)
        issue_number: Issue 编号
        comment_body: 评论正文（Markdown 格式）
        config: 插件配置

    Returns:
        是否成功
    """
    import httpx

    headers = {
        "Authorization": f"Bearer {config['github_pat']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        resp = await client.post(
            f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/comments",
            json={"body": comment_body},
        )
        if resp.status_code in (200, 201):
            logger.info("Issue #%d 智能回复已发表", issue_number)
            return True
        else:
            logger.error(
                "Issue #%d 回复发表失败: %d %s",
                issue_number, resp.status_code, resp.text[:200]
            )
            return False
```

**集成到 Webhook 流程中**（在标签应用之后、通知管理员之前）：

```python
# webhook.py 中的 webhook_handler 扩展

async def webhook_handler(request: web.Request) -> web.Response:
    event_type = request.headers.get("X-GitHub-Event", "")
    body = await request.json()

    if event_type == "issues" and body.get("action") == "opened":
        issue_data = body["issue"]
        repo_name = body["repository"]["full_name"]

        # ── 1. 自动标签 ──
        try:
            labels = await auto_label_issue(issue_data, repo_name, _config, _engine_proxy)
            await apply_labels_to_issue(repo_name, issue_data["number"], labels, _config)
        except Exception as exc:
            logger.warning("自动标签失败: %s", exc)
            labels = []

        # ── 2. 智能 Issue 回复（非阻塞，失败不影响主流程）──
        try:
            comment = await generate_issue_comment(
                issue_data, labels, repo_name, _engine_proxy
            )
            await post_issue_comment(repo_name, issue_data["number"], comment, _config)
            logger.info("Issue #%d 智能回复已发表", issue_data["number"])
        except Exception as exc:
            logger.warning("智能回复失败（不阻塞主流程）: %s", exc)

        # ── 3. 生成 TaskID → 持久化 → 通知管理员 ──
        task_id = uuid.uuid4().hex[:12]
        task_data = {
            "task_id": task_id,
            "repo": repo_name,
            "issue_number": issue_data["number"],
            "issue_title": issue_data["title"],
            "issue_body": issue_data.get("body", ""),
            "labels": labels,
            "status": "PENDING_APPROVAL",
            "created_at": time.time(),
        }
        _data_store.set(f"task_{task_id}", task_data)

        label_str = " ".join(f"[{l}]" for l in labels) if labels else "（未自动标签）"
        await _adapter.send_private_message(
            _config["admin_user_id"],
            f"📥 新 Issue #{issue_data['number']}: {issue_data['title']}\n"
            f"🏷 自动标签: {label_str}\n"
            f"💬 已在 Issue 下发表智能回复\n"
            f"📋 仓库: {repo_name}\n"
            f"💬 回复 /gh {task_id} auto 启动自动修复"
        )

        return web.json_response({"status": "ok", "task_id": task_id})

    return web.json_response({"status": "ignored"})
```

**评论效果示意**（在 GitHub Issue 页面显示）：

> 🤖 **Sirius GitHub Agent 自动回复**
>
> 感谢提交 Issue #42：Login page crashes on empty password！
>
> 我理解问题是：当用户在登录页面不输入密码直接点击登录时，页面出现未处理异常导致崩溃。这是一个需要修复的 bug。
>
> 已自动分析并应用标签：`type:bug` `priority:high` `difficulty:easy` `area:core` `status:needs-triage`
>
> 管理员将尽快评估此 Issue。如果确认需要自动修复，我会立即开始工作。感谢你的反馈！

**关键设计决策**：
- **共享 LLM 分析**：标签分析 prompt 和评论生成 prompt 是可合并的（一次 API 调用输出 `{type, priority, ..., comment}`），但当前设计保持独立以降低耦合——标签失败不影响评论，反之亦然
- **模板降级**：LLM 调用失败时使用预设模板，确保 Issue 提交者至少收到一条确认回复
- **Markdown 格式**：评论支持 GitHub Flavored Markdown，可包含代码块、引用等
- **不阻塞主流程**：智能回复在 `try/except` 中执行，失败不影响后续的审批通知

---


### 3.2 节点②：人工干预与调度（Human-in-the-Loop）

**原始设计**：

> 拦截事件，向管理员发送格式化消息。
> 挂起任务状态为 PENDING_APPROVAL。
> 监听 /fix XX 指令，校验权限，更新状态为 APPROVED。

**框架匹配度**：✅ 完全可行。

**落地方案**：

```python
# commands.py
from sirius_pulse.plugins import command, PluginResponse

class GithubAgentPlugin(PluginBase):
    @command("gh", prefix="/", patterns=["/gh"], render_mode="direct")
    async def handle_gh_command(self, issue_id: str, action: str = "auto") -> PluginResponse:
        """处理管理员私聊指令"""
        # 校验调用者为 admin_user_id
        if self.ctx.message.user_id != self._admin_user_id:
            return PluginResponse.fail("权限不足")

        if action == "auto":
            # 从 PluginDataStore 读取事件数据
            task_data = self.ctx.data_store.get(f"task_{issue_id}")
            if task_data is None:
                return PluginResponse.fail(f"未找到 Issue #{issue_id}")
            # 标记为 APPROVED 并异步启动 agent_loop
            task_data["status"] = "APPROVED"
            self.ctx.data_store.set(f"task_{issue_id}", task_data)
            asyncio.create_task(self._run_agent_loop(task_data))
            return PluginResponse.ok(text=f"任务已启动：Issue #{issue_id}")

        return PluginResponse.fail(f"未知操作: {action}")
```

**关键实现点**：
- 权限校验：通过 `config()` 中配置的 `admin_user_id` 做字符串比对
- `@command` 的参数类型注解自动由框架解析并校验
- `asyncio.create_task` 启动后台 agent_loop，不阻塞指令响应

**adapter 持久引用问题**：
- `PluginContext.adapter` 在每次 `PluginExecutor.execute()` 调用时被注入
- agent_loop 是长期运行的后台任务，无法依赖这次性注入
- **解法**：在 `on_load()` 中保存 adapter 引用到 `self._adapter`（需框架在此时提供 adapter，或者通过 `PluginExecutor` 在 instantiate 后首次 execute 时保存）

更稳健的替代方案：在 `_run_agent_loop` 启动时从 `PluginExecutor` 重新获取 adapter（通过 `self.ctx.adapter` 在 execute 上下文中已可用），并将引用传递给 loop 内部。

---

### 3.3 节点③：工作区准备（Workspace Preparation）

**原始设计**：

> Fork → Merge Upstream → Clone → Checkout 新分支 `fix-issue-{id}`

**框架匹配度**：✅ 完全可行，全部为纯插件代码。

**落地方案**：

```python
# agent_loop.py 中的 prepare_workspace()
async def prepare_workspace(repo_name: str, issue_number: int, config: dict) -> Path:
    """准备本地工作区"""
    workspace_root = Path(config["workspace_dir"])
    task_dir = workspace_root / f"task_{issue_number}"  # 隔离目录

    async with httpx.AsyncClient(headers=github_headers(config)) as client:
        # 1. Fork（幂等：已 Fork 则跳过）
        await ensure_fork(client, repo_name, config["github_username"])

        # 2. Sync upstream
        await sync_fork(client, repo_name, config["github_username"])

    # 3. Clone（用 GitPython 异步包装）
    fork_url = f"https://{config['github_pat']}@github.com/{config['github_username']}/{repo_name}.git"
    await clone_repo(fork_url, task_dir)

    # 4. 创建并切换到 fix 分支
    repo = Repo(task_dir)
    fix_branch = f"fix-issue-{issue_number}"
    repo.git.checkout("-b", fix_branch)

    return task_dir
```

**安全要求**：
- Fork URL 中嵌入 PAT，日志输出时必须脱敏
- workspace_dir 默认 `data/github_workspace/`，确保被 `.gitignore` 覆盖
- 每个任务独立子目录（`task_{issue_number}`），自然隔离

---

### 3.4 节点④-⑤：信息检索与代码修改（Retrieval & Modification）

**原始设计**：暴露 4 个 SKILL 工具给大模型调用。

**框架匹配度**：✅ 可行。建议不用框架的 `SkillRegistry`，而采用 agent_loop 内部的私有工具函数。

**推荐方案：轻量 ToolRegistry**

```python
# skills.py — 工具定义与注册
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class ToolDef:
    """工具定义（不依赖框架 SkillRegistry）"""
    name: str
    description: str
    parameters: dict  # JSON Schema 格式
    handler: Callable[..., Awaitable[Any]]

class ToolRegistry:
    """Agent Loop 内部工具注册表"""
    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef):
        self._tools[tool.name] = tool

    def get_schema_list(self) -> list[dict]:
        """生成 OpenAI function calling 格式的工具列表"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
            }
            for t in self._tools.values()
        ]

    async def call(self, name: str, **kwargs) -> str:
        """调用工具并返回结果字符串"""
        tool = self._tools.get(name)
        if tool is None:
            return f"未知工具: {name}"
        try:
            result = await tool.handler(**kwargs)
            return str(result)
        except Exception as e:
            return f"工具执行失败: {e}"
```

**4 个工具实现**：

```python
# skills.py

async def search_content(keyword: str, directory: str = ".") -> str:
    """搜索关键词，返回文件路径与行号"""
    proc = await asyncio.create_subprocess_exec(
        "rg", "-n", "--no-heading", keyword, directory,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode not in (0, 1):  # rg returns 1 for no matches
        return f"搜索失败: {stderr.decode()}"
    return stdout.decode() or "未找到匹配结果"


async def read_file_chunk(file_path: str, start_line: int, end_line: int) -> str:
    """按行读取文件片段，防止超大文件撑爆 Token"""
    # 安全检查：确保文件在 workspace_dir 内
    _validate_path(file_path)
    lines = []
    async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if i > end_line:
                break
            if i >= start_line:
                lines.append(f"{i}:{line.rstrip()}")
    return "\n".join(lines)


async def search_and_replace_block(file_path: str, old_block: str, new_block: str) -> str:
    """精确字符串替换，校验唯一性"""
    _validate_path(file_path)
    content = Path(file_path).read_text(encoding="utf-8")
    count = content.count(old_block)
    if count == 0:
        return f"错误：未在 {file_path} 中找到目标代码块"
    if count > 1:
        # 列出所有匹配位置帮助模型提供更多上下文
        positions = _find_all_positions(content, old_block)
        return f"错误：目标代码块在 {file_path} 中出现 {count} 次（不唯一）。位置：{positions}。请提供更多上下文使匹配唯一。"
    new_content = content.replace(old_block, new_block, 1)
    Path(file_path).write_text(new_content, encoding="utf-8")
    return f"成功替换 {file_path} 中的代码块"


async def run_local_test(test_command: str) -> dict:
    """在沙盒中运行测试命令"""
    proc = await asyncio.create_subprocess_exec(
        *test_command.split(),
        cwd=str(_current_workspace),  # 上下文注入
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return {"success": False, "stdout": "", "stderr": "测试超时（>60秒）"}
    return {
        "success": proc.returncode == 0,
        "stdout": stdout.decode(),
        "stderr": stderr.decode(),
    }
```

**路径安全校验**：

```python
# skills.py

_workspace_root: Path | None = None  # 由 agent_loop 在启动时设置

def _validate_path(file_path: str) -> None:
    """确保文件路径在 workspace 内，防止路径穿越攻击"""
    resolved = Path(file_path).resolve()
    if _workspace_root is None:
        raise RuntimeError("工作区未初始化")
    if not str(resolved).startswith(str(_workspace_root.resolve())):
        raise PermissionError(f"禁止访问工作区外的文件: {file_path}")
```

---

### 3.5 节点⑥：沙盒验证与自修复循环（Validation & Self-Healing Loop）

**原始设计**：

> 运行静态检查（flake8）+ 测试（pytest） → 失败则喂 stderr 给 LLM → 回到节点④继续修改 → MAX_RETRIES=3 上限

**框架匹配度**：✅ 完全可行。此为 agent_loop.py 内部逻辑。

**核心流程**（两阶段验证：先静态检查通过后才跑测试）：

```python
# agent_loop.py

async def agentic_loop(issue_data: dict, workspace_dir: Path, tool_registry: ToolRegistry) -> str:
    """核心自治修复循环"""
    MAX_RETRIES = 3

    # 构建初始 prompt
    system_prompt = build_system_prompt(tool_registry, workspace_dir)
    user_message = f"Issue #{issue_data['number']}: {issue_data['title']}\n\n{issue_data['body']}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    for attempt in range(1, MAX_RETRIES + 1):
        # 调用大模型（带工具定义）
        response = await call_llm_with_tools(messages, tool_registry)

        # 处理 tool calls
        if response.tool_calls:
            for tc in response.tool_calls:
                result = await tool_registry.call(tc.name, **tc.arguments)
                messages.append({"role": "assistant", "content": None, "tool_calls": [...]})
                messages.append({"role": "tool", "content": result, "tool_call_id": tc.id})
            continue  # 回到 LLM 继续思考

        # 没有 tool calls → LLM 认为修改完成 → 两阶段验证
        # 阶段①：静态检查（flake8）
        lint_result = await run_local_test("flake8 .")
        if not lint_result["success"]:
            if attempt < MAX_RETRIES:
                messages.append({
                    "role": "user",
                    "content": f"静态检查失败（第{attempt}次）：\n{lint_result['stderr']}\n请修复代码风格/语法问题。"
                })
                continue
            else:
                return "MAX_RETRIES_EXCEEDED"

        # 阶段②：单元测试（pytest）
        test_result = await run_local_test("pytest")
        if test_result["success"]:
            return "TESTS_PASSED"  # 成功退出

        # 测试失败：将 stderr 喂回 LLM
        if attempt < MAX_RETRIES:
            messages.append({
                "role": "user",
                "content": f"测试失败（第{attempt}次）：\n{test_result['stderr']}\n请分析错误并修复。"
            })
        else:
            return "MAX_RETRIES_EXCEEDED"  # 达到上限

    return "MAX_RETRIES_EXCEEDED"
```

**System Prompt 策略**（`build_system_prompt`）：

```
你是一名资深软件工程师，正在通过 tool calling 修复一个 GitHub Issue。

工作区路径：{workspace_dir}

你可以使用以下工具：
- search_content(keyword, directory)：全局搜索关键词
- read_file_chunk(file_path, start_line, end_line)：按行读取文件
- search_and_replace_block(file_path, old_block, new_block)：精确替换代码块
- run_local_test(test_command)：运行测试（如 pytest）

工作流程：
1. 先用 search_content 定位相关代码
2. 用 read_file_chunk 查看上下文
3. 用 search_and_replace_block 进行修改
4. 修改完毕后先运行 run_local_test("flake8 .") 做静态检查
5. 静态检查通过后运行 run_local_test("pytest") 做单元测试
6. 如果任何检查失败，分析错误并继续修改
```

---

### 3.5.1 实时交互可视化：独立 CMD 控制台窗口（Live Console Viewer）

**设计目标**：当自动修复流启动后，弹出一个独立的 Windows CMD 控制台窗口，实时滚动展示 AI 的思考过程、工具调用参数与返回值、测试结果，让管理员可以直观地观察 AI 的决策链路。

**设计原则**：
- **非侵入**：控制台窗口仅做展示，不参与流程控制。agent_loop 的决策不受 viewer 影响。
- **独立生命周期**：viewer 窗口由 agent_loop 启动时创建，修复完成（或失败）后 30 秒自动关闭，也可由管理员手动关闭。
- **Windows 原生**：利用 `subprocess.CREATE_NEW_CONSOLE` 创建真正的 CMD 窗口，不依赖 Web UI。

**技术方案**：

采用 **临时日志文件 + 独立 Python viewer 进程** 架构：

```
agent_loop.py                         console_viewer.py
     │                                      │
     │ 写入结构化事件                          │ 轮询读取（0.1s 间隔）
     │   → 写入行                            │   → 解析 + ANSI 着色
     │   → f.flush()                         │   → 打印到控制台
     │                                      │
     ├─→ logs/agent_{task_id}.stream ──────→┤
     │                                      │
     │ 修复完成写入 [END]                      │ 读到 [END] → 倒计时 30s → 退出
     ▼                                      ▼
```

**事件日志格式**（每行一个 JSON 事件）：

```python
# 事件行格式
# {"ts": 1700000000.123, "type": "<event_type>", "data": {...}}
#
# 事件类型：
#   phase        — 阶段切换（PREPARATION / ANALYSIS / MODIFICATION / VALIDATION / COMMIT）
#   think        — LLM 推理文本（流式累积或最终输出）
#   tool_call    — 工具调用开始（name + 参数）
#   tool_result  — 工具调用结果
#   test_run     — 测试执行（命令 + 结果）
#   retry        — 重试触发
#   error        — 错误
#   done         — 修复完成（成功/失败）
```

**agent_loop.py 中的事件写入**：

```python
# stream_writer.py — 事件流写入器

import json
import time
from pathlib import Path
from typing import Any


class StreamWriter:
    """向临时日志文件写入结构化事件，供 console_viewer.py 消费。"""

    def __init__(self, stream_file: Path) -> None:
        self._file = stream_file
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._file.open("w", encoding="utf-8", buffering=1)  # 行缓冲

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        line = json.dumps({"ts": time.time(), "type": event_type, "data": data}, ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()

    def phase(self, name: str, detail: str = "") -> None:
        """输出阶段切换标记。"""
        self._emit("phase", {"name": name, "detail": detail})

    def think(self, text: str) -> None:
        """输出 LLM 推理片段。大段文本建议分段写入。"""
        self._emit("think", {"text": text})

    def tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        """输出工具调用开始。"""
        self._emit("tool_call", {"name": name, "arguments": arguments})

    def tool_result(self, name: str, result: str, success: bool = True) -> None:
        """输出工具调用结果。结果过长时截断。"""
        truncated = result[:500] + ("..." if len(result) > 500 else "")
        self._emit("tool_result", {"name": name, "result": truncated, "success": success})

    def test_run(self, command: str, success: bool, stdout: str, stderr: str) -> None:
        """输出测试执行结果。"""
        self._emit("test_run", {
            "command": command,
            "success": success,
            "stdout": stdout[:300],
            "stderr": stderr[:300],
        })

    def retry(self, attempt: int, max_retries: int, reason: str) -> None:
        """输出重试信息。"""
        self._emit("retry", {"attempt": attempt, "max_retries": max_retries, "reason": reason})

    def error(self, message: str) -> None:
        """输出错误。"""
        self._emit("error", {"message": message})

    def done(self, success: bool, summary: str, pr_url: str = "") -> None:
        """输出最终结果。viewer 读取到此事件后进入倒计时退出。"""
        self._emit("done", {"success": success, "summary": summary, "pr_url": pr_url})

    def close(self) -> None:
        self._fh.close()
```

**console_viewer.py — 独立控制台渲染进程**：

```python
#!/usr/bin/env python3
"""GitHub Agent 实时交互流查看器。

在独立 CMD 窗口中运行，轮询读取 agent 写入的事件流文件，
以 ANSI 着色格式实时展示 AI 的思考与操作过程。

用法：python console_viewer.py <stream_file> [--keep-open]
"""

import json
import os
import sys
import time
from pathlib import Path

# ── ANSI 颜色常量 ──
C_RESET   = "\033[0m"
C_BOLD    = "\033[1m"
C_DIM     = "\033[2m"
C_CYAN    = "\033[36m"
C_GREEN   = "\033[32m"
C_YELLOW  = "\033[33m"
C_RED     = "\033[31m"
C_MAGENTA = "\033[35m"
C_BLUE    = "\033[34m"
C_WHITE   = "\033[37m"


def _ts() -> str:
    """生成时间戳字符串。"""
    return time.strftime("%H:%M:%S")


def _print_phase(name: str, detail: str) -> None:
    phase_map = {
        "PREPARATION":  (f"{C_CYAN}🔧 工作区准备", C_CYAN),
        "ANALYSIS":     (f"{C_YELLOW}🔍 代码分析",   C_YELLOW),
        "MODIFICATION": (f"{C_MAGENTA}✏️  代码修改", C_MAGENTA),
        "VALIDATION":   (f"{C_BLUE}🧪 测试验证",     C_BLUE),
        "COMMIT":       (f"{C_GREEN}📦 提交与PR",    C_GREEN),
    }
    label, color = phase_map.get(name, (name, C_WHITE))
    print(f"\n{C_BOLD}{'═'*60}{C_RESET}")
    print(f"{C_BOLD}{label}{C_RESET}  {C_DIM}{_ts()}{C_RESET}")
    if detail:
        print(f"  {color}{detail}{C_RESET}")
    print(f"{C_BOLD}{'─'*60}{C_RESET}")


def _print_think(text: str) -> None:
    indent = "  "
    for line in text.split("\n"):
        # 截断超长行
        if len(line) > 120:
            line = line[:117] + "..."
        print(f"{C_DIM}{indent}💭 {line}{C_RESET}")


def _print_tool_call(name: str, arguments: dict) -> None:
    args_str = ", ".join(f"{k}={repr(v)[:60]}" for k, v in arguments.items())
    print(f"  {C_YELLOW}⚙ 调用工具: {name}({args_str}){C_RESET}")


def _print_tool_result(name: str, result: str, success: bool) -> None:
    icon = "✅" if success else "❌"
    color = C_GREEN if success else C_RED
    print(f"  {color}{icon} {name} 返回:{C_RESET}")
    for line in result.split("\n")[:10]:
        print(f"     {C_DIM}{line[:120]}{C_RESET}")


def _print_test_run(command: str, success: bool, stdout: str, stderr: str) -> None:
    if success:
        print(f"  {C_GREEN}✅ {command} — 全部通过{C_RESET}")
    else:
        print(f"  {C_RED}❌ {command} — 测试失败{C_RESET}")
        for line in stderr.split("\n")[:5]:
            if line.strip():
                print(f"     {C_RED}{line[:120]}{C_RESET}")


def _print_retry(attempt: int, max_retries: int, reason: str) -> None:
    print(f"\n  {C_YELLOW}🔄 第 {attempt}/{max_retries} 次重试{C_RESET}")
    print(f"  {C_DIM}原因: {reason[:200]}{C_RESET}")


def _print_error(message: str) -> None:
    print(f"  {C_RED}⚠ 错误: {message[:300]}{C_RESET}")


def _print_done(success: bool, summary: str, pr_url: str) -> None:
    print(f"\n{C_BOLD}{'═'*60}{C_RESET}")
    if success:
        print(f"{C_GREEN}{C_BOLD}✅ 修复完成！{C_RESET}")
    else:
        print(f"{C_RED}{C_BOLD}❌ 修复失败{C_RESET}")
    if summary:
        print(f"  {summary}")
    if pr_url:
        print(f"  🔗 {C_BLUE}{pr_url}{C_RESET}")
    print(f"{C_BOLD}{'═'*60}{C_RESET}")


# ── 事件处理器注册表 ──
_EVENT_HANDLERS = {
    "phase":       lambda d: _print_phase(d["name"], d.get("detail", "")),
    "think":       lambda d: _print_think(d["text"]),
    "tool_call":   lambda d: _print_tool_call(d["name"], d["arguments"]),
    "tool_result": lambda d: _print_tool_result(d["name"], d["result"], d.get("success", True)),
    "test_run":    lambda d: _print_test_run(d["command"], d["success"], d.get("stdout", ""), d.get("stderr", "")),
    "retry":       lambda d: _print_retry(d["attempt"], d["max_retries"], d.get("reason", "")),
    "error":       lambda d: _print_error(d["message"]),
    "done":        lambda d: _print_done(d["success"], d.get("summary", ""), d.get("pr_url", "")),
}


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python console_viewer.py <stream_file> [--keep-open]")
        sys.exit(1)

    stream_path = Path(sys.argv[1])
    keep_open = "--keep-open" in sys.argv

    print(f"{C_BOLD}{C_CYAN}╔══════════════════════════════════════════════════════════╗{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}║        Sirius Pulse — GitHub Agent 实时交互流            ║{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}╚══════════════════════════════════════════════════════════╝{C_RESET}")
    print(f"{C_DIM}  等待 AI 开始工作...{C_RESET}\n")

    # 等待流文件创建（最多等待 30 秒）
    waited = 0
    while not stream_path.exists() and waited < 30:
        time.sleep(0.5)
        waited += 0.5

    if not stream_path.exists():
        print(f"{C_RED}等待超时：流文件未创建 ({stream_path}){C_RESET}")
        if not keep_open:
            time.sleep(3)
        else:
            input("按回车键关闭...")
        return

    last_pos = 0
    done_received = False

    try:
        while True:
            try:
                content = stream_path.read_text(encoding="utf-8")
            except Exception:
                time.sleep(0.1)
                continue

            lines = content.splitlines()[last_pos:]
            last_pos = len(content.splitlines())

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    event_type = event.get("type", "")
                    handler = _EVENT_HANDLERS.get(event_type)
                    if handler:
                        handler(event.get("data", {}))
                except json.JSONDecodeError:
                    pass

                if event.get("type") == "done":
                    done_received = True

            if done_received:
                break

            time.sleep(0.1)

    except KeyboardInterrupt:
        pass

    if not keep_open and done_received:
        print(f"\n{C_DIM}窗口将在 30 秒后自动关闭（按 Ctrl+C 立即关闭）...{C_RESET}")
        try:
            time.sleep(30)
        except KeyboardInterrupt:
            pass
    else:
        input(f"\n{C_DIM}按回车键关闭窗口...{C_RESET}")


if __name__ == "__main__":
    main()
```

**在 agent_loop 中集成**：

```python
# agent_loop.py 中启动 console viewer 的关键代码

import subprocess
import sys
import tempfile
from pathlib import Path


def _launch_console_viewer(stream_file: Path, keep_open: bool = False) -> subprocess.Popen | None:
    """在独立 CMD 窗口中启动 console_viewer.py。

    仅在 Windows 上生效。失败时静默返回 None（不阻塞修复流程）。
    """
    if sys.platform != "win32":
        return None  # 仅 Windows 支持独立 CMD 窗口

    viewer_script = Path(__file__).resolve().parent / "console_viewer.py"
    if not viewer_script.exists():
        return None

    try:
        args = ["cmd", "/c", "start", '"Sirius GitHub Agent"',
                "python", str(viewer_script), str(stream_file)]
        if keep_open:
            args.append("--keep-open")
        proc = subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return proc
    except Exception as exc:
        logger.warning("无法启动 console viewer: %s", exc)
        return None


async def run_agent_loop(task_data: dict, config: dict, ...) -> str:
    """完整的 agent 修复管线，带实时控制台输出。"""

    task_id = task_data["task_id"]

    # 1. 创建流文件并启动 console viewer
    stream_file = Path(config["workspace_dir"]) / "logs" / f"agent_{task_id}.stream"
    stream = StreamWriter(stream_file)

    if config.get("console_viewer_enabled", True):
        _launch_console_viewer(stream_file, keep_open=config.get("console_viewer_keep_open", False))

    try:
        # 2. 准备阶段
        stream.phase("PREPARATION", f"Issue #{task_data['issue_number']}: {task_data['issue_title']}")
        workspace_dir = await prepare_workspace(...)
        stream.phase("PREPARATION", f"工作区: {workspace_dir}")

        # 3. 分析 + 修改 + 验证循环
        stream.phase("ANALYSIS", "开始代码检索与定位...")

        for attempt in range(1, config["max_retries"] + 1):
            response = await call_llm_with_tools(messages, tool_registry)

            # 写入 LLM 思考内容
            if response.thinking:
                stream.think(response.thinking)
            if response.content:
                stream.think(response.content)

            if response.tool_calls:
                for tc in response.tool_calls:
                    stream.tool_call(tc.name, tc.arguments)
                    result = await tool_registry.call(tc.name, **tc.arguments)
                    stream.tool_result(tc.name, result, success=True)
                continue

            # 修改完成 → 两阶段验证（flake8 + pytest）
            stream.phase("VALIDATION", f"第 {attempt} 轮验证")

            # 阶段①：静态检查
            lint_result = await run_local_test("flake8 .")
            stream.test_run("flake8 .", lint_result["success"], lint_result.get("stdout", ""), lint_result.get("stderr", ""))
            if not lint_result["success"]:
                if attempt < config["max_retries"]:
                    stream.retry(attempt, config["max_retries"], lint_result.get("stderr", ""))
                    continue
                else:
                    stream.error("静态检查未通过，已达重试上限")
                    stream.done(success=False, summary="flake8 检查未通过")
                    return "MAX_RETRIES_EXCEEDED"

            # 阶段②：单元测试
            test_result = await run_local_test(config["test_command"])
            stream.test_run(
                config["test_command"],
                test_result["success"],
                test_result.get("stdout", ""),
                test_result.get("stderr", ""),
            )

            if test_result["success"]:
                stream.phase("COMMIT", "测试通过，准备提交...")
                break

            if attempt < config["max_retries"]:
                stream.retry(attempt, config["max_retries"], test_result.get("stderr", ""))
            else:
                stream.error(f"达到最大重试次数 ({config['max_retries']})，修复失败")
                stream.done(success=False, summary="测试未通过，已达重试上限")
                return "MAX_RETRIES_EXCEEDED"

        # 4. 提交与 PR
        pr_url = await finalize_and_create_pr(workspace_dir, ...)
        stream.done(success=True, summary=f"PR 已创建", pr_url=pr_url)
        return "SUCCESS"

    except Exception as exc:
        stream.error(str(exc))
        stream.done(success=False, summary=f"异常终止: {exc}")
        raise

    finally:
        stream.close()
```

**控制台窗口效果示意**：

```
╔══════════════════════════════════════════════════════════╗
║        Sirius Pulse — GitHub Agent 实时交互流            ║
╚══════════════════════════════════════════════════════════╝
  等待 AI 开始工作...

══════════════════════════════════════════════════
🔧 工作区准备  14:32:05
  Issue #42: Login page crashes on empty password
──────────────────────────────────────────────────────

══════════════════════════════════════════════════
🔍 代码分析  14:32:12
──────────────────────────────────────────────────────
  💭 首先搜索登录页面相关的代码...
  ⚙ 调用工具: search_content(keyword='login', directory='.')
  ✅ search_content 返回:
     src/auth/login.py:15:def handle_login(password):
     src/auth/login.py:22:    if len(password) == 0:

══════════════════════════════════════════════════
✏️ 代码修改  14:32:20
──────────────────────────────────────────────────────
  💭 发现问题：handle_login 没有对空密码做防护...
  ⚙ 调用工具: read_file_chunk(file_path='src/auth/login.py', start_line=15, end_line=30)
  ✅ read_file_chunk 返回:
     15:def handle_login(password):
     16:    hashed = hash_password(password)
     ...

  ⚙ 调用工具: search_and_replace_block(file_path='src/auth/login.py', ...)
  ✅ search_and_replace_block 返回:
     成功替换 src/auth/login.py 中的代码块

══════════════════════════════════════════════════
🧪 测试验证  14:32:28
──────────────────────────────────────────────────────
  ✅ pytest — 全部通过

══════════════════════════════════════════════════
📦 提交与PR  14:32:35
──────────────────────────────────────────────────────

══════════════════════════════════════════════════
✅ 修复完成！
  PR 已创建
  🔗 https://github.com/example/repo/pull/128
══════════════════════════════════════════════════

窗口将在 30 秒后自动关闭...
```

**关键设计决策**：
- **文件轮询而非 Socket**：文件方案避免端口管理，Windows 上 `CREATE_NEW_CONSOLE` 启动的进程无法轻易共享 socket fd
- **行缓冲 + flush**：确保 viewer 能实时读到最新事件（延迟 < 0.5 秒）
- **viewer 独立进程**：即使 agent_loop 崩溃，viewer 窗口也可以读取到错误事件后优雅退出
- **--keep-open 参数**：调试时使用，窗口不自动关闭，方便回溯查看
- **仅 Windows**：macOS/Linux 上没有等效的独立控制台窗口机制，可降级到 `tail -f` 终端命令

---

### 3.6 节点⑦：提交与闭环（Commit & PR）

**原始设计**：

> git add . → git commit → git push → 创建 PR → 通知管理员 → 清理工作区

**框架匹配度**：✅ 完全可行。

```python
# agent_loop.py

async def finalize_and_create_pr(
    workspace_dir: Path,
    repo_name: str,
    issue_number: int,
    config: dict,
    adapter,        # 用于发送私聊通知
    admin_user_id: str,
) -> None:
    """提交代码并创建 Pull Request"""

    # 1. Git 提交
    repo = Repo(workspace_dir)
    repo.git.add(".")
    repo.index.commit(f"Auto-fix issue #{issue_number}")
    repo.git.push("origin", f"fix-issue-{issue_number}")

    # 2. 生成 PR 内容（LLM 根据 diff 生成人类可读的 Changelog）
    pr_title = f"Fix #{issue_number}: {issue_data['title']}"
    diff_stat = repo.git.diff("main", "--stat")
    diff_full = repo.git.diff("main")  # 完整 diff（截断后给 LLM）
    changelog = await generate_changelog(diff_full[:6000], issue_data, engine_proxy)
    pr_body = (
        f"## 🤖 自动修复\n\n"
        f"### 变更摘要 (Changelog)\n{changelog}\n\n"
        f"### 文件变更统计\n```\n{diff_stat}\n```\n\n"
        f"Closes #{issue_number}"
    )

    # 3. 调用 GitHub API 创建 PR
    async with httpx.AsyncClient(...) as client:
        resp = await client.post(
            f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls",
            json={
                "title": pr_title,
                "body": pr_body,
                "head": f"{config['github_username']}:fix-issue-{issue_number}",
                "base": "main",
            },
        )
        pr_data = resp.json()
        pr_url = pr_data["html_url"]

    # 4. 通知管理员
    await adapter.send_private_message(
        admin_user_id,
        f"✅ 修复完成，请查阅 PR：{pr_url}"
    )

    # 5. 清理工作区
    shutil.rmtree(workspace_dir, ignore_errors=True)


async def generate_changelog(
    diff: str,
    issue_data: dict,
    engine_proxy: "EngineProxy",
) -> str:
    """使用 LLM 根据 git diff 生成人类可读的 Changelog。

    这是原始设计中 "PR 内容由模型根据修改内容自动生成一份简要的 Changelog" 的实现。
    """
    if not diff.strip():
        return "无文本变更（可能仅修改了二进制文件）。"

    prompt = f"""你是一个技术文档撰写者。请根据以下 git diff 生成一份简洁的中文 Changelog。

Issue: #{issue_data['number']} - {issue_data['title']}

要求：
1. 以要点列表形式列出每项变更（3-6 条为宜）
2. 每条包含：修改的文件（取 basename）、修改原因、影响
3. 使用 Markdown 格式（每行以 - 开头）
4. 不需要评价代码质量，只描述事实
5. 禁止输出 JSON，直接输出 Markdown 要点

Git Diff:
{diff}
"""
    try:
        result = await engine_proxy.generate_text_analysis(prompt)
        return result.strip()
    except Exception:
        # LLM 调用失败时使用纯 diff stat 作为降级
        lines = []
        for line in diff.split("\n")[:20]:
            if line.startswith("diff --git") or line.startswith("---") or line.startswith("+++"):
                lines.append(f"- {line.split()[-1] if line.startswith('---') or line.startswith('+++') else line}")
        return "\n".join(lines) if lines else "（自动生成失败，请查看文件变更统计）"
```

---

### 3.7 并行工作流：PR 自动审阅（Automated PR Review）

**设计目标**：当有人向仓库提交 PR 时，agent 自动对 PR 进行代码审阅（Code Review），从正确性、安全性、代码风格、测试覆盖等维度给出评审意见，作为人工审阅的前置过滤器。

**触发时机**：Webhook 收到 `pull_request` 事件，`action` 为 `opened` 或 `synchronize`（PR 有新提交推送）。

**与自动修复流的关系**：
- 自动修复流（节点①→⑦）：对 **Issue** 做出反应，主动修改代码并提交 PR
- PR 审阅流（节点⑧）：对 **PR** 做出反应，只读审阅并发表评论，不修改代码
- 两者是**并行独立的触发链路**，合并在同一个 Webhook handler 中按 `X-GitHub-Event` 头分流

**审阅维度**：

| 维度 | 检查内容 | 严重程度 |
|---|---|---|
| **正确性** | 逻辑错误、边界条件遗漏、空指针风险 | 🔴 critical / 🟡 warning |
| **安全性** | SQL 注入、路径穿越、密钥硬编码、不安全的反序列化 | 🔴 critical |
| **代码风格** | 命名不规范、过长函数、过深嵌套 | 🔵 suggestion |
| **测试覆盖** | 新增代码无对应测试、测试用例不充分 | 🟡 warning |
| **性能** | 不必要的循环、同步阻塞调用、大对象拷贝 | 🔵 suggestion |
| **依赖** | 引入了不必要的重依赖、依赖版本锁定问题 | 🟡 warning |

**审阅深度控制**：

为避免 token 消耗过大和审阅耗时过长，采用分层策略：

| 层级 | 触发条件 | 审阅方式 |
|---|---|---|
| **快速扫描 (quick)** | 所有 PR 默认 | 仅扫描 PR diff，使用轻量分析模型，关注明显问题 |
| **深度审阅 (deep)** | 管理员通过 `/gh review <pr_number> deep` 手动触发 | 拉取完整 PR 分支到本地，运行静态分析 + 测试，使用强模型 |
| **增量审阅 (incremental)** | PR 有新提交推送 (`synchronize`) | 仅审阅本次新增的 diff 部分 |

**快速扫描落地方案**：

```python
# review.py — PR 自动审阅模块

async def auto_review_pr(
    pr_data: dict,
    repo_full_name: str,
    engine_proxy: "EngineProxy",
    config: dict,
    review_mode: str = "quick",
) -> dict:
    """对 PR 进行自动代码审阅。

    Args:
        pr_data: Webhook 中的 pull_request 对象
        repo_full_name: 仓库全名 (owner/repo)
        engine_proxy: 引擎代理，用于 LLM 调用
        config: 插件配置
        review_mode: "quick" | "deep" | "incremental"

    Returns:
        {"review_id": int, "comments": int, "summary": str, "verdict": str}
    """
    pr_number = pr_data["number"]
    pr_title = pr_data["title"]
    pr_body = pr_data.get("body", "")

    # 1. 获取 PR 的 diff
    async with httpx.AsyncClient(headers=github_headers(config)) as client:
        # GitHub PR diff API（Accept: application/vnd.github.v3.diff）
        diff_resp = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}",
            headers={
                **github_headers(config),
                "Accept": "application/vnd.github.v3.diff",
            },
        )
        diff_content = diff_resp.text
        if diff_resp.status_code != 200:
            logger.error("获取 PR #%d diff 失败: %d", pr_number, diff_resp.status_code)
            return {"error": f"获取 diff 失败: {diff_resp.status_code}"}

        # 获取 PR 的文件列表（用于上下文理解）
        files_resp = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/files",
            params={"per_page": 100},
        )
        files_data = files_resp.json() if files_resp.status_code == 200 else []

    # 2. 如果 diff 为空（如仅修改二进制文件），跳过
    if not diff_content.strip():
        logger.info("PR #%d diff 为空，跳过审阅", pr_number)
        return {"verdict": "skipped", "reason": "无可审阅的文本 diff"}

    # 3. 构建审阅 prompt
    files_summary = "\n".join(
        f"- {f['filename']} ({f['changes']} 行变更, +{f['additions']}/-{f['deletions']})"
        for f in files_data[:30]  # 防止文件列表过长
    )

    # 截断 diff 防止超 token（快速扫描限制 ~8000 字符）
    diff_truncated = diff_content[:8000]
    if len(diff_content) > 8000:
        diff_truncated += f"\n... (diff 被截断，原始大小 {len(diff_content)} 字符)"

    system_prompt = f"""你是一名资深代码审阅者（Code Reviewer）。请对以下 Pull Request 进行审阅。

审阅规则：
1. 按维度分类问题：正确性 (correctness)、安全性 (security)、风格 (style)、测试 (testing)、性能 (performance)
2. 每条问题给出：严重程度 (critical/warning/suggestion)、涉及文件、行号、问题描述、修改建议
3. 不要对无关紧要的风格差异吹毛求疵
4. 如果 diff 中没有明显问题，诚实地说"未发现明显问题"
5. 输出严格的 JSON 格式

PR 信息：
- 标题: {pr_title}
- 描述: {pr_body[:2000]}

变更文件列表:
{files_summary}

DIFF:
{diff_truncated}

请输出 JSON（不要 Markdown 代码块包裹）:
{{
    "verdict": "approve|comment|request_changes",
    "summary": "审阅摘要（1-3句中文）",
    "issues": [
        {{
            "severity": "critical|warning|suggestion",
            "category": "correctness|security|style|testing|performance|dependency",
            "file": "文件路径",
            "line": 行号或null,
            "title": "问题简述",
            "description": "详细描述",
            "suggestion": "修改建议"
        }}
    ]
}}
"""
    # 4. 调用 LLM 进行审阅
    if review_mode == "quick":
        result_text = await engine_proxy.generate_text_analysis(system_prompt)
    else:
        # 深度审阅使用更强的模型
        result_text = await engine_proxy.generate_text(system_prompt)

    try:
        review_result = json.loads(result_text.strip())
    except json.JSONDecodeError:
        logger.warning("PR 审阅 JSON 解析失败，降级为纯文本评论")
        review_result = {
            "verdict": "comment",
            "summary": "（自动审阅生成，原始输出非 JSON 格式）",
            "issues": [],
        }

    # 5. 将审阅结果提交为 PR Review
    async with httpx.AsyncClient(headers=github_headers(config)) as client:
        # 构建 Review Comment 正文
        body_lines = [f"🤖 **自动代码审阅**\n\n{review_result['summary']}\n"]

        issues = review_result.get("issues", [])
        if issues:
            body_lines.append(f"发现 {len(issues)} 个问题：\n")
            severity_emoji = {
                "critical": "🔴", "warning": "🟡", "suggestion": "🔵"
            }
            category_labels = {
                "correctness": "正确性", "security": "安全性", "style": "风格",
                "testing": "测试", "performance": "性能", "dependency": "依赖",
            }
            for i, issue in enumerate(issues, 1):
                sev = severity_emoji.get(issue.get("severity", "suggestion"), "⚪")
                cat = category_labels.get(issue.get("category", ""), issue.get("category", ""))
                body_lines.append(
                    f"\n**{i}. {sev} [{cat}] {issue.get('title', '')}**\n"
                    f"- 文件: `{issue.get('file', 'N/A')}`"
                )
                if issue.get("line"):
                    body_lines[-1] += f" (L{issue['line']})"
                body_lines.append(f"- 描述: {issue.get('description', '')}")
                if issue.get("suggestion"):
                    body_lines.append(f"- 建议: {issue.get('suggestion', '')}")
        else:
            body_lines.append("\n✅ 未发现明显问题。")

        # 按 verdict 选择 Review 事件类型
        verdict = review_result.get("verdict", "comment")
        event_map = {
            "approve": "APPROVE",
            "comment": "COMMENT",
            "request_changes": "REQUEST_CHANGES",
        }
        review_event = event_map.get(verdict, "COMMENT")

        review_body = "\n".join(body_lines)

        review_resp = await client.post(
            f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/reviews",
            json={
                "body": review_body,
                "event": review_event,
            },
        )
        if review_resp.status_code in (200, 201):
            review_data = review_resp.json()
            logger.info(
                "PR #%d 审阅完成: verdict=%s, issues=%d",
                pr_number, verdict, len(issues),
            )
            return {
                "review_id": review_data["id"],
                "verdict": verdict,
                "issues_count": len(issues),
                "summary": review_result["summary"],
            }
        else:
            logger.error("提交 PR Review 失败: %d %s", review_resp.status_code, review_resp.text)
            return {"error": f"提交 Review 失败: {review_resp.status_code}"}


async def post_inline_review_comments(
    repo_full_name: str,
    pr_number: int,
    commit_id: str,
    issues: list[dict],
    config: dict,
) -> int:
    """对 PR 的特定代码行发布行内评论（Inline Comment）。

    仅在深度审阅模式下使用，快速扫描使用 Review Summary 即可。

    Args:
        repo_full_name: 仓库全名
        pr_number: PR 编号
        commit_id: PR 的 head commit SHA
        issues: 审阅发现的问题列表（需包含 file, line, description, suggestion）
        config: 插件配置

    Returns:
        成功发布的评论数量
    """
    posted = 0
    async with httpx.AsyncClient(headers=github_headers(config)) as client:
        for issue in issues:
            if not issue.get("file") or not issue.get("line"):
                continue
            body = f"🤖 **{issue.get('title', '自动审阅')}**\n\n{issue.get('description', '')}"
            if issue.get("suggestion"):
                body += f"\n\n💡 建议: {issue['suggestion']}"
            try:
                resp = await client.post(
                    f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/comments",
                    json={
                        "body": body,
                        "commit_id": commit_id,
                        "path": issue["file"],
                        "line": issue["line"],
                        "side": "RIGHT",
                    },
                )
                if resp.status_code == 201:
                    posted += 1
            except Exception as exc:
                logger.warning("发布行内评论失败: %s", exc)
    return posted


async def has_existing_review(
    repo_full_name: str,
    pr_number: int,
    config: dict,
) -> bool:
    """检查 agent 是否已经对该 PR 提交过审阅。

    用于 synchronize 事件时判断是更新已有 Review 还是创建新 Review。
    """
    async with httpx.AsyncClient(headers=github_headers(config)) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/reviews",
            params={"per_page": 100},
        )
        if resp.status_code != 200:
            return False
        for review in resp.json():
            if "🤖 **自动代码审阅**" in review.get("body", ""):
                return True
    return False
```

**Webhook 中的 PR 审阅集成**：

```python
# webhook.py 的 webhook_handler 扩展

async def webhook_handler(request: web.Request) -> web.Response:
    event_type = request.headers.get("X-GitHub-Event", "")
    body = await request.json()

    # ── Issue 事件 ──
    if event_type == "issues" and body.get("action") == "opened":
        # ... 见 3.1.1 自动标签流程
        return web.json_response({"status": "ok", "event": "issue_opened"})

    # ── PR 事件（审阅流） ──
    if event_type == "pull_request":
        action = body.get("action", "")

        # 仅处理新 PR 或新提交推送
        if action not in ("opened", "synchronize"):
            return web.json_response({"status": "ignored", "reason": f"action={action}"})

        pr_data = body["pull_request"]
        repo_name = body["repository"]["full_name"]
        pr_number = pr_data["number"]

        # synchronize 时检查是否已有 Review，避免重复
        if action == "synchronize" and not await has_existing_review(repo_name, pr_number, _config):
            logger.info("PR #%d synchronize 但无已有 Review，执行增量审阅", pr_number)
            review_mode = "incremental"
        elif action == "synchronize":
            review_mode = "incremental"
        else:
            review_mode = "quick"

        # 异步执行审阅（不阻塞 Webhook 响应）
        asyncio.create_task(
            _run_pr_review(pr_data, repo_name, review_mode)
        )

        return web.json_response({
            "status": "ok",
            "event": "pr_review_triggered",
            "pr_number": pr_number,
            "review_mode": review_mode,
        })

    return web.json_response({"status": "ignored"})


async def _run_pr_review(pr_data: dict, repo_name: str, review_mode: str) -> None:
    """后台执行 PR 审阅并通知管理员。"""
    try:
        result = await auto_review_pr(
            pr_data, repo_name, _engine_proxy, _config, review_mode
        )
        if "error" in result:
            logger.error("PR #%d 审阅失败: %s", pr_data["number"], result["error"])
            return

        # 通知管理员
        pr_url = pr_data["html_url"]
        verdict_emoji = {"approve": "✅", "comment": "💬", "request_changes": "🔴"}
        emoji = verdict_emoji.get(result.get("verdict", ""), "🤖")

        await _adapter.send_private_message(
            _config["admin_user_id"],
            f"{emoji} PR #{pr_data['number']} 自动审阅完成\n"
            f"📋 {pr_data['title']}\n"
            f"📊 结论: {result.get('verdict', 'N/A')}（{result.get('issues_count', 0)} 个问题）\n"
            f"📝 摘要: {result.get('summary', '')}\n"
            f"🔗 {pr_url}"
        )
    except Exception as exc:
        logger.error("PR 审阅后台任务异常: %s", exc)
```

**手动触发深度审阅**：

```python
# commands.py 新增指令

@command("gh", prefix="/", patterns=["/gh review"], render_mode="direct")
async def handle_gh_review(self, pr_number: str, mode: str = "quick") -> PluginResponse:
    """管理员手动触发 PR 审阅。

    用法: /gh review <pr_number> [quick|deep]
    """
    if self.ctx.message.user_id != self._admin_user_id:
        return PluginResponse.fail("权限不足")

    try:
        pr_num = int(pr_number.lstrip("#"))
    except ValueError:
        return PluginResponse.fail(f"无效的 PR 编号: {pr_number}")

    if mode not in ("quick", "deep"):
        return PluginResponse.fail("审阅模式应为 quick 或 deep")

    # 通过 API 获取 PR 数据
    pr_data = await get_pr_data(self._config["repo"], pr_num, self._config)
    if pr_data is None:
        return PluginResponse.fail(f"未找到 PR #{pr_num}")

    asyncio.create_task(
        _run_pr_review(pr_data, self._config["repo"], mode)
    )
    return PluginResponse.ok(text=f"已启动 {mode} 模式审阅 PR #{pr_num}")
```

**关键设计决策**：
- **默认快速扫描**：所有 PR 自动触发，使用 `generate_text_analysis()`（轻量模型），降低 token 成本
- **深度审阅按需触发**：由管理员手动 `/gh review N deep` 启动，使用强模型 + 完整 diff + 行内评论
- **synchronize 增量**：PR 有新提交时仅审阅新增 diff，且检查是否已有 Review 避免刷屏
- **不阻塞 Webhook 响应**：审阅在 `asyncio.create_task` 中异步执行，Webhook 在 3 秒内返回 200
- **审阅结果为结构化 JSON**：便于后续扩展（如统计、趋势分析）
- **降级兼容**：LLM 输出非 JSON 时创建纯文本评论，不中断流程

---



```
plugins/github_agent/
├── __init__.py         # 插件入口：继承 PluginBase，注册生命周期
│                         on_load(): 注册 Webhook 路由、初始化 ToolRegistry、加载配置
│                         on_unload(): 取消后台任务、清理资源
│
├── config.py           # 插件配置模型（Pydantic + 持久化到 PluginDataStore）
│                         - github_pat: str
│                         - github_username: str
│                         - admin_user_id: str
│                         - workspace_dir: Path (默认 data/github_workspace/)
│                         - max_retries: int (默认 3)
│                         - test_command: str (默认 "pytest")
│                         - auto_label: bool (是否启用自动标签，默认 true)
│                         - auto_comment: bool (是否启用 Issue 智能回复，默认 true)
│                         - auto_review: bool (是否启用自动 PR 审阅，默认 true)
│                         - review_mode: str (默认审阅深度: "quick" | "deep"，默认 "quick")
│                         - webhook_secret: str (Webhook 签名密钥，可选)
│                         - console_viewer_enabled: bool (是否弹出独立 CMD 窗口，默认 true)
│                         - console_viewer_keep_open: bool (修复完成后是否保持窗口打开，默认 false)
│
├── api.py              # GitHub REST API 异步封装
│                         - get_issue(repo, issue_number) -> dict
│                         - get_pr(repo, pr_number) -> dict
│                         - get_pr_diff(repo, pr_number) -> str
│                         - get_pr_files(repo, pr_number) -> list[dict]
│                         - get_pr_reviews(repo, pr_number) -> list[dict]
│                         - fork_repo(repo, username) -> dict
│                         - sync_fork(repo, username) -> None
│                         - create_pr(repo, title, body, head, base) -> dict
│                         - create_review(repo, pr_number, body, event) -> dict
│                         - create_inline_comment(repo, pr_number, commit_id, path, line, body) -> dict
│                         - get_labels(repo) -> list[dict]
│                         - create_label(repo, name, color, description) -> dict
│                         - add_labels_to_issue(repo, issue_number, labels) -> dict
│                         - post_issue_comment(repo, issue_number, body) -> dict
│                         使用 httpx.AsyncClient，所有方法 async
│
├── webhook.py          # Webhook handler + 事件分发（合并原设计 events.py）
│                         （原设计自定义 GithubWebhookEvent → 现直接用 PluginEventType.CUSTOM）
│                         - parse_github_event(request) -> dict | None
│                         - verify_signature(payload, signature, secret) -> bool
│                         - webhook_handler(request) -> web.Response
│                         （按 X-GitHub-Event 分流到 Issue 流程 / PR 审阅流程）
│
├── labeler.py          # Issue 自动分类与标签模块
│                         - auto_label_issue(issue_data, repo, config, engine) -> list[str]
│                         - apply_labels_to_issue(repo, issue_number, labels, config) -> bool
│                         - _fallback_label_by_keywords(issue_data) -> list[str]
│                         - _label_metadata(label_name) -> (color, description)
│
├── commenter.py        # Issue 智能回复模块（在 GitHub Issue 下发表评论）
│                         - generate_issue_comment(issue_data, labels, repo, engine) -> str
│                         - post_issue_comment(repo, issue_number, body, config) -> bool
│
├── review.py           # PR 自动代码审阅模块
│                         - auto_review_pr(pr_data, repo, engine, config, mode) -> dict
│                         - post_inline_review_comments(repo, pr, commit, issues, config) -> int
│                         - has_existing_review(repo, pr_number, config) -> bool
│                         - _build_review_prompt(pr_data, files, diff) -> str
│
├── commands.py         # 私聊指令拦截（@command 装饰器）
│                         - /gh <task_id> auto: 启动自动修复
│                         - /gh <task_id> status: 查询任务状态
│                         - /gh <task_id> abort: 中止任务
│                         - /gh review <pr_number> [quick|deep]: 手动触发 PR 审阅
│
├── skills.py           # 本地代码操作工具（不在框架 SkillRegistry 中注册）
│                         - ToolDef / ToolRegistry（轻量工具封装）
│                         - search_content(keyword, directory) -> str
│                         - read_file_chunk(file_path, start, end) -> str
│                         - search_and_replace_block(file_path, old, new) -> str
│                         - run_local_test(test_command) -> dict
│                         - _validate_path(file_path) 路径安全校验
│
├── agent_loop.py       # 核心后台任务：Agent 自我反思与修复管线
│                         - prepare_workspace(repo, issue_number, config) -> Path
│                         - build_system_prompt(tool_registry, workspace) -> str
│                         - call_llm_with_tools(messages, tool_registry, config)
│                         - agentic_loop(issue_data, workspace, tools) -> str
│                         - finalize_and_create_pr(workspace, repo, ...) -> None
│                         - _launch_console_viewer(stream_file) -> subprocess.Popen | None
│
├── stream_writer.py    # 事件流写入器（供 agent_loop 调用）
│                         - StreamWriter: 向临时日志文件写入结构化事件
│                         - 事件类型: phase / think / tool_call / tool_result
│                           / test_run / retry / error / done
│
├── console_viewer.py   # 独立 CMD 控制台渲染进程
│                         - 轮询读取 stream 文件，ANSI 着色实时打印
│                         - 支持 --keep-open 参数（调试用）
│                         - done 事件后自动倒计时 30s 退出
│
└── plugin.json         # 插件元数据（PluginLoader 自动发现）
                          - name: "github_agent"
                          - display_name: "GitHub Agent"
                          - description: "..."
                          - version: "1.0.0"
                          - dependencies: ["httpx", "GitPython", "aiofiles"]
```

---

## 五、框架层改动详情

### 5.1 改动文件：`sirius_pulse/webui/server_core.py`

**新增内容**：

```python
# 在 WebUIServer.__init__() 中新增
self._plugin_routes: dict[str, list[tuple[str, str, object]]] = {}
# 格式: {plugin_name: [(method, path, handler), ...]}

# 新增公开方法
def register_plugin_route(
    self,
    plugin_name: str,
    method: str,
    path: str,
    handler,
) -> None:
    """为插件注册自定义 HTTP 路由。

    插件在 on_load() 中调用此方法以注入 Webhook 等端点。
    路由前缀自动添加 /api/plugin/{plugin_name}/。

    Args:
        plugin_name: 插件名（用于路由隔离和卸载时清理）
        method: HTTP 方法（GET/POST/PUT/DELETE）
        path: 路由路径（不含前缀），如 "/webhook/github"
        handler: async callable(request) -> web.Response
    """
    if plugin_name not in self._plugin_routes:
        self._plugin_routes[plugin_name] = []

    full_path = f"/api/plugin/{plugin_name}{path}"
    method_upper = method.upper()

    if method_upper == "GET":
        self.app.router.add_get(full_path, handler)
    elif method_upper == "POST":
        self.app.router.add_post(full_path, handler)
    elif method_upper == "PUT":
        self.app.router.add_put(full_path, handler)
    elif method_upper == "DELETE":
        self.app.router.add_delete(full_path, handler)

    self._plugin_routes[plugin_name].append((method_upper, full_path, handler))
    LOG.info("Plugin [%s] 注册路由: %s %s", plugin_name, method_upper, full_path)

def unregister_plugin_routes(self, plugin_name: str) -> None:
    """卸载插件的所有自定义路由（在插件 on_unload 时调用）。"""
    routes = self._plugin_routes.pop(plugin_name, [])
    for _method, path, _handler in routes:
        # aiohttp 没有直接删除路由的 API，需要通过重新构建 router
        # 简单处理：标记为已删除（路由仍存在但 handler 会被置为返回 410 Gone）
        LOG.info("卸载 Plugin [%s] 路由: %s", plugin_name, path)
```

### 5.2 改动文件：`sirius_pulse/plugins/context.py`

在 `EngineProxy` 中新增方法，使插件可以访问 WebUI 服务器引用：

```python
# EngineProxy 新增
def register_webhook_route(self, method: str, path: str, handler) -> None:
    """向 WebUI 注册插件的自定义 HTTP 路由。"""
    if self._engine is None:
        raise RuntimeError("引擎未绑定")
    webui_server = getattr(self._engine, "_webui_server", None)
    if webui_server is None:
        raise RuntimeError("WebUI 服务器不可用")
    webui_server.register_plugin_route(
        plugin_name=self._plugin_name,
        method=method,
        path=path,
        handler=handler,
    )
```

### 5.3 改动量统计

| 文件 | 改动类型 | 预估行数 |
|---|---|---|
| `webui/server_core.py` | 新增 2 个方法 + 1 个属性 | +60 行 |
| `plugins/context.py` | EngineProxy 新增 1 个方法 | +13 行 |
| **合计** | | **~73 行** |

---

## 六、数据流总览

### 6.1 Issue 自动修复流（节点①→⑦）

```
                              ┌────────────────────────────────┐
                              │         GitHub Webhook          │
                              │  POST /api/plugin/github_agent  │
                              │       /webhook/github           │
                              └──────────────┬─────────────────┘
                                             │
                                    ┌────────▼────────┐
                                    │   webhook.py     │
                                    │  X-GitHub-Event  │
                                    │  = "issues"      │
                                    └────────┬────────┘
                                             │
                           ┌─────────────────┼─────────────────┬─────────────────┐
                           │                 │                 │                 │
                    ┌──────▼──────┐ ┌───────▼───────┐ ┌──────▼──────┐ ┌───────▼──────┐
                    │  labeler.py  │ │ commenter.py  │ │ PluginData  │ │   adapter    │
                    │ LLM 自动分类  │ │ LLM 智能回复   │ │   Store     │ │ 私聊通知管理员 │
                    │ 打标签到Issue │ │ 评论到Issue    │ │ task_{id}   │ │ 附带标签信息  │
                    └──────────────┘ └───────────────┘ │ PENDING     │ └──────┬───────┘
                                                       └─────────────┘        │
                                                      ┌────────▼────────┐
                                                      │ 管理员 /gh X auto │
                                                      │ → @command 拦截  │
                                                      │ → 标记 APPROVED  │
                                                      │ → create_task    │
                                                      └────────┬────────┘
                                                               │
                              ┌────────────────────────────────▼──────────────┐
                              │              agent_loop.py                    │
                              │                                              │
                              │  ① prepare_workspace()                       │
                              │     Fork → Clone → Branch                    │
                              │                                              │
                              │  ② agentic_loop()  ┌─────────────────┐      │
                              │     LLM + ToolRegistry│               │      │
                              │      search_content   │               │      │
                              │      read_file_chunk  │◄──────────┐   │      │
                              │      replace_block    │            │   │      │
                              │      run_local_test   │            │   │      │
                              │     └────────┬────────┘            │   │      │
                              │              │                     │   │      │
                              │     ┌────────▼────────┐           │   │      │
                              │     │ pytest pass?     │           │   │      │
                              │     │  YES → 退出      │           │   │      │
                              │     │  NO  → stderr →LLM───────────┘   │      │
                              │     └─────────────────┘                │      │
                              │              (max 3 retries)           │      │
                              │                                              │
                              │  ③ finalize_and_create_pr()                  │
                              │     git push → GitHub PR                     │
                              │     → adapter 通知管理员                      │
                              │     → 清理 workspace                         │
                              └────────────┬─────────────────────────────────┘
                                           │
                              ┌────────────▼────────────────────────────────┐
                              │     stream_writer.py  ──── 实时写入事件 ────►│
                              │     logs/agent_{id}.stream                  │
                              └────────────┬────────────────────────────────┘
                                           │
                              ┌────────────▼────────────────────────────────┐
                              │        🖥 独立 CMD 窗口                       │
                              │     console_viewer.py                       │
                              │     轮询读取 → ANSI 着色 → 实时打印            │
                              │     (管理员可视化观察 AI 决策全过程)            │
                              └─────────────────────────────────────────────┘
```

### 6.2 PR 自动审阅流（并行独立链路）

```
                              ┌────────────────────────────────┐
                              │         GitHub Webhook          │
                              │  POST /api/plugin/github_agent  │
                              │       /webhook/github           │
                              └──────────────┬─────────────────┘
                                             │
                                    ┌────────▼────────┐
                                    │   webhook.py     │
                                    │  X-GitHub-Event  │
                                    │  ="pull_request" │
                                    │ action=opened/   │
                                    │ synchronize      │
                                    └────────┬────────┘
                                             │
                              ┌──────────────▼──────────────┐
                              │      review.py               │
                              │                              │
                              │  获取 PR diff + 文件列表      │
                              │        │                     │
                              │  ┌─────▼─────┐              │
                              │  │ quick/deep │              │
                              │  │ 模式判定    │              │
                              │  └─────┬─────┘              │
                              │        │                     │
                              │  ┌─────▼──────────────────┐ │
                              │  │ LLM 审阅（结构化 JSON） │ │
                              │  │ 维度:                   │ │
                              │  │ 正确性/安全性/风格/测试 │ │
                              │  └─────┬──────────────────┘ │
                              │        │                     │
                              │  ┌─────▼──────────┐         │
                              │  │ verdict:        │         │
                              │  │ approve/comment │         │
                              │  │ /request_changes│         │
                              │  └─────┬──────────┘         │
                              └────────┼────────────────────┘
                                       │
                          ┌────────────┼────────────┐
                          │            │            │
                   ┌──────▼──────┐ ┌──▼────────┐ ┌─▼──────────┐
                   │ GitHub API   │ │ adapter   │ │ (deep模式) │
                   │ POST /reviews│ │ 通知管理员 │ │ 行内评论    │
                   └──────────────┘ └───────────┘ └────────────┘
```

### 6.3 手动触发的深度审阅

```
管理员 /gh review N deep
        │
        ▼
  commands.py → 校验权限 → 获取 PR 数据
        │
        ▼
  review.py → 拉取完整分支 → 本地静态分析 → LLM 深度审阅
        │
        ▼
  GitHub API → 创建 Review + 行内评论
        │
        ▼
  adapter → 通知管理员审阅结果
```

---

## 七、开发路径建议

考虑到阻塞项的存在和复杂度，建议按以下顺序实施：

### 阶段一：框架扩展（1 个文件）
1. 在 `webui/server_core.py` 中实现 `register_plugin_route()` / `unregister_plugin_routes()`
2. 在 `EngineProxy` 中暴露 `register_webhook_route()`
3. 写一个简单的测试插件验证路由注册功能

### 阶段二：插件骨架（3 个文件）
4. 创建 `plugins/github_agent/plugin.json` 和 `__init__.py` 骨架
5. 实现 `config.py`（Pydantic 模型 + PluginDataStore 持久化，包含 auto_label/auto_review/review_mode/webhook_secret 字段）
6. 验证插件能被 PluginLoader 正确发现和加载

### 阶段三：GitHub API 封装（1 个文件）
7. 实现 `api.py`（Fork / Sync / PR / Issue / Label / Review 全套 REST API 封装）
8. 用真实 GitHub 仓库验证每个 API 方法

### 阶段四：Webhook + 自动标签 + 智能回复（3 个文件）
9. 实现 `webhook.py`（Webhook handler + 事件分流 + 签名验证）
10. 实现 `labeler.py`（自动标签：LLM 分类 + 关键词降级 + 标签创建/应用）
11. 实现 `commenter.py`（智能回复：LLM 生成评论 + 模板降级 + 发表到 Issue）
12. 端到端验证：收到 Issue Webhook → 自动打标签 → 智能回复 → 通知管理员

### 阶段五：指令审批（1 个文件）
13. 实现 `commands.py`（`@command` /gh auto/status/abort、`/gh review` 指令）
14. 验证管理员私聊 → 指令 → 后台任务启动全链路

### 阶段六：代码操作工具（1 个文件）
15. 实现 `skills.py`（4 个工具 + ToolRegistry + 路径安全校验）
16. 写单元测试验证每个工具的正确性和安全性

### 阶段七：Agent 循环 + 实时控制台（3 个文件）
17. 实现 `stream_writer.py`（结构化事件写入器：phase/think/tool_call/tool_result/test_run/retry/error/done）
18. 实现 `console_viewer.py`（独立 CMD 窗口渲染进程：轮询读取 + ANSI 着色 + 倒计时自动关闭）
19. 实现 `agent_loop.py`（完整流程：准备工作区 → agentic_loop → 提交 PR，全程接入 StreamWriter）
20. 对接真实 LLM 测试 tool calling 的往返，验证控制台窗口实时展示

### 阶段八：PR 自动审阅（1 个文件）
21. 实现 `review.py`（快速扫描 + 深度审阅 + 增量审阅 + 行内评论）
22. 验证：收到 PR Webhook → 自动审阅 → Review 评论 → 管理员通知

### 阶段九：联调与加固
23. 全流程端到端测试（Issue 修复流 + PR 审阅流 + 控制台可视化）
24. 异常场景覆盖（Fork 失败、PR 冲突、LLM 幻觉、标签不存在、审阅重复、viewer 崩溃不影响主流程等）
25. 日志与遥测完善

---

## 八、风险与注意事项

| 风险 | 缓解措施 |
|---|---|
| LLM Tool Calling 格式兼容性 | `ToolRegistry.get_schema_list()` 生成标准 OpenAI function calling 格式；多 provider 兼容需测试 |
| Git 操作并发冲突 | 每个 Issue 独立 workspace 子目录（`task_{number}`），互不干扰 |
| Webhook 签名伪造 | 可选但强烈建议实现 HMAC-SHA256 签名验证（`config.webhook_secret`） |
| LLM 生成危险命令 | `run_local_test` 的 timeout=60 硬限制；subprocess 不走 shell=True |
| PAT 泄露 | 配置存储在 `plugins/_config.json`，确保该文件权限正确；日志禁止打印 PAT |
| 大量 token 消耗 | `read_file_chunk` 限制读取范围；System Prompt 保持简洁；优先使用 gpt-4o-mini 作为分析模型 |
| **自动标签误分类** | LLM 分类结果经 `type_map` / `priority_map` 白名单校验；失败时降级到关键词匹配，绝不阻塞主流程 |
| **智能回复不恰当** | LLM 输出仅描述事实 + 引导，不做承诺；失败时使用模板降级；评论标注 🤖 自动生成标识 |
| **PR 审阅误报** | 审阅结果按 severity 分级（critical/warning/suggestion），仅 critical 级别会 REQUEST_CHANGES；管理员始终保留最终决定权 |
| **审阅重复提交** | `synchronize` 事件先调用 `has_existing_review()` 检查；快速扫描防重入 |
| **Webhook 处理超时** | 审阅和修复均为 `asyncio.create_task` 异步执行；Webhook handler 在 3 秒内返回 200，避免 GitHub 重试风暴 |
| **标签不存在导致 API 失败** | `apply_labels_to_issue` 先检查仓库已有标签，不存在的自动创建（带颜色和描述） |
| **多 PR 同时审阅的资源竞争** | 审阅流程无状态（每次独立拉取 diff），自然支持并发 |
| **console_viewer 启动失败** | `_launch_console_viewer` 静默吞异常返回 None；agent_loop 不依赖 viewer 运行 |
| **stream 文件残留** | 流文件位于 `workspace_dir/logs/`，随 workspace 清理时一并删除（节点⑦） |
| **ANSI 颜色不兼容** | Windows 10 build 16257+ 原生支持 ANSI；低版本通过 `colorama.init()` 兜底 |
