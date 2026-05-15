# 多人格生命周期管理

> **v1.0 核心架构** — 主进程调度多个人格子进程，每个人格独立运行、独立数据、独立 NapCat 实例。

## 一句话定位

`PersonaManager` + `PersonaWorker` 构成 Sirius Chat 的**多人格进程模型**。主进程负责扫描、创建、启停和监控；子进程负责加载配置、运行引擎、维护心跳。

## 为什么需要它

v1.0 之前，Sirius Chat 是单 workspace 单会话模式。当用户需要同时运行多个不同角色（比如"月白"和" Sirius"）时，必须手动启动多个进程并管理端口冲突。v1.0 引入多人格架构后：

- 每个人格拥有独立的 `data/personas/{name}/` 目录（人格定义、记忆、日志完全隔离）
- 每个人格运行在一个独立的 Python 子进程中，崩溃不影响其他人格
- `PersonaManager` 自动分配 NapCat WebSocket 端口，避免冲突
- 通过 WebUI 或 CLI 统一管理所有人格的启停和状态

## 架构总览

```
主进程（python main.py run）
    │
    ├── PersonaManager
    │       ├── 扫描 data/personas/ 目录
    │       ├── 维护 data/adapter_port_registry.json
    │       ├── create_persona() / remove_persona()
    │       ├── start_persona() / stop_persona()
    │       └── get_status() / get_logs()
    │
    └── WebUIServer ── REST API ── 调用 PersonaManager

子进程（python -m sirius_chat.persona_worker --config {dir}）
    │
    └── PersonaWorker
            ├── 加载 adapters.json / experience.json / persona.json
            ├── 创建 EngineRuntime（懒加载 EmotionalGroupChatEngine）
            ├── 创建 NapCatAdapter（每 adapter 一个）
            ├── 注册 skill bridge
            └── 心跳循环（每 10 秒写入 worker_status.json）
```

---

## PersonaManager（主进程调度器）

**定位**：多人格的单一权威。知道每个人格的文件在哪、占用了哪个端口、进程是否存活。

### 核心能力

| 能力 | 方法 | 说明 |
|------|------|------|
| **扫描发现** | `list_personas()` | 读取 `data/personas/` 下所有子目录，返回元数据列表 |
| **深度检查** | `_inspect_persona(name)` | 解析 `adapters.json`、`experience.json`、心跳状态、PID 存活 |
| **创建人格** | `create_persona(name, ...)` | 创建目录树 + 默认 `persona.json`/`adapters.json`/`experience.json`/`orchestration.json` + 分配端口 |
| **迁移旧版** | `migrate_persona(source_dir, name)` | 从旧版单 workspace 布局推断 QQ 号/端口，迁移到新人格目录 |
| **删除人格** | `remove_persona(name)` | 先停止子进程，再 `shutil.rmtree`，释放端口 |
| **启动人格** | `start_persona(name)` | `subprocess.Popen` 启动 `python -m sirius_chat.persona_worker --config {dir}` |
| **停止人格** | `stop_persona(name)` | 先发送 `CTRL_BREAK_EVENT`/SIGTERM，等待 10 秒，再 `kill()` |
| **批量启停** | `start_all()` / `stop_all()` | 遍历所有 enabled 人格 |
| **健康检查** | `is_running(name)` | 检查 `proc.poll()` → PID 存活 → 命令行含 `persona_worker` → 心跳时间 < 60s |
| **日志读取** | `get_logs(name, lines=50)` | tail `logs/worker.log` |

### 端口分配

`PersonaManager` 维护 `data/adapter_port_registry.json`：

```json
{
  "月白": {"port": 3001, "leased_at": 1714492800.0},
  "Sirius": {"port": 3002, "leased_at": 1714492900.0}
}
```

- 从 `global_config.napcat_base_port`（默认 3001）递增分配
- 60 秒 lease：即使 OS 报告端口空闲，60 秒内仍视为占用（降低多进程竞争）
- 人格删除时自动释放

### 进程模型（Windows）

```python
subprocess.Popen(
    cmd,
    creationflags=subprocess.CREATE_NEW_CONSOLE,
    ...
)
```

`CREATE_NEW_CONSOLE` 为每个人格打开独立的控制台窗口，方便查看单个人格的日志输出。

---

## PersonaWorker（子进程入口）

**定位**：单个人格的"容器"。加载一份配置，运行一个事件循环，直到收到关闭信号。

### 启动流程

1. **解析命令行**：`--config {persona_dir}` `--log-level INFO`
2. **加载配置**：读取 `adapters.json`（平台适配器）、`experience.json`（体验参数）
3. **自动发现 peer AI**：扫描兄弟人格目录，读取它们的 `adapters.json`，将其他人格的 QQ 号注入 `peer_ai_ids`（防止 AI 之间互相@造成混乱）
4. **构建 plugin_config**：将 `PersonaExperienceConfig` 平铺为 `EngineRuntime` 所需的字典格式
5. **创建 EngineRuntime**：`EngineRuntime(persona_dir, plugin_config, global_data_path)`
6. **启动适配器**：对每个 enabled 的 `NapCatAdapterConfig`：
   - 创建 `NapCatAdapter`（WebSocket 客户端，含事件处理）
   - `adapter.start()` 连接 WebSocket 并启动后台投递循环
   - `runtime.add_skill_bridge('napcat', adapter)` 注册平台桥接
