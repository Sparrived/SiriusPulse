#!/usr/bin/env python3
"""
文档自动同步 Agent
主仓库 push 触发 → 积累 diff → LLM 判断 → 自动更新 docs 仓库 → 创建人格化 PR

核心机制：
- 用 git tag `docs-last-synced` 标记最后一次成功同步的 commit
- 小改动自动积累，等下一次大改动时一起处理
- PR 描述以「月白/Sirius」的猫娘人格撰写

必须的环境变量：
  DOCS_REPO_PAT    — 访问 docs 仓库的 GitHub PAT（repo 权限）
  LLM_API_KEY      — LLM API Key
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

# 小改动阈值（同时满足才判定为「小」）
SMALL_FILE_THRESHOLD = 3       # 变更文件数 ≤ 3
SMALL_LINE_THRESHOLD = 30      # diff 行数（+/- 合计）≤ 30

# 安全上限——即便全是小改动，积累到这些值之一也会强制触发
MAX_ACCUMULATED_COMMITS = 20   # 积累 commit 数上限
MAX_ACCUMULATED_DIFF_CHARS = 15000  # 积累 diff 长度上限

# 同步标记 tag 名
SYNC_TAG = "docs-last-synced"

# 代码路径 → 受影响的文档
PATH_TO_DOCS: dict[str, set[str]] = {
    "sirius_pulse/core/":        {"docs/architecture.md"},
    "sirius_pulse/providers/":   {"docs/provider-system.md"},
    "sirius_pulse/config/":      {"docs/configuration-guide.md"},
    "sirius_pulse/memory/":      {"docs/persistence-system.md"},
    "sirius_pulse/skills/":      {"docs/skill-guide.md"},
    "sirius_pulse/platforms/":   {"docs/platforms.md"},
    "sirius_pulse/embedding/":   {"docs/architecture.md"},
    "sirius_pulse/models/":      {"docs/architecture.md"},
    "sirius_pulse/persona_manager.py": {"docs/persona-lifecycle.md"},
    "sirius_pulse/persona_config.py":  {"docs/configuration-guide.md"},
    "sirius_pulse/webui/":       {"docs/architecture.md"},
    "main.py":                   {"docs/architecture.md"},
    "pyproject.toml":            {"docs/configuration-guide.md"},
}

ALL_DOCS: set[str] = {v for vs in PATH_TO_DOCS.values() for v in vs}


# ═══════════════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════════════

def run(
    cmd: list[str],
    cwd: str | None = None,
    silent: bool = False,
) -> str:
    """
    运行 shell 命令，返回 stdout。
    不抛出异常——任何失败都返回空字符串，保证管道不断。
    """
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd, check=False,
        )
        if result.returncode != 0 and not silent:
            print(f"  ⚠️ 命令返回非零: {' '.join(cmd)}\n     {result.stderr.strip()}")
        return result.stdout.strip()
    except FileNotFoundError:
        print(f"  ⚠️ 命令不存在: {cmd[0]}")
        return ""
    except Exception as e:
        print(f"  ⚠️ 命令异常: {' '.join(cmd)}\n     {e}")
        return ""


def parse_diff_stat(stat: str) -> tuple[int, int]:
    """解析 `git diff --stat` 输出，返回 (文件数, 变更行数)"""
    if not stat:
        return 0, 0
    lines = stat.strip().splitlines()
    if not lines:
        return 0, 0
    file_count = len(lines) - 1  # 最后一行是汇总
    # 从汇总行提取行数（如 "3 files changed, 15 insertions(+), 2 deletions(-)"）
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
#  同步标记管理
# ═══════════════════════════════════════════════════════════════════

def get_last_synced_commit() -> str | None:
    """读取 docs-last-synced 标记指向的 commit"""
    sha = run(["git", "rev-parse", SYNC_TAG], silent=True)
    return sha if sha else None


def is_first_commit() -> bool:
    """当前仓库是否只有 1 个 commit"""
    count = run(["git", "rev-list", "--count", "HEAD"])
    return count == "1"


def update_sync_tag() -> None:
    """将 docs-last-synced 标记移到 HEAD 并推送"""
    run(["git", "tag", "-f", SYNC_TAG, "HEAD"])
    run(["git", "push", "origin", SYNC_TAG, "--force"])
    print(f"  ✓ 同步标记已更新为 {run(['git', 'rev-parse', '--short', 'HEAD'])}")


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
            # 可能是第一个 commit 没有 parent
            stat = run(["git", "show", "--stat", sha])
        files, lines = parse_diff_stat(stat)
        stats.append({"sha": sha, "files": files, "lines": lines})
    return stats


def get_accumulated_diff(commits: list[str]) -> str:
    """获取积累 diff（last_synced..HEAD），限制长度"""
    if not commits:
        return ""
    diff = run(["git", "diff", f"{commits[0]}^..HEAD", "--", ".", ":!*.md", ":!.github"])
    # 限制长度，避免超 token
    max_len = MAX_ACCUMULATED_DIFF_CHARS
    if len(diff) > max_len:
        diff = diff[:max_len] + "\n\n...(diff 过长, 已截断)"
    return diff


def should_process(commits: list[str], stats: list[dict]) -> tuple[bool, str]:
    """
    决策是否需要触发 LLM 处理。

    触发条件（满足任一即可）：
    1. 存在单个「大改动」commit
    2. 积累 commit 数达到上限
    3. 积累 diff 长度达到上限
    4. 所有 commit 都是小改动，但累计总量超过了双倍阈值
    """
    if not commits:
        return False, "没有新增 commit"

    # 条件 1：是否存在大改动 commit
    for s in stats:
        if s["files"] > SMALL_FILE_THRESHOLD or s["lines"] > SMALL_LINE_THRESHOLD:
            return True, f"commit {s['sha'][:8]} 改动较大（{s['files']} 文件, {s['lines']} 行）"

    # 条件 2：积累数达到上限
    if len(commits) >= MAX_ACCUMULATED_COMMITS:
        return True, f"已积累 {len(commits)} 个 commit，达到处理上限"

    # 条件 3：计算累计总量
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
        "max_tokens": 8192,
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
        # 去掉 markdown 包裹
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
    "保持原文档结构、标题层级、风格不变。只修改直接相关的部分。"
    "必须输出严格 JSON。"
)


def update_single_doc(
    filepath: Path,
    diff_content: str,
    changed_files: set[str],
) -> str | None:
    """LLM 自动更新单个文档，返回新内容；None 表示无需修改"""
    old_content = filepath.read_text(encoding="utf-8")

    prompt = f"""## 任务
