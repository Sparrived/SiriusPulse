"""直接调模型：生成一段文本或一张图，不经过工具循环。

实现 :class:`sirius_pulse.tools.engine_context.GenerationAccess`。
"""

from __future__ import annotations

from typing import Any


class GenerationMixin:
    """直接调模型：生成一段文本或一张图，不经过工具循环。"""

    _engine: Any

    async def generate_text(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        group_id: str,
        task_name: str = "passive_tool",
        post_process: bool = False,
        **kwargs: Any,
    ) -> str:
        return await self._engine.brain.generate_text(
            system_prompt,
            messages,
            group_id,
            task_name=task_name,
            post_process=post_process,
        )

    async def generate_image(
        self,
        *,
        prompt: str,
        reference_images: list[tuple[str, bytes]] | None = None,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """让 AMKR 生成一张图，返回原始字节与媒体类型。

        任务名固定为 ``image_generate``（注册表里有它，AMKR 按任务定义换成真实
        生图模型）；参考图同时决定走编辑端点还是文生图端点。
        """
        from sirius_pulse.providers.base import ImageGenerationRequest

        request = ImageGenerationRequest(
            model="image_generate",
            prompt=prompt,
            reference_images=list(reference_images or []),
            timeout_seconds=timeout_seconds,
        )
        result = await self._engine.brain.generate_image_async(request)
        return {"image_bytes": result.image_bytes, "mime_type": result.mime_type}
