# Sirius Pulse 完整架构流程

> **v1.0 多人格架构的真实执行路径与模块边界**
>
> 本文档用人类易读的方式，从"一条消息怎么被处理"到"整个系统怎么运转"，完整描述 Sirius Pulse 的架构。流程图使用 Mermaid 语法，可在支持 Mermaid 的编辑器或浏览器中渲染。

---

## 第一章：系统全景图

### 1.1 你在看什么

Sirius Pulse 是一个**支持多人格启用的异步角色扮演程序**。想象一个 QQ 群里同时有几个不同的 AI 角色在聊天——有的活泼、有的高冷、有的毒舌——每个人格独立运行、独立记忆、独立配置。

### 1.2 进程模型

```mermaid
flowchart TD
    subgraph MainProcess["主进程"]
        CLI["python main.py run"]
        PM["PersonaManager<br/>扫描/启停/端口分配"]
        WebUI["WebUIServer<br/>aiohttp REST + 静态页面"]
        NM["NapCatManager<br/>全局安装/多实例调度"]
    end

    CLI --> PM
    CLI --> WebUI
    CLI --> NM

    subgraph PersonaA["子进程 A（人格：月白）"]
        PWA["PersonaWorker<br/>--config data/personas/月白"]
        RTA["EngineRuntime"]
        EngineA["EmotionalGroupChatEngine"]
        AdapterA["NapCatAdapter<br/>platforms/onebot_v11/napcat/adapter.py"]
        PWA --> RTA --> EngineA
        PWA --> AdapterA
        RTA --> AdapterA
    end

    subgraph PersonaB["子进程 B（人格：Sirius）"]
        PWB["PersonaWorker<br/>--config data/personas/Sirius"]
        RTB["EngineRuntime"]
        EngineB["EmotionalGroupChatEngine"]
        AdapterB["NapCatAdapter<br/>platforms/onebot_v11/napcat/adapter.py"]
        PWB --> RTB --> EngineB
        PWB --> AdapterB
        RTB --> AdapterB
    end

    PM -->|"subprocess.Popen<br/>CREATE_NEW_CONSOLE"| PWA
    PM -->|"subprocess.Popen<br/>CREATE_NEW_CONSOLE"| PWB
    NM -->|"共享全局二进制<br/>独立配置/日志"| AdapterA
    NM -->|"共享全局二进制<br/>独立配置/日志"| AdapterB

    PM -->|"维护"| Registry["data/adapter_port_registry.json<br/>端口分配表"]
    EngineA -->|"共用"| Providers["data/providers/provider_keys.json<br/>全局 Provider 注册表"]
    EngineB -->|"共用"| Providers
```

### 1.3 关键设计决策

| 决策 | 说明 |
|------|------|
| **独立子进程** | 每个人格一个独立进程，崩溃不影响其他人格 |
| **数据隔离** | 每个人格有自己的目录 `data/personas/{name}/`，记忆、配置、日志完全隔离 |
| **Provider 共享** | 所有人格共用 `data/providers/provider_keys.json`，避免重复配置 API Key |
| **NapCat 多实例** | 每个人格独立的 QQ 实例，共享全局二进制，独立配置和日志 |
| **端口自动分配** | `PersonaManager` 从 3001 开始递增分配 WebSocket 端口 |

---

## 第二章：主进程启动流程

### 2.1 从命令行到运行

```bash
python main.py run
```

```mermaid
flowchart TD
    A["python main.py run"] --> B["加载 data/global_config.json"]
    B --> C["创建 PersonaManager<br/>扫描 data/personas/ 目录"]
    C --> D["NapCatManager 全局安装检查<br/>自动安装缺失的 NapCat 二进制"]
    D --> E["为每个 enabled 人格<br/>分配 NapCat 端口与实例目录"]
    E --> F["为每个人格启动 NapCat 实例<br/>CREATE_NEW_CONSOLE"]
    F --> G["为每个人格启动 PersonaWorker 子进程<br/>python -m sirius_pulse.persona_worker --config {pdir}"]
    G --> H["启动 WebUIServer<br/>aiohttp REST API"]
    H --> I["主进程阻塞等待<br/>SIGTERM/SIGINT 优雅退出"]
    I --> J["停止所有子进程<br/>停止 NapCat 实例<br/>停止 WebUI"]
```

