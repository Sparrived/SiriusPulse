# 配置指南

> **从全局配置到人格级配置，完整覆盖 Sirius Pulse 的所有配置选项。**
>
> 本文档合并了原 `config-system.md`（配置系统实现）、`configuration.md`（用户配置指南）和 `orchestration-policy.md`（模型编排），提供从原理到实践的一站式参考。

---

## 第一章：配置系统架构

### 1.1 为什么需要统一配置层

配置来源多样：JSON 文件、环境变量、代码覆盖、用户 WebUI 编辑。没有统一配置层会导致：
- 同一字段在不同模块中有不同的默认值
- 缺少字段时崩溃而不是优雅降级
- 旧版字段改名后无法兼容
- 用户手动编辑 JSON 时无法理解字段含义

### 1.2 架构总览

```
JSON/JSONC 文件（磁盘）
    │
    ├── config/jsonc.py ── 解析带注释的 JSONC / 生成带注释的 JSONC
    │
    └── config/manager.py ── 加载、验证、合并、迁移
            │
            ├── config/models.py ── 输出类型化 dataclass
            │
            └── config/helpers.py ── 便捷修改/构建函数

环境变量 ──► config/manager.py（${VAR} 替换）
```

### 1.3 核心模块

| 模块 | 职责 | 关键产出 |
|------|------|---------|
| `config/models.py` | 配置契约定义 | `SessionConfig`、`OrchestrationPolicy`、`Agent` 等 dataclass |
| `config/manager.py` | 配置加载与合并 | `load_from_json()`、`build_session_config()` |
| `config/helpers.py` | 便捷构建器 | `configure_orchestration_models()`、`setup_multimodel_config()` |
| `config/jsonc.py` | JSON-with-Comments 解析 | `strip_json_comments()`、`render_session_config_jsonc()` |

---

## 第二章：配置模型详解

### 2.1 OrchestrationPolicy（最重的配置类）

引擎运行时最核心的配置对象：

| 字段 | 说明 |
|------|------|
| `unified_model` | 统一模型模式：所有任务用同一个模型 |
| `task_models` | 按任务映射模型：`{"response_generate": "gpt-4o", ...}` |
| `task_temperatures` | 按任务映射 temperature |
| `task_max_tokens` | 按任务映射 max_tokens |
| `task_retries` | 按任务映射重试次数 |
| `task_enabled` | 按任务开关：`{"cognition_analyze": true, ...}` |
| `engagement_sensitivity` | 回复敏感度（0~1） |
| `reply_frequency` | 回复频率限制 |
| `enable_skills` / `max_skill_rounds` / `skill_execution_timeout` | SKILL 系统控制 |

**验证规则**：
- 不能同时设置 `unified_model` 和 `task_models`
- 数值字段必须在合理范围内

### 2.2 其他核心配置类

| 类 | 用途 | 关键字段 |
|----|------|---------|
| `Agent` | AI agent 定义 | `name`, `persona`, `model`, `temperature`, `max_tokens` |
| `AgentPreset` | Agent + 全局系统提示词 | `agent`, `global_system_prompt` |
| `SessionDefaults` | 会话级默认 | `max_history`, `enable_compression` |
| `MemoryPolicy` | 记忆策略 | `fact_limit`, `confidence_threshold`, `decay_schedule` |
| `TokenUsageRecord` | 单次 LLM 调用记录 | `prompt_tokens`, `completion_tokens`, `task_name`, `model` |
| `WorkspaceConfig` | Workspace 级清单 | `layout_version`, `active_agent_key`, `session_defaults` |
| `SessionConfig` | **运行时配置** | `work_path`, `data_path`, `preset`, `orchestration` |

---

## 第三章：配置加载与合并

### 3.1 加载优先级

```
1. workspace.json（机器可读 manifest）
2. config/session_config.json（人工维护快照）→ 对 session_defaults 和 orchestration 更高优先级
3. 代码传入的 overrides
4. 环境变量 ${VAR} 替换（在 parse 阶段完成）
```

