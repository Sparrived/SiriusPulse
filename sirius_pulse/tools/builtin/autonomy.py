"""Built-in autonomous-life TOOL: the heartbeat of her own day.

She has a day of her own, shaped like a person's:

- **Morning** -- on the first heartbeat after she wakes up, she plans the day in
  one structured call (``life_plan``): a few blocks, some spent on her projects,
  some just routine.  With no project yet, the plan starts one, drawn from her
  persona, her interests and what she wrote down wanting to look into.
- **Day** -- when a project block comes due she spends one focused turn on it
  (``autonomy_generate`` with tools) and writes the result into that project's
  work file.  A routine block (a nap, a walk) costs no model call at all; it only
  sets what she is doing, so a chat during it can feel it.
- **Night** -- on the first heartbeat after bedtime she looks back on the day in
  one structured call (``life_reflect``), which rewrites her short self-note,
  updates her projects and nudges her baseline mood.  While asleep she does no
  work and sends nothing.
- **Dream** -- a while after she falls asleep she dreams once (``life_dream``),
  out of her own day and a few fragments from one chat.  The dream is kept only
  in ``memory/life/dreams.json`` and never becomes memory; she remembers it in
  the morning, in that chat only (see :mod:`sirius_pulse.life.dreams`).

Why it is built this way: the previous design offered her open "free time" with
no context and explicitly allowed "nothing", and a model nearly always took that
option, so autonomy produced almost nothing.  Now each turn continues something
she committed to herself, with its progress in front of her, and leaves an
artifact behind.  She can still skip a block, but skipping is a decision with a
reason (and it shows on her mood), not the default.

Sharing is unchanged: a tell-intention with an audience is delivered by rule (no
model call), at most once per cooldown and never while she is asleep; one whose
audience is undecided gets one turn to decide, within its attempt budget.

Missing people is a rule too: when someone she is close to has been away for
``miss_after_days``, she forms a *thought* of checking in on them (a draft
tell-intention with no audience).  The words, and where to say them, come from
the same audience turn; the rule never writes a greeting for her.  At most one
thought per person per ``miss_again_after_days`` and ``max_miss_per_day`` a day,
only toward people she can actually reach, and never while she is asleep.

Cost per day is bounded: one plan, at most ``max_work_blocks_per_day`` work
blocks, one reflection, one dream -- plus audience decisions, each capped per
intention.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.core.autonomy import Episode, is_quiet_hours
from sirius_pulse.core.intent import RESOLUTION_TELL, IntentFileStore, Intention, IntentStore
from sirius_pulse.core.prompt_factory import PromptFactory
from sirius_pulse.extension_runtime import BackgroundTaskSpec
from sirius_pulse.life import life_dir, schedule_from_config
from sirius_pulse.life.agenda import (
    BLOCK_DOING,
    BLOCK_DONE,
    BLOCK_SKIPPED,
    AgendaStore,
    Block,
    DayAgenda,
)
from sirius_pulse.life.clock import DaySchedule, to_local
from sirius_pulse.life.dreams import DreamStore, is_dream_time, parse_dream
from sirius_pulse.life.missing import MissLedger, people_to_miss
from sirius_pulse.life.projects import (
    MAX_ACTIVE_PROJECTS,
    STATUS_ACTIVE,
    STATUS_DONE,
    ProjectLimitError,
    ProjectStore,
)
from sirius_pulse.life.reflection import (
    SelfNoteStore,
    parse_plan,
    parse_reflection,
)
from sirius_pulse.life.state import LifeStateStore, energy_at, energy_word
from sirius_pulse.memory.units.models import MemoryUnit
from sirius_pulse.memory.units.store import MemoryUnitFileStore
from sirius_pulse.utils.json_io import atomic_write_json

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 600
_MIN_INTERVAL_SECONDS = 60

_MAX_EPISODES = 200
_OUTCOME_MAX_CHARS = 600

# A tell-intention is cheap to deliver, so it gets its own, much tighter gate:
# one share per hour per persona, on top of the normal reply cooldown.
_DEFAULT_SHARE_COOLDOWN_SECONDS = 3600

_DEFAULT_MAX_WORK_BLOCKS = 3
_MAX_WORK_BLOCKS_CEILING = 8

# A morning plan gets two tries a day, half an hour apart: enough to survive one
# transient upstream failure without turning a broken task binding into a loop.
_PLAN_MAX_TRIES = 2
_PLAN_RETRY_GAP_SECONDS = 30 * 60
# Not worth planning a day that is nearly over.
_PLAN_MIN_REMAINING_MINUTES = 60

# Missing people: after this many days away someone close starts to be on her
# mind; she thinks of the same person at most once per ``_MISS_AGAIN_DAYS``.
_DEFAULT_MISS_AFTER_DAYS = 5
_DEFAULT_MISS_AGAIN_DAYS = 7
_DEFAULT_MAX_MISS_PER_DAY = 1
_MAX_MISS_PER_DAY_CEILING = 2
_DEFAULT_MISS_MIN_CLOSENESS = 0.5
_MISS_URGENCY = 0.7
_MISS_KIND = "miss"
_MISS_STORE_KEY = "missing"

# A dream draws on at most this many fragments of the busiest chat of her day.
_DREAM_MAX_FRAGMENTS = 6
_DREAM_FRAGMENT_CHARS = 80

# Below this she is too worn out to start a project block.
_TOO_TIRED = 0.2

# How she declines a block.  Skipping must come with a reason, so the reason is
# whatever follows the marker.
_DECLINE_RE = re.compile(
    r"^\s*(?:先不做了|今天不做了|什么也不做|什么都不做|不想做|没什么想做的)[:：,，。\s]*(.*)",
    re.DOTALL,
)

# Mood nudges from her own life (valence, arousal).
_MOOD_PLANNED = (0.03, 0.02)
_MOOD_STEP = (0.03, 0.0)
_MOOD_WROTE = (0.05, 0.02)
_MOOD_FINISHED = (0.15, 0.05)
_MOOD_SKIPPED = (-0.03, 0.0)

TOOL_META = {
    "name": "autonomy",
    "description": (
        "【适用场景】后台被动工具，驱动人格自己的一天：早上排日程、白天按日程推进自己的项目、"
        "晚上回顾这一天（系统心跳触发，模型不可见）。\n"
        "【使用方法】由后台定时心跳驱动；项目段落会跑一次专注的工作回合并把成果写进作品，"
        "日常段落不调用模型；有对象的分享按规则投递。\n"
        "【注意事项】模型不可主动调用；睡眠时段内不工作也不外发；每天的工作段数受上限约束。"
    ),
    "version": "3.0.0",
    "model_visible": False,
    "side_effect": "external_write",
    "tags": ["autonomy", "life", "intent", "persona", "background"],
    "config": {
        "check_interval_seconds": {
            "type": "int",
            "description": "心跳间隔秒数，最少 60，默认 600。日程段最短 20 分钟，心跳不宜超过它。",
            "default": _DEFAULT_INTERVAL_SECONDS,
            "group": "节奏",
        },
        "wake_time": {
            "type": "str",
            "description": "她每天几点起床（本地时间 HH:MM），默认 07:30。",
            "default": "07:30",
            "group": "作息",
        },
        "sleep_time": {
            "type": "str",
            "description": "她每天几点睡觉（本地时间 HH:MM，可以过午夜），默认 23:30。睡着时不工作也不外发。",
            "default": "23:30",
            "group": "作息",
        },
        "max_work_blocks_per_day": {
            "type": "int",
            "description": "她每天最多花几段时间推进自己的项目（每段一次模型回合），默认 3，设为 0 表示只过日常不做项目。",
            "default": _DEFAULT_MAX_WORK_BLOCKS,
            "group": "节奏",
        },
        "share_cooldown_seconds": {
            "type": "int",
            "description": "两次主动分享之间的最小间隔秒数，默认 3600。",
            "default": _DEFAULT_SHARE_COOLDOWN_SECONDS,
            "group": "节奏",
        },
        "miss_after_days": {
            "type": "int",
            "description": "亲近的人多少天没出现，她会开始惦记、想去问候一下，默认 5，设为 0 表示不主动想念。",
            "default": _DEFAULT_MISS_AFTER_DAYS,
            "group": "想念",
        },
        "miss_again_after_days": {
            "type": "int",
            "description": "同一个人至少隔几天才会再想起一次，默认 7。",
            "default": _DEFAULT_MISS_AGAIN_DAYS,
            "group": "想念",
        },
        "max_miss_per_day": {
            "type": "int",
            "description": "她一天最多惦记几个人（每人一次决定说什么的模型回合），默认 1，最多 2。",
            "default": _DEFAULT_MAX_MISS_PER_DAY,
            "group": "想念",
        },
        "miss_min_closeness": {
            "type": "float",
            "description": "多亲近才会想念（0 到 1，按聊天次数、对方回应她的程度和她对对方的好感算），默认 0.5。",
            "default": _DEFAULT_MISS_MIN_CLOSENESS,
            "group": "想念",
        },
        "dream_enabled": {
            "type": "bool",
            "description": "睡着后会不会做梦（每晚一次模型调用），第二天早上她可能会顺口提起。梦不会写进记忆，默认开。",
            "default": True,
            "group": "梦",
        },
    },
}


def create_background_tasks(ctx: Any) -> list[BackgroundTaskSpec]:
    """Register the life heartbeat."""

    async def _tick() -> None:
        await run_tick(ctx)

    return [
        BackgroundTaskSpec(
            name="autonomy_tick",
            interval_seconds=_option_seconds(
                ctx, "check_interval_seconds", _DEFAULT_INTERVAL_SECONDS
            ),
            task_func=_tick,
        )
    ]


def _now() -> datetime:
    """Current UTC time.  Indirection point so tests can pin the clock."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


