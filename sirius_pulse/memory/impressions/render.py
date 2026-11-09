"""把印象写进聊天提示词：她自己心里的感觉，而不是一份资料卡。"""

from __future__ import annotations

from datetime import datetime, timezone

from sirius_pulse.memory.impressions.models import PersonImpression

#: 每轮最多带几个人的印象：发言人优先，其次是消息里点到名字的人。
MAX_PEOPLE_PER_TURN = 4
_MAX_SHARED_SHOWN = 3
_MAX_THREADS_SHOWN = 2
_HEADER = "【你心里对他们的印象】这是你自己对这些人的了解和感觉。它只是背景，不要沿用其中的情绪口吻，也不要主动复述；自然地带进相处里就好，惦记的事只在合适的时候提一次，别追着问。"


def pick_people(
    impressions: dict[str, PersonImpression],
    speaker_ids: list[str],
    text: str,
    *,
    limit: int = MAX_PEOPLE_PER_TURN,
) -> list[PersonImpression]:
    """这一轮该想起谁：先是说话的人，再是消息里被叫到名字的人。"""
    chosen: list[PersonImpression] = []
    for user_id in speaker_ids:
        impression = impressions.get(user_id)
        if impression is not None and impression not in chosen:
            chosen.append(impression)
    for impression in impressions.values():
        if len(chosen) >= limit:
            break
        name = impression.name.strip()
        if impression not in chosen and len(name) >= 2 and name in text:
            chosen.append(impression)
    return chosen[:limit]


def render_impressions(people: list[PersonImpression], now: datetime | None = None) -> str:
    """【你心里对他们的印象】块；没有可说的就返回空串。"""
    now = now or datetime.now(timezone.utc)
    blocks: list[str] = []
    for person in people:
        if not person.impression and not person.shared and not person.live_threads(now):
            continue
        name = person.name or person.user_id
        lines = [f"· {name}：{person.impression}".rstrip("：")]
        if person.shared:
            lines.append("  一起经历过：" + "；".join(person.shared[:_MAX_SHARED_SHOWN]))
        threads = person.live_threads(now)[:_MAX_THREADS_SHOWN]
        if threads:
            notes = []
            for thread in threads:
                if thread.asked_at:
                    notes.append(f"{thread.text}（你已经问过了，等对方的消息就好）")
                elif thread.is_due_soon(now):
                    notes.append(f"{thread.text}（就是这几天，合适时可以自然问问）")
                elif thread.due:
                    notes.append(f"{thread.text}（大概 {thread.due}）")
                else:
                    notes.append(thread.text)
            lines.append("  你还惦记着：" + "；".join(notes))
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return _HEADER + "\n" + "\n".join(blocks)
