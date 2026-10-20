"""内置的后台生图 TOOL。

生图接口通常要跑几十秒到几分钟，远超一次工具调用的执行超时。所以 ``run()`` 只把
请求写进持久化队列并立刻返回"已提交"；真正的生成与交付由 ``create_background_tasks``
注册的轮询任务完成：

1. 调 ``ctx.generate_image`` 生成图片，把字节落到本地文件；
2. 跑一个"后台任务完成"回合，提示词推荐模型用 ``group_file_exec`` 发图到原群；
3. 把该回合的正文用 ``ctx.dispatch_proactive_message`` 发到原群。

图片始终以本地绝对路径交付，不塞进模型上下文：大图会挤爆 token，模型也没法把字节
直接发给聊天平台。这就是为什么通知回合只拿到一个路径，而不是图片本身。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.extension_runtime import BackgroundTaskSpec
from sirius_pulse.tools.builtin._internal._bash_runtime import (
    expand_persona_path,
    home_root_path,
)
from sirius_pulse.tools.models import ToolInvocationContext

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 300
_DEFAULT_MAX_PENDING_JOBS = 3
_INTERVAL_SECONDS = 2.0

_MAX_REFERENCE_IMAGES = 4
_MAX_REFERENCE_BYTES = 20 * 1024 * 1024
_REFERENCE_DOWNLOAD_TIMEOUT_SECONDS = 30.0

_NOTIFY_MAX_ATTEMPTS = 3
# 通知回合失败后只剩"仅发图"这一条降级路径。它若一直失败（适配器掉线、群不可达），
# 不能无限重试：队首任务会永远卡在待通知，后面的任务全被饿死。
_FALLBACK_MAX_ATTEMPTS = 10
_REQUEST_SUMMARY_MAX_CHARS = 500
_ERROR_MAX_CHARS = 1_000

# 队列很长时只裁掉最早的"已通知"任务。未完成/待通知的一条都不能丢，否则那张图
# 就永远没人知道它画好了。
_MAX_RETAINED_JOBS = 100

_PENDING_STATUSES = {"pending", "running"}
_FINISHED_STATUSES = {"done", "failed"}

_config = ConfigBuilder()
_config.group("生图").add(
    "prompt",
    type="str",
    description=("要画的画面描述。写清主体、风格、构图和想要的情绪；" "参考图能把角色或场景固定下来。"),
    required=True,
)
_config.group("生图").add(
    "reference_images",
    type="list",
    description=(
        "可选参考图数组：每项是本地绝对路径或 http(s) 图片 URL。"
        "聊天里收到的图片/表情包会把本地缓存路径写进上下文"
        "（历史 XML 的 <image path=…> 或用户消息里的「本地图片路径」列表），"
        "把这些路径直接填进来就能以原图为参考，不要让用户重发。"
        "你自己的三视图/立绘路径见 system prompt 的「你自己的形象参考图」，"
        "同样可以直接填进来。最多 4 张，单张不超过 20MB。"
    ),
    default=[],
)

TOOL_META = {
    "name": "image_generate",
    "description": (
        "让后台画一张图：传入提示词，可选参考图（本地路径或图片 URL）。"
        "聊天里收到的图片/表情包会带出本地缓存路径，"
        "可直接作为 reference_images 复用原图，不用让用户重发；"
        "你自己的三视图/立绘路径见 system prompt 的「你自己的形象参考图」，"
        "要画自己时可以直接填进来。"
        "生图很慢（可能要几十秒到几分钟），调用后立刻返回“已提交”，不会阻塞当前回复。"
        "完成时会收到一条结果通知，里面给出图片的绝对路径；"
        "届时用 group_file_exec（action=“image”）把图发到当前聊天。"
        "不要重复提交同一个请求，也不要在完成通知之前假装已经画好。"
    ),
    "version": "1.0.0",
    "side_effect": "external_write",
    "tags": ["image", "generate", "background", "art"],
    "dependencies": ["httpx"],
    "parameters": _config.build(),
    "config": {
        "timeout_seconds": {
            "type": "int",
            "description": "单次生图的最长等待秒数，超时按失败处理。默认 300，范围 1 到 3600。",
            "default": _DEFAULT_TIMEOUT_SECONDS,
            "group": "限制",
        },
        "max_pending_jobs": {
            "type": "int",
            "description": "同一人格同时排队的生图任务上限，超过时工具直接拒绝新请求。默认 3。",
            "default": _DEFAULT_MAX_PENDING_JOBS,
            "group": "限制",
        },
    },
}


async def run(
    prompt: str,
    reference_images: list[str] | None = None,
    *,
    data_store: Any = None,
    chat_context: dict[str, Any] | None = None,
    invocation_context: ToolInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """提交一个后台生图任务并立刻返回。

    参考图会先物化到任务目录，队列里只记文件名；这样后台轮询、进程重启后都还能
    把图读回来，也避免把二进制塞进 JSON 存储。

    Args:
        prompt: 画面描述，不能为空。
        reference_images: 参考图来源（本地绝对路径或 http(s) URL），最多 4 张。
        data_store: 框架注入的持久化数据存储。
        chat_context: 框架注入的聊天上下文，用于记住结果要发回哪个群。
        invocation_context: 框架注入的调用者信息。

    Returns:
        工具结果字典；成功时只表示"已入队"，不代表图片已经生成。
    """
    if data_store is None:
        return {"success": False, "error": "image_generate 需要可持久化的数据存储"}
    reload_store = getattr(data_store, "reload", None)
    if callable(reload_store):
        reload_store()
    if data_store.get("_enabled", True) is False:
        return {"success": False, "error": "image_generate Tool 已被当前人格禁用"}

    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return {"success": False, "error": "prompt 不能为空，请描述想画什么"}

    try:
        references = _normalize_references(reference_images)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if len(references) > _MAX_REFERENCE_IMAGES:
        return {
            "success": False,
            "error": f"参考图最多 {_MAX_REFERENCE_IMAGES} 张，请减少后再提交",
        }

    max_pending = _config_int(
        data_store,
        "max_pending_jobs",
        _DEFAULT_MAX_PENDING_JOBS,
        minimum=1,
        maximum=20,
    )
    if _pending_job_count(_load_jobs(data_store)) >= max_pending:
        return {
            "success": False,
            "error": f"后台生图任务已排满（最多 {max_pending} 个），等前面的画完再提交",
            "summary": "生图队列已满",
        }

    context = dict(chat_context or {})
    if not context and invocation_context is not None:
        context = dict(invocation_context.chat_context)
    group_id = str(context.get("group_id") or context.get("chat_id") or "").strip()
    user_id = str(context.get("user_id") or "").strip()
    adapter_type = str(context.get("adapter_type") or "").strip()
    user_name = ""
    caller_is_developer = False
    if invocation_context is not None:
        user_id = invocation_context.caller_user_id or user_id
        user_name = invocation_context.caller_name
        caller_is_developer = invocation_context.caller_is_developer

    job_id = f"img_{uuid.uuid4().hex[:8]}"
    try:
        reference_names = await _materialize_references(data_store, job_id, references)
    except Exception as exc:
        _remove_reference_dir(data_store, job_id)
        logger.warning("参考图准备失败: %s", exc)
        return {"success": False, "error": f"参考图准备失败: {exc}"}

    # 参考图下载期间可能有别的请求也通过上面的容量检查，所以在真正入队前再查一次。
    # 重新读盘保证拿到的是最新列表；从这次读取到 append/save 之间没有 await。
    _reload_store(data_store)
    jobs = _load_jobs(data_store)
    if _pending_job_count(jobs) >= max_pending:
        _remove_reference_dir(data_store, job_id)
        return {
            "success": False,
            "error": f"后台生图任务已排满（最多 {max_pending} 个），等前面的画完再提交",
            "summary": "生图队列已满",
        }

    jobs.append(
        {
            "id": job_id,
            "status": "pending",
            "prompt": prompt_text,
            "references": reference_names,
            "group_id": group_id,
            "user_id": user_id,
            "user_name": user_name,
            "adapter_type": adapter_type,
            "caller_is_developer": caller_is_developer,
            "created_at": _now_iso(),
            "notified": False,
            "notify_attempts": 0,
        }
    )
    _save_jobs(data_store, jobs)
    logger.info("已提交后台生图任务: %s", job_id)

    return {
        "success": True,
        "summary": f"已提交后台生图任务（{job_id}），完成后会收到结果通知",
        "text_blocks": [
            (
                f"已提交后台生图任务（{job_id}）。图片生成需要一点时间，完成后会收到结果通知；"
                "通知里会给出图片的绝对路径，届时用 group_file_exec"
                '（action="image"，image_path=通知中的绝对路径）把图发到当前聊天，'
                "再用一两句话自然地说一声。现在不要重复提交同一个请求，也不要假装已经画好。"
            )
        ],
        "data": {"job_id": job_id, "status": "pending"},
        "internal_metadata": {"job_id": job_id},
    }


def create_background_tasks(ctx: Any) -> list[BackgroundTaskSpec]:
    """注册生图队列轮询任务。

    只有每个进程的第一次 tick 需要处理"上个进程留下的 running 任务"，所以恢复
    标记用闭包变量而不是写进 data_store：写进去的话，第二次进程启动时会读到
    True，崩溃残留的 running 就永远不会被标失败了。
    """
    first_tick = True

    async def _tick() -> None:
        nonlocal first_tick
        data_store = ctx.get_data_store("image_generate")
        await _process_once(ctx, data_store, recover_interrupted=first_tick)
        first_tick = False

    return [
        BackgroundTaskSpec(
            name="image_generate_jobs",
            interval_seconds=_INTERVAL_SECONDS,
            task_func=_tick,
        )
    ]


async def _process_once(
    ctx: Any,
    data_store: Any,
    *,
    recover_interrupted: bool = True,
) -> None:
    """跑一次轮询：最多生成一张图、最多发一条完成通知。

    单 tick 只做一张图，避免把人/账号的并发生图额度打满；处理时间再长也只是
    这个 tick 变长，``BackgroundTaskSpec.run_loop`` 会在它返回后再等下一个间隔。

    Args:
        ctx: 引擎注入的工具上下文，需提供生图与主动消息方法。
        data_store: 该 TOOL 的持久化数据存储。
        recover_interrupted: 是否为进程启动后的第一次 tick（用于恢复残留任务）。
    """
    _reload_store(data_store)
    if recover_interrupted:
        jobs = _load_jobs(data_store)
        if _recover_interrupted_jobs(jobs):
            # 这里到 save() 之间没有 await，整表写回是安全的。
            _save_jobs(data_store, jobs)
    try:
        await _generate_one(ctx, data_store)
        await _notify_one(ctx, data_store)
    except asyncio.CancelledError:
        # 关停时正常向上抛，让 run_loop 结束这个任务；running 状态留在磁盘上，
        # 下次进程启动的第一次 tick 会把它恢复成 failed 并补发通知。
        raise
    except Exception:
        logger.exception("生图后台任务本轮处理失败")


async def _generate_one(ctx: Any, data_store: Any) -> None:
    """取最早一个 pending 任务生成图片。"""
    _reload_store(data_store)
    job = _first_with_status(_load_jobs(data_store), "pending")
    if job is None:
        return

    job_id = str(job.get("id", ""))
    # 先落盘 running 再开始慢操作：进程若在此期间被杀，下次启动能凭这个状态认出
    # 任务被中断，而不是永远停在队列里。
    _update_job(data_store, job_id, status="running", started_at=_now_iso())

    timeout = _config_int(
        data_store,
        "timeout_seconds",
        _DEFAULT_TIMEOUT_SECONDS,
        minimum=1,
        maximum=3600,
    )
    try:
        image_path, mime_type = await _generate_image_for_job(ctx, data_store, job, timeout)
        _update_job(
            data_store,
            job_id,
            status="done",
            image_path=image_path,
            mime_type=mime_type,
            finished_at=_now_iso(),
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        _fail_job(data_store, job_id, f"生图超时（{timeout} 秒）")
    except Exception as exc:
        logger.warning("生图失败: %s | %s", job_id, exc)
        _fail_job(data_store, job_id, str(exc))


async def _generate_image_for_job(
    ctx: Any,
    data_store: Any,
    job: dict[str, Any],
    timeout: int,
) -> tuple[str, str]:
    """调引擎生图并把返回的字节写到本地文件，返回（绝对路径, 媒体类型）。"""
    references = _read_reference_images(data_store, job)
    # wait_for 与 ``timeout_seconds`` 用同一个值：契约里 ctx 自己也会超时，这里
    # 只是再兜一层，保证后台 tick 不会因为引擎实现漏了超时而永远挂住。
    payload = await asyncio.wait_for(
        ctx.generate_image(
            prompt=str(job.get("prompt", "")),
            reference_images=references,
            timeout_seconds=float(timeout),
        ),
        timeout=float(timeout),
    )
    if not isinstance(payload, dict):
        raise ValueError("生图接口返回格式不正确")
    image_bytes = payload.get("image_bytes")
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        raise ValueError("生图接口没有返回图片数据")

    mime_type = str(payload.get("mime_type") or "").strip()
    image_path = _write_generated_image(
        data_store,
        str(job.get("id", "")),
        bytes(image_bytes),
        mime_type,
    )
    return str(image_path), mime_type


async def _notify_one(ctx: Any, data_store: Any) -> None:
    """取最早一个已生成但未通知的任务，交付到原聊天。"""
    _reload_store(data_store)
    job = _first_unnotified_finished(_load_jobs(data_store))
    if job is None:
        return

    job_id = str(job.get("id", ""))
    group_id = str(job.get("group_id") or "").strip()
    if not group_id:
        # 没有会话上下文就没有可送达的地方（例如测试直接调用）。图片路径留在
        # 任务里供排查；标记已通知只是让队列能继续往前走，不代表有人看到过。
        _update_job(data_store, job_id, notified=True, notified_at=_now_iso())
        logger.info(
            "生图任务 %s 没有群上下文，跳过通知；图片: %s",
            job_id,
            job.get("image_path", ""),
        )
        return

    await _deliver_notification(ctx, data_store, job, group_id)


async def _deliver_notification(
    ctx: Any,
    data_store: Any,
    job: dict[str, Any],
    group_id: str,
) -> None:
    """跑通知回合并把模型正文投递到原群；失败时最多重试三次后降级发图。"""
    job_id = str(job.get("id", ""))
    attempts = _int_value(job.get("notify_attempts", 0))
    if attempts >= _NOTIFY_MAX_ATTEMPTS:
        await _deliver_image_only(ctx, data_store, job, group_id)
        return

    succeeded = job.get("status") == "done"
    image_path = str(job.get("image_path") or "")
    error = str(job.get("error") or "")
    if succeeded:
        deliver_hint = (
            '先调用 group_file_exec，参数 action="image"、'
            f'image_path="{image_path}"，把这张图发到当前聊天，'
            "再用你自己的口吻说一两句，不要重复解释整个请求。"
        )
    else:
        deliver_hint = "如实告诉对方这次没画成，不要假装成功。"

    try:
        payload = await ctx.generate_background_result_message(
            tool_name=TOOL_META["name"],
            outcome="success" if succeeded else "error",
            request_summary=str(job.get("prompt") or "")[:_REQUEST_SUMMARY_MAX_CHARS],
            deliver_hint=deliver_hint,
            artifact_path=image_path if succeeded else "",
            error="" if succeeded else error,
            group_id=group_id,
            user_id=str(job.get("user_id") or ""),
            user_name=str(job.get("user_name") or ""),
            adapter_type=str(job.get("adapter_type") or ""),
            caller_is_developer=bool(job.get("caller_is_developer", False)),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _update_job(data_store, job_id, notify_attempts=attempts + 1)
        logger.warning("生图完成通知回合失败（第 %d 次）: %s", attempts + 1, job_id, exc_info=True)
        return

    text = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
    image_sent = bool(payload.get("image_sent")) if isinstance(payload, dict) else False
    if succeeded and image_path and not image_sent:
        # 模型回了话却没按提示把图发出去（漏调工具、路径写错）。不能只把图留在
        # 磁盘上：这里由框架把文案和图片一起补发，用户至少能收到图。
        if await _dispatch(ctx, job, group_id, text=text, image_path=image_path):
            _update_job(data_store, job_id, notified=True, fallback=True, notified_at=_now_iso())
            logger.info("通知回合没有发出图片，已由框架补发: %s", job_id)
            return
        _update_job(data_store, job_id, notify_attempts=attempts + 1)
        logger.warning("生图结果补发失败（第 %d 次）: %s", attempts + 1, job_id)
        return

    if not text:
        # 模型这一回合没留下正文（可能主动选择沉默），没有可投递的内容。图片已经
        # 由回执回合发出（image_sent）或本就落盘，不能让任务永远卡在待通知状态。
        _update_job(data_store, job_id, notified=True, notified_at=_now_iso())
        return

    if await _dispatch(ctx, job, group_id, text=text):
        _update_job(data_store, job_id, notified=True, notified_at=_now_iso())
        return
    _update_job(data_store, job_id, notify_attempts=attempts + 1)
    logger.warning("生图结果通知投递失败（第 %d 次）: %s", attempts + 1, job_id)


async def _deliver_image_only(
    ctx: Any,
    data_store: Any,
    job: dict[str, Any],
    group_id: str,
) -> None:
    """通知回合连续失败后的降级：只把图片送到原群，不编造人格文案。"""
    job_id = str(job.get("id", ""))
    image_path = str(job.get("image_path") or "")
    if not image_path:
        # 生成失败的任务没有图片可发，降级也就没有意义；记一条日志收尾，避免每
        # 个 tick 都重试同一条通知。
        _update_job(data_store, job_id, notified=True, notified_at=_now_iso())
        logger.warning("生图任务 %s 生成失败且无图片，放弃完成通知: %s", job_id, job.get("error", ""))
        return
    if await _dispatch(ctx, job, group_id, text="", image_path=image_path):
        _update_job(data_store, job_id, notified=True, fallback=True, notified_at=_now_iso())
        logger.info("生图结果已降级为仅发图: %s", job_id)
        return
    attempts = _int_value(job.get("notify_attempts", 0)) + 1
    _update_job(data_store, job_id, notify_attempts=attempts)
    if attempts >= _FALLBACK_MAX_ATTEMPTS:
        # 队首任务一直发不出去会饿死后面所有已完成任务的通知；写一条错误日志后
        # 放弃它，让队列能继续往前走。
        _update_job(data_store, job_id, notified=True, fallback_failed=True, notified_at=_now_iso())
        logger.error("生图结果降级发图连续失败 %d 次，放弃通知: %s", attempts, job_id)
        return
    logger.warning("生图结果降级发图仍失败（第 %d 次）: %s", attempts, job_id)


async def _dispatch(
    ctx: Any,
    job: dict[str, Any],
    group_id: str,
    *,
    text: str,
    image_path: str = "",
) -> bool:
    """通过统一主动消息管线投递；异常按投递失败处理，不打断后台循环。"""
    try:
        accepted = await ctx.dispatch_proactive_message(
            group_id=group_id,
            text=text,
            adapter_type=str(job.get("adapter_type") or ""),
            event_id=f"image_generate:{job.get('id', '')}",
            image_path=image_path,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("生图结果投递异常: %s", job.get("id"), exc_info=True)
        return False
    return bool(accepted)


def _normalize_references(raw: Any) -> list[str]:
    """把模型给的参考图参数整理成字符串列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        # 有的模型会把数组序列化成 JSON 字符串传进来，能解就解。
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        raw = parsed if isinstance(parsed, list) else [text]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("reference_images 必须是字符串数组")
    return [str(item).strip() for item in raw if str(item or "").strip()]