async def run_tick(ctx: Any) -> Episode | None:
    """Run one heartbeat.  Returns the episode, or None when nothing was recorded."""
    store = ctx.get_data_store("autonomy")
    reload_store = getattr(store, "reload", None)
    if callable(reload_store):
        reload_store()
    if store.get("_enabled", True) is False:
        return None

    now = _now()
    schedule = schedule_from_config(store)
    _settle_mood(ctx)

    if schedule.is_asleep(now):
        await _reflect_if_due(ctx, schedule, now)
        await _dream_if_due(ctx, schedule, now)
        return None

    agenda = await _ensure_today_plan(ctx, schedule, now)
    _miss_people(ctx, schedule, now)

    state = _load_state(store)
    # Sharing does not spend a model turn, so it is gated by its own cooldown and
    # goes first: words she already chose should not wait behind a work block.
    if _share_ready(state, now, ctx):
        share = _next_share(_load_intentions(ctx), now)
        if share is not None and await _deliver_share(ctx, share, state, now, schedule):
            return _record_share(ctx, state, share, now)

    if agenda is not None:
        agendas = AgendaStore(_life(ctx))
        if agenda.settle(schedule, now):
            agendas.save(agenda)
        block = agenda.due(schedule, now)
        if block is not None:
            return await _run_block(ctx, schedule, agenda, block, now)

    await _decide_audience(ctx, now)
    return None


