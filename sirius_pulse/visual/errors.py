"""视觉资产的错误码与统一异常。

PRD TOOL-06 要求错误语义可被模型直接理解，且不得泄露内部路径、凭据或隐藏资料。
领域层只抛 :class:`VisualError`，工具层再把它转成 ``{"success": False, "code": ...}``。
"""

from __future__ import annotations

from typing import Any

#: PRD TOOL-06 与 AUTH／ASSET／AUTO 章节使用到的稳定错误码。
CODES: frozenset[str] = frozenset(
    {
        "source_ambiguous",
        "evidence_invalid",
        "subject_ambiguous",
        "not_found_or_not_visible",
        "permission_denied",
        "consent_required",
        "revision_conflict",
        "required_asset_missing",
        "asset_invalid",
        "reference_limit_exceeded",
        "quota_exceeded",
        "already_pending",
        "cancelled_by_revocation",
        "outcome_uncertain",
        "unsupported_action",
        "already_processed",
        "invalid_argument",
        "confirm_required",
        "confirm_invalid",
        "service_unavailable",
        "queue_unavailable",
        "pending_assignment",
    }
)


class VisualError(Exception):
    """带稳定错误码的领域异常。"""

    def __init__(self, code: str, message: str = "", *, data: dict[str, Any] | None = None):
        if code not in CODES:
            code = "invalid_argument"
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.data = dict(data or {})

    def to_result(self) -> dict[str, Any]:
        """转成工具/API 可直接返回的结果字典。"""
        result: dict[str, Any] = {
            "success": False,
            "status": "error",
            "code": self.code,
            "error": self.message,
        }
        if self.data:
            result["data"] = dict(self.data)
        return result

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"VisualError({self.code!r}, {self.message!r})"


def as_visual_error(exc: BaseException, default_code: str = "invalid_argument") -> VisualError:
    """把外部异常（尤其是 people.PersonError）映射成视觉领域错误。

    people 层约定异常带 ``code`` 属性；未知异常保留原文但收敛错误码，避免把
    内部堆栈或路径漏给模型。异常链里已经带有 :class:`VisualError` 时原样返回，
    不再被外层 ``except Exception`` 误降级成 ``invalid_argument``。
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, VisualError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    code = str(getattr(exc, "code", "") or "").strip()
    if code in CODES:
        return VisualError(code, str(exc), data=getattr(exc, "data", None) or {})
    return VisualError(default_code, str(exc))
