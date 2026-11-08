"""可编程的假 AMKR：给 LLM 传输层（Go 侧车）端到端用例当上游。

实现侧车真正会打的两组地址：

- **对话补全** ``POST /v1/chat/completions``（契约 §4）。场景由
  :meth:`FakeAmkr.set_scenario` 在用例内切换，因此一个假上游就能覆盖「多帧正文 /
  思维链 / 分块 tool_calls / 末尾 usage 帧 / 流内 error 事件 / 坏帧 / 非 SSE 回退 /
  HTML 200 / 401 / 429 / 500 / 块间静默超时」这些分支。
- **生图与图像编辑** ``POST /v1/images/generations``（无参考图）与
  ``POST /v1/images/edits``（有参考图），以及生成图片的 ``GET`` 下载地址
  （契约 §5.1）。场景由 :meth:`FakeAmkr.set_image_scenario` 切换，覆盖
  「b64_json / url 下载 / 下载 404 / 下载空内容 / 非法 JSON / 空 data /
  b64 解码失败 / 空 b64 / edits 4xx 回退 JSON / 401/403/408/429 不回退」。

两条链路各有独立的场景开关，互不干扰：生图用例不会因为聊天的场景名而改变行为。

**为什么用线程内的 ``http.server`` 而不是 aiohttp**：被测对象是一个独立进程，
它只认 ``127.0.0.1:<port>`` 上的真实 TCP 连接，不需要调用方的事件循环配合；
线程实现还能在场景里直接 ``time.sleep``，把「块间静默」做成真实的上游行为，
而不是把超时 mock 掉。SSE 用真正的 chunked 传输（每帧一个 chunk），这样侧车的
看门狗看到的就是逐块到达的数据。
"""

from __future__ import annotations

import base64
import email
import json
import sys
import threading
import time
from dataclasses import dataclass
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

__all__ = [
    "BROWSER_USER_AGENT",
    "FakeAmkr",
    "IMAGE_SCENARIOS",
    "JPEG_1PX",
    "PNG_1PX",
    "RecordedRequest",
    "SSE_CONTENT_TYPE",
]

SSE_CONTENT_TYPE = "text/event-stream"
JSON_CONTENT_TYPE = "application/json"

#: 契约 §5.1.2/§5.1.4：所有图像请求（含下载）必须带浏览器 UA，上游套了 Cloudflare
#: 校验，默认 UA 会被 403（error code 1010）。断言写成常量的同一份，任一环节漏带
#: 都会在用例里直接对不上。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

#: 上游把「内容策略拦截」的说明文字当正文返回时的识别标记（契约 §4.7）。
CONTENT_POLICY_TEXT = (
    "Sorry, the prompt could not be submitted because it contains sensitive words."
)

#: 真 PNG 字节（1×1 透明）：侧车按魔数判 mime，所以不能用只造前缀的假字节。
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)

#: 真 JPEG 字节（1×1 白色）：用来验证「按字节判 mime 而不是按后缀猜」。
JPEG_1PX = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)

#: 生成图片的下载路径前缀：侧车拿到 ``url`` 后会自己再 GET 一次。
_DOWNLOAD_PREFIX = "/files/"


@dataclass(slots=True)
class RecordedRequest:
    """假 AMKR 收到的一次请求，用于断言侧车真正发出去的内容。"""

    path: str
    headers: dict[str, str]
    body: bytes
    method: str = "POST"

    @property
    def content_type(self) -> str:
        """``Content-Type`` 原值（multipart 用例要断言它的形态）。"""
        return self.headers.get("content-type", "")

    @property
    def user_agent(self) -> str:
        """``User-Agent`` 原值（契约 §5.1.2 第 4 条）。"""
        return self.headers.get("user-agent", "")

    @property
    def is_multipart(self) -> bool:
        """是否是 multipart/form-data 请求（``edits`` 的「先标准」形态）。"""
        return self.content_type.startswith("multipart/form-data;")

    @property
    def json_body(self) -> dict[str, Any] | None:
        """请求体解析成 dict；不是合法对象时返回 ``None``。"""
        try:
            parsed = json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def multipart_fields(self) -> dict[str, list[tuple[bytes, str]]]:
        """把 multipart 请求体拆成 ``{字段名: [(原始字节, 部件声明的 Content-Type)]}``。

        用标准库的 mail 解析器而不是手写分隔符切分：侧车用的 multipart writer 会把
        字段名、文件名与 Content-Type 都放在部件头里，交给解析器才不会漏掉转义。

        ``image`` 是**重复**的文件字段（契约 §5.1.2 第 3 条明确不许用 ``image[]``），
        所以值必须是列表，保留到达顺序供用例逐张比对。
        """
        if not self.is_multipart:
            raise AssertionError(f"不是 multipart 请求：{self.content_type!r}")
        raw = (
            b"Content-Type: "
            + self.content_type.encode("latin-1")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + self.body
        )
        message = email.message_from_bytes(raw)
        parts = message.get_payload()
        if not isinstance(parts, list):
            raise AssertionError("请求体不是 multipart/form-data")
        fields: dict[str, list[tuple[bytes, str]]] = {}
        for part in parts:
            if not isinstance(part, Message):
                continue
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True) or b""
            if not isinstance(name, str):
                continue
            fields.setdefault(name, []).append((payload, part.get_content_type()))
        return fields


