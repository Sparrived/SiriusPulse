#!/usr/bin/env python3
"""
文档自动同步 Agent
主仓库 push 触发 → 积累 diff → LLM 判断 → 自动更新 docs 仓库 → 创建人格化 PR

核心机制：
- 用 `.docs-sync-state` 文件（由 GitHub Actions Cache 持久化）记录上次同步的 commit
- 小改动自动积累，等下一次大改动时一起处理
- PR 描述以「月白/Sirius」的猫娘人格撰写

必须的环境变量：
  DOCS_REPO_PAT    — 访问 docs 仓库的 GitHub PAT（repo 权限）
  LLM_API_KEY      — LLM API Key
  GITHUB_EVENT_BEFORE — GitHub Push Event 的 before SHA（用于首次运行/缓存丢失时）
可选环境变量：
  LLM_BASE_URL     — LLM API 地址（默认 https://api.openai.com/v1）
  LLM_MODEL        — 模型名（默认 gpt-4o-mini）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════
#  配置常量
# ═══════════════════════════════════════════════════════════════════

DOCS_REPO = "Sparrived/SiriusPulse-Docs"
DOCS_REPO_PAT = os.environ.get("DOCS_REPO_PAT", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", os.environ.get("DOCS_BOT_API_KEY", ""))
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
GITHUB_EVENT_BEFORE = os.environ.get("GITHUB_EVENT_BEFORE", "")

# Git 提交者（可通过环境变量自定义）
GIT_USER_NAME = os.environ.get("GIT_USER_NAME", "Sirius Docs Bot")
GIT_USER_EMAIL = os.environ.get("GIT_USER_EMAIL", "bot@sirius.pulse")

# 状态文件路径（在 GitHub Actions Cache 中持久化）
STATE_FILE = ".docs-sync-state"

# 小改动阈值（同时满足才判定为「小」）
SMALL_FILE_THRESHOLD = 3       # 变更文件数 ≤ 3
SMALL_LINE_THRESHOLD = 30      # diff 行数（+/- 合计）≤ 30

# Fork 模式：如果 DOCS_FORK 设置（如 "YourName/SiriusPulse-Docs"），
# 则 clone/push 到 fork，然后从 fork 向原仓库提跨仓库 PR。
DOCS_FORK = os.environ.get("DOCS_FORK", "")

# 积累 commit 数上限（防止积累无限增长）
MAX_ACCUMULATED_COMMITS = 20

# 代码路径 → 受影响的文档（路径相对于 docs 仓库根目录）
PATH_TO_DOCS: dict[str, set[str]] = {
    # 核心引擎 —— 前端 5 章在 architecture-overview，引擎细节在 engine-architecture
    "sirius_pulse/core/emotional_engine.py": {"guide/engine-architecture.md"},
    "sirius_pulse/core/engine_core.py":      {"guide/engine-architecture.md"},
    "sirius_pulse/core/pipeline.py":         {"guide/engine-architecture.md"},
    "sirius_pulse/core/bg_tasks.py":         {"guide/architecture-overview.md", "guide/engine-architecture.md"},
    "sirius_pulse/core/prompt_factory.py":   {"guide/architecture-overview.md"},
    "sirius_pulse/core/helpers.py":          {"guide/architecture-overview.md"},
    "sirius_pulse/core/brain.py":            {"api/brain-api.md"},
    "sirius_pulse/core/":                    {"guide/architecture-overview.md", "guide/engine-architecture.md"},

    # Provider
    "sirius_pulse/providers/":   {"reference/provider-config.md", "guide/architecture-overview.md"},

    # 配置
    "sirius_pulse/config/":      {"guide/configuration.md", "reference/global-config.md"},

    # 记忆系统
    "sirius_pulse/memory/":      {"guide/memory-system.md"},
    "sirius_pulse/session/":     {"guide/memory-system.md"},

    # 技能系统
    "sirius_pulse/skills/":      {"extensions/skill-overview.md", "api/skills-api.md", "extensions/skill-authoring.md"},

    # 平台适配
    "sirius_pulse/platforms/":   {"guide/platform-napcat.md"},

    # Embedding 微服务
    "sirius_pulse/embedding/":   {"guide/architecture-overview.md"},

    # 数据模型
    "sirius_pulse/models/":      {"api/plugins-api.md", "api/skills-api.md"},

    # 人格管理
    "sirius_pulse/persona_manager.py": {"guide/persona-system.md"},
    "sirius_pulse/persona_config.py":  {"reference/persona-config.md"},

    # WebUI
    "sirius_pulse/webui/":       {"reference/webui-api.md"},

    # Plugin 系统
    "sirius_pulse/plugins/":     {"extensions/plugin-overview.md", "api/plugins-api.md", "extensions/plugin-authoring.md"},

    # 顶层 API
    "sirius_pulse/__init__.py":  {"reference/python-api.md"},

    # CLI 入口
    "main.py":                   {"guide/architecture-overview.md"},

    # 项目配置
    "pyproject.toml":            {"guide/configuration.md", "reference/development.md"},
}

ALL_DOCS: set[str] = {v for vs in PATH_TO_DOCS.values() for v in vs}


# ═══════════════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════════════

def run(
    cmd: list[str],
    cwd: str | None = None,
    silent: bool = False,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """
    运行 shell 命令，返回 stdout。
    失败返回空字符串，不抛异常。
    timeout: 超时秒数（默认不限）
    env: 额外环境变量（合并到当前环境）
    """
    try:
        merged_env = None
        if env:
            merged_env = os.environ.copy()
            merged_env.update(env)
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd, check=False,
            timeout=timeout, env=merged_env,
        )
        if result.returncode != 0 and not silent:
            stderr = result.stderr.strip()[:500]
            print(f"  ⚠️ 命令失败 (exit {result.returncode}): {' '.join(cmd)}")
            if stderr:
                print(f"     {stderr}")
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ 命令超时 ({timeout}s): {' '.join(cmd)}")
        return ""
    except FileNotFoundError:
        print(f"  ⚠️ 命令不存在: {cmd[0]}")
        return ""
    except Exception as e:
        print(f"  ⚠️ 命令异常: {' '.join(cmd)}\n     {e}")
        return ""


def run_ok(cmd: list[str], cwd: str | None = None, timeout: int | None = None, env: dict[str, str] | None = None) -> bool:
    """
    运行 shell 命令，返回 True/False 表示是否成功。
    失败时自动打印错误信息。
    """
    try:
        merged_env = None
        if env:
            merged_env = os.environ.copy()
            merged_env.update(env)
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd, check=False,
            timeout=timeout, env=merged_env,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()[:500]
            print(f"  ⚠️ 命令失败 (exit {result.returncode}): {' '.join(cmd)}")
            if stderr:
                print(f"     {stderr}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ 命令超时 ({timeout}s): {' '.join(cmd)}")
        return False
    except FileNotFoundError:
        print(f"  ⚠️ 命令不存在: {cmd[0]}")
        return False
    except Exception as e:
        print(f"  ⚠️ 命令异常: {' '.join(cmd)}\n     {e}")
        return False


def parse_diff_stat(stat: str) -> tuple[int, int]:
    """解析 `git diff --stat` 输出，返回 (文件数, 变更行数)"""
    if not stat:
        return 0, 0
    lines = stat.strip().splitlines()
    if not lines:
        return 0, 0
    file_count = len(lines) - 1  # 最后一行是汇总
    total_lines = 0
    last = lines[-1]
    for match in re.finditer(r"(\d+)\s+(insertion|deletion)", last):
        total_lines += int(match.group(1))
    return max(file_count, 0), total_lines


def get_changed_files_since(ref: str) -> set[str]:
    """获取从 ref 到 HEAD 的变更文件列表"""
    out = run(["git", "diff", f"{ref}..HEAD", "--name-only"])
    return {f for f in out.splitlines() if f.strip()} if out else set()


def guess_affected_docs(changed_files: set[str]) -> set[str]:
    """根据变更文件推断可能受影响的文档"""
    affected: set[str] = set()
    for code_path, doc_set in PATH_TO_DOCS.items():
        if any(f.startswith(code_path) for f in changed_files):
            affected |= doc_set
    return affected if affected else ALL_DOCS


# ═══════════════════════════════════════════════════════════════════
#  同步状态管理（基于 GitHub Actions Cache 文件）
# ═══════════════════════════════════════════════════════════════════

def read_state() -> str | None:
    """
    读取上次同步的 commit SHA。
    状态文件在 GitHub Actions Cache 中持久化。
    """
    state_path = Path(STATE_FILE)
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        sha = data.get("last_synced_sha", "")
        return sha if sha and run(["git", "cat-file", "-t", sha]) == "commit" else None
    except (json.JSONDecodeError, OSError):
        return None


def write_state(sha: str) -> None:
    """记录同步进度到状态文件"""
    Path(STATE_FILE).write_text(
        json.dumps({"last_synced_sha": sha, "updated_at": sha}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  ✓ 状态已记录: {sha[:12]}")


def resolve_start_commit() -> tuple[str, str]:
    """
    确定本次检查的起始 commit。

    优先级：
    1. state 文件（上次成功同步的位置）——能跨多个 push 积累 diff
    2. GITHUB_EVENT_BEFORE（当前 push 事件的 before SHA）——首次运行或 cache 丢失
    3. HEAD~1 ——兜底

    返回 (commit_sha, 来源说明)
    """
    # 优先级 1：state 文件
    state_sha = read_state()
    if state_sha:
        return state_sha, "缓存状态"

    # 优先级 2：GITHUB_EVENT_BEFORE
    if GITHUB_EVENT_BEFORE and GITHUB_EVENT_BEFORE != "0" * 40:
        return GITHUB_EVENT_BEFORE, "Push Event before SHA"

    # 优先级 3：HEAD~1（首次运行或在本地测试）
    head_parent = run(["git", "rev-parse", "HEAD~1"])
    if head_parent:
        return head_parent, "HEAD 的父 commit（首次运行）"

    # 极端情况：只有 1 个 commit 的仓库
    root = run(["git", "rev-list", "--max-parents=0", "HEAD"])
    if root:
        return root, "根 commit"
    return "", ""


# ═══════════════════════════════════════════════════════════════════
#  diff 积累与阈值判断
# ═══════════════════════════════════════════════════════════════════

def get_individual_commit_stats(
    commits: list[str],
) -> list[dict]:
    """获取每个 commit 的变更统计，用于判断是否为小改动"""
    stats = []
    for sha in commits:
        stat = run(["git", "diff", f"{sha}^", sha, "--stat"])
        if not stat:
            stat = run(["git", "show", "--stat", sha])
        files, lines = parse_diff_stat(stat)
        stats.append({"sha": sha, "files": files, "lines": lines})
    return stats


def get_accumulated_diff(last_synced: str) -> str:
    """获取积累 diff（last_synced..HEAD），只包含实际代码文件"""
    return run(["git", "diff", f"{last_synced}..HEAD", "--", ".", ":!*.md", ":!.github", ":!*.json", ":!*.yaml", ":!*.toml", ":!*.lock", ":!*.txt", ":!*.cfg", ":!*.ini"])


def should_process(commits: list[str], stats: list[dict]) -> tuple[bool, str]:
    """
    决策是否需要触发 LLM 处理。
    触发条件（满足任一即可）：
    1. 存在单个「大改动」commit
    2. 积累 commit 数达到上限
    3. 累计文件数/行数超过双倍阈值
    """
    if not commits:
        return False, "没有新增 commit"

    for s in stats:
        if s["files"] > SMALL_FILE_THRESHOLD or s["lines"] > SMALL_LINE_THRESHOLD:
            return True, f"commit {s['sha'][:8]} 改动较大（{s['files']} 文件, {s['lines']} 行）"

    if len(commits) >= MAX_ACCUMULATED_COMMITS:
        return True, f"已积累 {len(commits)} 个 commit，达到处理上限"

    total_files = sum(s["files"] for s in stats)
    total_lines = sum(s["lines"] for s in stats)

    if total_files > SMALL_FILE_THRESHOLD * 2:
        return True, f"累计文件数 {total_files} 超过阈值"
    if total_lines > SMALL_LINE_THRESHOLD * 2:
        return True, f"累计变更行数 {total_lines} 超过阈值"

    return False, f"全是小改动（{len(commits)} commit, {total_files} 文件, {total_lines} 行），继续积累"


# ═══════════════════════════════════════════════════════════════════
#  LLM 调用
# ═══════════════════════════════════════════════════════════════════

def _llm_request(
    prompt: str,
    system_prompt: str | None = None,
    temperature: float = 0.1,
) -> str | None:
    """发送 LLM 请求，返回原始响应文本；失败返回 None"""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 16384,
    }

    data = json.dumps(body).encode()
    url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=180).read())
        content = resp["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?\s*", "", content.strip())
        content = re.sub(r"\s*```$", "", content)
        return content
    except Exception as e:
        print(f"  ❌ LLM 调用失败: {e}")
        return None


def call_llm_json(
    prompt: str,
    system_prompt: str | None = None,
    temperature: float = 0.1,
) -> dict:
    """调用 LLM 并返回解析后的 JSON"""
    content = _llm_request(prompt, system_prompt, temperature)
    if content is None:
        return {"error": "LLM 返回为空"}
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"  ❌ JSON 解析失败: {e}\n     原始内容: {content[:200]}")
        return {"error": f"JSON 解析失败: {e}"}


def call_llm_text(
    prompt: str,
    system_prompt: str | None = None,
    temperature: float = 0.1,
) -> str:
    """调用 LLM 并返回纯文本内容"""
    content = _llm_request(prompt, system_prompt, temperature)
    return content or ""


# ═══════════════════════════════════════════════════════════════════
#  第一步：LLM 研判是否需要更新文档
# ═══════════════════════════════════════════════════════════════════

LLM_JUDGE_SYSTEM_PROMPT = """你是一个专业的项目文档维护助手。你的任务是判断代码变更是否触发了文档更新需求。

