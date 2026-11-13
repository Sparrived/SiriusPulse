"""她自己生活里的三次模型调用：早上排日程、睡前回顾、夜里做梦。

回复的解析在 :mod:`sirius_pulse.life.reflection`（日程与回顾）和
:mod:`sirius_pulse.life.dreams`（梦），提示词与解析放在同一个包里，改一边时
另一边就在手边。排列与安全说明沿用 :mod:`sirius_pulse.utils.prompt_layout`。
"""

from __future__ import annotations

from typing import Any

from sirius_pulse.utils.prompt_layout import join_turn_sections


def build_life_plan_prompt(
    *,
    wake: str,
    sleep: str,
    state_line: str,
    self_note: dict[str, Any],
    projects: list[dict[str, str]],
    intentions: list[dict[str, str]],
    yesterday: list[str],
    max_work_blocks: int,
    free_project_slots: int,
) -> tuple[str, list[dict[str, str]]]:
    """Build the morning-plan prompt: she lays out her own day as JSON."""
    if free_project_slots > 0:
        new_project_rule = (
            f"也可以开新项目（最多 {free_project_slots} 个）：在 new_projects 里给它一个 key"
            '（如 "n1"），再在 project 里填这个 key；\n'
        )
    else:
        new_project_rule = "手上的项目已经够多，今天不开新项目；\n"
    rules = (
        "【排今天的日程】\n"
        "你刚醒，给自己排一下今天。这是你自己的生活：想读的书、想学的东西、想写的、"
        "想收集整理的、想动手做的，都可以是一个「项目」。每个项目有一份你自己的作品，"
        "你每次做一点、写一点，日积月累。\n"
        "规则：\n"
        f"- 日程只排在你醒着的时间里（{wake}–{sleep}），每段 20–180 分钟，互不重叠，"
        "最多 6 段；\n"
        f"- 做项目的段落最多 {max_work_blocks} 段，填 project；"
        "其余是日常（午睡、散步、听歌、做饭、发呆……），不填 project；\n"
        "- project 填已有项目的 id；" + new_project_rule + "- 手上没有项目时，从你的人设、兴趣和惦记的事里挑一件真正想做的开始；\n"
        "- 新项目要具体、能一点点推进、能写出东西："
        "「读《雪国》并写读书笔记」是好项目，「了解文学」太空；\n"
        "- 你惦记着想弄明白的事可以排进来，用到的把 id 填进 use_intentions；\n"
        "- 按你此刻的精力和心情安排，不必排满。\n"
        "只输出一个 JSON 对象，不要任何别的文字：\n"
        '{"note": "今天想怎么过，一句话", '
        '"new_projects": [{"key": "n1", "title": "", '
        '"kind": "read|learn|write|collect|make|explore", "why": "", "next_step": ""}], '
        '"blocks": [{"start": "HH:MM", "end": "HH:MM", "activity": "", "project": ""}], '
        '"use_intentions": []}'
    )
    situation = ["【今天的你】", f"状态：{state_line}"]
    if self_note.get("recent"):
        situation.append(f"近况：{self_note['recent']}")
    if self_note.get("interests"):
        situation.append("最近感兴趣的：" + "、".join(self_note["interests"]))
    if self_note.get("tomorrow"):
        situation.append(f"昨晚睡前想着今天：{self_note['tomorrow']}")
    if projects:
        lines = ["手上的项目："]
        for item in projects:
            line = (
                f"- {item.get('project_id')}｜{item.get('kind_label', '')}"
                f"「{item.get('title', '')}」"
            )
            if item.get("next_step"):
                line += f"；下一步：{item['next_step']}"
            if item.get("last_step"):
                line += f"；上次：{item['last_step']}"
            lines.append(line)
        situation.append("\n".join(lines))
    else:
        situation.append("手上还没有项目。")
    if intentions:
        lines = ["惦记着想弄明白的事："]
        for item in intentions[:6]:
            why = f"（{item['why']}）" if item.get("why") else ""
            lines.append(f"- {item.get('intention_id')}｜{item.get('what', '')}{why}")
        situation.append("\n".join(lines))
    if yesterday:
        situation.append("昨天：\n" + "\n".join(f"- {line}" for line in yesterday[:8]))
    system_prompt = join_turn_sections(stable=[rules], situation="\n".join(situation))
    return system_prompt, [{"role": "user", "content": "（早上了，排一下今天）"}]