class Responder:
    """场景脚本用来写响应的最小工具。"""

    def __init__(self, handler: BaseHTTPRequestHandler) -> None:
        self._handler = handler
        self._closed = False

    # ── 普通（非 SSE）响应 ──────────────────────────────
    def send(self, status: int, body: bytes, *, content_type: str = JSON_CONTENT_TYPE) -> None:
        handler = self._handler
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        self._write(body)

    def send_json(self, status: int, payload: object) -> None:
        self.send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    # ── SSE（chunked，逐帧到达）──────────────────────────
    def sse_start(self) -> None:
        handler = self._handler
        handler.send_response(200)
        handler.send_header("Content-Type", SSE_CONTENT_TYPE)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()

    def sse_frame(self, payload: object) -> None:
        """写一个 ``data: <json>`` 帧。"""
        self.sse_raw("data: " + json.dumps(payload, ensure_ascii=False))

    def sse_raw(self, text: str) -> None:
        """写一段原始 SSE 文本（末尾自动补空行）。"""
        self._write_chunk((text + "\n\n").encode("utf-8"))

    def sse_done(self) -> None:
        self.sse_raw("data: [DONE]")

    def finish(self) -> None:
        """写 chunked 的终止块，正常结束响应。"""
        if self._closed:
            return
        self._closed = True
        self._write(b"0\r\n\r\n")

    def sleep(self, seconds: float) -> None:
        """故意沉默一段时间，让侧车的块间静默看门狗生效。"""
        time.sleep(seconds)

    # ── 内部 ──────────────────────────────────────────
    def _write_chunk(self, data: bytes) -> None:
        if self._closed:
            return
        self._write(b"%x\r\n" % len(data) + data + b"\r\n")

    def _write(self, data: bytes) -> None:
        try:
            self._handler.wfile.write(data)
            self._handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # 侧车取消请求（超时用例）时对端已关闭，这里静默收尾即可。
            self._closed = True


# ── SSE 帧构造 ────────────────────────────────────────────
def _delta(**fields: object) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": dict(fields)}]}


def _finish(reason: str = "stop") -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def _usage(prompt: int = 1234, completion: int = 56) -> dict[str, Any]:
    return {
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }
    }


# ── 场景 ──────────────────────────────────────────────────
def _scenario_text(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
    responder.sse_start()
    responder.sse_frame(_delta(role="assistant", content=""))
    responder.sse_frame(_delta(content="你好"))
    responder.sse_frame(_delta(content="，世界"))
    responder.sse_frame(_finish("stop"))
    responder.sse_done()
    responder.finish()


def _scenario_reasoning(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
    responder.sse_start()
    responder.sse_frame(_delta(reasoning_content="先想"))
    responder.sse_frame(_delta(reasoning_content="一下"))
    responder.sse_frame(_delta(content="答案"))
    responder.sse_frame(_finish("stop"))
    responder.sse_done()
    responder.finish()


def _scenario_reasoning_only(
    amkr: "FakeAmkr", responder: Responder, request: RecordedRequest
) -> None:
    """只有思维链、没有正文：契约 v1.1 下侧车仍回 200，判定留给 Python。"""
    responder.sse_start()
    responder.sse_frame(_delta(reasoning_content="只有想法"))
    responder.sse_frame(_delta(reasoning_content="没有正文"))
    responder.sse_frame(_finish("stop"))
    responder.sse_done()
    responder.finish()


def _scenario_tool_calls(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
    responder.sse_start()
    responder.sse_frame(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"q":'},
                            }
                        ]
                    },
                }
            ]
        }
    )
    responder.sse_frame(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"天气"}'}}]},
                }
            ]
        }
    )
    responder.sse_frame(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_b",
                                "type": "function",
                                "function": {"name": "send", "arguments": "{}"},
                            }
                        ]
                    },
                }
            ]
        }
    )
    responder.sse_frame(_finish("tool_calls"))
    responder.sse_done()
    responder.finish()