### 2.2 主进程三大组件

**PersonaManager（人格管家）**
- `create_persona(name)` — 创建新人格目录和默认配置
- `start_persona(name)` — 启动单个人格（含 NapCat 自动管理）
- `run_all()` — 批量启动所有 enabled 人格
- `get_logs(name)` — 读取子进程日志
- `get_status(name)` — 读取子进程心跳状态

**WebUIServer（管理面板）**
- 提供 REST API：人格列表、状态、配置、日志
- 提供静态页面：Dashboard + 配置面板
- 不直接操作 NapCat 进程，只通过 API 与 PersonaManager 交互

**NapCatManager（QQ 管理器）**
- 管理 NapCat 全局二进制（安装、更新）
- 为每个人格创建独立实例目录
- 启动/停止 NapCat 进程

---

## 第三章：人格子进程启动流程

### 3.1 子进程内部发生了什么

```mermaid
flowchart TD
    A["PersonaWorker.run()"] --> B["加载配置<br/>adapters.json / experience.json /<br/>orchestration.json / persona.json"]
    B --> C["创建 EngineRuntime<br/>work_path=人格目录<br/>global_data_path=data/"]
    C --> D["启动 EngineRuntime<br/>懒加载 EmotionalGroupChatEngine<br/>启动后台任务"]
    D --> E["为每个 enabled adapter<br/>创建 NapCatAdapter<br/>platforms/onebot_v11/napcat/adapter.py"]
    E --> F["adapter.start()<br/>启动 WebSocket 连接"]
    F --> G["启动心跳循环<br/>每 10 秒写入 worker_status.json"]
    G --> H["阻塞等待关闭信号"]
    H --> I["清理：停止 adapter、停止 runtime"]
```

### 3.2 子进程内的关键协作

- 所有 bridge 共享同一个 `EngineRuntime` 和同一个 `EmotionalGroupChatEngine`
- 每个 bridge 有自己的 `allowed_group_ids` 配置
- engine 的 `_pending_reminders` 是共享的（所有 bridge 都能投递提醒）

---

## 第四章：消息处理完整流程

### 4.1 一条消息的一生

假设群里有人发了一条消息："今天工作好累"，看看它怎么被处理。

