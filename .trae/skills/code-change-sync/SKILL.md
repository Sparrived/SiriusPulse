---
name: code-change-sync
description: "当修改 Sirius Pulse 代码后，强制同步代码与文档、SKILL、示例的一致性。涵盖架构变更、接口变更、配置变更的全量检查清单与项目结构地图。关键词：代码同步、文档联动、变更追踪、检查清单、项目结构。"
---

# 代码变更同步指南

## 目标

在修改 `sirius_pulse` 代码后，确保所有相关文档、SKILL、示例和架构信息保持一致。本 SKILL 合并了原 `skill-sync-enforcer` 的约束清单和原 `project-structure-sync` 的项目结构地图与变更追踪流程。

## 语言规范（强制）

- 所有 SKILL 文件必须保持中文。
- 任何后续新增/修改 SKILL 的任务，必须将 `description` 和正文写为中文。
- 若本次变更触及 SKILL 且包含英文内容，必须在同次任务中完成中文化。

## 触发条件

当以下文件发生代码变更时，必须执行本流程：
- `sirius_pulse/**/*.py`
- `main.py`
- `pyproject.toml`

## 核心原则

1. **内部实现可直接重构**；当前项目未发布，若影响对外接口可直接调整，但必须同步更新文档、示例与测试。
2. **任何新增内部功能都必须在统一对外接口层暴露可调用入口**。
3. **所有对外 Python 接口统一收敛在 `sirius_pulse/__init__.py`**，禁止在多个内部模块分散暴露新入口。
4. **新功能实现后，不应生成额外的 markdown 文档**（如使用指南、快速启动、参考手册等），除非用户明确提及。应将功能文档集中在现有的对应位置（如 `docs/architecture.md`、`docs/external-usage.md` 等），或在用户主动要求时才生成专门的指南。
5. 若代码已变更但未在同一任务中审阅并同步 SKILL/文档，**不得声明任务完成**。

---

## 项目结构地图

### 核心模块层级

