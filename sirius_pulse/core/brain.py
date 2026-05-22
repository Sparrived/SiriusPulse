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
    哪怕 [SKILL_CALL:] 也只是原生文本里的一种标记。
    Brain 是这一流程的唯一入口，任何外部只能通过有限的参数类来调控。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sirius_pulse.core.prompt_factory import PromptFactory, StyleAdapter, StyleParams

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
    post_process: bool = False  # True 时引擎 post-hooks 执行（记忆/去重/表情包等）


@dataclass(slots=True)
class ChatResult:
    """chat() 通道的单轮结果。"""

    raw_text: str
    clean_text: str
    model_name: str
    duration_ms: float
    token_record: Any
    sticker_names: list[str] = field(default_factory=list)
    has_skill_call: bool = False
    skill_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


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
class _HookEntry:
    """带优先级的 hook 包装。priority 越大越晚执行。"""
    hook: PreHook | PostHook
    priority: int = 0


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
        pre:  语气对齐 → 当前时间注入 → 模型路由 → 风格覆盖 → 构建请求
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

        # 上下文函数（延迟注入，避免循环导入）
        self._recent_messages_fn: Callable[[str, int], list[dict[str, Any]]] | None = None
        self._get_tone_alignment_fn: Callable[[str], str] | None = None
        self._classify_exception_fn: Callable[[Exception], str] | None = None

        # ── Hook 注册表 ──
        self._pre_hooks: list[_HookEntry] = []
        self._post_hooks: list[_HookEntry] = []

    # ═══════════════════════════════════════════════════════════════════
    # 上下文函数注入
    # ═══════════════════════════════════════════════════════════════════

    def set_context_fns(
        self,
        *,
        recent_messages_fn: Callable[[str, int], list[dict[str, Any]]] | None = None,
        tone_alignment_fn: Callable[[str], str] | None = None,
        classify_exception_fn: Callable[[Exception], str] | None = None,
    ) -> None:
        """注入引擎上下文函数（延迟绑定，避免循环导入）。"""
        if recent_messages_fn is not None:
            self._recent_messages_fn = recent_messages_fn
        if tone_alignment_fn is not None:
            self._get_tone_alignment_fn = tone_alignment_fn
        if classify_exception_fn is not None:
            self._classify_exception_fn = classify_exception_fn

    # ═══════════════════════════════════════════════════════════════════
    # Hook 注册 API
    # ═══════════════════════════════════════════════════════════════════

    def register_pre_hook(self, hook: PreHook, priority: int = _PRE_DEFAULT_PRIORITY) -> None:
        """注册前处理 hook。priority 越大越晚执行（默认 0，最先执行）。

        签名: hook(brain, request, ctx) -> None
        - request: ChatRequest（可修改 system_prompt、messages 等）
        - ctx: 跨 hook 共享的字典
        """
        self._pre_hooks.append(_HookEntry(hook=hook, priority=priority))
        self._pre_hooks.sort(key=lambda e: e.priority)

    def register_post_hook(self, hook: PostHook, priority: int = _POST_DEFAULT_PRIORITY) -> None:
        """注册后处理 hook。priority 越大越晚执行（默认 100，最后执行）。

        签名: hook(brain, request, result, ctx) -> None
        - request: 原始 ChatRequest
        - result: ChatResult（可修改 clean_text、sticker_names 等）
        - ctx: 跨 hook 共享的字典
        """
        self._post_hooks.append(_HookEntry(hook=hook, priority=priority))
        self._post_hooks.sort(key=lambda e: e.priority)

    # ═══════════════════════════════════════════════════════════════════
    # 通道 1：原生 API 调用（Cognition 等分析任务）
    # ═══════════════════════════════════════════════════════════════════

    async def raw_call(self, request: RawRequest) -> str:
        """直接调用 LLM API，只做最小处理。

        处理链：
        1. 构建 GenerationRequest
        2. provider.generate_async()
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
        )

        t0 = time.perf_counter()
        raw = await self._provider_call(gen_request)
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)

        self._record_raw_tokens(gen_request, raw, duration_ms)

        return raw

    # ═══════════════════════════════════════════════════════════════════
    # 通道 2：对话生成（回复、Plugin 风格化等）
    # ═══════════════════════════════════════════════════════════════════

    async def chat(self, request: ChatRequest) -> ChatResult:
        """执行一次上下文感知的对话生成。

        处理链：
        1. 用户 pre-hooks（按注册顺序）
        2. 默认 pre: 语气对齐 → 时间注入 → 模型路由 → 风格覆盖
        3. provider.generate_async()
        4. 默认 post: XML 剥离 → SKIP 检测 → SKILL 解析 → 表情包解析 → token 记录
        5. 用户 post-hooks（按注册顺序）

        SKILL 反馈循环由调用方管理，chat() 只负责单轮生成。
        """
        ctx: dict[str, Any] = {}
        ctx["post_process"] = request.post_process
        system_prompt = request.system_prompt

        # ── 1. 用户 pre-hooks（按 priority 升序）──
        for entry in self._pre_hooks:
            entry.hook(self, request, ctx)

        # ── 2. 默认 pre: 语气对齐 ──
        if self._get_tone_alignment_fn is not None:
            tone_hint = self._get_tone_alignment_fn(request.group_id)
            if tone_hint:
                system_prompt = system_prompt + "\n\n" + tone_hint

        # ── 默认 pre: 当前时间注入 ──
        china_tz = timezone(timedelta(hours=8))
        now_str = datetime.now(china_tz).strftime("%Y-%m-%d %H:%M:%S")
        system_prompt = PromptFactory.build_current_time_section(now_str) + "\n\n" + system_prompt

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
            effective_temperature = request.temperature if request.temperature else cfg.temperature
        else:
            effective_max_tokens = cfg.max_tokens
            effective_temperature = cfg.temperature

        # ── 构建 GenerationRequest ──
        from sirius_pulse.providers.base import GenerationRequest

        gen_request = GenerationRequest(
            model=cfg.model_name,
            system_prompt=system_prompt.strip(),
            messages=request.messages,
            temperature=effective_temperature,
            max_tokens=effective_max_tokens,
            timeout_seconds=cfg.timeout,
            purpose=request.task_name,
        )

        # 估算输入 token
        from sirius_pulse.providers.base import estimate_generation_request_input_tokens

        estimated_input_tokens = estimate_generation_request_input_tokens(gen_request)

        # 调试日志
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "LLM prompt for group=%s:\nSYSTEM:\n%s\n\nMESSAGES:\n%s",
                request.group_id,
                system_prompt,
                "\n".join(
                    f"  [{m.get('role')}] {m.get('content', '')[:200]}" for m in request.messages
                ),
            )

        # ── 3. 调用 provider ──
        reply = ""
        duration_ms = 0.0
        try:
            t0 = time.perf_counter()
            reply = await self._provider_call(gen_request)
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
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

        # ── 4. 默认 post: 剥离模型回显的 XML 块 ──
        reply = self._strip_conversation_history_xml(reply)

        # ── 默认 post: SKIP 标签检测 ──
        if re.search(r"<\s*skip\s*/?\s*>", reply, flags=re.IGNORECASE):
            logger.info("[%s] LLM 主动选择跳过回复（输出 skip 标签）。", request.task_name)
            reply = ""

        # ── 默认 post: 解析 SKILL_CALL 标记 ──
        skill_calls: list[tuple[str, dict[str, Any]]] = []
        if request.enable_skills:
            from sirius_pulse.skills.executor import parse_skill_calls

            skill_calls = parse_skill_calls(reply)

        # ── 默认 post: 解析表情包标签 ──
        from sirius_pulse.skills.executor import strip_skill_calls

        sticker_names: list[str] = []
        clean_reply = strip_skill_calls(reply).strip()
        if clean_reply:
            clean_reply, sticker_names = self._parse_sticker_tags(clean_reply)

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
        )

        result = ChatResult(
            raw_text=reply,
            clean_text=clean_reply,
            model_name=cfg.model_name,
            duration_ms=duration_ms,
            token_record=token_record,
            sticker_names=sticker_names,
            has_skill_call=bool(skill_calls),
            skill_calls=skill_calls,
        )

        # ── 5. 用户 post-hooks（按 priority 升序）──
        for entry in self._post_hooks:
            entry.hook(self, request, result, ctx)

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
        return result.raw_text

    # ═══════════════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════════════

    async def _provider_call(self, request: Any) -> str:
        """调用 provider 生成回复。"""
        from sirius_pulse.providers.base import LLMProvider

        if hasattr(self.provider_async, "generate_async"):
            return await self.provider_async.generate_async(request)
        elif isinstance(self.provider_async, LLMProvider):
            return await asyncio.to_thread(self.provider_async.generate, request)
        else:
            raise RuntimeError("配置的提供商未实现 generate/generate_async 方法。")

    @staticmethod
    def _strip_conversation_history_xml(text: str) -> str:
        """移除 LLM 模型可能回显的 conversation_history XML 块。"""
        if not text:
            return text
        cleaned = re.sub(
            r"<\s*conversation_history\s*[^>]*>.*?</\s*conversation_history\s*>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        return cleaned.strip()

    @staticmethod
    def _parse_sticker_tags(text: str) -> tuple[str, list[str]]:
        """从回复文本中解析 [STICKERS: "name1", "name2"] 格式的标签。

        Returns:
            (清理后的文本, 选中的表情包名称列表)
        """
        pattern = r"\[STICKERS:\s*(.+?)\s*\]"
        match = re.search(pattern, text)
        if not match:
            return text, []

        raw = match.group(1)
        names: list[str] = []
        for part in re.split(r"\s*,\s*", raw):
            part = part.strip()
            while part and part[0] in "'\"\u201c\u2018\u300c":
                part = part[1:]
            while part and part[-1] in "'\"\u201d\u2019\u300d":
                part = part[:-1]
            if part:
                names.append(part)

        chosen = names[:3]
        prefix = text[: match.start()].rstrip()
        suffix = text[match.end():].lstrip()
        cleaned_text = f"{prefix} {suffix}".strip() if prefix and suffix else (prefix + suffix)
        return cleaned_text, chosen

    def _record_raw_tokens(
        self,
        gen_request: Any,
        raw_output: str,
        duration_ms: float,
    ) -> None:
        """记录 raw_call() 通道的基础 token 用量。"""
        from sirius_pulse.config import TokenUsageRecord
        from sirius_pulse.providers.base import estimate_generation_request_input_tokens

        estimated_input_tokens = estimate_generation_request_input_tokens(gen_request)
        from sirius_pulse.token.utils import estimate_tokens

        estimated_output_tokens = estimate_tokens(raw_output) if raw_output else 0

        persona_name = self.persona.name if self.persona else ""
        provider_name = getattr(
            self.provider_async,
            "_last_provider_name",
            getattr(self.provider_async, "_provider_name", "unknown"),
        )

        record = TokenUsageRecord(
            actor_id="assistant",
            task_name=gen_request.purpose,
            model=gen_request.model,
            prompt_tokens=estimated_input_tokens,
            completion_tokens=estimated_output_tokens,
            total_tokens=estimated_input_tokens + estimated_output_tokens,
            input_chars=sum(
                len(str(m.get("content", ""))) for m in (gen_request.messages or [])
            )
            + len(gen_request.system_prompt),
            output_chars=len(raw_output),
            estimation_method="tiktoken" if estimated_output_tokens > 0 else "char_div4",
            retries_used=0,
            persona_name=persona_name,
            group_id="",
            provider_name=provider_name,
            breakdown_json="",
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
    ) -> Any:
        """记录 chat() 通道的完整 token 用量。"""
        from sirius_pulse.config import TokenUsageRecord
        from sirius_pulse.providers.base import get_last_generation_usage
        from sirius_pulse.token.utils import estimate_tokens

        output_chars = len(reply)
        estimated_output_tokens = estimate_tokens(reply) if reply else 0
        real_usage = get_last_generation_usage()
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
                pass

        return record