```mermaid
flowchart TD
    A["QQ 群消息<br/>'今天工作好累'"] --> B["NapCatAdapter<br/>OneBot v11 事件"]
    B --> C["NapCatAdapter.on_message()<br/>解析事件 → 提取内容/发送者/群号"]
    C --> D["EngineRuntime.process_message()"]
    D --> E["EmotionalGroupChatEngine.process_message()"]

    subgraph Perception["① 感知层（零 LLM 成本）"]
        E --> P1["IdentityResolver.resolve()<br/>'今天工作好累' 是谁发的？"]
        P1 --> P2["UserManager.register()<br/>更新/创建用户档案"]
        P2 --> P3["BasicMemoryManager.add_entry()<br/>加入群聊窗口"]
        P3 --> P4["RhythmAnalyzer.analyze()<br/>计算群聊热度"]
        P4 --> P5["emit PERCEPTION_COMPLETED"]
    end

    subgraph Cognition["② 认知层（统一 CognitionAnalyzer）"]
        P5 --> C1["联合规则引擎<br/>零成本热路径（~90% 命中率）"]
        C1 --> C2["单次 LLM fallback<br/>复杂情况（~10% 命中率）"]
        P5 --> C3["记忆检索<br/>BasicMemory + DiaryManager"]
        C2 --> C4["emit COGNITION_COMPLETED"]
        C3 --> C4
    end

    subgraph Decision["③ 决策层（纯规则，零 LLM 成本）"]
        C4 --> D1["RhythmAnalyzer<br/>heat_level / pace / topic_stability"]
        D1 --> D2["ThresholdEngine<br/>threshold = base × activity × engagement × time"]
        D2 --> D3["ResponseStrategyEngine<br/>IMMEDIATE / DELAYED / SILENT / PROACTIVE"]
        D3 --> D4["更新 AssistantEmotionState"]
        D4 --> D5["emit DECISION_COMPLETED"]
    end

    subgraph Execution["④ 执行层"]
        D5 --> X1{"策略？"}
        X1 --"IMMEDIATE"--> X2["立即生成回复"]
        X1 --"DELAYED"--> X3["入 DelayedResponseQueue<br/>等待话题间隙"]
        X1 --"SILENT"--> X4["仅更新内部状态<br/>不生成回复"]
        X1 --"PROACTIVE"--> X5["由 ProactiveTrigger 外部触发<br/>生成自然开场白"]
        X2 --> X6["PromptFactory.assemble_chat()<br/>组装 prompt"]
        X6 --> X7["StyleAdapter 输出长度/语气指令<br/>不再动态缩减 max_tokens"]
        X7 --> X8["ModelRouter 选择模型"]
        X8 --> X9["Provider.generate_async()<br/>全链路异步 httpx"]
        X9 --> X10["解析 SKILL_CALL"]
        X10 --> X11["Token 追踪记录"]
        X11 --> X12["emit EXECUTION_COMPLETED"]
    end

    X12 --> U["_background_update()<br/>更新群体氛围 + 群规范学习 + 反馈结算 + 情感孤岛检测"]
```

### 4.2 认知层内部细节

```mermaid
flowchart TD
    A["消息内容 + 上下文"] --> B{"规则引擎置信度<br/>≥ 0.9 ?"}
    B --"是（~90%）"--> C["零 LLM 成本返回"]
    B --"否（~10%）"--> D["单次 LLM fallback"]

    subgraph RuleEngine["联合规则引擎"]
        A --> E1["情感词典匹配<br/>valence / arousal / intensity"]
        A --> E2["意图模式匹配<br/>social_intent / subtype"]
        A --> E3["12维指向性信号<br/>mention / reference / name_match / ..."]
        A --> E4["讽刺检测<br/>5 类启发式规则"]
        A --> E5["资格感判断<br/>persona 与话题重叠度"]
        E1 --> C
        E2 --> C
        E3 --> C
        E4 --> C
        E5 --> C
    end

    subgraph LLMFallback["单次 LLM fallback"]
        D --> F1["轻量模型请求联合 JSON"]
        F1 --> F2["返回完整分析结果"]
    end

    C --> G["上下文融合<br/>情感轨迹 + 群体氛围 + 助手情绪"]
    F2 --> G
    G --> H["返回三元组<br/>EmotionState + IntentAnalysisV3 + EmpathyStrategy"]
```

### 4.3 延迟回复的触发

```mermaid
sequenceDiagram
    participant User as 用户
    participant QQ as QQ 群
    participant Adapter as NapCatAdapter
    participant Engine as EmotionalEngine
    participant LLM as Provider

    User->>QQ: "今天工作好累"
    QQ->>Adapter: OneBot v11 消息事件
    Bridge->>Engine: process_message()
    Engine->>Engine: 感知层 + 认知层 + 决策层
    Engine-->>Engine: 策略 = DELAYED
    Engine->>Engine: 加入 DelayedResponseQueue
    Engine-->>Bridge: 返回（无立即回复）

    Note over Bridge,Engine: 3 秒后，后台投递循环

    Bridge->>Engine: tick_delayed_queue()
    Engine->>Engine: 话题间隙就绪度 = 0.8 > threshold
    Engine->>Engine: 触发延迟回复生成
    Engine->>Engine: PromptFactory.assemble_chat() 组装 prompt
    Engine->>LLM: provider.generate_async()（全链路异步 httpx）
    LLM-->>Engine: "辛苦啦！周末好好休息~"
    Engine->>Engine: Token 追踪
    Engine-->>Bridge: 返回回复文本
    Bridge->>QQ: 发送群消息
    QQ->>User: "辛苦啦！周末好好休息~"
```