def _life(ctx: Any) -> Path:
    return life_dir(ctx.get_work_path() or ".")


def _settle_mood(ctx: Any) -> None:
    settle = getattr(ctx, "settle_mood", None)
    if callable(settle):
        try:
            settle()
        except Exception:
            logger.debug("心情回落失败", exc_info=True)


def _nudge(ctx: Any, delta: tuple[float, float], reason: str) -> None:
    nudge = getattr(ctx, "nudge_mood", None)
    if callable(nudge):
        try:
            nudge(delta[0], delta[1], reason)
        except Exception:
            logger.debug("心情变化失败", exc_info=True)


def _mood_label(ctx: Any) -> str:
    label = getattr(ctx, "mood_label", None)
    try:
        return str(label() or "") if callable(label) else ""
    except Exception:
        return ""


def _state_line(ctx: Any, schedule: DaySchedule, now: datetime, work_done: int) -> str:
    state = LifeStateStore(_life(ctx)).load()
    energy = energy_at(
        schedule,
        now,
        work_done_today=work_done,
        woken_last_night=state.woken_count(now, within_seconds=12 * 3600),
    )
    mood = _mood_label(ctx)
    return f"精力{energy_word(energy)}" + (f"，心情{mood}" if mood else "")


# ---------------------------------------------------------------------------
# Morning: plan the day
# ---------------------------------------------------------------------------


