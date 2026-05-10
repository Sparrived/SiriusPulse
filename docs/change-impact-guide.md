# Sirius Chat 变更联动确认指南

> **目标**：当修改后端代码、配置结构或数据契约时，快速定位需要同步检查的前端页面、API、文档和测试位置。  
> **使用方式**：按变更类型找到对应章节，逐项勾选确认清单。

---

## 目录

1. [按变更类型速查](#1-按变更类型速查)
2. [后端 API 变更 → 前端联动](#2-后端-api-变更--前端联动)
3. [配置/数据契约变更 → 联动范围](#3-配置数据契约变更--联动范围)
4. [Provider 变更 → 联动范围](#4-provider-变更--联动范围)
5. [人格系统变更 → 联动范围](#5-人格系统变更--联动范围)
6. [记忆系统变更 → 联动范围](#6-记忆系统变更--联动范围)
7. [Skill 系统变更 → 联动范围](#7-skill-系统变更--联动范围)
8. [WebUI 前端独立变更 → 后端联动](#8-webui-前端独立变更--后端联动)
9. [通用检查清单](#9-通用检查清单)

---

## 1. 按变更类型速查

| 你修改了什么 | 首先看这里 | 然后检查 |
|-------------|-----------|---------|
| `sirius_chat/webui/*.py` 新增/删除/修改 API 路由或返回字段 | [章节 2](#2-后端-api-变更--前端联动) | `core.js`, `config.js`, 对应 `.html` 页面 |
| `sirius_chat/persona_config.py` 配置字段增删改 | [章节 3](#3-配置数据契约变更--联动范围) | `persona_api.py`, `config.js`, `docs/configuration-guide.md`, `docs/persona-lifecycle.md` |
| `sirius_chat/config/models.py` 数据契约变更 | [章节 3](#3-配置数据契约变更--联动范围) | `sirius_chat/__init__.py`, `docs/architecture.md`, 所有引用该契约的 API |
| `sirius_chat/models/models.py` Message/Participant/Transcript 变更 | [章节 3](#3-配置数据契约变更--联动范围) | 所有序列化/反序列化代码、session store、前端使用字段 |
| `sirius_chat/providers/` 新增/修改 Provider | [章节 4](#4-provider-变更--联动范围) | `config.js` (PROVIDER_TYPE_OPTIONS), `docs/provider-system.md`, `routing.py` |
| `sirius_chat/core/emotional_engine.py` 引擎行为变更 | [章节 5](#5-人格系统变更--联动范围) | `docs/engine-deep-dive.md`, `docs/architecture.md`, 体验参数默认值 |
| `sirius_chat/core/prompt_factory.py` Prompt 构建变更 | [章节 5](#5-人格系统变更--联动范围) | `docs/engine-deep-dive.md`, `docs/architecture.md`, `bg_tasks.py`（延迟/主动 prompt） |
| `sirius_chat/embedding/` Embedding 服务变更 | [章节 6](#6-记忆系统变更--联动范围) | `persona_manager.py`（服务启动）, `diary/indexer.py`, `skills/sticker/indexer.py`, `sticker/learner.py`, `docs/persistence-system.md` |
| `sirius_chat/persona_generation/` 人格生成变更 | [章节 5](#5-人格系统变更--联动范围) | `docs/persona-lifecycle.md`, `sirius_chat/__init__.py`（公开 API 导出） |
| `sirius_chat/memory/` 记忆系统变更 | [章节 6](#6-记忆系统变更--联动范围) | `docs/persistence-system.md`, `memory_api.py`, 前端对应面板 |
| `sirius_chat/skills/` Skill 增删改 | [章节 7](#7-skill-系统变更--联动范围) | `docs/skill-guide.md`, `server_skill_api.py`, `skills.html` |
| `sirius_chat/webui/static/` 前端页面/JS 变更 | [章节 8](#8-webui-前端独立变更--后端联动) | 对应后端 API 是否仍返回所需字段 |
| `pyproject.toml` 依赖/版本/入口变更 | [章节 9](#9-通用检查清单) | `README.md`, CI 配置, `docs/README.md` |

---

## 2. 后端 API 变更 → 前端联动

### 2.1 路由变更（新增/删除/修改 URL）

**触发条件**：修改 `server_core.py` 中的 `_setup_routes()` 方法。

**必须检查**：
- [ ] `sirius_chat/webui/server.py` 中是否已绑定对应的代理方法（若 handler 拆分到独立模块）
- [ ] 前端 `core.js` 中的 `get()` / `post()` 调用路径是否同步
- [ ] 前端对应 `.html` 页面中的 `onclick` 或事件绑定是否指向正确路径
- [ ] `docs/webui.md` 中的「API 路由总览」表格是否更新

**示例**：
```python
# server_core.py 新增路由
self.app.router.add_get("/api/new-feature", self.api_new_feature)

# 前端 core.js 需同步
const res = await get('/new-feature');
```

### 2.2 响应字段变更（增删改 JSON 字段）

**触发条件**：修改 API handler 的返回字典结构。

**必须检查**：
- [ ] 前端 `core.js` / `config.js` / `analytics.js` / `platform.js` 中解析该响应的代码
- [ ] 前端对应 `.html` 页面中通过 `$('id')` 回填的字段 ID 是否匹配
- [ ] 若字段删除，前端是否仍有残留引用（会导致 `undefined` 或 UI 空白）
- [ ] 若字段重命名，前后端必须同时改，避免半同步状态

**高频联动点**：

| API | 前端消费位置 | 说明 |
|-----|------------|------|
| `GET /api/personas` | `core.js:loadPersonas()`, `renderPersonaCards()` | `personas` 数组字段 |
| `GET /api/personas/{name}` | `core.js:loadPersonaStatus()` | `status` 对象字段 |
| `GET /api/personas/{name}/persona` | `config.js:loadPersonaPreview()` | `persona` 对象字段 |
| `GET /api/personas/{name}/experience` | `config.js:loadExperience()` | `experience` 对象字段 |
| `GET /api/personas/{name}/orchestration` | `config.js:loadOrchestration()` | `model_choices`, `task_models` 等 |
| `GET /api/personas/{name}/adapters` | `config.js:loadAdapters()` | `adapters` 数组字段 |
| `GET /api/providers` | `config.js:loadProviders()` | `providers` 数组字段 |
| `GET /api/models` | `config.js:loadAvailableModels()` | `model_choices` 数组 |
| `GET /api/tokens` | `core.js:loadTokenStats()` | `summary` 对象字段 |
| `GET /api/telemetry` | `core.js:loadTelemetry()` | `skills`, `total_calls` 字段 |
| `GET /api/personas/{name}/cognition` | `analytics.js` (cognition.html) | 认知事件字段 |
| `GET /api/personas/{name}/users` | `analytics.js` (users.html) | 用户画像字段 |
| `GET /api/napcat/status` | `config.js:ncLoadStatus()` | `installed`, `running`, `qq_installed` |

### 2.3 请求体字段变更

**触发条件**：修改 API handler 中 `await request.json()` 后读取的字段。

**必须检查**：
- [ ] 前端 `post()` 调用处传入的 body 字段名是否一致
- [ ] 前端表单/输入框的 ID 是否与 body 字段名对应
- [ ] 必填字段缺失时后端是否有合理默认值或报错提示

---

## 3. 配置/数据契约变更 → 联动范围

### 3.1 `PersonaExperienceConfig` 字段变更

**定义位置**：`sirius_chat/persona_config.py`

**联动链**：
```
persona_config.py (字段定义)
    ├── persona_api.py:api_experience_get()   → 返回字段列表
    ├── persona_api.py:api_experience_post()  → 接收字段列表
    ├── config.js:loadExperience()            → 读取并回填表单
    ├── config.js:saveExperience()            → 收集并发送字段
    ├── docs/configuration-guide.md           → 字段说明表格
    └── docs/persona-lifecycle.md             → 体验参数章节
```

**必须检查**：
- [ ] `persona_config.py` 中 `to_dict()` / `from_dict()` 是否包含新字段
- [ ] `persona_api.py` 的 `api_experience_get/post` 的字段白名单是否同步
- [ ] `config.js` 的 `loadExperience()` 是否有对应 `$('expXxx').value = e.xxx`
- [ ] `config.js` 的 `saveExperience()` 的 payload 是否包含新字段
- [ ] `docs/configuration-guide.md` 第 5.4/5.5 节是否更新
- [ ] `docs/persona-lifecycle.md` 的 `PersonaExperienceConfig` 表格是否更新

### 3.2 `NapCatAdapterConfig` 字段变更

**定义位置**：`sirius_chat/persona_config.py`

**联动链**：
```
persona_config.py:NapCatAdapterConfig
    ├── persona_api.py:api_adapters_get/post()
    ├── persona_api.py:api_config_post()      → 桥接配置更新
    ├── config.js:loadAdapters()              → 表单回填
    ├── config.js:saveAdapters()              → 表单收集
    └── docs/configuration-guide.md           → adapters.json 示例
```

### 3.3 `OrchestrationPolicy` / `SessionConfig` 字段变更

**定义位置**：`sirius_chat/config/models.py`

**联动链**：
```
config/models.py
    ├── sirius_chat/__init__.py               → 是否需导出新符号
    ├── sirius_chat/core/orchestration_store.py → 持久化字段
    ├── persona_api.py:api_orchestration_get/post() → API 字段
    ├── config.js:loadOrchestration()         → 前端读取
    ├── config.js:saveOrchestration()         → 前端发送
    ├── docs/configuration-guide.md           → 配置模型详解
    └── docs/architecture.md                  → 架构契约说明
```

### 3.4 `Message` / `Participant` / `Transcript` 字段变更

**定义位置**：`sirius_chat/models/models.py`

**联动链**：
```
models/models.py
    ├── 所有序列化/反序列化代码 (to_dict/from_dict)
    ├── session/store.py                      → SessionStore 读写
    ├── memory/ 各模块                        → 消息引用字段
    ├── core/ 引擎各组件                      → 消息处理逻辑
    └── 前端若直接消费消息结构                → 对应解析代码
```

**注意**：此类变更影响面极广，建议先全局搜索字段名，确认所有引用点。

---

## 4. Provider 变更 → 联动范围

### 4.1 新增 Provider 类型

**触发条件**：在 `sirius_chat/providers/` 新增 Provider 实现。

**联动链**：
```
providers/new_provider.py
    ├── providers/__init__.py                 → 导出
    ├── providers/routing.py                  → 注册路由逻辑
    ├── sirius_chat/__init__.py               → 公开 API 导出
    ├── webui/server_core.py                  → 若需特殊处理
    ├── config.js                             → PROVIDER_TYPE_OPTIONS, PROVIDER_DEFAULT_URLS, BUILTIN_PROVIDER_TYPES
    ├── docs/provider-system.md               → 新增 Provider 说明
    └── docs/architecture.md                  → Provider 模块边界
```

**必须检查**：
- [ ] `config.js` 三处常量是否同步（类型选项、默认 URL、内置类型列表）
- [ ] 前端 Provider 编辑表单是否能正确渲染新类型的字段
- [ ] 模型列表 API (`/api/models`) 是否能正确返回新 Provider 的模型

### 4.2 Provider 配置结构变更

**触发条件**：修改 `provider_keys.json` 存储结构或 Provider dataclass 字段。

**必须检查**：
- [ ] `server_core.py:api_providers_get/post()` 的脱敏/保存逻辑
- [ ] `config.js` 中 Provider 的编辑/保存/渲染逻辑
- [ ] `WorkspaceProviderManager` 的加载逻辑

---

## 5. 人格系统变更 → 联动范围

### 5.1 `PersonaProfile` 字段变更

**定义位置**：`sirius_chat/models/persona.py`

**联动链**：
```
models/persona.py:PersonaProfile
    ├── core/persona_store.py                 → 持久化
    ├── persona_api.py:api_persona_get/post() → API 字段白名单
    ├── config.js:loadPersonaPreview()        → 表单回填
    ├── config.js:savePersonaForm()           → 表单收集
    ├── docs/configuration-guide.md           → persona.json 示例
    └── docs/persona-lifecycle.md             → 人格定义章节
```

### 5.2 人格创建/删除/迁移逻辑变更

**触发条件**：修改 `persona_manager.py` 或 `persona_worker.py`。

**必须检查**：
- [ ] `persona_api.py` 的对应 API 是否委托正确
- [ ] 前端人格列表/卡片状态显示是否正常
- [ ] `docs/persona-lifecycle.md` 的生命周期描述

---

## 6. 记忆系统变更 → 联动范围

### 6.1 记忆配置参数变更

**触发条件**：修改 `PersonaExperienceConfig` 中记忆相关字段，或 `OrchestrationPolicy` 中记忆策略。

**联动链**：
```
experience.json 字段 / OrchestrationPolicy.memory
    ├── persona_api.py                        → API 返回
    ├── config.js:loadExperience()            → 前端回填
    ├── memory/ 各子模块                      → 运行时读取
    ├── docs/persistence-system.md            → 持久化系统文档
    └── docs/engine-deep-dive.md              → 引擎深度解析
```

### 6.2 记忆 API 变更

**触发条件**：修改 `memory_api.py` 中的路由或返回结构。

**必须检查**：
- [ ] 前端对应分析面板（`cognition.html`, `users.html`, `diary.html`）
- [ ] `analytics.js` 或对应页面的数据解析代码
- [ ] `docs/webui.md` 中 API 路由总览

---

## 7. Skill 系统变更 → 联动范围

### 7.1 新增/删除内置 Skill

**触发条件**：修改 `sirius_chat/skills/builtin/` 目录。

**联动链**：
```
skills/builtin/new_skill.py
    ├── skills/builtin/__init__.py            → 注册（若需要）
    ├── skills/registry.py                    → 自动发现逻辑
    ├── server_skill_api.py                   → API 暴露
    ├── docs/skill-guide.md                   → Skill 说明
    └── 前端 skills.html                      → 列表渲染
```

### 7.2 Skill 配置结构变更

**触发条件**：修改 `SkillDefinition` 或 `SkillParameter` 数据模型。

**必须检查**：
- [ ] `server_skill_api.py` 的 config get/post 逻辑
- [ ] 前端 `skills.html` / `config.js:openSkillConfig()` 的表单渲染
- [ ] `docs/skill-guide.md` 的数据模型说明

---

## 8. WebUI 前端独立变更 → 后端联动

### 8.1 新增前端页面

**触发条件**：在 `static/pages/` 新增 `.html` 文件。

**必须检查**：
- [ ] `core.js:pageTitles` 中是否添加页面标题映射
- [ ] `core.js:navTo()` 中是否添加页面加载逻辑
- [ ] 是否需要新增后端 API 支撑（避免前端空转）
- [ ] `index.html` 侧边栏导航是否添加入口

### 8.2 前端字段 ID 变更

**触发条件**：修改 `.html` 表单元素的 `id` 属性。

**必须检查**：
- [ ] `config.js` / `core.js` / `analytics.js` 中对应的 `$('oldId')` 引用
- [ ] `saveXxx()` 函数中收集表单值的逻辑
- [ ] `loadXxx()` 函数中回填表单值的逻辑

### 8.3 前端图表/可视化变更

**触发条件**：修改 ECharts 相关配置或数据解析。

**必须检查**：
- [ ] 后端 API 返回的数据结构是否满足图表需求
- [ ] `renderXxx()` 函数中的字段映射是否同步

---

## 9. 通用检查清单

每次提交前，无论变更类型，都建议执行以下检查：

- [ ] **代码侧**：`python main.py --help` 可正常执行
- [ ] **代码侧**：若修改了 API，用浏览器或 curl 快速验证一次请求/响应
- [ ] **前端侧**：刷新 WebUI，确认修改的页面无 JS 报错（F12 Console）
- [ ] **文档侧**：若变更影响用户可见行为，`docs/` 中至少一处文档已同步
- [ ] **导出侧**：若新增公开类/函数，已加入 `sirius_chat/__init__.py` 的 `__all__`
- [ ] **测试侧**：若新增逻辑，已有对应测试覆盖（或至少未破坏现有测试）
- [ ] **配置侧**：若新增配置字段，已确认旧配置的兼容性（默认值/迁移逻辑）

---

## 附录：快速定位工具

```bash
# 1. 查找某个字段在前后端的所有引用
grep -r "field_name" sirius_chat/webui/ sirius_chat/persona_config.py sirius_chat/config/models.py sirius_chat/models/models.py

# 2. 查找某个 API 路径的所有引用
grep -r "/api/path" sirius_chat/webui/static/

# 3. 查找前端某个函数的所有调用
grep -r "functionName" sirius_chat/webui/static/*.js

# 4. 确认 docs 中是否提及某个模块
grep -r "module_name" docs/
```
