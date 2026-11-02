"""人物视觉资产管理 TOOL（PRD TOOL-04）与自动候选后台控制器。

工具只做薄编排：解析参数 → 取领域服务与逐调用权限 → ``manage``。正式启用、
授权与停用都要求本回合真实证据；自主回合（``self_initiated``）不能执行外部写入。

``create_background_tasks`` 注册的是**领域控制器**而不是新队列：它扫描有效
``auto_candidate`` 授权与缺项，通过同一个视觉服务把任务交给现有 ``image_generate``
队列，既不加每轮 LLM 调用，也不放开模型自主写入。
"""

from __future__ import annotations

import logging
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.extension_runtime import BackgroundTaskSpec
from sirius_pulse.visual import tooling
from sirius_pulse.visual.errors import VisualError

logger = logging.getLogger(__name__)

_AUTO_INTERVAL_SECONDS = 45.0
_ACTIONS = [
    "create_character",
    "stage",
    "derive",
    "activate",
    "set_policy",
    "revoke_consent",
    "retire",
]

_config = ConfigBuilder()
_config.group("动作").add(
    "action",
    type="str",
    required=True,
    choices=_ACTIONS,
    description=(
        "要执行的动作：create_character 登记虚构角色；stage 保存原图/草稿；"
        "derive 派生三视图候选；activate 启用某修订；set_policy 记录自动候选预授权或兼容许可；"
        "revoke_consent 撤回授权；retire 停用修订或资产。"
    ),
)
_config.group("主体").add(
    "target",
    type="str",
    description=(
        '单个主体引用，JSON 对象或对象本身：{"ref": "requester"}、'
        '{"ref": "persona"}、{"ref": "person_id", "value": "<UUID>"}、'
        '{"ref": "name", "value": "<名称>"}。省略时默认 requester；'
        "为其他人投稿须明确选定可见人物，投稿者不必是图中人物。"
        "自主回合不能省略 target，请显式指定 persona 或 person_id。"
    ),
)
_config.group("素材").add(
    "source",
    type="str",
    description=(
        '入库素材来源：默认 {"kind": "attachment"}（本回合聊天图片，可用 index 选第几张）；'
        '或 {"kind": "asset", "asset_id": "<受管资产ID>"}；'
        '自主回合可直接用 {"kind": "path", "path": "任意本地图片路径"} 导入（相对路径按人格目录解析）。'
        '远程 URL 仍会被拒绝。'
    ),
)
_config.group("素材").add("view", type="str", description="参考图视角/用途说明，如 正视图、侧视图、背面、立绘。")
_config.group("素材").add(
    "purpose",
    type="str",
    description="视觉档案用途：identity（默认）/outfit/pose/style/scene。",
)
_config.group("素材").add(
    "scope",
    type="str",
    description="默认当前会话范围（group:<群号>）；人格资料用 persona；仅应用管理员可扩大为 persona。",
)
_config.group("角色").add("name", type="str", description="create_character 时的角色名称。")
_config.group("说明").add("identity_description", type="str", description="外观/身份特征说明。")
_config.group("说明").add("default_outfit", type="str", description="默认服装说明。")
_config.group("说明").add("variant_notes", type="str", description="版本差异或不可变限制。")
_config.group("说明").add("inferred_details", type="str", description="推测/补全细节，需明确标注不确定。")
_config.group("授权").add(
    "capabilities",
    type="list",
    description="自动候选预授权或旧兼容能力：view / vision_read / upload_generate / external_send；不限制手动共享。",
)
_config.group("授权").add(
    "mode",
    type="str",
    description="manual（默认）或 auto_candidate；不提供自动启用。",
)
_config.group("授权").add(
    "revision_scope",
    type="str",
    description="授权覆盖范围：selected（默认）/current/future。",
)
_config.group("授权").add("expires_at", type="str", description="授权到期时间（ISO 8601）。")
_config.group("授权").add("max_uses", type="int", description="授权最多使用次数。")
_config.group("授权").add("grant_id", type="str", description="要撤回的授权 ID。")
_config.group("授权").add("grantee", type="str", description="允许使用此许可的人物 UUID；默认仅操作者本人。")
_config.group("修订").add("profile_id", type="str", description="视觉档案 ID。")
_config.group("修订").add("revision", type="int", description="修订号。")
_config.group("修订").add("expected_revision", type="int", description="启用时的当前预期修订（CAS）。")
_config.group("修订").add("asset_id", type="str", description="要停用的资产 ID。")
_config.group("修订").add(
    "activate",
    type="bool",
    description="只有维护者可在 stage 时顺带启用原图；其他投稿者仍仅保存草稿并返回需确认。",
)
_config.group("修订").add("redo", type="bool", description="derive 时明确重做，允许新消耗一次额度。")
_config.group("来源").add(
    "evidence_ref",
    type="str",
    description="本回合用户明确指令的来源引用（缺省时用框架注入的当轮证据）。",
)
_config.group("来源").add(
    "source_ref",
    type="str",
    description="本次创作/请求的来源引用，用于解析 requester 原作者。",
)