async def _ensure_today_plan(ctx: Any, schedule: DaySchedule, now: datetime) -> DayAgenda | None:
    life = _life(ctx)
    today = schedule.life_date(now)
    agendas = AgendaStore(life)
    agenda = agendas.load(today)
    if agenda is not None:
        return agenda

    states = LifeStateStore(life)
    state = states.load()
    if not state.can_try_plan(
        today, now, max_tries=_PLAN_MAX_TRIES, gap_seconds=_PLAN_RETRY_GAP_SECONDS
    ):
        return None
    elapsed = schedule.minutes_since_wake(now)
    if schedule.awake_span - elapsed < _PLAN_MIN_REMAINING_MINUTES:
        return None
    # Booked before the call: a failing call must not be retried every heartbeat.
    states.update(lambda s: s.record_plan_try(today, now))

    projects = ProjectStore(life)
    active = projects.active()
    intentions = _load_intentions(ctx)
    do_items = [item for item in intentions.open_items(now=now.isoformat()) if not item.is_tell]
    max_blocks = _option_int(
        ctx, "max_work_blocks_per_day", _DEFAULT_MAX_WORK_BLOCKS, ceiling=_MAX_WORK_BLOCKS_CEILING
    )
    free_slots = max(0, MAX_ACTIVE_PROJECTS - len(active))
    note = SelfNoteStore(life).load()
    yesterday = agendas.load(schedule.life_date(now - timedelta(days=1)))

    system_prompt, messages = PromptFactory.build_life_plan_prompt(
        wake=schedule.wake,
        sleep=schedule.sleep,
        state_line=_state_line(ctx, schedule, now, 0),
        self_note=note.to_dict(),
        projects=[
            {
                "project_id": p.project_id,
                "title": p.title,
                "kind_label": p.kind_label,
                "next_step": p.next_step,
                "last_step": p.steps[-1]["did"] if p.steps else "",
            }
            for p in active
        ],
        intentions=[
            {"intention_id": item.intention_id, "what": item.what, "why": item.why}
            for item in do_items
        ],
        yesterday=_agenda_lines(yesterday),
        max_work_blocks=max_blocks,
        free_project_slots=free_slots,
    )
    try:
        text = await ctx.generate_text(
            system_prompt, messages, _fallback_group(ctx), task_name="life_plan"
        )
    except Exception:
        logger.warning("晨间计划调用失败，稍后可能再试一次", exc_info=True)
        return None

    plan = parse_plan(
        text,
        schedule=schedule,
        project_ids={p.project_id for p in active},
        intention_ids={item.intention_id for item in do_items},
        max_work_blocks=max_blocks,
        free_project_slots=free_slots,
        not_before=elapsed,
    )
    if plan is None:
        logger.warning("晨间计划无法解析，今天先不排: %s", str(text)[:200])
        return None

    keys: dict[str, str] = {}
    for new in plan.new_projects:
        try:
            project = projects.start(
                title=new.title, kind=new.kind, why=new.why, next_step=new.next_step, now=now
            )
        except (ProjectLimitError, ValueError):
            continue
        keys[new.key] = project.project_id
    blocks: list[Block] = []
    for block in plan.blocks:
        if block.project_id in keys:
            block.project_id = keys[block.project_id]
        elif block.project_id and not projects.get(block.project_id):
            block.project_id = ""
        blocks.append(block)

    agenda = DayAgenda(date=today, blocks=blocks, note=plan.note, made_at=now.isoformat())
    agenda.sort(schedule)
    agendas.save(agenda)
    states.update(lambda s: setattr(s, "last_plan_date", today))

    if plan.used_intentions:
        intentions = _load_intentions(ctx)
        for intention_id in plan.used_intentions:
            intentions.resolve(intention_id, outcome="排进了日程", now=now.isoformat())
        _save_intentions(ctx, intentions)

    _nudge(ctx, _MOOD_PLANNED, "排好了今天的日程")
    ctx.log_inner_thought(f"今天想：{plan.note or '照着日程过'}")
    await ctx.emit_event(
        "agent_turn_updated",
        {"origin": "self_initiated", "phase": "planned", "agenda": agenda.to_dict()},
    )
    return agenda


def _agenda_lines(agenda: DayAgenda | None) -> list[str]:
    if agenda is None:
        return []
    labels = {"done": "做了", "skipped": "没做", "missed": "错过", "planned": "没轮到"}
    lines = []
    for block in agenda.blocks:
        line = f"{block.start} {block.activity}（{labels.get(block.status, block.status)}"
        if block.reason and block.status != BLOCK_DONE:
            line += f"：{block.reason}"
        lines.append(line + "）")
    return lines


# ---------------------------------------------------------------------------
# Day: run the block that is due
# ---------------------------------------------------------------------------


