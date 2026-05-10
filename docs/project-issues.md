# Sirius Chat 项目问题跟踪

> **本文档记录对 Sirius Chat v1.1.0 代码库的深度分析中发现的问题、风险与改进建议。**
> 
> 更新日期：2026-05-02
> 分析范围：完整代码库 + 所有文档 + 所有 SKILL

---

## 一、文档与代码脱节（最高优先级）

### 1.1 文档引用不存在的模块/目录

| 问题 | 位置 | 实际情况 | 状态 |
|------|------|---------|------|
| `workspace/` 目录被多处引用 | `docs/architecture.md`、`SKILL/framework-quickstart` | 代码中不存在该目录，功能已迁移至 `sirius_chat/utils/layout.py` | **已修复** — `docs/architecture.md` 中无此引用；SKILL 中无此引用 |
| `cache/` 目录 | `docs/architecture.md` | 代码中不存在 | **已修复** — `docs/architecture.md` 中无此引用 |
| `performance/` 目录 | `docs/architecture.md` | 代码中不存在，但 `tests/benchmarks/` 存在 | **已修复** — `docs/architecture.md` 中无此引用 |
| `api/` 目录 | `docs/architecture.md` | 不存在，公开 API 在 `sirius_chat/__init__.py` 中统一导出 | **已修复** — `docs/architecture.md` 中无此引用 |

**验证命令**：
```bash
# 应返回空（排除 webui.md 中的 REST API 路径和 provider base_url）
grep -rn "workspace/\|cache/\|performance/" docs/ .trae/skills/ | grep -v "webui.md" | grep -v "api/paas" | grep -v "api/v3" | grep -v "api.ytea" | grep -v "project-issues.md"
```

**建议**：
- [x] ~~在 CI 中加入文档校验脚本~~（建议保留，但当前已无此类问题）
- [x] ~~建立"文档变更门控"~~（建议保留作为长期机制）

### 1.2 SKILL 项目结构地图过时

`.trae/skills/code-change-sync/SKILL.md` 中的项目结构地图已更新，补充了以下此前缺失的模块：

- [x] `sirius_chat/background_tasks.py` — 后台任务管理器
- [x] `sirius_chat/mixins.py` — JsonSerializable 混入
- [x] `sirius_chat/developer_profiles.py` — 开发者身份校验
- [x] `sirius_chat/trait_taxonomy.py` — 特征分类体系
- [x] `sirius_chat/exceptions.py` — 自定义异常
- [x] `sirius_chat/logging_config.py` — 日志配置

**状态**：**已修复** — `code-change-sync` SKILL 的项目结构地图已包含上述所有模块。

### 1.3 文档合并后的残留引用

部分文档曾引用已合并/删除的旧文件名，现已全部修复：

- [x] `docs/architecture.md` 第 3 行引用 `docs/full-architecture-flow.md` — **已修复**（链接格式已修正为相对路径）
- [x] `SKILL/framework-quickstart` 阅读顺序中的 `docs/engine-emotional.md`、`docs/memory-system.md`、`docs/skill-system.md` — **已修复**（替换为 `engine-deep-dive.md`、`persistence-system.md`、`skill-guide.md`）
- [x] `SKILL/code-change-sync` 的"文档文件"表格 — **已修复**（更新为合并后的新文档名，新增 `project-issues.md`）
- [x] `SKILL/code-change-sync` 中的 `docs/configuration.md` 引用 — **已修复**（替换为 `docs/configuration-guide.md`）
- [x] `SKILL/code-change-sync` 场景 3 中的 `docs/skill-system.md` 引用 — **已修复**（替换为 `docs/skill-guide.md`）
- [x] `SKILL/external-integration` 阅读顺序中的 `docs/external-usage.md` — **已修复**（替换为 `docs/configuration-guide.md`）

**验证命令**：
```bash
# 应返回空
grep -rn "docs/engine-emotional\|docs/memory-system\|docs/skill-system\|docs/configuration\.md\|docs/session-store\|docs/token-system\|docs/config-system\|docs/orchestration-policy\|docs/emotion-intent-analysis\|docs/skill-authoring\|docs/external-usage" docs/ .trae/skills/
```

### 1.4 文档中仍提及已移除的旧系统

部分文档和 SKILL 仍提及 `AsyncRolePlayEngine`、`WorkspaceRuntime`、`v0.28+`、`v0.27` 等已移除或不再相关的历史系统，造成阅读干扰。