TOOL_META = {
    "name": "visual_asset_manage",
    "description": (
        "【适用场景】维护受管人物的共享视觉素材库（立绘、三视图、外观参考草稿等）。用户指令时调用；"
        "自主回合也可直接登记任意人物的视觉资料（含自己、虚构角色与真人），不需要逐轮授权。\n"
        "【使用方法】通过 action 执行操作（如 stage 投稿草稿、activate 启用版本、retire 停用版本）；"
        "target 传入目标主体；evidence_ref 传入用户授权证据引用。\n"
        "【注意事项】普通投稿为草稿态，正式启用、停用或删除必须拥有目标人物维护权；投稿不改变目标身份资料；生成素材不自动启用或发送。"
    ),
    "version": "1.0.0",
    "side_effect": "external_write",
    "tags": ["person", "visual", "asset", "image", "consent"],
    "parameters": _config.build(),
    "config": {
        "auto_candidate_interval_seconds": {
            "type": "int",
            "description": "自动候选后台控制器的检查间隔（秒），默认 45。",
            "default": int(_AUTO_INTERVAL_SECONDS),
            "group": "限制",
        },
    },
}


async def run(
    action: str,
    target: Any = None,
    source: Any = None,
    name: str = "",
    identity_description: str = "",
    default_outfit: str = "",
    variant_notes: str = "",
    inferred_details: str = "",
    purpose: str = "",
    view: str = "",
    scope: str = "",
    mode: str = "",
    capabilities: Any = None,
    revision_scope: str = "",
    expires_at: str = "",
    grantee: str = "",
    max_uses: Any = None,
    profile_id: str = "",
    revision: Any = None,
    asset_id: str = "",
    expected_revision: Any = None,
    grant_id: str = "",
    activate: Any = False,
    redo: Any = False,
    draft_profile_id: str = "",
    evidence_ref: str = "",
    source_ref: str = "",
    *,
    engine_context: Any = None,
    invocation_context: Any = None,
    data_store: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """执行一个视觉资产动作并返回可读结果。"""
    del kwargs
    try:
        service = tooling.visual_service(engine_context)
        access = tooling.person_access(engine_context, invocation_context)
        target_map = tooling.normalise_mapping(target, name="target")
        source_map = tooling.normalise_mapping(source, name="source")
        capabilities_list = _normalise_capabilities(capabilities)
        fields: dict[str, Any] = {
            "name": name,
            "identity_description": identity_description,
            "default_outfit": default_outfit,
            "variant_notes": variant_notes,
            "inferred_details": inferred_details,
            "purpose": purpose,
            "view": view,
            "scope": scope,
            "mode": mode,
            "capabilities": capabilities_list,
            "revision_scope": revision_scope,
            "expires_at": expires_at,
            "grantee": grantee,
            "profile_id": profile_id,
            "asset_id": asset_id,
            "grant_id": grant_id,
            "draft_profile_id": draft_profile_id,
            "activate": tooling.normalise_bool(activate),
            "redo": tooling.normalise_bool(redo),
        }
        for key, value in (
            ("source", source_map),
            ("max_uses", tooling.normalise_int(max_uses, name="max_uses")),
            ("revision", tooling.normalise_int(revision, name="revision")),
            (
                "expected_revision",
                tooling.normalise_int(expected_revision, name="expected_revision"),
            ),
        ):
            if value is not None:
                fields[key] = value
        effective_evidence = str(evidence_ref or "").strip()
        if not effective_evidence:
            refs = tuple(getattr(invocation_context, "evidence_refs", ()) or ())
            effective_evidence = str(refs[0]) if refs else ""
        queue = None
        if action == "derive":
            queue = _visual_queue(data_store)
        result = await service.manage(
            action,
            access=access,
            evidence_ref=effective_evidence,
            source_ref=source_ref,
            target=target_map,
            queue=queue,
            **fields,
        )
        return _tool_result(result)
    except VisualError as exc:
        return _error_result(exc)
    except Exception as exc:  # noqa: BLE001 - 工具层不泄露内部堆栈
        logger.exception("visual_asset_manage 执行失败")
        return _error_result(VisualError("invalid_argument", str(exc)))


def create_background_tasks(ctx: Any) -> list[BackgroundTaskSpec]:
    """注册自动候选控制器：只补齐缺项，不新增每轮视觉或 LLM 调用。"""

    async def _tick() -> None:
        try:
            service = tooling.visual_service(ctx)
            data_store = ctx.get_data_store("image_generate")
        except Exception:  # noqa: BLE001 - 引擎尚未就绪时静默跳过
            return
        queue = _visual_queue(data_store)
        if queue is None:
            return
        await service.auto_candidates(queue, limit=1)

    return [
        BackgroundTaskSpec(
            name="visual_auto_candidates",
            interval_seconds=_AUTO_INTERVAL_SECONDS,
            task_func=_tick,
        )
    ]


def _visual_queue(data_store: Any) -> Any:
    if data_store is None:
        return None
    from sirius_pulse.tools.builtin import image_generate

    builder = getattr(image_generate, "build_visual_queue", None)
    return builder(data_store) if callable(builder) else None


def _normalise_capabilities(value: Any) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("["):
            import json

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise VisualError("invalid_argument", "capabilities 不是合法 JSON 数组") from exc
            value = parsed
        else:
            value = [part.strip() for part in text.split(",")]
    if not isinstance(value, (list, tuple)):
        raise VisualError("invalid_argument", "capabilities 必须是数组")
    return [str(item).strip() for item in value if str(item).strip()]


def _tool_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("success") is False:
        return _error_result(
            VisualError(
                str(result.get("code") or "invalid_argument"),
                str(result.get("error") or "操作失败"),
                data=result.get("data") if isinstance(result.get("data"), dict) else None,
            )
        )
    summary = str(result.get("summary") or "已完成")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    detail_lines = [summary]
    if result.get("change_id"):
        detail_lines.append(f"变更记录: {result['change_id']}")
    if data.get("job_id"):
        detail_lines.append(f"后台任务: {data['job_id']}（完成后只保存候选，不会自动启用）")
    if data.get("profile_id") and data.get("revision"):
        detail_lines.append(f"视觉档案: {data['profile_id']} · 修订 v{data['revision']}")
    return {
        "success": True,
        "summary": summary,
        "text_blocks": detail_lines,
        "data": {"status": result.get("status", ""), **data},
        "internal_metadata": {"action": "visual_asset_manage"},
    }


def _error_result(exc: VisualError) -> dict[str, Any]:
    hint = _HINTS.get(exc.code, "")
    text = exc.message + (f"（{hint}）" if hint else "")
    return {
        "success": False,
        "error": text,
        "code": exc.code,
        "summary": f"视觉资产操作失败：{exc.message}",
        "text_blocks": [f"失败：{text}"],
        "data": {"code": exc.code, **(exc.data or {})},
    }


_HINTS = {
    "evidence_invalid": "请让用户在本回合明确说出要做的事，或重新选择合法来源",
    "consent_required": "需要本人或角色控制者明确授权",
    "revision_conflict": "请重新查询当前修订后再确认，不要沿用旧预期值",
    "required_asset_missing": "缺少可用正式参考图，请先保存形象原图",
    "reference_limit_exceeded": "参考图超过 4 张，需精简或分次生成",
    "quota_exceeded": "生成额度不足，停止重复提交",
    "already_pending": "已有同类任务在进行，等它完成即可",
    "cancelled_by_revocation": "授权已撤回，任务已取消或结果被隔离",
    "outcome_uncertain": "上游结果未知，不会自动重试付费操作",
    "pending_assignment": "已保存为待绑定草稿，需确认图中人物归属",
}
