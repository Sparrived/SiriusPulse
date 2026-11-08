# LLM Transport Sidecar 契约（v1.2）

> 本文件是 Go 侧车与 Python 侧**唯一的接口约定**。两侧实现都必须以本文件为准；
> 任何字段改名、语义变化，先改本文件再改代码。
>
> 目标：把「对 AMKR 的 HTTP 调用 + SSE 聚合 + 多模态内联 + 图像生成」交给 Go，
> **Python 保留全部编排**（prompt 组装、工具循环、重试、兜底任务、token 记账）。
>
> **v1.2 起侧车是必需的**：Python 侧不再有第二套传输实现，找不到二进制即不就绪。

## 1. 进程与网络

| 项 | 约定 |
| --- | --- |
| 监听地址 | 固定 `127.0.0.1`（不加 `--host` 时）；**不得**监听 `0.0.0.0` |
| 端口 | 由 Python 传入 `--port <n>`（Python 先绑 0 取空闲端口再释放）；`--port 0` 时打印实际端口 |
| 就绪信号 | 监听成功后向 stdout 打印**单行** `READY <port>` 并 flush，然后进入服务 |
| 鉴权 | 启动参数 `--token <t>`（或环境变量 `LLM_TRANSPORT_TOKEN`）。所有 `/v1/*` 请求必须带 `Authorization: Bearer <t>`，否则 401 |
| 健康检查 | `GET /health` 免鉴权，返回 `{"status":"ok","version":"0.2.0","pid":1234}` |
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

## 4. 语义对齐（v1.2 起以本文件为唯一权威）

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

## 5.1 图像生成与图像编辑：`POST /v1/generate-image`

v1.2 新增。聊天补全之外，**生图能力也迁到侧车**；编排（任务名、工作空间、超时预算、
失败语义）仍在 Python。

### 5.1.1 请求

```jsonc
{
  "upstream": {
    "base_url": "http://127.0.0.1:8000",
    "api_key": "amkr_ik_…",
    "workspace": "sirius-pulse/yuebai",
    "timeout_seconds": 120          // 注意：这里是**整请求总超时**，不是 §4.2 的块间静默超时
  },
  "request": {
    "model": "person_image",        // AMKR 任务名，原样透传
    "prompt": "……",
    "reference_images": [           // 可缺省/为空：为空走 generations
      {"filename": "sticker.png", "data_base64": "iVBORw0KGgo…"}
    ]
  }
}
```

`reference_images[].data_base64` 是参考图**原始字节**的 base64：参考图来自内存里的表情库 /
用户上传，不保证有可读的本地路径，因此不能用路径传递。`filename` 只用于兜底推断 mime。

### 5.1.2 语义（必须与迁移前的 `openai_compatible.py::generate_image_async` 一致）

1. **端点是总超时**：`timeout_seconds` 直接作为整次（含 url 下载）的上限，超时 →
   `UPSTREAM_TIMEOUT`。**不要**套用 §4.2 的块间静默看门狗。
2. **端点选择**：`reference_images` 为空 → `{base_url}/v1/images/generations`，JSON
   `{"model","prompt"}`；非空 → `{base_url}/v1/images/edits`。
3. **edits 的「先标准、后回退」**：先发 OpenAI 标准 multipart 表单——`model`、`prompt`
   文本字段 + **重复的 `image` 文件字段**（每个含 filename 与按魔数判定的 Content-Type；
   不要用 `image[]`，本地上游只认 `image`）。若上游返回 `4xx` 且状态码**不在**
   `{401,403,408,429}`（这四种分别是凭据与超时/限流，重发无意义），则回退为 JSON：
   `{"model","prompt","images":[{"image_url":"data:<mime>;base64,…"}]}`。
4. **User-Agent**：所有图像请求（edits、generations、以及下载生成图片的 GET）都必须带
   浏览器 UA——上游套了 Cloudflare 校验，默认 UA 会被 403（error code 1010）：
   `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36`
   **聊天链路不要带这个 UA**（§4 的路由不经过该层校验）。
5. **鉴权头**与 §2 相同：`Authorization: Bearer <api_key>`，`workspace` 非空时加
   `X-AMKR-Workspace`。
