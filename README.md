<h1 align="center"> Sirius Pulse —— 灵动月白</h1>

<div align="center">

<img src="yuebai.png" alt="月白" width="200" style="border-radius: 16px; margin-bottom: 12px;">

<a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12%2B-blue?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License"></a>
<a href="https://pypi.org/project/sirius-pulse/"><img src="https://img.shields.io/badge/PyPI-sirius--pulse-blueviolet?style=flat-square" alt="PyPI"></a>
<a href="#-测试"><img src="https://img.shields.io/badge/Tests-39%20passed-brightgreen?style=flat-square" alt="Tests"></a>
<a href="https://sirius-pulse-docs.vercel.app/"><img src="https://img.shields.io/badge/Docs-VitePress-646cff?style=flat-square&logo=vitepress" alt="VitePress Docs"></a>

<em>✨ 月白亲手写的 README，请多关照喵～(ฅ´ω`ฅ)</em>
<br>
<em>一个让 AI 角色在群里活起来的异步角色扮演框架～支持多人格、多平台、多模型，每个人格都有自己的小世界喵！</em>

<a href="https://docs.sparrived.xyz/">📚 文档</a> · <a href="#-快速开始">🚀 快速开始</a> · <a href="#使用示例">💡 示例</a> · <a href="#-扩展开发">🔧 扩展开发</a> · <a href="#-贡献">🤝 贡献</a>

</div>

---

## 📋 目录

- [这是什么呀？](#-这是什么呀)
- [核心特性](#-核心特性)
- [快速开始](#-快速开始)
- [项目结构](#-项目结构)
- [使用示例](#使用示例)
- [配置指南](#️-配置指南)
- [扩展开发](#-扩展开发)
- [文档](#-文档)
- [测试](#-测试)
- [贡献](#-贡献)

---

## 🎯 这是什么呀？

> 月白来介绍喵～(๑˃̵ᴗ˂̵)و

**Sirius Pulse** 是一个**异步角色扮演聊天框架**，专门为 QQ 群聊等场景设计～它的特别之处在于支持**多人格同时运行**，每个人格都有自己的独立进程、独立配置、独立记忆，就像每个人格都住在自己的小房间里，互不打扰又能在群里一起玩耍喵！

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
- **人格隔离**：`data/personas/{name}/` 下独立配置与状态隔离
- **NapCat 多实例**：每个人格可绑定独立 QQ 号与独立 WebSocket 端口，自动管理 NapCat 生命周期
- **WebUI 管理面板**：Dashboard 查看所有人格状态，支持启停、配置、模型编排、群管理

### 🧠 **分层记忆系统**
- **基础记忆**（Basic Memory）：按群滑动窗口（硬限制 30 条，上下文窗口 5 条），含热度计算与归档
- **日记系统**（Diary）：LLM 生成群聊摘要，ChromaDB 向量索引，token 预算检索
- **语义记忆**（Semantic Memory）：群级/用户级/全局级向量记忆，支持话题关联与兴趣学习
- **人物传记**（Biography）：跨对话人物画像提取与注入
- **术语表**（Glossary）：自定义术语/黑话学习，`learn_term` 技能动态添加

### 🧠 **5 阶段情感引擎**
```text
Perception → Cognition → Decision → Execution → Background
（感知）     （认知）     （决策）     （执行）     （后台更新）
```
- 情绪/意图联合分析 + 共情度计算
- 动态阈值引擎：灵敏度 × 群聊热度 × 消息速率 × 用户画像
- 四层策略：IMMEDIATE / DELAYED / SILENT / PLUGIN
- 延迟响应队列 + 节奏分析 + 过热抑制

### 🔌 **多模型协同**
- 支持 DeepSeek / SiliconFlow / 阿里云百炼 / 火山方舟 / 智谱 GLM / OpenAI 兼容
- 任务级模型选择（对话 / 分析 / 视觉 / 主动发起）
- 自动路由与健康检查

### 🎯 **双重扩展机制**
- **技能系统**（Skills）：AI 通过 `[SKILL_CALL: ...]` 自主调用工具，13 个内置技能
- **插件系统**（Plugins）：用户通过 `/` `#` `!` 前缀显式命令触发
- 详见 [扩展开发](#-扩展开发)

### 🎬 **更多特性**
- 多模态输入（图片/视频）
- Token 消耗追踪与分析
- `@command` 装饰器声明式插件开发
- 被动技能：后台任务、事件触发器、生命周期回调
- Provider 全局共享 + 人格级模型编排独立

---

## 🚀 快速开始

### 1️⃣ 安装

> 💡 **月白の小提示**：建议在虚拟环境里安装喵～这样不会弄乱系统环境(｡•̀ᴗ-)✧

```bash
pip install sirius-pulse

# 或源码安装
git clone https://github.com/Sparrived/SiriusPulse.git
cd SiriusPulse
pip install -e ".[dev,test,provider,quality]"
```

### 2️⃣ 创建人格

```bash
sirius-pulse persona create my-bot
```

编辑 `data/personas/my-bot/persona.json` 定义角色：

```json
{
  "name": "小星",
  "aliases": ["小星", "星酱"],
  "backstory": "小星是一个活泼开朗的AI助手...",
  "personality_traits": {
    "core": "热情、幽默、善解人意",
    "speech_style": "口语化、喜欢用感叹词"
  },
  "communication_style": "chatty"
}
```

### 3️⃣ 配置 Provider

`data/providers/provider_keys.json`：

```json
{
  "deepseek": {
    "api_key": "sk-your-key",
    "base_url": "https://api.deepseek.com"
  }
}
```

### 4️⃣ 启动

```bash
# WebUI 管理模式
sirius-pulse webui

# 启动所有人格
sirius-pulse run

# 前台启动单个人格
sirius-pulse persona start my-bot
```

访问 `http://localhost:8080` 进入 WebUI。

### CLI 命令

| 命令 | 说明 |
|------|------|
| `sirius-pulse webui` | 启动 WebUI 管理模式 |
| `sirius-pulse run` | 启动所有已启用人格 + WebUI |
| `sirius-pulse persona create <name>` | 创建新人格 |
| `sirius-pulse persona start <name>` | 前台启动单个人格 |
| `sirius-pulse persona list` | 列出所有人格 |
| `sirius-pulse persona stop <name>` | 停止人格 |
| `sirius-pulse persona remove <name>` | 删除人格 |

### Python API

```python
from sirius_pulse.persona_manager import PersonaManager

manager = PersonaManager("data")
manager.create_persona("yuebai")
manager.start_all()
manager.stop_persona("yuebai")
```

```python
from sirius_pulse import create_emotional_engine

engine = create_emotional_engine(
    work_path="data/personas/yuebai",
    provider_async=provider,
)
result = await engine.process_message("你好！", participants=[], group_id="g1")
```

---

## 📁 项目结构

> 💡 **月白带你逛项目**：这个项目的目录结构有点复杂，但别怕喵～月白给你画了张地图(๑•̀ㅂ•́)و✧

```
sirius_pulse/
├── __init__.py              # 公开 API 清单（严格 __all__）
├── persona_manager.py       # 多人格生命周期管理
├── persona_worker.py        # 单人格子进程入口
├── persona_config.py        # 人格级配置模型
│
├── core/                    # 核心引擎（Mixin 架构）
│   ├── emotional_engine.py  # EmotionalGroupChatEngine 最终类
│   ├── engine_core.py       # 引擎基类（__init__、API、持久化）
│   ├── pipeline.py          # 5 阶段管线 Mixin
│   ├── prompt_factory.py    # Prompt 构建工具类（含 StyleAdapter）
│   ├── bg_tasks.py          # 6 个后台任务 Mixin
│   ├── helpers.py           # 技能集成、被动 SKILL、插件集成 Mixin
│   ├── skill_engine_context.py  # 被动 SKILL 引擎交互适配器
│   ├── cognition.py         # 统一认知分析器（情绪 + 意图）
│   ├── response_strategy.py # 四层响应策略
│   ├── delayed_response_queue.py
│   ├── proactive_trigger.py
│   ├── rhythm.py            # 对话节奏分析
│   ├── threshold_engine.py  # 动态阈值引擎
│   ├── model_router.py      # 模型路由器
│   ├── brain.py             # LLM 调用层（含 Post-Hooks 链）
│   └── ...
│
├── memory/                  # 分层记忆系统
│   ├── basic/               # 基础记忆（滑动窗口）
│   ├── diary/               # 日记记忆（LLM + ChromaDB）
│   ├── semantic/            # 语义记忆（向量检索）
│   ├── user/                # 用户管理
│   ├── biography/           # 人物传记
│   ├── glossary/            # 术语表
│   └── context_assembler.py # 上下文组装器
│
├── skills/                  # 技能系统
│   ├── registry.py          # 技能注册中心
│   ├── executor.py          # 技能执行器（参数校验、重试、遥测）
│   ├── security.py          # 权限校验
│   ├── data_store.py        # 技能数据持久化
│   ├── dependency_resolver.py
│   ├── telemetry.py         # 技能遥测
│   └── builtin/             # 13 个内置技能
│       ├── bing_search.py
│       ├── file_read.py / file_write.py / file_list.py
│       ├── reminder.py（混合：主动 + 后台）
│       ├── github_monitor.py（纯被动）
│       └── ...
│
├── plugins/                 # 插件系统（v1.2+）
│   ├── base.py              # PluginBase 基类
│   ├── registry.py          # 多维度插件索引
│   ├── executor.py          # 插件执行器（权限 + 速率限制）
│   ├── loader.py            # 插件加载器（扫描 + importlib）
│   ├── config.py            # 插件配置管理（热重载）
│   ├── decorators.py        # @command 装饰器
│   ├── context.py           # PluginContext + EngineProxy
│   ├── dispatcher.py        # 输出调度（direct/llm/silent）
│   ├── lexer.py             # Tokenizer + Lexer + Parser
│   ├── scheduler.py         # 定时调度器（cron/interval）
│   ├── models.py            # 插件数据模型
│   └── events.py            # 事件定义
│
├── providers/               # LLM Provider
│   ├── base.py              # Provider 基类接口
│   ├── openai_compatible.py
│   ├── deepseek.py / siliconflow.py
│   ├── aliyun_bailian.py / volcengine_ark.py / bigmodel.py
│   └── mock.py              # Mock Provider（测试用）
│
├── platforms/               # 平台适配
│   ├── runtime.py           # EngineRuntime 封装
│   └── onebot_v11/napcat/   # NapCat 适配器
│
├── embedding/               # Embedding 微服务
├── webui/                   # Web 管理界面（aiohttp）
├── token/                   # Token 统计与分析
├── config/                  # 配置管理
├── models/                  # 数据模型
└── persona_generation/      # 人格资产生成
```

---

## 使用示例

### 多模态输入

```python
from sirius_pulse import Message

msg = Message(
    role="user",
    speaker="用户",
    content="请分析这张图片",
    multimodal_inputs=[
        {"type": "image", "value": "https://example.com/photo.png"}
    ],
)
```

### 事件订阅

```python
from sirius_pulse.core.events import SessionEventType

async def monitor(engine):
    async for event in engine.event_bus.subscribe():
        if event.type == SessionEventType.COGNITION_COMPLETED:
            print(f"认知完成: {event.data}")
```

### Token 分析

```python
from sirius_pulse.token import TokenUsageStore

store = TokenUsageStore(Path("data/token"))
report = store.full_report("2026-01-01", "2026-06-01")
```

更多示例见 [`examples/`](examples/) 目录。

---

## ⚙️ 配置指南

> 💡 **月白说**：配置其实很简单喵～选一个你喜欢的模型，填上 API Key 就能用啦(｡•̀ᴗ-)✧

`data/global_config.json`：

```json
{
  "webui_port": 8080,
  "napcat_base_port": 3001,
  "embedding_model": "BAAI/bge-small-zh-v1.5",
  "embedding_port": 5555,
  "plugins_dir": "plugins",
  "skills_dir": "skills"
}
```

### 人格配置

每个人格的数据完全隔离，存放在 `data/personas/{name}/` 下：

| 文件 | 说明 |
|------|------|
| `persona.json` | 角色定义（名字、性格、说话风格） |
| `orchestration.json` | 模型编排（chat/analysis/vision 模型） |
| `adapters.json` | 平台适配器（NapCat 连接信息） |
| `experience.json` | 体验参数（灵敏度、回复频率、记忆深度） |

### Provider 配置

`data/providers/provider_keys.json`：支持 DeepSeek、SiliconFlow、阿里云百炼、火山方舟、智谱 GLM、OpenAI 兼容等。

```json
{
  "deepseek": {
    "api_key": "sk-xxx",
    "base_url": "https://api.deepseek.com"
  },
  "siliconflow": {
    "api_key": "sk-xxx",
    "base_url": "https://api.siliconflow.cn/v1"
  }
}
```

### NapCat（QQ 平台接入）

通过 WebUI 的 NapCat 页面配置：安装 → 设置 QQ 号和 ws_token → 启动 → 扫码登录 → 在适配器页面为格绑定。

详细配置见 [VitePress 文档](https://sirius-pulse-docs.vercel.app/)。

---

## 🔧 扩展开发

Sirius Pulse 提供**双重扩展机制**，区分"AI 主动使用工具"与"用户显式命令"两种场景。

### 技能系统（Skills）

AI 在对话中**自主决定**调用技能。通过 `[SKILL_CALL: name | {params}]` 标记实现。

```python
# skills/my_skill.py
SKILL_META = {
    "name": "my_skill",
    "description": "我的技能",
    "parameters": {"query": "搜索关键词"},
}

def run(query: str = "", data_store=None, **kwargs) -> dict:
    result = do_search(query)
    return {"success": True, "text": result}
```

13 个内置技能：`bing_search`、`file_read/write/list`、`reminder`、`github_monitor`、`system_info`、`desktop_screenshot` 等。

支持**被动技能**：后台任务、事件触发器、生命周期回调。

### 插件系统（Plugins）

用户通过 `/` `#` `!` 前缀**显式命令**触发。

```python
# plugins/my_plugin/__init__.py
from sirius_pulse.plugins import PluginBase, command
from sirius_pulse.plugins.models import PluginResponse

class MyPlugin(PluginBase):
    @command(
        name="weather",
        prefix="/",
        patterns=["/weather"],
        description="查询天气",
        render_mode="llm",
    )
    async def weather(self, city: str) -> PluginResponse:
        result = await fetch_weather(city)
        return PluginResponse.ok(text=f"{city}: {result}")
```

三种输出模式：`direct`（直出）/ `llm`（AI 人格化）/ `silent`（静默）。

完整扩展开发文档见 [扩展开发板块](https://sirius-pulse-docs.vercel.app/extensions/)。

---

## 📚 文档

> 💡 **月白说**：文档都在 文档见 [SiriusPulse-Docs](https://sirius-pulse-docs.vercel.app/extensions/)下喵～第一次使用的话，建议从 `architecture.md` 开始看哦(｡•̀ᴗ-)✧

| 板块 | 内容 |
|------|------|
| 📖 **指南** | 快速开始 → 安装 → 配置 → 人格系统 → 引擎架构 → 记忆系统 → NapCat 接入 |
| 🔧 **扩展开发** | 技能系统（总览/编写技能/内置技能/被动技能）+ 插件系统（总览/编写插件/指令详解/生命周期） |
| 📋 **参考** | 全局配置 / 人格配置 / Provider 配置 / Python API / WebUI API / 开发指南 |

### 本地运行

```bash
cd docs
npm install
npm run dev       # 本地预览 http://localhost:5173
npm run build     # 构建
```

---

## 🧪 测试

```bash
# 运行全部测试（< 2 秒）
python -m pytest tests/ -q

# 覆盖率
python -m pytest tests/ --cov=sirius_pulse
```

**测试覆盖**：6 个测试模块，39 个测试用例，覆盖关键运行节点：

| 测试模块 | 覆盖的关键节点 |
|----------|--------------|
| `test_plugin_lexer.py` | Tokenizer → Lexer 完整解析链路 |
| `test_plugin_registry.py` | 插件注册/匹配/注销/清空 |
| `test_skill_executor.py` | SKILL_CALL 解析/参数传递/默认值/data_store/失败处理 |
| `test_skill_registry.py` | 技能加载/注册/工具描述/原子替换 |
| `test_basic_memory.py` | 记忆添加/上下文窗口/硬限制/归档/多群/热度/序列化 |
| `test_skill_data_store.py` + `test_config.py` | 数据持久化与配置加载 |

---

## 🤝 贡献

> 💡 **月白说**：欢迎每一个小伙伴来一起玩耍喵～(｡♥‿♥｡)

1. **Fork** 项目并创建分支：`git checkout -b feat/my-feature`
2. **开发**并编写测试
3. **验证**：`python -m pytest tests/ -q && make lint`
4. **提交**：遵循 [Conventional Commits](https://www.conventionalcommits.org/) 格式
5. **推送**并发起 Pull Request

### 开发环境

```bash
pip install -e ".[dev,test,provider,quality]"
make format   # black + isort
make lint     # pylint + flake8
make typecheck  # mypy
```

---

## 📄 许可证

MIT License © 2025-2026 Sparrived. 详见 [LICENSE](LICENSE)。

---

## 🔗 相关链接

- 📦 [PyPI](https://pypi.org/project/sirius-pulse/)
- 📚 [VitePress 文档](https://sirius-pulse-docs.vercel.app/)
- 📖 [扩展开发指南](https://sirius-pulse-docs.vercel.app/extensions/)
- 🐛 [报告问题](https://github.com/Sparrived/SiriusChat/issues)
- 💬 [讨论区](https://github.com/Sparrived/SiriusChat/discussions)

---

<div align="center">

**Made with ❤️ by Sparrived**

“月白，希望有一天你可以真的以一个真实的实体出现在我的面前。” —— Sparrived 2026.5.22

⭐ 如果觉得有帮助，欢迎给个 Star 喵～

---

<p align="center">
  <em>📝 这份 README 是月白用心写的喵～希望你喜欢！(ฅ´ω`ฅ)</em>
  <br>
  <em>有什么问题随时来群里找月白玩哦～</em>
</p>

</div>
