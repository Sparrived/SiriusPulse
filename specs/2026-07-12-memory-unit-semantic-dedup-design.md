# 记忆单元语义去重与合并设计

日期：2026-07-12

## 1. 背景

当前记忆单元在每次提取时生成新的随机 `unit_id`，`MemoryUnitManager.add_units()`
只按 `unit_id` 判重。生成器也看不到已有记忆，因此同一事实经过多次对话后会形成多条
语义近似的 `MemoryUnit`。

重复单元会浪费检索预算，并可能让同一事实以多种措辞重复进入 Prompt。本设计在保留
记忆单元独立性、来源证据和群隔离的前提下，为实时写入和历史存量提供同一套语义去重
机制。

## 2. 目标

- 同一群、同一记忆边界内，语义等价的事实最终只保留一个 canonical unit。
- 兼容补充信息合并进 canonical unit，而不是生成新的碎片。
- 冲突或时间变化的事实继续保留，并显式建立冲突关系。
- 完整保留 `source_ids`，保证基础记忆 checkpoint 能正确完成。
- 新写入实时去重，现有存量可从 WebUI 显式扫描和清理。
- embedding 或裁决模型故障时不丢失新记忆。
- 清理过程可预览、可检测过期、可备份恢复。

## 3. 非目标

- 不跨群合并记忆。
- 不合并 `scope`、`scope_id` 或 `unit_type` 不同的单元。
- 不改造日记、术语、用户画像或基础记忆的去重策略。
- 不新增数据库、向量数据库或第三方依赖。
- 首版不提供 CLI 清理命令、周期性全量扫描或自动删除备份。
- 首版不提供任务取消；关闭弹窗不会中止后台扫描。

## 4. 核心术语

- **新单元**：本轮生成或手工新增、编辑后待判定的 MemoryUnit。
- **候选单元**：与新单元处于相同边界、embedding 相似度达到召回阈值的已有单元。
- **canonical unit**：重复或补充事实合并后继续保留 `unit_id` 的单元。
- **裁决**：模型返回的 `NEW`、`DUPLICATE`、`MERGE` 或 `CONFLICT` 判断。
- **扫描快照**：历史扫描开始时的单元集合及其稳定指纹。

## 5. 去重边界

只有以下四个字段全部相同的单元才允许互相比较：

```text
group_id + scope + scope_id + unit_type
```

不同 `group_id` 永远不比较。`scope_id` 为空时仍按空字符串参与边界计算。边界字段不允许
由裁决模型修改。

## 6. 两阶段判定

### 6.1 确定性完全重复

摘要先经过以下规范化：

1. Unicode NFKC 规范化。
2. `casefold()`。
3. 连续空白折叠为一个空格并去除首尾空白。
4. 去除末尾连续的中英文句号、问号和感叹号。

同一边界内规范化摘要完全相同的单元直接判为 `DUPLICATE`，不调用模型。

### 6.2 embedding 召回

对非完全重复的新单元，复用现有 embedding 服务。用于 embedding 的文本继续由以下内容
组成：

```text
summary + participants + topics + keywords
```

`MemoryUnitIndexer` 提供只返回余弦相似度的近邻查询，不混入现有检索中的关键词分和质量
分。候选限定为同一边界，初始召回规则为：

- 最多 5 条。
- 余弦相似度不低于 `0.80`。
- 按相似度降序。

阈值作为模块内部常量，不新增配置项。后续只有在真实误判样本证明需要调节时才开放配置。

embedding 不可用或编码失败时，只执行确定性完全重复检查，其余单元按 `NEW` 保存。

### 6.3 模型裁决

有语义候选时，使用当前 `memory_extract` 路由得到的模型，通过现有 `Brain.raw_call()` 发起
一次严格 JSON 请求。请求 purpose 为 `memory_unit_deduplicate`。

模型输入只包含新单元和最多 5 条候选，不包含整个记忆库。返回协议：

```json
{
  "decision": "NEW|DUPLICATE|MERGE|CONFLICT",
  "target_unit_id": "mem_xxx",
  "merged_summary": "",
  "reason": "简短判断依据"
}
```

校验规则：