| 文件 | 原内容 | 修复后 |
|------|--------|--------|
| `docs/architecture.md` | `AsyncRolePlayEngine` 与 `WorkspaceRuntime` 已完全移除 | 删除该句，仅保留当前引擎说明 |
| `docs/architecture.md` | `v1.0.0 默认引擎` | `默认引擎` |
| `docs/architecture.md` | `Emotional 路径（v0.28+ 默认）` | `Emotional 路径（默认）` |
| `docs/engine-deep-dive.md` | 替代 legacy `AsyncRolePlayEngine`（已完全移除） | Sirius Chat 的核心对话编排引擎 |
| `docs/persona-system.md` | `v0.28+ 新增` | 删除版本前缀 |
| `docs/persona-system.md` | 行为与 v0.27 一致 | 重写为"默认人格"段落 |
| `docs/skill-guide.md` | `结构化结果通道（v0.27.9）` | `结构化结果通道` |
| `SKILL/external-integration` | `WorkspaceRuntime` 等旧版兼容层已完全移除 | 删除该句 |
| `SKILL/framework-quickstart` | `WorkspaceRuntime` 等旧版兼容层已在 v1.1 彻底移除 | 删除该句 |
| `SKILL/framework-quickstart` | 哪些只是兼容层或历史迁移材料 | 删除该部分 |
| `SKILL/write-tests` | `AsyncRolePlayEngine` 已完全移除 | 删除该句 |

**状态**：**已修复** — 所有 docs/ 和 SKILL/ 中的过时历史系统引用已清理。

**验证命令**：
```bash
# 应返回空（README 中的迁移文档列表除外，属于历史存档）
grep -rn "AsyncRolePlayEngine\|WorkspaceRuntime\|旧版兼容层\|兼容层\|v0\.28+\|v0\.27" docs/ .trae/skills/
```

---

## 二、架构层面的风险

### 2.1 认知层规则引擎命中率缺乏实证

**问题**：文档和代码均宣称规则引擎有"~90% 命中率"，但：
- 没有 A/B 测试数据支撑
- 没有不同场景（游戏群/学习群/工作群）的命中率分布
- 规则集的覆盖度和准确率未量化

**代码位置**：`sirius_chat/core/cognition.py`

**影响**：如果实际命中率只有 60%，LLM fallback 频率会大幅上升，Token 成本优势消失。

**建议**：
- [ ] 添加认知层命中率监控（通过事件总线收集规则命中 vs fallback 的比例）
- [ ] 用真实群聊语料做离线基准测试
- [ ] 在 WebUI 中展示命中率趋势图

### 2.2 记忆系统可扩展性瓶颈

**问题**：
- 基础记忆硬限制 30 条（`BasicMemoryManager`），对于活跃群聊可能不够
- 日记生成是 CPU+IO 密集型操作，虽然放在后台任务，但仍可能占用大量资源
- 没有向量数据库，日记检索依赖关键词+嵌入索引，大量日记后的检索质量未知

**代码位置**：`sirius_chat/memory/basic/`、`sirius_chat/memory/diary/`

**建议**：
- [ ] 基础记忆的 30 条限制改为可配置（`experience.json` 中增加参数）
- [ ] 引入可选的向量数据库（Chroma/Milvus）作为日记索引的后端
- [ ] 增加日记检索质量的自动化测试（用已知答案的查询验证召回率）

### 2.3 多人格子进程资源开销

**问题**：
- 每个人格 = 1 个 Python 进程 + 1 个 NapCat 进程
- 10 个人格 = 20 个进程，每个 Python 进程仍需加载 Pillow 等库
- 没有进程池或共享内存机制

**代码位置**：`sirius_chat/persona_manager.py`

**影响**：在资源受限的服务器上（如 2C4G 云主机），同时运行 3-5 个人格就可能内存不足。

**已解决**：~~每个 Python 进程加载 sentence-transformers 重库~~ → Embedding 模型已迁移至共享 Embedding 微服务（`sirius_chat/embedding/`），由 `PersonaManager` 在主进程启动一次，各子进程通过 `EmbeddingClient` HTTP 调用，不再各自加载模型权重。

**建议**：
- [ ] 测量单人格内存占用 baseline
- [ ] 提供资源限制配置（如 max_memory_per_persona、max_concurrent_personas）

### 2.4 NapCat 平台绑定过深

**问题**：
- 平台适配层几乎完全围绕 NapCat/QQ 设计
- `NapCatBridge`、`NapCatAdapter`、`NapCatManager` 是核心组件
- `setup_wizard.py` 是 QQ 私聊交互式的，不具备通用性
- 如果 NapCat 停止维护或 QQ 协议变更，整个项目受严重影响

**代码位置**：`sirius_chat/platforms/`

