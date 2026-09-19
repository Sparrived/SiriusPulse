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
├── core/                     - 编排核心（emotional_engine.py、prompt_factory.py、model_router.py、engine_persistence.py、identity_resolver.py）
├── embedding/                - Embedding 微服务（server.py aiohttp 服务端 + client.py 同步客户端）
├── persona_generation/       - 人格资产生成子包（templates.py 数据模型 + builders.py LLM 生成）
├── config/                   - SessionConfig / WorkspaceBootstrap / JSONC / ConfigManager
├── models/                   - Message / Participant / Transcript 等数据契约
├── memory/                   - basic/diary/semantic/user/units 子包；context_assembler.py 将短期记忆以 XML 嵌入 system prompt，返回 [system, user] 两条消息；日记条目支持时间戳显示
├── session/                  - SessionStore / runner
├── providers/                - 唯一 LLM 边界：AMKR 连接配置、任务名注册、OpenAI 兼容客户端
├── token/                    - token 记录、SQLite 持久化与分析
├── tools/                    - Tool 注册、执行与 data store；被动工具支持；内置 Tool 位于 tools/builtin/
├── platforms/                - NapCat 多实例管理、QQ 桥接器、EngineRuntime 封装
├── webui/                    - WebUI REST API + 静态页面
├── utils/                    - 工具函数、WorkspaceLayout 路径布局
└── cli.py                    - 库内薄 CLI
```

### 关键配置文件

| 文件 | 用途 | 监控变化 |
|------|------|--------|
| `pyproject.toml` | 项目元数据、依赖、入口 | 版本、依赖、命令名称 |
| `.github/workflows/ci.yml` | CI/CD 流程 | Python 版本、测试命令 |
| `.pre-commit-config.yaml` | 提交前钩子 | 工具版本、钩子定义 |
| `scripts/` | 设置/工具脚本 | 新增脚本 |

### 文档文件

文档目录按 VitePress 侧边栏划分为 `guide/`、`modules/`、`reference/`、`extensions/` 四块。

| 文件 | 内容 | 同步触发 |
|------|------|----------|
| `docs/guide/architecture-overview.md` | 系统架构全景、模块边界 | 模块重构、新增模块、接口变化、数据流变化 |
| `docs/guide/engine-architecture.md` | 对话引擎详细说明 | 引擎行为变更 |
| `docs/guide/memory-system.md` | 分层记忆底座 | 记忆系统变更 |
| `docs/guide/configuration.md` | 配置项详解 | 配置选项新增、参数变化 |
| `docs/guide/quickstart.md` | 快速启动指南 | 命令名称、基本用法变化 |
| `docs/modules/provider-system.md` | AMKR 接入模块（任务名契约、工作空间、注册策略） | 任务名变化、AMKR 边界变化 |
| `docs/reference/provider-config.md` | AMKR 连接配置与任务名契约 | AMKR 配置项、环境变量、接口变化 |
| `docs/reference/global-config.md` | 全局配置字段 | 全局配置字段变化 |
| `docs/reference/python-api.md` | Python API 参考 | 对外 API 变化 |
| `docs/reference/webui-api.md` | WebUI REST API 参考 | WebUI 路由变化 |
| `docs/reference/cli.md` | CLI 参考 | 命令名称、参数变化 |
| `README.md` | 项目总览 | 用法、特性、依赖版本 |

### SKILL 文件

| SKILL | 内容 | 同步触发 |
|-------|------|----------|
| `framework-quickstart` | 架构快速理解、模块导读 | 新增/删除模块、模块位置变化、依赖关系变化 |
| `external-integration` | 外部接入指南、API 用法 | AMKR 边界变化、配置变化、API 变化 |
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
- 新增/删除 WebUI/API 路由或页面

**必须更新**：
- [ ] `docs/guide/architecture-overview.md` - 新增模块说明与数据流/执行流
- [ ] `.github/skills/framework-quickstart/SKILL.md` - 更新阅读顺序和模块描述
- [ ] `.github/skills/external-integration/SKILL.md` - 若涉及外部接入
- [ ] 所有其他 SKILL 的推荐读取顺序（若改变了模块位置）
- [ ] 对应的 `tests/test_*.py` 文件

**示例提示**：
```
检测到新增模块: sirius_pulse/<new_module>/
请更新：
1. docs/guide/architecture-overview.md - 在「核心边界」中补充该模块说明
2. .github/skills/framework-quickstart/SKILL.md - 在阅读顺序添加对应模块
3. 新增 tests/test_<new_module>.py
```

#### B. 接口/API 变更（中-高影响）

**触发条件**：
- 修改顶层公开接口（公开符号统一从 `sirius_pulse/__init__.py` 导出，没有 `sirius_pulse/api/` 子模块）
- 修改 `EmotionalGroupChatEngine` 的公开方法签名
- 新增/删除 CLI 命令（`sirius-pulse`）
- 新增/删除 WebUI 路由（`sirius_pulse/webui/routes.py`）
- 配置结构变化

**必须更新**：
- [ ] `docs/reference/python-api.md` / `docs/reference/cli.md` - 使用示例
- [ ] `docs/reference/webui-api.md` - 若涉及 WebUI 路由
- [ ] `.github/skills/external-integration/SKILL.md` - API 说明
- [ ] `README.md` - 快速开始示例

**示例提示**：
```
检测到 API 变更: EmotionalGroupChatEngine.process_message() 签名变化
请更新：
1. docs/reference/python-api.md - 更新方法说明和示例
2. README.md - 更新快速开始代码
```

#### C. 细节实现变更（中影响）

**触发条件**：
- `sirius_pulse/models/models.py` 的消息 / transcript 契约变化
- `sirius_pulse/config/models.py` 的 session / workspace / orchestration 契约变化
- 系统提示词生成逻辑改动
- 记忆压缩、缓存策略逻辑修改

**必须更新**：
- [ ] `docs/guide/architecture-overview.md` - 对应部分的详解
- [ ] `.github/skills/framework-quickstart/SKILL.md` - 心智模型部分
- [ ] 对应的 tests 文件

**示例提示**：
```
检测到数据契约变化: SessionConfig / WorkspaceConfig / Transcript 新增字段
请更新：
1. docs/guide/architecture-overview.md - 更新模型说明
2. docs/reference/python-api.md - 若外部调用契约受影响则补充说明
3. 对应 tests 文件 - 补充测试覆盖
```

#### D. 配置/依赖变更（中-低影响）

**触发条件**：
- `pyproject.toml` 的版本/依赖变化
- `config/manager.py` 的配置选项变化
- `.pre-commit-config.yaml` 的工具版本变化

**必须更新**：
- [ ] `docs/guide/configuration.md` - 新增配置选项说明
- [ ] `docs/reference/global-config.md` - 若涉及全局配置字段
- [ ] `README.md` - 依赖版本、安装步骤

**示例提示**：
```
检测到依赖变更: 新增 redis>=4.0
请更新：
1. README.md - 依赖安装说明
2. docs/guide/configuration.md - Redis 配置选项
```

#### E. 工具/流程变更（低影响）

**触发条件**：
- `.github/workflows/` 的 CI/CD 流程变化
- `.pre-commit-config.yaml` 的钩子或工具版本变化
- `scripts/` 下的工具脚本变化

**必须更新**：
- [ ] 对应的 SKILL 文件（若涉及开发流程）
- [ ] `README.md` - 开发指南部分

## 变更检查清单（快速对照）

### 【新增/修改模块时】

- [ ] 新模块已在 `sirius_pulse/<module>/` 下创建
- [ ] 新模块包含 `__init__.py` 导出公开接口（顶层符号同步到 `sirius_pulse/__init__.py`）
- [ ] 新增 `tests/test_<module>.py` 单元测试
- [ ] 所有新增类/函数都有完整的文档字符串和类型注解
- [ ] `docs/guide/architecture-overview.md` 已补充模块说明和设计初衷
- [ ] `framework-quickstart SKILL` 已更新阅读顺序和模块描述
- [ ] `external-integration SKILL` 已更新（若涉及外部接入）
- [ ] 可用性检查：`python main.py --help` 执行正常
- [ ] 自测：执行一次完整的会话流程测试

### 【修改现有接口时】

- [ ] 所有修改都有类型注解
- [ ] 向后兼容性已确认（或有明确的弃用计划）
- [ ] `sirius_pulse/__init__.py` 中的顶层导出已同步更新
- [ ] `docs/reference/python-api.md` 已补充新用法说明
- [ ] `tests/` 的相关测试已更新
- [ ] `README.md` 的快速开始已验证
- [ ] 破坏性变更已在 `CHANGELOG.md` 记录

### 【配置/依赖变更时】

- [ ] `pyproject.toml` 已正确更新
- [ ] 可选依赖已在 `[project.optional-dependencies]` 中声明
- [ ] `docs/guide/configuration.md` 已补充新配置项说明
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

### 场景 1：新增模块

```
变更：创建 sirius_pulse/<new_module>/{...}.py