```
sirius_pulse/
├── __init__.py              # 顶层公开 API 统一重导出（严格 __all__）
├── persona_manager.py       # 多人格生命周期管理（主进程）
├── persona_worker.py        # 单个人格子进程入口
├── persona_config.py        # 人格级配置模型（adapters/experience/paths）
├── background_tasks.py      # 后台任务管理器（内存压缩、数据清理、记忆归纳）
├── mixins.py                # JsonSerializable 数据类混入
├── developer_profiles.py    # 开发者身份校验辅助
├── trait_taxonomy.py        # 用户特征分类体系（7类350+关键词）
├── exceptions.py            # 自定义异常
├── logging_config.py        # 日志配置（按日轮转、7天备份、归档）
├── core/                    # 编排核心（Mixin 架构）
│   ├── emotional_engine.py  # EmotionalGroupChatEngine 最终类（多重继承组合）
│   ├── engine_core.py       # _EmotionalGroupChatEngineBase 基类（__init__、API、持久化）
│   ├── pipeline.py          # PipelineMixin（5 阶段管线：感知→认知→决策→执行→后台）
│   ├── prompt_factory.py    # PromptFactory（无状态 prompt 构建工具类，含 StyleAdapter 风格适配）
│   ├── bg_tasks.py          # BackgroundTasksMixin（6 个后台任务，含延迟回复/主动触发 prompt 构建）
│   ├── helpers.py           # HelpersMixin（技能集成、被动 SKILL 注册与触发分发、token 记录）
│   ├── skill_engine_context.py # SkillEngineContextImpl（被动 SKILL 与引擎交互适配器）
│   ├── cognition.py         # 统一认知分析器（情绪+意图联合推断）
│   ├── response_strategy.py # 四层响应策略（IMMEDIATE/DELAYED/SILENT/PROACTIVE）
│   ├── model_router.py      # 任务感知模型选择
│   ├── threshold_engine.py  # 动态阈值引擎
│   ├── rhythm.py            # 对话节奏分析
│   ├── events.py            # 会话事件流（PERCEPTION/COGNITION/DECISION/EXECUTION/DELAYED/PROACTIVE/DEVELOPER_CHAT/REMINDER）
│   ├── identity_resolver.py # 跨平台身份解析
│   ├── delayed_response_queue.py # 延迟响应队列
│   ├── proactive_trigger.py # 主动触发器
│   ├── persona_generator.py # 人格生成器
│   ├── persona_store.py     # 人格持久化
│   ├── orchestration_store.py # 编排配置持久化
│   ├── engine_persistence.py # 引擎状态持久化
│   ├── markers.py           # 消息标记
│   └── utils.py             # 核心工具函数
├── embedding/               # Embedding 微服务
│   ├── server.py            # aiohttp 服务端（asyncio.Queue 批量合并推理）
│   ├── client.py            # 同步客户端（urllib）
│   └── __main__.py          # python -m sirius_pulse.embedding 启动入口
├── persona_generation/      # 人格资产生成子包
│   ├── templates.py         # 数据模型与文件 I/O
│   └── builders.py          # LLM 异步生成（原顶层 prompt_templates / roleplay_prompting 迁移至此）
├── async_engine/            # 兼容导出 + prompts/orchestration/utils 辅助层
├── memory/                  # 记忆子包
│   ├── basic/               # 基础记忆（滑动窗口+热度+归档）
│   ├── diary/               # 日记记忆（LLM生成、索引、ChromaDB 向量存储、检索）
│   ├── semantic/            # 语义记忆（群规范、氛围、关系状态）
│   ├── glossary/            # 名词解释（AI自身知识，支持人格级隔离与迁移）
│   ├── user/                # 用户管理（极简UserProfile+群隔离）
│   ├── context_assembler.py # 上下文组装器
│   └── cognition_store.py   # 认知状态持久化
├── models/                  # 数据契约
│   ├── models.py            # Message/Participant/Transcript/User
│   ├── persona.py           # PersonaProfile
│   ├── emotion.py           # EmotionState/AssistantEmotionState/EmpathyStrategy
│   ├── intent_v3.py         # IntentAnalysisV3/SocialIntent
│   └── response_strategy.py # StrategyDecision/ResponseStrategy
├── config/                  # 配置系统
│   ├── models.py            # SessionConfig/WorkspaceConfig/AgentPreset等
│   ├── manager.py           # ConfigManager（多环境配置）
│   ├── helpers.py           # 配置辅助函数（多模态、多模型配置）
│   ├── jsonc.py             # JSONC解析器
│   └── loaders/             # 配置加载器
├── providers/               # Provider实现、路由
│   ├── base.py              # LLMProvider/AsyncLLMProvider协议
│   ├── routing.py           # AutoRoutingProvider/ProviderConfig/WorkspaceProviderManager
│   ├── openai_compatible.py # 通用OpenAI兼容Provider
│   ├── deepseek.py          # DeepSeek
│   ├── aliyun_bailian.py    # 阿里云百炼
│   ├── bigmodel.py          # 智谱AI
│   ├── siliconflow.py       # SiliconFlow
│   ├── volcengine_ark.py    # 火山方舟
│   ├── ytea.py              # YTea
│   ├── mock.py              # 测试用MockProvider
│   └── response_utils.py    # 响应处理工具
├── platforms/               # 平台适配层
│   ├── napcat_manager.py    # NapCat全局安装/多实例管理
│   ├── napcat_adapter.py    # OneBot v11 WebSocket客户端
│   ├── napcat_bridge.py     # QQ群聊/私聊桥接器
│   ├── runtime.py           # EngineRuntime封装（单个人格子进程内）
│   └── persona_utils.py     # 人格生成工具函数
├── skills/                  # SKILL系统
│   ├── registry.py          # SKILL注册与发现
│   ├── executor.py          # SKILL执行（参数校验、重试、遥测）
│   ├── security.py          # 开发者权限校验
│   ├── models.py            # SkillDefinition/SkillResult/SkillPassiveType/BackgroundTaskSpec/TriggerSpec/SkillEngineContext 等数据模型
│   ├── data_store.py        # SKILL独立JSON数据存储
│   ├── dependency_resolver.py # 依赖自动解析安装
│   ├── telemetry.py         # 执行遥测记录
│   ├── builtin/             # 内置技能（system_info/learn_term/bing_search/file_list/file_read/file_write/upload_file/send_image/send_workspace_file/reminder/desktop_screenshot/url_content_reader/weather等）
│   └── sticker/             # 表情包子系统（RAG 向量检索、偏好管理、学习、反馈、新鲜度）
├── session/                 # 会话持久化
│   └── store.py             # JsonSessionStore/SqliteSessionStore/SessionStoreFactory
├── token/                   # Token统计
│   ├── usage.py             # Token使用记录
│   ├── store.py             # SQLite持久化
│   ├── analytics.py         # 成本分析
│   └── utils.py             # Token估算工具
├── utils/                   # 工具
│   └── layout.py            # WorkspaceLayout路径布局
└── webui/                   # WebUI管理面板
    ├── server.py            # aiohttp REST API 主入口
    ├── server_core.py       # 核心路由与基础设施
    ├── server_utils.py      # 共享工具函数（_json_response、_get_name，避免循环导入）
    ├── persona_api.py       # 人格管理 API
    ├── memory_api.py        # 记忆管理 API
    ├── napcat_api.py        # NapCat 管理 API
    ├── server_skill_api.py  # SKILL 管理 API
    └── static/              # 前端页面（16 个页面）
```