### 4.4 四种响应策略的触发条件

| 策略 | 触发场景 | 行为 |
|------|---------|------|
| **IMMEDIATE** | 被 @、紧急求助、高 relevance | 立即生成并发送回复 |
| **DELAYED** | 一般性对话、话题间隙不够 | 加入队列，等自然间隙再回 |
| **SILENT** | 无关话题、低 relevance、冷却中 | 不回复，只后台学习 |
| **PROACTIVE** | 群聊沉默过久、记忆触发、情感触发 | 主动发起新话题 |

---

## 第五章：后台任务系统

### 5.1 引擎后台任务

引擎内置 6 个后台任务，另有被动 SKILL 注册的任务（如 reminder）并行运行：

```mermaid
flowchart LR
    subgraph BG["内置后台任务（并行运行）"]
        T1["任务1<br/>延迟队列 ticker<br/>智能休眠（3-30s）"]
        T2["任务2<br/>主动触发 checker<br/>每 60 秒"]
        T3["任务3<br/>日记生成 promoter<br/>每 180 秒"]
        T4["任务4<br/>日记 consolidator<br/>每 600 秒"]
        T5["任务5<br/>开发者私聊 checker<br/>每 60 秒"]
        T6["任务6<br/>表情包新鲜度更新<br/>每 3600 秒"]
    end

    subgraph PassiveSK["被动 SKILL 任务"]
        T7["reminder checker<br/>每 15 秒<br/>通过 create_background_tasks() 注册"]
    end

    T1 -->|"检测话题间隙<br/>触发延迟回复"| Engine["EmotionalGroupChatEngine"]
    T2 -->|"检查沉默群聊<br/>生成主动发言"| Engine
    T3 -->|"冷群检测<br/>LLM 生成日记"| Engine
    T4 -->|"合并相似日记"| Engine
    T5 -->|"检查开发者私聊"| Engine
    T6 -->|"衰减 novelty_score<br/>模拟喜新厌旧"| Engine
    T7 -->|"扫描到期提醒<br/>支持 once/interval/daily/weekly"| Engine
```

### 5.2 提醒系统完整链路

提醒是一个**双模式 SKILL**：主动模式由模型调用 `run()` 创建/管理提醒，被动模式通过 `create_background_tasks(ctx)` 注册周期性检查任务。

```mermaid
sequenceDiagram
    participant User as 用户
    participant AI as AI 回复
    participant Skill as reminder SKILL (active)
    participant Store as SkillDataStore
    participant Passive as reminder 被动任务
    participant Engine as EmotionalEngine
    participant Adapter as NapCatAdapter
    participant QQ as QQ 群

    User->>AI: "提醒我明天下午 3 点开会"
    AI->>AI: 生成回复含 [SKILL_CALL: reminder]
    AI->>Skill: 执行 reminder.run()
    Skill->>Store: 存入 skill_data/reminder.json
    Note over Engine: 引擎自动注入 group_id 和 adapter_type

    Note over Passive: 被动任务 checker（每 15 秒）
    Passive->>Passive: _check_and_fire_reminders(ctx)
    Passive->>Passive: 扫描 reminder.json
    Passive->>Passive: 发现到期提醒
    Passive->>Passive: _execute_skill_chain()（若有预执行链）
    Passive->>Passive: _generate_reminder_message(ctx)
    Passive->>Passive: LLM 生成人格化提醒
    Passive->>Passive: 放入 _pending_reminders[group_id]

    Note over Adapter: 事件总线监听
    Adapter->>Engine: pop_reminders(gid, adapter_type)
    Engine-->>Adapter: 返回提醒消息
    Adapter->>QQ: _send_group_text_raw()
    QQ->>User: "月白提醒：下午 3 点的会议别忘了哦~"
```

