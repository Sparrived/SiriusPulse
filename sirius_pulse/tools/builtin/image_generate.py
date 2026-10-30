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
from sirius_pulse.visual import policies as visual_policies
from sirius_pulse.visual.errors import VisualError
from sirius_pulse.visual.images import sha256_bytes

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
_FINISHED_STATUSES = {"done", "failed", "cancelled"}

_config = ConfigBuilder()
_config.group("生图").add(
    "prompt",
    type="str",
    description=("要画的画面描述。写清主体、风格、构图和想要的情绪；" "参考图能把角色或场景固定下来。"),
    required=True,
)
_config.group("生图").add(
    "subjects",
    type="list",
    description=(
        "人物生图的主体列表（需要画出具体人物时使用）。每项是主体引用对象："
        '{"ref": "persona"} 画你自己；{"ref": "requester"} 画当前请求者；'
        '{"ref": "person_id", "value": "<UUID>"} 画已登记人物；也可加 '
        '"revision"/"profile_id"/"outfit"。提供 subjects 时由框架绑定人物正式形象、'
        "图序及素材状态；可见共享素材不需逐人物许可，reference_images 只作为额外参考。"
    ),
    default=[],
)
_config.group("生图").add(
    "source_ref",
    type="str",
    description="本次创作对应的当轮用户指令来源（框架注入的 evidence_ref），用于核对请求作者和 requester，不是图中人物的同意凭证。",
)
_config.group("生图").add(
    "reference_images",
    type="list",
    description=(
        "可选参考图数组：每项是本地绝对路径或 http(s) 图片 URL。"
        "聊天里收到的图片/表情包会把本地缓存路径写进上下文"
        "（历史 XML 的 <image path=…> 或用户消息里的「本地图片路径」列表），"
        "把这些路径直接填进来就能以原图为参考，不要让用户重发。"
        "画具体人物时优先用 subjects 绑定其正式形象，这里的额外参考最多 4 张，"
        "单张不超过 20MB。"
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
        "要画具体人物时用 subjects 绑定其正式形象，不要用历史里的图片路径猜。"
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
    subjects: Any = None,
    source_ref: str = "",
    *,
    data_store: Any = None,
    chat_context: dict[str, Any] | None = None,
    invocation_context: ToolInvocationContext | None = None,
    engine_context: Any = None,
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
            "code": "reference_limit_exceeded",
            "error": f"参考图最多 {_MAX_REFERENCE_IMAGES} 张，请减少后再提交",
            "summary": f"参考图超过 {_MAX_REFERENCE_IMAGES} 张上限",
            "data": {"code": "reference_limit_exceeded", "limit": _MAX_REFERENCE_IMAGES},
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

    try:
        subject_list = _normalize_subjects(subjects)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if subject_list:
        return await _submit_person_job(
            prompt_text=prompt_text,
            subjects=subject_list,
            source_ref=str(source_ref or ""),
            extra_references=references,
            data_store=data_store,
            group_id=group_id,
            user_id=user_id,
            user_name=user_name,
            adapter_type=adapter_type,
            caller_is_developer=caller_is_developer,
            self_initiated=bool(invocation_context and invocation_context.self_initiated),
            engine_context=engine_context,
            invocation_context=invocation_context,
            max_pending=max_pending,
        )

    managed_refs, denial = await _guard_legacy_references(
        engine_context, invocation_context, references
    )
    if denial is not None:
        return denial
    if managed_refs:
        if invocation_context is not None and len(invocation_context.evidence_refs) > 1:
            return {
                "success": False,
                "code": "source_ambiguous",
                "error": "多作者回合的受管参考图请用 subjects 和 source_ref 明确请求者",
            }
        return await _submit_person_job(
            prompt_text=prompt_text,
            subjects=[],
            source_ref=str(source_ref or ""),
            extra_references=references,
            data_store=data_store,
            group_id=group_id,
            user_id=user_id,
            user_name=user_name,
            adapter_type=adapter_type,
            caller_is_developer=caller_is_developer,
            self_initiated=bool(invocation_context and invocation_context.self_initiated),
            engine_context=engine_context,
            invocation_context=invocation_context,
            max_pending=max_pending,
            managed_only=True,
        )

    job_id = f"img_{uuid.uuid4().hex[:8]}"
    try:
        reference_names = await _materialize_references(data_store, job_id, references)
    except Exception as exc:
        _remove_reference_dir(data_store, job_id)
        logger.warning("参考图准备失败: %s", exc)
        return {
            "success": False,
            "code": "asset_invalid",
            "error": f"参考图准备失败: {exc}",
            "summary": "参考图准备失败：文件缺失、格式不支持或超过限制",
            "data": {"code": "asset_invalid"},
        }

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
            "visual_managed_refs": managed_refs,
        }
    )
    _save_jobs(data_store, jobs)
    logger.info("已提交后台生图任务: %s", job_id)

    warning = (
        "" if not str(source_ref or "").strip() else "（本次未提供 subjects：没有人物形象绑定，无法保证人物一致性与授权边界。）"
    )
    return {
        "success": True,
        "summary": f"已提交后台生图任务（{job_id}），完成后会收到结果通知",
        "text_blocks": [
            (
                f"已提交后台生图任务（{job_id}）。图片生成需要一点时间，完成后会收到结果通知；"
                "这是给你的内部回调，不会自动发到聊天；是否发送、发哪些、何时发送由你决定。"
                "现在不要重复提交同一个请求，也不要假装已经画好。" + warning
            )
        ],
        "data": {"job_id": job_id, "status": "pending"},
        "internal_metadata": {"job_id": job_id},
    }


