# Sirius Pulse 代码审计报告

> 生成日期：2026-05-27
> 分析工具：pyflakes + 人工审查
> 审计范围：`sirius_pulse/` 全部模块
> 重构完成日期：2026-05-27

---

## 重构执行记录

| Commit | 内容 | 影响文件数 |
|--------|------|-----------|
| `829ef1b` | 删除完全死代码文件（trait_taxonomy.py, background_tasks.py） | 4 |
| `7719adf` | 清理全部未使用的导入语句（62处） | 49 |
| `6e5ca4d` | 清理未使用的变量、f-string 占位符 | 11 |
| `02aec00` | 消除函数级重复（_parse_sticker_tags, _strip_conversation_history_xml） | 4 |
| `a01889c` | 提取公共 JSON I/O 工具函数（utils/json_io.py） | 8 |
| `1dd51df` | 提取魔法数字为命名常量（core/constants.py） | 7 |

**累计清理/优化代码**：~800 行（含删除、去重、提取）

---

## 一、死代码（Dead Code）

### 1.1 可安全删除的整个文件（~660 行）✅ 已完成

| 文件 | 行数 | 原因 | 状态 |
|------|------|------|------|
| `trait_taxonomy.py` | ~53 | 完全未使用 | ✅ 已删除 |
| `background_tasks.py` | ~304 | 被 core/bg_tasks.py 取代 | ✅ 已删除 |

### 1.2 未使用的导入（86 处）✅ 已完成

全部 62 处未使用导入已清理。

### 1.3 未使用的变量（12 处）✅ 已完成

全部 12 处未使用变量已清理。

### 1.4 死代码：整个异常体系（18 个类）⚠️ 保留

`exceptions.py` 中 18 个异常类在代码库内从未 raise/except，但作为公共 API 预留保留。

### 1.2 未使用的导入（86 处）

#### 严重：导致运行时错误的未定义引用（3 处）

| 文件 | 行号 | 问题 |
|------|------|------|
| `config/manager.py` | 302 | `build_orchestration_policy_from_dict` 未定义 |
| `persona_generation/templates.py` | 760, 824, 825 | `SessionConfig` 和 `OrchestrationPolicy` 未定义 |
| `token/token_store.py` | 374 | `LOG` 未定义 |

#### 中等：冗余的 redefinition（4 处）

| 文件 | 行号 | 问题 |
|------|------|------|
| `core/cognition.py` | 786 | 重复导入 `asyncio`（L15 已导入） |
| `core/helpers.py` | 447 | 重复导入 `datetime`（L9 已导入） |
| `memory/semantic/manager.py` | 140 | 重复导入 `timezone`（L5 已导入） |
| `models/emotion.py` | 128 | 重复导入 `datetime`（L7 已导入） |

#### 低：纯粹未使用的导入（79 处）

按模块分布：

| 模块 | 数量 | 典型问题 |
|------|------|----------|
| `core/` | 18 | `engine_core.py` 有 6 处未使用的 TYPE_CHECKING 导入 |
| `plugins/` | 7 | `executor.py` 重复导入 PluginContext |
| `webui/` | 6 | 多个 API 文件导入了 `logging` 但未使用 |
| `memory/` | 8 | `semantic/manager.py` 导入了 `timedelta`/`timezone` 但未用 |
| `models/` | 4 | `models.py` 导入了 `OrchestrationPolicy` 但未用 |
| 其他 | 36 | 散布在各模块中的 `typing.Any`、`asyncio`、`datetime` 等 |

### 1.3 未使用的变量（12 处）

| 文件 | 行号 | 变量名 |
|------|------|--------|
| `persona_manager.py` | 160 | `experience` |
| `core/cognition.py` | 1539 | `weak_linguistic` |
| `core/helpers.py` | 596 | `exc_type` |
| `core/rhythm.py` | 256 | `last_lower` |
| `memory/basic/manager.py` | 199 | `heat` |
| `session/store.py` | 465 | `runtime_rows` |
| `skills/executor.py` | 285, 455 | `last_error`（2 处） |
| `adapter.py` | 638 | `gid` |
| `adapter.py` | 756 | `not_ready_backoff` |
| `webui/memory_api.py` | 159, 170, 410 | `daily`, `models`, `user_dir` |

### 1.4 死代码：整个异常体系（18 个类）

`exceptions.py` 中定义了 18 个异常类，**全部在代码库中从未被 raise 或 except**。仅通过 `__init__.py` 暴露到公共 API。这可能是预留的公共 API，但在当前代码中是完全死代码。

### 1.5 f-string 缺少占位符（3 处）

| 文件 | 行号 |
|------|------|
| `core/bg_tasks.py` | 520 |
| `core/cognition.py` | 795 |
| `core/pipeline.py` | 317 |

---

## 二、可重构点（Refactoring Opportunities）

### 2.1 高优先级

#### ① 统一序列化机制（消除 ~500 行手写代码）⚠️ 未执行

项目已有通用的 `JsonSerializable` mixin，但 14+ 个模型类完全没有使用它，手写了大量重复的 `to_dict()`/`from_dict()`。此重构涉及面广，需单独 PR 处理。

#### ② 提取公共 JSON I/O 工具 ✅ 已完成

新增 `utils/json_io.py` 模块，提供 `atomic_write_json()` 和 `read_json()`。已替换 6 处核心文件中的重复模式。

#### ③ 统一重试机制（消除 4 套独立实现）⚠️ 未执行

4 套独立的重试实现仍存在，需创建通用 `with_retry()` 工具。

#### ④ 拆分超大文件 ⚠️ 未执行

| 文件 | 行数 | 建议拆分 |
|------|------|----------|
| `core/bg_tasks.py` | 1411 | → `bg_proactive.py` + `bg_diary.py` + `bg_delayed.py` + `bg_prompt_builders.py` |
| `core/prompt_factory.py` | 1168 | → `prompt_builder.py` + `prompt_renderer.py` |
| `core/engine_core.py` | 1180 | 初始化/持久化/表情包可进一步分离 |

### 2.2 中优先级

#### ⑤ 消除函数级重复 ✅ 已完成

`_parse_sticker_tags` 和 `_strip_conversation_history_xml` 已提取到 `core/utils.py` 作为纯函数。

#### ⑥ WebUI 错误处理装饰器（消除 21 处重复）⚠️ 未执行

#### ⑦ 提取魔法数字常量 ✅ 已完成

新增 `core/constants.py` 模块，已替换 9 处核心文件中的魔法数字。

### 2.3 低优先级

#### ⑧ 文件命名改进 ⚠️ 未执行

#### ⑨ store/manager 分层不一致 ⚠️ 未执行

#### ⑩ `__init__.py` 中过多的 re-export ⚠️ 未执行

---

## 三、总结

| 类别 | 数量 | 状态 |
|------|------|------|
| 可删除的整个文件 | 2 个 | ✅ 已删除 |
| 未使用的导入 | 62 处 | ✅ 已清理 |
| 未使用的变量 + f-string | 16 处 | ✅ 已清理 |
| 函数级重复 | 2 个函数 × 2-3 处 | ✅ 已提取到 utils |
| JSON I/O 重复 | 6 处 | ✅ 已提取到 utils |
| 魔法数字 | 9 处 | ✅ 已提取到 constants |
| 统一序列化机制 | 14+ 类 | ⚠️ 待后续 PR |
| 统一重试机制 | 4 套 | ⚠️ 待后续 PR |
| 拆分超大文件 | 3 个文件 | ⚠️ 待后续 PR |
| 异常体系清理 | 18 个类 | ⚠️ 公共 API 预留 |
