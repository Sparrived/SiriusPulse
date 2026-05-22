# 平台适配层（Platforms）

> **v1.0 新增** — NapCat 多实例管理、OneBot v11 WebSocket 适配、QQ 群聊/私聊桥接。

## 一句话定位

平台层负责**让 Sirius Pulse 与 QQ 对话**。它管理 NapCat 二进制、建立 WebSocket 连接、翻译 OneBot 事件与引擎消息。

## 为什么需要它

Sirius Pulse 本身只处理抽象的消息和参与者。要与真实的 QQ 群聊交互，需要：
1. **NapCat**：在 QQ 进程中注入 OneBot v11 支持
2. **WebSocket 客户端**：接收群消息、发送回复
3. **事件翻译层**：把 OneBot JSON 事件转成引擎的 `Message`/`Participant`

v1.0 支持**多实例隔离**——每个人格有自己的 NapCat 配置目录和 QQ 号，共享全局二进制。

## 架构总览

```
QQ 进程 ←──注入── NapCat (DLL/Hook)
    │
    └── OneBot v11 正向 WS ←─── NapCatAdapter (WS 客户端)
                                    │
                                    ├── 事件回调 ──► 事件翻译层
                                    │                      │
                                    │                      ├── 渲染 prompt
                                    │                      ├── 图片缓存
                                    │                      ├── 白名单过滤
                                    │                      └── process_message()
                                    │                              │
                                    │                              ▼
                                    │                        EngineRuntime
                                    │                              │
                                    │                              ▼
                                    │                        EmotionalGroupChatEngine
                                    │
                                    └── API 调用 ◄── send_group_msg / send_private_msg

NapCatManager（全局二进制管理）
    │
    ├── install() ──► 从 GitHub Release 下载 NapCat.Shell.zip
    ├── configure() ──► 生成 napcat_{qq}.json / onebot11_{qq}.json
    └── start() / stop() ──► 启动/停止 NapCat 实例
```

---

## NapCatManager（NapCat 环境管理器）

**定位**：NapCat 的安装器、配置生成器和进程管理器。

### 核心能力

| 方法 | 说明 |
|------|------|
| `for_persona(global_install_dir, persona_name)` | 为指定人格创建隔离实例目录 `napcat/instances/{name}/` |
| `is_installed` | 检查 `napcat.mjs` 是否存在 |
| `is_qq_installed()` / `get_qq_path()` | Windows 注册表扫描 + 回退路径查找 `QQ.exe` |
| `install(version="latest")` | 异步下载 GitHub Release → 解压到 `install_dir` |
| `configure(qq_number, ws_port, ws_token)` | 合并生成 NapCat 核心配置和 OneBot v11 配置（merge 模式保留用户手动修改） |
| `start(qq_number)` | `CREATE_NEW_CONSOLE` 启动 `NapCatWinBootMain.exe` 注入 QQ |
| `stop()` | Windows 上仅释放引用（避免误杀用户正常 QQ）；Linux 上 `terminate()` → `kill()` |
| `wait_for_ws(host, port, token, timeout)` | TCP 探测 → WebSocket 握手 → 等待 `meta_event` 中的 `self_id` |
| `get_logs(lines)` | 读取 `logs/` 最新日志 |
| `get_status()` | 返回安装/运行/QQ 状态字典 |

### 多实例隔离

```
napcat/                          # 全局安装目录（共享）
├── NapCatWinBootMain.exe
├── NapCatWinBootHook.dll
├── napcat.mjs
└── instances/                   # 人格隔离目录（独立）
    ├── 月白/
    │   ├── config/
    │   │   ├── napcat_123456.json
    │   │   └── onebot11_123456.json
    │   └── logs/
    └── Sirius/
        └── ...
```

### 生命周期

1. **安装**：`install()` 下载并解压 NapCat 二进制
2. **配置**：`configure()` 根据 QQ 号和分配的 WS 端口生成 JSON
3. **启动**：`start()` 启动 NapCat 进程，写入 PID 文件
4. **等待就绪**：`wait_for_ws()` 确认 WebSocket 可连接
5. **停止**：`stop()` 终止进程（Linux）或释放句柄（Windows）

---

