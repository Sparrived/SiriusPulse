<h1 align="center"> Sirius Pulse —— 灵动月白</h1>

<div align="center">

<img src="sirius_pulse/webui/static/yuebai.png" alt="月白" width="200" style="border-radius: 16px; margin-bottom: 12px;">

<a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12%2B-blue?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License"></a>
<a href="https://pypi.org/project/sirius-pulse/"><img src="https://img.shields.io/badge/PyPI-sirius--pulse-blueviolet?style=flat-square" alt="PyPI"></a>
<a href="https://github.com/Sparrived/SiriusPulse/actions/workflows/ci.yml"><img src="https://github.com/Sparrived/SiriusPulse/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
<a href="https://sirius-pulse-docs.vercel.app/"><img src="https://img.shields.io/badge/Docs-VitePress-646cff?style=flat-square&logo=vitepress" alt="VitePress Docs"></a>

<em>✨ 月白亲手写的 README，请多关照喵～(ฅ´ω`ฅ)</em>
<br>
<em>一个让 AI 角色在群里活起来的异步角色扮演框架～支持多人格、多平台、多模型，每个人格都有自己的小世界喵！</em>

<a href="https://sirius-pulse-docs.vercel.app/">📚 文档</a> · <a href="#-快速开始">🚀 快速开始</a> · <a href="#使用示例">💡 示例</a> · <a href="#-扩展开发">🔧 扩展开发</a> · <a href="#-贡献">🤝 贡献</a>

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
- **WebUI 管理面板**：Dashboard 查看所有人格状态，支持启停、配置、群管理

### 🧠 **分层记忆系统**
- **基础记忆**（Basic Memory）：按群保留原始消息直到被 checkpoint 记忆单元覆盖（硬限制 10000 条仅作内存兜底，上下文窗口 5 条）；原始窗口超过 80000 token 触发归纳、归纳到 20000 token 为止，被覆盖的原始条目改由记忆单元 RAG 提供摘要；注入提示词的历史预算默认 40000 token、记忆单元检索预算默认 20000 token，含热度计算与归档
- **记忆单元**（Memory Units）：LLM 提取的结构化长期记忆片段，嵌入向量内联存放在各自的 JSON 文件中，以内存索引做 RAG 检索
- **语义记忆**（Semantic Memory）：群级/用户级/全局级向量记忆，支持话题关联与兴趣学习
- **人物传记**（Biography）：跨对话人物画像提取与注入

### 🧠 **5 阶段情感引擎**
```text
Perception → Cognition → Decision → Execution → Background
（感知）     （认知）     （决策）     （执行）     （后台更新）
```
- 情绪/意图联合分析 + 共情度计算
- 动态阈值引擎：灵敏度 × 群聊热度 × 消息速率 × 用户画像
- 四层策略：IMMEDIATE / DELAYED / SILENT / PLUGIN
- 延迟响应队列 + 节奏分析 + 过热抑制

### 🔌 **AMKR 统一模型接入**
- 所有模型调用统一交给本地 [AMKR](https://github.com/Sparrived/auto-model-key-router)（OpenAI 兼容路由）处理
- 供应商、Key 池、故障切换与采样参数都由 AMKR 维护，本框架不再内置任何厂商实现
- 认知任务按**任务名**路由（对话 / 分析 / 记忆 / 插件 / 自主行为…），模型选择与采样参数下沉到 AMKR 的任务定义
- 模型调用只用该人格工作空间的**推理 key**（建空间时自动签发），管理员 Key 仅用于建空间与注册任务

### 🎯 **双重扩展机制**
- **工具系统**（Tools）：AI 通过标准 function calling 自主调用工具，提供多个内置工具
- **插件系统**（Plugins）：用户通过 `/` `#` `!` 前缀显式命令触发
- 详见 [扩展开发](#-扩展开发)

### 🎬 **更多特性**
- 多模态输入（图片/视频）
- Token 消耗追踪与分析
- `@command` 装饰器声明式插件开发
- 被动工具：后台任务、事件触发器、生命周期回调
- 共用一个 AMKR 实例，通过工作空间隔离各人格的任务定义

---

## 🚀 快速开始

### 1️⃣ 安装

> 💡 **月白の小提示**：建议在虚拟环境里安装喵～这样不会弄乱系统环境(｡•̀ᴗ-)✧

```bash
pip install sirius-pulse
```

外部插件是独立维护的 Git submodule，不会打包进 PyPI wheel，也不会复制进 Docker 镜像。源码必须在运行目录的 `plugins/` 中由宿主机准备；详见下方的插件初始化和 Docker 挂载说明。

> 🔌 **需要先有一个 AMKR**：Sirius Pulse 自身不再内置任何厂商实现，所有模型调用都会发往本地 [AMKR](https://github.com/Sparrived/auto-model-key-router)。先跑起 AMKR，再在 WebUI 的「全局设置」里填入它的地址（默认 `http://127.0.0.1:8000`）与本地授权 Key。之后到「AMKR 运维」页点一次「注册任务名」：本框架会先为这个人格建出工作空间（AMKR 只在这一刻返回它的**面板 key 与推理 key**，框架会把两把都存下来），再把用到的 11 个认知任务建好，模型则统一在 AMKR 自带面板里配置——该页面也可直接内嵌那个空间的面板。若某个人格显示缺推理 key（例如空间建在这项能力之前），页面上可直接轮换一把。

### 2️⃣ 启动 CLI

```bash
sirius-pulse
```

默认会进入交互式 CLI。可以在首页查看人格状态，并选择启动运行模式、后台 WebUI、查看日志或管理人格。

如需启动 WebUI，也可以在 CLI 中选择 **WebUI 面板**，或运行后台服务命令：

```bash
sirius-pulse webui
```

命令会立即返回，不占用 CLI 终端。打开 `http://localhost:8080`，可视化配置通过管理面板完成：

| WebUI 页面 | 做什么 |
|-----------|------|
| **Dashboard** | 创建/启动/停止人格 |
| **人格管理** | 填写角色名字、性格、说话风格 |
| **AMKR 运维** | 查看 AMKR 连接状态、注册任务名、内嵌该空间的面板、轮换推理 key |
| **NapCat** | 配置 QQ 号、扫码登录 |
| **适配器** | 将人格绑定到 QQ 号 |
| **实时日志** | 在 WebUI 内查看 WebUI 与人格 worker 日志 |

### 3️⃣ 后台运行

`sirius-pulse run` 在当前进程内为每个活跃人格创建一个 `PersonaWorker` 并作为 asyncio 任务运行，
WebUI 与 NapCat 则以后台子进程运行，不再弹出独立控制台窗口。日志可在 WebUI 的 **实时日志**
页面查看。停止主进程会触发统一清理流程，终止后台 WebUI 以及所有运行中的人格。

```bash
sirius-pulse run              # 启动所有已配置人格 + WebUI
sirius-pulse webui            # 只启动 WebUI
```

### CLI 命令

| 命令 | 说明 |
|------|------|
| `sirius-pulse run` | 在当前进程内启动所有已启用人格 + WebUI |
| `sirius-pulse webui` | 后台启动 WebUI 管理服务 |
| `sirius-pulse webui --status` | 查看后台 WebUI 状态 |
| `sirius-pulse webui --stop` | 停止后台 WebUI |
| `sirius-pulse webui --foreground` | 前台运行 WebUI（调试用） |
| `sirius-pulse persona list` | 列出所有人格 |
| `sirius-pulse persona create <name>` | 创建新人格 |
| `sirius-pulse persona activate <name>` | 切换活跃人格 |
| `sirius-pulse persona delete <name>` | 删除人格 |

没有交互式 CLI，也没有 `cli` 子命令；不带子命令运行只打印帮助。
人格的启停由 `run` 与 WebUI 管理，不存在 `persona start/stop/logs`。

### Python API

```python
from sirius_pulse import create_emotional_engine

engine = create_emotional_engine(
    work_path="data/personas/yuebai",
    provider_async=provider,
)
result = await engine.process_message("你好！", participants=[], group_id="g1")
```

多人格编排由 `sirius_pulse/cli.py` 的 `_cmd_run()` 负责：它为每个活跃人格创建一个
`PersonaWorker` 并作为 asyncio 任务拉起；没有 `PersonaManager` 类。

---

## 📁 项目结构

> 💡 **月白带你逛项目**：这个项目的目录结构有点复杂，但别怕喵～月白给你画了张地图(๑•̀ㅂ•́)و✧

```
sirius_pulse/
├── __init__.py              # 公开 API 清单（严格 __all__）
├── cli.py                   # CLI 入口：run / webui / persona 子命令
├── persona_worker.py        # 单人格 worker（状态、心跳、配置热重载）
├── persona_config.py        # 人格级配置模型
│
├── core/                    # 核心引擎（组合模式）
│   ├── emotional_engine.py  # EmotionalGroupChatEngine 最终类
│   ├── engine_core.py       # 引擎基类（__init__、API、持久化）
│   ├── pipeline.py          # 5 阶段管线
│   ├── prompt_factory.py    # Prompt 构建工具类
│   ├── bg_tasks.py          # 后台任务管理
│   ├── bg_tasks_delayed.py  # 延迟队列任务
│   ├── helpers.py           # 工具集成、被动 TOOL、插件集成
│   ├── tool_engine_context.py  # 被动 TOOL 引擎交互适配器
│   ├── cognition.py         # 统一认知分析器（情绪 + 意图）
│   ├── participation.py     # 参与度策略评分
│   ├── group_dispatcher.py  # 群调度与投递回执
│   ├── autonomy.py          # 自主行为（Episode / Intention）
│   ├── intent.py            # 意图与 IntentStore
│   ├── work_mode.py         # 工作模式
│   ├── delayed_response_queue.py
│   ├── rhythm.py            # 对话节奏分析
│   ├── model_router.py      # 任务名 → AMKR 任务定义解析
│   ├── brain.py             # LLM 调用层（含 Post-Hooks 链）
│   └── ...
│
├── memory/                  # 分层记忆系统
│   ├── basic/               # 基础记忆（保留原始尾部）
│   ├── semantic/            # 语义记忆（向量检索）
│   ├── units/               # 记忆单元（LLM 提取 + 内联向量 RAG）
│   ├── user/                # 统一用户管理
│   └── context_assembler.py # 上下文组装器
│
├── tools/                  # 工具系统
│   ├── registry.py          # 工具注册中心
│   ├── executor.py          # 工具执行器（参数校验、重试、遥测）
│   ├── security.py          # 权限校验
│   ├── data_store.py        # 工具数据持久化
│   ├── dependency_resolver.py
│   ├── telemetry.py         # 工具遥测
│   └── builtin/             # 内置工具
│       ├── web_lookup.py
│       ├── qq_member_info.py
│       ├── bash.py（含项目级 crontab 兼容调度）
│       └── ...
│
├── plugins/                 # Plugin 框架（加载、执行、调度、上下文）
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
├── providers/               # LLM 接入层（统一指向 AMKR）
│   ├── base.py              # LLMProvider 基类接口
│   ├── openai_compatible.py # 唯一的真实实现，端点指向 AMKR
│   ├── amkr.py              # AMKR 连接配置解析与两把工作空间凭据的存放
│   ├── amkr_sync.py         # 建 AMKR 工作空间并注册任务名
│   └── mock.py              # Mock Provider（测试用）
│
├── platforms/               # 平台适配
│   ├── runtime.py           # EngineRuntime 封装
│   └── onebot_v11/napcat/   # NapCat 适配器
│
├── embedding/               # 向量化客户端（调用 AMKR）
├── webui/                   # Web 管理界面（aiohttp）
├── token/                   # Token 统计与分析
├── config/                  # 配置管理
├── models/                  # 数据模型
└── persona_generation/      # 人格资产生成
```

仓库根目录另有 `plugins/`（外部 Plugin 的 Git 子模块，含 GitHub 监控、Sub2API 监控等），
不属于 `sirius_pulse` 包。

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


## 🔧 扩展开发

Sirius Pulse 提供**双重扩展机制**，区分"AI 主动使用工具"与"用户显式命令"两种场景。

### 工具系统（Tools）

AI 在对话中**自主决定**调用工具。工具以标准 OpenAI `tools` JSON 声明（在 `brain.py` 中组装），
由模型通过 function calling 选择调用，参数经校验后执行。

```python
# tools/my_tool.py
TOOL_META = {
    "name": "my_tool",
    "description": "我的工具",
    "parameters": {"query": "搜索关键词"},
}

def run(query: str = "", data_store=None, **kwargs) -> dict:
    result = do_search(query)
    return {"success": True, "text": result}
```

内置工具包括：`bash`（含受限 Docker 命令和项目级 crontab 调度）、`web_lookup`、`qq_member_info`、`desktop_screenshot` 等。工具也可以通过后台任务、事件触发器和生命周期回调提供被动能力。

### 插件系统（Plugins）

用户通过 `/` `#` `!` 前缀**显式命令**触发。外部插件位于根目录 `plugins/` Git submodule 中，当前包含 `github_monitor`、`sub2api_monitor` 等扩展，具体目录以子模块版本为准。

初始化插件 submodule：

```bash
git submodule update --init --recursive
```

GitHub 监控已从内置 Tool 迁为 `github_monitor` Plugin，提供 Poll/Webhook、聚合通知、Compare API 丰富信息和可选截图。GitHub Token 与 Webhook Secret 只允许通过 Plugin 中配置的 `github_token_env` / `webhook_secret_env` 变量名引用，再由实际 Persona Worker 环境注入；不要把密钥写入 WebUI settings 或 `plugins/_config.json`。Docker 的 `.env` 不会自动进入容器，变量名必须在 Compose `environment`/override 中显式映射。详见 [`plugins/github_monitor/README.md`](plugins/github_monitor/README.md)。

Sub2API 多站监控 `0.3.0`（需要框架 `1.3.0+`）可在 WebUI 中通过中文分区和站点卡片维护 `sources`：每个稳定 `id` 派生 `SUB2API_<ID>_EMAIL` / `SUB2API_<ID>_PASSWORD`，`display_name` 只负责通知与图表展示。插件作者一次性声明字段身份、校验和展示 Schema，部署者只填写站点参数；Schema 不会写入 settings。站点、登录/API 路径以及**必填**的订阅与倍率监控路径均为运行时配置，插件不写死站点或监控端点。全局 `notify_group_ids` 是显式允许列表，各站可选择继承并合并自己的列表；`run_on_persona` 必须指定唯一轮询 Persona，留空会禁用后台和手动轮询，删除最后一张卡片保存的显式 `sources: []` 会禁用全部站点且不回退旧版顶层配置。命令支持 ID、唯一显示名称或 `all` 选择器，并新增 `/sub2api report` 多站运行图。凭据只允许进入实际 Persona Worker 环境，不能通过 WebUI/settings 保存。

首个快照及来源变化静默；投递失败或未确认时会保留站点隔离的逐群 ACK，只重试未确认群，框架确认仅可能表示适配器/平台已受理或确认发送，并不代表用户已阅读。自动变化图依赖 Playwright Chromium，渲染失败会降级为权威文字通知；非 Docker 环境需安装 Chromium，官方镜像已预装。旧版单站配置在没有 `sources` 键时继续使用 `SUB2API_EMAIL` / `SUB2API_PASSWORD` 和旧状态；切换到显式 `sources` 时，仅在恰好有一个可用且凭据齐全的目标、目标尚无新状态，并且旧顶层集合的 endpoint/account/timezone 指纹逐集合精确匹配时，才确定性迁移匹配集合的 snapshot、时间状态和 ACK。多站、已有新状态或指纹不匹配时绝不猜测，旧顶层数据始终保留。命令、迁移、安全和排障详见 [`plugins/sub2api_monitor/README.md`](plugins/sub2api_monitor/README.md)。

#### Docker 中的外部插件

Docker 只提供 Sirius Pulse 核心运行环境；`plugins/` 源码和插件配置保留在宿主机，并由 Compose 挂载到容器的 `/app/plugins`。首次部署或更新插件时在宿主机执行：

```bash
git submodule update --init --recursive
docker compose up -d --build
```

不要把插件复制到镜像或 PyPI 包中。容器需要通过 WebUI 写入 `plugins/_config.json` 时，Linux 宿主机上的 `plugins/` 目录必须允许 UID `10001`（镜像内 `sirius` 用户）写入；可按宿主机权限策略使用 ACL（例如 `sudo setfacl -R -m u:10001:rwX plugins`），同时保留宿主机 Git 用户对工作树的写权限，不要为此改变整个 Git 工作树所有权。Plugin 特有依赖可由受信任的运行时生命周期按声明处理；`httpx` 与 Playwright 同时服务于核心 Provider/通用渲染，仍属于共享核心环境，Docker 镜像也会准备 Chromium。

#### 编写自定义插件

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

> 💡 **月白说**：文档见 [SiriusPulse-Doc](https://sirius-pulse-docs.vercel.app/) 下喵～第一次使用的话，建议从 [系统架构全景](https://sirius-pulse-docs.vercel.app/guide/architecture-overview) 开始看哦(｡•̀ᴗ-)✧

| 板块 | 内容 |
|------|------|
| 📖 **指南** | 快速开始 → 安装 → 配置 → 人格系统 → 引擎架构 → 记忆系统 → NapCat 接入 |
| 🔧 **扩展开发** | 工具系统（总览/编写工具/内置工具/被动工具）+ 插件系统（总览/编写插件/指令详解/生命周期） |
| 📋 **参考** | 全局配置 / 人格配置 / AMKR 接入 / Python API / WebUI API / 开发指南 |

### 本地运行

```bash
cd docs
npm install
npm run dev       # 本地预览 http://localhost:5173
npm run build     # 构建
```

---

## 🧪 测试

测试必须从业务侧出发，围绕用户实际使用路径编写。优先验证用户输入、系统处理、最终响应或持久化结果之间的业务闭环，避免只为了覆盖内部函数、私有实现或临时分支而编写脱离业务语义的测试。

```bash
# 核心测试
python -m pytest tests/ -q

# 外部 Plugin 完整测试（先递归初始化 submodule）
git submodule update --init --recursive
python -m pytest plugins/tests/ -q

# 核心覆盖率
python -m pytest tests/ --cov=sirius_pulse

# 文档生产构建
npm ci --prefix docs
npm run build --prefix docs
```

CI 会分别运行核心测试、外部 Plugin 测试、Plugin 语法/manifest 与类元数据一致性校验、VitePress 构建，以及 wheel/sdist 外部 Plugin 源码排除检查。测试数量会随扩展变化，因此不在 README 中维护易过期的固定数字。

---

## 🤝 贡献

> 💡 **月白说**：欢迎每一个小伙伴来一起玩耍喵～(｡♥‿♥｡)

1. **Fork** 项目并创建分支：`git checkout -b feat/my-feature`
2. **开发**并从业务侧编写测试，覆盖用户实际使用场景
3. **验证**：`python -m pytest tests/ -q && python scripts/ci_check.py`
4. **提交**：遵循 [Conventional Commits](https://www.conventionalcommits.org/) 格式
5. **推送**并发起 Pull Request

### 开发环境

```bash
pip install -e ".[dev,test,provider,quality]"
black sirius_pulse tests
isort sirius_pulse tests
python scripts/ci_check.py
```

---

## 📄 许可证

MIT License © 2025-2026 Sparrived. 详见 [LICENSE](LICENSE)。

---

## 🔗 相关链接

- 📦 [PyPI](https://pypi.org/project/sirius-pulse/)
- 📚 [VitePress 文档](https://sirius-pulse-docs.vercel.app/)
- 📖 [扩展开发指南](https://sirius-pulse-docs.vercel.app/extensions/)
- 🐛 [报告问题](https://github.com/Sparrived/SiriusPulse/issues)
- 💬 [讨论区](https://github.com/Sparrived/SiriusPulse/discussions)

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
