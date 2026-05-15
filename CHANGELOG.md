# 变更日志

本文档记录 Sirius Chat 的所有版本变更。采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 规范。

## [Unreleased]

### Added

- **记忆可视化 WebUI 页面**：新增 `memory-viz.html` 记忆浏览器与知识图谱可视化，后端 `memory_api.py` 提供记忆 CRUD、搜索、知识图谱、统计 API。
- **技能管理 WebUI 页面**：新增 `skills.html` 技能管理页面（状态/安装/配置卡片），后端 `server_skill_api.py` 提供技能状态、安装、配置管理 API。
- **Token 用量分析图表**：`analytics.js` 新增 Token 用量可视化模块。

### Fixed

- **Pylance mixin 类型错误**：`engine_core.py` 添加 `TYPE_CHECKING` 条件桩方法声明；`pipeline.py`/`bg_tasks.py`/`helpers.py` 添加条件继承，消除 mixin 方法不可见诊断。
- **Pylance 真实代码 bug**：`bg_tasks.py` 修复 `resolved_uid` 未绑定、`build_skill_status_message` 参数缺失、`_round`/`calls`/`reply` 未初始化；`cognition.py`/`bg_tasks.py` 添加 `provider_async` None 守卫。
- **Pylance server_core 子类方法不可见**：`server_core.py` 添加 `TYPE_CHECKING` 桩方法声明；`FileResponse` 返回类型改为 `StreamResponse`。
- **Pylance napcat_bridge 缺失导入**：补充 `Callable` 导入。
- **表情包系统**：修复标签解析、缓存策略、上下文注入与过滤条件。
- **embedding 重试退避**：`client.py` 添加连接失败重试与指数退避策略；`server.py` 优化请求队列处理。
- **上下文截断**：`context_assembler.py` 修复截断逻辑。
- **技能执行器**：`executor.py` 修复 `parse_skill_calls` 返回类型与 `strip_skill_calls` 边界处理。

## [1.1.0] - 2026-05-01

### Added

- **单旋钮活泼度 `ExpressivenessConfig`**：`experience.json` 新增 `expressiveness: float = 0.5`（0~1），自动推导 8 个行为阈值（指向门槛、间隙门槛、资格感、冷却时间、去重检测、讽刺加分、主动间隙等）。WebUI Experience 页面提供四象限可视化 + 点击设值。
- **12维指向性打分系统**：`IntentAnalysisV3` 新增 mention/reference/name_match/second_person/question/imperative/topic_relevance/emotional_disclosure/attention_seeking/recency/turn_taking 共 12 维原始信号，合成 `directed_score`（0~1）。规则引擎保底，LLM 语义增强。
- **讽刺检测 `sarcasm_score`**：5 类启发式规则（正面词+负面标点、引号强调、过度笑声、反讽句式、emoji-文本矛盾），≥0.4 时 `directed_score` 额外 +15%。
- **资格感判断 `entitlement_score`**：计算 AI persona 与消息话题的重叠度，`< threshold` 时决策阈值 ×1.5。
- **话题间隙检测 `turn_gap_readiness`**：`RhythmAnalysis` 新增对话自然转折就绪度（问句+转换词+低稳定性+长沉默 vs 爆发-独白），用于 IMMEDIATE→DELAYED 降级和主动发言抑制。
- **表达去重**：`_expression_similarity()` 字符二元组 Jaccard，>threshold 时追加提示要求 LLM 换说法重试一次。
- **情绪基调同步（Tone Alignment）**：`_get_tone_alignment()` 从群氛围历史提取 valence/arousal，映射为 5 种基调提示注入 system prompt。
- **other_ai 折扣**：`sender_type == "other_ai"` 时 `directed_score = min(score, score*0.5+0.1)`，避免 AI 互聊过度。
- **用户画像 WebUI 页面**：后端 `/api/personas/{name}/users`，前端 `pages/users.html` + `analytics.js`，展示关系状态、兴趣图谱、群聊筛选。
- **认知分析 WebUI 扩展**：`cognition.html` 新增 12维指向性雷达图、最近认知事件表格（含 sarcasm/entitlement/gap_readiness）、群筛选下拉框；情感分布饼图改为横向条形图 + 中文标签。
- **Cognition Store Schema v2**：`cognition_events.db` 新增 `directed_score`、`sarcasm_score`、`entitlement_score`、`turn_gap_readiness`、`directed_signals` 列，支持旧库自动迁移（`ALTER TABLE ADD COLUMN`）。
- **WebUI 用户画像 API**：补全缺失的 `api_persona_users_get` 和 `api_persona_user_get` 方法。
- **Token 估算精度修复**：`(output_chars + 3) // 4` → `estimate_tokens()`（优先 tiktoken），`PromptTokenBreakdown` 新增 `output_total`，`total` 含输入+输出。

### Changed

- **WebUI Experience 页面重排**：按功能拆分为 5 个卡片——行为风格（含四象限图）、主动行为、回复控制、技能与资源、记忆与身份。
- **情感标签中文化**：所有 WebUI 图表和表格中的英文基础情感（JOY→喜悦、SADNESS→悲伤等）统一映射为中文。
- **情感时间线 tooltip**：显示中文情感标签 + 各维度中文状态描述（积极/愉快、兴奋/激动等）。

### Fixed

- **`SkillDataStore.set()` 重复标记**：移除多余的 `self._dirty = True`。
- **`MultiModelConfig.to_dict()` 崩溃**：修复 copy-paste 导致的 `AttributeError`（原代码引用已删除的 `WorkspaceConfig` 字段）。
- **删除 `UserMemoryManager`**：依赖的 `sirius_chat.memory.quality.models` 模块不存在，直接删除该管理器。
- **`pop_reminders` 兼容性**：将 `adapter_type` 为空字符串的 reminder 视为通配符，兼容旧版 reminder。
- **移除硬编码默认群号**：`napcat_adapter.py` 不再使用默认群 ID `728196560`。
- **图片缓存使用内容哈希**：`MD5(url)` → `MD5(下载内容)`，避免同图不同 URL 的重复下载。
- **Token 估算 CJK 感知**：`len(text)//4` 替换为 `estimate_tokens_heuristic`，更准确处理中英文混合文本。
- **文档与示例修复**：`README.md` 代码示例、AGENTS.md 依赖说明、workspace-runtime.md 弃用横幅。

### Changed