---

## 第六章：数据流与存储

### 6.1 全局共享数据

| 路径 | 说明 | 谁读写 |
|------|------|--------|
| `data/global_config.json` | WebUI 参数、NapCat 管理、日志级别 | 主进程读写 |
| `data/providers/provider_keys.json` | Provider 凭证（所有人格共用） | 主进程/子进程读 |
| `data/adapter_port_registry.json` | NapCat 端口分配表 | PersonaManager 维护 |

### 6.2 人格隔离数据

```mermaid
flowchart TD
    subgraph PersonaDir["data/personas/{name}/"]
        Config["配置层"]
        State["运行状态"]
        Memory["记忆层"]
        SkillData["SKILL 数据"]
        Logs["日志"]
    end

    subgraph Config
        C1["persona.json<br/>人格定义"]
        C2["orchestration.json<br/>模型编排"]
        C3["adapters.json<br/>平台适配器"]
        C4["experience.json<br/>体验参数"]
    end

    subgraph State
        S1["engine_state/persona.json<br/>运行时人格状态"]
        S2["engine_state/worker_status.json<br/>子进程心跳"]
        S3["engine_state/enabled<br/>启停标志"]
    end

    subgraph Memory
        M1["memory/basic/<group_id>.jsonl<br/>基础记忆（30条）"]
        M2["memory/diary/<group_id>.jsonl<br/>日记记忆"]
        M3["memory/diary/index/<group_id>.json<br/>日记索引"]
        M4["memory/glossary/terms.json<br/>名词解释（人格级隔离）"]
        M5["memory/semantic/<br/>群语义画像"]
    end

    subgraph SkillData
        SD1["skill_data/reminder.json<br/>提醒数据"]
        SD2["skill_data/*.json<br/>其他 SKILL 数据"]
        SD3["skill_data/stickers/<br/>表情包 RAG 库"]
    end

    subgraph Logs
        L1["logs/worker.log<br/>子进程主日志"]
        L2["logs/archive/<br/>归档日志"]
    end
```

### 6.3 NapCat 多实例数据

```mermaid
flowchart TD
    subgraph Global["全局共享"]
        G1["napcat/NapCatWinBootMain.exe"]
        G2["napcat/NapCatWinBootHook.dll"]
        G3["napcat/napcat.mjs"]
    end

    subgraph InstanceA["napcat/instances/月白/"]
        A1["config/napcat_{qq}.json<br/>独立 NapCat 配置"]
        A2["config/onebot11_{qq}.json<br/>独立 OneBot 配置"]
        A3["logs/<br/>独立日志"]
    end

    subgraph InstanceB["napcat/instances/Sirius/"]
        B1["config/napcat_{qq}.json<br/>独立 NapCat 配置"]
        B2["config/onebot11_{qq}.json<br/>独立 OneBot 配置"]
        B3["logs/<br/>独立日志"]
    end

    G1 -.->|"共享二进制"| InstanceA
    G1 -.->|"共享二进制"| InstanceB
```

---

## 第七章：事件总线

引擎在处理每条消息时发射事件，外部可以订阅：

```python
from sirius_pulse.core.events import SessionEventType

async for event in engine.event_bus.subscribe():
    if event.type == SessionEventType.PERCEPTION_COMPLETED:
        print(f"感知完成：{event.data['group_id']}")
    elif event.type == SessionEventType.COGNITION_COMPLETED:
        print(f"认知完成：情绪={event.data['emotion']}")
    elif event.type == SessionEventType.DECISION_COMPLETED:
        print(f"决策完成：策略={event.data['strategy']}")
    elif event.type == SessionEventType.EXECUTION_COMPLETED:
        print(f"执行完成：回复={event.data['reply']}")
```

