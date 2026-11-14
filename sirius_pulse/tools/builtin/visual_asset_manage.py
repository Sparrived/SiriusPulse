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
from sirius_pulse.visual.access import GRANTABLE_CAPABILITIES
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
        "stage 保存一张原图（默认直接设为正式形象）；create_character 登记虚构角色；"
        "derive 派生三视图候选；activate 启用某个修订；retire 停用修订或资产；"
        "set_policy 允许以后自动生成三视图候选；revoke_consent 撤回这类允许。"
    ),
)
_config.group("主体").add(
    "target",
    type="str",
    description=(
        '是谁的素材：{"ref": "requester"}（默认，说话的人）、{"ref": "persona"}（你自己）、'
        '{"ref": "person_id", "value": "<UUID>"} 或 {"ref": "name", "value": "<名称>"}。'
        "自主回合必须写明。"
    ),
)
_config.group("素材").add(
    "source",
    type="str",
    description=(
        '图从哪来：默认 {"kind": "attachment", "index": 0}（本轮聊天里的图）；'
        '{"kind": "asset", "asset_id": "..."}；或本地文件 {"kind": "path", "path": "..."}。'
        "不收网络 URL。"
    ),
)
_config.group("素材").add("view", type="str", description="视角，如 正视图、侧视图、背面、立绘。")
_config.group("素材").add(
    "purpose", type="str", description="identity（默认）/ outfit / pose / style / scene。"
)
_config.group("素材").add("scope", type="str", description="默认只在当前聊天可见；persona 表示所有聊天（需管理员）。")
_config.group("角色").add("name", type="str", description="create_character：角色名。")
_config.group("说明").add("identity_description", type="str", description="发色、五官等身份特征。")
_config.group("说明").add("default_outfit", type="str", description="默认服装。")
_config.group("说明").add("variant_notes", type="str", description="这个版本的差异或限制。")
_config.group("说明").add("inferred_details", type="str", description="你推测补全的部分。")
_config.group("授权").add(
    "revision_scope",
    type="str",
    description="set_policy：selected（默认，只管当前正式图）或 future（以后的新图也算）。",
)
_config.group("授权").add(
    "expires_at", type="str", description="set_policy：到期时间（ISO 8601），和 max_uses 至少填一个。"
)
_config.group("授权").add("max_uses", type="int", description="set_policy：最多自动生成几次。")
_config.group("授权").add("grant_id", type="str", description="revoke_consent：只撤这一条；不填则撤回此人的全部允许。")
_config.group("修订").add("profile_id", type="str", description="视觉档案 ID。")
_config.group("修订").add("revision", type="int", description="修订号。")
_config.group("修订").add(
    "expected_revision", type="int", description="activate：你看到的当前修订号，防止覆盖别人的改动。"
)
_config.group("修订").add("asset_id", type="str", description="retire：要停用的资产 ID。")
_config.group("修订").add(
    "activate",
    type="bool",
    default=True,
    description="stage：false 时只存为草稿，不设为正式形象。",
)
_config.group("修订").add("redo", type="bool", description="derive：明确要重做一次（会再消耗额度）。")
_config.group("来源").add(
    "evidence_ref",
    type="str",
    description="提出这件事的那条消息的 evidence_ref；不填用本轮的。",
)
_config.group("来源").add("source_ref", type="str", description="这次请求的来源引用，用于认出原作者。")

TOOL_META = {
    "name": "visual_asset_manage",
    "description": (
        "管理人物的形象图库（立绘、三视图、外观参考）：有人让你记住谁长什么样、换形象、"
        "做三视图时用，自主回合也能用。登记谁的形象都不用对方确认，之后画图会用上。"
        "你自己（persona）的正式形象只有控制者或管理员能改；生成出来的图不会自动启用。"
    ),
    "version": "1.0.0",
    "side_effect": "external_write",
    "redact_logs": True,
    "engagement_exempt": True,
    "request_source": True,
    # 自助登记只写入人格自己的视觉库，不向外部发送；允许在自主回合执行。
    "allowed_when_self_initiated": True,
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
    activate: Any = None,
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
        if action == "set_policy" and not mode:
            # 模型可见的 set_policy 只有一个意思：允许以后自动生成三视图候选。
            # manual 是旧兼容许可，不限制任何共享，不再暴露给模型。
            mode = "auto_candidate"
        if action == "revoke_consent" and not grant_id and capabilities_list is None:
            capabilities_list = sorted(GRANTABLE_CAPABILITIES)
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
            "activate": None if activate is None else tooling.normalise_bool(activate),
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
    "reference_limit_exceeded": "参考图超过 8 张，需精简或分次生成",
    "quota_exceeded": "生成额度不足，停止重复提交",
    "already_pending": "已有同类任务在进行，等它完成即可",
    "cancelled_by_revocation": "授权已撤回，任务已取消或结果被隔离",
    "outcome_uncertain": "上游结果未知，不会自动重试付费操作",
    "pending_assignment": "已保存为待绑定草稿，需确认图中人物归属",
}
