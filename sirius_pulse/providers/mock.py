from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field

from sirius_pulse.providers.base import (
    AsyncLLMProvider,
    GenerationRequest,
    GenerationResult,
    estimate_generation_request_input_tokens,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MockProvider(AsyncLLMProvider):
    """Deterministic provider for unit tests and local dry runs."""

    responses: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._queue = deque(self.responses)
        self.requests: list[GenerationRequest] = []

    async def generate_async(
        self, request: GenerationRequest, return_reasoning: bool = False
    ) -> GenerationResult:
        # 基础调用日志（INFO）
        msg_count = len(request.messages)
        estimated_input_tokens = estimate_generation_request_input_tokens(request)
        estimated_total_upper = estimated_input_tokens + max(0, int(request.max_tokens))

        logger.info(
            f"正准备向模拟的 {request.model} 请教问题，"
            f"手头有 {msg_count} 条消息想说，"
            f"温度调到 {request.temperature}，Token 上限设了 {request.max_tokens}，"
            f"预计要花 {estimated_input_tokens} 个 Token，"
            f"预计总 Token 上限 {estimated_total_upper}～"
        )
        debug_input = {
            "system_prompt": request.system_prompt,
            "messages": request.messages,
        }
        logger.debug(
            f"[模型调用详情] mock-{request.model} | 完整输入:\n"
            f"{json.dumps(debug_input, ensure_ascii=False, indent=2)}"
        )

        self.requests.append(request)
        if self._queue:
            response = self._queue.popleft()
            logger.info(f"模拟的 {request.model} 回复我了，写了 {len(response)} 个字～")
            logger.debug(f"[模型输出] mock-{request.model} | 响应内容:\n{response}")
            return GenerationResult(content=response)
        logger.warning(f"[模型调用] mock-{request.model} | 无配置响应")
        return GenerationResult(content="[mock] no configured response")