### 关键配置文件

| 文件 | 用途 | 监控变化 |
|------|------|--------|
| `pyproject.toml` | 项目元数据、依赖、入口 | 版本、依赖、命令名称 |
| `.github/workflows/ci.yml` | CI/CD 流程 | Python 版本、测试命令 |
| `.github/workflows/publish.yml` | PyPI发布流程 | 发布配置 |
| `Makefile` | 开发便利命令 | 命令定义 |
| `scripts/` | 设置/工具脚本 | 新增脚本 |

### 文档文件

| 文件 | 内容 | 同步触发 |
|------|------|----------|
| `docs/architecture.md` | 架构设计详解 | 模块重构、新增模块、接口变化 |
| `docs/full-architecture-flow.md` | 完整架构流程图 | 数据流、执行流变化 |
| `docs/engine-deep-dive.md` | 情感化群聊引擎深度解析 | 引擎行为变更 |
| `docs/persistence-system.md` | 持久化系统（记忆+会话+Token） | 记忆系统变更 |
| `docs/persona-lifecycle.md` | 多人格生命周期 | 人格管理变更 |
| `docs/skill-guide.md` | SKILL 系统指南 | 技能系统变更 |
| `docs/provider-system.md` | Provider系统 | Provider变更 |
| `docs/platforms.md` | 平台适配层 | 平台适配变更 |
| `docs/configuration-guide.md` | 配置指南 | 配置选项新增、参数变化 |
| `docs/best-practices.md` | 最佳实践 | 性能优化、模式新增 |
| `docs/project-issues.md` | 项目问题跟踪 | 已知问题、风险分析 |
| `docs/change-impact-guide.md` | 变更联动确认指南 | 后端/配置/契约变更后需同步检查的前端、API、文档位置速查表 |
| `docs/README.md` | 文档索引 | 文档结构变化 |
| `README.md` | 项目总览 | 用法、特性、依赖版本 |

### SKILL 文件

| SKILL | 内容 | 同步触发 |
|-------|------|----------|
| `framework-quickstart` | 架构快速理解、模块导读 | 新增/删除模块、模块位置变化 |
| `external-integration` | 外部接入指南、API 用法 | Provider 变化、配置变化、API 变化 |
| `code-change-sync` | 代码变更检查清单 | 所有满足触发条件的变更 |
| `commit-preparation` | Commit 前检查 | ChangeLog 格式、版本信息变化 |
| `release-management` | 发布管理 | 版本信息、文档同步状态 |
| `write-tests` | 测试编写规范 | 测试模式变化 |
| `debug-diagnosis` | 调试诊断 | 核心模块异常排查 |

---

## 变更追踪流程

### 步骤 1：识别变更类型

```bash
# 查看最近的代码变更
git log --oneline -n 5

# 查看改动文件列表
git diff HEAD~1 --name-only

# 查看具体改动统计
git diff HEAD~1 --stat
```

### 步骤 2：变更分类与影响范围

#### A. 模块级变更（高影响）

**触发条件**：
- 新增 `sirius_pulse/<module_name>/` 目录及 `.py` 文件
- 删除现有模块
- 模块文件重构（拆分/合并）
- 新增 Provider 类型

**必须更新**：
- [ ] `docs/architecture.md` - 新增模块说明
- [ ] `docs/full-architecture-flow.md` - 更新数据流/执行流
- [ ] `.trae/skills/framework-quickstart/SKILL.md` - 更新阅读顺序和模块描述
- [ ] `.trae/skills/external-integration/SKILL.md` - 若涉及外部接入
- [ ] 对应的 `tests/test_*.py` 文件

#### B. 接口/API 变更（中-高影响）

