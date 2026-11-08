"""把 LLM 的传输层交给 Go 侧车（``go/llm-transport``）的 provider。

契约见 ``go/llm-transport/CONTRACT.md``。这一层只做四件事：

1. 把 :class:`GenerationRequest` 翻成契约 §2 的报文（``upstream`` + ``request``）。
   ``messages`` **原样**交给侧车——本地图片转 data URL、超大图降采样都在 Go 侧做，
   Python 这条路径上不再碰图片字节；
2. 把契约 §3 的成功响应还原成 :class:`GenerationResult`，并把 ``usage`` 原样交给
   ``set_last_generation_usage``（token 记账仍全部留在 Python）；
3. 把契约 §6 的错误码映射回本仓库既有的四类 ``ProviderError``；
4. 生图与图像编辑走契约 §5.1 的 ``POST /v1/generate-image``：只发任务名、prompt
   与参考图字节，端点选择、``edits`` 的「先 multipart 后 JSON」回退、浏览器 UA
   与 mime 判定全在侧车。

**不重试**：重试归 ``Brain``。传输层换了，编排与失败策略一行都没变，这也是这次
迁移的边界——Go 只做 HTTP + SSE 聚合 + 多模态内联。

与改造前相比还有两处刻意的差别：

- **长生命周期的 httpx 客户端**。原实现每个请求新建一次 client，握手与连接池都白
  扔掉；这里持有到 :meth:`aclose` 为止。
- **不在热路径上做无条件的 ``json.dumps(..., indent=2)``**。调试上下文里的
  token 估算要跑一遍 tiktoken，是本地最贵的一步，因此整块都收进
  ``logger.isEnabledFor(logging.DEBUG)`` 守卫里。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
from typing import TYPE_CHECKING, Any, NoReturn

import httpx

from sirius_pulse.exceptions import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderContentFilteredError,
    ProviderError,
    ProviderResponseError,
)
from sirius_pulse.providers.base import (
    DEFAULT_TIMEOUT_SECONDS,
    AsyncLLMProvider,
    GenerationRequest,
    GenerationResult,
    ImageGenerationRequest,
    ImageGenerationResult,
    ToolCall,
    build_generation_debug_context,
    resolve_generation_timeout_seconds,
    set_last_generation_usage,
)

if TYPE_CHECKING:
    from sirius_pulse.utils.llm_sidecar import LLMSidecar

logger = logging.getLogger(__name__)

__all__ = ["GoTransportProvider"]

#: 连不上侧车是本机进程问题，几秒内就该知道；**读超时给 ``None``**：一次长生成
#: 合法地比 ``timeout_seconds`` 久得多，真正的「没有数据太久了」由侧车的块间静默
#: 看门狗判定（契约 §4.2），Python 这层再切一刀只会误杀正常的长回复。
_CONNECT_TIMEOUT_SECONDS = 5.0

#: 契约 §6 的错误码。未知错误码一律当响应异常处理，并把 HTTP 状态带进异常。
_ERR_UNAUTHORIZED = "UNAUTHORIZED"
_ERR_PROVIDER_AUTH = "PROVIDER_AUTH_ERROR"
_ERR_PROVIDER_RESPONSE = "PROVIDER_RESPONSE_ERROR"
_ERR_PROVIDER_CONTENT_FILTERED = "PROVIDER_CONTENT_FILTERED"
_ERR_PROVIDER_CONNECTION = "PROVIDER_CONNECTION_ERROR"
_ERR_UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"


class GoTransportProvider(AsyncLLMProvider):
    """传输在 Go 侧车、编排在 Python 的 provider。

    入参出参与迁移前的 Python 传输层完全一致（同样的 ``GenerationRequest`` /
    ``GenerationResult`` / 异常类型），所以 ``Brain``、``cognition`` 这些调用方
    不需要知道自己拿到的是哪种传输层。

    对话补全（契约 §2）与生图（契约 §5.1）都在侧车里：Python 这条路径上不再有
    任何直连 AMKR 的 HTTP 代码。
    """

    _provider_name = "go-transport"

    def __init__(
        self,
        *,
        sidecar_url: str,
        sidecar_token: str,
        base_url: str,
        api_key: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        workspace: str = "",
        sidecar: LLMSidecar | None = None,
    ) -> None:
        self._sidecar_url = sidecar_url.rstrip("/")
        self._sidecar_token = sidecar_token
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._workspace = workspace.strip()
        # 侧车句柄：用来在每次生成前确认进程还活着（崩溃重启的唯一入口）。
        self._sidecar = sidecar
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._client_lock = threading.Lock()

    # ── 对外接口 ──────────────────────────────────────────────

    async def generate_async(
        self, request: GenerationRequest, return_reasoning: bool = False
    ) -> GenerationResult | tuple[str, GenerationResult]:
        """把一次生成交给侧车，返回聚合好的结果。

        When return_reasoning=True, returns (reasoning_content, GenerationResult) tuple.
        """
        timeout_seconds = resolve_generation_timeout_seconds(request, self._timeout_seconds)
        base_url, token = self._resolve_endpoint()
        url = f"{base_url}/v1/generate"
        payload = self._build_payload(request, timeout_seconds)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        logger.info(
            f"正准备通过 LLM 传输层向 {request.model} 请教问题，"
            f"手头有 {len(request.messages)} 条消息想说，"
            f"超时 {timeout_seconds:.1f} 秒～"
        )
        if logger.isEnabledFor(logging.DEBUG):
            # 这一块包含一次 tiktoken 估算，只允许在开 debug 时跑。
            debug_context = build_generation_debug_context(
                request,
                provider_name=self._provider_name,
                url=url,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
            debug_detail = json.dumps(
                {**debug_context, "request_body_bytes": len(body)}, ensure_ascii=False
            )
            logger.debug(f"[模型调用详情] {request.model} | 传输层请求:{debug_detail}")

        client = self._ensure_client()
        try:
            response = await client.post(
                url,
                content=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
                f"| {type(exc).__name__}: {exc or '(无详细信息)'}",
                exc_info=True,
            )
            raise ProviderConnectionError(
                self._provider_name,
                f"LLM 传输层请求异常：{exc}",
                original_error=exc,
            ) from exc

        raw = response.text
        if response.status_code >= 400:
            self._raise_for_sidecar_error(
                response.status_code,
                raw,
                model=request.model,
                url=url,
            )
        result = self._parse_generation_body(
            raw,
            request=request,
            status_code=response.status_code,
            url=url,
        )

        # 契约 §4.6（v1.1）：空响应**不再由侧车判定**——侧车照样回 200 + 空 content，
        # 是否算失败属于业务语义，由这里决定。`return_reasoning=True` 只取思维链是合法
        # 场景，不能因为正文为空就报错。
        if not result.content and not result.tool_calls and not return_reasoning:
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
                f"| 响应为空 | finish_reason={result.finish_reason}"
            )
            raise ProviderResponseError(
                self._provider_name,
                "提供商响应内容为空。",
                http_status=200,
            )

        content = result.content or ""
        logger.info(f"{self._provider_name} 的 {request.model} 回复我了，写了 {len(content)} 个字～")
        logger.debug(
            f"[模型输出] {request.model} | Provider: {self._provider_name} | URL: {url} "
            f"| 响应内容:\n{content}"
        )
        if return_reasoning:
            return (result.reasoning_content, result)
        return result

    async def generate_image_async(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        """把一次生图 / 图像编辑交给侧车（契约 §5.1）。

        端点选择、``edits`` 的「先 multipart 后 JSON」回退、浏览器 UA、``b64_json``
        与 url 下载的取舍、mime 判定都在侧车里；这里只发任务名、prompt 与参考图
        **字节**——参考图来自内存里的表情库与用户上传，不保证有可读的本地路径。

        超时是**整请求总超时**（契约 §5.1.2）：与迁移前一致，下限 120 秒。
        """
        timeout_seconds = request.timeout_seconds or max(float(self._timeout_seconds), 120.0)
        base_url, token = self._resolve_endpoint()
        url = f"{base_url}/v1/generate-image"
        payload = self._build_image_payload(request, timeout_seconds)

        logger.info(
            f"正在通过 LLM 传输层向 {request.model} 要一张图，"
            f"参考图 {len(request.reference_images)} 张，超时 {timeout_seconds:.1f} 秒～"
        )
        try:
            response = await self._ensure_client().post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            logger.error(
                f"[生图失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
                f"| {type(exc).__name__}: {exc or '(无详细信息)'}",
                exc_info=True,
            )
            raise ProviderConnectionError(
                self._provider_name,
                f"生图请求异常：{exc}",
                original_error=exc,
            ) from exc

        raw = response.text
        if response.status_code >= 400:
            self._raise_for_sidecar_error(
                response.status_code,
                raw,
                model=request.model,
                url=url,
                label="[生图失败]",
            )
        return self._parse_image_body(
            raw,
            model=request.model,
            status_code=response.status_code,
            url=url,
        )

    async def aclose(self) -> None:
        """关闭长生命周期的 HTTP 客户端。

        只在进程退出/整体拆除时调用；请求热路径不走这里，也不需要在每次生成后重建
        客户端（那正是这次要修掉的性能问题）。
        """
        client = self._client
        self._client = None
        self._client_loop = None
        if client is not None and not client.is_closed:
            await client.aclose()

    # ── 内部：报文与响应 ──────────────────────────────────────

    def _resolve_endpoint(self) -> tuple[str, str]:
        """返回本次该用的 ``(sidecar_base_url, token)``。

        侧车是进程级单例，崩溃后会被 ``ensure_running()`` 重启，而重启可能换端口，
        所以每次都现取地址，而不是缓存一个可能已经失效的 URL。
        """
        sidecar = self._sidecar
        if sidecar is None:
            return self._sidecar_url, self._sidecar_token
        base_url = sidecar.ensure_running()
        if not base_url:
            raise ProviderConnectionError(
                self._provider_name,
                "LLM 传输层侧车不可用（启动失败或崩溃后重启失败），" "请查看日志中的 [llm-transport] 记录。",
            )
        return base_url, sidecar.token

    def _build_payload(
        self, request: GenerationRequest, timeout_seconds: float
    ) -> dict[str, object]:
        """构造契约 §2 的报文。

        **不含采样参数**：AMKR 的任务定义是权威，多传会让它回 400。``messages``
        原样透传，多模态 part 的本地路径转换在 Go 侧完成。
        """
        return {
            "upstream": {
                "base_url": self._base_url,
                "api_key": self._api_key,
                "workspace": self._workspace,
                "timeout_seconds": _wire_timeout_seconds(timeout_seconds),
            },
            "request": {
                "model": request.model,
                "system_prompt": request.system_prompt,
                "messages": request.messages,
                "tools": request.tools,
                "tool_choice": request.tool_choice,
                "response_format": request.response_format,
                "purpose": request.purpose,
            },
        }

    def _build_image_payload(
        self, request: ImageGenerationRequest, timeout_seconds: float
    ) -> dict[str, object]:
        """构造契约 §5.1.1 的报文。

        参考图只传**字节**：它们来自内存里的表情库 / 用户上传，不保证有可读的本地
        路径（契约 §5.1.1）。``filename`` 仅供侧车兜底推断 mime。
        """
        return {
            "upstream": {
                "base_url": self._base_url,
                "api_key": self._api_key,
                "workspace": self._workspace,
                "timeout_seconds": _wire_timeout_seconds(timeout_seconds),
            },
            "request": {
                "model": request.model,
                "prompt": request.prompt,
                "reference_images": [
                    {
                        "filename": filename,
                        "data_base64": base64.b64encode(data).decode("ascii"),
                    }
                    for filename, data in request.reference_images
                ],
            },
        }

    def _parse_generation_body(
        self,
        raw: str,
        *,
        request: GenerationRequest,
        status_code: int,
        url: str,
    ) -> GenerationResult:
        """解析契约 §3 的成功响应。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
                f"| 传输层响应 JSON 解析失败: {exc} | raw: {raw[:200]}"
            )
            raise ProviderResponseError(
                self._provider_name,
                f"LLM 传输层响应无法解析为 JSON：{exc}",
                http_status=status_code,
                response_body=raw,
            ) from exc
        if not isinstance(data, dict):
            raise ProviderResponseError(
                self._provider_name,
                "LLM 传输层响应的顶层结构不是对象。",
                http_status=status_code,
                response_body=raw,
            )

        usage = data.get("usage")
        # 契约要求原样透传；上游 ``usage: {}`` 视为没有（与迁移前一致）。
        set_last_generation_usage(dict(usage) if isinstance(usage, dict) and usage else None)

        content = data.get("content")
        reasoning = data.get("reasoning_content")
        return GenerationResult(
            content=content if isinstance(content, str) else "",
            reasoning_content=reasoning if isinstance(reasoning, str) else "",
            tool_calls=self._parse_tool_calls(data.get("tool_calls")),
            finish_reason=str(data.get("finish_reason") or "stop"),
        )

    @staticmethod
    def _parse_tool_calls(raw_tool_calls: object) -> list[ToolCall] | None:
        """按契约 §3 的扁平形态（``function_name`` / ``function_arguments``）还原。"""
        if not isinstance(raw_tool_calls, list) or not raw_tool_calls:
            return None
        parsed: list[ToolCall] = []
        for raw_tool_call in raw_tool_calls:
            if not isinstance(raw_tool_call, dict):
                continue
            parsed.append(
                ToolCall(
                    id=str(raw_tool_call.get("id", "")),
                    type=str(raw_tool_call.get("type") or "function"),
                    function_name=str(raw_tool_call.get("function_name", "")),
                    function_arguments=str(raw_tool_call.get("function_arguments", "")),
                )
            )
        return parsed or None

    def _parse_image_body(
        self,
        raw: str,
        *,
        model: str,
        status_code: int,
        url: str,
    ) -> ImageGenerationResult:
        """解析契约 §5.1.3 的成功响应，把 ``image_base64`` 解回原始字节。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error(
                f"[生图失败] {model} | Provider: {self._provider_name} | URL: {url} "
                f"| 传输层响应 JSON 解析失败: {exc} | raw: {raw[:200]}"
            )
            raise ProviderResponseError(
                self._provider_name,
                f"生图接口响应无法解析为 JSON：{exc}",
                http_status=status_code,
                response_body=raw,
            ) from exc
        encoded = data.get("image_base64") if isinstance(data, dict) else None
        if not isinstance(encoded, str) or not encoded:
            logger.error(
                f"[生图失败] {model} | Provider: {self._provider_name} | URL: {url} "
                f"| 响应里没有 image_base64 | raw: {raw[:200]}"
            )
            raise ProviderResponseError(
                self._provider_name,
                "生图接口没有返回图片数据。",
                http_status=status_code,
                response_body=raw,
            )
        try:
            # 严格解码：侧车给的必须是**原始字节**的 base64，掺了杂字符说明响应已被改坏。
            image_bytes = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ProviderResponseError(
                self._provider_name,
                f"生图接口返回的 image_base64 无法解码：{exc}",
                http_status=status_code,
                response_body=raw,
            ) from exc
        if not image_bytes:
            raise ProviderResponseError(
                self._provider_name,
                "生图接口返回了空图片。",
                http_status=status_code,
                response_body=raw,
            )
        mime_type = str(data.get("mime_type") or "").strip() or "image/png"
        logger.info(f"{self._provider_name} 的画好了：{mime_type}，{len(image_bytes)} 字节")
        return ImageGenerationResult(image_bytes=image_bytes, mime_type=mime_type)

    # ── 内部：错误映射（契约 §6）──────────────────────────────

    def _raise_for_sidecar_error(
        self,
        status_code: int,
        raw: str,
        *,
        model: str,
        url: str,
        label: str = "[模型调用失败]",
    ) -> NoReturn:
        """把侧车的错误响应翻成本仓库既有的四类 provider 异常。

        文案直接用侧车给的那份：契约 §6 要求它与 Python 原实现的措辞一致，因此既有
        的排障经验（搜「提供商 HTTP 错误 400」之类）继续有效。
        """
        error = _read_error_payload(raw)
        if error is None:
            logger.error(
                f"{label} {model} | Provider: {self._provider_name} | URL: {url} "
                f"| HTTP {status_code}: {raw[:500]}"
            )
            detail = raw[:200]
            message = (
                f"LLM 传输层 HTTP 错误 {status_code}：{detail}"
                if detail
                else f"LLM 传输层 HTTP 错误 {status_code}（响应体为空）。"
            )
            if status_code == 401:
                raise ProviderAuthError(
                    self._provider_name,
                    message,
                    http_status=status_code,
                )
            raise ProviderResponseError(
                self._provider_name,
                message,
                http_status=status_code,
                response_body=raw,
            )

        error_code = str(error.get("error_code") or "").strip().upper()
        message = str(error.get("message") or "").strip() or (
            f"LLM 传输层返回错误（HTTP {status_code}），未提供错误说明。"
        )
        http_status = _error_http_status(error, status_code)
        logger.error(
            f"{label} {model} | Provider: {self._provider_name} | URL: {url} "
            f"| {error_code or '(未知错误码)'} | HTTP {http_status}: {message}"
        )

        exc: ProviderError
        if error_code in (_ERR_PROVIDER_AUTH, _ERR_UNAUTHORIZED):
            exc = ProviderAuthError(
                self._provider_name,
                message,
                http_status=http_status,
            )
        elif error_code == _ERR_PROVIDER_CONTENT_FILTERED:
            exc = ProviderContentFilteredError(
                self._provider_name,
                message,
                content_preview=str(error.get("content_preview") or ""),
            )
        elif error_code in (_ERR_UPSTREAM_TIMEOUT, _ERR_PROVIDER_CONNECTION):
            # 传输层没有独立的超时异常：块间静默超时在改造前也是一次
            # ProviderConnectionError（httpx.ReadTimeout），保持同一类，
            # Brain 的重试与兜底策略不需要为它写新分支。
            exc = ProviderConnectionError(self._provider_name, message)
        else:
            exc = ProviderResponseError(
                self._provider_name,
                message,
                http_status=http_status,
                response_body=raw,
            )

        _apply_retry_hint(exc, error.get("is_retryable"))
        raise exc

    # ── 内部：HTTP 客户端 ─────────────────────────────────────

    def _ensure_client(self) -> httpx.AsyncClient:
        """取长生命周期的客户端；只有首次使用或换了事件循环才新建。

        ``trust_env=False`` 是刻意的：侧车**永远**监听 ``127.0.0.1``，而 httpx 默认
        会读 ``HTTP_PROXY``/``ALL_PROXY`` 之类的环境变量。机器上一旦配了代理，回环
        请求也会被送进代理，得到的要么是代理的 502、要么是莫名其妙的超时——那会表现
        成「侧车明明活着却调不通」。回环地址不该经过任何代理。
        """
        loop = asyncio.get_running_loop()
        client = self._client
        if client is not None and not client.is_closed and self._client_loop is loop:
            return client
        with self._client_lock:
            client = self._client
            if client is not None and not client.is_closed and self._client_loop is loop:
                return client
            # 旧客户端属于另一个（通常已关闭的）事件循环，不能在这里 await 关闭，
            # 直接丢弃即可——它的连接随 loop 一起消失。
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(None, connect=_CONNECT_TIMEOUT_SECONDS),
                trust_env=False,
            )
            self._client_loop = loop
            return self._client


def _wire_timeout_seconds(value: float) -> int | float:
    """契约写的是整数秒；非整秒时如实发小数，别把 30.5 悄悄改成 30。"""
    number = float(value)
    return int(number) if number.is_integer() else number


def _read_error_payload(raw: str) -> dict[str, Any] | None:
    """取 ``{"error": {...}}`` 里的 ``error`` 对象；结构不对时返回 ``None``。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    return error if isinstance(error, dict) else None


def _error_http_status(error: dict[str, Any], fallback: int) -> int | None:
    """异常该带的 HTTP 状态：优先**上游**状态码，退化到侧车返回的状态码。

    ``ProviderResponseError.is_retryable`` 由这个值算出，而契约 §6 的重试规则正是
    按上游状态码定的（429/5xx 可重试，其余 4xx 不可，无上游状态码按可重试）。
    取侧车的 502 会让一个上游 400 变成「可重试」，那是改造前没有的行为。
    """
    for key in ("upstream_status", "http_status"):
        value = error.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return fallback


def _apply_retry_hint(exc: ProviderError, hint: object) -> None:
    """尊重侧车给出的 ``is_retryable``：它看得到上游状态码，我们只看到 502。"""
    if isinstance(hint, bool):
        exc.is_retryable = hint