## NapCatAdapter（OneBot v11 WebSocket 客户端）

**定位**：轻量级、自动重连的 OneBot v11 协议客户端，位于 `platforms/onebot_v11/napcat/adapter.py`。

### 核心能力

| 方法 | 说明 |
|------|------|
| `connect()` | 启动自动重连后台任务 |
| `close()` | 停止重连、取消等待中的 future、关闭连接 |
| `on_event(handler)` | 注册事件回调 `Callable[[dict], Any]` |
| `call_api(action, params)` | 发送 API 请求，通过 `echo` 匹配异步响应，带超时 |
| `send_group_msg(group_id, message)` | 发送群消息（支持字符串或 segment 数组） |
| `send_private_msg(user_id, message)` | 发送私聊消息 |
| `upload_group_file(group_id, file_path, name)` | 上传群文件 |
| `get_group_member_info(group_id, user_id)` | 获取群成员信息 |
| `get_login_info()` | 获取登录信息 |

### 连接模型

```
connect()
    └── _reconnect_loop() ── 指数退避（最大 30s，最多 5 次）
            └── _connect_once() 成功
                    └── _listen_loop()
                            └── async for raw in ws
                                    └── _dispatch(data)
                                            ├── echo 匹配 → resolve Future
                                            └── 无 echo → 触发所有 on_event handler
```

- 所有 API 调用通过 `echo` 字段与响应匹配
- 每个 `call_api` 创建一个 `asyncio.Future`，超时后清理
- 连接断开时自动重连，不影响等待中的新请求

---

## 事件翻译层（QQ ↔ 引擎桥接）

**定位**：翻译层。把 OneBot 事件翻译成引擎能理解的 `Message`/`Participant`，把引擎回复翻译成 QQ 消息发出去。此功能已集成到 `NapCatAdapter` 中，位于 `platforms/onebot_v11/napcat/adapter.py`。

### 核心能力

| 方法 | 说明 |
|------|------|
| `start()` | 启动 runtime、注册 OneBot 事件回调、启动事件总线监听 |
| `stop()` | 停止事件总线监听、停止 runtime |
| `_on_group_message(event)` | 群消息事件处理：白名单检查、渲染 prompt、调用引擎 |
| `_on_private_message(event)` | 私聊消息事件处理 |
| `_process_message(group_id, user_id, prompt, event)` | 核心处理：构造 `Participant` + `Message` → `runtime.engine.process_message()` → 发送回复 |
| `_event_bus_listener()` | 订阅引擎事件总线，分发主动发言、延迟回复、开发者私聊、提醒等事件 |
| `_render_group_prompt(event)` | 将 OneBot segment（text/at/image）渲染为纯文本 prompt |
| `_cache_image(url)` | 下载图片到 `image_cache/`（MD5 内容哈希命名），上限 200 张，单张上限 10MB |
| `_send_group_text_raw(group_id, text)` | 带 `asyncio.Lock` 的群消息发送（防止消息交错） |
| `wait_event(predicate, timeout)` | 阻塞等待匹配条件的事件 |

### 白名单与过滤

1. **群白名单**：`allowed_group_ids` 为空时允许所有群；非空时只处理指定群
2. **私聊白名单**：`allowed_private_user_ids` 控制允许私聊的 QQ 号
3. **Peer AI 过滤**：`peer_ai_ids` 中的 QQ 号被视为其他 AI，其消息不会触发回复（防止 AI 互相对话）
4. **自身消息过滤**：忽略自己发送的消息
5. **启用标志**：`engine_state/enabled` 文件为 `0` 时不处理任何消息

### 后台投递循环（事件总线模式）

NapCatAdapter 通过订阅引擎事件总线接收异步事件，而非轮询：

```python
async for event in self.runtime.engine.event_bus.subscribe():
    if event.type == SessionEventType.PROACTIVE_RESPONSE_TRIGGERED:
        # 发送主动回复到群聊
    elif event.type == SessionEventType.DELAYED_RESPONSE_TRIGGERED:
        # 调用 tick_delayed_queue 生成并发送延迟回复
    elif event.type == SessionEventType.DEVELOPER_CHAT_TRIGGERED:
        # 发送开发者主动私聊消息
    elif event.type == SessionEventType.REMINDER_TRIGGERED:
        # 发送提醒消息（群/私聊，按 adapter_type 过滤）
```