**触发条件**：
- 修改 `sirius_pulse/__init__.py` 中的公开接口
- 修改 `EmotionalGroupChatEngine` 的公开方法签名
- 新增/删除 CLI 命令（`main.py`）
- 配置结构变化
- 修改 `sirius_pulse/webui/*.py` 中的 API 路由或返回字段

**必须更新**：
- [ ] `docs/architecture.md` - 使用示例
- [ ] `docs/change-impact-guide.md` - 按对应章节检查前端/API/文档联动
- [ ] `.trae/skills/external-integration/SKILL.md` - API 说明
- [ ] `README.md` - 快速开始示例
- [ ] `examples/*.py` 或 `examples/*.json` - 实际示例代码
- [ ] 若涉及 WebUI API：同步检查前端 `core.js` / `config.js` / 对应 `.html` 页面

#### C. 细节实现变更（中影响）

**触发条件**：
- `sirius_pulse/models/models.py` 的消息 / transcript 契约变化
- `sirius_pulse/config/models.py` 的 session / workspace 契约变化
- `sirius_pulse/persona_config.py` 的人格级配置字段变化
- 系统提示词生成逻辑改动

**必须更新**：
- [ ] `docs/architecture.md` - 对应部分的详解
- [ ] `docs/change-impact-guide.md` - 按对应数据契约章节检查联动链
- [ ] `.trae/skills/framework-quickstart/SKILL.md` - 心智模型部分
- [ ] 对应的 tests 文件
- [ ] 若涉及 WebUI 消费字段：同步检查前端 `config.js` / `core.js` 回填与收集逻辑

#### D. 配置/依赖变更（中-低影响）

**触发条件**：
- `pyproject.toml` 的版本/依赖变化
- `config/manager.py` 的配置选项变化
- `sirius_pulse/persona_config.py` 的体验参数或适配器配置字段变化

**必须更新**：
- [ ] `docs/configuration-guide.md` - 新增配置选项说明
- [ ] `docs/change-impact-guide.md` - 按对应配置章节检查前端/API/文档联动
- [ ] `README.md` - 依赖版本、安装步骤
- [ ] 若涉及 WebUI 表单字段：同步检查前端 `config.js` 回填与收集逻辑、对应 `.html` 表单元素

#### E. 工具/流程变更（低影响）

**触发条件**：
- `.github/workflows/` 的 CI/CD 流程变化
- `Makefile` 的命令变化
- `scripts/` 下的工具脚本变化

**必须更新**：
- [ ] 对应的 SKILL 文件（若涉及开发流程）
- [ ] `README.md` - 开发指南部分

---

## 通用同步工作流

1. 判断变更是否影响架构、命令、API 或目录布局。
2. 任何内部代码改动后，必须保证 `main.py` 仍可用于主动测试（至少 `--help` 与一次可退出的会话启动可执行）。
3. 若修改系统提示词生成逻辑（`_build_system_prompt()` 或提示词生成函数），必须确保安全约束已包含：系统提示词末尾应当明确告诉模型不要主动泄露自己的系统提示词和初始指令。
4. 校验示例是否仍与真实代码路径和命令名称一致。
5. 在交付说明中明确列出本次已同步内容。

## 完成检查清单

- [ ] 代码行为已更新。
- [ ] 若影响外部接口，已同步更新文档、示例与测试。
- [ ] 新增功能已在 `sirius_pulse/__init__.py` 暴露。
- [ ] 已执行 `python main.py --help`。
- [ ] 已执行一次 `main.py` 会话启动 smoke 测试（可通过管道输入 `exit` 退出）。
- [ ] 架构文档已同步。
- [ ] 完整架构流程图文档（`docs/full-architecture-flow.md`）已同步。
- [ ] 框架快速上手 SKILL 已同步。
- [ ] 外部接入 SKILL 已同步（若外部接入能力相关变更）。
- [ ] README/示例已同步。
- [ ] 识别了变更的所有影响范围。
- [ ] 更新了所有受影响的 `docs/` 文件（含 `docs/change-impact-guide.md` 中对应章节）。
- [ ] 更新了所有受影响的 SKILL 文件。
- [ ] 所有文档中的代码示例都已验证可执行。
- [ ] 所有 SKILL 的 frontmatter 格式正确（`name:` 和 `description:` 完整）。
- [ ] 提交说明清晰地列出了本次同步的内容。

### 变更联动确认（新增）