async def _run_block(
    ctx: Any, schedule: DaySchedule, agenda: DayAgenda, block: Block, now: datetime
) -> Episode | None:
    life = _life(ctx)
    agendas = AgendaStore(life)
    states = LifeStateStore(life)
    until = schedule.moment(agenda.date, block.end_minutes)

    if not block.is_work:
        states.update(lambda s: s.set_activity(block.activity, until))
        block.status = BLOCK_DONE
        agendas.save(agenda)
        return None

    def _skip(reason: str) -> None:
        block.status = BLOCK_SKIPPED
        block.reason = reason[:300]
        agendas.save(agenda)

    max_blocks = _option_int(
        ctx, "max_work_blocks_per_day", _DEFAULT_MAX_WORK_BLOCKS, ceiling=_MAX_WORK_BLOCKS_CEILING
    )
    work_done = agenda.work_done()
    if work_done >= max_blocks:
        _skip("今天已经做得够多了")
        return None
    projects = ProjectStore(life)
    project = projects.get(block.project_id)
    if project is None or project.status != STATUS_ACTIVE:
        _skip("这件事已经放下了")
        return None
    state = states.load()
    energy = energy_at(
        schedule,
        now,
        work_done_today=work_done,
        woken_last_night=state.woken_count(now, within_seconds=12 * 3600),
    )
    if energy < _TOO_TIRED:
        _skip("太累了，歇一会儿")
        states.update(lambda s: s.set_activity("歇一会儿", until))
        _nudge(ctx, _MOOD_SKIPPED, "累得做不动了")
        return None

    # Booked before the turn: a turn that fails must not run again next heartbeat.
    # If it never comes back, ``settle`` closes the block as not done.
    block.status = BLOCK_DOING
    agendas.save(agenda)
    states.update(lambda s: s.set_activity(block.activity, until))
    work_before = len(projects.read_work(project.project_id))
    steps_before = len(project.steps)

    try:
        result = await ctx.run_life_work_turn(
            activity=block.activity,
            block_end=block.end,
            project={
                "project_id": project.project_id,
                "title": project.title,
                "kind_label": project.kind_label,
                "why": project.why,
                "next_step": project.next_step,
            },
            recent_steps=[f"{_local_day(step['at'])} {step['did']}" for step in project.steps[-5:]],
            work_tail=projects.work_tail(project.project_id),
            state_line=_state_line(ctx, schedule, now, work_done),
            group_id=_fallback_group(ctx),
        )
    except Exception:
        logger.warning("项目段落的工作回合失败，这一段算没做成", exc_info=True)
        _skip("没做成")
        return None
    text = str(result.get("text", "") or "").strip()

    declined = _DECLINE_RE.match(text) if text else None
    after = projects.get(project.project_id) or project
    wrote = len(projects.read_work(project.project_id)) > work_before
    stepped = len(after.steps) > steps_before
    if declined and not wrote:
        _skip(declined.group(1).strip() or "不想做")
        _nudge(ctx, _MOOD_SKIPPED, "跳过了自己排的事")
        return None
    if not text and not wrote and not stepped:
        _skip("没做成")
        return None

    outcome = text or (after.steps[-1]["did"] if after.steps else block.activity)
    if not stepped:
        # Her progress log must move even if she forgot to record the step.
        projects.record_step(project.project_id, did=outcome[:200], now=now)
    block.status = BLOCK_DONE
    block.reason = ""
    block.outcome = outcome[:300]
    agendas.save(agenda)

    after = projects.get(project.project_id) or after
    if after.status == STATUS_DONE:
        _nudge(ctx, _MOOD_FINISHED, f"做完了「{project.title}」")
    elif wrote:
        _nudge(ctx, _MOOD_WROTE, f"「{project.title}」有了新进展")
    else:
        _nudge(ctx, _MOOD_STEP, f"推进了「{project.title}」")

    ended = datetime.now(timezone.utc)
    episode = Episode(
        episode_id=uuid.uuid4().hex,
        started_at=now.isoformat(),
        ended_at=ended.isoformat(),
        kind=project.kind,
        seed=block.activity,
        outcome=outcome[:_OUTCOME_MAX_CHARS],
        refs=_extract_refs(outcome),
        intensity=0.6 if wrote else 0.4,
        status="done",
        project_id=project.project_id,
    )
    _append_episode(ctx, episode)
    await _write_memory_unit(ctx, _fallback_group(ctx), episode, title=project.title)
    ctx.log_inner_thought(f"{block.activity}：{episode.outcome[:60]}")
    await ctx.emit_event(
        "agent_turn_updated",
        {"origin": "self_initiated", "phase": "complete", "episode": episode.to_dict()},
    )
    return episode


def _local_day(value: str) -> str:
    try:
        return to_local(datetime.fromisoformat(value)).strftime("%m-%d")
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Night: look back on the day
# ---------------------------------------------------------------------------


async def _reflect_if_due(ctx: Any, schedule: DaySchedule, now: datetime) -> None:
    life = _life(ctx)
    day = schedule.life_date(now)
    states = LifeStateStore(life)
    if states.load().last_reflect_date == day:
        return
    # Booked before the call, like the plan: one try per night.
    states.update(lambda s: setattr(s, "last_reflect_date", day))
    agenda = AgendaStore(life).load(day)
    if agenda is None:
        return

    projects = ProjectStore(life)
    all_projects = projects.load()
    steps = [
        f"「{p.title}」{step['did']}"
        for p in all_projects
        for step in p.steps
        if _life_date_of(schedule, step.get("at", "")) == day
    ]
    shares = [
        str(item.get("outcome", ""))[:120]
        for item in _read_episodes(_episodes_path(ctx))
        if item.get("kind") == "share"
        and _life_date_of(schedule, str(item.get("ended_at", ""))) == day
    ]
    notes = SelfNoteStore(life)
    note = notes.load()
    active = [p for p in all_projects if p.status == STATUS_ACTIVE]
    system_prompt, messages = PromptFactory.build_life_reflect_prompt(
        plan_note=agenda.note,
        blocks=_agenda_lines(agenda),
        steps=steps,
        shares=shares,
        mood=_mood_label(ctx),
        self_note=note.to_dict(),
        projects=[
            {"project_id": p.project_id, "title": p.title, "next_step": p.next_step} for p in active
        ],
    )
    try:
        text = await ctx.generate_text(
            system_prompt, messages, _fallback_group(ctx), task_name="life_reflect"
        )
    except Exception:
        logger.warning("夜间回顾调用失败，今晚跳过", exc_info=True)
        return
    result = parse_reflection(text, project_ids={p.project_id for p in active})
    if result is None:
        logger.warning("夜间回顾无法解析，保留原来的近况: %s", str(text)[:200])
        return

    result.note.updated_at = now.isoformat()
    notes.save(result.note)
    for update in result.project_updates:
        if update.next_step:
            projects.set_next_step(update.project_id, update.next_step, now=now)
        if update.status:
            try:
                projects.set_status(update.project_id, update.status, now=now)
            except (ProjectLimitError, ValueError):
                pass
    settle = getattr(ctx, "settle_mood", None)
    if callable(settle):
        settle(baseline_shift=result.mood_shift)
    ctx.log_inner_thought(f"睡前想了想今天：{result.note.recent[:60]}")