7. **心跳循环**：每 10 秒写入 `engine_state/worker_status.json`
8. **阻塞等待**：`asyncio.Event` 等待关闭信号

### 心跳文件

`{persona_dir}/engine_state/worker_status.json`：

```json
{
  "status": "running",
  "pid": 12345,
  "heartbeat_at": "2026-05-01T10:00:00+08:00"
}
```

`PersonaManager` 通过读取该文件判断子进程健康状态。

### 关闭流程

1. 收到 SIGTERM/SIGINT → `shutdown()` 触发事件
2. 取消心跳任务
3. 停止所有 bridge → 关闭所有 adapter
4. 停止 runtime（保存状态、停止后台任务）
5. 写入 `"status": "stopped"` 到 worker_status.json

---

## PersonaConfig（人格级配置模型）

**定位**：人格目录下四个 JSON 文件的 schema 与持久化权威。

### 四个配置文件

| 文件 | 模型类 | 内容 |
|------|--------|------|
| `persona.json` | `PersonaProfile` | 人格定义（名字、性格、说话方式、情绪基线等） |
| `orchestration.json` | `OrchestrationStore` | 模型编排（analysis/chat/vision 模型） |
| `adapters.json` | `PersonaAdaptersConfig` | 平台适配器列表（NapCat WS URL、QQ 号、允许群号等） |
| `experience.json` | `PersonaExperienceConfig` | 体验参数（回复模式、敏感度、主动发言间隔、技能开关等） |

### PersonaAdaptersConfig

```python
@dataclass(slots=True)
class NapCatAdapterConfig:
    type: str                    # "napcat"
    enabled: bool
    ws_url: str                  # "ws://localhost:3001"
    token: str
    qq_number: str
    allowed_group_ids: list[str]
    allowed_private_user_ids: list[str]
    peer_ai_ids: list[str]       # 其他 AI 的 QQ 号，用于过滤
    enable_group_chat: bool
    enable_private_chat: bool
```

### PersonaExperienceConfig

控制运行时的行为参数：

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `reply_mode` | `"auto"` | 回复模式：auto / never / mention_only |
| `engagement_sensitivity` | `0.5` | 回复敏感度（0~1） |
| `heat_window_seconds` | `60.0` | 热度计算窗口 |
| `proactive_enabled` | `true` | 是否允许主动发言 |
| `proactive_interval_seconds` | `300.0` | 主动发言检查间隔 |
| `delay_reply_enabled` | `true` | 是否启用延迟回复 |
| `pending_message_threshold` | `4.0` | 延迟队列合并阈值 |
| `reply_frequency_max_replies` | `8` | 每窗口最大回复数 |
| `max_concurrent_llm_calls` | `1` | 最大并发 LLM 调用 |
| `enable_skills` | `true` | 是否启用 SKILL 系统 |
| `max_skill_rounds` | `3` | 单轮最大 SKILL 调用次数 |
| `skill_execution_timeout` | `30.0` | SKILL 执行超时（秒） |
| `memory_depth` | `"deep"` | 记忆深度 |

### 持久化约定

所有配置类都提供：
- `load(path)` → 文件不存在时返回默认值
- `save(path)` → 原子写入（临时文件 + replace）
- `to_dict()` / `from_dict()` → JSON 序列化

---

## PersonaUtils（人格生成工具）

**定位**：供 WebUI 共用的 LLM 驱动人格生成函数。

### 核心函数

```python
generate_persona_from_interview(
    work_path,
    provider,
    name,
    answers: dict[str, str],      # 8 个问题的回答
    aliases: list[str],
    model: str,
) -> PersonaProfile
```

流程：
1. 将 8 道访谈题 + 用户回答 + JSON schema 组装成 prompt
2. 写入 `engine_state/pending_persona_interview.json`（原子保存，便于失败重试）
3. 调用 provider 生成 JSON
4. 解析为 `PersonaProfile`，写入 `engine_state/persona_interview_record.json`
5. 返回 `PersonaProfile`

### 8 道访谈题

覆盖社交角色、群聊节奏、关系模式、情绪反应、语言风格、边界原则、标志性习惯、成长经历。LLM 根据回答自动推断 `personality_traits`、`communication_style`、`emotional_baseline` 等字段。

---

## 数据隔离

```
data/personas/
├── 月白/
│   ├── persona.json
│   ├── orchestration.json
│   ├── adapters.json
│   ├── experience.json
│   ├── engine_state/
│   │   ├── persona.json
│   │   ├── worker_status.json
│   │   └── enabled
│   ├── memory/
│   │   ├── basic/
│   │   ├── diary/
│   │   ├── glossary/
│   │   └── semantic/
│   ├── skill_data/
│   │   └── stickers/
│   ├── image_cache/
│   └── logs/
│       ├── worker.log
│       └── archive/
│
└── Sirius/
    └── ...（完全独立）
```

---

## 与其他系统的关系

| 交互对象 | 方式 |
|---------|------|
| **PersonaWorker** | `PersonaManager` 通过 `subprocess.Popen` 启动，通过 `worker_status.json` 监控 |
| **EngineRuntime** | `PersonaWorker` 创建并管理其生命周期 |
| **NapCatAdapter** | `PersonaWorker` 为每个 enabled adapter 创建实例 |
| **PersonaStore / PersonaGenerator** | 读写 `persona.json`；生成默认人格 |
| **OrchestrationStore** | 读写 `orchestration.json` |
| **WebUIServer** | 通过 REST API 调用 `PersonaManager` 的所有方法 |
