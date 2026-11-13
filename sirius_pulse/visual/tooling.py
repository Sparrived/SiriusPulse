"""工具层与视觉领域服务之间的适配。

Lead 的引擎实现负责提供 ``get_visual_service`` / ``get_person_service`` /
``get_person_access``；本模块只做取用与兼容兜底，不复制权限判断。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sirius_pulse.visual.errors import VisualError

if TYPE_CHECKING:
    from sirius_pulse.tools.engine_context import PeopleAccess
    from sirius_pulse.tools.models import ToolInvocationContext


def visual_service(engine_context: Any) -> Any:
    return _service(engine_context, "get_visual_service", "视觉资产服务")


def person_service(engine_context: Any) -> Any:
    return _service(engine_context, "get_person_service", "人物服务")


def visual_context(
    engine_context: PeopleAccess | None,
    invocation_context: ToolInvocationContext | None,
) -> tuple[Any, Any]:
    """视觉资产服务与这次调用的权限上下文。

    没有引擎上下文时（脚本或测试直接调用工具）返回 ``(None, None)``，工具按未接入
    视觉服务的旧路径处理；没有逐调用上下文时权限为 None，由调用方拒绝受管资产。
    """
    if engine_context is None:
        return None, None
    access = (
        engine_context.get_person_access(invocation_context)
        if invocation_context is not None
        else None
    )
    return engine_context.get_visual_service(), access


def person_access(engine_context: Any, invocation_context: Any) -> Any:
    """从可信逐调用上下文构造 PersonAccess；模型参数永远不参与。"""
    if invocation_context is None:
        raise VisualError("permission_denied", "缺少逐调用上下文，不能确定操作者")
    if engine_context is not None:
        access = engine_context.get_person_access(invocation_context)
        if access is not None:
            return access
    people = person_service(engine_context)
    caller_id = str(getattr(invocation_context, "caller_user_id", "") or "")
    actor_id = ""
    if caller_id:
        try:
            actor_id = str(people.person_id_for_user(caller_id) or "")
        except Exception:  # noqa: BLE001 - 旧用户可能还没有 UUID
            actor_id = ""
    evidence_refs = tuple(
        str(ref) for ref in (getattr(invocation_context, "evidence_refs", ()) or ()) if ref
    )
    kwargs = {
        "actor_id": actor_id,
        "group_id": str(getattr(invocation_context, "group_id", "") or ""),
        "evidence_refs": evidence_refs,
        "is_admin": bool(getattr(invocation_context, "caller_is_developer", False)),
        "self_initiated": bool(getattr(invocation_context, "self_initiated", False)),
    }
    try:
        from sirius_pulse.people import PersonAccess

        return PersonAccess(**kwargs)
    except Exception:  # noqa: BLE001 - people 尚未就绪时退回等价字段对象
        return _FallbackAccess(**kwargs)


class _FallbackAccess:
    """与冻结 PersonAccess 字段一致的兜底对象（仅测试/迁移期）。"""

    __slots__ = ("actor_id", "group_id", "evidence_refs", "is_admin", "self_initiated")

    def __init__(
        self,
        *,
        actor_id: str = "",
        group_id: str = "",
        evidence_refs: tuple[str, ...] = (),
        is_admin: bool = False,
        self_initiated: bool = False,
    ) -> None:
        self.actor_id = actor_id
        self.group_id = group_id
        self.evidence_refs = evidence_refs
        self.is_admin = is_admin
        self.self_initiated = self_initiated


def normalise_mapping(value: Any, *, name: str) -> dict[str, Any] | None:
    """接受对象或 JSON 字符串；模型两条路径都常见。"""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        import json

        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VisualError("invalid_argument", f"{name} 不是合法 JSON 对象") from exc
        if not isinstance(parsed, dict):
            raise VisualError("invalid_argument", f"{name} 必须是对象")
        return parsed
    raise VisualError("invalid_argument", f"{name} 必须是对象")


def normalise_int(value: Any, *, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise VisualError("invalid_argument", f"{name} 必须是整数") from exc


def normalise_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _service(engine_context: Any, getter_name: str, label: str) -> Any:
    if engine_context is None:
        raise VisualError("service_unavailable", f"{label}未接入引擎上下文")
    getter = getattr(engine_context, getter_name, None)
    if not callable(getter):
        raise VisualError(
            "service_unavailable",
            f"引擎上下文缺少 {getter_name}()，无法使用{label}",
        )
    service = getter()
    if service is None:
        raise VisualError("service_unavailable", f"{label}尚未初始化")
    return service