**建议**：
- [ ] 抽象 `PlatformBridge` 基类（类似 `BaseBridge` / `BaseAdapter`），将 NapCat specifics 下沉到 `platforms/napcat/`
- [ ] 新增 `platforms/discord/`、`platforms/telegram/` 适配器作为 PoC
- [ ] `setup_wizard` 重构为通用配置向导，支持多平台

---

## 三、测试覆盖盲区

### 3.1 缺少集成测试

**现状**：490+ 个测试用例，但主要是单元测试。

**缺失的关键路径**：
- [ ] `PersonaManager` 的子进程管理（`start_persona` / `stop_persona` / `run_all`）
- [ ] NapCat 桥接层的 WebSocket 交互（可以用 `unittest.mock` Mock WebSocket）
- [ ] 端口分配冲突场景（两个 persona 同时请求端口）
- [ ] 子进程崩溃后的自动重启逻辑

**代码位置**：`tests/integration/` 只有 3 个测试文件

**建议**：
- [ ] 为 `PersonaManager` 写集成测试（Mock `subprocess.Popen`）
- [ ] 为 `NapCatBridge` 写 Mock WebSocket 测试
- [ ] 增加"混沌测试"：模拟子进程异常退出，验证主进程是否能正确清理

### 3.2 性能基准缺失

**问题**：
- `tests/benchmarks/` 存在但内容单薄
- 没有单人格内存占用 baseline
- 没有多人格并发时的资源消耗数据
- 没有认知层规则引擎 vs LLM fallback 的实际比例测量

**建议**：
- [ ] 补充 `tests/benchmarks/test_memory_usage.py` — 测量 1/5/10 个人格的内存占用
- [ ] 补充 `tests/benchmarks/test_cognition_latency.py` — 测量感知/认知/决策/执行各层的延迟
- [ ] 补充 `tests/benchmarks/test_rule_engine_accuracy.py` — 用标注语料验证规则引擎命中率

### 3.3 公开 API 完整性测试不足

**现状**：`tests/test_public_api.py` 只有 4 个测试函数。

**问题**：
- `sirius_chat/__init__.py` 中导出的 20+ 个公开符号未全部测试
- 新增公开接口后容易遗漏测试

**建议**：
- [ ] 用 `inspect` 自动遍历 `__all__` 中的所有符号，验证它们可导入且可实例化
- [ ] 为每个公开类写最小化的"冒烟测试"

---

## 四、配置系统复杂度

### 4.1 配置文件过多

**当前配置分散在**：
- `data/global_config.json` — 全局参数
- `data/personas/{name}/persona.json` — 人格定义
- `data/personas/{name}/orchestration.json` — 模型编排
- `data/personas/{name}/adapters.json` — 平台适配器
- `data/personas/{name}/experience.json` — 体验参数
- `data/providers/provider_keys.json` — Provider 凭证
- `data/adapter_port_registry.json` — 端口分配

**影响**：新用户需要理解 7 个配置文件的概念和关系，学习曲线陡峭。

**建议**：
- [ ] 提供"快速配置模板"：一组预设的 `experience.json`（活泼型/高冷型/知识型）
- [ ] WebUI 增加"配置向导"：引导用户一步步完成首次配置
- [ ] 考虑将人格级配置合并为单个 `persona.yaml`（可选，保持向后兼容）

### 4.2 配置校验不够严格

**问题**：
- 部分配置项缺少范围校验（如 `engagement_sensitivity` 应在 0-1 之间）
- 配置文件损坏时的降级策略不够明确

**建议**：
- [ ] 为所有数值型配置项添加范围校验
- [ ] 配置加载失败时提供清晰的错误信息（指出哪个文件、哪个字段有问题）

---

## 五、代码质量细节

### 5.1 异常处理一致性

**问题**：
- 部分模块使用自定义异常（`sirius_chat/exceptions.py`），部分模块直接使用 `ValueError`/`RuntimeError`
- 异步代码中的异常捕获不够统一

**建议**：
- [ ] 统一异常体系：所有模块使用项目自定义异常
- [ ] 为每个异常类添加错误码，便于日志分析和问题定位

### 5.2 日志规范

**问题**：
- 部分模块使用 `logger = logging.getLogger(__name__)`，部分使用硬编码名称（如 `LOG = logging.getLogger("sirius.persona_manager")`）
- 日志格式未统一配置

**建议**：
- [ ] 统一使用 `logging.getLogger(__name__)`
- [ ] 在 `logging_config.py` 中提供统一的格式和级别配置

### 5.3 类型注解覆盖率

**现状**：整体覆盖率较高，但仍有盲区：
- [ ] `persona_manager.py` 中部分函数缺少返回类型注解
- [ ] `platforms/napcat_manager.py` 中部分内部函数未注解

---