def build_life_reflect_prompt(
    *,
    plan_note: str,
    blocks: list[str],
    steps: list[str],
    shares: list[str],
    mood: str,
    self_note: dict[str, Any],
    projects: list[dict[str, str]],
) -> tuple[str, list[dict[str, str]]]:
    """Build the bedtime reflection prompt.

    Only her own first-hand record goes in -- no chat transcript and nothing
    about other people -- and only small, overwrite-in-place fields come out.
    That is what keeps this from repeating the old diary's failure mode.
    """
    rules = (
        "【睡前回想】\n"
        "一天结束了，睡前回想一下今天你自己的生活。下面只有你自己做过的事的记录。"
        "只根据这些记录写，不要编造没发生的事，也不要写别人的事。\n"
        "只输出一个 JSON 对象，不要任何别的文字：\n"
        '{"recent": "第一人称写你最近的状态，150 字以内，会替换掉旧的近况", '
        '"interests": ["最近感兴趣的东西，2-6 个字的短词，最多 8 个"], '
        '"tomorrow": "明天想做点什么，一句话", '
        '"mood_shift": 0.0, '
        '"projects": [{"id": "p-…", "status": "paused|done|dropped", "next_step": ""}]}\n'
        "mood_shift 是 -0.05 到 0.05 之间的数：今天整体让你更开心就为正，更低落就为负。"
        "projects 只写有变化的项目；状态不变就不写 status，下一步不变就不写 next_step。"
    )
    situation = ["【今天】"]
    if plan_note:
        situation.append(f"早上想的：{plan_note}")
    if blocks:
        situation.append("日程：\n" + "\n".join(f"- {line}" for line in blocks))
    else:
        situation.append("今天没有排日程。")
    if steps:
        situation.append("做成的事：\n" + "\n".join(f"- {line}" for line in steps))
    if shares:
        situation.append("说给别人的话：\n" + "\n".join(f"- {line}" for line in shares))
    if mood:
        situation.append(f"此刻心情：{mood}")
    if self_note.get("recent"):
        situation.append(f"之前的近况（会被替换）：{self_note['recent']}")
    if projects:
        lines = ["手上的项目："]
        for item in projects:
            lines.append(
                f"- {item.get('project_id')}｜「{item.get('title', '')}」；"
                f"下一步：{item.get('next_step', '')}"
            )
        situation.append("\n".join(lines))
    system_prompt = join_turn_sections(stable=[rules], situation="\n".join(situation))
    return system_prompt, [{"role": "user", "content": "（该睡了，回想一下今天）"}]


def build_life_dream_prompt(
    *,
    name: str,
    day: list[str],
    fragments: list[str],
    mood: str,
    recent: str,
) -> tuple[str, list[dict[str, str]]]:
    """Build the dream prompt: tonight's dream, made from the day's leftovers.

    A dream is allowed -- expected -- to be untrue, which is exactly why its
    output never goes back into memory.  The input is still bounded: her own
    day and a handful of fragments from a single chat.
    """
    who = f"你是{name}。" if name else ""
    rules = (
        "【做梦】\n"
        f"{who}你已经睡着了，正在做一个梦。梦是白天的碎片被打乱、变形、拼在一起的样子："
        "地点会突然换掉，人会变成别的东西，小事会被放得很大，逻辑说不通也没关系。\n"
        "从下面的碎片里挑一两样当引子，用梦的方式把它们串起来，不要照搬原话，"
        "也不要解释这个梦有什么含义。第一人称，像醒来后跟人复述梦那样，"
        "60 到 150 字，有一两个具体又古怪的细节。\n"
        "只输出一个 JSON 对象，不要任何别的文字：\n"
        '{"dream": "梦的内容", "feeling": "醒来时的感觉，一句话，20 字以内"}'
    )
    situation = ["【白天留下的碎片】"]
    if day:
        situation.append("你自己的一天：\n" + "\n".join(f"- {line}" for line in day[:8]))
    if fragments:
        situation.append("聊天里的片段：\n" + "\n".join(f"- {line}" for line in fragments))
    if mood:
        situation.append(f"睡前的心情：{mood}")
    if recent:
        situation.append(f"你最近：{recent}")
    system_prompt = join_turn_sections(stable=[rules], situation="\n".join(situation))
    return system_prompt, [{"role": "user", "content": "（夜深了，你在做梦）"}]
