"""人物生图编译：把人物、视觉修订与参考集变成确定性的 prompt 与图序。

编译在入队前完成（PRD GEN-01），输出固定内容快照；后台不再重新解释“我”，
也不会重新挑选“最新图”。这里只做纯函数式组装，权限与字节校验在 service 层。
"""

from __future__ import annotations

from typing import Any

from sirius_pulse.visual.errors import VisualError

PERSON_REFERENCE_LIMIT: int = 8
PERSON_RECIPE: str = "person_image_v1"
TURNAROUND_RECIPE: str = "turnaround_v1"
TURNAROUND_RECIPE_VERSION: str = "1"

_DESCRIPTION_LIMIT = 400


def clean_text(value: Any, limit: int = _DESCRIPTION_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _subject_block(subject: dict[str, Any]) -> list[str]:
    display = str(subject.get("display_name") or subject.get("person_id") or "人物")
    label = f"人物「{display}」"
    lines: list[str] = [f"- {label}（UUID: {subject.get('person_id', '')}）"]
    identity = clean_text(subject.get("identity_description"))
    outfit = clean_text(subject.get("default_outfit"))
    variant = clean_text(subject.get("variant_notes"))
    inferred = clean_text(subject.get("inferred_details"))
    override = clean_text(subject.get("outfit_override"))
    if identity:
        lines.append(f"  身份特征（必须保持）：{identity}")
    if outfit:
        lines.append(f"  默认服装：{outfit}")
    if variant:
        lines.append(f"  版本差异与限制：{variant}")
    if inferred:
        lines.append(f"  未展示/推测细节（AI 补全，未必准确）：{inferred}")
    if override:
        lines.append(f"  本次换装覆盖（仅本次生效，不改变正式形象）：{override}")
    return lines


def build_prompt(
    base_prompt: str,
    subjects: list[dict[str, Any]],
    mapping: list[dict[str, Any]],
    *,
    extra_references: int = 0,
) -> str:
    """把画面描述、人物身份说明与「参考序号—人物」映射合成最终 prompt。"""
    base = str(base_prompt or "").strip()
    if not subjects:
        raise VisualError("required_asset_missing", "没有可编译的人物主体")
    parts: list[str] = [base, "", "【人物身份约束】"]
    for subject in subjects:
        parts.extend(_subject_block(subject))
    parts.append("")
    parts.append("【参考图顺序与人物对应】")
    for item in mapping:
        index = item.get("index")
        if item.get("extra"):
            parts.append(f"- 参考图{index}：附加参考（场景/姿势/风格），不属于正式人物身份")
            continue
        display = str(item.get("display_name") or item.get("person_id") or "人物")
        label = str(item.get("purpose") or "identity")
        view = str(item.get("view") or "").strip()
        suffix = f"，{view}" if view else ""
        parts.append(f"- 参考图{index}：人物「{display}」的{label}参考{suffix}")
    if extra_references:
        parts.append(f"（其中 {extra_references} 张为附加参考，不改变人物身份。）")
    parts.append("")
    parts.append("请严格按上述人物与参考图对应关系绘制，不要混淆人物、不要交换人物特征；" "未被参考图展示的背面或结构可以合理补全，但不得改变已确认的身份特征。")
    return "\n".join(parts)


def apply_reference_budget(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """按 8 张总额裁剪参考图，绝不丢某个必需人物。

    Raises:
        VisualError: ``reference_limit_exceeded`` —— 必需图自身已超限。
    """
    if len(entries) <= PERSON_REFERENCE_LIMIT:
        return list(entries), 0

    required = [entry for entry in entries if entry.get("required")]
    optional = [entry for entry in entries if not entry.get("required")]
    if len(required) > PERSON_REFERENCE_LIMIT:
        raise VisualError(
            "reference_limit_exceeded",
            (
                f"必需参考图共 {len(required)} 张，超过单次 {PERSON_REFERENCE_LIMIT} 张上限；"
                "请精简正式参考集或分次生成，不能丢掉任何一个必需人物"
            ),
            data={"required": len(required), "limit": PERSON_REFERENCE_LIMIT},
        )

    keep_optional = PERSON_REFERENCE_LIMIT - len(required)
    kept = required + optional[:keep_optional]
    # 保持原始顺序，图序映射才对得上。
    kept_ids = {id(entry) for entry in kept}
    ordered = [entry for entry in entries if id(entry) in kept_ids]
    return ordered, len(entries) - len(ordered)


def build_turnaround_prompt(subject: dict[str, Any], reference_count: int) -> str:
    """固定配方：一张可审阅的三视图合集，不盲目按三等分切图。"""
    display = str(subject.get("display_name") or "角色")
    lines = [
        f"为角色「{display}」生成一张角色三视图设定合集：正面、侧面、背面三个视角并排，",
        "全身入画，白色纯背景，自然站姿，光线均匀，不添加文字、水印或边框。",
        "",
        "【人物身份约束】",
    ]
    lines.extend(_subject_block(subject))
    lines.extend(
        [
            "",
            "【参考图使用】",
            f"共 {reference_count} 张已确认参考图，按顺序作为该角色的身份与服装依据；",
            "必须保持参考图中的发色、五官、标志物与服装结构。三视图未展示的背面和侧面",
            "属于设计补全，可以合理推断，但不得改变已确认的身份特征。",
            "",
            "这是供人工审阅的候选设计稿，不是正式形象；不要加入其他人物或场景元素。",
        ]
    )
    return "\n".join(lines)
