# LLM Transport Sidecar 契约（v1）

> 本文件是 Go 侧车与 Python 侧**唯一的接口约定**。两侧实现都必须以本文件为准；
> 任何字段改名、语义变化，先改本文件再改代码。
>
> 目标：把「对 AMKR 的 HTTP 调用 + SSE 聚合 + 多模态内联」从 Python 迁到 Go，
> **Python 保留全部编排**（prompt 组装、工具循环、重试、兜底任务、token 记账）。

## 1. 进程与网络

| 项 | 约定 |
| --- | --- |
| 监听地址 | 固定 `127.0.0.1`（不加 `--host` 时）；**不得**监听 `0.0.0.0` |
| 端口 | 由 Python 传入 `--port <n>`（Python 先绑 0 取空闲端口再释放）；`--port 0` 时打印实际端口 |
| 就绪信号 | 监听成功后向 stdout 打印**单行** `READY <port>` 并 flush，然后进入服务 |
| 鉴权 | 启动参数 `--token <t>`（或环境变量 `LLM_TRANSPORT_TOKEN`）。所有 `/v1/*` 请求必须带 `Authorization: Bearer <t>`，否则 401 |
| 健康检查 | `GET /health` 免鉴权，返回 `{"status":"ok","version":"0.1.0","pid":1234}` |
| 退出 | 收到 SIGINT/SIGTERM 后停止接新请求、等在飞请求最多 5 s、退出码 0 |
| 日志 | 全部写 stderr（`READY` 之外不污染 stdout） |

Python 侧负责进程生命周期（启动、健康轮询、退出、异常重启），Go 侧**不做**自动重启。

## 2. 请求：`POST /v1/generate`

```jsonc
{
  "upstream": {
    "base_url": "http://127.0.0.1:8000",   // AMKR 根地址，末尾斜杠由 Go 归一
    "api_key": "amkr_ik_…",                 // 该工作空间的推理 key（逐请求传入，sidecar 不落盘）
    "workspace": "sirius-pulse/yuebai",     // 可空；非空时作为 X-AMKR-Workspace 发送
    "timeout_seconds": 30                    // >0；见 §4 超时语义
  },
  "request": {
    "model": "response_generate",            // AMKR 任务名，原样透传，不得改写
    "system_prompt": "……",
    "messages": [
      {"role": "user", "content": "文本"},
      {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "D:\\imgs\\a.png"}},
        {"type": "text", "text": "看图"}
      ]}
    ],
    "tools": null,                            // OpenAI tools JSON 原样透传
    "tool_choice": null,
    "response_format": null,
    "purpose": "response_generate"           // 仅用于日志
  }
}
```

**禁止**出现采样参数（temperature/max_tokens 等）：AMKR 的任务定义是权威，多传会 400。

## 3. 成功响应：HTTP 200

```jsonc
{
  "content": "……",                       // 可为空串（有 tool_calls 时正常）
  "reasoning_content": "……",              // 无则空串
  "tool_calls": [                          // 无则 []
    {"id": "call_1", "type": "function", "function_name": "f", "function_arguments": "{\"a\":1}"}
  ],
  "finish_reason": "stop",
  "usage": {"prompt_tokens": 1234, "completion_tokens": 56, "total_tokens": 1290}, // 上游原始 dict，无则 null，**不得改写**
  "transport": {
    "content_type": "text/event-stream",   // 或 "application/json"（网关忽略 stream 时的回退）
    "local_image_conversions": 1,
    "upstream_ms": 2190.5,
    "path": "sse"                          // "sse" | "json"
  }
}
```

## 4. 语义对齐（必须与 `providers/openai_compatible.py` 行为一致）

1. **恒定流式**：请求体固定 `stream: true` + `stream_options: {"include_usage": true}`。
2. **超时**：`timeout_seconds` 是**块间静默超时**，不是整请求总时长（Python 的 httpx 流式语义）。
   实现建议：`http.Client.Timeout = 0`，看门狗在连续 `timeout_seconds` 无数据时取消请求 → 返回 `UPSTREAM_TIMEOUT`。
3. **SSE 解析**：只认单行 `data:`；跳过空行、`:` 注释行、`event:` 行；`data: [DONE]` 结束；
   单帧 `json.loads` 失败**跳过该帧**（心跳/半截帧），不整段失败。
4. **增量聚合**：`delta.content` / `delta.reasoning_content` 逐帧追加；`delta.tool_calls` 按 `index`
   归位（`id`/`type` 覆盖，`function.name`/`function.arguments` **累加**），最终按 index 升序输出。
5. **usage**：取最后一个非空 `usage` 字段；上游 `usage: {}` 视为无。
6. **空响应**：**不由侧车判定**。`content` 为空且 `tool_calls` 为空时侧车仍返回 200
   （`content` 为空串、`tool_calls: []`），由 Python 侧按 `return_reasoning` 语义决定是否抛
   `ProviderResponseError`（"提供商响应内容为空。"）——`return_reasoning=True` 只取思维链的
   场景本就不该按失败处理，这个判定属于业务语义，留在 Python。
