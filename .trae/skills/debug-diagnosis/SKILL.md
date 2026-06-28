---
name: debug-diagnosis
description: "在修改 Sirius Pulse 核心模块（core/memory/providers/platforms 等）后，系统化排查异常、定位根因并验证修复。关键词：调试诊断、bug排查、异常定位、核心模块、修复验证。"
---

# 调试诊断指南

## 目标

在修改 `sirius_pulse` 核心模块后出现异常或行为偏离预期时，通过结构化的排查流程快速定位根因，避免盲目猜测和过度修复。

## 语言规范（强制）

- 本 SKILL 及所有后续新增/修改的 SKILL 必须使用中文。
- `description` 和正文必须为中文。
- 若任务中发现英文 SKILL 内容，需在同一任务中同步中文化。

## 触发条件

当以下场景出现时，必须执行本流程：
- 修改 `sirius_pulse/core/`、`sirius_pulse/memory/`、`sirius_pulse/providers/`、`sirius_pulse/platforms/` 后测试失败或行为异常
- 修改 `sirius_pulse/models/` 数据模型后引发上下游兼容性问题
- 修改 `sirius_pulse/config/` 配置系统后导致引擎初始化失败
- 运行时出现未捕获异常、无限循环、内存泄漏或响应延迟剧增
- 用户明确要求"帮我排查这个问题"或"为什么出错了"

## 核心原则

1. **先复现，后修复**：没有稳定复现步骤的修复是赌博。
2. **最小改动验证**：每次只改一个地方，确认因果关系。
3. **日志优先**：先看日志和堆栈，不要直接读代码猜原因。
4. **隔离变量**：用 `MockProvider` 和临时目录排除外部干扰。

---

## 排查流程

### 步骤 1：收集异常信息

**必须收集的信息**：
- [ ] 完整的异常堆栈（traceback）
- [ ] 触发异常的最小输入/操作序列
- [ ] 最近的代码变更范围（`git diff HEAD~1 --name-only`）
- [ ] 相关日志片段（`logs/` 或控制台输出）

**快速命令**：
```bash
# 查看最近改动
git diff HEAD~1 --name-only

# 查看改动统计
git diff HEAD~1 --stat

# 查看特定文件的改动
git diff HEAD~1 -- sirius_pulse/core/emotional_engine.py
```

### 步骤 2：定位异常层级

根据异常类型和堆栈，判断问题发生在哪个架构层级：

| 异常特征 | 可能层级 | 优先检查文件 |
|---------|---------|------------|
| `KeyError`、`AttributeError`、`TypeError` 在数据访问时 | 数据模型/配置 | `sirius_pulse/models/`、`sirius_pulse/config/` |
| 消息处理无响应或响应内容异常 | 引擎核心 | `sirius_pulse/core/emotional_engine.py`、`core/prompt_factory.py`、`core/cognition.py` |
| 记忆丢失、重复或检索异常 | 记忆系统 | `sirius_pulse/memory/basic/`、`memory/diary/`、`memory/semantic/`、`memory/context_assembler.py` |
| LLM 调用失败、超时或返回解析错误 | Provider/路由 | `sirius_pulse/providers/`、`providers/routing.py` |
| 群消息收发异常、连接断开 | 平台适配 | `sirius_pulse/platforms/napcat_adapter.py`、`platforms/napcat_bridge.py` |
| 子进程崩溃、端口冲突 | 运行时/管理 | `sirius_pulse/platforms/runtime.py`、`persona_manager.py`、`persona_worker.py` |
| 配置加载失败、路径错误 | Workspace/配置 | `sirius_pulse/utils/layout.py`、`config/manager.py` |
| SKILL 调用失败 | 技能系统 | `sirius_pulse/skills/registry.py`、`skills/executor.py`、`skills/security.py` |
| WebUI 无法访问 | WebUI | `sirius_pulse/webui/server.py` |

### 步骤 3：缩小变更范围

**二分法定位**：
1. 回退到上一个稳定 commit，确认问题是否消失。
2. 逐步应用改动，找到引入问题的最小变更集。
3. 若改动较大，优先检查数据流变更（新增/删除字段、修改函数签名）。

**关键检查点**：
- [ ] 是否有函数签名变更但未同步所有调用点？
- [ ] 是否有新增字段未设置默认值导致旧数据反序列化失败？
- [ ] 是否有异步/同步混用（如 `async def` 被同步调用）？
- [ ] 是否有路径变更导致文件读写失败？
- [ ] 是否有导入循环（circular import）？

