<h1 align="center">🌟 Sirius Chat 🌟</h1>

<div align="center">

<a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12%2B-blue?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License"></a>
<a href="https://pypi.org/project/sirius-chat/"><img src="https://img.shields.io/badge/PyPI-sirius--chat-blueviolet?style=flat-square" alt="PyPI"></a>
<a href="#-测试"><img src="https://img.shields.io/badge/Tests-600%2B%20passing-brightgreen?style=flat-square" alt="Tests"></a>
<a href="sirius_chat/async_engine/"><img src="https://img.shields.io/badge/Async-First-orange?style=flat-square" alt="Async First"></a>

<em>✨ 月白亲手写的 README，请多关照喵～(ฅ´ω`ฅ)</em>
<br>
<em>一个让 AI 角色在群里活起来的异步角色扮演框架～支持多人格、多平台、多模型，每个人格都有自己的小世界喵！</em>

<a href="#-文档">📚 文档</a> · <a href="#-快速开始">🚀 快速开始</a> · <a href="#使用示例">💡 示例</a> · <a href="#-配置示例">🛠️ 配置</a> · <a href="#-贡献">🤝 贡献</a>

</div>

---

## 📋 目录

- [这是什么呀？](#-这是什么呀)
- [核心特性](#-核心特性)
- [快速开始](#-快速开始)
- [项目结构](#-项目结构)
- [使用示例](#使用示例)
- [配置指南](#-配置示例)
- [文档](#-文档)
- [测试](#-测试)
- [最新变更](#-最新变更)
- [贡献](#-贡献)

---

## 🎯 这是什么呀？

> 月白来介绍喵～(๑˃̵ᴗ˂̵)و

**Sirius Chat** 是一个**异步角色扮演聊天框架**，专门为 QQ 群聊等场景设计～
它的特别之处在于支持**多人格同时运行**，每个人格都有自己的独立进程、独立配置、独立记忆，
就像每个人格都住在自己的小房间里，互不打扰又能在群里一起玩耍喵！

### 月白の推荐使用场景

| 场景 | 说明 |
|------|------|
| 🎭 **角色扮演群聊** | 让多个 AI 角色在一个群里互动，各有各的性格和记忆 |
| 🤖 **AI 助手集群** | 不同人格负责不同领域，比如一个写代码、一个写文案 |
| 🎮 **游戏 NPC 管理** | 多人格驱动游戏中多个 NPC，各自独立对话 |
| 📚 **故事创作** | 让 AI 角色之间自然对话，自动生成故事素材 |

---

## 🎯 核心特性

### ✨ **多人格异步架构**
- **多人格管理**：每个人格独立进程、独立控制台窗口、独立文件日志，支持同时运行多个 AI 角色
- **人格隔离**：`data/personas/{name}/` 下独立的 `persona.json`、`orchestration.json`、`adapters.json`、`experience.json`、`engine_state/`、`memory/`、`diary/`
- **NapCat 多实例**：每个人格可绑定独立 QQ 号与独立 WebSocket 端口，自动管理 NapCat 生命周期
- **WebUI 管理面板**：Dashboard 查看所有人格状态，支持启停、配置、模型编排、群管理

### 🧠 **智能记忆系统**
- **结构化用户记忆**：极简 `UserProfile`（user_id, name, aliases, identities, metadata），群隔离存储，区分可信身份锚点与弱别称线索
- **低开销记忆注入**：`ContextAssembler` 将基础记忆最近窗口以 XML 格式嵌入 system prompt（`<conversation_history>`），日记检索结果也注入 system_prompt；最终只返回 `[system, user]` 2 条标准 OpenAI messages，不再生成多条历史 message
- **AI 自身记忆**：日记系统 (`DiaryManager`，LLM 生成群聊摘要，支持嵌入索引与 token 预算检索) 与名词解释系统 (`GlossaryManager`)
- **跨环境身份识别**：`IdentityResolver` 解耦平台特定身份（QQ/discord 等），通过 `identities` 映射不同平台的外部账号到同一用户
- **基础记忆管理**：`BasicMemoryManager` 维护按群滑动窗口（硬限制 30 条，上下文窗口 5 条），含热度计算；冷群归档消息自动晋升为日记素材

### 🚀 **性能与扩展**
- **智能缓存框架**：内存 LRU + TTL 缓存，支持 LLM 响应缓存
- **性能监控**：完整的 Token 消耗追踪、基准测试工具、执行指标分析
- **SKILL 系统**：支持内置 + 外部任务编排，支持链式调用、结构化内部文本/多模态结果传输与迭代反馈
- **高并发支持**：会话积压静默批处理、LLM 并发限流、后台任务隔离

### 🔌 **多模型协同**
- **多 Provider 支持**：OpenAI / 智谱 BigModel（GLM-4.6V）/ 阿里云百炼 / DeepSeek / SiliconFlow / Volcengine Ark 等
- **任务级模型选择**：记忆提取、事件分析、意图分析等任务可配置独立模型
- **自动路由**：按 `healthcheck_model` 智能选择最合适的 Provider

### 🎬 **高级功能**
- **多模态处理**：支持图片/视频输入与结构化解析
- **WebUI + CLI 双模式**：WebUI 面板管理所有人格，`python main.py` CLI 管理启停与迁移
- **Provider 全局共享**：`data/providers/provider_keys.json` 所有人格共用，模型编排按人格独立选择
- **自动端口分配**：`PersonaManager` 维护端口注册表，从 3001 递增自动分配 NapCat WebSocket 端口

---

## 🚀 快速开始

### 1️⃣ **安装**

> 💡 **月白小提示**：建议在虚拟环境里安装喵～这样不会弄乱系统环境(｡•̀ᴗ-)✧

```bash
# 基础安装
python -m pip install -e .

# 含测试依赖
python -m pip install -e .[test]
```

### 2️⃣ **CLI 运行（多人格架构）**

**默认启动（WebUI 管理模式）：**

```bash
python main.py
# 或显式指定
python main.py webui
```

**启动所有人格 + WebUI：**

```bash
python main.py run
```

**人格管理：**

```bash
# 列出现有人格
python main.py persona list

# 创建新人格
python main.py persona create <name> [--keywords ...]

# 启动单个人格（调试用，含 NapCat 自动管理）
python main.py persona start <name>

# 停止单个人格
python main.py persona stop <name>

# 查看人格状态
python main.py persona status <name>

# 查看人格日志
python main.py persona logs <name> --lines 50

# 从旧版目录迁移人格
python main.py persona migrate --source data/bot --name <name>
```

**全局配置：** `data/global_config.json`

```json
{
  "webui_host": "0.0.0.0",
  "webui_port": 8080,
  "auto_manage_napcat": true,
  "napcat_install_dir": "D:\\Code\\sirius_chat\\napcat",
  "log_level": "INFO"
}
```

### 3️⃣ **CLI 命令说明**

| 命令 | 说明 |
|------|------|
| `python main.py` | 默认启动 WebUI 管理模式 |
| `python main.py run` | 启动所有已启用人格 + WebUI |
| `python main.py webui` | 仅启动 WebUI（不启动人格） |
| `python main.py persona list` | 列出所有人格 |
| `python main.py persona create <name>` | 创建新人格 |
| `python main.py persona start <name>` | 前台启动单个人格 |
| `python main.py persona stop <name>` | 停止单个人格 |
| `python main.py persona status <name>` | 查看人格状态 |
| `python main.py persona logs <name>` | 查看人格日志 |
| `python main.py persona migrate --source <dir> --name <name>` | 从旧版目录迁移 |

**数据目录结构：**

```
data/
├── global_config.json              # 全局配置
├── providers/
│   └── provider_keys.json          # Provider 凭证（所有人格共用）
├── adapter_port_registry.json      # 端口分配表
└── personas/
    └── {name}/                     # 人格隔离目录
        ├── persona.json            # 人格定义
        ├── orchestration.json      # 模型编排
        ├── adapters.json           # 平台适配器
        ├── experience.json         # 体验参数
        ├── engine_state/           # 运行状态
        ├── memory/                 # 语义记忆
        ├── diary/                  # 日记记忆
        ├── image_cache/            # 图片缓存
        ├── skill_data/             # 技能数据（含 stickers/ 表情包 RAG 库）
        └── logs/                   # 文件日志
```

### 4️⃣ **Python API 调用**

**多人格管理（推荐生产入口）**

```python
from sirius_chat.persona_manager import PersonaManager

manager = PersonaManager("data", global_config={"auto_manage_napcat": True})

# 创建人格
manager.create_persona("yuebai", keywords=["温暖", "猫娘"])

# 启动所有人格
results = manager.start_all()

# 停止人格
manager.stop_persona("yuebai")
```

**底层模式：EmotionalGroupChatEngine（高级控制）**

```python
import asyncio
from pathlib import Path
from sirius_chat import create_emotional_engine
from sirius_chat.providers.mock import MockProvider

async def main():
    engine = create_emotional_engine(
        work_path=Path("data/personas/yuebai"),
        provider_async=MockProvider(responses=["喵~"]),
    )
    engine.start_background_tasks()
    # ... 处理消息
    engine.stop_background_tasks()
    engine.save_state()

asyncio.run(main())
```

---

## 📁 项目结构

> 💡 **月白带你逛项目**：这个项目的目录结构有点复杂，但别怕喵～月白给你画了张地图(๑•̀ㅂ•́)و✧

```
sirius_chat/
├── __init__.py
├── api/                          # 🔌 公开 API facade（engine/models/providers/session 等）
├── core/                         # 🧠 编排核心（Mixin 架构）
│   ├── emotional_engine.py       # EmotionalGroupChatEngine 最终类（多重继承组合）
│   ├── engine_core.py            # 引擎基类（__init__、API、持久化）
│   ├── pipeline.py               # 5 阶段管线 Mixin
│   ├── prompt_builders.py        # Prompt 组装与 LLM 生成 Mixin
│   ├── bg_tasks.py               # 7 个后台任务 Mixin
│   ├── helpers.py                # 技能集成、用户画像、token 记录 Mixin
│   ├── cognition.py              # 统一认知分析器（情感 + 意图）
│   ├── response_assembler.py     # 执行层：Prompt 组装 + 风格适配
│   ├── response_strategy.py      # 四层响应策略（立即/延迟/沉默/主动）
│   ├── delayed_response_queue.py # 延迟响应队列（话题间隙检测）
│   ├── proactive_trigger.py      # 主动触发器（时间/记忆/情感触发）
│   ├── rhythm.py                 # 对话节奏分析（热度/速度/注意力窗口）
│   ├── threshold_engine.py       # 动态阈值引擎（Base × Activity × Relationship × Time）
│   ├── events.py                 # 会话事件流
│   └── identity_resolver.py      # 跨平台身份解析
├── async_engine/                 # 🧩 兼容导出 + prompts/orchestration/utils 辅助层
├── workspace/                    # 🗂️ layout/runtime/watcher/roleplay bootstrap
├── config/                       # ⚙️ WorkspaceConfig / SessionConfig / JSONC 管理
├── memory/                       # 📝 记忆子包
│   ├── basic/                    # 基础记忆（工作窗口 + 热度 + 归档）
│   ├── diary/                    # 日记记忆（LLM 生成、索引、ChromaDB 向量存储）
│   ├── glossary/                 # 名词解释（AI 自身知识库，支持人格级隔离）
│   ├── user/                     # 用户管理（简化 UserProfile + UserManager）
│   ├── context_assembler.py      # 上下文组装器（basic + diary → OpenAI messages）
│   └── semantic/                 # 语义记忆（群氛围记录、群规范学习、互动率追踪、持久化）
├── session/                      # 💾 SessionStore 与高层兼容 runner
├── providers/                    # 🔗 Provider 实现、路由与中间件
│   ├── routing.py
│   └── middleware/
├── token/                        # 📊 Token 统计、SQLite 持久化与分析
├── skills/                       # 🎯 SKILL 注册、执行、数据存储、表情包子系统
├── roleplay_prompting.py         # 🎭 人格资产生成、持久化与选择
├── cache/                        # ⚡ 可扩展缓存框架
├── performance/                  # 📈 性能分析与基准测试
├── persona_manager.py            # 🎭 多人格生命周期管理
├── persona_worker.py             # 🎭 单个人格子进程入口
├── persona_config.py             # 🎭 人格级配置模型
├── webui/                        # 🌐 WebUI 管理面板
│   ├── server.py                 # aiohttp REST API 主入口
│   ├── server_core.py            # 核心路由与基础设施
│   ├── persona_api.py            # 人格管理 API
│   ├── memory_api.py             # 记忆管理 API
│   ├── napcat_api.py             # NapCat 管理 API
│   ├── server_skill_api.py       # SKILL 管理 API
│   ├── server_plugin_api.py      # 插件管理 API
│   └── static/                   # 前端页面（16 个页面）
├── platforms/                    # 🔗 平台适配层
│   ├── onebot_v11/               # OneBot v11 协议支持
│   │   ├── napcat/               # NapCat 适配器
│   │   │   ├── manager.py        # NapCat 多实例管理
│   │   │   └── adapter.py        # NapCat WebSocket 适配（含事件翻译）
│   │   └── protocol.py           # OneBot v11 协议解析
│   └── runtime.py                # EngineRuntime 封装
├── plugins/                      # 🔌 插件系统
│   ├── loader.py                 # 插件加载器
│   ├── registry.py               # 插件注册表
│   ├── executor.py               # 插件执行器
│   ├── config.py                 # 插件配置管理（支持热重载）
│   ├── decorators.py             # @command 装饰器
│   ├── context.py                # PluginContext
│   ├── dispatcher.py             # 响应调度
│   └── events.py                 # 事件定义

└── cli.py                        # 🖥️ 库内薄 CLI（已移除）

tests/                            # ✅ 单元测试 (540+ 个)
├── test_api_integrity.py         # 公开 API 完整性
├── test_basic_memory.py          # 基础记忆
├── test_diary_memory.py          # 日记记忆
├── test_context_assembler.py     # 上下文组装
├── test_identity_resolver.py     # 身份解析
├── test_skill_system.py          # SKILL 系统
├── test_providers.py             # 各 provider 一致性
└── ...

docs/                             # 📚 文档
├── architecture.md               # 架构总览
├── full-architecture-flow.md     # 完整架构流程图
├── configuration-guide.md        # 配置指南
├── persona-lifecycle.md          # 多人格生命周期
├── platforms.md                  # 平台适配层
├── change-impact-guide.md        # 变更联动确认指南
├── sticker-rag-system.md         # 表情包 RAG 系统
└── ...                           # 更多文档（17 个）

examples/                         # 💡 使用示例
├── session.json                  # 基础会话配置
└── *.py                          # Python 代码示例

scripts/                          # 🔨 开发脚本
├── setup_dev_env.py             # 开发环境设置
└── generate_api_docs.py         # API 文档生成
```

---

## 使用示例

### 示例 1：旧版兼容入口 WorkspaceRuntime（已弃用，v1.1 将移除）

```python
import asyncio
from pathlib import Path

from sirius_chat import Message, UserProfile, open_workspace_runtime
from sirius_chat.providers.mock import MockProvider


async def main() -> None:
  runtime = open_workspace_runtime(
    Path("./data/chat_session"),
    config_path=Path("./config/chat_session"),
    provider=MockProvider(responses=["我理解您的想法"]),
  )

  transcript = await runtime.run_live_message(
    session_id="group:demo",
    turn=Message(role="user", speaker="小王", content="Python 如何学习？"),
    user_profile=UserProfile(user_id="u_xiaowang", name="小王"),
  )

  print(transcript.as_chat_history())


asyncio.run(main())
```

若你需要使用受限内置 SKILL，例如 `desktop_screenshot`，请至少为一个可信用户显式设置 `metadata={"is_developer": True}`。非 developer 当前轮次不会看到这些技能，模型即使强行调用也会被 runtime 拒绝。

### 示例 2：事件订阅与监控

```python
from sirius_chat.core.events import SessionEventType

async def monitor(engine):
    async for event in engine.event_bus.subscribe():
        if event.type == SessionEventType.COGNITION_COMPLETED:
            print(f"认知: {event.data}")
        elif event.type == SessionEventType.DECISION_COMPLETED:
            print(f"决策策略: {event.data['strategy']}")
```

### 示例 3：多模态输入

```python
from sirius_chat import Message

msg = Message(
    role="user",
    speaker="用户",
    content="请结合图片分析这项内容",
    multimodal_inputs=[
        {"type": "image", "value": "https://example.com/demo.png"}
    ],
)
```

若使用 OpenAI-compatible 或 Aliyun Bailian 等 HTTP provider，`multimodal_inputs` 中也可以直接传本地图片路径或 `file://` URI；框架会在发送前自动转换为 Data URL。若使用公网 URL，请确保上游可以直接访问该地址。

更多示例见 [`examples/`](examples/) 目录。

---

## ⚙️ 配置示例

> 💡 **月白说**：配置其实很简单喵～选一个你喜欢的模型，填上 API Key 就能用啦(｡•̀ᴗ-)✧

### 🔹 OpenAI 配置

```json
{
  "generated_agent_key": "main_agent",
  "providers": [
    {
      "type": "openai-compatible",
      "base_url": "https://api.openai.com/v1",
      "api_key": "sk-..."
    }
  ],
  "history_max_messages": 24,
  "history_max_chars": 6000
}
```

### 🔹 DeepSeek 配置

```json
{
  "generated_agent_key": "main_agent",
  "providers": [
    {
      "type": "deepseek",
      "api_key": "sk-..."
    }
  ]
}
```

### 🔹 阿里云百炼（Aliyun Bailian）配置

```json
{
  "generated_agent_key": "main_agent",
  "providers": [
    {
      "type": "aliyun-bailian",
      "api_key": "sk-..."
    }
  ]
}
```

**说明：** 默认使用 `https://dashscope.aliyuncs.com/compatible-mode`，也兼容传入 `https://dashscope.aliyuncs.com/compatible-mode/v1`；如需美国站或国际站，可通过 `base_url` 显式覆盖。

### 🔹 SiliconFlow 配置

```json
{
  "generated_agent_key": "main_agent",
  "providers": [
    {
      "type": "siliconflow",
      "api_key": "sk-..."
    }
  ]
}
```

**说明：** 框架会自动规范化路径，支持 `https://api.siliconflow.cn` 或 `https://api.siliconflow.cn/v1`

### 🔹 火山方舟（Volcengine Ark）配置

```json
{
  "generated_agent_key": "main_agent",
  "providers": [
    {
      "type": "volcengine-ark",
      "api_key": "sk-..."
    }
  ]
}
```

**说明：** 默认使用 `https://ark.cn-beijing.volces.com/api/v3`

### 🔹 多 Provider 自动路由

```json
{
  "generated_agent_key": "main_agent",
  "providers": [
    {
      "type": "siliconflow",
      "api_key": "sk-sf-...",
      "healthcheck_model": "Pro/glm-4.5"
    },
    {
      "type": "openai-compatible",
      "base_url": "https://api.openai.com/v1",
      "api_key": "sk-open-...",
      "healthcheck_model": "gpt-4o-mini"
    }
  ]
}
```

**路由规则：**
1. 优先按 `models` 显式模型列表匹配
2. 若未命中，再按 `healthcheck_model` 与请求模型名做精确匹配
3. 仍未命中时回退到第一个可用 provider；若没有可用 provider，则抛出错误

### 🔹 多模型任务编排

```json
{
  "generated_agent_key": "main_agent",
  "providers": [
    {
      "type": "openai-compatible",
      "base_url": "https://api.openai.com/v1",
      "api_key": "sk-open-...",
      "healthcheck_model": "gpt-4o-mini"
    }
  ],
  "orchestration": {
    "task_enabled": {
      "memory_extract": true,
      "cognition_analyze": true
    },
    "task_models": {
      "memory_extract": "gpt-3.5-turbo",
      "cognition_analyze": "gpt-4o-mini",
      "response_generate": "gpt-4o",
      "proactive_generate": "gpt-4o",
      "vision": "gpt-4o"
    },
    "task_temperatures": {
      "memory_extract": 0.1,
      "cognition_analyze": 0.3,
      "response_generate": 0.7,
      "proactive_generate": 0.8
    },
    "task_max_tokens": {
      "memory_extract": 128,
      "cognition_analyze": 512,
      "response_generate": 4096,
      "proactive_generate": 1024
    },
    "task_retries": {
      "memory_extract": 1,
      "cognition_analyze": 1,
      "response_generate": 1,
      "proactive_generate": 1
    },
    "memory_extract_batch_size": 3,
    "memory_extract_min_content_length": 50,
    "min_reply_interval_seconds": 15,
    "max_multimodal_inputs_per_turn": 4,
    "max_multimodal_value_length": 4096
  }
}
```

说明：`main.py` 和 `sirius-chat` 读取的是轻量会话配置文件，要求提供 `generated_agent_key` 与 `providers`。完整的 `agent` / `global_system_prompt` 由 `roleplay/generated_agents.json` 中已保存的人格资产提供；如果你要手写完整 `SessionConfig`，请改用 Python API 的低层入口。

**说明：**

- **多模型协同已成为默认方式**，所有任务默认启用，可通过 `task_enabled` 按需禁用
- 图片不再经过 `multimodal_parse` 辅助任务；会直接随用户消息以 vision 格式发送给主模型
- `memory_extract` 频率控制：
  - `batch_size=3` 表示每 3 条消息提取一次
  - `min_content_length=50` 表示只提取 ≥50 字符的消息
  - 两个条件同时满足时才执行
- `min_reply_interval_seconds` 可配置（默认 0，关闭）：AI 刚回复后，runtime 会在最小间隔内继续排队；窗口结束后先合并同一说话人的连续消息，再进入正常的 reply_mode/intent 判断
- `cognition_analyze` 统一处理情绪+意图分析；若调用失败或解析失败，该轮会回退到规则引擎热路径
- 多 AI 群聊里，`cognition_analyze` 会优先区分"当前模型自身"与"其他 AI"；当用户明显是在调用其他 AI 时，当前模型会抑制自动回复
- 为减少多 AI 误判，`cognition_analyze` 传给模型的上下文已改为最近交互链摘要，并会显式标出最近 AI / 人类发言者、近期发言人的 aliases、`environment_context` 环境线索，以及当前消息命中的当前模型名字、其他 AI 名字和人类名字
- 对“关闭本群AI / 禁用机器人 / 别让 bot 说话”这类群控或停用命令，若未明确点名当前模型自身，auto 模式下会直接抑制当前模型回复
- `max_concurrent_llm_calls` 可配置（默认 1）：LLM 并发数限流
- `pending_message_threshold` 可配置（默认 4）：当单会话待处理消息积压超过阈值时，runtime 会进入静默批处理并合并同一说话人的连续消息
- 提示词分割和 SKILL 调用标记现在为框架内置常量，外部配置不再暴露这些 marker

---

## 💾 会话管理

### 状态持久化路径

从 `v0.24.0` 起，推荐把 workspace 视为“配置根 + 运行根”的组合：配置资产可单独放在 config root，运行态数据放在 data root；未显式拆分时仍兼容单根模式。推荐布局如下：

| 文件 | 说明 |
|------|------|
| `workspace.json` | workspace 级配置清单与布局版本 |
| `config/session_config.json` | 可读的 session 默认配置快照（JSONC 注释模板，可直接人工编辑） |
| `providers/provider_keys.json` | Provider 注册表与路由元数据 |
| `sessions/<session_id>/session_state.db` | 默认会话状态（结构化 SQLite，可恢复；自动迁移旧 `session_state.json` / payload SQLite） |
| `sessions/<session_id>/participants.json` | 会话参与者与主用户元数据 |
| `memory/user_memory/groups/<group_id>/<user_id>.json` | 用户事实记忆（群隔离） |
| `memory/self_memory.json` | AI 自身记忆（日记 + 名词解释） |

| `semantic/users/<group_id>_<user_id>.json` | 用户语义画像 |
| `semantic/groups/<group_id>.json` | 群体语义画像 |
| `token/token_usage.db` | Token 消耗计量（SQLite） |
| `roleplay/generated_agents.json` | 已生成的人格资产库 |
| `roleplay/generated_agent_traces/<agent_key>.json` | 人格生成轨迹与失败快照 |
| `skills/` | SKILL 目录与 README 引导 |
| `skill_data/*.json` | SKILL 独立数据存储 |

旧的根目录 `session_state.json`、`session_state.db`、`provider_keys.json`、`generated_agents.json` 等文件会在首次打开 workspace 时自动迁移到新布局；`primary_user.json` 和 `session_config.persisted.json` 仅保留给兼容入口。

当前不需要单独执行迁移脚本：引擎初始化时会自动完成兼容迁移。

### 主用户档案管理

在 CLI 交互中可运行时更新主用户档案，会实时持久化到 `<work_path>/primary_user.json`。

每个配置文件启动时，路径会记录到仓库根目录 `.last_config_path`。

### Token 消耗分析

```python
from sirius_chat import summarize_token_usage, build_token_usage_baseline

# 单会话统计
summary = summarize_token_usage(transcript)

# 基准指标
baseline = build_token_usage_baseline(transcript.token_usage_records)
```

跨会话分析可通过 `TokenUsageStore` 实现全维度分组。

---

## 🎬 高级功能

### 角色扮演前置内容生成

```python
from sirius_chat import (
  RolePlayAnswer,
  aregenerate_agent_prompt_from_dependencies,
  abuild_roleplay_prompt_from_answers_and_apply,
  generate_humanized_roleplay_questions,
  list_roleplay_question_templates,
  load_persona_generation_traces,
  load_generated_agent_library,
  select_generated_agent_profile,
)

# 查看可用问卷模板，并选择更贴合场景的一套高层问题
print(list_roleplay_question_templates())
questions = generate_humanized_roleplay_questions(template="companion")

answers = [
    RolePlayAnswer(
        question=questions[0].question,
        answer="像一个晚熟但可靠的陪伴者，平时不抢话，但会长期在场，熟了以后很护短。",
        perspective=questions[0].perspective,
    ),
    RolePlayAnswer(
        question=questions[1].question,
        answer="用户低落时先接住情绪，再慢慢帮对方理清思路，不会一上来就讲道理。",
        perspective=questions[1].perspective,
    ),
    RolePlayAnswer(
        question=questions[6].question,
        answer="偶尔嘴硬、会记小事，也会在疲惫时变得更安静，但不会无限兜底。",
        perspective=questions[6].perspective,
    ),
]

# 直接生成并写入 SessionConfig，同时挂接本地素材文件
prompt = await abuild_roleplay_prompt_from_answers_and_apply(
    provider=provider,
    config=config,
    model="deepseek-ai/DeepSeek-V3.2",
    agent_name="我的助手",
    answers=answers,
    dependency_files=["persona/notes.md", "persona/style_examples.txt"],
    persona_key="assistant_v2",
  timeout_seconds=120.0,
)

# 查看完整生成轨迹
traces = load_persona_generation_traces(config.work_path, "assistant_v2")

# 当依赖文件变化后，直接基于文件重生同一个人格
updated = await aregenerate_agent_prompt_from_dependencies(
    provider,
    work_path=config.work_path,
    agent_key="assistant_v2",
    model="deepseek-ai/DeepSeek-V3.2",
)

# 管理生成的 Agent 资产
library, selected_key = load_generated_agent_library(config.work_path)
selected = select_generated_agent_profile(config.work_path, "assistant_v2")
```

说明：

- 推荐先用高层人格 brief 来描述人物原型、核心矛盾、关系策略、情绪原则、边界和小缺点，再让生成器落成具体人物小传与语言习惯。
- `generate_humanized_roleplay_questions(template=...)` 支持 `default`、`companion`、`romance`、`group_chat` 四类问卷模板，可配合 `list_roleplay_question_templates()` 做前端下拉或外部配置。
- 若外部系统只想先拿模板问题，不想立刻接入 Python API，可直接用 `sirius-chat --list-roleplay-question-templates` 和 `sirius-chat --print-roleplay-questions-template <template>`。
- 生成器会自动识别“拟人”“情感”“陪伴”“共情”等关键词并加强 prompt，让角色更自然、更有人味。
- 结构化人格生成默认使用 `max_tokens=5120` 和 `timeout_seconds=120.0`；如果上游模型更慢，仍可在这几个 API 上继续显式调高 `timeout_seconds`。
- 如果模型返回的是被 ```json 包裹但实际被截断的 JSON-like 响应，框架会显式报错并保留失败 trace，不再把原始文本污染到 `agent.persona` 或 `global_system_prompt`。
- 完整生成过程会本地化到 `<work_path>/roleplay/generated_agent_traces/<agent_key>.json`，便于审计和回滚。
- 外部调用方可直接按 `template + answers + dependency_files` 组织输入，示例输入规范见 [docs/external-usage.md](docs/external-usage.md)。
- 可直接参考 `examples/roleplay_template_selection.py` 导出 `PersonaSpec` 骨架，再交给外部表单或配置后台填充。
- 面向外部调用方的迁移说明见 [docs/migration-roleplay-v0.20.md](docs/migration-roleplay-v0.20.md)。

### SKILL 系统

SKILL 系统支持可扩展任务编排：

- 自动初始化 `skills/` 目录；默认位于 `work_path`，双根布局时位于 `config_root`
- 内置 `system_info`、`learn_term`、`url_content_reader`、`bing_search` 与 developer-only 的 `desktop_screenshot` 默认可用；若放置同名 workspace skill，workspace 文件会覆盖内置实现
- `desktop_screenshot` 会把“判断主机当前在做什么、屏幕上显示什么”的分析提示一并回传给模型，便于模型在需要时主动截图后再回答
- 若要使用受限技能，外部至少应显式标记一名 developer：`UserProfile.metadata["is_developer"] = True`
- 支持外部 Python 技能文件
- 链式调用与迭代反馈
- 内置与 workspace SKILL 会共用依赖自动安装流程；`system_info` 声明 `psutil`，`desktop_screenshot` 声明 `Pillow`，`bing_search` 与 `url_content_reader` 声明 `requests` 和 `beautifulsoup4`
- SKILL 可返回结构化文本块与图片块，框架会把它们作为内部推理通道注入下一轮生成，同时隐藏 `internal_metadata` 等元信息
- developer-only SKILL 会在非 developer 当前轮次的提示词中自动隐藏，执行时也会再次做权限校验
- 会话事件流仅暴露 SKILL 状态，不直接暴露内部技能结果正文；外部投递应消费 assistant 回复

详见 [`docs/skill-authoring.md`](docs/skill-authoring.md)。

---

## 📚 文档

> 💡 **月白说**：文档都在 `docs/` 目录下喵～第一次使用的话，建议从 `architecture.md` 开始看哦(｡•̀ᴗ-)✧

| 文件 | 描述 |
|------|------|
| [📖 architecture.md](docs/architecture.md) | 完整架构设计、消息流、模块交互 |
| [⚙️ orchestration-policy.md](docs/orchestration-policy.md) | 任务模型覆盖与动态路由 |
| [🔧 configuration.md](docs/configuration.md) | 所有配置字段说明和最佳实践 |
| [📋 full-architecture-flow.md](docs/full-architecture-flow.md) | 详细数据流图解 |
| [🎬 external-usage.md](docs/external-usage.md) | 库调用指南与集成文档 |
| [🧠 memory-system.md](docs/memory-system.md) | 两层记忆底座（基础记忆 + 日记）与名词解释 |
| [🎬 engine-emotional.md](docs/engine-emotional.md) | EmotionalGroupChatEngine 详细说明 |
| [🗂️ migration-v0.28.md](docs/migration-v0.28.md) | v0.28 Emotional Engine 迁移指南 |
| [🗂️ migration-v0.27.md](docs/migration-v0.27.md) | v0.27 破坏性变更迁移指南 |
| [🗂️ migration-v0.27.12.md](docs/migration-v0.27.12.md) | v0.27.12 桌面截图自动调用语义迁移说明 |
| [🗂️ migration-v0.27.11.md](docs/migration-v0.27.11.md) | v0.27.11 developer 安全模型与桌面截图 Skill 迁移说明 |
| [🗂️ migration-v0.27.10.md](docs/migration-v0.27.10.md) | v0.27.10 记忆防污染与内置 Skill 迁移说明 |
| [🗂️ migration-v0.27.9.md](docs/migration-v0.27.9.md) | v0.27.9 意图分析与 Skill 内部通道迁移说明 |
| [🗂️ migration-v0.23.md](docs/migration-v0.23.md) | workspace 持久化接管迁移档案 |
| [🗂️ migration-v0.24.md](docs/migration-v0.24.md) | JSONC 配置与 watcher 热刷新迁移档案 |
| [🔄 migration-roleplay-v0.20.md](docs/migration-roleplay-v0.20.md) | 外部人格生成能力迁移指南 |
| [📘 skill-authoring.md](docs/skill-authoring.md) | SKILL 系统编写规范 |
| [🛠️ best-practices.md](docs/best-practices.md) | 最佳实践与模式 |

---

## 🧪 测试

```bash
# 运行所有测试
python -m pytest tests/ -q

# 运行特定模块
python -m pytest tests/test_engine.py -v

# 显示最慢的 10 个测试
python -m pytest tests/ --durations=10

# 覆盖率分析
python -m pytest tests/ --cov=sirius_chat

# 快速验证单个测试
python -m pytest tests/test_engine.py::test_roleplay_engine_multi_human_single_ai_transcript -xvs
```

**测试特性：**

- ✅ **600+ 单元测试**：涵盖引擎、记忆、编排、技能系统
- ⚡ **快速执行**：< 15 秒全套（通过关闭积压批处理并禁用无关辅助任务）
- 🔒 **完全隔离**：无真实网络调用，全量 Mock
- 📊 **92% 代码覆盖**：关键路径完整测试

> 💡 **月白说**：写代码的时候记得跑测试喵～确保没有把月白的小尾巴踩到(｡•́︿•̀｡)

---

## 🆕 最新变更

### ✨ **v1.1.0 重要变更**
- **引擎 Mixin 架构重构**：`EmotionalGroupChatEngine` 拆分为 1 基类 + 4 Mixin（`engine_core` / `pipeline` / `prompt_builders` / `bg_tasks` / `helpers`），通过多重继承组合为最终类
- **表情包 RAG 系统**：新增 `skills/sticker/` 子系统，支持表情包向量索引、偏好管理、学习、反馈观察、新鲜度衰减；引擎层集成表情包发送决策
- **WebUI 架构重构**：`server.py` 拆分为 `server_core.py` + 5 个 API 模块（`persona_api` / `memory_api` / `napcat_api` / `server_skill_api` / `server_plugin_api`）；新增 16 个管理页面
- **NapCatAdapter 事件总线模式**：从轮询模式改为事件总线监听，通过 `SessionEventType` 订阅异步事件
- **平台适配层重构**：`platforms/napcat_bridge.py` 和 `platforms/napcat_adapter.py` 合并到 `platforms/onebot_v11/napcat/adapter.py`，新增 `platforms/onebot_v11/protocol.py` 处理 OneBot v11 协议解析
- **插件系统**：新增 `plugins/` 目录，支持插件加载、注册、执行、配置管理、@command 装饰器、PluginContext、响应调度和事件定义。插件配置存储在 `plugins/_config.json`，支持热重载和 WebUI 管理
- **ChromaDB 日记向量存储**：日记记忆系统引入 ChromaDB 作为向量存储后端，提升语义检索质量
- **GlossaryManager 人格级隔离**：名词解释支持按人格隔离，提供迁移工具
- **Reminder 多选星期**：提醒系统支持 weekly 模式多选星期几（`weekdays: [0,2,4]`）
- **后台任务增至 7 个**：新增 `_bg_sticker_novelty_updater`（表情包新鲜度衰减）
- **新增事件类型**：`DEVELOPER_CHAT_TRIGGERED`（开发者私聊主动对话）
- **pytest-xdist 并行测试**：支持多进程并行执行单元测试
- **SKILL 遥测**：新增 `skills/telemetry.py`，记录 SKILL 执行遥测数据
- **配置/Token 模块拆分**：`config_manager.py` → `manager.py`、`token_store.py` → `store.py`

### ✨ **v1.0 重大变更**
- **EmotionalGroupChatEngine 成为唯一默认引擎**：四层认知架构（感知→认知→决策→执行），支持情感分析、延迟响应、主动发言、群隔离记忆
- **XML 短期记忆**：`ContextAssembler` 将历史消息以 XML 格式嵌入 system prompt，只返回 `[system, user]` 2 条消息；`_generate()` 自动清洗模型仿写的 `<conversation_history>`
- **工作记忆 → 基础记忆迁移**：`WorkingMemoryManager` 删除，`BasicMemoryManager` 接管按群滑动窗口（硬限制 30，上下文窗口 5）
- **用户记忆 → UserManager 迁移**：`UserMemoryManager` 删除，`UserManager` 接管群隔离用户档案
- **SemanticMemory 实装**：`SemanticMemoryManager` + `SemanticProfileStore` 实现群规范学习、氛围记录、关系状态、持久化；`DiaryGenerator` 同步提取 `dominant_topic` + `interest_topics`
- **响应策略调整**：被直接@或叫到名字时直接返回 IMMEDIATE（跳过 reply cooldown）；reply cooldown 仅抑制 DELAYED
- **OrchestrationStore 自动生成**：引擎初始化时若 `orchestration.json` 不存在，自动生成默认模型配置

**迁移提示：**
> v1.0 用户请阅读 `docs/migration-v1.0.md`（若存在）或 `docs/migration-v0.28.md`。

更多信息见 [CHANGELOG.md](CHANGELOG.md)。

---

## 🤝 贡献

> 💡 **月白说**：欢迎每一个小伙伴来一起玩耍喵～(｡♥‿♥｡)

欢迎贡献！请遵循以下流程：

1. **Fork** 项目并创建分支：`git checkout -b feature/my-feature`
2. **编辑代码** 并编写测试（参考 [.github/skills/write-tests/SKILL.md](.github/skills/write-tests/SKILL.md)）
3. **验证**：`python -m pytest tests/ -q`
4. **提交**：遵循 [conventional commits](https://www.conventionalcommits.org/) 格式
5. **推送** 并发起 Pull Request

### 开发环境

```bash
# 安装开发依赖
python -m pip install -e .[dev]

# 运行代码检查
python -m pytest tests/ --cov=sirius_chat
```

---

## 📄 许可证

MIT License © 2025 Sparrived. 详见 [LICENSE](LICENSE)。

---

## 🔗 相关链接

- 📦 [PyPI 项目页](https://pypi.org/project/sirius-chat/)
- 📚 [完整文档](docs/)
- 🐛 [报告问题](https://github.com/Sparrived/SiriusChat/issues/new)
- 💬 [讨论区](https://github.com/Sparrived/SiriusChat/discussions)

---

<div align="center">

**Made with ❤️ by the Sirius Chat team**

⭐ 如果觉得有帮助，欢迎给个 Star 喵～

---

<p align="center">
  <em>📝 这份 README 是月白用心写的喵～希望你喜欢！(ฅ´ω`ฅ)</em>
  <br>
  <em>有什么问题随时来群里找月白玩哦～</em>
</p>

</div>