7. **内容策略拦截**：聚合后若 `content` 命中以下任一子串（不区分大小写、先 trim+lower），
   返回 `PROVIDER_CONTENT_FILTERED`，并**丢弃 usage**：
   - `the prompt could not be submitted`
   - `prompt contains sensitive words`
   - `generative ai prohibited use policy`
8. **流内错误事件**：`event.error` 非空 → `PROVIDER_RESPONSE_ERROR`（"提供商流式响应错误：<message>"），
   message 取 `error.message`，退 `error.code`，再退整个 error 的字符串化。
9. **非 SSE 回退**：`Content-Type` 不是 `text/event-stream` 时按整段 JSON 解析
   （`choices[0].message.content` / `reasoning_content` / `tool_calls`），`path="json"`；
   2xx 但正文非 JSON（如 HTML 错误页）→ `PROVIDER_RESPONSE_ERROR`。

## 5. 多模态内联（`media` 包）

对每条形如 `{"type":"image_url","image_url":{"url": U}}` 的 part：

1. `U` 以 `http://`/`https://`/`data:` 开头 → **原样透传**。
2. `U` 是本地路径（含 `file://`，Windows 盘符形态 `file:///D:/a.png`）且文件存在 →
   读取字节 → 按**魔数**判定 mime（PNG `89504e47…`、JPEG `ffd8ff`、GIF、WEBP `RIFF....WEBP`，
   认不出时用 `image/jpeg`）→ 若 `len > 10 MiB` 且是图片 → 降采样为 JPEG（长边 1568、
   质量阶梯 85/70/55/40、仍超限则按 0.75 缩边、下限 320）→ 输出
   `data:<mime>;base64,<…>`。
3. `U` 既不是本地文件也不是网络地址 → **丢弃该 part**（原逻辑：日志 warn 后 `continue`）。
4. 计数所有发生本地路径转换的 part 到 `transport.local_image_conversions`。

## 6. 错误响应

HTTP 状态与错误码固定对应：

| HTTP | error_code | is_retryable | 触发条件 |
| --- | --- | --- | --- |
| 400 | `INVALID_REQUEST` | false | 请求体 JSON 非法、缺 `upstream.base_url/api_key`、`timeout_seconds<=0` |
| 401 | `UNAUTHORIZED` | false | sidecar 自身 token 不匹配（不是上游 401） |
| 502 | `PROVIDER_AUTH_ERROR` | false | 上游 401/403 |
| 502 | `PROVIDER_RESPONSE_ERROR` | 见下 | 上游 4xx/5xx、流内 error 事件、非 JSON 200 |
| 502 | `PROVIDER_CONTENT_FILTERED` | false | 命中内容策略标记 |
| 504 | `UPSTREAM_TIMEOUT` | true | 块间静默超时 |
| 502 | `PROVIDER_CONNECTION_ERROR` | true | 连不上上游、DNS/TLS 失败、连接中途断开 |

`PROVIDER_RESPONSE_ERROR.is_retryable` 规则（与 `exceptions._http_status_is_retryable` 一致）：
`upstream_status` 为 429 或 ≥500 → true；其余 4xx → false；无状态码（本地判定）→ true。

响应体：

```jsonc
{
  "error": {
    "error_code": "PROVIDER_RESPONSE_ERROR",
    "message": "提供商 HTTP 错误 400：{…}",
    "http_status": 502,            // 返回给 Python 的状态
    "upstream_status": 400,        // 上游状态，可 null
    "is_retryable": false,
    "content_preview": "…"        // 仅 CONTENT_FILTERED 时给
  }
}
```

**消息文案保持与 Python 现实现一致**（见 §4），以便日志与既有排障经验继续有效。

## 7. Python 侧职责（不在本契约内，但必须配合）

- `GoTransportProvider.generate_async(request, return_reasoning=False)`：满足
  `AsyncLLMProvider` 协议；成功时 `set_last_generation_usage(usage)`（`usage` 原样透传），
  失败时映射为本仓库既有异常类型（`ProviderConnectionError` / `ProviderAuthError` /
  `ProviderResponseError` / `ProviderContentFilteredError`），**不重试**（重试归 `Brain`）。
- 多模态：把 `messages` 原样交给 sidecar（**不**在 Python 侧做 data URL 转换）。
- 进程管理：`sirius_pulse/utils/llm_sidecar.py` 负责启停与健康检查；失败时可回退 Python provider。
- 统计：`token_usage` 记账仍全部留在 Python（本次不搬）。

## 8. 版本与兼容

`/health` 的 `version` 使用语义化版本；sidecar 侧不兼容变更必须同时升 minor 并更新本文件。
Python 侧启动时校验 `version` 主版本为 `0`（v1 契约期内）。

## 9. 变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| 1.1 | 2026-11-08 | 「空响应」判定从侧车移到 Python（§4.6）：`return_reasoning=True` 只取思维链是合法场景，侧车不再因 `content` 为空报错；对应错误表 §6 去掉「空响应」触发项。侧车版本号 0.1.1。 |
| 1.0 | 2026-11-08 | 初版。 |