async def _dream_if_due(ctx: Any, schedule: DaySchedule, now: datetime) -> None:
    """Dream once a night, after the reflection and past the first stretch of sleep."""
    store = ctx.get_data_store("autonomy")
    if store.get("dream_enabled", True) is False or not is_dream_time(schedule, now):
        return
    life = _life(ctx)
    day = schedule.life_date(now)
    states = LifeStateStore(life)
    state = states.load()
    if state.last_reflect_date != day or state.last_dream_date == day:
        return
    agenda = AgendaStore(life).load(day)
    chat_id, fragments = _dream_fragments(ctx, schedule, day)
    if agenda is None and not fragments:
        # Nothing happened to her today; there is nothing to dream about.
        return
    # Booked before the call, like the reflection: one try per night.
    states.update(lambda s: setattr(s, "last_dream_date", day))

    persona = ctx.get_persona()
    system_prompt, messages = PromptFactory.build_life_dream_prompt(
        name=str(getattr(persona, "name", "") or ""),
        day=_agenda_lines(agenda),
        fragments=fragments,
        mood=_mood_label(ctx),
        recent=SelfNoteStore(life).load().recent,
    )
    try:
        text = await ctx.generate_text(
            system_prompt, messages, chat_id or _fallback_group(ctx), task_name="life_dream"
        )
    except Exception:
        logger.warning("做梦的调用失败，今晚无梦", exc_info=True)
        return
    dream = parse_dream(text, date=day, chat_id=chat_id if fragments else "")
    if dream is None:
        logger.info("梦的内容无法解析，今晚无梦: %s", str(text)[:200])
        return
    dream.dreamt_at = now.isoformat()
    # Deliberately not ``_write_memory_unit``: a dream must never be recalled as fact.
    DreamStore(life).save(dream)
    ctx.log_inner_thought(f"做了个梦：{dream.text[:60]}")


def _dream_fragments(ctx: Any, schedule: DaySchedule, day: str) -> tuple[str, list[str]]:
    """The busiest chat of her day and its most salient memory fragments.

    Read-only.  Only one chat goes into a dream, so the dream can be told in that
    chat without carrying anything said elsewhere.  Her own self-initiated units
    are skipped: her day already comes in through the agenda.
    """
    try:
        store = MemoryUnitFileStore(Path(ctx.get_work_path() or "."))
        by_chat: dict[str, list[MemoryUnit]] = {}
        for group_id in store.list_group_ids():
            for unit in store.load(group_id, with_embeddings=False):
                if (unit.metadata or {}).get("origin") == "self_initiated":
                    continue
                if not unit.summary.strip():
                    continue
                if _life_date_of(schedule, unit.created_at or unit.event_time) != day:
                    continue
                by_chat.setdefault(str(unit.group_id or group_id), []).append(unit)
    except Exception:
        logger.debug("读取当天的记忆碎片失败，只用她自己的一天做梦", exc_info=True)
        return "", []
    if not by_chat:
        return "", []
    chat_id, units = max(by_chat.items(), key=lambda item: len(item[1]))
    units.sort(key=lambda unit: unit.salience, reverse=True)
    return chat_id, [
        " ".join(unit.summary.split())[:_DREAM_FRAGMENT_CHARS]
        for unit in units[:_DREAM_MAX_FRAGMENTS]
    ]


def _life_date_of(schedule: DaySchedule, stamp: str) -> str:
    try:
        return schedule.life_date(datetime.fromisoformat(stamp))
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


async def _deliver_share(
    ctx: Any,
    share: Intention,
    state: dict[str, Any],
    now: datetime,
    schedule: DaySchedule | None = None,
) -> bool:
    """Say what she wanted to say, to the audience she picked."""
    audience = share.audience.strip()
    if not audience:
        # She never picked anyone.  Ask her on a model turn rather than guessing a
        # destination on her behalf.
        return False
    # Sleep gates *sending* only, and deliberately not ``last_share_at``: the
    # intention stays pending so it goes out after she wakes up instead of being
    # dropped or silently marked as said.
    if is_quiet_hours(now, schedule):
        logger.debug("睡眠时段，暂不投递分享: %s", audience)
        return False
    try:
        delivered = await ctx.deliver_share(
            audience=audience,
            text=share.what,
            event_id=f"autonomy-share:{share.intention_id}",
        )
    except Exception as exc:
        logger.warning("自主分享投递失败: %s", exc)
        return False
    if not delivered:
        logger.info("自主分享当前不可达，保留待投递: %s", audience)
        return False
    state["last_share_at"] = now.isoformat()
    return True