async def _materialize_references(
    data_store: Any,
    job_id: str,
    references: list[str],
) -> list[str]:
    """把参考图下载/读取到任务目录，返回文件名列表。"""
    if not references:
        return []
    reference_dir = _artifact_dir(data_store) / "references" / job_id
    reference_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for index, source in enumerate(references, start=1):
        data = await _read_reference_bytes(source, data_store)
        name = f"{index:02d}_{_reference_name(source)}"
        (reference_dir / name).write_bytes(data)
        names.append(name)
    return names


async def _read_reference_bytes(source: str, data_store: Any) -> bytes:
    """读取一张参考图的字节：URL 走下载，本地路径直接读，统一卡 20MB。"""
    if source.lower().startswith(("http://", "https://")):
        return await _download_reference(source)

    path = expand_persona_path(source, home=home_root_path(data_store))
    if not path.is_file():
        raise ValueError(f"参考图不存在: {source}")
    size = path.stat().st_size
    if size > _MAX_REFERENCE_BYTES:
        raise ValueError(
            f"参考图过大（{size} 字节），单张最多 {_MAX_REFERENCE_BYTES // 1024 // 1024}MB: {source}"
        )
    return path.read_bytes()


async def _download_reference(url: str) -> bytes:
    """下载参考图，边读边计数，避免被超大文件拖垮内存。"""
    async with httpx.AsyncClient(
        timeout=_REFERENCE_DOWNLOAD_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > _MAX_REFERENCE_BYTES:
                raise ValueError(f"参考图过大，单张最多 {_MAX_REFERENCE_BYTES // 1024 // 1024}MB")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > _MAX_REFERENCE_BYTES:
                    raise ValueError(f"参考图过大，单张最多 {_MAX_REFERENCE_BYTES // 1024 // 1024}MB")
                chunks.append(chunk)
            data = b"".join(chunks)
            if not data:
                raise ValueError(f"参考图内容为空: {url}")
            return data


def _reference_name(source: str) -> str:
    """给参考图取一个安全的文件名，保留扩展名方便事后辨认。"""
    if source.lower().startswith(("http://", "https://")):
        raw_name = Path(urlparse(source).path).name
    else:
        raw_name = Path(source).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name or "")[:80]
    return name or "reference"


