# WebUI 管理面板

> **v1.0 新增** — 基于 aiohttp 的 REST API + 静态页面，统一管理多人格、NapCat 和全局配置。

## 一句话定位

WebUI 是 Sirius Pulse 的**控制平面**。通过一个浏览器页面，你可以创建人格、启动/停止子进程、查看日志、配置 Provider、管理 NapCat 实例，而无需手动编辑 JSON 文件。

## 为什么需要它

在多人格架构下，手动管理多个 `data/personas/{name}/` 目录下的 JSON 配置文件容易出错。WebUI 提供：
- 人格的 CRUD 和生命周期控制（创建、启动、停止、删除）
- 实时状态监控（进程是否存活、心跳时间、adapter 状态）
- Provider 和模型编排的图形化配置
- 日志实时查看
- Token 用量统计
- NapCat 安装与启停

## 技术栈

- **后端**：aiohttp（asyncio HTTP 框架）
- **前端**：纯 HTML/CSS/JS（无框架），内嵌在 `webui/static/` 中
- **数据层**：直接读写 `data/personas/{name}/` 下的 JSON 文件
- **进程控制**：通过注入的 `PersonaManager` 实例调用 subprocess 管理

## 核心类：WebUIServer

### 初始化

```python
WebUIServer(
    persona_manager: PersonaManager,
    host: str = "0.0.0.0",
    port: int = 8080,
    napcat_install_dir: str | None = None,
)
```

`persona_manager` 是核心依赖，所有人格操作都委托给它。如果提供 `napcat_install_dir`，服务器还会初始化 `NapCatManager` 用于 NapCat 管理。

### 生命周期

```python
await server.start()   # 创建 AppRunner + TCPSite，绑定端口
...
await server.stop()    # 清理站点和 runner
```

---

## API 路由总览

### 人格管理

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/personas` | GET | 列出所有人格及其状态 |
| `/api/personas` | POST | 创建新人格 |
| `/api/personas/{name}` | GET | 获取单个人格完整状态 |
| `/api/personas/{name}` | DELETE | 删除人格 |
| `/api/personas/{name}/start` | POST | 启动人格子进程 |
| `/api/personas/{name}/stop` | POST | 停止人格子进程 |
| `/api/personas/{name}/logs` | GET | 读取 worker.log 最近 N 行 |

### 人格配置

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/personas/{name}/profile` | GET/POST | 人格定义（persona.json） |
| `/api/personas/{name}/orchestration` | GET/POST | 模型编排（orchestration.json） |
| `/api/personas/{name}/adapters` | GET/POST | 平台适配器（adapters.json） |
| `/api/personas/{name}/experience` | GET/POST | 体验参数（experience.json） |
| `/api/personas/{name}/keywords` | POST | 根据关键词生成人格 |
| `/api/personas/{name}/interview` | GET/POST | 访谈式人格生成 |

### 引擎控制

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/personas/{name}/engine/toggle` | POST | 启用/禁用引擎（写 `engine_state/enabled`） |
| `/api/personas/{name}/engine/reload` | POST | 通知 worker 重新加载引擎 |

### 全局配置

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/global-config` | GET/POST | 读写 `data/global_config.json` |
| `/api/providers` | GET/POST | 全局 Provider 配置（provider_keys.json） |
| `/api/models` | GET | 所有可用模型列表（去重） |

### NapCat 管理

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/napcat/status` | GET | NapCat 安装/运行/QQ 状态 |
| `/api/napcat/install` | POST | 下载安装 NapCat |
| `/api/napcat/configure` | POST | 为指定人格配置 NapCat |
| `/api/napcat/start` | POST | 启动 NapCat 实例 |
| `/api/napcat/stop` | POST | 停止 NapCat 实例 |
| `/api/napcat/logs` | GET | 读取 NapCat 日志 |

### Token 统计

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/tokens` | GET | 全局 Token 用量汇总（跨所有人格） |
| `/api/personas/{name}/tokens` | GET | 单个人格 Token 用量详情 |

### 认知分析

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/personas/{name}/cognition` | GET | 认知事件列表 + 情感分布。支持 `?group_id=` 按群筛选，`?limit=` 限制条数 |

### 用户画像

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/personas/{name}/users` | GET | 用户语义画像列表。支持 `?group_id=` 按群筛选 |
| `/api/personas/{name}/users/{user_id}` | GET | 单用户画像详情。支持 `?group_id=` |

### 遥测

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/telemetry` | GET | 技能调用统计（跨所有人格的 `.telemetry.jsonl`） |

---

## 启动人格时的特殊逻辑

当 WebUI 收到启动人格请求时，流程比普通 `persona_manager.start_persona()` 更复杂：

1. 检查该人格的 `adapters.json` 是否配置了 NapCat
2. 若有 NapCat 配置：
   - 确保 NapCat 全局二进制已安装（否则自动安装）
   - 为该人格创建 NapCat 实例目录
   - 生成 NapCat 配置文件
   - 启动 NapCat 进程
   - `wait_for_ws()` 等待 WebSocket 就绪（最多 120 秒）
3. 调用 `persona_manager.start_persona(name)` 启动 worker 子进程
4. 更新 `napcat_instance_registry.json` 记录实例映射

停止人格时则反向执行：先停止 worker，再停止 NapCat 实例。

---

## 静态页面

前端页面位于 `sirius_pulse/webui/static/`，由 aiohttp 直接 serve：

- **Dashboard** — 系统状态总览（WebUI 运行状态、人格 worker 状态）
- **Personas** — 人格列表、创建、启停、删除、日志查看
- **Config** — 全局配置编辑、Provider 管理、模型选择
- **Experience** — 行为风格配置（回复模式、敏感度、活泼度、四象限图预览）、主动行为、回复控制、技能与资源、记忆与身份
- **Cognition** — 认知分析面板：12维指向性雷达图、情感状态时间线、情感分布（支持群筛选）、最近认知事件流水
- **Users** — 用户画像：关系状态、兴趣图谱、群聊筛选
- **Diary** — 日记记忆查看与关键词筛选
- **Token Tracker** — Token 用量统计
- **Logs** — 日志查看面板

所有 API 调用使用原生 `fetch()`，无前端框架依赖。

---

## 与其他系统的关系

| 交互对象 | 方式 |
|---------|------|
| **PersonaManager** | 注入为构造参数；所有人格 CRUD 和生命周期都委托给它 |
| **NapCatManager** | 可选注入；用于 NapCat 安装/配置/启停/日志 API |
| **PersonaStore / OrchestrationStore** | 直接读写 `persona.json` / `orchestration.json` |
| **PersonaGenerator / persona_utils** | 关键词生成和访谈式人格生成 |
| **ProviderRegistry** | 通过 `WorkspaceProviderManager` 读写全局 provider_keys.json |
| **TokenUsageStore** | 遍历所有人格的 `token/token_usage.db` 进行汇总查询 |
| **SkillTelemetry** | 聚合所有人格的 `.telemetry.jsonl` 技能调用记录 |
