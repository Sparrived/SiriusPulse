---
name: project-structure-sync
description: "遍历项目结构变化并同步更新文档。监控模块变化、配置更新，生成检查清单以确保 SKILL、文档、示例和架构信息的实时一致性。关键词：项目结构、模块映射、文档同步、变更追踪、完整性检查。"
---

# 项目结构与文档同步指南

## 目标

在修改代码（特别是新增/删除模块或改变特性）时，通过系统化的方式遍历项目结构，识别影响范围，并生成清单以确保所有相关 SKILL、文档、示例和配置保持一致。

## 项目结构地图

### 核心模块层级

```
sirius_pulse/
├── api/                      - 对外 API facade（engine/models/providers/session 等）
├── core/                     - 编排核心（emotional_engine.py、prompt_factory.py、model_router.py、engine_persistence.py、identity_resolver.py）
├── async_engine/             - 兼容导出 + prompts/orchestration/utils 辅助层
├── embedding/                - Embedding 微服务（server.py aiohttp 服务端 + client.py 同步客户端）
├── persona_generation/       - 人格资产生成子包（templates.py 数据模型 + builders.py LLM 生成）
├── workspace/                - WorkspaceLayout / Runtime / Watcher / RoleplayManager
├── config/                   - SessionConfig / WorkspaceConfig / JSONC / ConfigManager
├── models/                   - Message / Participant / Transcript 等数据契约
├── memory/                   - basic/diary/glossary/user/semantic 子包；context_assembler.py 将短期记忆以 XML 嵌入 system prompt，返回 [system, user] 两条消息；日记条目支持时间戳显示
├── session/                  - SessionStore / runner
├── providers/                - Provider 实现、路由（全链路异步 httpx）
├── token/                    - token 记录、SQLite 持久化与分析
├── skills/                   - SKILL 注册、执行与 data store；被动 SKILL 支持；表情包子系统 sticker/
├── platforms/                - NapCat 多实例管理、QQ 桥接器、EngineRuntime 封装
├── webui/                    - WebUI REST API + 静态页面
├── utils/                    - 工具函数、WorkspaceLayout 路径布局
├── config/                   - SessionConfig / WorkspaceConfig / ConfigManager / JSONC
└── cli.py                    - 库内薄 CLI
```

### 关键配置文件

| 文件 | 用途 | 监控变化 |
|------|------|--------|
| `pyproject.toml` | 项目元数据、依赖、入口 | 版本、依赖、命令名称 |
| `.github/workflows/ci.yml` | CI/CD 流程 | Python 版本、测试命令 |
| `Makefile` | 开发便利命令 | 命令定义 |
| `scripts/` | 设置/工具脚本 | 新增脚本 |

### 文档文件

| 文件 | 内容 | 同步触发 |
|------|------|----------|
| `docs/architecture.md` | 架构设计详解 | 模块重构、新增模块、接口变化 |
| `docs/full-architecture-flow.md` | 完整架构流程图 | 数据流、执行流变化 |
| `docs/memory-system.md` | 四层记忆底座 | 记忆系统变更 |
| `docs/engine-emotional.md` | Emotional Engine 详细说明 | 引擎行为变更 |
| `docs/external-usage.md` | 外部接入指南 | Provider 变化、API 变化、配置变化 |
| `docs/best-practices.md` | 最佳实践与模式 | 性能优化、内存管理特性新增 |
| `docs/configuration.md` | 配置项详解 | 配置选项新增、参数变化 |
| `docs/quickstart.md` | 快速启动指南 | 命令名称、基本用法变化 |
| `README.md` | 项目总览 | 用法、特性、依赖版本 |
| `docs/migration-v0.28.md` | v0.28 Emotional Engine 迁移指南 | 引擎切换、记忆系统变更 |
| `docs/migration-roleplay-v0.20.md` | 外部人格生成迁移指南 | roleplay_prompting 对外用法变化 |

### SKILL 文件

| SKILL | 内容 | 同步触发 |
|-------|------|----------|
| `framework-quickstart` | 架构快速理解、模块导读 | 新增/删除模块、模块位置变化、依赖关系变化 |
| `external-integration` | 外部接入指南、API 用法 | Provider 变化、配置变化、API 变化 |
| `skill-sync-enforcer` | 代码变更检查清单 | 所有满足触发条件的变更 |
| `commit-preparation` | Commit 前检查 | ChangeLog 格式、版本信息变化 |
| `release-checklist` | 发布前检查 | 版本信息、文档同步状态 |

## 变更追踪流程

### 1. 识别变更类型

执行以下检查以确定变更范围：

```bash
# 查看最近的代码变更
git log --oneline -n 5

# 查看改动文件列表
git diff HEAD~1 --name-only

# 查看具体改动统计
git diff HEAD~1 --stat
```

