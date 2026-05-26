# Sirius Pulse 代码审计报告

> 生成日期：2026-05-27
> 分析工具：pyflakes + 人工审查
> 审计范围：`sirius_pulse/` 全部模块

---

## 一、死代码（Dead Code）

### 1.1 可安全删除的整个文件（~660 行）

| 文件 | 行数 | 原因 |
|------|------|------|
| `trait_taxonomy.py` | ~53 | 完全未使用，350 个关键词的特征分类字典从未被任何代码 import 或引用 |
| `background_tasks.py` | ~304 | 完全未使用，被 `core/bg_tasks.py` 的 `BackgroundTasksMixin` 取代 |

删除 `background_tasks.py` 时需同步清理 `__init__.py` 中的导入与 `__all__` 条目。

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

#### ① 统一序列化机制（消除 ~500 行手写代码）

项目已有通用的 `JsonSerializable` mixin，但 14+ 个模型类完全没有使用它，手写了大量重复的 `to_dict()`/`from_dict()`。

涉及文件：
- `models/persona.py`（PersonaProfile, ~30 字段 × 2）
- `models/intent_v3.py`（IntentAnalysisV3, ~30 字段 × 2）
- `persona_config.py`（NapCatAdapterConfig, PersonaExperienceConfig, ~35 字段 × 2）
- `memory/semantic/models.py`（GroupSemanticProfile, UserSemanticProfile, ~25 字段 × 2）
- `memory/biography/models.py`（UserPersonaCard, ~25 字段 × 2）
- 其他 6 个文件（BasicMemoryEntry, DiaryEntry, EmotionState 等, ~40 字段 × 2）

#### ② 提取公共 JSON I/O 工具（消除 14+ 处重复）

原子写入模式 `tmp.write_text(json.dumps(...)) + tmp.replace(path)` 出现 14 次，JSON 读取模式 `json.loads(path.read_text(encoding="utf-8"))` 出现 19 次。

#### ③ 统一重试机制（消除 4 套独立实现）

| 位置 | 文件 |
|------|------|
| `brain.py:557-599` | LLM 调用重试 |
| `diary/generator.py:101-140` | JSON 解析重试 |
| `skills/executor.py:262-286` | SKILL 执行重试 |
| `github/events.py:47-68` | HTTP 请求重试 |

#### ④ 拆分超大文件

| 文件 | 行数 | 建议拆分 |
|------|------|----------|
| `core/bg_tasks.py` | 1411 | → `bg_proactive.py` + `bg_diary.py` + `bg_delayed.py` + `bg_prompt_builders.py` |
| `core/prompt_factory.py` | 1168 | → `prompt_builder.py` + `prompt_renderer.py` |
| `core/engine_core.py` | 1180 | 初始化/持久化/表情包可进一步分离 |

### 2.2 中优先级

#### ⑤ 消除函数级重复

| 函数 | 出现次数 | 位置 |
|------|----------|------|
| `_parse_sticker_tags()` | 2 次 | engine_core.py:1007, brain.py:629 |
| `_strip_conversation_history_xml()` | 3 次 | engine_core.py:85, brain.py:616, helpers.py:424 |

建议移入 `core/utils.py` 作为纯函数。

#### ⑥ WebUI 错误处理装饰器（消除 21 处重复）

`return _json_response({"error": str(exc)}, 500)` 出现 21 次，建议创建 `@handle_api_errors` 装饰器。

#### ⑦ 提取魔法数字常量

| 值 | 出现次数 | 含义 |
|------|----------|------|
| `300` | 6 次 | 5 分钟超时/窗口 |
| `512` | 4 次 | 默认 max_tokens |
| `60` | 6 次 | 1 分钟 |
| `3600` | 3 次 | 1 小时 |

### 2.3 低优先级

#### ⑧ 文件命名改进

- `models/models.py` → `models/conversation.py`（文件名含义不清）

#### ⑨ store/manager 分层不一致

- `memory/user/simple.py` 和 `memory/glossary/manager.py` 没有独立的 store 层

#### ⑩ `__init__.py` 中过多的 re-export

`__init__.py` re-export 了大量符号（含全部 18 个从未使用的异常类），建议精简。

---

## 三、总结

| 类别 | 数量 | 估算可清理行数 |
|------|------|---------------|
| 可删除的整个文件 | 2 个 | ~660 行 |
| 未使用的导入 | 86 处 | ~86 行 |
| 未使用的变量 | 12 处 | ~12 行 |
| 从未 raise/except 的异常类 | 18 个 | ~350 行 |
| 重复的序列化代码 | 14+ 类 | ~500 行 |
| 重复的 JSON I/O 模式 | 33 处 | ~150 行 |
| 重复的重试逻辑 | 4 套 | ~100 行 |
| 函数级重复 | 2 个函数 × 2-3 处 | ~30 行 |
| **合计可清理/优化** | | **~1900 行** |