### 步骤 4：编写最小复现测试

在修复前，先写一个能稳定复现问题的测试：

```python
import pytest
from sirius_pulse.core.emotional_engine import EmotionalGroupChatEngine
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.providers.mock import MockProvider
from sirius_pulse.models.models import Message, Participant

@pytest.mark.asyncio
async def test_reproduce_issue(tmp_path):
    """复现 [问题描述]"""
    provider = MockProvider(responses=["测试回复"])
    engine = EmotionalGroupChatEngine(
        work_path=tmp_path,
        persona=PersonaProfile(name="DebugBot"),
        provider_async=provider,
    )

    # 构造触发异常的输入
    participant = Participant(name="u1", user_id="u1")
    msg = Message(role="human", content="触发内容", speaker="u1")

    # 执行并观察异常
    reply = await engine.process_message(msg, [participant], "group_a")

    # 断言异常行为或错误结果
    # assert ...
```

### 步骤 5：修复与验证

**修复原则**：
- 只修改根因，不附带"优化"或"清理"。
- 若涉及接口变更，同步更新所有调用点和测试。
- 修复后运行相关测试：`pytest tests/test_<module>.py -xvs`

**验证清单**：
- [ ] 最小复现测试现在通过。
- [ ] 该模块的全部测试通过：`pytest tests/test_<module>.py -q`
- [ ] 全量测试通过：`pytest tests/ -q`
- [ ] `python main.py --help` 正常输出。
- [ ] 若修改了外部接口，`sirius_pulse/__init__.py` 已同步更新。

### 步骤 6：日志与监控增强（可选）

若问题是隐蔽的（如时序问题、状态竞争），考虑增强日志：
- 在关键路径添加 `logger.debug()` 记录状态变更。
- 使用 `pytest -xvs -k <test_name>` 查看详细输出。
- 检查 `engine_state/` 和 `logs/` 中的持久化状态是否一致。

---

## 常见陷阱速查

| 现象 | 常见根因 | 快速验证 |
|------|---------|---------|
| `ModuleNotFoundError` | 新增文件未包含在包内或 `__init__.py` 未导出 | 检查 `sirius_pulse/__init__.py` 的 `__all__` |
| `PersonaProfile` 反序列化失败 | 新增必填字段无默认值 | 检查 `persona.json` 与 `PersonaProfile` 定义 |
| 引擎无响应 | `sensitivity=0` 或阈值过高 | 检查 `config` 中的 `sensitivity` 和 `task_enabled` |
| LLM 返回解析失败 | 模型返回被截断或非预期格式 | 检查 `MockProvider` 响应格式是否匹配预期 |
| 记忆不持久化 | `work_path` 未正确传入或权限问题 | 检查 `tmp_path` / `work_path` 目录内容 |
| 后台任务异常 | `start_background_tasks()` 未正确关闭 | 确保测试中使用 `engine.stop_background_tasks()` |
| NapCat 连接失败 | 端口冲突或配置不匹配 | 检查 `adapter_port_registry.json` 和 `napcat` 日志 |
| 子进程崩溃 | 人格目录缺失或配置错误 | 检查 `data/personas/` 目录结构和 `persona.json` |
| SKILL 调用被拒绝 | 开发者权限未配置 | 检查 `UserProfile.metadata["is_developer"]` |
| WebUI 无法访问 | 端口被占用或服务未启动 | 检查 `global_config.json` 中的 `webui_host`/`webui_port` |

---

## 完成检查清单

- [ ] 异常已稳定复现。
- [ ] 根因已定位到具体代码行或逻辑。
- [ ] 修复方案已验证（最小复现测试通过）。
- [ ] 相关模块全量测试通过。
- [ ] 未引入新的异常或副作用。
- [ ] 若涉及 SKILL/文档相关变更，已同步更新（参考 `code-change-sync` SKILL）。

## 交付输出

提供一份简要诊断报告，包含：
1. **问题描述**：异常现象与复现步骤。
2. **根因分析**：定位到的具体原因。
3. **修复方案**：修改了哪些代码。
4. **验证结果**：测试通过情况。
5. **后续建议**：是否需要增强日志、补充测试或更新文档。