**事件类型**：

| 事件 | 触发时机 | 数据 |
|------|---------|------|
| `PERCEPTION_COMPLETED` | 感知层完成后 | group_id, user_id, message |
| `COGNITION_COMPLETED` | 认知层完成后 | emotion, intent, empathy |
| `DECISION_COMPLETED` | 决策层完成后 | strategy, threshold |
| `EXECUTION_COMPLETED` | 执行层完成后 | reply, tokens_used |
| `DELAYED_RESPONSE_TRIGGERED` | 延迟回复触发时 | group_id, original_message |
| `PROACTIVE_RESPONSE_TRIGGERED` | 主动发言触发时 | group_id, trigger_type |
| `DEVELOPER_CHAT_TRIGGERED` | 开发者私聊主动对话触发时 | group_id, chat_content |
| `REMINDER_TRIGGERED` | 提醒到期时 | group_id, reminder_content |

**有损广播**：如果消费者处理慢了，队列满后事件会被丢弃，不会阻塞引擎。

---

## 第八章：Provider 路由

### 8.1 支持的 Provider 平台

| 平台 | 标识 | 默认 base_url |
|------|------|--------------|
| OpenAI 兼容 | `openai-compatible` | https://api.openai.com |
| 阿里云百炼 | `aliyun-bailian` | https://dashscope.aliyuncs.com/compatible-mode |
| 智谱 AI | `bigmodel` | https://open.bigmodel.cn/api/paas/v4 |
| DeepSeek | `deepseek` | https://api.deepseek.com |
| SiliconFlow | `siliconflow` | https://api.siliconflow.cn |
| 火山方舟 | `volcengine-ark` | https://ark.cn-beijing.volces.com/api/v3 |
| YTea | `ytea` | https://api.ytea.top |

### 8.2 路由规则

```mermaid
flowchart TD
    A["EngineRuntime._build_provider()"] --> B["从全局位置加载 ProviderRegistry"]
    B --> C["data/providers/provider_keys.json"]
    C -->|"未找到"| D["回退到人格目录"]
    D --> E["data/personas/{name}/providers/"]
    E --> F["创建 AutoRoutingProvider"]
    C -->|"找到"| F

    F --> G{"路由决策"}
    G -->|"优先"| H["ProviderConfig.models<br/>显式模型列表"]
    G -->|"其次"| I["healthcheck_model<br/>精确匹配"]
    G -->|"回退"| J["第一个启用的 provider"]
```

---

## 第九章：模块职责速查表