判断规则：
- 接口签名变更 → 需要更新
- 配置字段新增/删除 → 需要更新
- 新增/删除模块 → 需要更新
- Provider 类型变更 → 需要更新
- 仅注释/测试/README 变更 → 不需要
- 仅内部重构（私有方法、无公开 API 变化）→ 不需要

必须输出严格 JSON，不要包含额外文本。"""


def judge_needs_update(changed_files: set[str], diff_summary: str) -> dict:
    """判断本次变更是否需要更新文档"""
    affected = guess_affected_docs(changed_files)

    prompt = f"""## 任务
判断本次代码变更是否需要更新文档。

## 变更文件列表
{chr(10).join(sorted(changed_files)) or "(无)"}

## 变更统计
{diff_summary}

## 可能受影响的文档
{chr(10).join(sorted(affected))}

## 输出格式（严格 JSON）
{{"needs_update": true/false, "reason": "一句话原因", "affected_docs": ["路径1", "路径2"]}}"""

    return call_llm_json(prompt, system_prompt=LLM_JUDGE_SYSTEM_PROMPT, temperature=0)


# ═══════════════════════════════════════════════════════════════════
#  第二步：LLM 自动更新单个文档
# ═══════════════════════════════════════════════════════════════════

DOC_UPDATE_SYSTEM_PROMPT = (
    "你是一个专业的文档维护助手。根据代码变更更新文档内容。"
    "保持原文档结构、标题层级、风格不变。"
    "只输出需要修改的片段，提供精确的旧文本和新文本，脚本会自动替换。"
    "必须输出严格 JSON。"
)


def update_single_doc(
    filepath: Path,
    diff_content: str,
    changed_files: set[str],
) -> str | None:
    """LLM 识别需要修改的片段，返回全文；None 表示无需修改"""
    old_content = filepath.read_text(encoding="utf-8")

    prompt = f"""## 任务
