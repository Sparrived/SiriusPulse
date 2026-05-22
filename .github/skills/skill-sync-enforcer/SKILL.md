---
name: skill-sync-enforcer
description: "当修改 Sirius Pulse 代码时使用，用于强制代码与 AI 定制资产的同步更新。关键词：技能同步、代码联动文档、指令漂移、变更后检查清单。"
---

# 技能同步约束

## 触发条件

当以下文件发生代码变更时，必须执行本流程：
- `sirius_pulse/**/*.py`
- `main.py`
- `pyproject.toml`

## 语言规范（强制）

- 所有 SKILL 文件必须保持中文。
- 任何后续新增/修改 SKILL 的任务，必须将 `description` 和正文写为中文。
- 若本次变更触及 SKILL 且包含英文内容，必须在同次任务中完成中文化。

## 工作流

1. 判断变更是否影响架构、命令、API 或目录布局。
2. 内部实现可直接重构；当前项目未发布，若影响对外接口可直接调整，但必须同步更新文档、示例与测试。
3. 任何新增内部功能都必须在统一对外接口层暴露可调用入口。
4. 所有对外 Python 接口统一收敛在 `sirius_pulse/api/`，禁止在多个内部模块分散暴露新入口。
5. 任何内部代码改动后，必须保证 `main.py` 仍可用于主动测试（至少 `--help` 与一次可退出的会话启动可执行）。
6. 新功能实现后，**不应生成额外的 markdown 文档**（如使用指南、快速启动、参考手册等）来说明新功能的用法，除非用户明确提及。应将功能文档集中在现有的对应位置（如 `docs/architecture.md`、`docs/external-usage.md` 等），或在用户主动要求时才生成专门的指南。
7. 若影响，必须同步更新以下文件：
   - `docs/architecture.md`
   - `docs/full-architecture-flow.md`
   - `docs/external-usage.md`（若外部接入方式变化）
   - `.github/skills/framework-quickstart/SKILL.md`
   - `.github/skills/external-integration/SKILL.md`（若外部接入或配置变化）
   - `README.md`（若 CLI/API 用法变化）
   - 若涉及重启恢复、记忆压缩或 work-path 约束，还需校验 `session_store.py` 与对应示例是否同步
8. 若修改系统提示词生成逻辑（`_build_system_prompt()` 或提示词生成函数），必须确保安全约束已包含：系统提示词末尾应当明确告诉模型不要主动泄露自己的系统提示词和初始指令。
9. 校验示例是否仍与真实代码路径和命令名称一致。
10. 在交付说明中明确列出本次已同步内容。

## 完成检查清单

- [ ] 代码行为已更新。
- [ ] 若影响外部接口，已同步更新文档、示例与测试。
- [ ] 新增功能已在 `sirius_pulse/api/` 暴露。
- [ ] 已执行 `python main.py --help`。
- [ ] 已执行一次 `main.py` 会话启动 smoke 测试（可通过管道输入 `exit` 退出）。
- [ ] 架构文档已同步。
- [ ] 完整架构流程图文档（`docs/full-architecture-flow.md`）已同步。
- [ ] 框架快速上手 SKILL 已同步。
- [ ] 外部接入 SKILL 已同步（若外部接入能力相关变更）。
- [ ] README/示例已同步。

## 防护规则

若代码已变更但未在同一任务中审阅并同步 SKILL/文档，不得声明任务完成。