def _remove_reference_dir(data_store: Any, job_id: str) -> None:
    """入队失败时清掉已落盘的参考图，不留孤儿目录。"""
    reference_dir = _artifact_dir(data_store) / "references" / job_id
    try:
        for path in reference_dir.glob("*"):
            path.unlink(missing_ok=True)
        reference_dir.rmdir()
    except OSError:
        logger.debug("清理参考图目录失败: %s", reference_dir)


def _read_reference_images(data_store: Any, job: dict[str, Any]) -> list[tuple[str, bytes]]:
    """从任务目录读回参考图；个别文件丢了只跳过，不让整张图生成失败。"""
    names = [str(name) for name in job.get("references", []) if str(name)]
    if not names:
        return []
    reference_dir = _artifact_dir(data_store) / "references" / str(job.get("id", ""))
    images: list[tuple[str, bytes]] = []
    for name in names:
        path = reference_dir / name
        try:
            images.append((name, path.read_bytes()))
        except OSError as exc:
            logger.warning("参考图读取失败，已跳过: %s | %s", path, exc)
    return images


def _write_generated_image(
    data_store: Any,
    job_id: str,
    image_bytes: bytes,
    mime_type: str,
) -> Path:
    """把生成结果写成图片文件，返回绝对路径。"""
    image_dir = _artifact_dir(data_store) / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    path = image_dir / f"{job_id}.{_extension_for_mime(mime_type)}"
    path.write_bytes(image_bytes)
    return path.resolve()