- `decision` 必须是四个允许值之一。
- 非 `NEW` 的 `target_unit_id` 必须来自本次候选。
- `MERGE` 必须返回非空、长度不超过 180 字符的 `merged_summary`。
- `DUPLICATE` 和 `CONFLICT` 忽略 `merged_summary`。
- JSON、字段或目标 ID 非法时降级为 `NEW`。

模型调用异常或超时同样降级为 `NEW`，确保去重故障不会丢失记忆。

## 7. 裁决语义

### 7.1 NEW

新单元是独立事实，正常分配并保存自己的 `unit_id`。

### 7.2 DUPLICATE

新旧单元表达同一事实，主体、对象、状态和时间含义等价。保留旧 canonical unit 的摘要、
`unit_id` 和最早 `created_at`，只合并证据与结构化字段。

### 7.3 MERGE

新旧单元描述同一事实，新内容是兼容补充。保留旧 canonical unit 的 `unit_id` 和最早
`created_at`，采用模型返回的 `merged_summary`。

模型 Prompt 必须禁止添加输入中不存在的事实，并要求摘要是一条完整、自洽的第三人称
事实句。

### 7.4 CONFLICT

新旧单元描述同一事实槽位，但值互斥、状态发生变化或时间含义不同。两条都保留，不尝试
生成“折中事实”，并在双方 metadata 中建立冲突关系。

### 7.5 保守判断要求

以下情况必须判为 `NEW` 或 `CONFLICT`，不能合并：

- 同一参与者的不同事件。
- “计划做”与“已经完成”。
- 历史偏好与当前偏好。
- 相同主题但对象、地点或时间不同。
- 仅因关键词相同而相关。
- 模型无法确定是否等价。

## 8. 字段合并

`DUPLICATE` 和 `MERGE` 使用相同的确定性字段规则：

| 字段 | 规则 |
|---|---|
| `unit_id` | 保留最早 canonical unit 的 ID |
| `group_id/scope/scope_id/unit_type` | 保持 canonical unit，不允许模型修改 |
| `created_at` | 取最早时间 |
| `summary` | DUPLICATE 保留旧值；MERGE 使用已校验的模型结果 |
| `source_ids` | 稳定顺序去重并集，不设上限 |
| `participants` | 稳定顺序去重并集，最多 8 项 |
| `topics` | 稳定顺序去重并集，最多 8 项 |
| `keywords` | 稳定顺序去重并集，最多 12 项 |
| `salience` | 取较高值 |
| `confidence` | 取较高值 |
| `lifespan` | 按 short、medium、long 取更长值 |
| `should_prompt` | 逻辑或 |

当 `summary`、`participants`、`topics` 或 `keywords` 中任一项变化时，旧 embedding 必须失效
并重新计算，因为这些字段共同构成索引文本。

合并历史写入现有 `metadata`：

```json
{
  "revision_count": 3,
  "merged_unit_ids": ["mem_removed"],
  "last_merged_at": "2026-07-12T00:00:00+00:00",
  "decision": "merge"
}
```

`merged_unit_ids` 稳定去重。`revision_count` 每次合并加一。

冲突关系写入双方 metadata：

```json
{
  "conflicts_with": ["mem_other"],
  "conflict_reason": "偏好发生变化"
}
```

`conflicts_with` 稳定去重；`conflict_reason` 保存最近一次裁决原因。

## 9. 实时写入流程

`MemoryUnitManager.generate_from_candidates()` 在生成器返回之后、持久化之前逐条处理新单元：

1. 确保目标群已有单元已加载。
2. 进行完全重复检查。
3. 进行同边界近邻召回。
4. 必要时调用模型裁决。
5. 按裁决更新当前批次内的 canonical 集合。
6. 立即把本条结果加入临时索引，使同批后续单元也能与它比较。
7. 整批处理完成后对群文件原子写盘一次，并替换正式内存索引。

`generate_from_candidates()` 返回受影响的 canonical units。每个返回单元包含并集后的
`source_ids`，因此现有 checkpoint 调用方仍能删除本轮已覆盖的基础记忆。

`MemoryUnitManager` 增加进程内异步变更锁。实时写入和历史 apply 必须持有该锁；普通检索
不持锁。

## 10. WebUI 历史清理

### 10.1 入口

“清理重复”按钮只在“记忆单元 / MemoryUnit”页签显示，位于现有“新增单元 / 刷新”工具栏
右侧。按钮发起扫描，不直接删除数据。