检查清单：
✓ 在 docs/guide/architecture-overview.md 的「核心边界」表格中补充该模块说明
✓ 在 framework-quickstart SKILL 的"阅读顺序"中添加新模块
✓ 在 external-integration SKILL 中补充使用示例（若对外可见）
✓ 新增 tests/test_<new_module>.py
✓ tests/test_*.py 中的所有相关测试通过
✓ python main.py --help 可正常执行
```

### 场景 2：修改 SessionConfig / WorkspaceConfig 数据结构

```
变更：在 sirius_pulse/config/models.py 中新增 SessionConfig 或 WorkspaceConfig 字段

检查清单：
✓ 字段包含完整类型注解和文档字符串
✓ 在 config/models.py 中补充字段说明
✓ 在 docs/guide/architecture-overview.md 中更新对应数据模型描述
✓ 在 docs/reference/python-api.md 中补充使用示例
✓ tests/test_config.py 或相关测试中有相应覆盖
✓ 旧代码的兼容性已确认（提供默认值或迁移逻辑）
```

### 场景 3：新增认知任务名

任务名是 Sirius Pulse 与 AMKR 之间的**唯一线协议**：框架把任务名填进 OpenAI 兼容请求的 `model` 字段，AMKR 用它查任务定义换成真实模型。因此新增一个认知任务 = 新增一个任务名，**不是**新增一个 Provider 实现。

```
变更：在 sirius_pulse/core/model_router.py 的 _DEFAULT_TASK_REGISTRY 中新增任务名