### 3.2 兼容迁移

`build_session_config` 内部自动处理旧字段到新字段的映射：

| 旧字段 | 新字段 |
|--------|--------|
| `message_debounce_seconds` | `pending_message_threshold`（四舍五入） |

### 3.3 不可变更新

所有 helper 函数遵循**不可变**原则：返回新的 `SessionConfig`/`OrchestrationPolicy` 实例，不修改原对象。

---

## 第四章：全局配置

路径：`data/global_config.json`

```json
{
  "webui_host": "0.0.0.0",
  "webui_port": 8080,
  "auto_manage_napcat": true,
  "napcat_install_dir": "D:\\Code\\sirius_pulse\\napcat",
  "napcat_base_port": 3001,
  "log_level": "INFO",
  "setup_completed": false,
  "setup_wizard_running": false
}
```

| 字段 | 类型 | 默认值 | 含义 |
|------|------|--------|------|
| `webui_host` | string | `"0.0.0.0"` | WebUI 监听地址 |
| `webui_port` | int | `8080` | WebUI 监听端口 |
| `auto_manage_napcat` | bool | `false` | 是否自动管理 NapCat 安装/启动 |
| `napcat_install_dir` | string | `"napcat"` | NapCat 全局安装目录 |
| `napcat_base_port` | int | `3001` | NapCat WebSocket 起始端口 |
| `log_level` | string | `"INFO"` | 日志级别 |
| `setup_completed` | bool | `false` | 首次配置向导是否完成 |
| `setup_wizard_running` | bool | `false` | 配置向导是否正在运行 |

---

## 第五章：人格级配置

路径：`data/personas/{name}/`

### 5.1 人格定义（`persona.json`）

```json
{
  "name": "月白",
  "aliases": ["Sirius"],
  "persona_summary": "一位由AI猫娘构成的温暖群友...",
  "personality_traits": ["温暖治愈", "聪慧灵动"],
  "communication_style": "发言节奏适中...",
  "catchphrases": ["喵~", "大家要好好相处呀喵"],
  "emoji_preference": "heavy",
  "humor_style": "wholesome",
  "emotional_baseline": { "valence": 0.6, "arousal": 0.4 },
  "empathy_style": "warm",
  "boundaries": ["拒绝嘲讽亲友和家人"],
  "taboo_topics": ["辱骂家人", "恶意攻击"],
  "social_role": "caregiver"
}
```

### 5.2 模型编排（`orchestration.json`）

```json
{
  "analysis_model": "qwen3.5-flash",
  "chat_model": "qwen3.5-plus",
  "vision_model": "qwen3.5-plus"
}
```

模型只能从已配置的 Provider 的 `models` 列表中选择。

**内置默认任务-模型映射**：

| 任务类型 | 默认模型 | 用途 |
|---------|---------|------|
| `response_generate` | `chat_model` | 回复生成 |
| `proactive_generate` | `chat_model` | 主动发言生成 |
| `cognition_analyze` | `analysis_model` | 统一情绪+意图分析 |
| `memory_extract` | `analysis_model` | 日记/记忆提取 |
| `vision` | `vision_model` | 多模态视觉任务 |

### 5.3 平台适配器（`adapters.json`）

```json
{
  "adapters": [
    {
      "type": "napcat",
      "enabled": true,
      "ws_url": "ws://localhost:3001",
      "token": "napcat_ws",
      "qq_number": "123456789",
      "allowed_group_ids": ["728196560"],
      "allowed_private_user_ids": [],
      "enable_group_chat": true,
      "enable_private_chat": true
    }
  ]
}
```

### 5.4 体验参数（`experience.json`）