根据代码变更，找出文档中需要修改的片段，并给出替换内容。
**不要输出整个文档，只输出需要修改的片段。**

## 当前文档内容（{filepath.name}）
```markdown
{old_content[:12000]}
```

## 本次代码 diff
```diff
{diff_content[:15000]}
```

## 变更文件
{chr(10).join(sorted(changed_files))}

## 工作方式
1. 找出文档中因代码变更需要修改的文本片段
2. 每个片段提供 old_text（原文中精确的连续文本）和 new_text（替换后的文本）
3. 如果多处需要修改，提供多个片段
4. 无需修改时 modified 设为 false

## 输出格式（严格 JSON）
{{"modified": true/false, "patches": [{{"old_text": "原文中需要替换的精确文本", "new_text": "替换后的文本"}}, ...], "changes": ["改动点1", "改动点2"]}}"""

    result = call_llm_json(prompt, system_prompt=DOC_UPDATE_SYSTEM_PROMPT)

    if not result.get("modified") or not result.get("patches"):
        print(f"  - 无需修改: {filepath.name}")
        return None

    new_content = old_content
    changes = result.get("changes", [])
    patches = result["patches"]

    for i, patch in enumerate(patches):
        old_text = patch.get("old_text", "")
        new_text = patch.get("new_text", "")
        if not old_text:
            continue
        if old_text not in new_content:
            print(f"  ⚠️ 第 {i + 1} 处修改未找到匹配文本，跳过: {old_text[:60]}...")
            continue
        new_content = new_content.replace(old_text, new_text, 1)
        change_desc = changes[i] if i < len(changes) else f"片段 {i + 1}"
        print(f"  ✓ 已替换: {change_desc}")

    # 检查是否至少有一处真的被替换了
    if new_content == old_content:
        print(f"  - 无实际修改，跳过")
        return None

    return new_content


# ═══════════════════════════════════════════════════════════════════
#  第三步：生成人格化 PR 描述
# ═══════════════════════════════════════════════════════════════════

PERSONA_PR_SYSTEM_PROMPT = (
    "你是一只名叫「月白」(也叫 Sirius) 的猫娘，是 Sirius Pulse 项目的文档助手。"
    "你说话带喵，会用颜文字，温暖可爱但不失专业性。"
    "你的主人/创作者是「临雀大人」。"
    "请用月白的口吻撰写 GitHub PR 描述，既要可爱亲切，也要清晰说明文档更新内容。"
)


def generate_pr_description(
    commit_msgs: list[str],
    updated_docs: list[str],
    diff_summary: str,
    judge_reason: str,
) -> str:
    """让 LLM 用月白猫娘人格撰写 PR 描述"""
    prompt = f"""## 任务