- **CognitionAnalyzer 简化**：`_dynamic_threshold` / `_decide_strategy` 内联为直接调用 `ThresholdEngine` / `ResponseStrategyEngine`，减少一层无意义转发。
- **Skill-chain regex 提升为模块常量**：避免每次调用重复编译正则。
- **MockProvider 移除 brittle 中文检测**：不再依赖 `

### Changed

- **版本号提升至 1.0.0**：框架进入稳定版本。
- **默认引擎**：`EmotionalGroupChatEngine` 成为唯一推荐引擎，`AsyncRolePlayEngine` 相关 legacy API 已移除。
- **Proactive 时间窗口**：新增 `active_start_hour`/`active_end_hour` 配置（默认 12:00-21:00），限制主动消息触发时段。
- **Proactive 氛围抑制**：`atmosphere` 类型 trigger 新增 `min_silence` 检查（默认 5 分钟），避免消息刚到达时立即触发。
- **Proactive 状态加载鲁棒性**：`_load_proactive_state` 强制 `str()` 转换 group_id，避免 int/str 类型不匹配导致黑名单失效；`load_state()` 通过 `try/finally` 确保 proactive 状态总能加载。

### Removed

- **移除 `create_async_engine` 公开 API**：`AsyncRolePlayEngine` 仍作为内部运行时组件保留（`WorkspaceRuntime` 依赖），但不再通过 `sirius_chat.api` 公开导出。
- **删除 session store legacy migration**：`_migrate_legacy_storage_if_needed` 及对应测试已移除。
- **删除 user memory auto-migration**：`UserMemoryFileStore.load_all()` 不再自动检测并迁移旧格式。

## [1.0.1] - 2026-04-28

### Added (Multi-Persona Architecture)

- **`PersonaManager`**：主进程多人格生命周期管理。扫描 `data/personas/` 目录，维护端口注册表，创建/删除/迁移人格，子进程启停，PID 存活检测。
- **`PersonaWorker`**：单个人格子进程入口。独立控制台窗口（Windows `CREATE_NEW_CONSOLE`），加载人格配置，创建 `EngineRuntime`，定期心跳写入 `worker_status.json`。
- **`PersonaConfig`**：人格级配置模型——`NapCatAdapterConfig`、`PersonaAdaptersConfig`、`PersonaExperienceConfig`、`PersonaConfigPaths`。
- **数据隔离**：`data/personas/{name}/` 含 `persona.json`、`orchestration.json`、`adapters.json`、`experience.json`、`engine_state/`、`memory/`、`diary/`、`image_cache/`、`skill_data/`、`logs/`。
- **端口自动分配**：`PersonaManager` 维护 `data/adapter_port_registry.json`，从 3001 递增自动分配 NapCat WebSocket 端口。
- **NapCat 多实例**：`NapCatManager.for_persona()` 创建 `napcat/instances/{name}/` 目录，共享全局二进制，独立配置/日志/QQ 号。
- **WebUI 多人格重构**：Dashboard 人格卡片列表，所有配置页面增加人格选择器，API 改为 `/api/personas/{name}/...`。
- **`python main.py` 子命令式 CLI**：`run`（启动所有人格+WebUI）、`webui`（仅 WebUI）、`persona list/create/start/stop/status/logs/migrate`。
- **NapCat 自动管理集成到 `persona start`**：启动子进程前自动检查/安装/启动/等待 NapCat WS 就绪。
- **Provider 配置全局化**：`data/providers/provider_keys.json` 所有人格共用；`EngineRuntime` 优先从全局位置加载，回退到人格目录兼容旧版。
- **模型编排下拉选择**：WebUI 后端返回 `available_models`（聚合全局 Provider 的 models 字段），前端 `<input>` 改为 `<select>`。

### Removed

- **删除 `sirius_chat/cli.py`**：旧版单人格薄 CLI，由 `main.py` 统一入口替代。
- **删除 `sirius_chat/cli_diagnostics.py`**：未使用。
- **删除 WebUI legacy API**：`/api/status`、`/api/persona`、`/api/orchestration`、`/api/config`、`/api/engine/toggle` 等旧版单人格兼容路由。
- **删除旧版 CLI 测试**：`test_cli_config.py`、`test_cli_diagnostics.py`、`test_cli_runtime.py`。
- **移除 `pyproject.toml` entry point**：`sirius-chat` 命令已移除。

### Changed

- **更新核心文档**：`AGENTS.md`、`README.md`、`docs/architecture.md`、`docs/configuration.md`、`docs/external-usage.md` 全面反映多人格架构。
- **修复 `is_installed()` 方法调用**：`main.py` 中 `global_mgr.is_installed` → `global_mgr.is_installed()`。

## [Unreleased]

### Added

- **被动 SKILL 系统**：SKILL 新增被动模式（不由模型调用，通过后台任务或事件触发自主运行）。新增 `BackgroundTaskSpec`（周期性后台任务）、`TriggerSpec`（事件触发器）、`SkillEngineContext` 协议（引擎交互接口）和 `SkillEngineContextImpl` 适配器。SKILL 文件可导出 `create_background_tasks(ctx)` / `create_triggers(ctx)` 工厂函数注册被动行为。引擎在 `helpers.py` 中统一管理被动 SKILL 的注册、启动和触发分发，事件总线 `emit` 自动转发为 trigger dispatch。内置 `reminder` SKILL 重构为双模式（主动 `run()` + 被动 `create_background_tasks()`），原有 `bg_tasks.py` 中硬编码的提醒检查逻辑已移除至 SKILL 内部。
- **NapCat 实例自管理**：`PersonaWorker` 在连接 WS 失败时自动安装/启动/等待 NapCat 实例就绪，简化 `main.py` 启动逻辑。
- **指向性识别新增 `other_mention_score`**：消息 @ 的不是 AI 时强制压低 `directed_score`，避免误将指向他人的消息判定为指向自己。
- **表情包学习即时展示**：`StickerLearner` 在学习开始前写入 pending 记录，WebUI 可立即展示正在学习的表情包。
- **远程图片缓存**：`send_image` skill 支持将远程 URL 图片下载到本地后发送，避免重复下载。
- **表情包 WebUI 扩展**：表情包页面新增"学习中"统计卡片和 pending 状态标识。
- **表情包标签匹配优化**：`StickerIndexer` 标签匹配逻辑支持多维度标签（mood、action、context），新增推荐标签功能，提升表情包检索准确性。

### Changed

- **user 消息格式改为 XML**：`_build_sender_line` 从纯文本 `[消息]` 前缀改为 `<message speaker=... user_id=... role=...>` XML 标签，与历史对话格式一致，减少模型误判说话者。
- **历史对话裁剪**：`_build_history_xml` 排除尾部未回复的 human 消息，避免与 user 角色内容重复。
- **移除群聊火热对 IMMEDIATE 的降级策略**：`ResponseStrategy.decide()` 不再对 hot/overheated 群聊做 urgency/relevance 分数压制，IMMEDIATE 决策不受热度影响。
- **IMMEDIATE 防抖窗口优化**：窗口从 8s 改为 5s，窗口期内每收到一条新 IMMEDIATE 消息增加 1s，上限 12s，避免刷屏的同时保持回复及时性。
- **指向性语言信号拆分**：强信号（name_match、imperative）与弱信号（second_person、question）分开处理，`topic_relevance` 不再计入指向性。
- **开发者特殊优先级移除**：`ThresholdEngine._relationship_factor` 不再对开发者身份特殊处理，改为由 pipeline 侧的 `relationship_state` 统一管理。
- **测试配置优化**：`conftest.py` 的 `ram_tmp_path` 优先使用 pytest basetemp 目录，避免 Windows Defender 实时扫描导致测试 I/O 超时。
- **消息标注与输出规范增强**：`ResponseAssembler` 移除 `_build_empathy_instruction` 方法和情绪段落中的 valence/arousal 数值输出，简化 system prompt 内容；延迟回复 prompt 新增 `speaker_name` 参数，关系上下文和首次交互个性化消息支持指名。`ResponseStrategyEngine.decide()` 恢复 `heat_level` 参数，hot 群聊 urgency ×0.85/relevance ×0.92，overheated 群聊 urgency ×0.68/relevance ×0.85。
- **WebUI 页面加载器重构**：`core.js`/`config.js` 页面加载器注册逻辑重构，统一加载流程。

### Fixed

- **主动消息开关未生效**：`proactive_enabled` 配置项未被引擎读取，导致关闭开关后仍触发主动消息。新增配置检查。
- **提醒 group_id 注入不完整**：`_inject_group_id_into_latest_reminder` 只为最新一条提醒注入 group_id，链式创建的早期提醒缺失。改为遍历所有提醒。
- **Bridge 主动消息投递未检查开关**：`NapCatBridge._event_bus_listener` 投递 `PROACTIVE_RESPONSE_TRIGGERED` 事件前未检查 `is_proactive_enabled` 状态。
- **测试并行兼容性**：`pyproject.toml` 移除 `pytest-xdist` 并行配置（与部分异步测试不兼容），改用 `asyncio_mode=strict`。
- **测试临时目录优化**：新增 `.pytest_tmp/` 到 `.gitignore`，避免测试临时文件污染仓库。

### Added (Persona System)

- **`PersonaProfile` 数据模型**：结构化角色人格，覆盖身份、性格、表达、情绪、行为、运行时偏好六大维度。
- **`PersonaGenerator`**：三条生成路径——
  - `from_template()`：6 个内置 archetype（warm_friend / sarcastic_techie / gentle_caregiver / chaotic_jester / stoic_observer / protective_elder），零 LLM 成本。
  - `from_keywords()`：关键词规则映射 + 可选 LLM 精炼。
  - `from_interview()`：问卷回答 → LLM 生成完整角色设定。
- **`PersonaStore`**：JSON 持久化到 `{work_path}/engine_state/persona.json`。
- **Roleplay 预设桥接**：`PersonaGenerator.from_roleplay_preset()` 将 legacy `AgentPreset` → `PersonaProfile`；`WorkspaceRuntime.create_emotional_engine()` 自动从 active agent preset 加载 persona。
- **引擎深度集成**：
  - `AssistantEmotionState` 使用 `persona.emotional_baseline` 初始化。
  - `ThresholdEngine` 按 `persona.reply_frequency` 动态调整回复阈值（high×0.8 / low×1.3 / selective×1.6）。
  - `StyleAdapter` 读取 `max_tokens_preference`、`temperature_preference`、`communication_style` 调整生成参数。
  - `ResponseAssembler` 将 persona 身份注入系统提示词，替代硬编码身份。
- **CLI `--persona` 参数**：`python main.py --engine emotional --persona sarcastic_techie`。
- **集成测试**：`tests/test_persona_integration.py`，16 个测试覆盖序列化、模板生成、关键词映射、提示词注入、阈值偏移、风格适配、持久化。
- **文档**：新增 `docs/persona-system.md`。

### Changed (Documentation Overhaul)

- **删除 29 个过时文档**：包括 v0.8~v0.25 的全部迁移文档、v0.27.x 的 12 个补丁迁移文档、旧 roleplay 迁移文档、`skill-sync-policy.md`、`integration-sync-guide.md`、37KB 的开发过程文档 `refactoring-v0.28-emotional-group-chat.md`。
- **重写 `docs/persona-system.md`**：从 AI/真人二元对立转向"用户自定义上位背景"的中性设计哲学；`build_system_prompt()` 从标签罗列改为叙事化角色剧本；`ResponseAssembler` 加入场景定位层。
- **新增 7 个模块系统详解文档**：
  - `docs/engine-emotional.md` — 情感化群聊引擎：四层认知管线、后台任务、事件总线
  - `docs/engine-legacy.md` — Legacy AsyncRolePlayEngine 定位、与 Emotional 引擎对比、迁移路径
  - `docs/memory-system.md` — 三层记忆底座：工作/情景/语义记忆的数据流、激活度衰减、检索机制
  - `docs/emotion-intent-analysis.md` — 情感分析（2D valence-arousal + 19 种情绪映射）与意图分析 v3（目的驱动分类）
  - `docs/skill-system.md` — SKILL 插件机制：文件格式、AI 自主调用、依赖自动安装、数据存储
  - `docs/workspace-runtime.md` — 工作空间运行时：目录结构、配置热刷新、会话管理、引擎生命周期
  - `docs/provider-system.md` — 模型提供者系统：统一接口、自动路由、模型选择、健康检查
- **新增 `docs/README.md`**：文档导航中心，按快速开始、模块详解、配置策略、架构 API、迁移文档分层索引。

## [0.28.0] - 2026-04-17

### Added (v0.28 Emotional Group Chat Engine)

- **全新引擎 `EmotionalGroupChatEngine`**：基于论文《AI记忆系统与情感化助手：群聊场景可落地方案框架》的四层认知架构（感知→认知→决策→执行）与三层记忆底座（工作→情景→语义）。
- **群聊隔离**：`UserMemoryManager.entries` 改为 `{group_id: {user_id: Entry}}` 双层字典；所有记忆操作按 `group_id` 隔离。
- **情感分析 `EmotionAnalyzer`**：2D valence-arousal 模型，19种基本情感映射，规则引擎为主 + LLM fallback；支持情感轨迹、群体情感聚合、情感孤岛检测。
- **意图分析 v3 `IntentAnalyzerV3`**：目的驱动分类（help_seeking / emotional / social / silent）， urgency/relevance 量化评分，规则为主 + LLM fallback。
- **响应策略引擎 `ResponseStrategyEngine`**：IMMEDIATE / DELAYED / SILENT / PROACTIVE 四层策略，替代二元 `should_reply`。
- **动态阈值引擎 `ThresholdEngine`**：多因子动态阈值 = base × activity × relationship × time。
- **对话节奏分析 `RhythmAnalyzer`**：heat_level + pace + topic_stability。
- **共情生成 `ResponseAssembler`**：情感上下文 + 共情策略 + 记忆引用 + 群级风格注入 Prompt。
- **风格适配 `StyleAdapter`**：按 heat/pace/user_style 动态调整 max_tokens、temperature、tone。
- **延迟响应队列 `DelayedResponseQueue`**：等待话题间隙或合并后触发。
- **主动触发器 `ProactiveTrigger`**：时间/记忆/情感三种触发类型，带冷却机制。
- **模型路由 `ModelRouter`**：按任务类型自动选择模型、温度、token 上限；urgency 升级切换更强模型。
- **三层记忆底座**：
  - `WorkingMemoryManager`：按群滑动窗口，关键信息保护，自动晋升到情景记忆。
  - `EpisodicMemoryManager`：结构化事件存储，激活度遗忘曲线，支持复合查询。
  - `SemanticMemoryManager`：用户画像（兴趣图谱、关系状态）+ 群体画像（氛围历史、规范）。
- **语义检索 `MemoryRetriever`**：关键词 + 语义相似度（可选 sentence-transformers）+ 用户画像检索。
- **激活度引擎 `ActivationEngine`**：遗忘曲线 + 访问强化 + 差异化衰减 + 休眠归档。
- **助手情感状态 `AssistantEmotionState`**：自身情感 + 惯性 + 恢复机制。
- **后台任务**：延迟队列 ticker、主动触发 checker、记忆 promoter、语义整合 consolidator。
- **群体规范学习**：被动学习 avg_message_length、emoji_usage_rate、mention_rate、active_hours、topic_switch_frequency、typical_interaction_style。
- **Token 追踪**：`_generate()` 中估算 input/output tokens，随状态持久化。
- **状态持久化 `EngineStateStore`**：持久化 working memory、assistant emotion、token usage 到 `engine_state/`。
- **数据迁移脚本**：`sirius_chat/memory/migration/v0_28_group_isolation.py`，自动检测旧格式并迁移到群隔离布局。
- **CLI 引擎切换**：`main.py --engine {legacy,emotional}`。
- **WorkspaceRuntime 集成**：`create_emotional_engine()` 工厂方法，自动注入 skill runtime。
- **完整文档更新**：`full-architecture-flow.md`、`architecture.md`、`external-usage.md` 全量重写。

### Changed

- 旧引擎 `AsyncRolePlayEngine` 迁移到 `core/_legacy/`，保留兼容导出。
- `UserMemoryFileStore` 存储布局改为 `user_memory/groups/<group_id>/<user_id>.json`。
- `EventMemoryFileStore` 存储布局改为 `event_memory/<group_id>/events.json`。

## [0.27.14] - 2026-04-16

### Fixed
- **self_ai 后处理不再只靠名字出现与否硬回退**：当用户明显在承接当前助手上一轮发言时，即使这条消息没有点名当前助手，也不会仅因为内容里提到其他参与者就被压成 `unknown` 或 `human`。
- **组合名 / 别名的近期上下文匹配更稳**：近期 assistant 发言者现在会按名字变体做匹配，像 `主助手/Sirius`、`月白/Sirius` 这类组合名不再被当成“不是当前助手”，减少 follow-up 追问被误杀。

### Added
- 新增回归测试，覆盖 indirect mention、组合名上下文承接，以及 `reply_mode=auto` 下 follow-up 消息仍保持 `self_ai` 的场景。

## [0.27.13] - 2026-04-16

### Changed
- **SKILL 内部结果新增短期上下文保留窗口**：最近几轮 assistant 回复内，先前 SKILL 的隐藏文本/图片结果会继续注入主模型上下文，避免用户紧接着追问时模型已经“忘掉”刚获取的事实。

### Fixed
- **SKILL 结果不再只在下一次再生成时可见**：此前详细的结构化技能结果只会在最后一次 assistant 之后那一小段上下文里以隐藏消息形式出现，跨过一次正常回复后就只剩较弱的 system note；现在最近追问仍能复用原始内部观察内容。

### Documentation
- 更新架构文档与 Skill 编写指南，说明 SKILL 内部结果的短期持久化语义。

## [0.27.12] - 2026-04-16

### Changed
- **`desktop_screenshot` 更明确面向“当前主机正在做什么”场景**：技能描述与返回的内部文本现在会明确提示模型用截图判断当前桌面上的前台窗口、应用、页面与任务状态，而不是把截图只当成普通图片输入。
- **主提示词会在可用时鼓励先截图再回答**：当 developer 当前轮次可见 `desktop_screenshot` 时，SKILL 系统提示会明确要求模型在判断主机当前状态前优先调用截图，而不是直接猜测。
- **截图 Skill 支持显式观察重点**：`desktop_screenshot` 新增可选 `focus` 参数，并把本次分析重点写入内部文本、元数据和 capture 历史，便于模型围绕“当前在做什么”等目标进行观察。

### Documentation
- 更新 README、外部接入文档与迁移说明，统一说明桌面截图的自动调用语义和 `focus` 参数。

## [0.27.11] - 2026-04-16

### Added
- **developer 元数据安全模型**：`UserProfile` / `Participant` 现在支持通过 `metadata["is_developer"]` 显式声明 developer，并新增 `SkillInvocationContext` 供受限 SKILL 做权限与审计判断。
- **内置 `desktop_screenshot` SKILL**：新增桌面截图内置技能，返回图片工件路径给模型做内部视觉分析，仅 developer 可调用。
- 新增迁移文档 `docs/migration-v0.27.11.md`，说明 developer 权限模型、内置截图技能与内置依赖自动安装语义。

### Changed
- **developer-only SKILL 改为双层权限防护**：非 developer 当前轮次不会在提示词中看到这些工具；即使模型强行调用，`SkillExecutor` 仍会在运行时拒绝。
- **内置 SKILL 也进入统一依赖自动安装流程**：内置 `system_info` 现在显式声明 `psutil`，内置 `desktop_screenshot` 显式声明 `Pillow`，两者都会在加载前参与依赖解析。
- **CLI 首次引导同步持久化 developer 身份**：`main.py` 会显式询问 primary user 是否为 developer，并将该元数据写入 `primary_user.json`。
- **SKILL 注入改为按签名处理**：`data_store` 与 `invocation_context` 只会注入给显式接收这些参数的 `run()` 函数。

### Documentation
- 更新 README、架构文档、外部接入文档、Skill 编写指南与相关仓库级 SKILL，统一说明 developer 元数据约束、`desktop_screenshot`、`system_info` 依赖声明与迁移步骤。

## [0.27.10] - 2026-04-16

### Added
- **内置 `system_info` SKILL**：框架现在会随 skill runtime 一起预加载包内置的本机状态技能，engine 与 `WorkspaceRuntime` 无需额外 workspace 文件即可直接调用。
- 新增回归测试，覆盖弱别称不参与稳定识人绑定、聚焦参与者记忆提示词、内置 SKILL 加载与 workspace 覆盖等场景。
- 新增迁移文档 `docs/migration-v0.27.10.md`，说明记忆防污染、聚焦提示词拼接与内置 SKILL 语义。

### Changed
- **主提示词的参与者记忆改为聚焦拼接**：只向主模型注入当前发言者、当前消息直接相关的参与者，以及尾部压缩后的 `session_summary`，不再整体注入所有参与者和原始近期消息。
- **SKILL 发现顺序固定为内置优先、workspace 覆盖**：skill runtime 会先注册包内置 SKILL，再加载 workspace `skills/` 目录，同名文件按 workspace 版本生效。

### Fixed
- **识人记忆污染收紧**：`memory_extract` 推断出的昵称不再直接写入 `profile.aliases` 或稳定识人索引，而是只存入 `runtime.inferred_aliases` 作为弱线索。
- **记忆提取 prompt 增加身份护栏**：辅助模型现在会显式收到 `strong_identity`、`trusted_labels`、`weak_labels` 与 alias guardrails，降低被第三方称呼、玩笑或上下文冒充污染的概率。

### Documentation
- 更新 README、架构文档、外部接入文档、Skill 编写指南与相关 SKILL，统一说明弱别称语义、聚焦记忆提示词与内置 `system_info` 的覆盖规则。

## [0.27.9] - 2026-04-16

### Added
- **SKILL 结构化内部结果通道**：SKILL 现在可以通过 `text_blocks`、`multimodal_blocks` 和 `internal_metadata` 返回结构化结果；engine 会把可用文本与图片作为隐藏内部上下文注入下一轮生成，而不是把原始结构直接暴露到用户侧。
- 新增回归测试，覆盖名称 AI 证据对象 / possible-AI 对象分流、Skill 结构化结果提取、隐藏图片通道注入，以及高热群聊下的环境插话抑制。

### Changed
- **意图分析提示词改为证据优先**：名称或别称中带有 `AI`、`bot`、`助手`、`Claude` 等明显线索的对象会被明确标为 AI 证据对象；没有这类线索的对象则作为“需结合上下文判断”的 possible-AI 候选交给模型判别，不再在提示词里先行硬标记为人类。
- **群聊热度对 ambient reply 更克制**：`hot` / `overheated` 场景下的环境插话惩罚略微增强，并叠加高热度连续惩罚，减少多人高频聊天中的抢答。

### Fixed
- **减少误把其他对象当成当前模型自身**：post-process 现在只会在命中其他 AI 或明确 AI 线索对象时，把误判的 `self_ai` 回退为 `other_ai`，避免仅凭普通对象名字就强行改写目标。
- **Skill 元信息不再混入最终回答语义**：系统提示词和内部技能通道现在都会显式要求模型只提炼结果结论，不复述 `internal_metadata`、`mime_type`、`label`、路径、URL 或 JSON 字段名。

### Documentation
- 更新 README、架构文档、Skill 编写指南与相关 SKILL，统一说明意图分析的 AI 证据 / possible-AI 语义，以及 SKILL 的内部文本/多模态结果通道。

## [0.27.8] - 2026-04-16

### Changed
- **SKILL 完成事件收紧为状态元数据**：`SessionEventType.SKILL_COMPLETED` 不再携带 `result_preview`，只保留 `skill_name` 与 `success`，避免把内部技能执行结果重复暴露给外部订阅方。

### Fixed
- **SKILL 执行结果不再通过事件流外泄**：engine 在技能执行完成后，外部订阅者不会再从 `SKILL_COMPLETED` 事件里读到内部技能结果摘要。
- **`on_reply` 路径保持只转发面向用户的 assistant 内容**：技能结果仍会作为内部 system 上下文参与下一轮生成，但不会以内部结果文本的形式透出到外部回调语义中。

### Added
- 新增回归测试，覆盖正常 SKILL / 未知 SKILL 两条 `SKILL_COMPLETED` 路径不再暴露结果预览，以及 `on_reply` 不接收内部 `SKILL执行结果` 文本的场景。

### Documentation
- 更新 README、架构文档、外部接入文档、事件流迁移说明与相关 SKILL，统一说明 `SKILL_COMPLETED` 现在只表达执行状态，外部投递应消费 assistant 消息。

## [0.27.7] - 2026-04-16

### Changed
- **意图分析新增近期发言人别称与环境线索**：`intent_analysis` 现在会把近期人类发言者的 aliases、一部分近期 AI 发言者身份线索，以及 `environment_context` 中的群名/环境摘要一并送入模型，减少只靠原始 speaker 名称带来的歧义。

### Fixed
- **群控/停用类命令不再误触发当前模型回复**：像“关闭本群ai”“禁用机器人”“别让 bot 说话”这类操作性语句，如果没有明确点名当前模型自身，即使意图模型误判成 `self_ai`，也会被硬抑制，不触发自动回复。
- **近期发言人别称现在会参与人类目标识别**：当用户近期发言者在记忆里已有 alias，意图分析不再只看 display name，而会把名字和 aliases 一起作为人类线索。

### Added
- 新增回归测试，覆盖近期发言人 aliases 与 `environment_context` 进入 intent prompt，以及群控命令在 LLM 误判时仍不触发主回复的场景。

### Documentation
- 更新 README、架构文档、外部接入文档与相关 SKILL，说明意图分析新增近期发言人 aliases / environment_context 线索，以及群控命令的自动回复抑制规则。

## [0.27.6] - 2026-04-16

### Changed
- **意图分析上下文升级为最近交互链摘要**：`intent_analysis` 发送给模型的上下文现在保留最近 4 个去重 turn 的交互链，并补充最近 AI / 人类发言者列表，不再近似依赖单条最近消息。

### Fixed
- **插话后代词指向不再轻易丢失**：当 AI 刚说完话后有人插一句，当前消息再用“你/您”追问时，回退路径会结合最近几轮交互链做加权判断，不再因为单条插话直接退化成 `unknown`。
- **多 AI 场景下插话不会掩盖最近相关 AI**：即使人类参与者在中间补充一句，意图分析仍能更稳定地保留“当前消息更可能是在接前一个 AI 话头”的信号。

### Added
- 新增回归测试，覆盖“AI 发言后有人插话”时的 current AI / other AI 代词跟随，以及 intent prompt 中最近交互链摘要的保留行为。

### Documentation
- 更新 README、架构文档、外部接入文档与相关 SKILL，说明 intent prompt 现使用最近交互链摘要与最近发言者列表，而非近似依赖单条最近消息。

## [0.27.5] - 2026-04-16

### Changed
- **意图分析上下文收紧为结构化摘要**：`intent_analysis` 发送给模型的上下文不再附带大段近期对话，而是只保留极小的最近消息摘要，并显式标注当前模型、其他 AI 与人类参与者的名字线索。

### Fixed
- **对其他 AI 的调用不再被长上下文带偏成当前模型**：在多 AI 群聊里，意图分析现在会优先参考当前消息内的名字命中，再结合压缩后的近期摘要，减少把“对别的 AI 说话”误识别为“在叫自己”的情况。
- **拆分名/简称的人类称呼识别更稳**：像“砂狼 白子”这类带空格或分段的人名，当前消息只写“白子”时也能更稳定地识别为人类参与者，而不是把判断压力全部留给模型。

### Added
- 新增回归测试，覆盖 intent prompt 上下文压缩，以及拆分名命中时的人类目标识别。

### Documentation
- 更新 README、架构文档、外部接入文档与相关 SKILL，说明多 AI 群聊下 intent prompt 已改为结构化摘要，并会优先暴露当前消息的名字命中线索。

## [0.27.4] - 2026-04-16

### Changed
- **意图分析对当前模型自身更敏感**：`intent_analysis` 现在会把 `target=ai` 细分为“当前模型自身”与“其他 AI”，不再把群聊中的所有 AI 都视为同一个目标。
- **代词推断会参考近期 AI 发言者**：当消息只出现“你/您”时，分析器会结合近期上下文判断是在回复当前模型、其他 AI 还是人类参与者。

### Fixed
- **自动回复不再抢答其他 AI**：`reply_mode=auto` 下，当消息明显是在调用或追问其他 AI 时，当前模型会抑制回复，不再把“对其他 AI 说话”误判成“在叫自己”。
- **回复频率豁免仅对当前模型自身生效**：频率限制中的 mention 豁免现在只在消息明确指向当前模型自身时触发。

### Added
- 新增多 AI 回归测试，覆盖 `target_scope=other_ai`、代词跟随近期 AI 上下文，以及 `reply_mode=auto` 对其他 AI 调用不回复的场景。

### Documentation
- 更新架构文档、编排策略文档、外部接入文档、README 与相关 SKILL，统一说明多 AI 群聊下的意图分析与自动回复抑制规则。

## [0.27.3] - 2026-04-15

### Changed
- **长上下文会直接触发记忆归纳**：会话 finalize 时若当前上下文已明显接近 `history_max_chars`，engine 会立即补跑一轮 `memory_manager` 归纳，不再只能等后台定时器。
- **AI 自身记忆新增长上下文触发**：除 `self_memory_extract_batch_size` 和 `self_memory_min_chars` 外，长对话也会主动触发 diary/glossary 提取，避免低频回复场景几乎不沉淀。
- **self-memory 默认复用 memory_manager 模型**：未单独配置 `task_models["self_memory_extract"]` 时，自身记忆会复用 `memory_manager` 的模型路由，便于两者共用同一辅助模型。

### Fixed
- **direct run_live_message 保持活跃会话时会正确启动后台归纳管理器**：不再只有先显式调用 `run_live_session()` 的路径才能让后台循环运行。

### Added
- 新增回归测试，覆盖长上下文 finalize 触发归纳、self-memory 长上下文触发以及 `run_live_message(..., finalize_and_persist=False)` 下的后台任务启动。

### Documentation
- 更新 README、架构文档、策略文档、外部接入文档与相关 SKILL，统一说明长上下文触发与 self-memory / memory_manager 的模型复用关系。

## [0.27.2] - 2026-04-15

### Added
- **最小回复间隔配置**：新增 `OrchestrationPolicy.min_reply_interval_seconds`，用于控制两次 AI 实际回复之间的最小时间间隔。
- 新增 runtime 回归测试，覆盖“冷却窗口内继续收消息、窗口结束后先合并再判断回复”的场景。
- 新增迁移说明 `docs/migration-v0.27.2.md`，说明新参数与现有 `reply_mode=auto` / `pending_message_threshold` 的关系。

### Changed
- **WorkspaceRuntime 队列新增冷却期蓄积逻辑**：当 AI 刚完成过一次回复且 `min_reply_interval_seconds > 0` 时，runtime 不会立刻处理下一条消息，而是继续保留会话队列，直到冷却结束后再进入下一次回复判断。
- **冷却结束后统一走合并入口**：等待期间同一说话人的连续消息会先按静默批处理规则合并，再作为一次 turn 送入 engine，从而让模型看到更完整的上下文。

### Fixed
- **最小回复间隔不再与自动回复语义冲突**：冷却只会推迟“下一次是否回复”的判断时机，不会绕过 `reply_mode=auto`、意图分析或 engagement 决策；因此冷却结束后仍可能选择不回复。

### Documentation
- 更新 README、配置说明、架构文档、外部接入文档与相关 SKILL，统一说明 `min_reply_interval_seconds` 的语义、与 `pending_message_threshold` 的区别，以及等待期内的消息合并行为。

## [0.27.1] - 2026-04-15

### Changed
- **移除 task_budgets 配置与预算跳过逻辑**：辅助任务不再根据预算近似值取消模型调用，`task_budgets` 从公开配置、示例、模板与持久化镜像中移除。
- **split marker 与 skill call marker 改为内置常量**：提示词分割统一使用内置 `<MSG_SPLIT>`，SKILL 调用统一使用内置 `[SKILL_CALL:`，不再对外暴露配置入口。
- **memory_manager 统一并入任务模型体系**：原 `memory_manager_model` / `memory_manager_temperature` / `memory_manager_max_tokens` 改为通过 `task_models/task_temperatures/task_max_tokens/task_retries` 配置 `memory_manager` 任务；旧字段仅在加载时做兼容映射。
- **后台记忆归纳改为静默常驻**：live session 启动后总会初始化后台归纳循环，不再暴露 `consolidation_enabled` 开关；若需停用相关 LLM 调用，改为关闭 `task_enabled["memory_manager"]`。
- **SKILL 生命周期前移到 runtime**：`WorkspaceRuntime` 现在在框架启动时预加载 `skills/`，并在 skill 文件变化时全量重载；消息路径不再按次扫描或热重载 SKILL。

### Added
- 新增 runtime 回归测试，覆盖 skill 目录监听与启动即初始化共享 skill runtime 的场景。
- 新增 skill registry 回归测试，覆盖全量 reload 会移除已删除 SKILL 的场景。
- 新增迁移说明 `docs/migration-v0.27.1.md`，说明配置面收缩与兼容迁移策略。

### Fixed
- **提示词分割约束更严格**：系统提示现在明确禁止用连续空行伪造多条消息，assistant 内容清洗也会压缩过多空行，减少错误分段。
- **background consolidation 使用 memory_manager 任务路由**：后台归纳、会话收尾整理与 live path 现在共享同一 `memory_manager` 任务模型配置。

### Documentation
- 更新 README、架构文档、外部接入文档、迁移文档与相关 SKILL，统一说明内置 marker、`memory_manager` 任务化、后台归纳默认常驻，以及 SKILL 启动时加载/变更时重载的新行为。

## [0.27.0] - 2026-04-15

### Changed
- **消息合并策略改为积压计数批处理**：`WorkspaceRuntime` 现在按 `pending_message_threshold` 决定是否进入静默批处理；当单会话待处理消息数超过阈值时，会合并同一说话人的连续消息并只触发一次主模型调用，替代旧的时间型 `message_debounce_seconds` 策略。
- **intent_analysis 改为严格模型路径**：当 `task_enabled["intent_analysis"]` 为 `true` 时，意图结论必须来自模型；预算不足、provider 调用失败或解析失败时，不再回退到关键词意图推断。
- **人格生成默认更克制**：角色生成器现在会默认产出更偏短句、纯文本、少 markdown 的角色行为约束，减少长段落和说明书式回复。

### Added
- 新增 runtime 回归测试，覆盖“单会话积压超过阈值后对同一说话人的连续消息执行静默批处理”场景。
- 新增 roleplay 回归测试，覆盖“人格生成提示词默认偏向短回复和纯文本表达”场景。
- 新增外部迁移文档 `docs/migration-v0.27.md`，说明配置键变更与行为迁移步骤。

### Fixed
- **意图分析失败时不再产生伪造结论**：启用 `intent_analysis` 的自动回复路径下，预算超限、provider 失败或响应解析失败时，本轮不会再出现“没有模型调用却仍有意图分析结果”的现象。

### Documentation
- 更新 README、架构文档、流程图、外部接入文档与相关 SKILL，统一说明 `pending_message_threshold`、runtime 队列批处理，以及新的 `intent_analysis` 语义。

## [0.26.10] - 2026-04-15

### Fixed
- **event_extract 收尾阶段不再错误回退到主模型**：`AsyncRolePlayEngine` 在会话结束时对刷新的事件缓冲执行 `finalize_pending_events()` 时，现改为沿用 `event_extract` 的任务模型解析逻辑；当配置了 `task_models["event_extract"]` 时，即使消息数未达到批量阈值，最后一次事件提取也不会再错误落到主模型。
- **event_extract 收尾阶段恢复预算门禁**：会话结束时的事件缓冲刷新现在会复用 `event_extract` 的预算判断；当任务预算已超限时，不会再额外触发一次事件提取调用。

### Added
- 新增 event_extract 回归测试，覆盖“未满批次时收尾提取仍使用 event-model”场景，并保留预算超限场景不触发 event-model 的行为断言。

## [0.26.9] - 2026-04-15

### Fixed
- **推理模型默认关闭 thinking 模式**：provider 出站请求现在会按上游协议显式附加关闭推理参数，避免 DeepSeek、GLM、火山方舟、百炼和 SiliconFlow 的支持模型默认进入深度思考模式，带来额外时延与 token 开销。

### Added
- 新增 provider 回归测试，覆盖不同 provider 的请求体会携带正确的 thinking 关闭字段，且不支持该能力的 OpenAI-compatible / YTea provider 不会误发未知参数。

## [0.26.8] - 2026-04-15

### Fixed
- **同一 WorkspaceBootstrap 重启后不再反复覆盖手工配置**：`WorkspaceRuntime` 现在会把 bootstrap payload 的签名持久化到 `workspace.json`，同一份 bootstrap 在后续重启时不会再次把 `active_agent_key`、`session_defaults`、`orchestration_defaults` 与 provider 注册表覆盖回初始化值，避免外部宿主场景下手工调整配置后再次启动又被重置。

### Added
- 新增 runtime 回归测试，覆盖“首次应用 bootstrap 后，手工修改 workspace 配置和 provider 注册表，再次重启仍保持生效”的场景。

### Documentation
- 更新 README、架构文档、外部接入文档和相关 SKILL，明确 `WorkspaceBootstrap` 是按签名一次性注入默认值，而不是每次启动强制覆盖现有 workspace。

## [0.26.7] - 2026-04-15

### Fixed
- **Aliyun Bailian / OpenAI-compatible 多模态本地图片修复**：当 `image_url` 实际上传入的是本地文件路径或 `file://` URI 时，provider 现在会在发送前自动转换为 Data URL，避免 OpenAI 兼容 HTTP 接口把本地路径误当成可下载公网地址，触发 `Failed to download multimodal content`。
- **多模态下载失败错误提示增强**：当百炼上游返回图片下载失败的 400 错误时，运行时异常现在会明确提示检查公网 URL 可访问性，以及 `Content-Type` / `Content-Length` 响应头要求，便于快速判断是本地文件路径问题还是远端资源头信息问题。

### Added
- 新增 provider 回归测试，覆盖本地图片路径自动转换为 Data URL 和多模态下载失败提示增强两类场景。

## [0.26.6] - 2026-04-15

### Added
- **provider DEBUG 日志增强**：各个上游 provider 在 DEBUG 级别下现在会输出结构化请求详情，包含实际请求 URL、base_url、超时、请求体大小、多模态统计与完整 payload，便于直接定位“模型实际打到了哪个地址”。
- **AutoRoutingProvider 路由 DEBUG 日志**：自动路由在发起调用前会记录命中的 provider_type、匹配来源（models 或 healthcheck_model）、base_url 与候选模型列表，便于排查模型为何被分配到某个 provider。
- 新增 provider / routing 回归测试，覆盖“DEBUG 日志包含真实请求 URL”和“自动路由日志包含命中的 provider 元信息”两类场景。

### Fixed
- **intent_analysis 配置入口彻底统一到 task 配置**：`OrchestrationPolicy` 默认任务开关现在直接包含 `intent_analysis`，引擎日志也会按真实任务解析结果输出模型分配，避免旧兼容字段与 `task_*` 配置并存时产生歧义。
- **旧 intent 字段改为只读兼容、不再写回模板与持久化文件**：`ConfigManager`、默认 JSONC 模板与 `main.py` 持久化镜像现在会把 `enable_intent_analysis` / `intent_analysis_model` 自动映射到 `task_enabled` / `task_models`，但新的 `workspace.json`、`config/session_config.json` 与默认配置不再继续写出旧字段。
- 新增配置回归测试，覆盖“旧字段自动映射到任务配置”和“保存 workspace 后旧字段被规范化移除”两类场景。

## [0.26.5] - 2026-04-15

### Fixed
- **workspace.json / session_config.json 不再被 null 污染**：`ConfigManager` 现在会在加载和保存时统一忽略空值，并用现有配置或默认值回填；外部宿主即使传入包含 `None` 的 payload，也不会再把 `workspace.json` 和 `config/session_config.json` 写成大面积 `null`。
- **已有 null 配置可自动恢复**：当历史 `workspace.json` 或 `config/session_config.json` 中已经混入 `null` 时，加载逻辑不再因 `int(None)` 等转换报错，而是自动回退到可用默认值或已有有效字段。
- **runtime 局部设置更新忽略 null 字段**：`WorkspaceRuntime.apply_workspace_updates()` 现在把 `null` 视为“未修改”，避免外部设置面板把空字段错误写回成字符串 `"None"` 或空值。

### Added
- 新增 config/runtime 回归测试，覆盖“已有配置被带 `None` 的对象重新保存”与“磁盘上已有 null 字段仍可正常加载”两类场景。

## [0.26.4] - 2026-04-15

### Fixed
- **session_config.json 的任务模型设置不再被较新的 workspace.json 覆盖**：`ConfigManager.load_workspace_config()` 现在始终以 `config/session_config.json` 中的 `session_defaults` 和 `orchestration` 作为运行时来源，避免外部程序场景下仅因 manifest 更新时间更晚，就把 `task_models`、`task_enabled` 等设置回退到旧值。

### Added
- 新增 config/runtime 回归测试，覆盖“manifest 更晚但 session snapshot 中的 `task_models` 仍应生效”场景，直接验证 `event_extract` 不会错误回退到主模型。

## [0.26.3] - 2026-04-15

### Fixed
- **外部 runtime 启动不再覆写已有任务模型**：`WorkspaceRuntime` 对 `WorkspaceBootstrap.orchestration_defaults` 和设置补丁改为递归合并，外部宿主只传局部字段时，不会再把已有 `task_models`、`task_enabled` 等配置整块抹掉。
- **provider_keys.json 中的 `models` 不再被重启清空**：workspace provider registry 现在按已有条目合并更新；当外部宿主传入的 `provider_entries` 省略 `models`、`healthcheck_model` 或其他可选字段时，会保留已有值，而不是把整条 provider 配置重写为空列表。
- **主入口兼容镜像更完整**：`main.py` 现在会优先按当前 workspace 配置重建 `SessionConfig`，`session_config.persisted.json` 只作为兼容镜像写回，并完整保留 orchestration 配置。

### Added
- 新增 runtime/provider 回归测试，覆盖 partial bootstrap 不再抹掉 task_models，以及 `provider_entries` 省略 `models` 时 registry 仍保留已有模型列表。
- 新增主入口回归测试，覆盖 persisted bundle 不再覆盖 workspace 设置，以及 orchestration 配置完整写回两类场景。

## [0.26.2] - 2026-04-15

### Changed
- **配置模板可发现性增强**：`--init-config` 生成的默认配置现在会展开完整的 `orchestration` 配置骨架，并为嵌套字段补充注释，包含 `intent_analysis` 相关设置。
- **session_config.json 注释增强**：`config/session_config.json` 的 JSONC 渲染改为支持嵌套字段注释，provider 与 orchestration 子字段现在也会带说明。
- **workspace 快照更完整**：持久化 workspace 配置时，`config/session_config.json` 会写出完整的 orchestration 默认项并与用户设置合并，避免“能设置但文件里看不到”的情况。

### Fixed
- 移除默认配置模板里无效的 `orchestration.enabled` 旧字段，改为当前框架真实支持的配置项。

## [0.26.1] - 2026-04-15

### Fixed
- **手动修改配置重启后回退**：修复 `workspace.json` 与 `config/session_config.json` 同时存在时的覆盖优先级问题。现在会以较新的文件作为事实来源，保证手动编辑在重启后不会被旧快照覆盖。
- **provider_keys.json 热刷新不生效**：修复 `WorkspaceRuntime` 在持有旧 `AutoRoutingProvider` 时，即使检测到 `providers/provider_keys.json` 变更、重建 engine 后仍继续复用旧路由配置的问题。现在 registry 驱动模式会在重建时重新从磁盘加载 provider 配置。

### Added
- 新增回归测试，覆盖“手动编辑 workspace 配置后重启保持生效”和“手动编辑 provider models 后 watcher 刷新真正切换到新模型路由”两类场景。

## [0.26.0] - 2026-04-15

### Added
- **BigModelProvider**：新增智谱 BigModel 专用 provider，默认请求 `https://open.bigmodel.cn/api/paas/v4/chat/completions`，适用于 `glm-4.6v` 等 GLM 模型，并兼容 OpenAI 风格多模态 `content` 列表。

### Changed
- **Provider 路由与平台清单**：新增 `bigmodel` 平台，支持 `zhipu` / `zhipuai` 别名归一化，并可通过 `AutoRoutingProvider`、`ProviderRegistry`、`register_provider_with_validation()` 等统一接入。
- **公开 API 导出**：`sirius_chat`、`sirius_chat.api` 与 `sirius_chat.api.providers` 新增导出 `BigModelProvider`。

### Documentation
- 更新 README、架构文档、外部接入文档和 SKILL，补充 BigModel GLM-4.6V 的接入方式。

## [0.25.0] - 2026-04-14

### Added
- **WorkspaceBootstrap**：新增 `WorkspaceBootstrap` 数据类，可通过 `open_workspace_runtime(bootstrap=...)` 在首次打开 workspace 时注入 active_agent_key、session_defaults、orchestration_defaults、provider_entries 和 provider_policy。支持 `persist_bootstrap=False` 仅在本次运行生效。
- **Workspace 读写 API**：`WorkspaceRuntime` 新增 `export_workspace_defaults()` / `apply_workspace_updates(patch)` 方法，外部无需理解文件布局即可管理 workspace 配置。
- **set_provider_entries()**：`WorkspaceRuntime` 新增 `set_provider_entries()` 方法，运行时注入 provider 配置并可选持久化。
- **RoleplayWorkspaceManager**：新增 `RoleplayWorkspaceManager` 类，封装 agent 选择 + workspace defaults 写入的一站式流程。
- **Legacy generated_agents.json 回退读取**：`load_generated_agent_library()` 在新路径 `roleplay/generated_agents.json` 找不到时，自动回退到根目录旧路径。
- **SqliteSessionStore legacy JSON 导入**：从 `session_state.json` 导入后自动重命名为 `.json.migrated`，防止 clear 后重新导入。
- 新增迁移文档 `docs/migration-v0.25.md`。

### Removed
- **WorkspaceMigrationManager 已移除**：`sirius_chat.workspace.migration` 模块及其导入均已删除。`WorkspaceRuntime.initialize()` 不再自动迁移根目录平铺布局。
- **EventMemoryManager v1 格式迁移已移除**：`from_dict()` 遇到 version < 2 数据时返回空 manager。

### Changed
- `WorkspaceRuntime.open()` 新增 `bootstrap` 和 `persist_bootstrap` 参数。
- `open_workspace_runtime()` API 新增对应参数。
- 公开 API 新增导出：`WorkspaceBootstrap`、`RoleplayWorkspaceManager`。

## [0.24.0] - 2026-04-14

### Added
- **双根 workspace 持久化**：`WorkspaceLayout`、`WorkspaceRuntime`、`SessionConfig` 与 CLI/API 现在支持分离 `config_path` 与 `work_path`，允许把配置资产和运行态数据写入不同目录。

### Changed
- **配置热刷新**：`WorkspaceRuntime` 现在通过文件监听即时跟踪 workspace/config/provider/roleplay 配置变更，并在不丢失既有 transcript 的前提下重建 engine 上下文；每次消息处理前仍保留签名校验作为兜底。
- **配置快照与 provider 归位**：`workspace.json`、`config/session_config.json`、`providers/provider_keys.json`、`roleplay/`、`skills/` 统一归到 config root；`sessions/`、`memory/`、`token/`、`skill_data/` 与 `primary_user.json` 统一归到 data root。
- **配置模板可注释化**：`--init-config` 与 workspace 生成的 `config/session_config.json` 改为写出 JSONC 风格注释模板，便于外部直接编辑并与热刷新联动。

### Fixed
- **外部接入路径歧义**：修复 `main.py`、`sirius-chat` CLI、`JsonPersistentSessionRunner` 和 roleplay 持久化在双路径模式下仍把部分配置错误写回 data root 的问题。

## [0.23.0] - 2026-04-14

### Added
- **WorkspaceRuntime / WorkspaceLayout / WorkspaceMigrationManager**：新增 workspace 级运行时、统一路径解析与旧布局迁移能力。对外推荐入口改为 `open_workspace_runtime(...)` / `WorkspaceRuntime.open(...)`，外部只需提供 `work_path`、`session_id` 与业务输入。
- **Workspace 配置模型**：新增 `WorkspaceConfig`、`SessionDefaults`、`ProviderPolicy` 与 `SessionStoreFactory`，由 workspace 层统一派生运行时 `SessionConfig`。
- **迁移档案与回归覆盖**：新增 `docs/migration-v0.23.md` 和 `tests/test_workspace_runtime.py`，覆盖自动恢复、多 session、删除会话、旧布局迁移与 legacy session JSON bootstrap。

### Changed
- **持久化布局统一收口**：provider、session、memory、token、roleplay、skills 全部改走 workspace 布局。默认路径现在是：`providers/provider_keys.json`、`sessions/<session_id>/session_state.db`、`sessions/<session_id>/participants.json`、`memory/events/events.json`、`memory/self_memory.json`、`token/token_usage.db`、`roleplay/generated_agents.json` 与 `roleplay/generated_agent_traces/`。
- **兼容入口复用 runtime**：`JsonPersistentSessionRunner`、`sirius-chat` CLI 和 `main.py` 现在尽量复用 `WorkspaceRuntime`，不再要求调用方显式 `store.load()` / `store.save()`。
- **Roleplay 与 provider 管理收敛到 workspace**：active agent 会同步写回 `WorkspaceConfig`，provider registry 统一托管在 `WorkspaceProviderManager` 下。

### Fixed
- **Windows SQLite 删除锁**：`SqliteSessionStore` 现在显式关闭连接，修复删除 session 目录时的 `WinError 32`。
- **兼容入口路径回归**：修复 `main.py` 在新布局下使用 `/provider add` 时错误地把 workspace 根路径解析为 `providers/` 子目录的问题。
- **包初始化循环依赖**：`sirius_chat.workspace` 与 `sirius_chat.session` 改为 lazy exports，避免 runtime 引入后的导入环路。

## [0.22.4] - 2026-04-14

### Added
- **阿里云百炼 Provider 支持**：新增 `AliyunBailianProvider`，默认接入 DashScope OpenAI 兼容端点 `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`，可直接调用 `qwen-plus` 等百炼模型。
- **百炼配置示例**：新增 `examples/session.aliyun_bailian.json`，可作为 CLI 与工程接入百炼的最小配置模板。

### Changed
- **Provider 路由与注册表扩展**：统一支持 `aliyun-bailian` 平台，并接受 `bailian` / `dashscope` 别名；session JSON、`provider_keys.json` 和自动路由都会规范化到同一个平台标识。
- **文档与外部接入说明更新**：README、架构文档、外部接入说明和相关 SKILL 已补充百炼默认地址、地域覆盖方式与 provider 选型说明。

## [0.22.3] - 2026-04-14

### Changed
- **结构化 SQLite 会话存储**：`SqliteSessionStore` 不再把整份 `Transcript` 写成单条 payload；现在按消息、reply runtime、用户 profile/runtime/facts 与 token 使用记录分表持久化，保留 SQLite 的事务语义，同时让存储结构与会话模型对齐。
- **新增显式迁移示例**：仓库增加 `examples/migrate_session_store.py`，用于在需要人工核验时显式触发 `session_state.json` / 旧 payload SQLite 到结构化 `session_state.db` 的迁移。

### Fixed
- **旧会话自动迁移**：首次打开默认 `session_state.db` 时会自动导入同目录 legacy `session_state.json`，并原地升级旧 `session_state(payload)` SQLite；`clear()` 后也不会因残留旧 JSON 而反复恢复已清空的会话。

## [0.22.2] - 2026-04-14

### Fixed
- **人格生成截断响应处理**：当模型返回被 ```json 包裹但未完整闭合的 JSON-like 响应时，框架不再把原始文本直接写入 `agent.persona` 和 `global_system_prompt`；现在会显式报错、保留失败原始响应到 trace，并保留最近一次暂存的 `PersonaSpec` 供恢复。

### Changed
- **人格生成默认预算再提高**：`agenerate_from_persona_spec(...)`、`agenerate_agent_prompts_from_answers(...)`、`abuild_roleplay_prompt_from_answers_and_apply(...)`、`aupdate_agent_prompt(...)`、`aregenerate_agent_prompt_from_dependencies(...)` 的默认 `max_tokens` 从 `1400` 提高到 `5120`，进一步降低长人格 JSON 被截断的概率。
- **人格生成支持请求级超时**：`GenerationRequest` 新增 `timeout_seconds`，各同步 provider 现在会优先使用请求级 timeout；上述人格生成入口也新增 `timeout_seconds` 参数，并默认使用 `120.0` 秒，避免长结构化输出在 provider 默认 30 秒超时前被中断。

## [0.22.1] - 2026-04-14

### Changed
- **人格生成输入预持久化**：`abuild_roleplay_prompt_from_answers_and_apply(...)`、`aupdate_agent_prompt(...)`、`aregenerate_agent_prompt_from_dependencies(...)` 现在会先把最新 `PersonaSpec` 与待生成快照落盘，再调用模型，避免在生成失败时丢失已经收集的高层人格输入。
- **轨迹文件增强**：`generated_agent_traces/<agent_key>.json` 在正式生成前会先记录待生成快照；生成成功后自动清理 pending 状态，失败时保留最近一次失败信息与依赖文件快照，便于恢复与排查。

### Fixed
- **失败恢复能力**：`load_persona_spec(...)` 现在能优先返回最近一次暂存的人格输入，保证 build / update / regenerate 失败后仍可恢复问卷回答、背景设定和 `dependency_files`。
- **迁移说明补强**：更新 `docs/migration-roleplay-v0.20.md`、`docs/architecture.md`、`docs/external-usage.md`、`docs/full-architecture-flow.md` 与相关 SKILL，明确外部接入方应理解新的“先落盘、后生成”工作流。

## [0.22.0] - 2026-04-13

### Added
- **人格问卷模板 API**：新增 `list_roleplay_question_templates()`，并让 `generate_humanized_roleplay_questions(template=...)` 支持 `default`、`companion`、`romance`、`group_chat` 四类场景模板，方便外部系统按陪伴型、恋爱向、群聊型等场景直接切换问卷。
- **CLI 模板辅助命令**：`sirius-chat` 新增 `--list-roleplay-question-templates` 与 `--print-roleplay-questions-template <template>`，无需先加载会话配置即可直接导出模板枚举和问题清单 JSON。
- **模板骨架示例脚本**：新增 `examples/roleplay_template_selection.py`，可按模板导出 `PersonaSpec` 问卷骨架，便于外部表单、配置后台或 Agent 平台直接接入。

### Changed
- **问卷升级为上位人格 brief 优先**：默认问卷从“直接写风格/台词”调整为优先收集人物原型、核心矛盾、关系策略、情绪原则、表达节奏、边界与小缺点，再交给 LLM 具体化。
- **人格生成 prompt 继续强化**：生成器现在会显式要求模型把抽象人格输入展开为具体的人物小传、语言习惯、回复节奏和互动边界，并新增对人物小传、反差感、口语节奏、边界分寸等维度的自动强化检测。
- **外部接入文档更新**：同步更新 `README.md`、`docs/architecture.md`、`docs/external-usage.md`、`docs/full-architecture-flow.md`、`docs/migration-roleplay-v0.20.md`、`docs/api.md`、`docs/api.json` 以及相关 SKILL，统一反映模板化问卷与高层人格输入流程。

## [0.21.0] - 2026-04-13

### Added
- **人格生成依赖文件输入**：`PersonaSpec`、`agenerate_agent_prompts_from_answers(...)`、`abuild_roleplay_prompt_from_answers_and_apply(...)`、`aupdate_agent_prompt(...)` 现支持 `dependency_files`，可把角色卡、设定稿、语气样本等本地文件作为人格生成素材。
- **依赖文件重生 API**：新增 `aregenerate_agent_prompt_from_dependencies(...)`，允许在素材文件更新后直接基于最新文件内容重生既有 `agent_key` 的人格。
- **完整本地生成轨迹**：新增 `generated_agent_traces/<agent_key>.json` 持久化产物，并提供 `load_persona_generation_traces(...)` 对外读取入口；轨迹中包含 prompt、原始模型返回、解析结果、依赖文件快照与最终输出 preset。
- **外部迁移文档**：新增 `docs/migration-roleplay-v0.20.md`，面向外部调用方说明如何从旧的人格生成流迁移到依赖文件 + 轨迹 + 重生模式。

### Changed
- **人格生成 prompt 强化**：当输入中包含“拟人”“情感”“陪伴”“关系”“共情”等信号时，生成器会自动加强 prompt，显式要求模型提升真实人感、情绪细节和关系连续性，避免机械助手腔。
- **问题清单增强**：`generate_humanized_roleplay_questions()` 新增一条聚焦“拟人感 / 情感温度 / 陪伴方式”的问题，用于更直接采集情绪表达与关系边界。
- **对外文档与 SKILL 同步**：更新 `README.md`、`docs/architecture.md`、`docs/external-usage.md`、`docs/full-architecture-flow.md`、`docs/api.md`、`docs/api.json` 以及相关 SKILL，统一反映新的角色生成工作流。

## [0.20.0] - 2026-04-13

### Changed (Internal)
- **`AsyncRolePlayEngine` 神类拆分（TD-09）**：将 2576 行的 `core/engine.py` 中内聚的方法组提取为独立模块，engine 方法保留为 thin wrapper：
  - `sirius_chat/core/memory_runner.py`：5 个记忆/事件任务函数（`run_memory_extract_task`、`run_self_memory_extract_task`、`run_batch_event_extract`、`run_memory_manager_task`、`build_memory_extract_task_input`）
  - `sirius_chat/core/engagement_pipeline.py`：3 个参与度/回复决策函数（`build_heat_analysis`、`run_engagement_intent_analysis`、`should_reply_for_turn`）
  - `sirius_chat/core/chat_builder.py`：6 个聊天上下文构建函数（`has_multimodal_inputs`、`get_model_for_chat`、`is_internal_memory_metadata_line`、`sanitize_assistant_content`、`collect_internal_system_notes`、`build_chat_main_request_context`）+ 3 个正则常量
  - `engine.py` 行数：2576 → 1932（减少 644 行，-25%）

- **`LiveSessionContext` 重构（TD-11）**：将 16 字段的平坦 dataclass 按抽象层次拆分为 3 个子对象：
  - `SessionStores`：存储层（`file_store`、`event_file_store`、`token_store`、`self_memory_store`）
  - `SessionSubsystems`：子系统层（`event_store`、`event_bus`、`bg_task_manager`、`skill_registry`、`skill_executor`、`self_memory`）
  - `SessionCounters`：计数器层（`task_token_usage`、`user_message_count_since_extract`、`self_memory_turn_counter`）
  - `LiveSessionContext` 现仅含 `stores`、`subsystems`、`counters` 三个子对象 + 4 个状态字段（`known_by_id`、`known_by_label`、`pending_turn`、`llm_semaphore`）
  - `LiveSessionContext` 为内部实现细节，未在公开 API 中导出，无外部破坏

## [0.19.0] - 2026-04-13

### Added
- **`sirius_chat/mixins.py`**：新增公开 Mixin 模块，将 `JsonSerializable` 迁入正式公开命名空间（原 `_mixin.py` 成为向后兼容垫片）。
- **`SessionStore.clear()`**：两种 Store 均新增 `clear()` 方法——`JsonSessionStore.clear()` 删除文件，`SqliteSessionStore.clear()` 清空行保留文件（避免 Windows 文件锁）。
- **序列化线路扩展**：`UserProfile` 和 `Participant` 继承 `JsonSerializable`，自动获得 `to_dict()` / `from_dict()`。
- 新增迁移文档 `docs/migration-v0.19.md`。

### Changed
- **默认 Session Store 改为 SQLite**：`JsonPersistentSessionRunner` 默认使用 `SqliteSessionStore`（`session_state.db`）代替 `JsonSessionStore`。已有 `session_state.json` 用户可显式传入 `JsonSessionStore` 或使用迁移脚本，详见迁移文档。
- **`JsonPersistentSessionRunner.reset_primary_user()`**：改用 `store.clear()`，兼容两种持久化后端，修复 Windows 文件锁导致的 `PermissionError`。
- **`UserMemoryFileStore._entry_to_payload()`**：由手工字段枚举改为 `entry.profile.to_dict()`；实际效果：保存的 `users/*.json` 现在额外包含 `identities` 和 `metadata` 字段（原先被遗漏），已有文件完全向后兼容。
- 所有模型文件内部导入从 `sirius_chat._mixin` 统一改为 `sirius_chat.mixins`。



### Changed
- **持久化模型自戒序列化**：`Message`、`ReplyRuntimeState`、`TokenUsageRecord`、`EventMemoryEntry`、`DiaryEntry`、`GlossaryTerm`、`MemoryFact` 的 `to_dict()` 匹中改用 `dataclasses.asdict()`，`from_dict()` 改用 `dataclasses.fields()` 反射加载。以后再向这些类新增字段（带默认值）时，无需手动更新序列化方法即可自动持久化。
- **`Transcript` 反射字段自动覆盖**：`Transcript.to_dict()` 通过 `dataclasses.fields()` 遍历自动包含未涉及复杂子对象的所有简单标量字段，`from_dict()` 同步采用反射加载并应用框架默认値。
- **Schema 写回机制（write-back on load）**：所有持久化 Store（`JsonSessionStore`、`SqliteSessionStore`、`EventMemoryFileStore`、`SelfMemoryFileStore`、`UserMemoryFileStore`）在 `load()` 完成后立即回写一次数据，确保任何新字段的默认值即时写入现有文件。
- 修复 `MemoryFact.to_dict()`：`context_metadata` 字段此前被遗漏未序列化，现已通过 `asdict()` 自动包含。
- 信息级日志拟人化：将引擎内 `logger.info()` 调用改为更自然的角色化表达，在有上下文的地方嵌入 agent/participant 名称。

### Added
- 引擎级记忆共享：`AsyncRolePlayEngine` 维护 `_shared_user_memory`、`_shared_self_memory`、`_shared_event_stores`，按 `work_path` 键索引，跨 Session 复用内存中的记忆数据，避免重复磁盘 I/O。
- 预处理并行流水线：`_process_live_turn` 中 `_add_human_turn`（含 memory_extract、event_extract）与 `intent_analysis` 通过 `asyncio.gather()` 并发执行，降低主模型调用前延迟。
- 新增迁移文档 `docs/migration-v0.17.md`。

### Changed
- 消息合并策略优化：debounce 窗口内同用户短消息（≤ 30 字符且单行）改用中文逗号 `，` 拼接，长消息及多行消息保留 `\n` 拼接。
- 多模态智能降级：仅当当前批次（最后一次 assistant 回复后的用户消息）含图片时，才以 vision 格式（`image_url`）发送历史图片；否则历史图片折叠为文本描述符 `[图片: url...]`，避免无图轮次触发 vision 定价。

## [0.16.1] - 2026-04-11

### Fixed
- 修复技能（SKILL）执行后大体积结果被注入 transcript 时，`compress_for_budget` 因将 `system` 消息计入字符预算，导致当前 user 消息被弹出、后续重新生成请求不含 user 角色消息、Qwen 等严格校验接口返回 400 的问题。现在字符预算计算中排除 `system` 消息（它们最终被合并进 `system_prompt`，不占 chat_history 预算）。
- 在 `_build_chat_main_request_context` 末尾添加防御性兜底：若构建出的 `chat_history` 不含任何 `user` 消息，自动从 transcript 中回填最近一条 user 消息并输出 WARNING，防止极端压缩场景下 API 拒绝请求。

## [0.16.0] - 2026-04-11

### Added
- 将意图分析器正式纳入任务编排体系，新增 `intent_analysis` 任务配置入口，可通过 `task_enabled`、`task_models`、`task_budgets`、`task_temperatures`、`task_max_tokens`、`task_retries` 单独控制。
- 新增迁移文档 `docs/migration-v0.16.md`，说明如何从 `enable_intent_analysis` / `intent_analysis_model` 迁移到统一任务配置。

### Changed
- `reply_mode=auto` / `smart` 下的 LLM 意图分析现在走统一任务执行路径，支持预算、重试、统计与专用模型配置；任务关闭、预算超限或调用失败时自动退回关键词回退路径。
- `main.py`、库内 `sirius_chat` CLI 与 `ConfigManager` 现在会完整加载 orchestration JSON 中的任务开关、模型、预算和参与决策相关设置，不再忽略这部分配置。

## [0.15.8] - 2026-04-11

### Fixed
- 修复 `merge_provider_sources` 的合并策略：当 session JSON 中未显式指定 `models` 字段时，不再用空列表覆盖 `provider_keys.json` 中已有的模型列表，而是保留持久化配置。这解决了即使已在持久化文件中配置了模型，路由仍然找不到 Provider 的问题。

## [0.15.7] - 2026-04-11

### Changed
- 强化路由约束：当目标模型不匹配任何已注册 Provider 的 `models` 列表或 `healthcheck_model` 时，不再随机回退到第一个 Provider，而是直接抛出 `RuntimeError`。这确保了调用的确定性并能及时发现配置错误。

## [0.15.6] - 2026-04-11

### Fixed
- 移除 `AutoRoutingProvider` 中基于模型名称前缀（如 `deepseek-`, `doubao-`, `/`）的硬编码路由逻辑。
- 路由现在完全遵循：1. 显式 `models` 列表匹配；2. `healthcheck_model` 精确匹配；3. 回退到第一个可用提供商。这解决了不同供应商提供相同模型或相似命名模型时的路由冲突问题。

## [0.15.5] - 2026-04-11

### Fixed
- 修复 `ProviderRegistry.load` 逻辑，增加自动迁移功能：当加载旧版本的 `provider_keys.json` 时，如果发现缺失 `models` 字段，会自动补齐并写回文件。

## [0.15.4] - 2026-04-11

### Changed
- `ProviderRegistry` 持久化 `provider_keys.json` 时现在始终包含 `models` 字段（默认为空列表 `[]`），方便用户直接在 JSON 中快速配置支持的模型。
- 更新 `ProviderRegistry.upsert` 方法，支持显式传入模型列表。

## [0.15.3] - 2026-04-11

### Added
- 新增 `YTeaProvider`，适配 `https://api.ytea.top` OpenAI 兼容接口（需提供 API Key）。

### Changed
- `DeepSeekProvider`、`SiliconFlowProvider`、`VolcengineArkProvider` 的端点 URL 改为硬编码，移除可配置 `base_url` 参数；仅 `OpenAICompatibleProvider` 保留 `base_url`。

## [0.15.0] - 2026-04-11

### Removed
- 删除 `OrchestrationPolicy.self_memory_extract_interval_seconds` 兼容字段，AI 自身记忆不再支持后台定时触发。
- 删除独立 `multimodal_parse` 辅助任务、对应常量与默认配置入口。

### Changed
- 主模型现在直接接收图片的 vision-format 输入；如果需要图片能力，应配置支持视觉的主模型，或通过 `Agent.metadata["multimodal_model"]` 升级模型。
- AI 自身记忆改回在主流程内按 `self_memory_extract_batch_size` 和 `self_memory_min_chars` 触发，保证对高频/长回复场景都能稳定生效。
- 默认示例配置、测试模板、外部接入文档全部同步移除 `multimodal_parse` 键。

### Added
- 新增迁移文档：`docs/migration-v0.15.md`

## [0.14.7] - 2026-04-11

### Changed
- **`message_debounce_seconds` 默认值改为 5.0（生产环境并发消息合并）**
  - 高并发场景（如群聊）多条消息在 5 秒窗口内自动合并为一条，减少 AI 调用次数×提升用户体验
  - **测试环境**：所有测试文件需显式设置 `message_debounce_seconds=0.0`，保证测试速度 < 1 秒
  - 测试不需要等待完整 debounce 时长，只需验证功能逻辑可用性
  - 需要立即处理的场景可显式设为 `message_debounce_seconds=0.0`

## [0.14.6] - 2026-04-11

### Added
- **`write-tests` SKILL**（`.github/skills/write-tests/SKILL.md`）：测试编写完整规范
  - 速度红线：单测 < 1 秒，套件 < 30 秒；禁止 `asyncio.sleep`、debounce、后台任务
  - 标准 `OrchestrationPolicy` 配置模板（关闭所有辅助 LLM 任务）
  - `MockProvider` 与 `_run_live_turns` 标准模式
  - 断言规范、文件组织规范、命名规范
  - 常见陷阱速查表（debounce / enable_self_memory / work_path 污染等）

### Fixed
- **`message_debounce_seconds` 默认值 `8.0` → `0.0`**（性能）
  - 默认 8s 导致每次 `run_live_message` 都睡 8 秒，全套测试从 605s 降至 13s（× 46）
  - Debounce 是群聊 opt-in 功能，需显式设置方可启用



### Changed
- **SelfMemory 触发机制：消息计数 → 定时后台任务**
  - 旧机制（v0.13+）：每 N 条 AI 回复后（`self_memory_extract_batch_size`，默认 3）在主流程中 fire-and-forget；触发频率与对话强度强耦合，对话空闲时不提取，高频对话时每条回复都检查。
  - 新机制（v0.14.5）：以固定时间间隔（`self_memory_extract_interval_seconds`，默认 360 秒 / 6 分钟）在后台任务循环中提取，与对话速率完全解耦；由 `BackgroundTaskManager` 的 `_self_memory_loop` 统一管理。
  - `self_memory_extract_batch_size` 保留在 `OrchestrationPolicy` 中（向后兼容），但 engine 不再使用该字段触发提取。
  - `BackgroundTaskConfig` 新增 `self_memory_enabled` 和 `self_memory_interval_seconds` 字段。
  - `BackgroundTaskManager` 新增 `set_self_memory_callback()`、`trigger_self_memory_now()` 和 `_self_memory_loop()` 方法。
  - `LiveSessionContext.assistant_reply_count_since_self_extract` 字段已移除，替换为 `llm_semaphore: asyncio.Semaphore | None`。

- **LLM 并发限流：`max_concurrent_llm_calls`**
  - 高并发场景下多个用户的消息同时抵达时，原先会触发等量的并行 LLM 生成调用，造成模型侧压力堆积和响应延迟。
  - 新增 `OrchestrationPolicy.max_concurrent_llm_calls`（默认 `1`）：每个 session context 最多允许指定数量的 LLM 主回复生成同时执行，超出部分排队等待。
  - 纯算法路径（热度分析 `HeatAnalyzer`、关键词意图回退）**不受限流影响**，直接运行；只有 `_generate_assistant_message` 受信号量保护。
  - 设为 `0` 则禁用限制（无限并发，与旧版行为一致）。
  - 实现：`LiveSessionContext.llm_semaphore: asyncio.Semaphore | None`；`_noop_semaphore()` 用于限流关闭时的零开销兼容。

- **`_PendingTurn.timer_task` 死代码清理**：移除遗留的 `timer_task` 引用（该字段已在 v0.14.2 debounce 重构时删除）。

### Migration Guide (v0.14.4 → v0.14.5)

**SelfMemory：**
- 原 `self_memory_extract_batch_size=N` 配置不会报错，但已无实际效果（默认每 6 分钟后台提取一次）。
- 如需调整频率，设置 `self_memory_extract_interval_seconds`（单位：秒，推荐 300–600）。

**并发限流：**
- 默认 `max_concurrent_llm_calls=1` 会将主回复生成串行化。如需并发（旧行为），设 `max_concurrent_llm_calls=0`。
- 群聊+多用户场景建议保持 `1`，避免模型排队积压。



### Changed
- **SKILL 执行模式：模板链 → 迭代反馈循环（Breaking Change for v0.14.3 chain syntax）**
  - 旧模式（v0.14.3）：AI 在同一回复中放置多个 `[SKILL_CALL:]`，后续参数用 `${skill_name}` 引用前序结果；engine 一次性执行全部调用，结果全部注入后统一重生。
  - 新模式（v0.14.4）：每轮 AI 只放 **一个** `[SKILL_CALL:]`，engine 立即执行并将结果以 `[SKILL执行结果: skill_name]\n{result}` 注入到对话上下文，然后重新调用 LLM；模型看到真实结果后自主决定下一步（继续调用其他 SKILL、传入新参数，或直接给出最终回复）。
  - 优点：参数值可完全由模型基于实际结果动态生成，无需预先使用 `${template}` 占位符；适应非结构化/意外结果的能力更强。
  - 行为变化：N 次 SKILL 调用现需 N 次 LLM 生成轮次（原为 1 次）。`max_skill_rounds` 语义不变：仍为单 turn 内允许的最大 SKILL→重生轮数。
  - `SkillChainContext`、`SkillResult.get_field()`、`executor.chain_context` 参数仍保留（供高级程序化用途），但 engine 内部不再通过 `chain_context` 进行模板替换。
  - 系统提示词 `<available_skills>` 段已更新为迭代反馈模式说明，删除 `${template}` 语法示例。

### Migration Guide (v0.14.3 → v0.14.4)

**如果你有配置 SKILL 但未使用 `${template}` 语法：** 无需任何修改，行为兼容。

**如果你的 SKILL 提示词/system prompt 中显式教导模型使用 `${skill_name}` 语法：**
- 建议删除相关自定义引导，或替换为："每轮只调用一个SKILL，看到结果后决定下一步，参数直接写你想传的值"
- 模型不再需要预先声明完整调用链，遇到复杂任务可逐步决策

**程序化使用 `SkillExecutor.execute(chain_context=...)` 的代码：** 不受影响，`chain_context` 参数仍然有效（可在自定义流程中继续使用 `${template}` 解析）。


### Added
- **SKILL 链式调用（Chain Invocation）**：AI 现在可在同一回复中顺序调用多个 SKILL，后续 SKILL 的参数可直接引用前序结果。
  - `SkillChainContext`（`sirius_chat/skills/models.py`）：单轮 SKILL 执行的共享上下文，存储每个 SKILL 的 `SkillResult`。
  - 参数模板语法：
    - `${skill_name}` — 引用前序 SKILL 的完整文本输出
    - `${skill_name.field}` — 引用前序 SKILL 返回 dict 的某字段（或 list 的 0 索引）
    - 未能解析的占位符保持原样传入，不会导致执行失败
  - 单轮多调用：引擎在每个生成轮次内按顺序执行当前内容中的**所有** `[SKILL_CALL:]` 标记，而不再仅限于第一个；所有调用共享同一个 `SkillChainContext`。
  - 一轮所有调用完成后才统一重新生成 AI 最终回复（减少 LLM 调用次数）。
  - `SkillResult.get_field(key)` 新方法：支持从 dict/list 结果中取值，用于模板解析。
  - `SkillExecutor.execute()` / `execute_async()` 新增 `chain_context` 可选参数。
  - 系统提示词（`prompts.py`）更新 `<available_skills>` 章节，文档化链式语法和示例。
  - 已知 SKILL 中途遇到未知 SKILL 时，当轮链式调用中止，已执行部分的结果仍保留；未知 SKILL 前的文本不再作为 partial 消息提前发出（保持原有语义）。

### Fixed
- **记忆时间戳动态化**：修复 `participant_memory` 提示词中旧记忆因缺少时间上下文被 AI 误作当前对话的问题。
  - 新增 `_relative_time_zh()` 辅助函数，从 `observed_at` 字段实时计算中文相对时间（"3天前"、"2个月前"等）。
  - 每条 memory fact 附加动态相对时间标签，替代过期的静态 `observed_time_desc` 字符串。
  - `<participant>` 标签新增 `最后记录="X天前"` 属性，AI 可一眼判断该用户数据的新鲜度。
  - `recent_messages` 标签从"近期"改为"历史消息"，消除歧义。
  - 记忆块前缀说明明确标注"历史记忆积累，非当前对话状态"，并添加"不要主动回答记忆中的历史问题"指令。
- **`get_rich_user_summary()` 补全 `observed_at`**：`facts_by_type` 中每条 fact_info 现在包含 `observed_at` 字段，供 prompt 层动态计算时间。
- **新增 `last_fact_at`**：`get_rich_user_summary()` 返回值新增 `last_fact_at` 字段，为该用户所有 memory fact 中最新的 `observed_at` 时间戳。

## [0.14.1] - 2026-04-10

### Removed
- **彻底移除旧意愿分系统兼容代码**：
  - 删除 `OrchestrationPolicy` 中全部 11 个 `auto_reply_*` Legacy 参数（传入将引发 `TypeError`）。
  - 删除 `ReplyWillingnessDecision` dataclass。
  - 删除旧 `_run_intent_analysis()` 方法（由 `_run_engagement_intent_analysis()` 替代）。
  - 删除 `sirius_chat/core/intent.py`（由 `core/intent_v2.py` 替代）。
  - 简化 `_should_reply_for_turn()` 签名为 `(turn: Message) -> bool`。

### Added
- **迁移指南**：新增 `docs/migration-v0.14.md`，覆盖配置迁移对照表、代码迁移示例和检查清单。

## [0.14.0] - 2026-04-10

### Added
- **三级参与决策系统**：完全重写旧意愿分系统（~15 个 auto_reply_* 参数），替换为三个协作子系统：
  - **HeatAnalyzer** (`core/heat.py`)：零 LLM 开销的群聊热度分析，基于消息密度、活跃参与者数和 AI 参与比计算热度等级（cold/warm/hot/overheated）。
  - **IntentAnalyzer v2** (`core/intent_v2.py`)：重写意图分析，新增显式 `target` 字段（ai/others/everyone/unknown），解决群聊中 AI 无法正确识别对话对象的问题。LLM 路径增强上下文（参与者列表 + 8 条近期消息）；关键词回退路径支持参与者名称匹配。
  - **EngagementCoordinator** (`core/engagement.py`)：融合热度、意图和 `engagement_sensitivity` 输出最终回复决策（`EngagementDecision`），内置回复频率限制。
- **简化配置**：仅 `engagement_sensitivity`（0–1，默认 0.5）和 `heat_window_seconds`（默认 60）两个参数。

### Changed
- **OrchestrationPolicy**：旧 auto_reply_* 参数已移除（v0.14.1），新增 `engagement_sensitivity` 和 `heat_window_seconds`。
- **core/engine.py**：`_process_live_turn` 流程重写为 heat → intent v2 → engagement coordinator → frequency limit。
- **core/__init__.py / api/__init__.py**：导出更新为新模块。
- **测试**：重写 `test_async_engine.py`、`test_intent_and_consolidation.py`、`test_self_memory.py` 中所有涉及旧意愿系统的用例。

### Removed
- 引擎内部方法 `_evaluate_reply_willingness`、`_compute_intent_score`、`_compute_addressing_score`、`_compute_event_relevance_score`、`_compute_richness_score`、`_deterministic_probability_roll`。

## [0.13.0] - 2026-04-10

### Added
- **AI 自身记忆系统**（`sirius_chat/memory/self/`）：独立于用户记忆的 AI 自主记忆子系统。
  - **日记子系统 (Diary)**：AI 自主决定需要记忆的内容，每条日记携带重要性评分、关键词标签和分类（reflection/observation/decision/emotion/milestone）。基于时间的遗忘曲线自动衰退置信度（3天95%→180天5%），高重要性条目衰退减缓40%，被提及的条目获得保留加成。
  - **名词解释子系统 (Glossary)**：在对话中收集 AI 不理解的名词，逐步建立定义库。支持多来源（conversation/user_explained/inferred）和多领域（tech/daily/culture/game/custom），相同术语自动合并。
  - **提示词集成**：日记和名词解释分别以 `<self_diary>` 和 `<glossary>` XML 段注入系统提示词，紧凑格式减少 token 消耗。
  - **LLM 自动提取**：每 N 条回复后（`self_memory_extract_batch_size`，默认3）自动触发 LLM 提取日记和名词，fire-and-forget 不阻塞主流程。
  - **持久化**：`SelfMemoryFileStore` 将自身记忆序列化为 `{work_path}/self_memory.json`。
- **回复频率限制器**：基于滑动窗口的 AI 回复频率控制。
  - `reply_frequency_window_seconds`（默认60秒）窗口内超过 `reply_frequency_max_replies`（默认8次）时跳过回复。
  - 对主动提及 AI 名字或别名的消息免除限制（`reply_frequency_exempt_on_mention=True`）。
  - 回复时间戳存储在 `Transcript.reply_runtime.assistant_reply_timestamps` 中。
- **OrchestrationPolicy 新配置项**：`enable_self_memory`、`self_memory_extract_batch_size`、`self_memory_max_diary_prompt_entries`、`self_memory_max_glossary_prompt_terms`、`reply_frequency_window_seconds`、`reply_frequency_max_replies`、`reply_frequency_exempt_on_mention`。
- **测试**：新增 56 条测试覆盖日记/名词解释/衰退/持久化/提示词集成/频率限制器（`test_self_memory.py`）。

### Changed
- **提示词优化**：精简系统提示词文本，缩短 splitting_instruction、skill 规则和 constraints 段，减少 token 消耗。
- **会话后台任务**：归纳周期中同步执行日记衰退与自身记忆持久化。

## [0.12.6] - 2026-04-09

### Fixed
- **恢复 v0.9.4 语义**：当模型在同一轮同时输出 `SKILL_CALL` 与普通用户可见文本时，engine 会继续将清理后的普通文本通过事件总线发送给外部消费者，而不会泄露 `SKILL_CALL` 标记本身。
- **on_reply 同轮提示恢复**：修复此前过度抑制中间文本导致外部插件收不到“正在查询中”等正常提示文案的问题。

### Added
- **回归测试**：新增 `test_on_reply_emits_plain_text_alongside_skill_call`，覆盖 `SKILL_CALL + 普通文本` 同轮输出场景。

## [0.12.5] - 2026-04-09

### Fixed
- **SKILL 空回复兜底优化**：当 SKILL 已执行但模型未生成最终自然语言答复时，engine 不再直接输出固定报错，而是优先基于前置文案和最后一次 SKILL 结果生成可用摘要回复。
- **外部 on_reply 可用性提升**：避免外部消费者收到“已执行 skill 但没有任何有用内容”的低质量兜底消息。

### Added
- **回归测试**：新增 `test_skill_rounds_exhausted_fallback_uses_skill_result_summary`，覆盖“多次 SKILL_CALL 后仅能依赖 skill 结果摘要输出”的场景。

## [0.12.4] - 2026-04-09

### Fixed
- **SKILL 轮次耗尽导致空回复**：当模型在 SKILL 执行后持续返回 `SKILL_CALL` 或最终内容被清理为空时，engine 会强制触发一次“仅生成最终答复”的再生成，避免落地空 assistant 消息。
- **最终回复兜底**：若再生成后仍为空，返回明确兜底文本，确保外部回调始终可收到可用回复。

### Added
- **回归测试**：新增 `test_skill_rounds_exhausted_still_returns_final_answer`，覆盖 transcript 中出现的“多次 SKILL 结果后 assistant 为空”场景。

## [0.12.3] - 2026-04-09

### Fixed
- **SKILL 命中失败即时重载**：当 `SKILL_CALL` 已解析但 `skill_registry.get()` 未命中时，engine 会即时重载 `work_path/skills` 并二次查找，修复“skill 文件存在但上下文复用导致注册表未命中”的问题。
- **on_reply 中间态泄露修复**：SKILL 轮次中的中间 assistant 文本不再通过事件总线对外发送，避免外部插件再次收到调用前文案。
- **未知 SKILL 回退输出**：未知技能场景不再提前结束，改为注入系统提示后再生成最终回复，确保外部仍能收到技能后语义完整输出。

### Added
- **回归测试**：新增/增强 on_reply+SKILL 与注册表重载命中路径测试，覆盖插件侧真实调用场景。

## [0.12.2] - 2026-04-09

### Fixed
- **SKILL 运行时懒挂载**：修复 live context 复用场景下，`enable_skills` 后置开启或 `skills/` 目录后置就绪时 `skill_registry/skill_executor` 可能为空，导致 `SKILL_CALL` 被解析但未进入执行分支的问题。
- **on_reply + SKILL 回调可达性**：增强 `run_live_message(..., on_reply=...)` 路径，确保技能执行后 assistant 内容可稳定通过回调送达外部插件。
- **可观测性增强**：当检测到 `SKILL_CALL` 但技能运行时未就绪时输出明确 warning，便于外部插件快速定位配置/挂载问题。

### Added
- **回归测试**：新增 context 复用下技能懒挂载测试 `test_skill_runtime_lazy_attach_when_context_reused`，覆盖“先无技能再启用技能”的真实插件路径。

## [0.12.1] - 2026-04-09

### Fixed
- **on_reply 回调订阅竞态**：修复 `run_live_message(..., on_reply=...)` 在高并发时可能在订阅建立前开始处理消息，导致首批事件丢失的问题；该问题会在外部插件的 SKILL 场景中表现为回复未正确投递。
- **SKILL 回调链路稳定性**：确保 `on_reply` 模式下 SKILL 执行后的 assistant 消息稳定送达回调，避免出现“技能执行了但外部未收到回复”的现象。

### Added
- **回归测试**：新增 `test_on_reply_callback_with_skill_execution`，覆盖 `on_reply + SKILL` 组合路径，验证 SKILL_CALL 标记不外泄且最终回复可达。

## [0.12.0] - 2026-04-10

### Added
- **`arun_live_message` 新增三个可选参数**：
  - `on_reply: Callable[[Message], Awaitable[None]]`：engine 自动管理事件订阅与消费，每条 AI 回复触发回调，外部无需操作 `asubscribe`/事件总线。
  - `user_profile: UserProfile | None`：自动注册用户到记忆系统，免去外部手动 `register_user` 调用。
  - `timeout: float`：engine 级超时，超时后自动清理内部资源并抛出 `TimeoutError`。
- **内部方法 `_run_live_message_with_callback`**：封装事件订阅、回调消费、超时清理的完整流程。

### Fixed
- **debounce `CancelledError` 吞没外部超时**：修复 `_run_live_message_core` 中 debounce sleep 的 `except CancelledError: return transcript` 错误地拦截了外部 `asyncio.wait_for` 的超时取消信号。移除该捕获，使外部超时与关停取消能正确传播。

### Changed
- **外部插件样板代码精简**：`sirius_chat_group` 插件的 `_chat_once_locked` 和 `_chat_private_once_locked` 各减少约 45 行手动事件订阅/消费/清理代码，改为使用 `on_reply` + `timeout` 参数。
- **迁移指南**：`docs/migration-v0.12.md`。

## [0.11.0] - 2026-04-09

### Added
- **Token 使用 SQLite 持久化** (`sirius_chat/token/store.py`)：新增 `TokenUsageStore` 类，每次模型调用自动将 `TokenUsageRecord` 写入 `{work_path}/token_usage.db`。基于 Python 标准库 `sqlite3`，无新依赖。支持 WAL 模式、批量写入、跨会话查询与多条件筛选。
- **多维度 Token 分析模块** (`sirius_chat/token/analytics.py`)：基于 SQLite 的全量分析函数集：
  - `compute_baseline()`：全局/筛选级基线统计（总调用数、token 合计、均值、重试率、completion/prompt 比值）
  - `group_by_session()`：按会话聚合
  - `group_by_actor()`：按用户聚合
  - `group_by_task()`：按任务类型聚合
  - `group_by_model()`：按模型聚合
  - `time_series()`：按固定时间桶聚合（默认 1 小时）
  - `full_report()`：一次性输出包含 baseline + 所有维度的完整报告
- **引擎自动集成**：`AsyncRolePlayEngine` 在初始化 live session 时自动创建 `TokenUsageStore`，每次 `_call_provider_with_retry` 成功后同步写入 SQLite，与现有 `Transcript.token_usage_records` 内存归档并行，向后兼容。
- **公共 API 导出**：`TokenUsageStore`、`AnalyticsReport`、`BaselineDict`、`BucketDict`、`TimeSliceDict`、`compute_baseline`、`full_report`、`group_by_actor`、`group_by_model`、`group_by_session`、`group_by_task`、`time_series`。
- **意图分析增强**：`IntentAnalysis` 新增 `reason` 和 `evidence_span` 字段，LLM 路径和关键词回退路径均填充解释信息；JSON 解析失败时记录 `WARNING` 级日志。

### Fixed
- **消息尾部空白清理**：所有 `Message` 在创建时和通过 `Transcript.add()` 添加时，自动去除尾部 `\n` 和空格。

## [0.10.0] - 2026-04-09

### Added
- **意图分析系统** (`sirius_chat/core/intent.py`)：LLM-based 用户意图分析器，支持 question/request/chat/reaction/information_share/command 六种意图分类。LLM 路径默认启用（`enable_intent_analysis=True`）；可显式设为 `False` 退回关键词回退路径（零 LLM 开销）。
- **系统提示词段落跳过** (`skip_sections`)：意图分析可判定当前消息是否需要参与者记忆或会话摘要，跳过不需要的段落以减少 token 消耗。
- **事件归纳** (`EventMemoryManager.consolidate_entries`)：按 category 分组使用 LLM 归纳合并冗余观察记录。
- **摘要归纳** (`UserMemoryManager.consolidate_summary_notes`)：LLM 合并冗余摘要为精炼条目。
- **事实归纳** (`UserMemoryManager.consolidate_memory_facts`)：LLM 按 fact_type 合并冗余事实，保留最高 confidence 与累加 mention_count。
- **后台归纳循环** (`BackgroundTaskManager`)：新增记忆归纳定时循环，支持异步回调注入与 `trigger_consolidation_now()` 即时触发。
- **`OrchestrationPolicy` 新增配置字段**：`enable_intent_analysis`、`intent_analysis_model`、`consolidation_enabled`、`consolidation_interval_seconds`、`consolidation_min_entries`、`consolidation_min_notes`、`consolidation_min_facts`。
- **公共 API 导出**：`IntentAnalysis`、`IntentAnalyzer`、`BackgroundTaskConfig`、`BackgroundTaskManager`。

### Fixed
- **意图分析意愿修正隔离**：关键词回退路径（`enable_intent_analysis=False` 时）不再修改 willingness score，避免低阈值配置下误拒回复。

## [0.9.4] - 2026-04-08

### Fixed
- **SKILL 前置内容丢失**：当模型输出中 SKILL_CALL 标记与普通文字同时出现时（如 `[SKILL_CALL: ...]\n\n好的喵！`），SKILL_CALL 工标已被 `strip_skill_calls` 清理后的剩余文字现在会经过事件总线送出。逆转了 v0.9.3 中过度抹除该路径事件发送的错误修复。

## [0.9.3] - 2026-04-08

### Fixed
- **分割消息尾部空白**：所有经分割标记拆分和未拆分路径的消息内容均统一对尾部空白字符（`\n`、空格等）执行 `rstrip()` 清理。
- **SKILL 中间消息对外泄露**：移除 SKILL 执行过程中 `partial_msg`（SKILL 调用前的局部内容）的事件总线发送。外部订阅者现在只会收到 SKILL 执行完成后重新生成的最终消息，避免中间状态消息被外部平台识别为特殊卡片格式。

## [0.9.2] - 2026-04-08

### Fixed
- **`<MSG_SPLIT>` 明文输出**：当模型在同一回复中同时输出 `<MSG_SPLIT>` 和 `[SKILL_CALL: ...]` 时，SKILL 执行前提取的 `remaining_content` 未走分割逻辑，导致标记被原样输出。现在 `partial_msg` 路径也会对分割标记进行拆分处理。
- **`[SKILL_CALL: ...]` 残留输出**：当 SKILL 调用轮次达到 `max_skill_rounds` 上限后强制退出循环，此时 `content` 中可能仍残留 `[SKILL_CALL: ...]` 文本。现在循环退出后统一执行 `strip_skill_calls` 清理。

## [0.9.1] - 2026-04-08

### Changed
- **消息分割提示词强化**：`enable_prompt_driven_splitting=True` 时注入的分割指令更新：明确声明群聊场景、要求每条消息简短（1-2 句）、强制禁止用连续换行代替分割符，引导模型始终使用 `split_marker` 分割独立内容。

## [0.9.0] - 2026-04-08

### Added
- **Session 级事件流**：新增 `SessionEventBus`、`SessionEvent`、`SessionEventType`，提供实时 pub/sub 事件推送
- **`engine.subscribe(transcript)`**：返回 `AsyncIterator[SessionEvent]`，外部可持续接听会话事件
- **`asubscribe()` API 门面**：高层异步订阅接口
- 7 种事件类型：`MESSAGE_ADDED`、`PROCESSING_STARTED`、`PROCESSING_COMPLETED`、`SKILL_STARTED`、`SKILL_COMPLETED`、`REPLY_SKIPPED`、`ERROR`
- 迁移文档：`docs/migration-event-stream.md`

### Removed
- **`on_message` 回调参数**（破坏性变更）：从 `run_live_message()`、`arun_live_message()`、`run_session()` 移除
- **`OnMessage` 类型别名**：已由 `SessionEvent` 替代

### Changed
- 消息投递模型从回调式改为 pub/sub 事件流，外部消费者通过 `subscribe()` 获取实时事件

## [0.8.4] - 2026-04-08

### Added
- 引擎会在 `run_live_session` 初始化阶段始终创建 `{work_path}/skills/` 目录及 `README.md`，即使 `enable_skills=False` 也保留目录引导结构。

### Changed
- `OrchestrationPolicy.enable_skills` 默认值调整为 `True`，SKILL 系统改为默认开启。

## [0.8.3] - 2026-04-08

### Added
- 启用 SKILL 系统时，框架会自动在当前 `work_path` 下创建 `skills/` 目录，并生成 `README.md` 引导文档。

### Changed
- `SkillRegistry.load_from_directory()` 不再在目录缺失时直接返回，而是先完成 SKILL 目录初始化再继续扫描。

## [0.8.2] - 2026-04-08

### Added
- **`PersonaSpec` 持久化生成规格**：新增 `PersonaSpec` dataclass，封装角色生成的全部输入（keywords、answers、background 等），随生成结果一起写入 `generated_agents.json`，支持增量微调
- **Tag-based 构建路径**：`PersonaSpec(trait_keywords=[...])` 仅凭关键词列表即可生成完整角色，无需完整问卷访谈
- **Hybrid 构建路径**：同时提供 `trait_keywords` + `answers`，关键词锚定特质、问答丰富细节
- **`agenerate_from_persona_spec()`**：统一生成入口，支持 tag-only / Q&A / hybrid 三条路径
- **`aupdate_agent_prompt()`**：增量微调已生成的 agent，仅更新指定字段（背景/关键词/答案），无需全量重写
- **`load_persona_spec()`**：加载已持久化的 `PersonaSpec`
- 迁移文档：`docs/migration-roleplay-v082.md`
- 7 个新测试覆盖 PersonaSpec/tag-based/hybrid/update 路径

### Changed
- **`Agent.persona` 语义**：由 200-400 字描述性文本改为 3-5 个关键词标签（'/' 分隔，≤30 字）；完整角色指南移至 `global_system_prompt`
- **`abuild_roleplay_prompt_from_answers_and_apply`** 的 `answers` 参数由必填改为可选，新增 `trait_keywords` 和 `persona_spec` 参数
- **LLM 提示词精简**：生成提示词（system + user prompt）总长减少约 60%，结构更清晰



### Added
- **SKILL 依赖自动安装**：加载 SKILL 文件前自动检测并安装缺失的第三方依赖
  - 新增 `sirius_chat/skills/dependency_resolver.py`：AST 扫描 `SKILL_META["dependencies"]` 和 import 语句
  - 优先使用 `uv pip install`，回退到 `pip install`
  - `OrchestrationPolicy` 新增 `auto_install_skill_deps`（默认 True），可在受限环境关闭
  - `SKILL_META` 新增可选 `dependencies` 字段用于显式声明包名
- **迁移文档**：新增 `docs/migration-v0.8.md`，提供 v0.7→v0.8 全量变更指南（供 AI 查阅）

### Changed
- **测试套件瘦身与整合**：36 个测试文件整合为 27 个，删除 9 个冗余/微型文件
  - 4 个独立 provider 测试 + mock + middleware → 统一 `test_providers.py`（参数化基准测试）
  - `test_token_usage.py` → 并入 `test_token_utils.py`
  - `test_session_store.py` → 并入 `test_session_runner.py`
  - `test_main_resume.py` → 并入 `test_main_bootstrap.py`
  - Provider 测试改为参数化基准模式，新增 provider 只需扩展注册表

## [0.8.0] - 2026-04-10

### Added
- **System Prompt 瘦身**：大幅压缩系统提示词体积（约 22%），合并 `<output_constraints>` 与 `<security_constraints>` 为 `<constraints>`，压缩参与者记忆格式（`?`/`~` 替代冗长标签）
- **SKILL 执行超时**：`OrchestrationPolicy` 新增 `skill_execution_timeout`（默认 30 秒），超时后返回 `SkillResult(success=False)` 及友好提示
- **环境上下文注入**：`run_live_message` / `arun_live_message` 新增 `environment_context` 参数，允许外部注入群组信息、渠道上下文等附加信息，自动写入系统提示词的 `<environment_context>` 段
- **SKILL 编写指南**：新增 `docs/skill-authoring.md`，提供 AI 友好的 SKILL 开发模板与规范
- **外部调用同步指南**：新增 `docs/integration-sync-guide.md`，供 AI 编码助手在变更接口后快速同步外部调用
- 新增 13 个测试覆盖超时、环境上下文、提示词紧凑格式

## [0.7.0] - 2026-04-09

### Added
- **SKILL 系统**：AI 可在运行时调用外部 Python 代码的扩展机制
  - `sirius_chat/skills/models.py`：SkillDefinition、SkillParameter、SkillResult 数据模型
  - `sirius_chat/skills/registry.py`：从 `{work_path}/skills/` 自动发现并加载 SKILL 文件
  - `sirius_chat/skills/executor.py`：参数校验、类型转换和安全执行
  - `sirius_chat/skills/data_store.py`：每个 SKILL 独立的 JSON 持久化键值存储
  - `OrchestrationPolicy` 新增 `enable_skills`、`skill_call_marker`、`max_skill_rounds` 配置
  - 引擎通过 `[SKILL_CALL: name | {params}]` 提示词驱动机制检测和执行 SKILL 调用
  - 持久化数据通过 `data_store` 参数自动注入 SKILL 的 `run()` 函数
  - 新增示例 SKILL：`examples/skills/system_info.py`
  - 新增 50 个 SKILL 系统专项测试

## [0.6.0] - 2026-04-08

### Breaking Changes
- **MemoryFact 模型重构**
  - 删除 `is_transient` 字段，改为 `is_transient(threshold=0.85)` 动态方法
  - 删除 `created_at` 字段，统一使用 `observed_at`
  - 新增 `__post_init__` 自动钳位 confidence 到 [0.0, 1.0]
- **衰退曲线更新**：`MemoryForgetEngine.DEFAULT_DECAY_SCHEDULE` 更为激进（180天: 0.20→0.05）

### Added
- **MemoryPolicy 集中配置** (`OrchestrationPolicy.memory`)
  - `max_facts_per_user`：每用户最大记忆条目数（默认50）
  - `transient_confidence_threshold`：RESIDENT/TRANSIENT 分界线（默认0.85）
  - `event_dedup_window_minutes`：事件去重窗口（默认5分钟）
  - `max_observed_set_size`：observed_* 集合大小上限（默认100）
  - `max_summary_facts_per_type`：摘要每类型限制（默认5）
  - `decay_schedule`：可配置衰退时间表
- **MemoryFact 富上下文字段**
  - `mention_count`：去重提频计数
  - `source_event_id`：事件来源追踪
  - `context_channel` / `context_topic`：渠道与主题上下文
  - `observed_time_desc`：人类友好时间描述
- **UserMemoryManager 增强**
  - `add_memory_fact()` 自动去重提频（同 fact_type+value 递增 mention_count）
  - `get_resident_facts()` / `get_transient_facts()` 支持自定义 threshold
  - `get_rich_user_summary()` 支持 `max_facts_per_type` 限长
  - `apply_event_insights()` 支持 `source_event_id`，observed_* 集合自动 cap
- **序列化完整性**：UserMemoryFileStore 从 5 字段升级到 12 字段，向后兼容旧格式
- **apply_decay 自定义 schedule**：`MemoryForgetEngine.apply_decay()` 新增 `decay_schedule` 参数
- 新增迁移文档 `docs/migration-memory-v2.md`
- 新增 26 个记忆系统 V2 专项测试

### Changed
- `message_debounce_seconds` 默认值从 0.0 调整为 5.0

### Fixed
- 修复 `_cap_set()` 方法内残余的重复代码块导致 `NameError: event_features`
- 修复 `test_run_live_session_reply_runtime_persists_across_calls` 未显式设置 debounce 导致的测试失败

## [0.5.11] - 2026-04-07

### Changed
- 引擎层 provider 调用新增超时兜底，避免上游请求长时间阻塞导致消息处理卡住
- orchestration 配置日志增加去重，同一配置不再在每条消息重复打印“多模型协同（方案2）”

### Test
- 新增回归测试，验证 memory_extract 超时不会阻塞 live message 执行

## [0.5.10] - 2026-04-07

### Changed
- **用户画像提取器上下文增强**
  - `memory_extract` 不再只解析单句，改为携带最近聊天上下文
  - 输入中会包含最新用户消息、最近用户/助手对话片段，帮助模型更准确推断画像

### Removed
- **事件提取时间窗口去重**
  - 去掉 `event_extract` 的短时间去重跳过逻辑
  - 连续消息会按并行流程正常触发事件提取

### Test
- 新增回归测试，验证用户画像提取请求包含最近聊天上下文
- 新增回归测试，验证连续消息不会被 event_extract 去重跳过

## [0.5.9] - 2026-04-07

### Changed
- **chat_main system 消息注入策略调整**
  - 将 transcript 内的 `system` 消息统一合并到首个 `system_prompt`
  - `chat_main` 的 `messages` 不再携带中途 `role=system` 历史项
  - 保留内部系统信息语义，同时降低模型对中途 system 行的复述倾向

### Test
- 新增回归测试，验证第二轮 `chat_main` 请求中：
  - `messages` 无 `role=system`
  - `system_prompt` 包含“会话内部系统补充”与事件说明

## [0.5.8] - 2026-04-07

### Added
- **下游安全消息提取 API**
  - 新增 `extract_assistant_messages(transcript, since_index=0)`，用于只提取 assistant 消息下发
  - 更新示例代码，避免将 system 内部说明误发到聊天渠道
- **OpenAI provider 测试覆盖补齐**
  - 新增 `tests/test_openai_compatible_provider.py`

### Changed
- **Provider 响应解析兼容性增强**
  - 新增统一解析工具 `providers/response_utils.py`
  - `openai_compatible` / `siliconflow` / `deepseek` / `volcengine_ark` 统一支持结构化 `content`
  - 支持 `refusal` / `output_text` 等字段回退，减少误判“响应为空”
- **Provider DEBUG 日志增强**
  - 在 DEBUG 级别新增“模型原始响应 raw”日志，便于线上排障

## [0.5.7] - 2026-04-07

### Added
- **DeepSeek provider 适配**
  - 新增 `DeepSeekProvider`，默认基地址 `https://api.deepseek.com`
  - 兼容传入 `https://api.deepseek.com/v1` 的 base_url 规范化
  - 支持 `reasoning_content` 回退解析
- **DeepSeek 示例配置**
  - 新增 `examples/session.deepseek.json`，可直接用于 DeepSeek 快速接入

### Changed
- **Provider 路由增强**
  - 自动路由新增 `deepseek` 平台与模型前缀识别
  - 支持平台清单增加 `deepseek`

### Docs
- 更新 README、架构文档、外部接入文档与相关 SKILL，补充 DeepSeek 使用方式

## [0.5.6] - 2026-04-06

### Added
- **辅助任务并行执行**
  - 单条用户消息处理中，`memory_extract`、`multimodal_parse`、`event_extract` 改为并行调度
  - 保留 `memory_manager` 后置执行，确保汇聚阶段读取的是已更新记忆
  - 新增并行回归测试，验证单轮内辅助任务存在重叠执行

### Changed
- **点名回复概率增强**
  - 在 `session_reply_mode=auto` 下，明确点名主 AI 时提高概率兜底下限
  - 降低“被叫到但未回复”的体验问题

### Docs
- 更新 `OrchestrationPolicy` 文档与示例，使字段说明与实现保持一致
- 修正文档中对 `orchestration.enabled` 的过时描述

## [0.5.5] - 2026-04-06

### Added
- **auto 回复概率决策日志增强**
  - 新增 `[会话] 触发回复` 日志，输出 `trigger`（`threshold` 或 `probability_fallback`）
  - 输出 `score`、`threshold`、`probability`、`roll`，便于在线调参与回放分析

### Changed
- **意愿系统概率兜底补齐**
  - 在分数未过阈值时，按 `auto_reply_probability_coefficient` 与 `auto_reply_probability_floor` 计算兜底回复概率
  - 保持 `session_reply_mode=auto` 下的长期参与性，降低连续沉默概率

## [0.5.4] - 2026-04-06

### Added
- **日志基础信息增强**
  - 在模型调用 INFO 日志中新增 `调用目的`、`预计输入Token`、`预计总Token上限`
  - 引入统一估算函数 `estimate_generation_request_input_tokens()` 用于请求输入 token 粗估

### Changed
- **GenerationRequest 扩展**
  - 新增 `purpose` 字段（默认 `chat_main`），支持多场景调用意图标识
  - 已在核心调用链路补充 purpose：
    - `chat_main`
    - `memory_extract` / `multimodal_parse` / `event_extract` / `memory_manager`
    - `roleplay_prompt_generation`
    - `event_memory_verification`
    - `provider_healthcheck`

### Test Results
- 278 个测试通过 ✅
- 1 个测试被跳过
- 0 个测试失败 ✅

## [0.5.3] - 2026-04-06

### Fixed
- **前移到提示词层的元信息防泄漏策略**
  - `build_system_prompt()` 对参与者记忆注入增加“反结构化复述”约束
  - 明确记忆仅供语义理解，不应影响回复的字段结构、分隔符和顺序
  - 输出边界约束继续禁止复述内部记忆元信息

- **元信息清洗规则增强**
  - 对模型输出中的内部元信息行继续做清洗兜底
  - 兼容字段乱序、字段缺失、中文/英文标签变体

### Changed
- **模型调用日志分级优化**
  - INFO 仅保留模型名、温度、token 上限、消息数、响应字数
  - DEBUG 输出完整输入输出，便于排查问题

### Test Results
- 278 个测试通过 ✅
- 1 个测试被跳过
- 0 个测试失败 ✅

## [0.5.2] - 2026-04-06

### Fixed
- **防止内部记忆元信息外泄到用户回复**
  - 在系统提示词中新增输出边界约束，明确禁止输出内部记忆元字段（置信度/类型/来源/时间/内容）
  - 在引擎回复落地前增加清洗逻辑，过滤结构化元信息泄漏行
  - 对过滤后为空的极端情况提供安全回退回复

### Changed
- **模型调用日志分级优化**
  - INFO 仅保留基础信息（模型名、温度、token 上限、消息数、响应字数）
  - DEBUG 输出完整模型输入（system_prompt + messages）和完整模型输出（不截断）
  - 覆盖 provider：openai-compatible / siliconflow / volcengine-ark / mock

### Test Results
- 276 个测试通过 ✅
- 1 个测试被跳过
- 0 个测试失败 ✅

## [0.5.1] - 2026-04-06

### Added
- **动态模型路由配置 API**：新增灵活的多模态模型配置方式
  - `create_agent_with_multimodal(...)` 便捷构造函数
  - `auto_configure_multimodal_agent(agent, multimodal_model=...)` 灵活参数化配置
  - 手动配置：直接设置 `agent.metadata["multimodal_model"]`
  - 透明的自动路由：无多媒体内容使用廉价模型，有多媒体自动升级至指定的多模态模型

### Changed
- **提示词生成器大幅优化** (sirius_chat/roleplay_prompting.py)
  - 精简拟人问题从 17 个核心到 8 个高质量问题，提高信号强度
  - 每个问题添加详细的 hints 字段，为回答者提供更清晰的引导
  - Agent 基础信息（name、alias、model、temperature、max_tokens）现在被精确传送至 LLM
  - 补充信息（background、alias）权重强化，单独作为【补充信息】块呈现
  - 重写 LLM 指令和输出规范，明确 agent_persona 与 global_system_prompt 的职责差异
  - 系统提示词生成时自动包含安全约束，防止 AI 泄露系统提示词

- **文档和 SKILL 同步更新**
  - docs/external-usage.md：新增 Agent 多模态配置的详细说明和三种配置方法示例
  - docs/architecture.md：新增动态模型路由的设计原理和使用指导
  - .github/skills/external-integration/SKILL.md：补充多模态模型配置建议
  - .github/skills/framework-quickstart/SKILL.md：补充动态模型路由设计说明

### Test Results
- 274 个测试通过 ✅
- 1 个测试被跳过
- 0 个测试失败 ✅

---

## [Unreleased - Previous]

### Changed
- **API隔离迁移完成** (Stage 1-4)：将单体模块分解为逻辑清晰的独立子包
  - `sirius_chat/config/`：配置管理（models.py, manager.py, helpers.py, __init__.py）
  - `sirius_chat/core/`：核心编排引擎（engine.py, __init__.py）
  - `sirius_chat/memory/`：统一记忆系统（user/, event/, quality/ 子模块）
    * `memory/user/`：用户档案与记忆管理（models.py, manager.py, store.py）
    * `memory/event/`：事件记忆系统（models.py, manager.py, store.py）
    * `memory/quality/`：记忆质量评估与智能遗忘（models.py, tools.py）
  - `sirius_chat/models/`：数据模型与结构定义（models.py, __init__.py）
  - `sirius_chat/session/`：会话管理与持久化（runner.py, store.py, __init__.py）
  - `sirius_chat/token/`：Token管理与使用统计（usage.py, utils.py, __init__.py）
- **删除所有弃用的包装文件**：
  - `config_manager.py`（使用 `from sirius_chat.config import ConfigManager`）
  - `orchestration_config.py`（使用 `from sirius_chat.config import configure_*`）
  - `user_memory.py`（使用 `from sirius_chat.memory import UserMemoryManager`）
  - `event_memory.py`（使用 `from sirius_chat.memory import EventMemoryManager`）
  - `async_engine/core.py`（使用 `from sirius_chat import AsyncRolePlayEngine`）
  - `memory_quality.py` / `memory_quality_tools.py`（使用 `from sirius_chat.memory.quality import *`）
- **更新所有导入路径**：20+ 个源文件和文档已升级到新的导入路径
- **清理过时设计文档**：
  - 删除 C2C3_ARCHITECTURE_DESIGN.md、C2C3_IMPLEMENTATION_COMPLETE.md
  - 删除 PERFORMANCE_OPTIMIZATION_PLAN.md、PERFORMANCE_OPTIMIZATION_IMPLEMENTATION.md
  - 统一文档维护在 docs/architecture.md 而非独立设计文档

### Fixed
- **删除过时和冗余的测试**：
  - test_event_user_memory_integration.py：移除3个调用不存在方法的测试
  - test_api_integrity.py：移除测试已删除弃用导入的测试
  - sirius_chat/core/engine.py：移除调用不存在的 `interpret_event_with_user_context()` 的代码
- **修复test_orchestration_config.py**：更新导入从 `async_engine.orchestration_config` 到新的模块位置

### Test Results
- 256 个测试通过 ✅
- 1 个测试被跳过
- 0 个测试失败 ✅

---

## [Unreleased]

### Added
- **logging_config.py**: 生产级日志系统，支持JSON结构化输出、彩色控制台格式、日志文件循环
  - JSONFormatter：输出机器可读的JSON日志
  - ColoredFormatter：ANSI彩色的人类友好日志
  - configure_logging()：集中配置函数，支持DEBUG/INFO/WARNING/ERROR级别
- **exceptions.py**: 语义化的异常体系（18个自定义异常类）
  - 基础：SiriusException（error_code, context, is_retryable）
  - 分类：ProviderError, TokenError, ParseError, ConfigError, MemoryError
  - 特性：上下文信息、可重试标记、序列化支持
- **token_utils.py**: 多语言感知的Token估算工具
  - estimate_tokens_heuristic()：中英文感知估算（中文1字=1token, 英文4字=1token）
  - estimate_tokens_with_tiktoken()：可选的精确计数（若安装tiktoken）
  - estimate_tokens()：智能回退实现（优先tiktoken，降级启发式）
  - 支持多个模型配置（gpt-4, claude-3, doubao-seed等）
- **cli_diagnostics.py** (P0-005): CLI 诊断和环境检查工具
  - EnvironmentDiagnostics：Python版本、工作目录、配置文件、Provider配置检查
  - run_preflight_check()：启动前全面检查，给出详细建议
  - generate_default_config()：生成默认配置文件模板
- **Provider 中间件系统** (P1-003)：可组合的Provider功能框架
  - `sirius_chat/providers/middleware/base.py`：Middleware ABC、MiddlewareContext、MiddlewareChain
  - `sirius_chat/providers/middleware/rate_limiter.py`：RateLimiterMiddleware（固定窗口）、TokenBucketRateLimiter（令牌桶算法）
  - `sirius_chat/providers/middleware/retry.py`：RetryMiddleware（指数退避）、CircuitBreakerMiddleware（故障转移）
  - `sirius_chat/providers/middleware/cost_metrics.py`：CostMetricsMiddleware（成本计量与追踪）
  - 支持链式添加中间件，支持异步请求/响应处理
- **async_engine 包重构** (P0-003 Phase 1-2)：将单个 async_engine.py 模块分解为多模块包
  - `sirius_chat/async_engine/core.py`：核心 AsyncRolePlayEngine 类，保持公开 API 不变
  - `sirius_chat/async_engine/utils.py` (120+ 行)：工具函数模块
    * build_event_hit_system_note()：事件记忆命中提示生成
    * record_task_stat()：任务统计记录
    * estimate_tokens()：Token 计算 (cheap heuristic)
    * extract_json_payload()：JSON 有效载荷提取
    * normalize_multimodal_inputs()：多模态输入规范化和验证
  - `sirius_chat/async_engine/prompts.py` (90+ 行)：系统提示构建
    * build_system_prompt()：生成完整系统提示，整合agent身份、用户记忆、时间上下文
  - `sirius_chat/async_engine/orchestration.py` (90+ 行)：任务编排配置和管理
    * 任务常量定义 (TASK_MEMORY_EXTRACT, TASK_MULTIMODAL_PARSE 等)
    * TaskConfig dataclass：任务配置管理
    * get_task_config()：从 SessionConfig 提取任务配置
    * get_system_prompt_for_task()：获取任务系统提示
- **事件系统与用户记忆系统的双向适配** (方案C)：
  - `UserRuntimeState` 扩展：支持 observed_keywords/observed_roles/observed_emotions/observed_entities 集合
  - `ContextualEventInterpretation` 新数据类：事件与用户历史的对齐度评分与上下文理解
  - `UserMemoryManager.apply_event_insights()`：将事件特征自动转化为用户记忆事实
    * emotion_tags → emotional_pattern 事实 (信度 base - 0.05)
    * keywords → user_interest 事实 (信度 base - 0.10)
    * role_slots → social_context 事实 + 自动特征提升 (信度 base - 0.05)
    * entities → observed_entities 集合
  - `UserMemoryManager.interpret_event_with_user_context()`：基于用户历史调整事件理解
    * 计算四维对齐度：keyword_alignment / role_alignment / emotion_alignment / entity_alignment
    * 动态信度调整：`adjusted_confidence = 0.65 + avg_alignment × 0.3`，范围 [0.5, 1.0]
    * 推荐处理类别：high_confidence(avg>0.6) | normal | low_relevance(avg<0.2) | pending(新用户)
  - 在 async_engine._add_human_turn() 中集成新流程：事件提取 → apply_event_insights() → interpret_event_with_user_context() → event_context_note
  - 实现**双向观测管道**：事件特征 → 用户理解 + 用户历史 → 事件信度调整
  - 提升事件特征转化率：0% → 95%+，真正构建统一的用户心智模型
  - 新增9个集成测试，验证情感、关键词、角色、对齐度、序列化等能力

### Changed
  - `.github/workflows/ci.yml`：GitHub Actions 工作流，支持多版本 Python (3.10, 3.11, 3.12) 测试、代码质量检查、安全扫描、构建验证
  - `.pre-commit-config.yaml`：预提交钩子配置（black, isort, flake8, mypy, bandit, yamllint 等）
  - `scripts/ci_check.py`：本地/CI 代码质量检查脚本（格式、lint、类型、测试、覆盖率）
  - `scripts/setup_dev_env.py`：开发环境自动初始化脚本
  - `Makefile`：便捷的开发命令集（format, lint, typecheck, test, build 等）
- **PROJECT_ISSUES.md**: 项目问题与改进方向追踪文档
  - P0（5项）、P1（4项）、P2（4项）优先级划分
  - 3个月 roadmap 与进度矩阵
- **集成测试框架** (P1-001)：
  - `tests/integration/`：网络弹性、并发会话、故障转移测试
  - `tests/benchmarks/`：性能吞吐量、延迟、可扩展性基准测试
  - `conftest.py`：MockLLMProvider、临时目录、会话配置等通用fixtures
- commit-preparation SKILL：commit前检查清单，包括gitignore验证、改动总结、ChangeLog更新与标准格式commit
  - P0（5项）、P1（4项）、P2（3项）优先级划分
  - 3个月roadmap与进度矩阵
- **集成测试框架** (P1-001)：
  - `tests/integration/`：网络弹性、并发会话、故障转移测试
  - `tests/benchmarks/`：性能吞吐量、延迟、可扩展性基准测试
  - `conftest.py`：MockLLMProvider、临时目录、会话配置等通用fixtures
- commit-preparation SKILL：commit前检查清单，包括gitignore验证、改动总结、ChangeLog更新与标准格式commit

### Changed
- **pyproject.toml**: 显式声明可选依赖groups
  - provider：httpx>=0.24.0, tenacity>=8.0.0
  - dev：测试、linting、类型检查工具
  - quality：tiktoken用于精确token估算
- **__init__.py**: 扩展导出至57个项目，分类组织
  - 核心模型(10), 会话管理(7), Provider(3), API函数(13), 日志(2), 异常(18)
- **main.py** (P0-005): 改进CLI错误处理和诊断
  - 添加 --init-config 命令生成默认配置
  - 添加 --check-config 命令进行环境检查
  - 整合日志系统用于审计和调试
  - 改进异常捕获和错误消息详细度
  - 添加 KeyboardInterrupt 处理

### Changed

### Fixed

### Deprecated

---

## [0.1.0] - 2026-04-05

### Added

#### 核心框架
- 多人角色扮演编排引擎（`AsyncRolePlayEngine`）
- 支持"多人用户 + 单AI主助手"交互模式
- 结构化会话与记录系统（`SessionConfig`, `Transcript`）

#### LLM Provider支持
- OpenAI 兼容接口适配（`openai_compatible.py`）
- SiliconFlow 专用适配（`siliconflow.py`，默认基地址 `https://api.siliconflow.cn`）
- 火山方舟 Ark 专用适配（`volcengine_ark.py`，默认基地址 `https://ark.cn-beijing.volces.com/api/v3`）
- Provider 自动路由（按模型前缀匹配）

#### 用户记忆系统（Phase 1）
- 用户档案与运行时状态管理（`UserProfile`, `UserRuntimeState`）
- 结构化记忆事实存储（`MemoryFact`），支持分类、验证、冲突检测
- 事件记忆管理（`EventMemoryManager`），支持事件命中评分
- 用户识别与身份索引（支持跨渠道同人识别）

#### 记忆质量评估与智能遗忘（Phase 2）
- 记忆质量评估模块（`MemoryQualityAssessor`）：
  - 多维度评分：置信度(50%) + 活跃度(30%) + 验证状态(15%)
  - 非线性活跃度评分：按年龄划分(0-7天/7-30天/30-90天/>90天)五等级
  - 用户行为一致性分析：身份/偏好/情感/事件四维度评分
- 智能遗忘引擎（`MemoryForgetEngine`）：
  - 时间衰退表：{7: 0.95, 30: 0.85, 60: 0.70, 90: 0.50, 180: 0.20}
  - 自动清理规则：极低置信+陈旧 / 冲突+低置信+极旧 / 低质量+陈旧
  - 冲突记忆加速衰退（额外乘以0.7）
- CLI工具（`memory_quality_tools.py`）：
  - 子命令：analyze/cleanup/decay/all
  - JSON报告导出与控制台展示
  - 完整argparse集成

#### 编排策略与多模态处理
- 任务级编排系统（`memory_extract`, `event_extract`, `multimodal_parse`, `memory_manager`）
- Token 预算控制与限流裁剪
- 遵循 `OrchestrationPolicy` 配置

#### CLI与API接口
- 脚本式CLI（`sirius-chat` 命令）
- Python 库式接口（`.api` 模块化facade）
- 会话配置加载与持久化（JSON + `JsonSessionStore`）

#### 开发工具与文档
- Framework Quickstart SKILL：快速架构理解
- External Integration SKILL：外部接入指南
- Skill Sync Enforcer SKILL：代码与文档联动检查
- Release Checklist SKILL：发布前检查清单
- Commit Preparation SKILL：commit前检查清单
- 完整架构文档（`docs/architecture.md`）
- 外部使用指南（`docs/external-usage.md`）
- 编排策略详解（`docs/orchestration-policy.md`）

#### 测试覆盖
- 综合单元测试（79个测试用例）
- 记忆质量系统测试（8个新增测试）

### Changed

### Fixed

### Deprecated

---

## 版本优先级

### 发布约定
- 主版本号(Major)：重大架构变更或破坏性API改动
- 次版本号(Minor)：新增功能或向后兼容的改动
- 修订号(Patch)：问题修复与性能优化

### 标签命名
- 格式：`v{Major}.{Minor}.{Patch}`
- 示例：`v0.1.0`, `v0.2.0`, `v1.0.0`

---

## 如何贡献

提交前请：
1. 检查 `.gitignore` 覆盖范围（隐私文件、运行时缓存）
2. 总结改动内容
3. 更新此 CHANGELOG.md（在 `[Unreleased]` 部分记录）
4. 按 Conventional Commits 规范提交（中文信息，包含type和scope）

详见：`.github/skills/commit-preparation/SKILL.md`