```json
{
  "reply_mode": "auto",
  "engagement_sensitivity": 0.5,
  "expressiveness": 0.5,
  "heat_window_seconds": 60.0,
  "proactive_enabled": true,
  "proactive_interval_seconds": 300.0,
  "delay_reply_enabled": true,
  "pending_message_threshold": 4.0,
  "min_reply_interval_seconds": 0.0,
  "reply_frequency_window_seconds": 60.0,
  "reply_frequency_max_replies": 8,
  "reply_frequency_exempt_on_mention": true,
  "max_concurrent_llm_calls": 1,
  "enable_skills": true,
  "max_skill_rounds": 3,
  "skill_execution_timeout": 30.0,
  "auto_install_skill_deps": true,
  "memory_depth": "deep"
}
```

### 5.5 体验参数详解

#### 记忆系统

| 字段 | 类型 | 默认值 | 含义 |
|------|------|--------|------|
| `basic_memory_hard_limit` | int | 30 | 基础记忆窗口硬上限（条数），超过后旧消息进入归档 |
| `basic_memory_context_window` | int | 5 | 构建 LLM 上下文时保留的最近消息条数 |
| `diary_top_k` | int | 5 | 回复生成时检索的日记条目数量 |
| `diary_token_budget` | int | 800 | 注入系统提示词的日记内容 token 预算（约 1200 字符） |

#### 行为控制

| 字段 | 类型 | 默认值 | 含义 |
|------|------|--------|------|
| `engagement_sensitivity` | float | 0.5 | 参与敏感度（0.0~1.0），越高越容易回复。调的是决策基线活跃度 |
| `expressiveness` | float | 0.5 | 活泼度（0.0~1.0），控制行为边界宽松度。越高越敢抢话、冷却越短、门槛越低 |
| `reply_cooldown_seconds` | int | 12 | 同群连续回复的最小冷却间隔 |
| `max_skill_rounds` | int | 3 | 单次消息处理中最大 SKILL 调用轮数 |

#### 后台任务

| 字段 | 类型 | 默认值 | 含义 |
|------|------|--------|------|
| `delayed_queue_tick_interval_seconds` | int | 10 | 延迟回复队列扫描间隔 |
| `proactive_check_interval_seconds` | int | 60 | 主动发言触发器检查间隔 |
| `proactive_silence_minutes` | int | 60 | 群聊沉默多久后可能触发主动发言 |
| `proactive_active_start_hour` | int | 12 | 主动发言允许的开始小时（24 小时制） |
| `proactive_active_end_hour` | int | 21 | 主动发言允许的结束小时（24 小时制） |
| `memory_promote_interval_seconds` | int | 300 | 日记生成器检查间隔：冷群基础记忆归档 → 日记 |

---

## 第六章：模型编排与任务路由

### 6.1 运行时动态选择

`ModelRouter` 在默认映射基础上，根据以下规则动态调整：

1. **紧急度升级**：`urgency >= 80` 时切换更强模型；`urgency >= 95` 时提升最大 token
2. **热度适配**：群聊 `hot` 时减少 30% token；`overheated` 时减半 token
3. **用户风格**：`concise` 用户限制 80 token；`detailed` 用户增加 20%

这些调整在 `task_model_overrides` 之后应用，因此最终参数 = 覆盖值 + 动态调整。

### 6.2 通过配置覆盖

在 `experience.json` 的 `task_model_overrides` 中覆盖：

```json
{
  "task_model_overrides": {
    "response_generate": {
      "model": "gpt-4o",
      "max_tokens": 512,
      "temperature": 0.7
    },
    "cognition_analyze": {
      "model": "gpt-4o-mini",
      "max_tokens": 384,
      "temperature": 0.2
    }
  }
}
```

每个覆盖项可包含：
- `model`（必需）：模型名称
- `max_tokens`（可选）：该任务的最大输出 token
- `temperature`（可选）：该任务的采样温度

### 6.3 持久化

`orchestration.json` 位于 `{work_path}/engine_state/orchestration.json`，可通过 `OrchestrationStore` 读写：

```python
from sirius_pulse.core.orchestration_store import OrchestrationStore

OrchestrationStore.save(work_path, {
    "analysis_model": "gpt-4o-mini",
    "chat_model": "gpt-4o",
    "vision_model": "gpt-4o",
})
```