### 2. 变更分类与影响范围

#### A. 模块级变更（高影响）

**触发条件**：
- 新增 `sirius_pulse/<module_name>/` 目录及 `.py` 文件
- 删除现有模块
- 模块文件重构（拆分/合并）
- 新增 Provider 类型

**必须更新**：
- [ ] `docs/architecture.md` - 新增模块说明
- [ ] `docs/full-architecture-flow.md` - 更新数据流/执行流
- [ ] `.github/skills/framework-quickstart/SKILL.md` - 更新阅读顺序和模块描述
- [ ] `.github/skills/external-integration/SKILL.md` - 若涉及外部接入
- [ ] 所有其他 SKILL 的推荐读取顺序（若改变了模块位置）
- [ ] 对应的 `tests/test_*.py` 文件

**示例提示**：
```
检测到新增模块: sirius_pulse/cache/
请更新：
1. docs/architecture.md - 在心智模型中添加 cache/ 说明
2. .github/skills/framework-quickstart/SKILL.md - 在阅读顺序添加对应模块
3. 新增 tests/test_cache.py
```

#### B. 接口/API 变更（中-高影响）

**触发条件**：
- 修改 `sirius_pulse/api/` 中的公开接口
- 修改 `EmotionalGroupChatEngine` 的公开方法签名
- 新增/删除 CLI 命令（`sirius-pulse`）
- 配置结构变化

**必须更新**：
- [ ] `docs/external-usage.md` - 使用示例
- [ ] `.github/skills/external-integration/SKILL.md` - API 说明
- [ ] `README.md` - 快速开始示例
- [ ] `examples/*.py` 或 `examples/*.json` - 实际示例代码

**示例提示**：
```
检测到 API 变更: EmotionalGroupChatEngine.process_message() 签名变化
请更新：
1. docs/external-usage.md - 更新方法说明和示例
2. examples/ - 修改使用示例
3. README.md - 更新快速开始代码
```

#### C. 细节实现变更（中影响）

**触发条件**：
- `sirius_pulse/models/models.py` 的消息 / transcript 契约变化
- `sirius_pulse/config/models.py` 的 session / workspace / orchestration 契约变化
- 系统提示词生成逻辑改动
- 缓存策略、性能监控逻辑修改

**必须更新**：
- [ ] `docs/architecture.md` - 对应部分的详解
- [ ] `.github/skills/framework-quickstart/SKILL.md` - 心智模型部分
- [ ] 对应的 tests 文件

**示例提示**：
```
检测到数据契约变化: SessionConfig / WorkspaceConfig / Transcript 新增字段
请更新：
1. docs/architecture.md - 更新模型说明
2. docs/external-usage.md - 若外部调用契约受影响则补充说明
3. 对应 tests 文件 - 补充测试覆盖
```

#### D. 配置/依赖变更（中-低影响）

**触发条件**：
- `pyproject.toml` 的版本/依赖变化
- `config/manager.py` 的配置选项变化
- `.pre-commit-config.yaml` 的工具版本变化

**必须更新**：
- [ ] `docs/configuration.md` - 新增配置选项说明
- [ ] `README.md` - 依赖版本、安装步骤
- [ ] `examples/session.json` - 配置示例

**示例提示**：
```
检测到依赖变更: 新增 redis>=4.0
请更新：
1. README.md - 依赖安装说明
2. docs/configuration.md - Redis 配置选项
```

#### E. 工具/流程变更（低影响）

**触发条件**：
- `.github/workflows/` 的 CI/CD 流程变化
- `Makefile` 的命令变化
- `scripts/` 下的工具脚本变化

**必须更新**：
- [ ] 对应的 SKILL 文件（若涉及开发流程）
- [ ] `README.md` - 开发指南部分

## 变更检查清单（快速对照）

### 【新增/修改模块时】

- [ ] 新模块已在 `sirius_pulse/<module>/` 下创建
- [ ] 新模块包含 `__init__.py` 导出公开接口
- [ ] 新增 `tests/test_<module>.py` 单元测试
- [ ] 所有新增类/函数都有完整的文档字符串和类型注解
- [ ] `docs/architecture.md` 已补充模块说明和设计初衷
- [ ] `docs/full-architecture-flow.md` 已更新数据流/执行流
- [ ] `framework-quickstart SKILL` 已更新阅读顺序和模块描述
- [ ] `external-integration SKILL` 已更新（若涉及外部接入）
- [ ] 可用性检查：`python main.py --help` 执行正常
- [ ] 自测：执行一次完整的会话流程测试

### 【修改现有接口时】