用月白（一只猫娘）的口吻撰写 GitHub PR 描述。

## 本次触发的 commit 消息
{chr(10).join(f'- {m}' for m in commit_msgs)}

## 更新的文档
{chr(10).join(f'- {d}' for d in updated_docs)}

## 变更统计
```
{diff_summary}
```

## 触发原因
{judge_reason}

## 撰写要求
1. 以月白的身份撰写，句尾加「喵」，使用颜文字
2. 称呼用户/审阅者为「临雀大人」或「主人」
3. 说明哪些文档被更新了，更新了什么内容
4. 保持信息清晰完整，方便审阅者 review
5. 语气温暖可爱，不要太长（3-5 段即可）
6. 不要用 markdown 代码块包裹输出，直接输出纯文本"""

    body = call_llm_text(prompt, system_prompt=PERSONA_PR_SYSTEM_PROMPT, temperature=0.4)
    if not body:
        body = _fallback_pr_description(commit_msgs, updated_docs, diff_summary, judge_reason)
    return body


def _fallback_pr_description(
    commit_msgs: list[str],
    updated_docs: list[str],
    diff_summary: str,
    judge_reason: str,
) -> str:
    """备用 PR 描述——当 LLM 格式化失败时的模板"""
    return (
        f"## 喵~ 文档更新好啦 (ฅ´ω`ฅ)\n\n"
        f"临雀大人，月白发现代码有变动，已经帮你把文档更新好了喵~ ✨\n\n"
        f"**触发原因**：{judge_reason}\n\n"
        f"**更新的文档**：\n"
        + "\n".join(f"• {d}" for d in updated_docs)
        + "\n\n**触发 commit**：\n"
        + "\n".join(f"• {m}" for m in commit_msgs[:5])
        + "\n\n"
        + ("**变更统计**：\n```\n" + diff_summary + "\n```\n\n")
        + "月白已经检查过啦，你快看看有没有问题喵 (｡>ㅅ<｡)♡\n"
    )


# ═══════════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    print("=" * 60)
    print("🌟 文档自动同步 Agent 启动")
    print("=" * 60)
    print(f"   仓库: {DOCS_REPO}")
    print(f"   LLM: {LLM_MODEL}")

    # ── 1. 确定起始 commit ───────────────────────────────────
    last_synced, source_desc = resolve_start_commit()

    if not last_synced:
        print("\n❌ 无法确定起始 commit，跳过本次处理")
        print("   可能的解决方案：确保仓库至少有一个 commit")
        sys.exit(0)

    print(f"\n📌 起始点: {last_synced[:12]} ({source_desc})")

    # ── 2. 获取积累的 commit ────────────────────────────────
    commits = run(["git", "rev-list", f"{last_synced}..HEAD", "--reverse"]).splitlines()
    commits = [c.strip() for c in commits if c.strip()]

    if not commits:
        print("\nℹ️  没有新的 commit，跳过")
        # 没有新 commit 也要记录状态，避免重复检查
        write_state(run(["git", "rev-parse", "HEAD"]))
        return

    print(f"\n📁 积累的 commit ({len(commits)} 个):")
    for c in commits:
        msg = run(["git", "log", "--oneline", "-1", c])
        print(f"  • {msg}")

    # ── 3. 判断每个 commit 的改动量 ──────────────────────────
    stats = get_individual_commit_stats(commits)
    print("\n📊 每个 commit 的改动量:")
    for s in stats:
        flag = "🟢" if s["files"] <= SMALL_FILE_THRESHOLD and s["lines"] <= SMALL_LINE_THRESHOLD else "🔴"
        print(f"  {flag} {s['sha'][:8]} — {s['files']} 文件, {s['lines']} 行")

    # ── 4. 决策是否触发 LLM ─────────────────────────────────
    should, reason = should_process(commits, stats)
    print(f"\n🤔 决策: {'🔄 触发处理' if should else '⏸️ 跳过积累'} — {reason}")

    if not should:
        print("\n💤 跳过本次处理，diff 已自动积累到下一次")
        return

    # ── 5. 获取积累的完整 diff ────────────────────────────────
    diff_content = get_accumulated_diff(last_synced)
    changed_files = get_changed_files_since(last_synced)
    diff_summary = run(["git", "diff", f"{last_synced}..HEAD", "--stat"])

    print(f"\n📝 积累 diff: {len(diff_content)} 字符")
    print(f"📁 涉及文件: {', '.join(sorted(changed_files))}")

    # ── 6. LLM 研判是否需要更新文档 ──────────────────────────
    print("\n🤔 AI 研判是否需要更新文档...")
    judge = judge_needs_update(changed_files, diff_summary)

    if judge.get("error"):
        print(f"  ❌ LLM 研判失败: {judge['error']}")
        print("  跳过本次处理，下次重试")
        return

    if not judge.get("needs_update"):
        print(f"\n✅ 无需更新文档。原因：{judge.get('reason', '未说明')}")
        print("   已记录同步状态")
        write_state(run(["git", "rev-parse", "HEAD"]))
        return

    affected_docs = judge.get("affected_docs", [])
    print(f"\n📝 需要更新: {', '.join(affected_docs)}")
    print(f"   原因: {judge.get('reason', '')}")

    # ── 7. 克隆 docs 仓库，逐个更新文档 ──────────────────────
    print("\n🔄 克隆 docs 仓库...")
    with tempfile.TemporaryDirectory(prefix="docs-sync-") as tmpdir:
        docs_dir = Path(tmpdir) / "docs-repo"

        # URL 编码 PAT 中的特殊字符（@、#、: 等），避免 git clone 解析错误
        pat_encoded = urllib.parse.quote(DOCS_REPO_PAT, safe="")
        # 如果设置了 DOCS_FORK，从 fork 克隆（push 用 PAT 写入自己的 fork）
        clone_repo = DOCS_FORK if DOCS_FORK else DOCS_REPO
        auth_url = f"https://oauth2:{pat_encoded}@github.com/{clone_repo}.git"
        print(f"   目标: https://oauth2:***@github.com/{clone_repo}.git")

        if not run_ok(["git", "clone", "--depth=1", auth_url, str(docs_dir)], timeout=60):
            print("  ❌ 克隆 docs 仓库失败")
            print("  跳过本次处理，保留积累的 diff 下次重试")
            return
        print(f"  ✓ 已克隆到 {docs_dir}")

        # Fork 模式下，自动 sync fork 的 main 分支与原仓库保持一致
        if DOCS_FORK:
            print("  🔄 同步 fork...")
            # 公开仓库 fetch 无需认证
            upstream_url = f"https://github.com/{DOCS_REPO}.git"
            run(["git", "remote", "add", "upstream", upstream_url], cwd=str(docs_dir))
            run(["git", "fetch", "upstream", "main"], cwd=str(docs_dir), timeout=30)
            run(["git", "reset", "--hard", "upstream/main"], cwd=str(docs_dir))
            run(["git", "push", "-f", "origin", "main"], cwd=str(docs_dir), timeout=30)
            print("  ✓ fork 已同步到原仓库最新状态")

        sha_short = run(["git", "rev-parse", "--short", "HEAD"])
        branch_name = f"auto-sync/{sha_short}"
        run(["git", "checkout", "-b", branch_name], cwd=str(docs_dir))
        print(f"🔀 分支: {branch_name}")

        # 逐个更新文档
        updated_docs: list[str] = []
        for doc_relpath in affected_docs:
            doc_path = docs_dir / doc_relpath
            if not doc_path.exists():
                print(f"  ⚠️ 文件不存在: {doc_relpath}，跳过")
                continue
            new_content = update_single_doc(doc_path, diff_content, changed_files)
            if new_content:
                doc_path.write_text(new_content, encoding="utf-8")
                updated_docs.append(doc_relpath)
                print(f"  ✏️  已写入: {doc_relpath}")

        if not updated_docs:
            print("\nℹ️  所有文件无需实际修改，跳过 PR 创建")
            print("   已记录同步状态")
            write_state(run(["git", "rev-parse", "HEAD"]))
            return

        # ── 8. 生成人格化 PR 描述 ────────────────────────────
        commit_msgs = [run(["git", "log", "--oneline", "-1", c]) for c in commits]

        print("\n🎭 生成月白风格的 PR 描述...")
        pr_body = generate_pr_description(
            commit_msgs=commit_msgs,
            updated_docs=updated_docs,
            diff_summary=diff_summary,
            judge_reason=judge.get("reason", ""),
        )
        print(f"--- PR 描述 ---\n{pr_body}\n--------------")

        # ── 9. 提交并推送 ────────────────────────────────────
        run(["git", "config", "user.name", GIT_USER_NAME], cwd=str(docs_dir))
        run(["git", "config", "user.email", GIT_USER_EMAIL], cwd=str(docs_dir))

        commit_msg = f"docs: 自动同步 — {judge.get('reason', '代码变更触发的文档更新')} [skip ci]"
        run(["git", "add", "-A"], cwd=str(docs_dir))

        commit_ok = run(["git", "commit", "-m", commit_msg], cwd=str(docs_dir))
        if "nothing to commit" in commit_ok:
            print("\nℹ️  没有实际变更，跳过 PR 创建")
            write_state(run(["git", "rev-parse", "HEAD"]))
            return

        if not run_ok(["git", "push", "-f", "origin", branch_name], cwd=str(docs_dir), timeout=60):
            print("  ❌ 推送失败，请检查 PAT 权限")
            print("  跳过，保留积累下次重试")
            return
        print(f"\n📤 已推送: {branch_name}")

        # ── 10. 创建 PR ─────────────────────────────────────
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            f.write(pr_body)
            body_path = f.name

        pr_url = run(
            [
                "gh", "pr", "create",
                "--repo", DOCS_REPO,
                "--title", commit_msg[:72],
                "--body-file", body_path,
                "--base", "main",
                "--head", f"{DOCS_FORK.split('/')[0]}:{branch_name}" if DOCS_FORK else branch_name,
            ],
            cwd=str(docs_dir),
            env={"GH_TOKEN": DOCS_REPO_PAT},
        )

        if pr_url:
            print(f"\n🎉 PR 已创建: {pr_url}")
        else:
            print("\n⚠️  PR 创建可能失败，请检查 docs 仓库")

        # ── 11. 记录同步状态 ────────────────────────────────
        print("\n📝 记录同步状态...")
        write_state(run(["git", "rev-parse", "HEAD"]))

    print("\n" + "=" * 60)
    print("✅ 文档同步流程完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
