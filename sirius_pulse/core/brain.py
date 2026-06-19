"""LLM 交互中枢（Brain）—— 统一管理所有 LLM API 调用。

两条通道：
    - raw_call(): 原生 API 调用，不组装上下文，不注入人格。
                  用于 Cognition（情感/意图分析）。
    - chat():     全上下文组装 + 人格注入 + 前/后处理 hook。
                  用于回复生成、Plugin 风格化、SKILL 反馈循环等。

Hook 机制：
    chat() 内置默认的前处理和后处理步骤。外部可以通过
    register_pre_hook / register_post_hook 注册自定义 hook，
    在默认步骤之前/之后执行。

设计原则：
    项目本质 = 组装消息 → 喂给 API → 拿到原生文本。
    SKILL 调用通过 function_call (tools) 机制实现，不在文本中嵌入标记。
    Brain 是这一流程的唯一入口，任何外部只能通过有限的参数类来调控。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sirius_pulse.core.prompt_factory import PromptFactory, StyleAdapter, StyleParams
from sirius_pulse.core.utils import parse_sticker_tags, strip_conversation_history_xml
from sirius_pulse.providers.base import GenerationResult, ToolCall

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 参数类
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class ChatRequest:
    """对话生成请求 —— Brain chat() 通道的唯一入口参数。

    外部只可以通过这个类来调控 LLM 的基本参数和上下文可见性。
    """

    group_id: str
    user_id: str
    system_prompt: str
    messages: list[dict[str, Any]] = field(default_factory=list)

    # ── 任务控制 ──
    task_name: str = "response_generate"
    urgency: int = 0

    # ── 风格覆盖（可选） ──
    temperature: float | None = None
    max_tokens: int | None = None
    style_params: StyleParams | None = None

    # ── SKILL 控制 ──
    enable_skills: bool = True
    caller_is_developer: bool = False

    # ── 对话深度 ──
    last_reply_at: float = 0.0
    last_reply_depth: int = 0

    # ── 后处理控制 ──
    post_process: bool = False  # True = 启用 hook 调度（总闸），False = 完全跳过

    # ── 重试控制（transport 级） ──
    retry_max: int = 1  # 最多重试次数（总调用次数 = retry_max + 1）
    retry_delay: float = 1.0  # 重试间隔（秒）


@dataclass(slots=True)
class ChatResult:
    """chat() 通道的单轮结果。"""

    raw_text: str
    clean_text: str
    model_name: str
    duration_ms: float
    token_record: Any
    system_prompt: str = ""  # 存储本次对话使用的完整 system prompt
    sticker_names: list[str] = field(default_factory=list)
    has_tool_call: bool = False
    tool_calls: list[ToolCall] = field(default_factory=list)
    # 兼容旧接口
    has_skill_call: bool = False
    skill_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # 引用回复信息（由 _hook_reply_reference 填充）
    reply_references: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class RawRequest:
    """原生 API 调用请求 —— Brain raw_call() 通道的入口参数。

    用于 Cognition 等不注入人格、不组装上下文的场景。
    """

    model: str
    system_prompt: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    temperature: float = 0.2
    max_tokens: int = 512
    timeout_seconds: float = 30.0
    purpose: str = "cognition_analyze"
    response_format: dict[str, object] | None = None

    # ── 重试控制（transport 级） ──
    retry_max: int = 2  # 最多重试次数（总调用次数 = retry_max + 1）
    retry_delay: float = 1.0  # 重试间隔（秒）


# ═══════════════════════════════════════════════════════════════════════
# Hook 类型与优先级
# ═══════════════════════════════════════════════════════════════════════

# 前处理 hook：在 LLM 调用前修改 ChatRequest 或注入上下文
# ctx 是跨 hook 共享的字典，Brain 内置步骤也通过它传递中间状态
PreHook = Callable[["Brain", ChatRequest, dict[str, Any]], None]

# 后处理 hook：在 LLM 调用后处理 ChatResult
# ctx 携带前处理阶段产生的中间状态（如 gen_request、estimated_tokens 等）
PostHook = Callable[["Brain", ChatRequest, ChatResult, dict[str, Any]], None]

# 默认 priority 阶梯：
#   pre:  0 = 用户自定义（最早），50 = 引擎内置
#   post: 0 = 深度追踪，10 = breakdown，20 = 表情包，30 = 去重，40 = 记忆记录，
#         100 = 用户自定义（最后一道防线）
_PRE_DEFAULT_PRIORITY = 0
_POST_DEFAULT_PRIORITY = 100


@dataclass(slots=True)
class _PreHookEntry:
    """前处理 hook 包装。priority 越大越晚执行。

    task_filter: None = 始终执行（用户自定义 hook 默认）
                 非 None = 仅当 ctx["task_name"] 在集合中时执行（引擎内置 hook）
    """

    hook: PreHook
    priority: int = 0
    task_filter: set[str] | None = None


@dataclass(slots=True)
class _PostHookEntry:
    """后处理 hook 包装。priority 越大越晚执行。

    task_filter: None = 始终执行（用户自定义 hook 默认）
                 非 None = 仅当 ctx["task_name"] 在集合中时执行（引擎内置 hook）
    """

    hook: PostHook
    priority: int = 100
    task_filter: set[str] | None = None


# ═══════════════════════════════════════════════════════════════════════
# Brain
# ═══════════════════════════════════════════════════════════════════════


class Brain:
    """LLM 交互中枢。

    两条通道：
    - raw_call(request: RawRequest) → str
      原生 API 调用，不组装上下文，不注入人格，不解析 SKILL。
      用于 Cognition（情感/意图分析）等纯分析类任务。

    - chat(request: ChatRequest) → ChatResult
      上下文感知的对话生成。内置默认处理链：
        pre:  人格注入 → 语气对齐 → 模型路由 → 风格覆盖 → 构建请求 → 当前时间追加
        call: provider.generate_async()
        post: XML 剥离 → SKIP 检测 → SKILL 解析 → 表情包解析 → token 记录
      可通过 register_pre_hook / register_post_hook 扩展。
    """

    def __init__(
        self,
        *,
        provider_async: Any,
        model_router: Any,
        persona: Any,
        rhythm_analyzer: Any | None = None,
        style_adapter: StyleAdapter | None = None,
        config: dict[str, Any] | None = None,
        token_store: Any | None = None,
        token_usage_records: list[Any] | None = None,
        sticker_names: list[str] | None = None,
        other_ai_names: list[str] | None = None,
        skill_registry: Any | None = None,
    ) -> None:
        self.provider_async = provider_async
        self.router = model_router
        self.persona = persona
        self.rhythm_analyzer = rhythm_analyzer
        self.style_adapter = style_adapter or StyleAdapter()
        self.config = dict(config or {})
        self.token_store = token_store
        self.token_usage_records: list[Any] = list(token_usage_records or [])
        self.sticker_names = list(sticker_names or [])
        self.other_ai_names = list(other_ai_names or [])
        self.skill_registry = skill_registry

        # 上下文函数（延迟注入，避免循环导入）
        self._recent_messages_fn: Callable[[str, int], list[dict[str, Any]]] | None = None
        self._get_tone_alignment_fn: Callable[[str], str] | None = None
        self._classify_exception_fn: Callable[[Exception], str] | None = None
        self._get_pinned_messages_fn: Callable[[str], list[Any]] | None = None
        self.current_adapter_type_fn: Callable[[], str | None] | None = None
        self.current_admin_allowed_fn: Callable[[str], bool] | None = None

        # ── Hook 注册表 ──
        self._pre_hooks: list[_PreHookEntry] = []
        self._post_hooks: list[_PostHookEntry] = []

        # ── chat() 串行化锁 ──
        self._chat_lock = asyncio.Lock()

    # ═══════════════════════════════════════════════════════════════════
    # 上下文函数注入
    # ═══════════════════════════════════════════════════════════════════

    def set_context_fns(
        self,
        *,
        recent_messages_fn: Callable[[str, int], list[dict[str, Any]]] | None = None,
        tone_alignment_fn: Callable[[str], str] | None = None,
        classify_exception_fn: Callable[[Exception], str] | None = None,
        pinned_messages_fn: Callable[[str], list[Any]] | None = None,
    ) -> None:
        """注入引擎上下文函数（延迟绑定，避免循环导入）。"""
        if recent_messages_fn is not None:
            self._recent_messages_fn = recent_messages_fn
        if tone_alignment_fn is not None:
            self._get_tone_alignment_fn = tone_alignment_fn
        if classify_exception_fn is not None:
            self._classify_exception_fn = classify_exception_fn
        if pinned_messages_fn is not None:
            self._get_pinned_messages_fn = pinned_messages_fn

    # ═══════════════════════════════════════════════════════════════════
    # Hook 注册 API
    # ═══════════════════════════════════════════════════════════════════

    def register_pre_hook(
        self,
        hook: PreHook,
        priority: int = _PRE_DEFAULT_PRIORITY,
        task_filter: set[str] | None = None,
    ) -> None:
        """注册前处理 hook。priority 越大越晚执行（默认 0，最先执行）。

        task_filter: None = 对所有 task_name 生效；非 None = 仅对集合中的 task_name 生效。
        签名: hook(brain, request, ctx) -> None
        - request: ChatRequest（可修改 system_prompt、messages 等）
        - ctx: 跨 hook 共享的字典
        """
        self._pre_hooks.append(_PreHookEntry(hook=hook, priority=priority, task_filter=task_filter))
        self._pre_hooks.sort(key=lambda e: e.priority)

    def register_post_hook(
        self,
        hook: PostHook,
        priority: int = _POST_DEFAULT_PRIORITY,
        task_filter: set[str] | None = None,
    ) -> None:
        """注册后处理 hook。priority 越大越晚执行（默认 100，最后执行）。

        task_filter: None = 对所有 task_name 生效；非 None = 仅对集合中的 task_name 生效。
        签名: hook(brain, request, result, ctx) -> None
        - request: 原始 ChatRequest
        - result: ChatResult（可修改 clean_text、sticker_names 等）
        - ctx: 跨 hook 共享的字典
        """
        self._post_hooks.append(
            _PostHookEntry(hook=hook, priority=priority, task_filter=task_filter)
        )
        self._post_hooks.sort(key=lambda e: e.priority)

    # ═══════════════════════════════════════════════════════════════════
    # 通道 1：原生 API 调用（Cognition 等分析任务）
    # ═══════════════════════════════════════════════════════════════════

    async def raw_call(self, request: RawRequest) -> str:
        """直接调用 LLM API，只做最小处理。

        处理链：
        1. 构建 GenerationRequest
        2. provider.generate_async()（带 transport 级重试）
        3. 基础 token 统计
        4. 返回原始文本

        不做：人格注入、上下文组装、SKILL 解析、表情包解析。
        """
        from sirius_pulse.providers.base import GenerationRequest

        gen_request = GenerationRequest(
            model=request.model,
            system_prompt=request.system_prompt,
            messages=request.messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            timeout_seconds=request.timeout_seconds,
            purpose=request.purpose,
            response_format=request.response_format,
        )

        t0 = time.perf_counter()
        raw, _, _ = await self._call_with_retry(
            gen_request,
            retry_max=request.retry_max,
            retry_delay=request.retry_delay,
            purpose_desc=f"raw_call({request.purpose})",
        )
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)

        raw_text = raw.content or ""
        self._record_raw_tokens(gen_request, raw_text, duration_ms)

        return raw_text

    # ═══════════════════════════════════════════════════════════════════
    # 通道 2：对话生成（回复、Plugin 风格化等）
    # ═══════════════════════════════════════════════════════════════════

    async def chat(self, request: ChatRequest) -> ChatResult:
        """执行一次上下文感知的对话生成（串行执行）。

        chat() 调用之间串行化，保证消息处理顺序与接收顺序一致。
        raw_call() 不受此锁影响，可与 chat() 并行。

        处理链：
        1. 用户 pre-hooks（按注册顺序）
        2. 默认 pre: 语气对齐 → 时间注入 → 模型路由 → 风格覆盖
        3. provider.generate_async()（带 transport 级重试，重试时刷新上下文）
        4. 默认 post: XML 剥离 → SKIP 检测 → SKILL 解析 → 表情包解析 → token 记录
        5. 用户 post-hooks（按注册顺序）

        SKILL 反馈循环由调用方管理，chat() 只负责单轮生成。
        """
        async with self._chat_lock:
            ctx: dict[str, Any] = {}
            ctx["task_name"] = request.task_name
            system_prompt = request.system_prompt

            # ── 1. 用户 pre-hooks（post_process 总闸 + task_filter 过滤）──
            task_name = ctx["task_name"]
            if request.post_process:
                for entry in self._pre_hooks:
                    if entry.task_filter is not None and task_name not in entry.task_filter:
                        continue
                    entry.hook(self, request, ctx)

            # ── 2. 默认 pre: 人格注入（无条件，最高优先级）──
            persona_base = self.persona.build_system_prompt()
            system_prompt = persona_base + "\n\n" + system_prompt

            # ── 3. 默认 pre: 语气对齐 ──
            if self._get_tone_alignment_fn is not None:
                tone_hint = self._get_tone_alignment_fn(request.group_id)
                if tone_hint:
                    system_prompt = system_prompt + "\n\n" + tone_hint

            # 保存时间注入前的 prompt 基底，用于重试时以最新时间刷新
            base_system_prompt = system_prompt

            # ── 默认 pre: 当前时间注入 ──
            china_tz = timezone(timedelta(hours=8))
            now_dt = datetime.now(china_tz)
            weekdays = [
                "星期一",
                "星期二",
                "星期三",
                "星期四",
                "星期五",
                "星期六",
                "星期日",
            ]
            wd = weekdays[now_dt.weekday()]
            now_str = f"{now_dt.strftime('%Y-%m-%d')} {wd} {now_dt.strftime('%H:%M:%S')}"
            system_prompt = (
                system_prompt + "\n\n" + PromptFactory.build_current_time_section(now_str)
            )

            # ── 默认 pre: 模型路由 ──
            heat_level = "warm"
            if self.rhythm_analyzer is not None and self._recent_messages_fn is not None:
                recent = self._recent_messages_fn(request.group_id, 5)
                rhythm = self.rhythm_analyzer.analyze(request.group_id, recent)
                heat_level = rhythm.heat_level

            cfg = self.router.resolve(
                request.task_name,
                urgency=request.urgency,
                heat_level=heat_level,
            )

            # ── 默认 pre: 风格覆盖 ──
            if request.style_params:
                effective_max_tokens = min(cfg.max_tokens, request.style_params.max_tokens)
                effective_temperature = request.style_params.temperature
            elif request.temperature is not None or request.max_tokens is not None:
                effective_max_tokens = request.max_tokens if request.max_tokens else cfg.max_tokens
                effective_temperature = (
                    request.temperature if request.temperature else cfg.temperature
                )
            else:
                effective_max_tokens = cfg.max_tokens
                effective_temperature = cfg.temperature

            # ── 构建 tools 参数 ──
            tools = None
            if request.enable_skills and self.skill_registry is not None:
                from sirius_pulse.memory.user.unified_models import UnifiedUser
                from sirius_pulse.skills.models import SkillInvocationContext

                caller = UnifiedUser(
                    user_id=request.user_id or "caller",
                    name="caller",
                    metadata={"is_developer": request.caller_is_developer},
                )
                inv_ctx = SkillInvocationContext(caller=caller)
                adapter_type = (
                    self.current_adapter_type_fn()
                    if self.current_adapter_type_fn is not None
                    else None
                )
                tools = self.skill_registry.build_tools_list(
                    invocation_context=inv_ctx,
                    adapter_type=adapter_type,
                    chat_type="private" if request.group_id.startswith("private_") else "group",
                    admin_allowed=(
                        self.current_admin_allowed_fn(request.group_id)
                        if self.current_admin_allowed_fn is not None
                        else False
                    ),
                )
                if not tools:
                    tools = None

            # ── 构建 GenerationRequest ──
            from sirius_pulse.providers.base import GenerationRequest

            gen_request = GenerationRequest(
                model=cfg.model_name,
                system_prompt=system_prompt.strip(),
                messages=request.messages,
                tools=tools,
                temperature=effective_temperature,
                max_tokens=effective_max_tokens,
                timeout_seconds=cfg.timeout,
                purpose=request.task_name,
            )

            # 估算输入 token
            from sirius_pulse.providers.base import estimate_generation_request_input_tokens

            estimated_input_tokens = estimate_generation_request_input_tokens(gen_request)

            # 定义重试时的 gen_request 刷新函数：重新拉取上下文 + 刷新当前时间
            async def _rebuild_gen_request() -> GenerationRequest:
                if request.post_process:
                    for entry in self._pre_hooks:
                        if entry.task_filter is not None and task_name not in entry.task_filter:
                            continue
                        entry.hook(self, request, ctx)
                china_tz = timezone(timedelta(hours=8))
                now_dt = datetime.now(china_tz)
                weekdays = [
                    "星期一",
                    "星期二",
                    "星期三",
                    "星期四",
                    "星期五",
                    "星期六",
                    "星期日",
                ]
                wd = weekdays[now_dt.weekday()]
                now_str = f"{now_dt.strftime('%Y-%m-%d')} {wd} " f"{now_dt.strftime('%H:%M:%S')}"
                fresh_time = PromptFactory.build_current_time_section(now_str)
                fresh_system_prompt = base_system_prompt + "\n\n" + fresh_time
                return GenerationRequest(
                    model=cfg.model_name,
                    system_prompt=fresh_system_prompt.strip(),
                    messages=request.messages,
                    tools=tools,
                    temperature=effective_temperature,
                    max_tokens=effective_max_tokens,
                    timeout_seconds=cfg.timeout,
                    purpose=request.task_name,
                )

            # 调试日志
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "LLM prompt for group=%s:\nSYSTEM:\n%s\n\nMESSAGES:\n%s",
                    request.group_id,
                    system_prompt,
                    "\n".join(
                        f"  [{m.get('role')}] {m.get('content', '')[:200]}"
                        for m in request.messages
                    ),
                )

            # ── 3. 调用 provider（带 transport 级重试，重试时刷新上下文）──
            gen_result: GenerationResult | None = None
            duration_ms = 0.0
            real_usage: dict | None = None
            try:
                t0 = time.perf_counter()
                gen_result, gen_request, real_usage = await self._call_with_retry(
                    gen_request,
                    retry_max=request.retry_max,
                    retry_delay=request.retry_delay,
                    purpose_desc=f"chat({request.task_name})",
                    rebuild_fn=_rebuild_gen_request,
                )
                duration_ms = round((time.perf_counter() - t0) * 1000, 2)
                # gen_request 可能在重试时被刷新，重新估算输入 token
                estimated_input_tokens = estimate_generation_request_input_tokens(gen_request)
            except Exception as exc:
                error_type = (
                    self._classify_exception_fn(exc) if self._classify_exception_fn else "unknown"
                )
                error_message = str(exc)[:200]
                logger.warning(
                    "[%s] 生成失败: %s | %s",
                    request.task_name,
                    error_type,
                    error_message,
                )
                raise

            reply = gen_result.content or ""

            # ── 4. 默认 post: 剥离模型回显的 XML 块 ──
            reply = strip_conversation_history_xml(reply)

            # ── 默认 post: SKIP 标签检测 ──
            if re.search(r"<\s*skip\s*/?\s*>", reply, flags=re.IGNORECASE):
                logger.info("[%s] LLM 主动选择跳过回复（输出 skip 标签）。", request.task_name)
                reply = ""

            # ── 默认 post: 处理 tool_calls ──
            tool_calls: list[ToolCall] = gen_result.tool_calls or []

            # ── 默认 post: 解析表情包标签 ──
            sticker_names: list[str] = []
            clean_reply = reply.strip()
            if clean_reply:
                clean_reply, sticker_names = parse_sticker_tags(clean_reply, self.sticker_names)

            # ── 默认 post: 记录 token 用量 ──
            token_record = self._record_chat_tokens(
                gen_request=gen_request,
                system_prompt_used=system_prompt,
                reply=reply,
                estimated_input_tokens=estimated_input_tokens,
                duration_ms=duration_ms,
                group_id=request.group_id,
                task_name=request.task_name,
                last_reply_at=request.last_reply_at,
                last_reply_depth=request.last_reply_depth,
                real_usage=real_usage,
            )

            result = ChatResult(
                raw_text=reply,
                clean_text=clean_reply,
                model_name=cfg.model_name,
                duration_ms=duration_ms,
                token_record=token_record,
                system_prompt=system_prompt,
                sticker_names=sticker_names,
                has_tool_call=bool(tool_calls),
                tool_calls=tool_calls,
            )

            # ── 5. 用户 post-hooks（post_process 总闸 + task_filter 过滤）──
            if request.post_process:
                for entry in self._post_hooks:  # type: ignore[assignment]
                    if entry.task_filter is not None and task_name not in entry.task_filter:
                        continue
                    entry.hook(self, request, result, ctx)  # type: ignore[arg-type, call-arg]

            return result

    async def generate_text(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        group_id: str,
        *,
        style_params: StyleParams | None = None,
        task_name: str = "response_generate",
        urgency: int = 0,
        enable_skills: bool = False,
        post_process: bool = False,
    ) -> str:
        """便捷方法：单轮 chat() → 返回 raw_text。

        供外部模块（plugins、skill context、dispatcher）使用，
        无需处理 ChatResult 对象。默认不启用引擎 post-hooks。
        """
        result = await self.chat(
            ChatRequest(
                group_id=group_id,
                user_id="",
                system_prompt=system_prompt,
                messages=messages,
                task_name=task_name,
                urgency=urgency,
                style_params=style_params,
                enable_skills=enable_skills,
                post_process=post_process,
            )
        )
        return result.clean_text

    # ═══════════════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════════════

    async def _call_with_retry(
        self,
        gen_request: Any,
        *,
        retry_max: int,
        retry_delay: float,
        purpose_desc: str,
        rebuild_fn: Callable[[], Any] | None = None,
    ) -> tuple[GenerationResult, Any, dict | None]:
        """调用 provider，在 transport 级异常时自动重试。

        重试阈值由调用方通过 retry_max / retry_delay 控制。
        如果提供 rebuild_fn，在每次重试前调用它以获取新的 gen_request，
        从而在重试时可以使用最新的上下文（如最新消息、当前时间等）。
        全部重试耗尽后抛出最后一次异常。

        Returns:
            (GenerationResult, 最终使用的 gen_request, 真实 token 用量或 None)
        """
        from sirius_pulse.providers.base import get_last_generation_usage

        current = gen_request
        last_exc: Exception | None = None
        for attempt in range(retry_max + 1):
            try:
                result = await self._provider_call(current)
                # 立即捕获 token 用量，避免后续 await 点被其他协程覆盖
                real_usage = get_last_generation_usage()
                return result, current, real_usage
            except Exception as exc:
                last_exc = exc
                if attempt < retry_max:
                    logger.warning(
                        "%s LLM 调用失败 (attempt=%d/%d): %s",
                        purpose_desc,
                        attempt + 1,
                        retry_max + 1,
                        exc,
                    )
                    await asyncio.sleep(retry_delay)
                    if rebuild_fn:
                        current = (
                            await rebuild_fn()
                            if asyncio.iscoroutinefunction(rebuild_fn)
                            else rebuild_fn()
                        )
                else:
                    logger.error(
                        "%s LLM 调用已耗尽 %d 次重试: %s",
                        purpose_desc,
                        retry_max + 1,
                        exc,
                    )
        # 所有重试均失败，抛出最后一次异常
        raise last_exc  # type: ignore[misc]

    async def _provider_call(self, request: Any) -> GenerationResult:
        """调用 provider 生成回复。"""
        from sirius_pulse.providers.base import LLMProvider

        if hasattr(self.provider_async, "generate_async"):
            return await self.provider_async.generate_async(request)
        elif isinstance(self.provider_async, LLMProvider):
            text = await asyncio.to_thread(self.provider_async.generate, request)
            return GenerationResult(content=text)
        else:
            raise RuntimeError("配置的提供商未实现 generate/generate_async 方法。")

    def _record_raw_tokens(
        self,
        gen_request: Any,
        raw_output: str,
        duration_ms: float,
    ) -> None:
        """记录 raw_call() 通道的基础 token 用量。"""
        from sirius_pulse.config import TokenUsageRecord
        from sirius_pulse.providers.base import estimate_generation_request_input_tokens
        from sirius_pulse.token.utils import estimate_tokens

        estimated_input_tokens = estimate_generation_request_input_tokens(gen_request)
        estimated_output_tokens = estimate_tokens(raw_output) if raw_output else 0

        persona_name = self.persona.name if self.persona else ""
        provider_name = getattr(
            self.provider_async,
            "_last_provider_name",
            getattr(self.provider_async, "_provider_name", "unknown"),
        )

        # 构建 breakdown：拆分 system_prompt / user_message / output 三段 token 分布
        sp_total = estimate_tokens(gen_request.system_prompt or "")
        um_total = sum(
            estimate_tokens(str(m.get("content", ""))) for m in (gen_request.messages or [])
        )
        out_total = estimated_output_tokens
        breakdown_json = json.dumps(
            {
                "system_prompt_total": sp_total,
                "user_message": um_total,
                "output_total": out_total,
                "total": sp_total + um_total + out_total,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        record = TokenUsageRecord(
            actor_id="assistant",
            task_name=gen_request.purpose,
            model=gen_request.model,
            prompt_tokens=estimated_input_tokens,
            completion_tokens=estimated_output_tokens,
            total_tokens=estimated_input_tokens + estimated_output_tokens,
            input_chars=sum(len(str(m.get("content", ""))) for m in (gen_request.messages or []))
            + len(gen_request.system_prompt),
            output_chars=len(raw_output),
            estimation_method="tiktoken" if estimated_output_tokens > 0 else "char_div4",
            retries_used=0,
            persona_name=persona_name,
            group_id="",
            provider_name=provider_name,
            breakdown_json=breakdown_json,
            duration_ms=duration_ms,
            conversation_depth=0,
        )
        self.token_usage_records.append(record)
        if self.token_store is not None:
            try:
                self.token_store.add(record)
            except Exception:
                pass

    def _record_chat_tokens(
        self,
        *,
        gen_request: Any,
        system_prompt_used: str,
        reply: str,
        estimated_input_tokens: int,
        duration_ms: float,
        group_id: str,
        task_name: str,
        last_reply_at: float,
        last_reply_depth: int,
        real_usage: dict | None = None,
    ) -> Any:
        """记录 chat() 通道的完整 token 用量。"""
        from sirius_pulse.config import TokenUsageRecord
        from sirius_pulse.token.utils import estimate_tokens

        output_chars = len(reply)
        estimated_output_tokens = estimate_tokens(reply) if reply else 0
        if real_usage and isinstance(real_usage, dict):
            prompt_tokens = int(real_usage.get("prompt_tokens", estimated_input_tokens))
            completion_tokens = int(real_usage.get("completion_tokens", estimated_output_tokens))
            total_tokens = int(real_usage.get("total_tokens", prompt_tokens + completion_tokens))
            estimation_method = "provider_real"
        else:
            prompt_tokens = estimated_input_tokens
            completion_tokens = estimated_output_tokens
            total_tokens = estimated_input_tokens + estimated_output_tokens
            estimation_method = "tiktoken" if estimated_output_tokens > 0 else "char_div4"

        persona_name = self.persona.name if self.persona else ""
        provider_name = getattr(
            self.provider_async,
            "_last_provider_name",
            getattr(self.provider_async, "_provider_name", "unknown"),
        )

        now_ts = time.time()
        conversation_depth = last_reply_depth + 1 if now_ts - last_reply_at < 60 else 1

        record = TokenUsageRecord(
            actor_id="assistant",
            task_name=task_name,
            model=gen_request.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            input_chars=len(system_prompt_used)
            + sum(len(str(m.get("content", ""))) for m in (gen_request.messages or [])),
            output_chars=output_chars,
            estimation_method=estimation_method,
            retries_used=0,
            persona_name=persona_name,
            group_id=group_id,
            provider_name=provider_name,
            breakdown_json="",
            duration_ms=duration_ms,
            conversation_depth=conversation_depth,
        )
        self.token_usage_records.append(record)

        if self.token_store is not None:
            try:
                self.token_store.add(record)
            except Exception:
                logger.warning("token_store.add() 失败", exc_info=True)
                pass

        return record
