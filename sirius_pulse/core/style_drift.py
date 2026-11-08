"""发现她的回复开始套同一个模板，并提醒她换个说法。

模型会模仿上下文里自己之前的回复：线上曾有八成回复是「X喵 / Y～」两行，旧的长度
规则改掉之后，几万 token 的历史仍在把她往这个格式上拉。靠一句「不要模板化」的常驻
规则拦不住，所以这里看她最近真实发出的几条，只在确实雷同时给出具体的提醒。
"""

from __future__ import annotations

import re
from collections import Counter

#: 至少看到这么多条才判断，免得刚开口两三句就被说成「模板化」。
MIN_REPLIES = 6
WINDOW = 10
#: 某种结构占到这个比例就算雷同。
DOMINANT_SHARE = 0.7
OPENING_SHARE = 0.4

_MARKER_RE = re.compile(r"\[(?:STICKER|POKE|REPLY|AT):[^\]]*\]")
_TRAILING_RE = re.compile(r"[\s～~！!？?。.,，…]+$")


def _clean(text: str) -> str:
    return _MARKER_RE.sub("", str(text or "")).strip()


def template_hint(replies: list[str]) -> str:
    """最近的回复雷同时返回一段提醒，否则返回空串。``replies`` 按时间顺序。"""
    texts = [t for t in (_clean(r) for r in replies[-WINDOW:]) if t]
    if len(texts) < MIN_REPLIES:
        return ""
    total = len(texts)
    notes: list[str] = []

    line_counts = Counter(len([ln for ln in t.splitlines() if ln.strip()]) for t in texts)
    lines, count = line_counts.most_common(1)[0]
    if lines >= 2 and count / total >= DOMINANT_SHARE:
        notes.append(f"几乎都是固定的{lines}行")

    endings = Counter(_TRAILING_RE.sub("", t)[-1:] for t in texts)
    tail, count = endings.most_common(1)[0]
    if tail and count / total >= DOMINANT_SHARE:
        notes.append(f"句尾几乎都落在「{tail}」上")

    openings = Counter(t[:2] for t in texts if len(t) >= 2)
    if openings:
        head, count = openings.most_common(1)[0]
        if count / total >= OPENING_SHARE:
            notes.append(f"总用「{head}」开头")

    if not notes:
        return ""
    return (
        "【换个说法】你最近几条回复" + "、".join(notes) + "，读起来像在套模板。"
        "这次换一种形式：可以只说一句、只甩个表情或戳一下、反问回去、接住别人的梗，"
        "或者认真多说几句——别再用同一个结构，口癖也不用句句都带。"
    )
