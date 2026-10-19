from __future__ import annotations

import base64
import binascii
import json
import logging
import mimetypes
from typing import Any, NoReturn, cast

import httpx

from sirius_pulse.exceptions import (
    ProviderAuthError,
    ProviderConnectionError,
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
    build_chat_completion_payload,
    build_generation_debug_context,
    prepare_openai_compatible_messages,
    resolve_generation_timeout_seconds,
    set_last_generation_usage,
)
from sirius_pulse.providers.response_utils import extract_assistant_text
from sirius_pulse.utils.image_bytes import sniff_image_mime

logger = logging.getLogger(__name__)

#: 流式请求要显式索取 usage。OpenAI 兼容接口默认不在流里带用量，没有它就只能
#: 退回字符估算；AMKR 只在「非 native」转换路径自己补 stream_options，native
#: 透传路径按原样转发，所以调用方必须带上。
_STREAM_OPTIONS: dict[str, bool] = {"include_usage": True}

#: 图像端点的上游（sub2api）套了一层 Cloudflare 校验，会按 User-Agent 判断请求像不像
#: 浏览器：默认的 httpx / python-urllib UA 直接被拦成 403（error code 1010）。AMKR 会把
#: 客户端请求头原样转发给上游，所以图像请求必须自带一个常见的浏览器 UA；聊天链路不
#: 经过这层校验，保持默认 UA，不要顺手改成同一个值。
_IMAGE_REQUEST_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class OpenAICompatibleProvider(AsyncLLMProvider):
    """OpenAI-compatible provider backed by /v1/chat/completions.

    本框架只保留这一个实现：端点固定指向 AMKR，由它承担供应商与 Key 池、
    故障切换与采样参数固定。

    **所有调用都走流式**（``stream: true``），由本类把 SSE 增量在本地聚合成
    完整的 ``GenerationResult``，调用方拿到的仍是“一次完整生成”。这样做不是
    为了逐字展示，而是为了可靠性：CPA 这类上游在非流式聚合路径上经常返回
    200 空响应，流式是逐块转发、绕开那条路径；长生成也不再受“整个请求超时”
    限制，只有块间静默超过超时才算失败。

    ``api_key`` 必须是该工作空间的**推理 key**（``amkr_ik_…``）：AMKR 用它决定
    这次请求落在哪个空间，因此调用方换不掉。``workspace`` 仍会作为
    ``X-AMKR-Workspace`` 头发送，但在推理 key 生效时 AMKR **忽略**它——留着是为了
    兼容旧版 AMKR（那里空间只由请求头决定），而不是因为它在起作用。
    """

    _provider_name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        workspace: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._workspace = workspace.strip()

    def _build_url(self, request: GenerationRequest) -> str:
        return f"{self._base_url}/v1/chat/completions"

    def _build_auth_headers(self) -> dict[str, str]:
        """只带凭据与工作空间的请求头；multipart 请求不能手工指定 Content-Type。

        ``X-AMKR-Workspace`` 是 AMKR 自己的路由状态，不会被转发给上游；空值
        时不发送，等价于 AMKR 的默认工作空间。
        """
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if self._workspace:
            headers["X-AMKR-Workspace"] = self._workspace
        return headers

    def _build_headers(self) -> dict[str, str]:
        """构造 JSON 请求头。"""
        headers = self._build_auth_headers()
        headers["Content-Type"] = "application/json"
        return headers

    async def generate_async(
        self, request: GenerationRequest, return_reasoning: bool = False
    ) -> GenerationResult | tuple[str, GenerationResult]:
        """流式发起生成，并在本地把 SSE 增量聚合成完整结果。

        When return_reasoning=True, returns (reasoning_content, GenerationResult) tuple.
        """
        timeout_seconds = resolve_generation_timeout_seconds(request, self._timeout_seconds)
        url = self._build_url(request)
        debug_context = build_generation_debug_context(
            request,
            provider_name=self._provider_name,
            url=url,
            base_url=self._base_url,
            timeout_seconds=timeout_seconds,
        )

        logger.info(
            f"正准备向 {self._provider_name} 的 {request.model} 请教问题，"
            f"手头有 {debug_context['input_message_count']} 条消息想说，"
            f"预计要花 {debug_context['estimated_input_tokens']} 个 Token，"
            f"超时 {timeout_seconds:.1f} 秒～"
        )
        payload = build_chat_completion_payload(
            request,
            provider_name=self._provider_name,
        )
        wire_messages, transport_stats = prepare_openai_compatible_messages(
            cast(list[dict[str, object]], payload["messages"])
        )
        wire_payload = dict(payload)
        wire_payload["messages"] = wire_messages
        wire_payload["stream"] = True
        wire_payload["stream_options"] = dict(_STREAM_OPTIONS)

        body = json.dumps(wire_payload).encode("utf-8")
        logger.debug(
            f"[模型调用详情] {request.model} | 请求详情:\n"
            f"{json.dumps({**debug_context, **transport_stats, 'request_body_bytes': len(body), 'stream': True, 'payload': payload}, ensure_ascii=False, indent=2)}"
        )

        headers = self._build_headers()

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                async with client.stream("POST", url, content=body, headers=headers) as response:
                    status_code = response.status_code
                    content_type = str(response.headers.get("Content-Type", "")).strip()
                    if status_code >= 400:
                        raw = (await response.aread()).decode("utf-8", errors="replace")
                        self._raise_for_status(status_code, raw, request, url)
                    if not self._is_event_stream(content_type):
                        # 兼容：个别网关会忽略 stream 参数，直接回一整段 JSON。
                        raw = (await response.aread()).decode("utf-8", errors="replace")
                        logger.debug(
                            f"[模型原始响应] {request.model} | Provider: {self._provider_name} | URL: {url} "
                            f"| HTTP状态: {status_code} | Content-Type: {content_type or '(未知)'} | raw:\n{raw}"
                        )
                        result = self._parse_completion_body(
                            raw,
                            request=request,
                            status_code=status_code,
                            url=url,
                        )
                    else:
                        result = await self._consume_event_stream(
                            response,
                            request=request,
                            status_code=status_code,
                            url=url,
                        )
        except httpx.HTTPError as exc:
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
                f"| {type(exc).__name__}: {exc or '(无详细信息)'}",
                exc_info=True,
            )
            raise ProviderConnectionError(
                self._provider_name,
                f"提供商请求异常：{exc}",
                original_error=exc,
            ) from exc

        # 没有 tool_calls 且内容为空，说明上游确实没给东西。流式路径上这通常是
        # AMKR 的 error 事件之外的空壳（[DONE] 前没有任何 delta）。
        if not result.content and not result.tool_calls and not return_reasoning:
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
                f"| 响应为空 | finish_reason={result.finish_reason}"
            )
            raise ProviderResponseError(
                self._provider_name,
                "提供商响应内容为空。",
                http_status=status_code,
            )

        content = result.content or ""
        logger.info(f"{self._provider_name} 的 {request.model} 回复我了，写了 {len(content)} 个字～")
        logger.debug(
            f"[模型输出] {request.model} | Provider: {self._provider_name} | URL: {url} | 响应内容:\n{content}"
        )
        if return_reasoning:
            return (result.reasoning_content, result)
        return result

    async def generate_image_async(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        """调用 AMKR 的图像端点，返回生成图片的原始字节。

        ``model`` 仍是 AMKR 任务名；工作空间推理 key 同样适用于图像端点。没有参考
        图时走 ``/v1/images/generations``；带参考图时走 ``/v1/images/edits``，参考图
        以 data URL 放进 JSON 的 ``images[].image_url``。**不能**用 OpenAI 经典的
        multipart 表单：AMKR 只从表单里读 model 做路由，随后把字节原样转发给上游，
        表单里的 model 仍是任务名，上游会以「requires an image model」400 拒绝；
        JSON 形态则会被 AMKR 正常改写 model。图像请求固定携带浏览器 UA，原因见
        ``_IMAGE_REQUEST_USER_AGENT``。
        """
        timeout_seconds = request.timeout_seconds or max(float(self._timeout_seconds), 120.0)
        has_references = bool(request.reference_images)
        endpoint = "edits" if has_references else "generations"
        url = f"{self._base_url}/v1/images/{endpoint}"
        headers = self._build_auth_headers()
        headers["User-Agent"] = _IMAGE_REQUEST_USER_AGENT
        payload: dict[str, Any] = {"model": request.model, "prompt": request.prompt}
        if has_references:
            payload["images"] = [
                {"image_url": self._image_data_url(filename, data)}
                for filename, data in request.reference_images
            ]
        logger.info(
            f"正在向 {self._provider_name} 的 {request.model} 要一张图，"
            f"参考图 {len(request.reference_images)} 张，超时 {timeout_seconds:.1f} 秒～"
        )
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={**headers, "Content-Type": "application/json"},
                )
                raw = response.text
                if response.status_code >= 400:
                    self._raise_image_status(response.status_code, raw, request, url)
                body = self._parse_image_body(raw, url)
                result = await self._read_image_item(client, body, url)
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
        logger.info(f"{self._provider_name} 的画好了：{result.mime_type}，{len(result.image_bytes)} 字节")
        return result

    @staticmethod
    def _guess_image_mime(filename: str, data: bytes) -> str:
        sniffed = sniff_image_mime(data)
        if sniffed is not None:
            return sniffed
        guessed, _ = mimetypes.guess_type(filename)
        return guessed or "image/png"

    @classmethod
    def _image_data_url(cls, filename: str, data: bytes) -> str:
        """把参考图编成 data URL；媒体类型按字节判定，文件名后缀不作数。"""
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{cls._guess_image_mime(filename, data)};base64,{encoded}"

    def _parse_image_body(self, raw: str, url: str) -> dict[str, Any]:
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise ProviderResponseError(
                self._provider_name,
                f"生图接口返回的不是合法 JSON：{raw[:200]}",
                response_body=raw,
            ) from exc
        if not isinstance(body, dict):
            raise ProviderResponseError(
                self._provider_name,
                f"生图接口返回结构异常：{raw[:200]}",
                response_body=raw,
            )
        return body

    async def _read_image_item(
        self,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        url: str,
    ) -> ImageGenerationResult:
        """从 data[0] 取图片：优先内联 b64_json，退化时下载 url。"""
        items = body.get("data")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise ProviderResponseError(
                self._provider_name,
                "生图接口没有返回任何图片数据。",
                response_body=json.dumps(body, ensure_ascii=False)[:500],
            )
        item = items[0]
        encoded = str(item.get("b64_json") or "").strip()
        if encoded:
            try:
                image_bytes = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ProviderResponseError(
                    self._provider_name,
                    f"生图接口返回的 b64_json 无法解码：{exc}",
                    response_body=encoded[:200],
                ) from exc
            if not image_bytes:
                raise ProviderResponseError(
                    self._provider_name,
                    "生图接口返回了空图片。",
                    response_body=encoded[:200],
                )
            return ImageGenerationResult(
                image_bytes=image_bytes,
                mime_type=sniff_image_mime(image_bytes) or "image/png",
            )
        image_url = str(item.get("url") or "").strip()
        if not image_url:
            raise ProviderResponseError(
                self._provider_name,
                "生图接口既没有 b64_json 也没有 url。",
                response_body=json.dumps(body, ensure_ascii=False)[:500],
            )
        download = await client.get(image_url, headers={"User-Agent": _IMAGE_REQUEST_USER_AGENT})
        if download.status_code >= 400:
            raise ProviderResponseError(
                self._provider_name,
                f"下载生成图片失败，HTTP {download.status_code}：{image_url}",
                http_status=download.status_code,
            )
        image_bytes = download.content
        if not image_bytes:
            raise ProviderResponseError(
                self._provider_name,
                f"下载生成图片得到空内容：{image_url}",
            )
        return ImageGenerationResult(
            image_bytes=image_bytes,
            mime_type=self._guess_image_mime(image_url, image_bytes),
        )

    def _raise_image_status(
        self,
        status_code: int,
        raw: str,
        request: ImageGenerationRequest,
        url: str,
    ) -> NoReturn:
        logger.error(
            f"[生图失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
            f"| HTTP {status_code}: {raw[:500]}"
        )
        message = f"生图接口 HTTP 错误 {status_code}：{raw[:200]}"
        if status_code in (401, 403):
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

    async def _consume_event_stream(
        self,
        response: httpx.Response,
        *,
        request: GenerationRequest,
        status_code: int,
        url: str,
    ) -> GenerationResult:
        """把一条 OpenAI 兼容 SSE 流聚合成完整结果。

        shortcut: 只认单行 ``data:`` 帧（OpenAI 兼容流不会把一个 JSON 拆到多行），
        多行 data 会被当成坏帧跳过；若将来接入非标准 SSE 需要补行合并。
        """
        content_chunks: list[str] = []
        reasoning_chunks: list[str] = []
        tool_call_parts: dict[int, dict[str, str]] = {}
        finish_reason = "stop"
        usage: dict[str, object] | None = None

        async for line in response.aiter_lines():
            line = line.strip()
            if not line or line.startswith((":", "event:")):
                continue
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if not data_str:
                continue
            if data_str == "[DONE]":
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                # 心跳或半截帧：跳过，别为一行坏数据丢掉整段回复。
                continue
            if not isinstance(event, dict):
                continue

            usage_payload = event.get("usage")
            if isinstance(usage_payload, dict):
                usage = usage_payload

            error_payload = event.get("error")
            if error_payload:
                # AMKR 在上游流中断时补发 error 事件（而不是正常结束）。这里必须
                # 当失败抛，否则残片会被当成一次成功的空回复。
                message = self._error_message(error_payload)
                logger.error(
                    f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
                    f"| 流式响应错误: {message}"
                )
                raise ProviderResponseError(
                    self._provider_name,
                    f"提供商流式响应错误：{message}",
                )

            choices = event.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    reasoning_delta = delta.get("reasoning_content")
                    if isinstance(reasoning_delta, str) and reasoning_delta:
                        reasoning_chunks.append(reasoning_delta)
                    content_delta = delta.get("content")
                    if isinstance(content_delta, str) and content_delta:
                        content_chunks.append(content_delta)
                    self._merge_tool_call_delta(tool_call_parts, delta.get("tool_calls"))
                choice_finish = choice.get("finish_reason")
                if choice_finish:
                    finish_reason = str(choice_finish)

        set_last_generation_usage(dict(usage) if usage else None)
        return GenerationResult(
            content="".join(content_chunks),
            reasoning_content="".join(reasoning_chunks),
            tool_calls=self._finalize_tool_calls(tool_call_parts),
            finish_reason=finish_reason,
        )

    def _parse_completion_body(
        self,
        raw: str,
        *,
        request: GenerationRequest,
        status_code: int,
        url: str,
    ) -> GenerationResult:
        """解析非 SSE 的一整段 chat completion 响应（网关忽略 stream 时的回退）。"""
        if status_code >= 200 and status_code < 300 and raw.lstrip()[:1] not in ("{", "["):
            # 200 但不是 JSON：通常是反代/网关返回了 HTML 错误页。
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
                f"| HTTP {status_code} 响应非 JSON: {raw[:200]}"
            )
            raise ProviderResponseError(
                self._provider_name,
                f"提供商返回了非 JSON 响应（HTTP {status_code}）：{raw[:200]}",
                http_status=status_code,
                response_body=raw,
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
                f"| 响应 JSON 解析失败: {exc} | raw: {raw[:200]}"
            )
            raise ProviderResponseError(
                self._provider_name,
                f"提供商响应无法解析为 JSON：{exc}",
                http_status=status_code,
                response_body=raw,
            ) from exc
        if not isinstance(data, dict):
            raise ProviderResponseError(
                self._provider_name,
                "提供商响应的顶层结构不是对象。",
                http_status=status_code,
                response_body=raw,
            )
        choices = data.get("choices", [])
        if not choices:
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} | 无 choices"
            )
            raise ProviderResponseError(
                self._provider_name,
                "提供商响应中没有 choices。",
                http_status=status_code,
                response_body=raw,
            )

        choice = choices[0]
        message = choice.get("message", {})
        if not isinstance(message, dict):
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} | message 字段无效"
            )
            raise ProviderResponseError(
                self._provider_name,
                "提供商响应中 message 字段无效。",
                http_status=status_code,
                response_body=raw,
            )

        usage = data.get("usage")
        if isinstance(usage, dict):
            set_last_generation_usage(dict(usage))
        else:
            set_last_generation_usage(None)

        # reasoning_content is private model output and must never become the
        # assistant-visible reply when content is empty (for example on a tool round).
        content = extract_assistant_text(message, include_reasoning=False)
        reasoning_content = extract_assistant_text(
            {"content": message.get("reasoning_content")}, include_reasoning=False
        )
        return GenerationResult(
            content=content,
            reasoning_content=reasoning_content,
            tool_calls=self._parse_tool_calls(message.get("tool_calls")),
            finish_reason=str(choice.get("finish_reason", "stop")),
        )

    @staticmethod
    def _is_event_stream(content_type: str) -> bool:
        return "text/event-stream" in content_type.lower()

    @staticmethod
    def _error_message(error_payload: object) -> str:
        if isinstance(error_payload, dict):
            message = error_payload.get("message")
            if message:
                return str(message)
            code = error_payload.get("code")
            if code:
                return str(code)
        return str(error_payload)

    @staticmethod
    def _parse_tool_calls(raw_tool_calls: object) -> list[ToolCall] | None:
        if not isinstance(raw_tool_calls, list) or not raw_tool_calls:
            return None
        parsed: list[ToolCall] = []
        for raw_tool_call in raw_tool_calls:
            if not isinstance(raw_tool_call, dict):
                continue
            function = raw_tool_call.get("function", {})
            if not isinstance(function, dict):
                function = {}
            parsed.append(
                ToolCall(
                    id=str(raw_tool_call.get("id", "")),
                    type=str(raw_tool_call.get("type", "function")),
                    function_name=str(function.get("name", "")),
                    function_arguments=str(function.get("arguments", "")),
                )
            )
        return parsed or None

    @staticmethod
    def _merge_tool_call_delta(
        accumulator: dict[int, dict[str, str]],
        raw_tool_calls: object,
    ) -> None:
        """把流式 tool_calls 增量并进按 index 归位的累加器。

        流式协议里 function.name 与 arguments 都是跨块拼接的，id/type 只在首块
        出现（id 覆盖即可，name/arguments 必须累加）。
        """
        if not isinstance(raw_tool_calls, list):
            return
        for position, raw_tool_call in enumerate(raw_tool_calls):
            if not isinstance(raw_tool_call, dict):
                continue
            raw_index = raw_tool_call.get("index", position)
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                index = position
            entry = accumulator.setdefault(
                index,
                {"id": "", "type": "function", "name": "", "arguments": ""},
            )
            if raw_tool_call.get("id"):
                entry["id"] = str(raw_tool_call["id"])
            if raw_tool_call.get("type"):
                entry["type"] = str(raw_tool_call["type"])
            function = raw_tool_call.get("function")
            if isinstance(function, dict):
                if function.get("name"):
                    entry["name"] += str(function["name"])
                if function.get("arguments"):
                    entry["arguments"] += str(function["arguments"])

    @staticmethod
    def _finalize_tool_calls(
        accumulator: dict[int, dict[str, str]],
    ) -> list[ToolCall] | None:
        if not accumulator:
            return None
        return [
            ToolCall(
                id=entry["id"],
                type=entry["type"],
                function_name=entry["name"],
                function_arguments=entry["arguments"],
            )
            for _, entry in sorted(accumulator.items())
        ]

    def _raise_for_status(
        self,
        status_code: int,
        raw: str,
        request: GenerationRequest,
        url: str,
    ) -> NoReturn:
        logger.error(
            f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
            f"| HTTP {status_code}: {raw[:500]}"
        )
        message = f"提供商 HTTP 错误 {status_code}：{raw[:200]}"
        if status_code == 400 and "Failed to download multimodal content" in raw:
            message = (
                f"{message}。多模态文件下载失败：请确认 image_url 使用公网可访问的 http/https URL，"
                "且响应头包含 Content-Type 与 Content-Length；若传入的是本地图片路径，"
                "请直接传本地文件路径让框架自动转换为 data URL，或自行传入 data:*;base64,...。"
            )
        # 401/403 是凭据问题，重试不会变好——显式标为不可重试，避免重试风暴烧配额。
        if status_code in (401, 403):
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
