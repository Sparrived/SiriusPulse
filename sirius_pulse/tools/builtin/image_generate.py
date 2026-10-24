"""内置的后台生图 TOOL。

生图接口通常要跑几十秒到几分钟，远超一次工具调用的执行超时。所以 ``run()`` 只把
请求写进持久化队列并立刻返回"已提交"；真正的生成与交付由 ``create_background_tasks``
注册的轮询任务完成：

1. 调 ``ctx.generate_image`` 生成图片，把字节落到本地文件；
2. 把产物路径或错误回调给 Agent，恢复内部工具回合；
3. Agent 自己决定保留、继续加工或显式发送，框架不自动发图或通知文案。

图片始终以本地绝对路径交付，不塞进模型上下文：大图会挤爆 token，模型也没法把字节
直接发给聊天平台。这就是为什么结果回调只拿到一个路径，而不是图片本身。
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

_CALLBACK_MAX_ATTEMPTS = 3
_REQUEST_SUMMARY_MAX_CHARS = 500
_ERROR_MAX_CHARS = 1_000

# 队列很长时只裁掉最早的投递终态任务；仍可执行/重试的任务不能丢。
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
        "完成后只把产物路径回调给你，不会自动发到聊天。"
        "由你决定保留、继续加工或用 group_file_exec 发送选中的图片；中间产物不必逐张通知。"
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
            "delivery_version": 3,
            "self_initiated": bool(invocation_context and invocation_context.self_initiated),
            "callback_state": "pending",
            "callback_attempts": 0,
            "notified": False,
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
                "这是给你的内部回调，不会自动发到聊天；是否发送、发哪些、何时发送由你决定。"
                "现在不要重复提交同一个请求，也不要假装已经画好。"
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
    """跑一次轮询：最多生成一张图、最多处理一个内部结果回调。

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
        # 下次进程启动的第一次 tick 会把它恢复成 failed 并回调 Agent。
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
    _update_job(data_store, job_id, status="running", started_at=_now_iso(), delivery_version=3)

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
    """只回调 Agent；繁忙会话让位，不阻塞其它会话的已完成任务。"""
    _reload_store(data_store)
    for job in _load_jobs(data_store):
        if (
            _delivery_finished(job)
            or job.get("status") not in _FINISHED_STATUSES
            or job.get("callback_state") == "running"
        ):
            continue
        group_id = str(job.get("group_id") or "").strip()
        if not group_id:
            _update_job(data_store, str(job.get("id", "")), callback_state="skipped")
            continue
        if await _deliver_notification(ctx, data_store, job, group_id):
            return


async def _deliver_notification(
    ctx: Any,
    data_store: Any,
    job: dict[str, Any],
    group_id: str,
) -> bool:
    """持久化回调边界；Agent 已执行工具后绝不整轮重放。"""
    job_id = str(job.get("id", ""))
    attempts = _int_value(job.get("callback_attempts"))
    if attempts >= _CALLBACK_MAX_ATTEMPTS:
        _update_job(data_store, job_id, callback_state="failed")
        return True
    succeeded = job.get("status") == "done"
    if job.get("image_sent"):
        hint = "旧版本已确认这张图片发送成功，不要再次发送；旧缓存通知不会自动补发。"
    elif _int_value(job.get("delivery_version")) < 3:
        hint = "旧版本产物的对外投递状态不能确认，不要盲目重发；先核对原任务和发送记录。"
    else:
        hint = "框架未向聊天发送产物或结果通知；由你决定如何继续，不必逐张发图。想让群里看到一句话就调用 send_midway_msg。"
    if _update_job(data_store, job_id, callback_state="running", callback_unsafe=False) is None:
        return True
    unsafe = False

    def before_tool() -> None:
        nonlocal unsafe
        # 先落盘再允许动作，磁盘写失败时不能继续执行有副作用的工具。
        if _update_job(data_store, job_id, callback_unsafe=True) is None:
            raise RuntimeError("生图任务丢失，停止回调工具执行")
        unsafe = True

    try:
        payload = await ctx.handle_background_tool_result(
            callback_id=f"image_generate:{job_id}",
            tool_name=TOOL_META["name"],
            outcome="success" if succeeded else "error",
            request_summary=str(job.get("prompt") or "")[:_REQUEST_SUMMARY_MAX_CHARS],
            deliver_hint=hint,
            artifact_path=str(job.get("image_path") or "") if succeeded else "",
            error="" if succeeded else str(job.get("error") or ""),
            group_id=group_id,
            user_id=str(job.get("user_id") or ""),
            user_name=str(job.get("user_name") or ""),
            adapter_type=str(job.get("adapter_type") or ""),
            caller_is_developer=bool(job.get("caller_is_developer", False)),
            self_initiated=bool(job.get("self_initiated", False)),
            artifact_delivery_blocked=bool(
                succeeded and (job.get("image_sent") or _int_value(job.get("delivery_version")) < 3)
            ),
            before_tool=before_tool,
        )
        if isinstance(payload, dict) and payload.get("status") == "deferred":
            _update_job(data_store, job_id, callback_state="pending")
            return False
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            raise ValueError("后台回调未确认处理完成")
    except asyncio.CancelledError:
        _update_job(data_store, job_id, callback_state="uncertain" if unsafe else "pending")
        raise
    except Exception as exc:
        attempts += 1
        state = (
            "uncertain" if unsafe else "failed" if attempts >= _CALLBACK_MAX_ATTEMPTS else "pending"
        )
        _update_job(
            data_store,
            job_id,
            callback_state=state,
            callback_attempts=attempts,
            callback_error=str(exc)[:_ERROR_MAX_CHARS],
        )
        logger.warning("生图内部回调失败 (%s, %s): %s", job_id, state, exc)
        return True
    # notified 仅表示 Agent 已处理结果，不是图片或文案已送达聊天。
    _update_job(
        data_store,
        job_id,
        callback_state="completed",
        notified=True,
        notified_at=_now_iso(),
        callback_result=str(payload.get("text") or ""),
    )
    return True


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
    """生成中断不重新扣费；回调中断按已落盘的副作用边界恢复。"""
    changed = False
    for job in jobs:
        if job.get("status") == "running":
            job.update(
                status="failed",
                error="进程重启，任务中断",
                finished_at=_now_iso(),
                notified=False,
                callback_state="pending",
                callback_attempts=0,
            )
            changed = True
            logger.warning("生图任务 %s 因进程重启被标记失败", job.get("id"))
        elif job.get("callback_state") == "running":
            job["callback_state"] = "uncertain" if job.get("callback_unsafe") else "pending"
            changed = True
            logger.warning("恢复生图回调 %s: %s", job.get("id"), job["callback_state"])
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


def _delivery_finished(job: dict[str, Any]) -> bool:
    """旧已通知任务不重开；新终态只描述内部回调，不暗示平台送达。"""
    return bool(job.get("notified")) or job.get("callback_state") in {
        "completed",
        "failed",
        "uncertain",
        "skipped",
    }


def _trim_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只裁掉最早的投递终态任务，尚可重试的任务不丢弃。"""
    overflow = len(jobs) - _MAX_RETAINED_JOBS
    if overflow <= 0:
        return jobs
    kept: list[dict[str, Any]] = []
    for job in jobs:
        if overflow > 0 and _delivery_finished(job):
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
