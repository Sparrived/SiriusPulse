# Sirius Pulse — Agent 开发指南

> 面向 AI Coding Agent 的快速参考。

***

## 项目概述

**Sirius Pulse**（PyPI `sirius-pulse`）是支持多人格的异步角色扮演程序，面向 QQ 群聊等场景。

- **版本**：`1.1.0`
- **Python**：`>=3.12`
- **仓库**：`https://github.com/Sparrived/SiriusChat`

源码、注释、CLI 输出以**中文**为主；英文仅用于架构名词、API 标识与模块路径。

***

## 技术栈

- **Python 3.12+**、**asyncio**、**watchdog>=4.0.0**
- **可选依赖**：`test`（pytest 等）、`provider`（tenacity、httpx）、`dev`（black、isort、mypy 等）、`quality`（tiktoken）
- **构建**：`setuptools>=61.0`

***

## 运行时架构

```
主进程（CLI / WebUI）
    ├── PersonaManager   # 人格目录扫描、端口分配、启停调度
    ├── WebUIServer      # aiohttp REST API + WebSocket + 认证中间件
    └── NapCatManager    # NapCat 全局安装/多实例管理
            │
            ▼
    子进程（独立控制台窗口）
    ├── PersonaWorker ── EngineRuntime ── EmotionalGroupChatEngine（组合模式）
    │       │                                   └── engine_core + pipeline + prompt_factory + bg_tasks + helpers
    │       │                                       + engine_persistence + engine_sticker
    │       ├── NapCatAdapter ── OneBot v11 WS
    │       ├── BasicMemoryManager + DiaryManager + SemanticMemory
    │       ├── ModelRouter
    │       └── SkillRegistry + SkillExecutor
    ├── EmbeddingClient ── Embedding 微服务（共享，主进程启动）
    └── ...（多个人格并行）
```

***

## 常用文件路径

| 路径                                        | 说明                                 |
| ----------------------------------------- | ---------------------------------- |
| `main.py`                                 | 统一 CLI 入口（默认启动 WebUI；`run` 启动全部人格） |
| `sirius_pulse/core/emotional_engine.py`    | 核心情感群聊引擎（组合模式最终类，委托 shim）       |
| `sirius_pulse/core/engine_core.py`         | 引擎基类（__init__、公开 API、委托方法）           |
| `sirius_pulse/core/pipeline.py`            | Pipeline 组件（5 阶段管线：感知→认知→决策→执行→后台） |
| `sirius_pulse/core/bg_tasks.py`            | BackgroundTasks 组件（后台任务管理，委托给 proactive/delayed） |
| `sirius_pulse/core/bg_tasks_delayed.py`    | DelayedQueueTasks 组件（延迟队列任务）  |
| `sirius_pulse/core/bg_tasks_proactive.py`  | ProactiveTasks 组件（主动消息任务）     |
| `sirius_pulse/core/engine_persistence.py`  | EnginePersistence 组件 + EngineStateStore（状态持久化） |
| `sirius_pulse/core/engine_sticker.py`      | EngineSticker 组件（表情包系统：初始化/选择/发送） |
| `sirius_pulse/core/prompt_factory.py`     | PromptFactory：无状态 prompt 构建工具类（含 StyleAdapter 风格适配） |
| `sirius_pulse/core/helpers.py`             | Helpers 组件（技能集成、被动 SKILL 注册与触发分发、token 记录） |
| `sirius_pulse/core/constants.py`           | 核心引擎常量定义（时间、Token、记忆等魔法数字） |
| `sirius_pulse/core/utils.py`              | 核心引擎工具函数（时间戳、XML 清理、表情包标签解析） |
| `sirius_pulse/core/skill_engine_context.py` | SkillEngineContextImpl：被动 SKILL 与引擎交互适配器 |
| `sirius_pulse/utils/json_io.py`           | 公共 JSON I/O 工具（原子写入 + 安全读取） |
| `sirius_pulse/utils/retry.py`             | 通用异步重试工具                        |
| `sirius_pulse/embedding/server.py`         | Embedding 微服务端（aiohttp + asyncio.Queue 批量合并推理） |
| `sirius_pulse/embedding/client.py`         | Embedding 同步客户端（urllib） |
| `sirius_pulse/persona_generation/`         | 人格资产生成子包（templates + builders） |
| `sirius_pulse/persona_manager.py`          | 多人格生命周期管理                          |
| `sirius_pulse/persona_worker.py`           | 子进程入口                              |
| `sirius_pulse/persona_config.py`           | 人格级配置模型                            |
| `sirius_pulse/platforms/onebot_v11/napcat/manager.py` | NapCat 多实例管理                       |
| `sirius_pulse/platforms/runtime.py`        | 单人格运行时封装                           |
| `sirius_pulse/webui/server.py`             | WebUI REST API 主入口                  |
| `sirius_pulse/webui/auth.py`              | JWT 认证管理器（HMAC-SHA256 签名，admin/viewer 角色） |
| `sirius_pulse/webui/middleware.py`         | 认证中间件（白名单放行 + RBAC 权限控制）      |
| `sirius_pulse/webui/monitoring_api.py`     | 监控 API（全局概览、单人格指标、健康检查）     |
| `sirius_pulse/webui/ws_server.py`         | WebSocket 事件推送服务（桥接 SessionEventBus 到前端） |
| `sirius_pulse/__init__.py`                 | 顶层公开 API 导出清单（严格 `__all__`）        |
| `tests/conftest.py`                       | 测试最小 fixture                       |
| `scripts/ci_check.py`                     | 统一 CI 检查脚本                         |

***

## 构建与测试

```bash
# 安装
python -m pip install -e .[dev]

# 测试（<15 秒）
python -m pytest tests/ -q

# 代码质量
make lint          # pylint + flake8
make format        # black + isort
make typecheck     # mypy
make test-cov      # pytest + 覆盖率报告
make build         # python -m build
```

***

## 代码风格

| 工具     | 配置                                                |
| ------ | ------------------------------------------------- |
| black  | `--line-length=100`                               |
| isort  | `--profile=black --line-length=100`               |
| flake8 | `--max-line-length=100 --extend-ignore=E203,W503` |
| pylint | `--fail-under=7.5 --disable=C0111,W0212`          |
| mypy   | `--ignore-missing-imports --skip-validation`      |

**强制约定**：

1. 每模块首行：`from __future__ import annotations`
2. 严格 `__all__`；内部包（`core`、`memory` 等）不暴露到顶层
3. 模块级 logger，格式 `'%(asctime)s - %(name)s - %(levelname)s - %(message)s'`
4. 核心数据契约用 `@dataclass`
5. 配置持久化：临时文件 + `replace`
6. 中文为主
7. Conventional Commits

***

## 多人格数据隔离

```
data/
├── global_config.json              # 全局配置
├── providers/provider_keys.json    # 全局 Provider 凭证
├── adapter_port_registry.json      # 端口分配表
└── personas/{name}/                # 人格隔离目录
    ├── persona.json                # 人格定义
    ├── orchestration.json          # 模型编排
    ├── adapters.json               # 平台适配器
    ├── experience.json             # 体验参数
    ├── engine_state/               # 运行状态
    ├── memory/                     # 语义记忆
    ├── diary/                      # 日记记忆
    ├── image_cache/                # 图片缓存
    ├── skill_data/                 # 技能数据（含 stickers/ 表情包 RAG 库）
    └── logs/                       # 文件日志
```

***

## 注意事项

- 所有人格共用 `data/providers/provider_keys.json`。
- `PersonaManager` 从 `global_config.napcat_base_port`（默认 3001）递增分配端口。