def _extension_for_mime(mime_type: str) -> str:
    normalized = mime_type.lower().strip()
    if "jpeg" in normalized or "jpg" in normalized:
        return "jpg"
    if "webp" in normalized:
        return "webp"
    if "gif" in normalized:
        return "gif"
    if "png" in normalized:
        return "png"
    return "png"


def _recover_interrupted_jobs(jobs: list[dict[str, Any]]) -> bool:
    """把上个进程留下的 running 任务标成失败，并重新纳入待通知。"""
    changed = False
    for job in jobs:
        if job.get("status") != "running":
            continue
        job["status"] = "failed"
        job["error"] = "进程重启，任务中断"
        job["finished_at"] = _now_iso()
        job["notified"] = False
        job["notify_attempts"] = 0
        changed = True
        logger.warning("生图任务 %s 因进程重启被标记失败", job.get("id"))
    return changed


def _fail_job(data_store: Any, job_id: str, error: str) -> None:
    _update_job(
        data_store,
        job_id,
        status="failed",
        error=str(error or "生图失败")[:_ERROR_MAX_CHARS],
        finished_at=_now_iso(),
    )


def _load_jobs(data_store: Any) -> list[dict[str, Any]]:
    raw = data_store.get("jobs", [])
    if not isinstance(raw, list):
        return []
    return [job for job in raw if isinstance(job, dict)]