检查清单：
✓ 任务名已加入 _DEFAULT_TASK_REGISTRY（键与 TaskConfig.model_name 一致）
✓ TaskConfig 只填本地字段（timeout / retries）与预算估算用的默认值；
  模型、temperature、max_tokens 一律不在此决定，留给 AMKR 的任务定义
✓ 任务名无需手工登记：known_task_names() 直接读取注册表键，
  引擎启动时会自动把它注册进 AMKR 的 <amkr_workspace>/<persona> 空间
✓ 确认注册是「只创建缺失」：已存在的任务不会被比对或覆盖
✓ 在 sirius_pulse/webui/model_catalog.py 的 TASK_LABELS 中补中文标签（可选但推荐）
✓ 在 docs/modules/provider-system.md 与 docs/reference/provider-config.md 的 12 个内置任务名清单中同步
✓ 新增或扩展 tests/test_model_router.py 覆盖该任务名
✓ 若任务有独立调用点，确认它通过 engine.model_router.resolve("<task>") 取配置

注意：不要在 sirius_pulse/providers/ 下新增厂商实现或路由注册表——
供应商、Key 池、模型选择与采样参数都由 AMKR 承担，加回来属于架构回退。
```

### 场景 4：新增 WebUI 页面 / REST API

```
变更：在 sirius_pulse/webui/routes.py 中新增 RouteSpec，并在 server_core.py 实现 handler

检查清单：
✓ RouteSpec 已登记（method + path + handler_name），handler 与名称一致
✓ 写操作需要鉴权时沿用既有 auth 流程，不要在页面里绕过
✓ 返回契约同步到 docs/reference/webui-api.md
✓ 前端静态页面已同步（sirius_pulse/webui/static/）
✓ 新增或扩展 tests/test_webui_routes.py
✓ 若涉及密钥字段（如 amkr_local_api_key），响应中必须脱敏为 sk-a****，
  且提交脱敏值时保留磁盘原值
```

## 防护与约定

1. **同步时间点**：每次代码提交前或在 skill-sync-enforcer 触发后立即执行。
2. **优先级顺序**：优先同步 `docs/` > `SKILL` > `README.md`。
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
- [ ] 更新了 `README.md`（若需要）
- [ ] 所有文档中的代码示例都已验证可执行
- [ ] 所有 SKILL 的 frontmatter 格式正确（`name:` 和 `description:` 完整）
- [ ] 提交说明清晰地列出了本次同步的内容
- [ ] 没有产生与 skill-sync-enforcer 的重复/冲突