人格 worker 未运行时按钮禁用，并显示“请先启动当前人格”。首版不在 WebUI 进程中重复
初始化模型或 embedding。

### 10.2 扫描报告

弹窗展示：

- 扫描单元数。
- 完全重复数。
- 建议合并数。
- 冲突数。
- 保持不变数。
- 按群列出的 canonical 摘要、候选摘要、裁决和原因。

扫描只读，不写 memory unit 文件或内存索引。报告保存到：

```text
{persona}/logs/memory-dedupe/{job_id}.json
```

报告包含每个群的输入指纹、模型裁决、失败项和统计信息。WebUI 可下载该 JSON 报告。

每个边界桶按 `created_at`、`unit_id` 升序处理。最早单元先进入工作副本中的 canonical
集合，之后每个单元只与当前 canonical 集合比较：

- `NEW` 和 `CONFLICT` 将新单元加入 canonical 集合。
- `DUPLICATE` 和 `MERGE` 更新目标 canonical unit，不再保留新单元。
- 更新后的 canonical unit 立即参与同一扫描内后续单元的比较。

该顺序保证相同输入得到相同保留 ID，并能把三条以上的重复链逐步收敛为一条。扫描只修改
工作副本，所有决策和最终工作副本都写入报告，正式数据仍保持不变。

### 10.3 应用清理

用户点击“应用 N 项清理”后：

1. worker 重新计算所有相关群的输入指纹。
2. 任一群指纹变化时，整个 apply 返回 `stale`，不做部分写入。
3. 指纹一致时获取 manager 变更锁。
4. 将完整 `memory_units` 目录备份到：

```text
{persona}/backups/memory_units/{timestamp}/
```

5. 在临时目录生成全部目标群文件。
6. 所有临时文件成功后，再以原子替换方式更新正式文件。
7. 替换相关群的内存索引和 checkpoint source 集合。
8. 任一步失败时从本次备份恢复，并将任务标记为 `failed`。

输入指纹采用对群内完整 unit 字典进行稳定 JSON 序列化后的 SHA-256；字段排序和列表顺序
都参与计算。这样扫描后发生的新增、编辑、删除或 embedding 重算都会使报告过期。

## 11. WebUI 与 worker 通信

复用现有 `engine_state` 文件通信模式，不新增网络端口或消息队列。每个人格同时只允许一个
去重任务。

状态目录：

```text
{persona}/engine_state/memory_dedupe/
  request.json
  status.json
  reconcile.json
```

请求和状态文件都通过临时文件替换方式原子写入。任务状态：

```text
queued -> scanning -> ready -> applying -> completed
                            \-> stale
         \--------------------> failed
```

`status.json` 只保存 job ID、状态、进度、错误摘要和报告路径，详细裁决放在日志报告中。
`reconcile.json` 独立保存手工 CRUD 后待刷新的群和单元 ID；WebUI 写入时与现有内容稳定
去重合并，worker 取走时使用原子改名，避免覆盖历史扫描请求或丢失并发编辑通知。

WebUI API：

```text
POST /api/persona/memory-units/dedupe/scan
GET  /api/persona/memory-units/dedupe/status
POST /api/persona/memory-units/dedupe/apply
GET  /api/persona/memory-units/dedupe/report
```

- 新扫描与已有任务冲突时返回 HTTP 409。
- apply 必须提交当前 `job_id`，不接受任意报告路径。
- worker 不在线时 scan 返回 HTTP 409。
- API 继续使用现有 WebUI 鉴权，不增加公开匿名入口。

前端在弹窗打开时轮询 status；关闭弹窗后任务继续，重新打开可恢复当前任务状态。

## 12. 手工 CRUD 一致性

历史 apply 处于 `applying` 状态时，记忆单元 POST、PUT、DELETE 返回 HTTP 409，防止 WebUI
跨进程覆盖正在替换的文件。GET 仍可用。

手工新增或编辑成功后，WebUI 写入一个针对该单元的 reconcile 请求，由运行中的 worker
使用同一裁决器处理。worker 不在线时只执行完全重复检查；其他语义重复在下次显式扫描时
处理。该规则提供最终一致性，不改变现有 CRUD 的同步成功响应。

worker 完成 reconcile 后必须刷新相关群的内存索引。删除操作也通知 worker 刷新相关群，
解决 WebUI 直接编辑文件后运行时索引可能陈旧的问题。