根据代码变更，更新以下文档。

## 当前文档内容（{filepath.name}）
```markdown
{old_content[:8000]}
```

## 本次代码 diff
```diff
{diff_content}
```

## 变更文件
{chr(10).join(sorted(changed_files))}

## 输出格式（严格 JSON）
{{"modified": true/false, "content": "完整的更新后文档内容（仅 modified=true 时）", "changes": ["改动点1", "改动点2"]}}"""

    result = call_llm_json(prompt, system_prompt=DOC_UPDATE_SYSTEM_PROMPT)

    if result.get("modified") and result.get("content"):
        changes = "; ".join(result.get("changes", []))
        print(f"  ✓ 已更新: {filepath.name} — {changes}")
        return result["content"]

    print(f"  - 无需修改: {filepath.name}")
    return None


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

    # ── 1. 获取同步状态 ──────────────────────────────────────
    last_synced = get_last_synced_commit()

    if last_synced is None:
        print("\n📌 首次运行，初始化同步标记...")
        if is_first_commit():
            update_sync_tag()
            print("   仓库只有根 commit，已建立标记，下次 push 再检查")
            return
        # 用 HEAD~1 作为起点，这样本次 push 的 diff 会被检查
        last_synced = run(["git", "rev-parse", "HEAD~1"])
        if not last_synced:
            print("   无法确定起始 commit，跳过")
            return
        print(f"   起始点: {last_synced[:12]} (HEAD 的父 commit)")
    else:
        print(f"\n📌 上次同步点: {last_synced[:12]}")

    # ── 2. 获取积累的 commit ────────────────────────────────
    commits = run(["git", "rev-list", f"{last_synced}..HEAD", "--reverse"]).splitlines()
    commits = [c.strip() for c in commits if c.strip()]

    if not commits:
        print("\nℹ️  没有新的 commit，跳过")
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
    diff_content = get_accumulated_diff(commits)
    changed_files = get_changed_files_since(last_synced)
    diff_summary = run(["git", "diff", f"{last_synced}..HEAD", "--stat"])

    print(f"\n📝 积累 diff: {len(diff_content)} 字符")
    print(f"📁 涉及文件: {', '.join(sorted(changed_files))}")

    # ── 6. LLM 研判是否需要更新文档 ────────────────────────
    print("\n🤔 AI 研判是否需要更新文档...")
    judge = judge_needs_update(changed_files, diff_summary)

    if judge.get("error"):
        print(f"  ❌ LLM 研判失败: {judge['error']}")
        print("  跳过本次处理，下次重试")
        return

    if not judge.get("needs_update"):
        print(f"\n✅ 无需更新文档。原因：{judge.get('reason', '未说明')}")
        print("   已标记为已同步，后续不再积累这些 diff")
        update_sync_tag()
        return

    affected_docs = judge.get("affected_docs", [])
    print(f"\n📝 需要更新: {', '.join(affected_docs)}")
    print(f"   原因: {judge.get('reason', '')}")

    # ── 7. 克隆 docs 仓库，逐个更新文档 ────────────────────
    print("\n🔄 克隆 docs 仓库...")
    with tempfile.TemporaryDirectory(prefix="docs-sync-") as tmpdir:
        docs_dir = Path(tmpdir) / "docs-repo"

        auth_url = (
            f"https://x-access-token:{DOCS_REPO_PAT}@github.com/{DOCS_REPO}.git"
        )
        clone_ok = run(["git", "clone", auth_url, str(docs_dir)])
        if not clone_ok:
            print("  ❌ 克隆 docs 仓库失败")
            print("  跳过本次处理，保留积累的 diff 下次重试")
            return
        print(f"  ✓ 已克隆到 {docs_dir}")

        # 创建分支
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
            print("   已标记为已同步")
            update_sync_tag()
            return

        # ── 8. 生成人格化 PR 描述 ────────────────────────────
        commit_msgs = [
            run(["git", "log", "--oneline", "-1", c]) for c in commits
        ]

        print("\n🎭 生成月白风格的 PR 描述...")
        pr_body = generate_pr_description(
            commit_msgs=commit_msgs,
            updated_docs=updated_docs,
            diff_summary=diff_summary,
            judge_reason=judge.get("reason", ""),
        )
        print(f"--- PR 描述 ---\n{pr_body}\n--------------")

        # ── 9. 提交并推送 ────────────────────────────────────
        run(["git", "config", "user.name", "Sirius Docs Bot"], cwd=str(docs_dir))
        run(["git", "config", "user.email", "bot@sirius.pulse"], cwd=str(docs_dir))

        commit_msg = f"docs: 自动同步 — {judge.get('reason', '代码变更触发的文档更新')} [skip ci]"
        run(["git", "add", "-A"], cwd=str(docs_dir))

        commit_ok = run(["git", "commit", "-m", commit_msg], cwd=str(docs_dir))
        if "nothing to commit" in commit_ok:
            print("\nℹ️  没有实际变更，跳过 PR 创建")
            update_sync_tag()
            return

        push_ok = run(["git", "push", "origin", branch_name], cwd=str(docs_dir))
        if not push_ok:
            print("  ❌ 推送失败")
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
                "--title", commit_msg[:72],  # GitHub 限制 72 字符
                "--body-file", body_path,
                "--base", "main",
                "--head", branch_name,
            ],
            cwd=str(docs_dir),
        )

        if pr_url:
            print(f"\n🎉 PR 已创建: {pr_url}")
        else:
            print("\n⚠️  PR 创建可能失败，请检查 docs 仓库")

        # ── 11. 更新同步标记 ────────────────────────────────
        print("\n🏷️  更新同步标记...")
        update_sync_tag()

    print("\n" + "=" * 60)
    print("✅ 文档同步流程完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