## 六、安全与隐私

### 6.1 Provider 凭证存储

**现状**：Provider API Key 以明文 JSON 存储在 `data/providers/provider_keys.json`。

**风险**：
- 文件权限不当可能导致密钥泄露
- 备份/分享时容易意外包含密钥

**建议**：
- [ ] 支持可选的密钥加密（用系统密钥环或环境变量主密钥）
- [ ] 在 WebUI 中显示密钥时做脱敏处理（只显示前 4 位和后 4 位）

### 6.2 SKILL 安全

**现状**：有 `validate_skill_access` 校验，但：
- 开发者身份验证逻辑不够详细
- 没有 SKILL 调用频率限制
- 没有 SKILL 沙箱（恶意 SKILL 可以访问文件系统）

**建议**：
- [ ] 增加 SKILL 调用频率限制（每分钟最多 N 次）
- [ ] 为文件操作类 SKILL 增加路径白名单校验
- [ ] 考虑引入轻量级沙箱（如限制 SKILL 只能访问 `skill_data/` 目录）

---

## 七、SKILL 系统改进空间

### 7.1 SKILL 发现机制

**现状**：内置 SKILL 在 `skills/builtin/` 中，需要手动注册。

**问题**：
- 新增内置 SKILL 后容易忘记在注册表中添加
- 没有 SKILL 市场/插件生态的概念

**建议**：
- [ ] 实现自动发现：扫描 `skills/builtin/` 目录自动注册
- [ ] 提供 SKILL 元数据规范（名称、版本、作者、依赖、权限）

### 7.2 SKILL 调试困难

**现状**：SKILL 执行失败时，错误信息分散在日志中。

**建议**：
- [ ] 在 WebUI 中增加 SKILL 执行历史页面（展示每次调用的参数、结果、耗时、错误）
- [ ] 为 SKILL 开发提供本地调试模式（绕过权限校验、增加详细日志）

---

## 八、后续推进路线图

### 短期（1-2 个月）— 夯实基础

| 优先级 | 任务 | 负责人 | 验收标准 |
|--------|------|--------|---------|
| P0 | 修复所有文档与代码的脱节问题 | 维护者 | `grep -r "workspace/\|cache/\|performance/" docs/ .trae/skills/` 返回空 |
| P0 | 建立 CI 文档校验脚本 | 维护者 | PR 中若文档引用不存在的文件，CI 失败 |
| P1 | 补充 `PersonaManager` 集成测试 | 开发者 | 覆盖 start/stop/run_all 主路径 |
| P1 | 测量单人格内存占用 baseline | 开发者 | 输出 `docs/benchmarks/memory-baseline.md` |
| P1 | 统一异常体系 | 开发者 | 所有模块使用 `sirius_chat/exceptions.py` 中的异常 |

### 中期（3-6 个月）— 扩展能力

| 优先级 | 任务 | 负责人 | 验收标准 |
|--------|------|--------|---------|
| P1 | 抽象 PlatformBridge 基类 | 架构师 | 新增 Discord 适配器 < 200 行代码 |
| P1 | 引入可选向量数据库 | 开发者 | 日记检索召回率 > 80%（用测试语料验证）|
| P2 | 认知层命中率监控 | 开发者 | WebUI 展示实时命中率趋势图 |
| P2 | 配置系统简化 | 产品经理 | 新用户首次配置时间 < 10 分钟 |
| P2 | Provider 密钥加密 | 安全负责人 | 支持系统密钥环存储 |

### 长期（6-12 个月）— 生态建设

| 优先级 | 任务 | 负责人 | 验收标准 |
|--------|------|--------|---------|
| P2 | 人格模板市场 | 产品经理 | 提供 10+ 预置人格模板 |
| P2 | SKILL 市场/插件生态 | 架构师 | 支持第三方 SKILL 安装 |
| P3 | 多模态能力增强 | 开发者 | 支持图片理解、表情包RAG系统 ✅ 已完成 |
| P3 | 商业化 SaaS 化 | 产品经理 | 提供托管多人格服务 |

---

## 九、附录：快速检查清单

在每次发布前，检查以下项目：

- [ ] 所有文档中引用的文件路径真实存在
- [ ] 所有 SKILL 中的项目结构地图与代码一致
- [ ] `pytest -q` 全部通过
- [ ] `python main.py --help` 可正常执行
- [ ] 新增公开 API 已在 `sirius_chat/__init__.py` 中导出
- [ ] 新增公开 API 已有对应测试
- [ ] 配置文件变更已同步到 `docs/configuration-guide.md`
- [ ] 架构变更已同步到 `docs/architecture.md` 和 `docs/full-architecture-flow.md`