### Prompt 渲染

| OneBot segment | 渲染为 |
|----------------|--------|
| `text` | 原样文本 |
| `at` | `@昵称`（通过 `get_group_member_info` 解析） |
| `image` | `[图片: 文件名]`（同时下载缓存图片供多模态使用） |
| `reply` | 忽略（不渲染引用回复上下文） |

---

## EngineRuntime（引擎运行时封装）

**定位**：`EmotionalGroupChatEngine` 的懒加载包装器，管理 provider 装配、技能注入和状态持久化。

### 设计动机

引擎需要 provider、persona、skill registry 才能运行。在多人格架构中，这些必须按人格组装。`EngineRuntime` 延迟初始化引擎，避免在配置未完成时创建默认人格。

### 核心能力

| 方法 | 说明 |
|------|------|
| `is_ready()` | provider + persona 都已配置且引擎能成功实例化 |
| `has_provider_config()` | 检查是否能构建出 provider |
| `has_persona()` | 检查 `persona.json` 是否存在 |
| `engine`（property） | 懒加载：首次访问时调用 `_build_engine()` 并启动后台任务 |
| `start()` | 预热身引擎；若未就绪则进入"配置待完成"模式 |
| `reload_engine()` | 保存状态 → 停止后台任务 → 置空引擎 → 下次访问重建 |
| `stop()` | 停止后台任务 → 保存状态 → 释放引擎引用 |
| `add_skill_bridge(adapter_type, bridge)` | 向 `SkillExecutor` 注册平台桥接，使 skill 能调用 adapter API |

### Provider 解析优先级

`_build_provider()` 按以下顺序尝试：

1. 全局 `data/providers/provider_keys.json`（所有人格共用）
2. 人格目录本地 `providers/provider_keys.json`（兼容旧版）
3. `plugin_config` 中显式传入的 provider 配置
4. 环境变量 `SIRIUS_API_KEY` / `SIRIUS_BASE_URL`

### 引擎装配流程

```
首次访问 .engine
    │
    ├── _build_provider() → AutoRoutingProvider
    ├── assemble config dict（sensitivity、proactive、memory 参数）
    ├── create_emotional_engine(work_path, provider, config)
    ├── engine.load_state()（从 disk 恢复记忆和情绪状态）
    ├── inject TokenUsageStore
    ├── _setup_skill_runtime() → SkillRegistry + SkillExecutor
    └── engine.start_background_tasks()
```

---

## PersonaWorker NapCat 自动管理

`PersonaWorker` 在启动 adapter 时自动管理 NapCat 实例的生命周期（`_ensure_napcat_running`）：

1. **端口探测**：通过 TCP 探测目标 WS 端口是否可达
2. **自动安装**：若 NapCat 未安装，从 GitHub Release 自动下载并解压
3. **自动配置**：根据 adapter 配置中的 `qq_number` 和 `ws_port` 生成 NapCat 配置
4. **自动启动**：`CREATE_NEW_CONSOLE` 启动 NapCat 实例
5. **等待就绪**：最多等待 180 秒，通过 WS 握手确认实例就绪

若自动管理失败，该 adapter 会被跳过并记录错误日志，不影响其他人格。

---

## 模块交互

```
NapCatManager              NapCatAdapter
     │                           │
     │ install/configure/start   │ connect/call_api / _on_event / _process_message
     │                           │
     └───────────────────────────┘
                                    │
                                    └── EngineRuntime (.engine)
                                            └── EmotionalGroupChatEngine
```

| 模块 | 向上游提供 | 向下游消费 |
|------|-----------|-----------|
| **NapCatManager** | 进程生命周期、配置生成 | GitHub Release、QQ 进程 |
| **NapCatAdapter** | WS 连接、API 调用、事件分发、事件翻译、prompt 渲染、图片缓存、后台投递 | NapCat 的 OneBot v11 WS |
| **EngineRuntime** | 懒加载引擎、provider 装配、skill 注入 | PersonaStore、ProviderRegistry、SkillRegistry |