def _record_share(ctx: Any, state: dict[str, Any], share: Intention, now: datetime) -> Episode:
    """Record a delivered share as an episode so her timeline shows it once."""
    intentions = _load_intentions(ctx)
    intentions.mark_shared(share.intention_id, now=now.isoformat())
    _save_intentions(ctx, intentions)

    episode = Episode(
        episode_id=uuid.uuid4().hex,
        started_at=now.isoformat(),
        ended_at=now.isoformat(),
        kind="share",
        seed=share.what,
        outcome=share.what,
        intention_id=share.intention_id,
        resolution=share.resolution,
        audience=share.audience,
        intensity=share.effective_urgency(now=now.isoformat()),
        status="done",
    )
    _append_episode(ctx, episode)
    state["last_episode_at"] = episode.ended_at
    _save_state(ctx.get_data_store("autonomy"), state)
    ctx.log_inner_thought(f"我把想说的话告诉了{share.audience_label or share.audience}")
    return episode


async def _decide_audience(ctx: Any, now: datetime) -> None:
    """Give one undecided tell-intention a turn to pick who to tell.

    Each attempt is booked against the intention before the turn runs, so one
    she never decides on cannot buy a model turn on every heartbeat.
    """
    intentions = _load_intentions(ctx)
    undecided = [
        item
        for item in intentions.open_items(now=now.isoformat())
        if item.is_tell and not item.audience.strip()
    ]
    if not undecided:
        return
    target = max(undecided, key=lambda item: item.effective_urgency(now=now.isoformat()))
    intentions.record_attempt(target.intention_id, now=now.isoformat())
    _save_intentions(ctx, intentions)
    try:
        await ctx.run_autonomous_turn(
            kind=target.kind,
            seed=target.what,
            group_id=target.notebook_origin_group or target.origin_group or _fallback_group(ctx),
            notebook_enabled=bool(target.notebook_origin_group),
            why=target.why,
            intention_id=target.intention_id,
            resolution=target.resolution,
        )
    except Exception:
        logger.warning("决定分享对象的回合失败，这次尝试已记下", exc_info=True)


def _miss_people(ctx: Any, schedule: DaySchedule, now: datetime) -> list[Intention]:
    """Turn "someone close has been away a long time" into a thought of checking in.

    Pure rule, no model call.  The intention is a draft with no audience: its
    ``what`` is her thought, not a message.  The audience turn
    (:func:`_decide_audience`) is where she writes the actual words and picks
    the chat, or decides not to bother them after all.
    """
    lister = getattr(ctx, "list_acquaintances", None)
    miss_after = _option_int(ctx, "miss_after_days", _DEFAULT_MISS_AFTER_DAYS, ceiling=365)
    if not callable(lister) or miss_after <= 0:
        return []
    store = ctx.get_data_store("autonomy")
    ledger = MissLedger.from_dict(store.get(_MISS_STORE_KEY, {}))
    today = schedule.life_date(now)
    max_per_day = _option_int(
        ctx, "max_miss_per_day", _DEFAULT_MAX_MISS_PER_DAY, ceiling=_MAX_MISS_PER_DAY_CEILING
    )
    if ledger.today_count(today) >= max_per_day:
        return []
    try:
        people = list(lister())
        labels = {str(a.chat_id): str(a.label or a.chat_id) for a in ctx.list_audiences()}
    except Exception:
        logger.debug("读取认识的人或可达会话失败", exc_info=True)
        return []

    intentions = _load_intentions(ctx)
    already = {
        ref
        for item in intentions.open_items(now=now.isoformat())
        if item.kind == _MISS_KIND
        for ref in item.refs
    }
    missed = people_to_miss(
        [p for p in people if f"user:{p.user_id}" not in already],
        now=now,
        today=today,
        reachable=labels,
        ledger=ledger,
        miss_after_days=miss_after,
        min_closeness=_option_float(ctx, "miss_min_closeness", _DEFAULT_MISS_MIN_CLOSENESS),
        gap_days=_option_int(ctx, "miss_again_after_days", _DEFAULT_MISS_AGAIN_DAYS, ceiling=365),
        max_per_day=max_per_day,
    )
    created: list[Intention] = []
    for item in missed:
        name = item.person.name
        chat = item.chats[0]
        intention = Intention.create(
            what=f"想找{name}聊聊，问候一下{name}最近怎么样",
            why=f"已经{item.days}天没见到{name}了，上次和{name}说话是在「{labels[chat]}」（{chat}）",
            resolution=RESOLUTION_TELL,
            kind=_MISS_KIND,
            urgency=_MISS_URGENCY,
            source="missing",
            origin_group=chat,
            refs=[f"user:{item.person.user_id}"],
            draft=True,
        )
        intentions.add(intention)
        ledger.record(item.person.user_id, today=today, now=now)
        created.append(intention)
        ctx.log_inner_thought(f"好久没见到{name}了，有点想找{name}聊聊")
    if created:
        _save_intentions(ctx, intentions)
        store.set(_MISS_STORE_KEY, ledger.to_dict())
        save = getattr(store, "save", None)
        if callable(save):
            save()
    return created