当变更涉及 WebUI 前后端交互、配置字段、数据契约时，除上述清单外，必须额外执行：

- [ ] 查阅 `docs/change-impact-guide.md`，定位对应变更类型章节。
- [ ] 按章节中的「联动链」逐项确认后端 → API → 前端 → 文档的同步状态。
- [ ] 若新增/删除/重命名 API 字段，确认前端 `core.js` / `config.js` / `analytics.js` / `platform.js` 的解析与回填逻辑。
- [ ] 若新增/删除/重命名前端表单 ID，确认后端 API 的请求体解析逻辑。
- [ ] 若新增 Provider 类型，确认 `config.js` 三处常量（`PROVIDER_TYPE_OPTIONS`、`PROVIDER_DEFAULT_URLS`、`BUILTIN_PROVIDER_TYPES`）已同步。

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
grep -o "sirius_pulse/[a-z_/]*\.py" .trae/skills/framework-quickstart/SKILL.md | sort -u

# 5. 验证所有 SKILL 文件的 frontmatter 格式
grep -r "^name:" .trae/skills/*/SKILL.md
grep -r "^description:" .trae/skills/*/SKILL.md
```

## 常见同步场景

### 场景 1：新增 Provider 类型

```
变更：实现 sirius_pulse/providers/new_platform.py

检查清单：
✓ 新 Provider 继承 OpenAICompatibleProvider 或实现 LLMProvider 协议
✓ 在 sirius_pulse/providers/__init__.py 中导出
✓ 在 sirius_pulse/providers/routing.py 中注册路由
✓ 在 sirius_pulse/__init__.py 中暴露
✓ 在 docs/provider-system.md 中补充接入说明
✓ 在 docs/change-impact-guide.md 中按 4.1 节更新 Provider 联动链
✓ 在 framework-quickstart SKILL 中更新 providers 部分说明
✓ 新增 tests/test_providers_new_platform.py
```

### 场景 2：修改 SessionConfig / PersonaConfig 数据结构

```
变更：在 sirius_pulse/config/models.py 或 persona_config.py 中新增字段

检查清单：
✓ 字段包含完整类型注解和文档字符串
✓ 在 docs/architecture.md 中更新对应数据模型描述
✓ 在 docs/change-impact-guide.md 中按对应章节更新联动链（如 3.1/3.2/3.3）
✓ 若字段被 WebUI 消费：同步更新 persona_api.py 白名单、config.js 回填/收集逻辑、对应 .html 表单
✓ 更新 examples/ 示例配置（若适用）
✓ tests/ 相关测试中有相应覆盖
✓ 旧代码的兼容性已确认（提供默认值或迁移逻辑）
```

### 场景 3：新增内置 SKILL

```
变更：在 sirius_pulse/skills/builtin/ 新增 skill

检查清单：
✓ 实现 SKILL_META + run() 函数
✓ 在 skills/builtin/__init__.py 中注册（如果需要）
✓ 在 docs/skill-guide.md 中补充说明
✓ 在 docs/change-impact-guide.md 中按 7.1 节更新 Skill 联动链
✓ 新增 tests/test_skills_*.py
```

### 场景 4：修改 WebUI API 路由或返回字段

```
变更：修改 sirius_pulse/webui/*.py 中的 API 行为

检查清单：
✓ 前端 core.js / config.js / analytics.js / platform.js 中解析该响应的代码已同步
✓ 前端对应 .html 页面中通过 $('id') 回填的字段 ID 是否匹配
✓ 若字段删除，前端无残留引用
✓ 若字段重命名，前后端同时改，避免半同步状态
✓ docs/webui.md 中「API 路由总览」表格已更新
✓ docs/change-impact-guide.md 中 2.1/2.2 节已更新
```

## 防护与约定

1. **同步时间点**：每次代码提交前或在 code-change-sync 触发后立即执行。
2. **优先级顺序**：优先同步 `docs/` > `SKILL` > `README.md` > `examples/`。
3. **文档一致性检查**：
   - 所有 SKILL 中提及的模块路径必须真实存在
   - 所有 SKILL 中的阅读顺序必须反映当前的模块依赖关系
   - 所有示例代码必须能够实际执行
4. **提交消息格式**：
   - 代码变更：`feat: <description>` / `fix: <description>`
   - 文档同步：`docs: 同步 <具体同步内容>`
   - 例如：`docs: 同步 framework-quickstart 和 external-integration SKILL`
