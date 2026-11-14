---
name: workflow-reuse
description: 多步、可能被打断、之后要接着做的外部任务，用 workflow_state 登记并按步推进；用户说“继续”“再来一次”“按刚才的流程”或省略参数时，优先恢复已登记流程。只做一个动作时不需要它。
---

# 流程复用

## 什么时候用

- **用**：任务有两个以上外部副作用步骤，做到一半可能被打断（工具失败、超时、进程重启），之后要接着做；或者用户让你继续、重试、照上次的流程做。
- **不用**：只做一个动作——点个赞、发一张图、上传一个文件、跑一条命令。直接调用对应工具；为它登记流程只会多出四五次调用。

## 固定路径

```text
list -> 找到候选就 resume -> 没找到就 resume 目标 key
     -> found=false 时 begin（自动登记）-> 确认 registered=true
     -> claim -> 专用 Tool -> checkpoint/fail
     -> 有 next_step 就继续 claim -> 没有 next_step 时 checkpoint 自动完成
```

`workflow_state` 的参数是普通 JSON 工具参数。`state_json` 必须是 JSON 字符串，例如 `"{\"user_id\":123}"`。工具返回的 `key`、`version`、`revision`、`claim_token` 和 `idempotency_key` 必须原样复制，不能猜或随机生成。

## 1. 先检查流程目录

决定用流程之后，先调用：

```json
{"action":"list"}
```

列表只提供候选摘要，不是执行许可。找到目标相同、用途相同的流程时，复制它的 `key` 和 `version`，再调用：

```json
{"action":"resume","key":"<列表中的 key>","version":"<列表中的 version>"}
```

如果列表没有匹配项，仍然要用准备好的稳定 key 调用 `resume`：

```json
{"action":"resume","key":"<稳定的流程 key>","version":"1"}
```

key 必须代表一项具体业务，目标不同就用不同 key。例如 `napcat.like:123456`、`serial_novel:book-7:chapter-12`。

## 2. 没有流程时登记

`resume` 返回 `found=false` 时调用一次 `begin`，只保存目标 ID、章节号等最小事实：

```json
{"action":"begin","key":"<同一个 key>","version":"1","state_json":"{\"target_id\":123}"}
```

必须确认 `begin` 返回顶层 `registered=true`；没有该字段或值为 false 时不得调用任何外部 Tool。

`begin` 遇到已有 key 会返回 `reused=true` 和 `registered=true`，这表示流程已经登记，不表示应该再次执行动作。

根据 `resume` 状态分支：

- `status=active` 或 `status=failed`：使用返回的 `next_step` 继续，不要从第一步重来。
- `status=completed`：不要再次 `begin` 或 `claim`。用户明确要求重新执行时，用最新 `revision` 调用 `restart`，然后从新流程继续。
- `conflict=true` 或 `success=false`：不要执行外部操作，重新 `list`/`resume`，以最新状态为准。

## 3. 占用一个副作用步骤

调用专用外部 Tool 之前必须先 `claim`。后续步骤的 `step` 必须等于 `resume` 或上一次 `checkpoint` 返回的 `next_step`；`tool_name` 填实际 Tool 名称；`expected_revision` 复制最近结果的顶层 `revision`。

```json
{
  "action":"claim",
  "key":"<同一个 key>",
  "version":"1",
  "step":"<next_step>",
  "tool_name":"<实际 Tool 名称>",
  "idempotency_key":"<由目标和动作组成的稳定幂等键>",
  "expected_revision":<最近结果的 revision>,
  "state_json":"{\"target_id\":123}"
}
```

幂等键必须由业务事实组成，例如 `napcat.like:123456:1` 或 `serial_novel:book-7:chapter-12:publish`。同一个动作重试时必须使用同一个键。

只按 `claim` 返回值分支：

| 返回值 | 下一步 |
|---|---|
| `claimed=true` | 保存顶层 `claim_token` 和 `revision`，然后只调用一次专用 Tool。 |
| `already_done=true` | 不调用专用 Tool，使用返回的 `result`，按返回的 `next_step` 继续。 |
| `in_progress=true` | 不调用专用 Tool，也不要立即再次 claim；本轮结束，之后 resume 或等租约过期。 |
| `conflict=true` 或 `success=false` | 不执行副作用，重新 list/resume，比较最新状态。 |

只有 `claimed=true` 才是执行许可。`workflow_state` 返回 `success=true` 本身不是执行专用 Tool 的许可。

## 4. 记录结果

专用 Tool 成功后调用 `checkpoint`，原样复制 `key`、`version`、`step`、`tool_name`、`idempotency_key`、`claim_token`，并使用 claim 返回的 `revision`：

```json
{
  "action":"checkpoint",
  "key":"<同一个 key>",
  "version":"1",
  "step":"<同一个 step>",
  "tool_name":"<同一个 Tool>",
  "idempotency_key":"<同一个幂等键>",
  "claim_token":"<从 claim 复制>",
  "expected_revision":<claim 返回的 revision>,
  "next_step":"<下一步；没有就留空>",
  "state_json":"{\"external_id\":\"m1\"}",
  "summary":"简短结果摘要"
}
```

专用 Tool 失败时调用 `fail`，复用同一个 `step`、`idempotency_key`、`claim_token` 和当前 `revision`。没有确认外部操作成功时不能 checkpoint。

## 5. 完成或重启

- `checkpoint` 返回的 `next_step` 非空：回到第 3 步，只 claim 这个步骤。
- `next_step` 为空：本次 `checkpoint` 已将流程标记为 `completed`，不再调用额外的完成 action。
- 用户明确要求对已完成动作再执行一轮：使用最新 `revision` 调用 `restart`，不要用 `clear`、随机 key 或第二次 `begin` 绕过历史。

## 禁止事项

- 用流程时不得跳过 `list`；不得在流程未登记或 `registered` 未验证时执行它的步骤。
- 已登记流程里的步骤，不得在 `claim` 之前调用有副作用的专用 Tool。
- 不得因为忘记上次结果而重复成功步骤；使用 `already_done` 和保存的 `result`。
- 不得跳过 `next_step`，不得覆盖 revision 冲突，不得使用过期 claim token。
- 不得用 bash、curl、原始 WebSocket 或另一套实现重新探索、模拟或重复已有 Tool。
- 只保存目标事实、外部 ID 和短摘要；不要保存密码、令牌、Cookie、完整聊天记录、完整日志、堆栈或大文件。