- [ ] 所有修改都有类型注解
- [ ] 向后兼容性已确认（或有明确的弃用计划）
- [ ] `sirius_pulse/api/` 中的导出已同步更新
- [ ] `examples/` 中的示例已测试并更新
- [ ] `docs/external-usage.md` 已补充新用法说明
- [ ] `tests/` 的相关测试已更新
- [ ] `README.md` 的快速开始已验证
- [ ] 破坏性变更已在 `CHANGELOG.md` 记录

### 【配置/依赖变更时】

- [ ] `pyproject.toml` 已正确更新
- [ ] 可选依赖已在 `[project.optional-dependencies]` 中声明
- [ ] `docs/configuration.md` 已补充新配置项说明
- [ ] `examples/session.json` 已示例新配置（若适用）
- [ ] `README.md` 的安装步骤已验证
- [ ] CI/CD 已测试新依赖的兼容性

## 自助诊断命令

```bash
# 1. 查看最近 5 次提交的变更范围
git log --oneline -n 5
git show HEAD --stat

# 2. 列出本地未推送的提交
git log origin/master..HEAD --oneline

# 3. 查看特定契约文件的历史变更
git log -p --follow sirius_pulse/config/models.py | head -100
git log -p --follow sirius_pulse/models/models.py | head -100

# 4. 对比文档和代码的一致性
# 查看 framework-quickstart SKILL 中提到的模块是否存在
grep -o "sirius_pulse/[a-z_/]*\.py" .github/skills/framework-quickstart/SKILL.md | sort -u

# 5. 验证所有 SKILL 文件的 frontmatter 格式
grep -r "^name:" .github/skills/*/SKILL.md
grep -r "^description:" .github/skills/*/SKILL.md
```

## 常见同步场景

### 场景 1：新增性能监控模块

```
变更：创建 sirius_pulse/performance/{metrics,profiler,benchmarks}.py

检查清单：
✓ 在 docs/architecture.md 的"心智模型"中补充性能监控特性说明
✓ 在 docs/full-architecture-flow.md 中补充性能指标的收集流程
✓ 在 framework-quickstart SKILL 的"阅读顺序"中添加新模块
✓ 在 external-integration SKILL 中补充使用示例
✓ 新增 tests/test_performance.py 包含 19+ 个测试
✓ tests/test_*.py 中的所有相关测试通过
✓ python main.py --help 可正常执行
```

### 场景 2：修改 SessionConfig / WorkspaceConfig 数据结构

```
变更：在 sirius_pulse/config/models.py 中新增 SessionConfig 或 WorkspaceConfig 字段

检查清单：
✓ 字段包含完整类型注解和文档字符串
✓ 在 config/models.py 中补充字段说明
✓ 在 docs/architecture.md 中更新对应数据模型描述
✓ 在 docs/external-usage.md 中补充使用示例
✓ 更新 examples/session.json 示例配置
✓ tests/test_config_manager.py 或相关测试中有相应覆盖
✓ 旧代码的兼容性已确认（提供默认值或迁移逻辑）
```

### 场景 3：新增 Provider 类型

```
变更：实现 sirius_pulse/providers/new_platform.py

检查清单：
✓ 新 Provider 继承 AsyncLLMProvider 或 LLMProvider
✓ 在 sirius_pulse/providers/__init__.py 中导出
✓ 在 sirius_pulse/providers/routing.py 中注册路由
✓ 在 docs/external-usage.md 中补充接入说明
✓ 在 examples/ 中提供配置示例
✓ 在 framework-quickstart SKILL 中更新 providers 部分说明
✓ 新增 tests/test_providers_new_platform.py
```

## 防护与约定

1. **同步时间点**：每次代码提交前或在 skill-sync-enforcer 触发后立即执行。
2. **优先级顺序**：优先同步 `docs/` > `SKILL` > `README.md` > `examples/`。
3. **文档一致性检查**：
   - 所有 SKILL 中提及的模块路径必须真实存在
   - 所有 SKILL 中的阅读顺序必须反映当前的模块依赖关系
   - 所有示例代码必须能够实际执行
4. **提交消息格式**：
   - 代码变更：`feat: <description>` / `fix: <description>`
   - 文档同步：`docs: 同步 <具体同步内容>`
   - 例如：`docs: 同步 framework-quickstart 和 external-integration SKILL`

## 交付检查表

当完成文档/SKILL 同步后，在 PR 或提交说明中检查：

- [ ] 识别了变更的所有影响范围
- [ ] 更新了所有受影响的 `docs/` 文件
- [ ] 更新了所有受影响的 SKILL 文件
- [ ] 更新了 `README.md` 和 `examples/`（若需要）
- [ ] 所有文档中的代码示例都已验证可执行
- [ ] 所有 SKILL 的 frontmatter 格式正确（`name:` 和 `description:` 完整）
- [ ] 提交说明清晰地列出了本次同步的内容
- [ ] 没有产生与 skill-sync-enforcer 的重复/冲突