def _save_jobs(data_store: Any, jobs: list[dict[str, Any]]) -> None:
    data_store.set("jobs", _trim_jobs(jobs))
    data_store.save()


def _reload_store(data_store: Any) -> None:
    reload_store = getattr(data_store, "reload", None)
    if callable(reload_store):
        reload_store()


def _update_job(data_store: Any, job_id: str, **fields: Any) -> dict[str, Any] | None:
    """只改某一条任务：先把磁盘上的最新列表读回来再写。

    worker 的慢操作动辄几十秒，期间 ``run()`` 可能往队列里追加新任务。如果拿着
    开始处理时的旧列表整表写回，那些新任务会被静默覆盖——模型已经收到"已提交"，
    却永远等不到结果。任务本身只由这个 worker 改，所以按 id 合并足够。
    """
    _reload_store(data_store)
    jobs = _load_jobs(data_store)
    for job in jobs:
        if str(job.get("id", "")) == job_id:
            job.update(fields)
            _save_jobs(data_store, jobs)
            return job
    logger.warning("生图任务已不在存储中: %s", job_id)
    return None


def _trim_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """任务很多时按顺序丢掉最早的已通知任务，未通知的一条都不删。"""
    overflow = len(jobs) - _MAX_RETAINED_JOBS
    if overflow <= 0:
        return jobs
    kept: list[dict[str, Any]] = []
    for job in jobs:
        if overflow > 0 and job.get("notified"):
            overflow -= 1
            continue
        kept.append(job)
    return kept


def _pending_job_count(jobs: list[dict[str, Any]]) -> int:
    return sum(1 for job in jobs if job.get("status") in _PENDING_STATUSES)


def _first_with_status(jobs: list[dict[str, Any]], status: str) -> dict[str, Any] | None:
    for job in jobs:
        if job.get("status") == status:
            return job
    return None


def _first_unnotified_finished(jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for job in jobs:
        if job.get("notified") or job.get("status") not in _FINISHED_STATUSES:
            continue
        return job
    return None


def _artifact_dir(data_store: Any) -> Path:
    artifact_dir = getattr(data_store, "artifact_dir", None)
    if isinstance(artifact_dir, Path):
        return artifact_dir
    if artifact_dir:
        return Path(str(artifact_dir))
    return Path.home() / ".sirius_pulse" / "tool_artifacts" / "image_generate"


def _config_int(
    data_store: Any,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(data_store.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
