# Sirius Chat — Agent 开发指南

> 面向 AI Coding Agent 的快速参考。

***

## 项目概述

**Sirius Chat**（PyPI `sirius-chat`）是支持多人格的异步角色扮演程序，面向 QQ 群聊等场景。

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
    ├── WebUIServer      # aiohttp REST API + 静态页面
    └── NapCatManager    # NapCat 全局安装/多实例管理
            │
            ▼
    子进程（独立控制台窗口）
    ├── PersonaWorker ── EngineRuntime ── EmotionalGroupChatEngine（Mixin 架构）
    │       │                                   └── engine_core + pipeline + prompt_factory + bg_tasks + helpers
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
| `sirius_chat/core/emotional_engine.py`    | v1.0 核心情感群聊引擎（Mixin 最终类）       |
| `sirius_chat/core/engine_core.py`         | 引擎基类（__init__、API、持久化）           |
| `sirius_chat/core/pipeline.py`            | 5 阶段管线 Mixin                        |
| `sirius_chat/core/bg_tasks.py`            | 6 个后台任务 Mixin                       |
| `sirius_chat/core/prompt_factory.py`     | PromptFactory：无状态 prompt 构建工具类（含 StyleAdapter 风格适配） |
| `sirius_chat/core/helpers.py`             | 技能集成、被动 SKILL 注册与触发分发、token 记录 Mixin |
| `sirius_chat/core/skill_engine_context.py` | SkillEngineContextImpl：被动 SKILL 与引擎交互适配器 |
| `sirius_chat/embedding/server.py`         | Embedding 微服务端（aiohttp + asyncio.Queue 批量合并推理） |
| `sirius_chat/embedding/client.py`         | Embedding 同步客户端（urllib） |
| `sirius_chat/persona_generation/`         | 人格资产生成子包（templates + builders） |
| `sirius_chat/persona_manager.py`          | 多人格生命周期管理                          |
| `sirius_chat/persona_worker.py`           | 子进程入口                              |
| `sirius_chat/persona_config.py`           | 人格级配置模型                            |
| `sirius_chat/platforms/onebot_v11/napcat/manager.py` | NapCat 多实例管理                       |
| `sirius_chat/platforms/runtime.py`        | 单人格运行时封装                           |
| `sirius_chat/webui/server.py`             | WebUI REST API                     |
| `sirius_chat/__init__.py`                 | 顶层公开 API 导出清单（严格 `__all__`）        |
| `tests/conftest.py`                       | 测试最小 fixture                       |
| `scripts/ci_check.py`                     | 统一 CI 检查脚本                         |
| `docs/architecture.md`                    | 架构边界与模块交互权威文档                      |

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

