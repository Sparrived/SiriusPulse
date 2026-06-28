---
name: release-checklist
description: "在为 Sirius Pulse 做发布准备时使用，用于校验版本信息、命令可运行性、文档准确性与技能同步状态。关键词：发布准备、发布前检查、文档同步、命令校验。"
---

# 发布检查清单

## 目标

在完成整个功能的设计后，执行一致的发布前检查流程，然后触发 release。

## 语言规范

- 发布前需检查仓库内 SKILL 是否全部为中文。
- 任何新增或修改的 SKILL 必须使用中文（包含 frontmatter 的 `description`）。
- 若发现英文 SKILL 内容，发布前必须完成中文化修正。

## 关键原则

**发布时机**：只有在完成整个功能的设计实现后，确保所有功能模块已集成、测试已通过、文档已同步的情况下，才进行 release 触发。禁止在功能尚未完成设计的阶段创建 release。

## 步骤

1. 版本与元数据
   - 校验 `pyproject.toml` 中版本号与描述是否正确。
2. 安装并执行检查
   - `python -m pip install -e .[test]`
   - `pytest -q`
3. 命令校验
   - `sirius-pulse --help`
   - `python main.py --help`
   - 若使用 `examples/session.json` 这类 `generated_agent_key` 示例配置，先在独立 smoke workspace 中准备最小 generated agent 资产，或按首次引导完成初始化
   - 使用独立工作目录运行 `python main.py --config examples/session.json --work-path data/release_smoke --config-root data/release_smoke_config`，验证首次引导和入口可用
   - 首次仅进入并退出时，检查 `data/release_smoke_config/workspace.json`、`data/release_smoke_config/roleplay/generated_agents.json` 与 `data/release_smoke/sessions/default/participants.json` 是否成功写出
   - 若要验证 `data/release_smoke/sessions/default/session_state.db`，需在可用 provider 下完成至少一轮真实消息往返
4. 文档与 AI 资产同步
   - 确认 `README.md` 中命令/示例仍可使用。
   - 确认 `docs/architecture.md` 与当前模块边界一致。
   - 确认 `.github/skills/framework-quickstart/SKILL.md` 反映最新架构。
5. 变更摘要
   - 总结关键变更与已知限制。

## 失败条件

- 测试执行失败。
- 命令示例过期。
- 架构或 SKILL 文档与代码不同步。

## 发布到 PyPI

当所有检查项全部通过后，执行以下命令触发自动发布：

```bash
git tag v{VERSION}  # 例如：git tag v0.5.0
git push origin v{VERSION}
```

GitHub Actions 会自动构建、验证包元数据，并发布到 PyPI。**无需手动创建 Release，workflow 会自动处理。**

详细的发布流程说明见：[pypi-publishing SKILL](.github/skills/pypi-publishing/SKILL.md)

## 交付输出

提供一份简要报告，包含：
- 已通过检查项
- 未通过检查项
- 后续处理动作