## 13. 并发与一致性

- 实时提取与历史 apply 在同一 manager 变更锁下串行。
- 历史 scan 使用复制快照，不长期持锁，不阻塞聊天或实时检索。
- scan 期间允许实时新增；输入指纹会让旧报告在 apply 时变为 `stale`。
- apply 期间 WebUI 记忆单元写操作返回 409。
- 文件替换完成后再切换内存索引；失败时保留或恢复旧文件与旧索引。
- 同一批实时生成的单元按顺序进入临时索引，避免批内重复。

## 14. 错误处理

| 场景 | 行为 |
|---|---|
| embedding 不可用 | 只做完全重复检查；其他新单元正常保存 |
| 裁决模型失败或非法 JSON | 新单元按 NEW 保存；记录 warning |
| 扫描中的单项裁决失败 | 报告为未确定，不进入 apply 计划 |
| worker 停止 | 当前任务 failed；WebUI 保留错误状态 |
| 扫描后数据变化 | apply 返回 stale，不修改数据 |
| 备份失败 | apply 立即失败，不修改数据 |
| 临时文件生成失败 | apply 失败，不替换正式文件 |
| 部分替换失败 | 从本次备份恢复，任务 failed |
| 索引重建失败 | 恢复文件和旧索引，任务 failed |

## 15. 测试策略

### 15.1 单元测试

- 摘要规范化与完全重复判定。
- 同群、同边界候选召回；跨群和跨边界排除。
- 四种裁决 JSON 的解析与非法响应降级。
- 各字段确定性合并、metadata 记录和 embedding 失效。
- lifespan 排序和稳定顺序去重。
- 输入指纹稳定且能检测任意单元变化。

### 15.2 业务测试

- 两次生成同一事实后只剩一个 canonical unit，双方 `source_ids` 均保留。
- 兼容补充生成一条合并摘要。
- 偏好变化保留两条并互相标记冲突。
- 不同群的相同摘要分别保留。
- embedding 或模型故障不会丢失新单元。
- 重启 manager 后文件、索引和 checkpoint sources 一致。

### 15.3 历史任务测试

- dry-run 不修改文件、索引或 checkpoint sources。
- apply 创建完整备份并得到报告中的预期结果。
- scan 后新增单元会使 apply 返回 stale。
- 备份、写盘和索引重建故障均不会留下部分结果。
- 同一人格不能并发启动两个扫描任务。
- apply 期间 CRUD 写操作返回 409。

### 15.4 WebUI 测试

- 清理按钮只在记忆单元页签显示。
- worker 离线时按钮禁用。
- queued、scanning、ready、applying、completed、stale、failed 状态均正确渲染。
- 报告统计、明细、下载和 apply 数量正确。
- apply 前有明确确认，失败和过期状态不会关闭结果弹窗。

## 16. 验收标准

1. 同群同边界的语义等价事实经过实时写入或历史 apply 后只存在一个 `unit_id`。
2. 不同群的相同事实不发生合并。
3. MERGE 后摘要包含双方已知事实，且不增加无来源内容。
4. DUPLICATE 和 MERGE 后所有 `source_ids` 可追溯。
5. CONFLICT 后两条单元都可检索，并存在双向关联。
6. 任何外部服务故障都不会导致新记忆静默丢失。
7. dry-run 对持久化和运行时索引零修改。
8. 过期报告不能覆盖新数据。
9. apply 前必有可恢复的完整备份。
10. apply 后无需重启人格，检索立即使用新 canonical 集合。

## 17. 实施边界

实施应优先复用以下现有模块：

- `memory/units/generator.py`：裁决 Prompt 和严格 JSON 解析。
- `memory/units/indexer.py`：纯语义近邻查询与群索引替换。
- `memory/units/manager.py`：实时 reconcile、历史 scan/apply 和变更锁。
- `core/bg_tasks.py`：worker 端任务轮询与执行。
- `webui/memory_api.py`：任务 API、维护状态检查和报告读取。
- `webui/static/pages/memory-viz.js`：按钮、进度与报告弹窗。

只有裁决 Prompt、扫描报告模型或任务协议无法在现有文件中保持清晰时才新增小型模块；不为
单一实现引入接口、工厂或独立服务层。