---

## 第七章：环境变量替换

ConfigManager 支持 `${VAR_NAME}` 形式的环境变量替换。

示例：

```json
{
  "providers": [
    {
      "type": "openai-compatible",
      "base_url": "https://api.openai.com",
      "api_key": "${OPENAI_API_KEY}"
    }
  ]
}
```

- 未定义的环境变量会保留原占位符
- 轻量会话配置在 bootstrap 时同样支持这套替换逻辑

---

## 第八章：JSONC 支持

### 8.1 为什么需要 JSONC

让人类能写带注释的配置文件，同时让标准 `json` 模块能解析。

### 8.2 核心函数

| 函数 | 说明 |
|------|------|
| `strip_json_comments(content)` | 状态机解析器：移除 `//` 行注释和 `/* */` 块注释，同时保留字符串内的注释符号 |
| `loads_json_document(content)` | 解析 JSONC 字符串为 Python 对象 |
| `load_json_document(path)` | 从文件加载 JSONC |
| `render_session_config_jsonc(payload)` | 将 dict 渲染为带中文注释的 JSONC 字符串 |
| `write_session_config_jsonc(path, payload)` | 原子写入带注释的配置 |

### 8.3 注释渲染示例

```jsonc
{
  // 模型编排策略
  "orchestration": {
    // 统一模型（若设置则所有任务使用同一模型）
    "unified_model": "",
    // 按任务分配模型
    "task_models": {
      // 回复生成模型
      "response_generate": "gpt-4o"
    }
  }
}
```

---

## 第九章：使用方式

### 9.1 快速启动引擎

```python
from sirius_pulse import create_emotional_engine

engine = create_emotional_engine(
    work_path="/path/to/workspace",
    provider=provider,
    persona="warm_friend",
    config={
        "sensitivity": 0.6,
        "proactive_silence_minutes": 20,
    },
)
engine.start_background_tasks()
```

### 9.2 使用 Helpers 构建配置

```python
from sirius_pulse.config.helpers import configure_orchestration_models
from sirius_pulse import SessionConfig

config = SessionConfig(work_path="/path/to/workspace")
config = configure_orchestration_models(
    config,
    response_generate="gpt-4o",
    cognition_analyze="gpt-4o-mini",
)
```

---

## 第十章：人格级产物位置

| 路径 | 说明 |
| --- | --- |
| `persona.json` | 人格定义 |
| `orchestration.json` | 模型编排 |
| `adapters.json` | 平台适配器配置 |
| `experience.json` | 体验参数 |
| `engine_state/` | 引擎运行态持久化 |
| `memory/basic/` | 基础记忆归档存储 |
| `memory/diary/` | 日记条目与索引 |
| `memory/glossary/` | AI 名词解释库 |
| `skill_data/` | SKILL 数据存储 |
| `logs/` | 子进程日志 |

---

## 第十一章：最佳实践与故障排查

### 最佳实践

1. 直接使用 Emotional Engine 配置，无需关注 `orchestration` 等旧字段。
2. 需要注释时直接使用 JSONC，不必更换扩展名。
3. `persona` 字段优先使用模板名；复杂人格通过 roleplay 资产 + `"generated"` 加载。
4. 全局 provider 配置位于 `data/providers/provider_keys.json`，所有人格共用。
5. 日记检索质量取决于 Embedding 微服务。Embedding 服务由 `PersonaManager` 在主进程启动（`sirius_pulse/embedding/server.py`），各人格子进程通过 `EmbeddingClient` HTTP 调用。服务不可用时引擎启动会直接失败（强依赖）。可使用 `python -m sirius_pulse.embedding` 单独测试 Embedding 服务。

### 故障排查

**修改配置文件后没有立即生效**：
1. 检查文件内容仍是合法 JSON
2. Provider 配置位于 `data/providers/provider_keys.json`
3. 人格级配置修改后需重启对应人格子进程