def _normalize_subjects(raw: Any) -> list[dict[str, Any]]:
    """把 subjects 参数整理为对象列表；字符串按 JSON 或名称处理。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        parsed: Any = None
        if text.startswith(("{", "[")):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
        raw = parsed if parsed is not None else [{"ref": "name", "value": text}]
    if isinstance(raw, dict):
        return [dict(raw)]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("subjects 必须是主体引用数组")
    result: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            result.append(dict(item))
        elif isinstance(item, str) and item.strip():
            result.append({"ref": "name", "value": item.strip()})
    return result


def _visual_context(engine_context: Any, invocation_context: Any) -> tuple[Any, Any]:
    """取视觉服务与逐调用权限；旧引擎没有这些方法时返回 (None, None)。"""
    service = None
    getter = getattr(engine_context, "get_visual_service", None)
    if callable(getter):
        service = getter()
    access = None
    getter = getattr(engine_context, "get_person_access", None)
    if callable(getter) and invocation_context is not None:
        access = getter(invocation_context)
    return service, access


async def _submit_person_job(
    *,
    prompt_text: str,
    subjects: list[dict[str, Any]],
    source_ref: str,
    extra_references: list[str],
    data_store: Any,
    group_id: str,
    user_id: str,
    user_name: str,
    adapter_type: str,
    caller_is_developer: bool,
    self_initiated: bool,
    engine_context: Any,
    invocation_context: Any,
    max_pending: int,
    managed_only: bool = False,
) -> dict[str, Any]:
    """人物分支：绑定正式形象 → 本地输入快照 → 入现有后台队列。"""
    service, access = _visual_context(engine_context, invocation_context)
    if service is None or access is None:
        return {
            "success": False,
            "error": ("人物生图需要视觉资产服务与逐调用权限上下文；当前引擎未接入，" "已拒绝提交，也不会用未绑定人物的路径猜着画"),
            "summary": "人物生图服务未接入",
        }
    purpose = "reference_image" if managed_only else "person_image"
    try:
        plan = await service.compile_generation(
            access=access,
            subjects=subjects,
            source_ref=source_ref,
            prompt=prompt_text,
            extra_references=extra_references,
            purpose=purpose,
            group_id=group_id,
        )
    except VisualError as exc:
        return {
            "success": False,
            "error": exc.message,
            "code": exc.code,
            "summary": f"人物生图未提交：{exc.message}",
            "data": {"code": exc.code, **(exc.data or {})},
        }
    if _pending_job_count(_load_jobs(data_store)) >= max_pending:
        return {
            "success": False,
            "error": f"后台生图任务已排满（最多 {max_pending} 个），等前面的画完再提交",
            "summary": "生图队列已满",
        }
    job_id = f"img_{uuid.uuid4().hex[:8]}"
    if not visual_policies.consume_deployment(service.store):
        return {
            "success": False,
            "error": "部署方人物生图额度已用尽",
            "code": "quota_exceeded",
            "summary": "人物生图未提交：额度不足",
        }
    try:
        await service.begin_generation(
            job_id,
            plan,
            access=access,
            source_ref=source_ref,
            purpose=purpose,
            group_id=group_id,
        )
    except VisualError as exc:
        visual_policies.refund_deployment(service.store)
        return {
            "success": False,
            "error": exc.message,
            "code": exc.code,
            "summary": f"人物生图未提交：{exc.message}",
        }
    try:
        names = _materialize_plan_images(data_store, job_id, plan["images"])
        _reload_store(data_store)
        jobs = _load_jobs(data_store)
        if _pending_job_count(jobs) >= max_pending:
            raise ValueError(f"后台生图任务已排满（最多 {max_pending} 个）")
        jobs.append(
            {
                "id": job_id,
                "status": "pending",
                "prompt": str(plan["prompt"]),
                "references": names,
                "group_id": group_id,
                "user_id": user_id,
                "user_name": user_name,
                "adapter_type": adapter_type,
                "caller_is_developer": caller_is_developer,
                "created_at": _now_iso(),
                "delivery_version": 3,
                "self_initiated": self_initiated,
                "callback_state": "pending",
                "callback_attempts": 0,
                "notified": False,
                "visual": plan["manifest"],
                "strict_references": True,
                "input_hash": str(plan.get("input_hash") or ""),
                "recipe": str(plan.get("recipe") or ""),
                "recipe_version": str(plan.get("recipe_version") or ""),
                "reference_sha256": [str(image.get("sha256") or "") for image in plan["images"]],
            }
        )
        _save_jobs(data_store, jobs)
        await service.mark_generation_enqueued(job_id)
    except Exception as exc:  # noqa: BLE001
        _remove_reference_dir(data_store, job_id)
        await service.cancel_generation(job_id, "queue_unavailable")
        logger.warning("人物生图入队失败: %s", exc)
        return {"success": False, "error": f"人物生图入队失败: {exc}", "summary": "入队失败"}
    subject_names = "、".join(
        str(item.get("display_name") or item.get("person_id") or "") for item in plan["subjects"]
    )
    preview_note = "本次使用未正式启用的修订作为预览参考；结果仍只是候选。" if plan.get("preview") else ""
    return {
        "success": True,
        "summary": f"已提交人物生图任务（{job_id}），完成后会收到结果通知",
        "text_blocks": [
            (
                f"已提交人物生图任务（{job_id}），已绑定人物：{subject_names or '（未命名）'}。"
                "生成需要一点时间，完成后只回调产物路径，不会自动发到聊天；"
                "是否发送由你决定。不要重复提交，也不要提前声称已经画好。" + preview_note
            )
        ],
        "data": {
            "job_id": job_id,
            "status": "pending",
            "subjects": plan["subjects"],
            "dropped_optional": plan.get("dropped_optional", 0),
        },
        "internal_metadata": {"job_id": job_id},
    }


def _materialize_plan_images(
    data_store: Any, job_id: str, images: list[dict[str, Any]]
) -> list[str]:
    """把编译好的参考图按顺序写入任务目录，队列里只记文件名。"""
    reference_dir = _artifact_dir(data_store) / "references" / job_id
    reference_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for index, image in enumerate(images, start=1):
        name = _safe_reference_name(str(image.get("name") or f"{index:02d}.png"))
        data = image.get("bytes")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("参考图快照为空")
        (reference_dir / name).write_bytes(bytes(data))
        names.append(name)
    return names


def _safe_reference_name(raw: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(raw).name)[:80]
    name = name.lstrip(".") or "reference"
    return name


async def _guard_legacy_references(
    engine_context: Any,
    invocation_context: Any,
    references: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """旧路径传受管素材时按实际能力校验；普通临时参考保持原策略。"""
    if not references:
        return [], None
    service, access = _visual_context(engine_context, invocation_context)
    if service is None or access is None:
        return [], None
    managed: list[dict[str, Any]] = []
    for source in references:
        try:
            guard = await service.guard_path(source, access, "upload_generate")
        except VisualError as exc:
            return [], {
                "success": False,
                "error": exc.message,
                "code": exc.code,
                "summary": "参考图不可用或超出当前可见范围",
            }
        if not guard.get("managed"):
            continue
        if not guard.get("allowed"):
            return [], {
                "success": False,
                "error": "参考图属于受管素材，但其来源状态或当前会话范围不允许使用",
                "code": str(guard.get("code") or "consent_required"),
                "summary": "参考图不可用或超出当前可见范围",
                "data": {"code": str(guard.get("code") or "consent_required")},
            }
        managed.append(
            {
                "asset_id": str(guard.get("asset_id") or ""),
                "job_id": str(guard.get("job_id") or ""),
            }
        )
    return managed, None


class VisualJobQueue:
    """把领域服务的派生任务接进现有持久化生图队列（不是第二个队列）。"""

    def __init__(self, data_store: Any) -> None:
        self.data_store = data_store

    def enqueue_visual(
        self,
        *,
        job_id: str,
        prompt: str,
        images: list[dict[str, Any]],
        meta: dict[str, Any],
    ) -> None:
        data_store = self.data_store
        _reload_store(data_store)
        max_pending = _config_int(
            data_store,
            "max_pending_jobs",
            _DEFAULT_MAX_PENDING_JOBS,
            minimum=1,
            maximum=20,
        )
        jobs = _load_jobs(data_store)
        if any(str(job.get("id", "")) == job_id for job in jobs):
            return
        if _pending_job_count(jobs) >= max_pending:
            raise ValueError(f"后台生图任务已排满（最多 {max_pending} 个）")
        try:
            names = _materialize_plan_images(data_store, job_id, images)
            _reload_store(data_store)
            jobs = _load_jobs(data_store)
            if _pending_job_count(jobs) >= max_pending:
                raise ValueError(f"后台生图任务已排满（最多 {max_pending} 个）")
            visual = meta.get("visual") if isinstance(meta.get("visual"), dict) else {}
            jobs.append(
                {
                    "id": job_id,
                    "status": "pending",
                    "prompt": str(prompt),
                    "references": names,
                    "group_id": str(meta.get("group_id") or ""),
                    "user_id": str(meta.get("actor_id") or ""),
                    "user_name": "",
                    "adapter_type": str(meta.get("adapter_type") or ""),
                    "caller_is_developer": bool(meta.get("caller_is_developer", False)),
                    "created_at": _now_iso(),
                    "delivery_version": 3,
                    "self_initiated": False,
                    "callback_state": "pending",
                    "callback_attempts": 0,
                    "notified": False,
                    "visual": visual,
                    "strict_references": True,
                    "visual_auto": bool(meta.get("auto")),
                    "input_hash": str(visual.get("input_hash") or ""),
                    "recipe": str(visual.get("recipe") or ""),
                    "recipe_version": str(visual.get("recipe_version") or ""),
                }
            )
            _save_jobs(data_store, jobs)
        except Exception:
            _remove_reference_dir(data_store, job_id)
            raise
        logger.info("已提交视觉派生任务: %s", job_id)


def build_visual_queue(data_store: Any) -> VisualJobQueue | None:
    """供 ``visual_asset_manage`` 与服务后台控制器使用。"""
    if data_store is None:
        return None
    return VisualJobQueue(data_store)


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
        changed = _recover_interrupted_jobs(jobs)
        changed = (await _recover_visual_jobs(ctx, data_store, jobs)) or changed
        if changed:
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
    visual = _is_visual_job(job)
    service = _visual_service_of(ctx)
    if (
        not visual
        and service is not None
        and await _legacy_references_are_managed(
            service, data_store, job_id, list(job.get("references") or [])
        )
    ):
        _update_job(
            data_store,
            job_id,
            status="cancelled",
            error_code="legacy_managed_unverifiable",
            error="受管参考图缺少可核验的持久来源绑定，已取消",
            isolated=True,
            artifact_suppressed=True,
            finished_at=_now_iso(),
        )
        return
    if visual and service is None:
        # 受管任务不能绕过权限复查直接生图。
        _fail_job(data_store, job_id, "视觉资产服务未接入，无法校验授权，任务已拒绝")
        return

    timeout = _config_int(
        data_store,
        "timeout_seconds",
        _DEFAULT_TIMEOUT_SECONDS,
        minimum=1,
        maximum=3600,
    )
    try:
        if visual:
            preflight = await service.preflight_generation(job_id)
            if not preflight.get("allow"):
                code = str(preflight.get("code") or "cancelled_by_revocation")
                _update_job(
                    data_store,
                    job_id,
                    status="cancelled",
                    error=code,
                    error_code=code,
                    finished_at=_now_iso(),
                    isolated=True,
                    artifact_suppressed=True,
                    visual_result={"isolated": True, "code": code},
                )
                return
        image_path, mime_type, visual_result = await _generate_image_for_job(
            ctx,
            data_store,
            job,
            timeout,
            service=service if visual else None,
            visual=visual,
        )
        fields: dict[str, Any] = {
            "status": "done",
            "image_path": image_path,
            "mime_type": mime_type,
            "finished_at": _now_iso(),
        }
        if visual_result is not None:
            isolated = bool(visual_result.get("isolated"))
            fields.update(
                status="done",
                isolated=isolated,
                error_code=str(visual_result.get("code") or ""),
                artifact_suppressed=isolated,
                visual_result=visual_result,
            )
            if isolated:
                fields["error"] = "授权已撤回或结果无法核实，图片已隔离，不交付也不启用"
        _update_job(data_store, job_id, **fields)
    except asyncio.CancelledError:
        raise
    except _VisualJobCancelled as exc:
        _update_job(
            data_store,
            job_id,
            status="cancelled",
            error=exc.code,
            error_code=exc.code,
            finished_at=_now_iso(),
            isolated=True,
            artifact_suppressed=True,
        )
    except asyncio.TimeoutError:
        if visual:
            # 是否算“结果未知”由持久化的外部提交边界决定，不能由调用点猜。
            await service.fail_generation(job_id, "generation_timeout")
        _fail_job(data_store, job_id, f"生图超时（{timeout} 秒）")
    except Exception as exc:
        logger.warning("生图失败: %s | %s", job_id, exc)
        if visual:
            await service.fail_generation(job_id, "generation_failed")
        _fail_job(data_store, job_id, str(exc))


class _VisualJobCancelled(Exception):
    """本地许可复查拒绝执行；不代表上游已经发生调用。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _generate_image_for_job(
    ctx: Any,
    data_store: Any,
    job: dict[str, Any],
    timeout: int,
    *,
    service: Any = None,
    visual: bool = False,
) -> tuple[str, str, dict[str, Any] | None]:
    """调引擎生图并把返回的字节写到本地文件，返回（绝对路径, 媒体类型, 视觉结果）。"""
    job_id = str(job.get("id", ""))
    # 参考图缺失必须在领取许可与计费之前拒绝，不能悄悄少一张继续画。
    references = _read_reference_images(data_store, job, strict=visual)
    if visual:
        permit = await service.acquire_generation_permit(job_id)
        if not permit.get("allow"):
            raise _VisualJobCancelled(str(permit.get("code") or "cancelled_by_revocation"))
        started = await service.mark_generation_submit_started(job_id)
        if not started.get("allow"):
            raise _VisualJobCancelled(str(started.get("code") or "cancelled_by_revocation"))
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
    if visual:
        current = service.store.job(job_id)
        if current and (current.get("manifest") or {}).get("deleted"):
            raise _VisualJobCancelled("cancelled_by_revocation")
    image_path = _write_generated_image(
        data_store,
        job_id,
        bytes(image_bytes),
        mime_type,
    )
    visual_result: dict[str, Any] | None = None
    if visual:
        visual_result = await service.complete_generation(
            job_id,
            image_bytes=bytes(image_bytes),
            mime_type=mime_type,
            artifact_path=str(image_path),
        )
    return str(image_path), mime_type, visual_result


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
    suppressed = bool(job.get("artifact_suppressed") or job.get("isolated"))
    succeeded = job.get("status") == "done" and not suppressed
    cancelled = job.get("status") == "cancelled"
    if job.get("image_sent"):
        hint = "旧版本已确认这张图片发送成功，不要再次发送；旧缓存通知不会自动补发。"
    elif suppressed:
        hint = "这张图因撤权、停用或归属无法核实已被隔离：不要发送，也不要从任务目录、" "缓存或历史路径读取重发；如用户重新授权，需要重新提交生成。"
    elif cancelled:
        hint = "任务已取消，没有可交付的图片；不要重试同一个付费请求，也不要假装已经画好。"
    elif _int_value(job.get("delivery_version")) < 3:
        hint = "旧版本产物的对外投递状态不能确认，不要盲目重发；先核对原任务和发送记录。"
    else:
        hint = "框架未向聊天发送产物或结果通知；由你决定如何继续，不必逐张发图。想让群里看到一句话就调用 send_midway_msg。"
    current = _update_job(data_store, job_id, callback_state="running", callback_unsafe=False)
    if current is None or current.get("deleted"):
        return True
    unsafe = False

    def before_tool() -> None:
        nonlocal unsafe
        # 先落盘再允许动作，磁盘写失败时不能继续执行有副作用的工具。
        current = _update_job(data_store, job_id, callback_unsafe=True)
        if current is None or current.get("deleted"):
            raise RuntimeError("生图任务丢失或已删除，停止回调工具执行")
        unsafe = True

    try:
        payload = await ctx.handle_background_tool_result(
            callback_id=f"image_generate:{job_id}",
            tool_name=TOOL_META["name"],
            outcome="success" if succeeded else "error",
            request_summary=str(job.get("prompt") or "")[:_REQUEST_SUMMARY_MAX_CHARS],
            deliver_hint=hint,
            artifact_path=str(job.get("image_path") or "") if succeeded else "",
            error="" if succeeded else str(job.get("error") or ("任务已取消" if cancelled else "")),
            group_id=group_id,
            user_id=str(job.get("user_id") or ""),
            user_name=str(job.get("user_name") or ""),
            adapter_type=str(job.get("adapter_type") or ""),
            caller_is_developer=bool(job.get("caller_is_developer", False)),
            self_initiated=bool(job.get("self_initiated", False)),
            artifact_delivery_blocked=bool(
                suppressed or job.get("image_sent") or _int_value(job.get("delivery_version")) < 3
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


def _read_reference_images(
    data_store: Any, job: dict[str, Any], *, strict: bool | None = None
) -> list[tuple[str, bytes]]:
    """从任务目录读回参考图。

    普通临时参考沿用旧策略（个别文件丢了跳过）；人物绑定任务必须严格：PRD 要求
    缺图、哈希不一致或顺序无法确认时不得静默拿更少的参考继续生成。
    """
    names = [str(name) for name in job.get("references", []) if str(name)]
    strict_mode = (
        bool(job.get("strict_references") or job.get("visual")) if strict is None else strict
    )
    if not names:
        if strict_mode:
            raise ValueError("人物生图任务缺少参考图快照，已拒绝计费请求")
        return []
    reference_dir = _artifact_dir(data_store) / "references" / str(job.get("id", ""))
    images: list[tuple[str, bytes]] = []
    for name in names:
        path = reference_dir / name
        try:
            images.append((name, path.read_bytes()))
        except OSError as exc:
            if strict_mode:
                raise ValueError(f"人物生图参考图快照丢失: {name}") from exc
            logger.warning("参考图读取失败，已跳过: %s | %s", path, exc)
    if strict_mode:
        expected = job.get("reference_sha256") or [
            item.get("sha256") for item in (job.get("visual") or {}).get("references", [])
        ]
        if len(expected) != len(images) or any(
            not digest or sha256_bytes(image[1]) != digest
            for image, digest in zip(images, expected)
        ):
            raise ValueError("人物生图参考图快照哈希或图序不一致，已拒绝计费请求")
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


def _is_visual_job(job: dict[str, Any]) -> bool:
    return isinstance(job.get("visual"), dict) and bool(job.get("visual"))


def _visual_service_of(ctx: Any) -> Any:
    getter = getattr(ctx, "get_visual_service", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001 - 旧引擎/测试上下文没有视觉服务
        return None


async def _recover_visual_jobs(ctx: Any, data_store: Any, jobs: list[dict[str, Any]]) -> bool:
    """重启恢复：视觉绑定按外部边界处理；旧任务引用受管素材则隔离。"""
    service = _visual_service_of(ctx)
    if service is None:
        return False
    changed = False
    by_id = {str(job.get("id", "")): job for job in jobs}
    for job in jobs:
        if job.get("status") != "running" or not _is_visual_job(job):
            continue
        job_id = str(job.get("id", ""))
        try:
            result = await service.recover_generation(job_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("视觉任务 %s 恢复失败: %s", job_id, exc)
            result = {"status": "outcome_uncertain", "code": "outcome_uncertain"}
        _apply_visual_recovery(job, result)
        changed = True
    try:
        pending_bindings = await service.pending_generation_jobs()
    except Exception:  # noqa: BLE001
        pending_bindings = []
    for binding in pending_bindings:
        job_id = str(binding.get("job_id") or "")
        if not job_id or job_id in by_id:
            continue
        # 有绑定却没有任何队列条目：可能崩溃在 enqueue 与 mark 之间，也可能队列被删。
        try:
            result = await service.recover_generation(job_id)
            logger.warning("视觉绑定 %s 没有队列条目，恢复为 %s", job_id, result.get("status"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("孤立视觉绑定 %s 恢复失败: %s", job_id, exc)

    for job in jobs:
        if _is_visual_job(job) or job.get("status") not in _PENDING_STATUSES:
            continue
        job_id = str(job.get("id", ""))
        names = [str(name) for name in job.get("references") or [] if str(name)]
        if not names:
            continue
        if not await _legacy_references_are_managed(service, data_store, job_id, names):
            continue
        job.update(
            status="cancelled",
            error="旧任务引用了受管人物素材但缺少可核验来源绑定，已隔离，不再执行也不交付",
            error_code="legacy_managed_unverifiable",
            finished_at=_now_iso(),
            notified=False,
            callback_state="pending",
            callback_attempts=0,
            isolated=True,
            artifact_suppressed=True,
        )
        changed = True
        logger.warning("旧生图任务 %s 引用受管素材且无绑定，已隔离", job_id)
    return changed


def _apply_visual_recovery(job: dict[str, Any], result: dict[str, Any]) -> None:
    status = str(result.get("status") or "")
    code = str(result.get("code") or "outcome_uncertain")
    if status == "cancelled":
        job.update(
            status="cancelled",
            error=code,
            error_code=code,
            finished_at=_now_iso(),
            notified=False,
            callback_state="pending",
            callback_attempts=0,
            isolated=True,
            artifact_suppressed=True,
        )
        return
    if status in {"outcome_uncertain", "failed"}:
        job.update(
            status="failed",
            error=(
                "上游结果未知（可能已经计费），不会自动重试；需要人工核对后再决定是否重做"
                if status == "outcome_uncertain"
                else f"视觉任务失败（{code}）"
            ),
            error_code=code,
            finished_at=_now_iso(),
            notified=False,
            callback_state="pending",
            callback_attempts=0,
            isolated=status == "outcome_uncertain",
            artifact_suppressed=status == "outcome_uncertain",
        )
        return
    job.update(
        status="failed",
        error="进程重启，视觉任务中断",
        finished_at=_now_iso(),
        notified=False,
        callback_state="pending",
        callback_attempts=0,
    )


async def _legacy_references_are_managed(
    service: Any,
    data_store: Any,
    job_id: str,
    names: list[str],
) -> bool:
    reference_dir = _artifact_dir(data_store) / "references" / job_id
    for name in names:
        try:
            data = (reference_dir / name).read_bytes()
        except OSError:
            continue
        try:
            persons = await service.managed_persons_for_digest(sha256_bytes(data))
        except Exception:  # noqa: BLE001
            return True  # 无法验证受管归属时拒绝旧任务，而非继续上传。
        if persons:
            return True
    return False


def _recover_interrupted_jobs(jobs: list[dict[str, Any]]) -> bool:
    """生成中断不重新扣费；回调中断按已落盘的副作用边界恢复。"""
    changed = False
    for job in jobs:
        if job.get("status") == "running" and not _is_visual_job(job):
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
            if job.get("deleted"):
                return job
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
