from __future__ import annotations

from sirius_pulse.memory.profile.models import PROFILE_SECTIONS, ProfileItem, UserPersonaProfile

_SECTION_TITLES = {
    "aliases": "别称",
    "identity": "身份",
    "interests": "兴趣",
    "preferences": "偏好",
    "communication_style": "沟通方式",
    "relationship": "与AI关系",
    "social_relations": "社交关系",
    "boundaries": "边界",
    "emotional_pattern": "情绪模式",
    "notes": "备注",
}


class ProfilePromptRenderer:
    """Render compact profile cards for chat prompts."""

    def render_section(self, profiles: list[UserPersonaProfile]) -> str | None:
        cards = [self.render_card(profile) for profile in profiles]
        cards = [card for card in cards if card]
        if not cards:
            return None
        rules = (
            "<user_persona_profiles>\n"
            "以下是长期人物画像，只作为相处参考。优先相信当前对话和用户纠正；"
            "不要把低置信或过期信息当成事实。"
        )
        return f"{rules}\n" + "\n".join(cards) + "\n</user_persona_profiles>"

    def render_card(self, profile: UserPersonaProfile) -> str:
        has_content = bool(profile.short_impression)
        for section_name in PROFILE_SECTIONS:
            if profile.section(section_name).active_items():
                has_content = True
                break
        if not has_content:
            return ""

        name = profile.display_name or profile.user_id
        lines = [
            f'<user_profile user_id="{_escape_attr(profile.user_id)}" name="{_escape_attr(name)}">'
        ]
        if profile.short_impression:
            lines.append(f"印象：{profile.short_impression[:180]}")

        for section_name in PROFILE_SECTIONS:
            section = profile.section(section_name)
            items = section.active_items()
            if not items and not section.summary:
                continue
            title = _SECTION_TITLES.get(section_name, section_name)
            parts: list[str] = []
            if section.summary:
                parts.append(section.summary[:140])
            for item in sorted(items, key=_item_rank, reverse=True)[:4]:
                parts.append(item.value[:120])
            if parts:
                lines.append(f"{title}：{'；'.join(parts[:4])}")

        if profile.familiarity_score or profile.affinity_score:
            lines.append(
                f"互动统计：熟悉度 {profile.familiarity_score:.2f}，回应亲和 {profile.affinity_score:.2f}"
            )
        lines.append("</user_profile>")
        return "\n".join(lines)


def _item_rank(item: ProfileItem) -> tuple[float, int, str]:
    return (item.confidence, item.update_count, item.last_seen_at)


def _escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


