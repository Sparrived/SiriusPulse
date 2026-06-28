---
name: commit-preparation
description: "在发布commit前执行检查清单，包括gitignore验证、改动总结、ChangeLog更新与标准格式commit。关键词：commit前检查、gitignore、ChangeLog、commit格式、版本管理。"
---

# Commit前检查清单

## 目标

在提交代码到仓库前，确保代码质量、文档完整性、隐私安全与提交规范。

## 语言规范（强制）

- 本 SKILL 及所有后续新增/修改的 SKILL 必须使用中文。
- `description` 和正文必须为中文。
- 若任务中发现英文 SKILL 内容，需在同一任务中同步中文化。

## 步骤

### 1. 检查 .gitignore 合规性

验证 `.gitignore` 包含以下必要项：

- **Python运行时**：`__pycache__/`, `*.pyc`, `*.pyo`, `*.egg-info`, `build/`, `dist/`, `wheels/`
- **虚拟环境**：`.venv`, `venv/`, `env/`
- **隐私配置**：`.last_config_path`（用户配置缓存），API快捷配置文件
- **测试缓存**：`.pytest_cache/`, `.coverage`
- **运行时数据**：`data/`（本地测试数据与会话记录）
- **IDE**：`.vscode/`, `.idea/`, `*.swp`, `*.swo`
- **环境**：`.env`, `.env.local`（若使用环境变量管理密钥）

**操作指南**：
```bash
# 查看当前gitignore规则
cat .gitignore

# 验证待提交文件不包含敏感内容（如API密钥）
git status --short
git diff --cached --name-only
```

**检查清单**：
- [ ] 运行时文件（__pycache__, .pytest_cache）已忽略
- [ ] 隐私文件（API密钥、.last_config_path）已忽略
- [ ] 本地数据（data/ 目录）已忽略
- [ ] 提交暂存区无敏感文件（执行 `git diff --cached` 确认）

### 2. 总结近期改动

列出此次提交涉及的改动范围：

```bash
# 查看与上次发布的差异（假设上一个tag为 v1.0.0）
git log --oneline v1.0.0..HEAD

# 或查看暂存区的改动
git diff --cached --stat
```

**改动摘要应包含**：
- 新增功能（Feature）
- 问题修复（Fix）
- 代码优化/重构（Refactor）
- 文档更新（Docs）
- 依赖变更（Chore）

**示例摘要**：
```
- feat: 实现日记索引模块（memory/diary/indexer.py、memory/diary/retriever.py）
- feat: 新增智能遗忘引擎与衰退调度
- test: 添加8个记忆质量系统测试（79/79通过）
- docs: 更新architecture.md中的记忆系统说明
- chore: 增强.gitignore覆盖范围
```

### 3. 更新 ChangeLog

编辑 `CHANGELOG.md`，按以下格式新增条目：

**格式** (Keep a Changelog 规范)：
```markdown
## [Unreleased]

### Added
- 新增功能描述

### Changed
- 修改功能描述

### Fixed
- 修复问题描述

### Deprecated
- 弃用功能描述（可选）

---

## [1.1.0] - 2026-04-05

### Added
- 初始版本发布：多人角色扮演编排引擎
- 支持OpenAI兼容接口与SiliconFlow适配
- 动态群聊模式与记忆管理系统
```

**操作指南**：
```bash
# 编辑CHANGELOG.md
# 1. 将待发版本从[Unreleased]改为[版本号] - [日期]
# 2. 新增[Unreleased]占位符供下次更新

# 示例：发布v1.1.0
# [Unreleased]
# （空占位符）
#
# [1.1.0] - 2026-04-05
# ### Added
# - ...
```

**检查清单**：
- [ ] CHANGELOG.md已更新，按[Added/Changed/Fixed]分类列出改动
- [ ] 版本号与pyproject.toml一致
- [ ] 日期使用ISO格式（YYYY-MM-DD）
- [ ] 所有重要改动已在日志中说明

### 4. 按标准格式发布Commit

使用 **Conventional Commits** 规范：

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Type 列表**：
- `feat`: 新功能
- `fix`: 问题修复
- `refactor`: 代码重构（无功能变化）
- `test`: 测试相关
- `docs`: 文档更新
- `chore`: 依赖、构建、工具等杂项
- `perf`: 性能优化

**Scope** (可选,但推荐)：
- 受影响的模块名，如 `memory`, `provider`, `cli`, `tests`

**Subject**：
- 使用祈使句，第一个单词大写
- 中英文需统一：**项目已使用中文，commit须全中文**
- 不超过50个字符

**Body** (可选，但对重要改动必需)：
- 解释何为改动、为何改动、与之前行为的区别等
- 每行不超过72个字符

**Footer** (可选)：
- 关闭相关issue：`Closes #123`
- 破坏性变更说明：`BREAKING CHANGE: 详细说明`

**提交示例**：

```bash
# 新增功能类提交
git commit -m "feat(memory): 实现记忆质量评估与智能遗忘引擎

新增sirius_pulse/memory/diary/indexer.py模块，包含：
- DiaryIndexer：关键词 + EmbeddingClient 向量索引
- DiaryRetriever：按 token 预算检索相关日记

新增sirius_pulse/memory/diary/retriever.py，提供检索工具：
- retrieve: 按查询检索适配 prompt 的日记列表
- search: 关键词/向量混合搜索

整合到DiaryManager：add()、search()、retrieve()

Closes #234"

# 修复问题类提交
git commit -m "fix(provider): 修复SiliconFlow API响应超时处理

之前在网络波动时未正确捕获超时异常，导致会话中断。
现在使用exponential backoff重试机制，重试次数配置为3次。"

# 文档更新提交
git commit -m "docs(architecture): 补充记忆质量系统说明

新增记忆质量评估与智能遗忘章节，包含：
- 评分算法与时间衰退机制
- CLI使用示例与集成方式
- 内部参数常量说明"

# 单行简短提交
git commit -m "chore(gitignore): 增强隐私文件覆盖范围"
```

**Commit前最终检查**：
- [ ] 已执行 `pytest -q` 确保测试全部通过
- [ ] 已执行 `git status` 确认暂存内容正确（无敏感文件）
- [ ] Commit message遵循Conventional Commits规范
- [ ] Commit message使用中文（与项目语言一致）
- [ ] 若为大功能，body部分包含改动理由与实现说明
