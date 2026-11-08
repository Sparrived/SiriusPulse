"""可编程的假 AMKR：给 LLM 传输层（Go 侧车）端到端用例当上游。

只实现一个端点——``POST /v1/chat/completions``，也就是契约里侧车唯一会打的地址
（``go/llm-transport/CONTRACT.md`` §4）。场景由 :meth:`FakeAmkr.set_scenario`
在用例内切换，因此一个假上游就能覆盖「多帧正文 / 思维链 / 分块 tool_calls /
末尾 usage 帧 / 流内 error 事件 / 坏帧 / 非 SSE 回退 / HTML 200 / 401 / 429 /
500 / 块间静默超时」这些分支。

**为什么用线程内的 ``http.server`` 而不是 aiohttp**：被测对象是一个独立进程，
它只认 ``127.0.0.1:<port>`` 上的真实 TCP 连接，不需要调用方的事件循环配合；
线程实现还能在场景里直接 ``time.sleep``，把「块间静默」做成真实的上游行为，
而不是把超时 mock 掉。SSE 用真正的 chunked 传输（每帧一个 chunk），这样侧车的
看门狗看到的就是逐块到达的数据。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

__all__ = ["FakeAmkr", "RecordedRequest", "SSE_CONTENT_TYPE"]

SSE_CONTENT_TYPE = "text/event-stream"
JSON_CONTENT_TYPE = "application/json"

#: 上游把「内容策略拦截」的说明文字当正文返回时的识别标记（契约 §4.7）。
CONTENT_POLICY_TEXT = (
    "Sorry, the prompt could not be submitted because it contains sensitive words."
)


@dataclass(slots=True)
class RecordedRequest:
    """假 AMKR 收到的一次请求，用于断言侧车真正发出去的内容。"""

    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def json_body(self) -> dict[str, Any] | None:
        """请求体解析成 dict；不是合法对象时返回 ``None``。"""
        try:
            parsed = json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None


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


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeAmkr/1.0"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        amkr: FakeAmkr = self.server.amkr  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        recorded = RecordedRequest(
            path=self.path,
            headers={key.lower(): value for key, value in self.headers.items()},
            body=body,
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

    def __init__(self, scenario: str = "text", **options: object) -> None:
        self._scenario = scenario
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

    # ── 内部 ──────────────────────────────────────────
    def record(self, request: RecordedRequest) -> None:
        with self._lock:
            self._requests.append(request)

    def dispatch(self, request: RecordedRequest, responder: Responder) -> None:
        if request.path != "/v1/chat/completions":
            responder.send_json(404, {"error": {"message": f"未知路径 {request.path}"}})
            return
        _SCENARIOS[self._scenario](self, responder, request)