def _scenario_usage(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
    responder.sse_start()
    responder.sse_frame(_delta(content="带用量"))
    responder.sse_frame(_usage())
    responder.sse_frame(_finish("stop"))
    responder.sse_done()
    responder.finish()


def _scenario_no_usage(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
    responder.sse_start()
    responder.sse_frame(_delta(content="没有用量"))
    responder.sse_frame({"usage": {}})
    responder.sse_frame(_finish("stop"))
    responder.sse_done()
    responder.finish()


def _scenario_bad_frames(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
    """注释行 / event: 行 / 半截 JSON / 纯文本坏帧都必须被跳过。"""
    responder.sse_start()
    responder.sse_raw(": 心跳注释")
    responder.sse_raw("event: ping")
    responder.sse_frame(_delta(content="前半"))
    responder.sse_raw('data: {"choices": [{"index": 0, "delta": {"cont')
    responder.sse_raw("data: 这不是 JSON")
    responder.sse_raw("")
    responder.sse_frame(_delta(content="后半"))
    responder.sse_frame(_finish("stop"))
    responder.sse_done()
    responder.finish()


def _scenario_truncated(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
    """没有 [DONE] 就 EOF：已聚合的内容不该丢。"""
    responder.sse_start()
    responder.sse_frame(_delta(content="说到一半"))
    responder.finish()


def _scenario_content_filtered(
    amkr: "FakeAmkr", responder: Responder, request: RecordedRequest
) -> None:
    """命中内容策略标记，且带一份 usage —— usage 必须被丢弃。"""
    responder.sse_start()
    responder.sse_frame(_delta(content=CONTENT_POLICY_TEXT))
    responder.sse_frame(_usage(prompt=0, completion=0))
    responder.sse_frame(_finish("stop"))
    responder.sse_done()
    responder.finish()


def _scenario_error_event(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
    responder.sse_start()
    responder.sse_frame(_delta(content="残片"))
    responder.sse_frame({"error": {"message": "上游模型中断", "code": "upstream_error"}})
    responder.finish()


def _scenario_json(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
    """网关忽略 stream 时的一整段 JSON 回退。"""
    responder.send_json(
        200,
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "整段 JSON 回复",
                        "reasoning_content": "先想一下",
                        "tool_calls": [
                            {
                                "id": "call_json",
                                "type": "function",
                                "function": {"name": "search", "arguments": '{"k":1}'},
                            }
                        ],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        },
    )


def _scenario_html(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
    """2xx 但正文是 HTML 错误页（反代典型行为）。"""
    responder.send(
        200,
        b"<html><body>502 Bad Gateway from reverse proxy</body></html>",
        content_type="text/html",
    )


def _status_scenario(status: int) -> Callable[[Any, Responder, RecordedRequest], None]:
    def _scenario(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
        responder.send_json(status, {"error": {"message": f"上游返回 {status}"}})

    return _scenario


def _scenario_silence(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
    """块间静默超时：先给一块，然后长时间不再给数据。"""
    responder.sse_start()
    responder.sse_frame(_delta(content="开了个头"))
    responder.sleep(float(amkr.options.get("silence_seconds", 2.0)))
    responder.sse_frame(_delta(content="…终于接上"))
    responder.sse_done()
    responder.finish()


_SCENARIOS: dict[str, Callable[[Any, Responder, RecordedRequest], None]] = {
    "text": _scenario_text,
    "reasoning": _scenario_reasoning,
    "reasoning_only": _scenario_reasoning_only,
    "tool_calls": _scenario_tool_calls,
    "usage": _scenario_usage,
    "no_usage": _scenario_no_usage,
    "bad_frames": _scenario_bad_frames,
    "truncated": _scenario_truncated,
    "content_filtered": _scenario_content_filtered,
    "error_event": _scenario_error_event,
    "json": _scenario_json,
    "html": _scenario_html,
    "status_400": _status_scenario(400),
    "status_401": _status_scenario(401),
    "status_429": _status_scenario(429),
    "status_500": _status_scenario(500),
    "silence": _scenario_silence,
}


# ── 图像场景（契约 §5.1：``/v1/images/generations`` 与 ``/v1/images/edits``）──
def _image_result_bytes(amkr: "FakeAmkr") -> bytes:
    """本次「上游画出来的图」：默认真 PNG，用例可换（如 JPEG 验 mime 判定）。"""
    configured = amkr.options.get("image_bytes")
    return configured if isinstance(configured, bytes) else PNG_1PX


def _image_mime_of(data: bytes) -> str:
    """按魔数给下载响应一个诚实的 ``Content-Type``。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return "application/octet-stream"


def _image_b64_payload(amkr: "FakeAmkr") -> dict[str, Any]:
    """契约 §5.1.2 第 6 条的首选形态：``data[0].b64_json``。"""
    return {"data": [{"b64_json": base64.b64encode(_image_result_bytes(amkr)).decode("ascii")}]}


def _image_url_payload(amkr: "FakeAmkr") -> dict[str, Any]:
    """次选形态：只给 ``url``，由侧车自己再 GET 一次。"""
    filename = str(amkr.options.get("image_filename") or "result.png")
    return {"data": [{"url": f"{amkr.base_url}{_DOWNLOAD_PREFIX}{filename}"}]}


def _image_b64(amkr: Any, responder: Responder, request: RecordedRequest) -> None:
    responder.send_json(200, _image_b64_payload(amkr))


def _image_url(amkr: Any, responder: Responder, request: RecordedRequest) -> None:
    responder.send_json(200, _image_url_payload(amkr))


def _image_download_failing(amkr: Any, responder: Responder, request: RecordedRequest) -> None:
    """上游给了链接，但那张图拿不到：下载失败必须报错，不能当成空图。"""
    responder.send_json(200, _image_url_payload(amkr))


def _image_invalid_json(amkr: Any, responder: Responder, request: RecordedRequest) -> None:
    """2xx 但正文不是 JSON（反代错误页是典型来源）。"""
    responder.send(
        200,
        b"<html><body>502 Bad Gateway from reverse proxy</body></html>",
        content_type="text/html",
    )


def _image_empty_data(amkr: Any, responder: Responder, request: RecordedRequest) -> None:
    responder.send_json(200, {"data": []})


def _image_non_object_item(amkr: Any, responder: Responder, request: RecordedRequest) -> None:
    """``data[0]`` 不是对象：与空 data 同类，属于结构异常。"""
    responder.send_json(200, {"data": ["oops"]})


def _image_bad_b64(amkr: Any, responder: Responder, request: RecordedRequest) -> None:
    """``b64_json`` 掺了非法字符：严格解码必须失败，而不是丢掉坏字符硬解。"""
    responder.send_json(200, {"data": [{"b64_json": "@@这不是 base64@@"}]})


def _image_empty_b64(amkr: Any, responder: Responder, request: RecordedRequest) -> None:
    """``b64_json`` 是空串：与「没给 b64_json」同义——迁移前也是先判空串再取 url。"""
    responder.send_json(200, {"data": [{"b64_json": ""}]})


def _image_edits_json_fallback(amkr: Any, responder: Responder, request: RecordedRequest) -> None:
    """先拒 multipart、再认 JSON：有些网关只认 JSON 形态的 ``images[].image_url``。"""
    if request.is_multipart:
        responder.send_json(400, {"error": {"message": "images endpoint requires an image model"}})
        return
    responder.send_json(200, _image_b64_payload(amkr))


def _image_edits_all_rejected(amkr: Any, responder: Responder, request: RecordedRequest) -> None:
    """两种形态都被拒：报错必须来自最后一次尝试。"""
    responder.send_json(422, {"detail": [{"msg": "prompt missing"}]})


def _image_status_scenario(status: int) -> Callable[[Any, Responder, RecordedRequest], None]:
    def _scenario(amkr: Any, responder: Responder, request: RecordedRequest) -> None:
        responder.send_json(status, {"error": {"message": f"上游返回 {status}"}})

    return _scenario


#: 图像链路的场景表。默认 ``b64``：一条成功路径就能让生图 E2E 跑起来。
IMAGE_SCENARIOS: dict[str, Callable[[Any, Responder, RecordedRequest], None]] = {
    "b64": _image_b64,
    "url": _image_url,
    "download_404": _image_download_failing,
    "download_empty": _image_download_failing,
    "invalid_json": _image_invalid_json,
    "empty_data": _image_empty_data,
    "non_object_item": _image_non_object_item,
    "bad_b64": _image_bad_b64,
    "empty_b64": _image_empty_b64,
    "edits_json_fallback": _image_edits_json_fallback,
    "edits_all_rejected": _image_edits_all_rejected,
    "status_401": _image_status_scenario(401),
    "status_403": _image_status_scenario(403),
    "status_408": _image_status_scenario(408),
    "status_429": _image_status_scenario(429),
}

#: 生图请求真正会打的两个上游路径（契约 §5.1.2 第 2 条）。
_IMAGE_PATHS = ("/v1/images/generations", "/v1/images/edits")


def _serve_image_download(amkr: "FakeAmkr", responder: Responder, request: RecordedRequest) -> None:
    """``GET {url}``：把「生成出来的图」按场景发回去，或按场景失败。"""
    if amkr.image_scenario == "download_404":
        responder.send_json(404, {"error": {"message": "图片不存在"}})
        return
    if amkr.image_scenario == "download_empty":
        responder.send(200, b"", content_type="image/png")
        return
    data = _image_result_bytes(amkr)
    responder.send(200, data, content_type=_image_mime_of(data))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeAmkr/1.0"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        self._handle("POST")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        self._handle("GET")

    def _handle(self, method: str) -> None:
        amkr: FakeAmkr = self.server.amkr  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        recorded = RecordedRequest(
            path=self.path,
            headers={key.lower(): value for key, value in self.headers.items()},
            body=body,
            method=method,
        )
        amkr.record(recorded)
        responder = Responder(self)
        try:
            amkr.dispatch(recorded, responder)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """静默：假上游的访问日志只会污染 pytest 输出。"""


class _Server(ThreadingHTTPServer):
    """假 AMKR 的 HTTP 服务。

    ``handle_error`` 必须吞掉连接类异常：侧车是 keep-alive 客户端，进程退出（或
    主动取消请求）时那批空闲连接会以 ``ConnectionResetError`` 收尾，而
    ``socketserver`` 默认会把完整 traceback 打到 stderr——那会把 pytest 的输出
    淹掉，看起来像是测试失败了。
    """

    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        if isinstance(
            error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)
        ):
            return
        super().handle_error(request, client_address)  # type: ignore[arg-type]


class FakeAmkr:
    """线程内的假 AMKR。用 ``with FakeAmkr() as amkr:`` 或 ``start()/stop()``。"""

    def __init__(
        self, scenario: str = "text", image_scenario: str = "b64", **options: object
    ) -> None:
        self._scenario = scenario
        self._image_scenario = image_scenario
        self.options: dict[str, object] = dict(options)
        self._requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.amkr = self  # type: ignore[attr-defined]
        # poll_interval 默认 0.5s：每次 shutdown() 都要白等它一个周期，用例一多
        # 就是十几秒。20ms 足够响应，也不烧 CPU。
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="fake-amkr",
            daemon=True,
        )

    # ── 生命周期 ──────────────────────────────────────
    def start(self) -> "FakeAmkr":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "FakeAmkr":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ── 地址与用例控制 ────────────────────────────────
    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def set_scenario(self, name: str, **options: object) -> None:
        """切换场景；``options`` 会并入 :attr:`options`（如 ``silence_seconds``）。"""
        if name not in _SCENARIOS:
            raise KeyError(f"未知场景: {name}")
        self._scenario = name
        self.options.update(options)

    def set_image_scenario(self, name: str, **options: object) -> None:
        """切换生图链路的场景（与聊天场景互不影响）。"""
        if name not in IMAGE_SCENARIOS:
            raise KeyError(f"未知生图场景: {name}")
        self._image_scenario = name
        self.options.update(options)

    @property
    def image_scenario(self) -> str:
        return self._image_scenario

    @property
    def requests(self) -> list[RecordedRequest]:
        with self._lock:
            return list(self._requests)

    @property
    def last_request(self) -> RecordedRequest:
        requests = self.requests
        if not requests:
            raise AssertionError("假 AMKR 没有收到任何请求")
        return requests[-1]

    @property
    def image_requests(self) -> list[RecordedRequest]:
        """打向 ``/v1/images/*`` 的请求（生图与图像编辑）。"""
        return [item for item in self.requests if item.path in _IMAGE_PATHS]

    @property
    def download_requests(self) -> list[RecordedRequest]:
        """侧车为取回生成图片而发的 ``GET``。"""
        return [item for item in self.requests if item.method == "GET"]

    # ── 内部 ──────────────────────────────────────────
    def record(self, request: RecordedRequest) -> None:
        with self._lock:
            self._requests.append(request)

    def dispatch(self, request: RecordedRequest, responder: Responder) -> None:
        if request.path == "/v1/chat/completions":
            _SCENARIOS[self._scenario](self, responder, request)
            return
        if request.path in _IMAGE_PATHS:
            IMAGE_SCENARIOS[self._image_scenario](self, responder, request)
            return
        if request.method == "GET" and request.path.startswith(_DOWNLOAD_PREFIX):
            _serve_image_download(self, responder, request)
            return
        responder.send_json(404, {"error": {"message": f"未知路径 {request.path}"}})