6. **响应取值**：`data[0]` 优先 `b64_json`（严格 base64 解码；解出空字节算失败），
   否则取 `url` 自行 GET 下载（带第 4 条的 UA）。
7. **mime**：按**魔数**判定（PNG/JPEG/GIF/WEBP）；认不出时按 filename/url 的后缀猜；
   仍认不出兜底 `image/png`。
8. 空/异常结构一律报 `PROVIDER_RESPONSE_ERROR`，文案见 §5.1.4。

### 5.1.3 成功响应

```jsonc
{
  "image_base64": "iVBORw0KGgo…",        // 图片原始字节的 base64
  "mime_type": "image/png",
  "transport": {
    "endpoint": "generations",            // "generations" | "edits" | "edits-json"
    "downloaded": false,                  // 是否走了 url 下载
    "upstream_ms": 4210.5
  }
}
```

### 5.1.4 错误文案（与迁移前一致，便于沿用既有排障经验）

| 情况 | message |
| --- | --- |
| 上游状态码 ≥400 | `生图接口 HTTP 错误 {code}：{raw[:200]}`（401/403 → `PROVIDER_AUTH_ERROR`，其余 `PROVIDER_RESPONSE_ERROR`） |
| 响应不是合法 JSON | `生图接口返回的不是合法 JSON：{raw[:200]}` |
| 响应顶层不是对象 | `生图接口返回结构异常：{raw[:200]}` |
| `data` 空 / `data[0]` 非对象 | `生图接口没有返回任何图片数据。` |
| `b64_json` 解码失败 | `生图接口返回的 b64_json 无法解码：{err}` |
| `b64_json` 解出空字节 | `生图接口返回了空图片。`（防御性守卫：`b64_json: ""` 等同「没给 b64_json」，会落到下一行，故线上不可达；保留与否实现自定，保留是为了压住未来回归） |
| 既无 `b64_json` 也无 `url` | `生图接口既没有 b64_json 也没有 url。` |
| 下载返回 ≥400 | `下载生成图片失败，HTTP {code}：{url}` |
| 下载得到空内容 | `下载生成图片得到空内容：{url}` |
| `reference_images[i].data_base64` 不是合法 base64 | `reference_images[i].data_base64 不是合法 base64：{err}`，返回 **400 `INVALID_REQUEST`**（§6 的 400 触发项之一，属入参校验失败，不是上游失败） |

> 实现补充（不改变语义，仅供排障对照）：`b64_json` 解码比 Go 默认更严——额外拒绝含 `\r\n`
> 的内联 base64（Python 的 `validate=True` 同样不接受换行）；mime 后缀兜底只认
> `png/jpg/jpeg/gif/webp/bmp/tiff/avif/svg`，且先剥掉 url 的 `?query#frag` 再判后缀。

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
- `GoTransportProvider.generate_image_async(request)`：调用 §5.1，把
  `image_base64` 解回原始字节构造 `ImageGenerationResult`；异常映射同上。
- 多模态：把 `messages` 原样交给 sidecar（**不**在 Python 侧做 data URL 转换）。
- 进程管理：`sirius_pulse/utils/llm_sidecar.py` 负责启停与健康检查。
  **v1.2 起没有回退**：侧车不可用时 `_build_provider` 返回 `None`（不就绪），
  Python 侧不允许再自行发 HTTP 到 AMKR。
- 统计：`token_usage` 记账仍全部留在 Python（生图不计 token）。

## 8. 版本与兼容

`/health` 的 `version` 使用语义化版本；sidecar 侧不兼容变更必须同时升 minor 并更新本文件。
Python 侧启动时校验 `version` 主版本为 `0`（v1 契约期内）。

## 9. 变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| 1.2 | 2026-11-09 | 新增 §5.1 `POST /v1/generate-image`（图像生成 / 图像编辑），生图能力从 Python 迁到侧车；侧车版本号 **0.2.0**。**Python 侧不再保留任何对话补全或生图的传输实现**——侧车由可选变为必需（§7 同步更新）。 |
| 1.1 | 2026-11-08 | 「空响应」判定从侧车移到 Python（§4.6）：`return_reasoning=True` 只取思维链是合法场景，侧车不再因 `content` 为空报错；对应错误表 §6 去掉「空响应」触发项。侧车版本号 0.1.1。 |
| 1.0 | 2026-11-08 | 初版。 |