def _share_ready(state: dict[str, Any], now: datetime, ctx: Any) -> bool:
    cooldown = timedelta(
        seconds=_option_seconds(ctx, "share_cooldown_seconds", _DEFAULT_SHARE_COOLDOWN_SECONDS)
    )
    return now - _parse_time(state.get("last_share_at", "")) >= cooldown


def _next_share(intentions: IntentStore, now: datetime) -> Intention | None:
    ready = intentions.pending_shares(now=now.isoformat())
    return ready[0] if ready else None


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _fallback_group(ctx: Any) -> str:
    groups = [str(item) for item in ctx.get_active_groups() if str(item).strip()]
    return groups[-1] if groups else ""


def _load_intentions(ctx: Any) -> IntentStore:
    return IntentFileStore(ctx.get_work_path()).load()


def _save_intentions(ctx: Any, intentions: IntentStore) -> None:
    IntentFileStore(ctx.get_work_path()).save(intentions)


def _append_episode(ctx: Any, episode: Episode) -> None:
    path = _episodes_path(ctx)
    episodes = _read_episodes(path)
    episodes.append(episode.to_dict())
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"episodes": episodes[-_MAX_EPISODES:]})


def _read_episodes(path: Path) -> list[dict[str, Any]]:
    import json

    if not path.exists():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    episodes = loaded.get("episodes") if isinstance(loaded, dict) else None
    return list(episodes) if isinstance(episodes, list) else []


def _episodes_path(ctx: Any) -> Path:
    """Store episodes under ``memory/`` so the WebUI refreshes them for free."""
    from sirius_pulse.utils.layout import WorkspaceLayout

    layout = WorkspaceLayout(Path(ctx.get_work_path() or "."))
    return layout.memory_dir() / "autonomy" / "episodes.json"


async def _write_memory_unit(ctx: Any, group_id: str, episode: Episode, *, title: str = "") -> None:
    """Feed the outcome into existing memory so she *knows* it later.

    This makes the episode part of what she remembers; it is deliberately not a
    delivery channel.  Whether she mentions it is decided elsewhere.
    """
    persona = ctx.get_persona()
    subject = f"「{title}」" if title else episode.kind
    terms = [episode.kind, *([title] if title else []), *episode.refs]
    unit = MemoryUnit(
        unit_id=f"autonomy-{episode.episode_id}",
        group_id=group_id,
        created_at=episode.ended_at or episode.started_at,
        unit_type="event",
        scope="persona",
        scope_id=str(getattr(persona, "name", "") or ""),
        summary=f"（我自己的时间）我在做{subject}：{episode.outcome}",
        topics=[episode.kind, *([title] if title else [])],
        keywords=terms[:6],
        retrieval_terms=terms,
        event_time=episode.ended_at or episode.started_at,
        salience=_clamp(0.4 + episode.intensity * 0.4),
        lifespan="long",
        should_prompt=True,
        metadata={
            "origin": "self_initiated",
            "episode_id": episode.episode_id,
            "project_id": episode.project_id,
            "first_person_experience": True,
        },
    )
    try:
        await ctx.add_memory_unit(unit)
    except Exception as exc:
        logger.warning("自主事件写入人格记忆失败: %s", exc)


def _extract_refs(text: str) -> list[str]:
    return re.findall(r"https?://\S+", str(text or ""))[:5]


def _load_state(store: Any) -> dict[str, Any]:
    state = store.get("state", {})
    return dict(state) if isinstance(state, dict) else {}


def _save_state(store: Any, state: dict[str, Any]) -> None:
    store.set("state", state)
    save = getattr(store, "save", None)
    if callable(save):
        save()


def _option_seconds(
    ctx: Any, key: str, default: int, *, minimum: int = _MIN_INTERVAL_SECONDS
) -> float:
    store = ctx.get_data_store("autonomy")
    raw = store.get(key, default)
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return float(default)


def _option_float(ctx: Any, key: str, default: float) -> float:
    store = ctx.get_data_store("autonomy")
    try:
        return _clamp(float(store.get(key, default)))
    except (TypeError, ValueError):
        return default


def _option_int(ctx: Any, key: str, default: int, *, ceiling: int) -> int:
    store = ctx.get_data_store("autonomy")
    try:
        return max(0, min(ceiling, int(store.get(key, default))))
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