| 分层 | 模块 | 主要职责 |
|------|------|---------|
| **入口层** | `main.py` | 统一 CLI：无参数启动 WebUI；`run` 启动所有人格；`persona` 管理单个人格 |
| **主进程管理** | `persona_manager.py` | 多人格生命周期：扫描、创建、删除、迁移、启停、监控 |
| **子进程入口** | `persona_worker.py` | 单个人格运行入口：加载配置、创建 EngineRuntime、启动 Bridge、心跳 |
| **子进程运行时** | `platforms/runtime.py` | EngineRuntime：懒加载引擎，管理 provider 和 skill bridge |
| **平台适配** | `platforms/onebot_v11/napcat/adapter.py` | NapCat 适配器（OneBot v11 WebSocket 客户端、事件处理、后台投递循环） |
| **QQ 管理** | `platforms/onebot_v11/napcat/manager.py` | NapCat 全局安装、多实例调度 |
| **协议解析** | `platforms/onebot_v11/protocol.py` | OneBot v11 协议解析 |
| **认知编排** | `core/emotional_engine.py` | Mixin 架构引擎（engine_core + pipeline + prompt_factory + bg_tasks + helpers） |
| **引擎核心** | `core/engine_core.py` | 引擎基类：__init__、公开 API、持久化、表情包系统初始化 |
| **引擎管线** | `core/pipeline.py` | 5 阶段管线：感知→认知→决策→执行→后台更新 |
| **Prompt 工厂** | `core/prompt_factory.py` | 无状态 PromptFactory：统一 prompt 拼装、StyleAdapter 风格适配、PromptBundle |
| **引擎后台任务** | `core/bg_tasks.py` | 6 个后台任务：延迟队列、主动触发、日记生成/合并、开发者私聊、表情包新鲜度 |
| **引擎辅助** | `core/helpers.py` | 技能集成（含被动 SKILL 注册与触发分发）、上下文辅助、用户画像分析、token 记录、异常分类 |
| **认知分析** | `core/cognition.py` | 统一情绪+意图分析、规则引擎+LLM fallback |
| **响应策略** | `core/response_strategy.py` | 四种策略选择（IMMEDIATE/DELAYED/SILENT/PROACTIVE） |
| **动态阈值** | `core/threshold_engine.py` | 阈值计算：base × activity × engagement × time |
| **对话节奏** | `core/rhythm.py` | 热度、速度、话题稳定性、间隙就绪度 |
| **Prompt 组装** | `core/response_assembler.py` | *(已迁移至 PromptFactory)* |
| **基础记忆** | `memory/basic/` | 滑动窗口（30条硬限制）、热度计算、归档 |
| **日记记忆** | `memory/diary/` | LLM 生成摘要、关键词/嵌入索引、ChromaDB 向量存储、token 预算检索 |
| **Embedding 服务** | `embedding/` | Embedding 微服务：aiohttp 服务端（批量合并推理）+ 同步客户端，DiaryIndexer / StickerIndexer 通过 EmbeddingClient 调用 |
| **人格生成** | `persona_generation/` | 人格资产生成子包（templates 数据模型 + builders LLM 生成），原顶层 prompt_templates / roleplay_prompting 迁移至此 |
| **用户管理** | `memory/user/` | 极简 UserProfile、群隔离、跨平台身份追踪 |
| **名词解释** | `memory/glossary/` | AI 自身知识库，支持人格级隔离与迁移 |
| **语义记忆** | `memory/semantic/` | 群氛围、群规范、反馈驱动的互动率追踪 |
| **上下文组装** | `memory/context_assembler.py` | 基础记忆+日记 → OpenAI messages |
| **Provider 层** | `providers/` | 统一请求协议、7 个平台实现、自动路由 |
| **插件系统** | `plugins/` | 插件加载、注册表、执行器、配置管理、@command 装饰器、PluginContext、响应调度、事件定义 |
| **SKILL 层** | `skills/` | 注册、执行、数据存储、依赖解析、内置技能、遥测；被动 SKILL 支持（BackgroundTaskSpec/TriggerSpec/SkillEngineContext） |
| **SKILL 引擎上下文** | `core/skill_engine_context.py` | SkillEngineContextImpl：被动 SKILL 与引擎交互的适配器 |
| **表情包系统** | `skills/sticker/` | RAG 表情包：向量索引、偏好管理、学习、反馈观察、新鲜度 |
| **配置层** | `config/` | 类型安全的配置契约、加载器、helpers、JSONC |
| **WebUI 层** | `webui/` | aiohttp REST API（server_core + 4 个 API 模块）+ 管理面板（16 个页面） |
| **Token 层** | `token/` | 统计、SQLite 持久化、多维分析 |
| **会话存储** | `session/store.py` | JsonSessionStore / SqliteSessionStore / SessionStoreFactory |
| **后台任务** | `background_tasks.py` | 轻量级 asyncio 任务调度器 |
| **工具函数** | `utils/` | WorkspaceLayout、JsonSerializable mixin、开发辅助 |
